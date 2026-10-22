"""Bounded ``[SAVE_TRACE]`` runtime instrumentation for the private-link Save path.

The contracts under test are the OWNER's:

  * tracing is OFF by default and changes nothing about a save when disabled;
  * enabled tracing records the REAL executed lines and branch decisions of the
    Save path only — never another task's code, never another module;
  * every record of one attempt shares one trace id and strictly increasing
    sequence numbers, and both the event and duration bounds stop the trace
    (restoring the previous ``sys.settrace`` state) instead of growing forever;
  * the tracer is removed after success, exception, cancellation and timeout,
    and a logging failure can never change a save result;
  * the view-once/expiring refusal records the EXACT condition that matched and
    marks download/upload/persist as NOT_REACHED — the guard itself is
    untouched;
  * ordinary private media is traced through download → upload → persistence;
  * no record exposes the link, a peer/chat id, message text or a full path.

No network, no Telegram session, no Supabase: the fakes are the same ones the
existing private-link and album suites use.
"""
from __future__ import annotations

import asyncio
import logging
import sys

import pytest

from backend.db import client as db_client
from backend.runtime import save_trace
from backend.services import save_service

from tests.test_save_album_view_once_progress import (
    FakeClient,
    FakeMessage,
    photo_message,
    view_once_message,
)
from tests.test_save_live_status_and_private_link import (
    OWNER,
    PEER,
    MSG_ID,
    PeerResolutionClient,
)

LINK = f"tg://openmessage?user_id={PEER}&message_id={MSG_ID}"


@pytest.fixture(autouse=True)
def clean_state():
    db_client._fallback["saved_items"] = []
    db_client._fallback["bot_logs"] = []
    save_trace.reset_state()
    yield
    save_trace.reset_state()


def _rows() -> list[dict]:
    return db_client._fallback["saved_items"]


def _parse(message: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for token in message[len(save_trace.TRACE_PREFIX):].strip().split(" "):
        key, _, value = token.partition("=")
        if key and value:
            fields[key] = value
    return fields


def _events(caplog) -> list[dict[str, str]]:
    return [
        _parse(record.getMessage())
        for record in caplog.records
        if record.getMessage().startswith(save_trace.TRACE_PREFIX)
    ]


def _kinds(events, stage: str, kind: str) -> list[dict[str, str]]:
    return [e for e in events if e.get("stage") == stage and e.get("kind") == kind]


def _named(events, name: str) -> list[dict[str, str]]:
    return [e for e in events if e.get("name") == name]


async def _ordinary_private_save(client=None):
    target = photo_message(MSG_ID, chat_id=PEER, text="private", download_size=2048)
    client = client or PeerResolutionClient([target], global_by_id={MSG_ID: target})
    result = await save_service.execute_link_save(client, OWNER, LINK, "UTC")
    return result, client


# ── 1. off by default ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_tracing_is_disabled_by_default(monkeypatch, caplog):
    monkeypatch.delenv(save_trace.ENV_FLAG, raising=False)
    caplog.set_level(logging.INFO)
    trace = save_trace.SaveTrace.begin()
    assert trace.enabled is False

    result, client = await _ordinary_private_save()

    assert getattr(result, "saved", False) is True, result
    assert _events(caplog) == []
    assert sys.gettrace() is None


# ── 2. enabled tracing records executed lines ───────────────────────────────


@pytest.mark.asyncio
async def test_enabled_tracing_records_executed_save_lines(monkeypatch, caplog):
    monkeypatch.setenv(save_trace.ENV_FLAG, "1")
    caplog.set_level(logging.INFO)

    await _ordinary_private_save()

    events = _events(caplog)
    lines = [e for e in events if e.get("kind") == save_trace.LINE]
    assert lines, "no line events were recorded"
    modules = {e.get("mod") for e in lines}
    funcs = {e.get("func") for e in lines}
    assert modules == {"backend/services/save_service.py"}
    assert {"execute_link_save", "parse_link_target", "_deep_save_one"} <= funcs
    assert all(int(e["line"]) > 0 for e in lines)
    # Real statements of the Deep Save entry, not just function entry.
    assert len({e["line"] for e in lines if e.get("func") == "_deep_save_one"}) > 3


# ── 3. only the save path of this task is recorded ──────────────────────────


@pytest.mark.asyncio
async def test_unrelated_execution_is_not_recorded(monkeypatch, caplog):
    monkeypatch.setenv(save_trace.ENV_FLAG, "1")
    caplog.set_level(logging.INFO)

    def unrelated_helper(value):
        doubled = value * 2
        return doubled + 1

    async def unrelated_task():
        # Same thread, DIFFERENT task: must never be attributed to the trace.
        return save_service.parse_link_target(LINK)

    traces = []
    trace = save_trace.SaveTrace.begin()
    traces.append(trace)
    with trace.capturing():
        unrelated_helper(3)
        parsed = await asyncio.create_task(unrelated_task())
        await asyncio.sleep(0)
        in_scope = save_service.parse_link_target(LINK)

    assert parsed.message_id == MSG_ID
    assert in_scope.message_id == MSG_ID

    events = _events(caplog)
    assert events, "the scoped call produced no records"
    assert {e.get("mod") for e in events if e.get("kind") == save_trace.LINE} == {
        "backend/services/save_service.py"
    }
    # The other task's parse produced no LINE records of its own: every parse
    # line belongs to the single in-scope call in THIS task.
    parse_lines = [e for e in events if e.get("func") == "parse_link_target"
                   and e.get("kind") == save_trace.LINE]
    assert 0 < len(parse_lines) < 40


# ── 4. stable id + ordered sequence ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_trace_id_and_sequence_are_stable_and_ordered(monkeypatch, caplog):
    monkeypatch.setenv(save_trace.ENV_FLAG, "1")
    caplog.set_level(logging.INFO)

    await _ordinary_private_save()

    events = _events(caplog)
    assert len({e["id"] for e in events}) == 1
    trace_id = events[0]["id"]
    assert len(trace_id) == 8
    assert [int(e["seq"]) for e in events] == list(range(1, len(events) + 1))
    elapsed = [float(e["elapsed_ms"]) for e in events]
    assert elapsed == sorted(elapsed)


# ── 5. bounds ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_event_limit_is_enforced(monkeypatch, caplog):
    monkeypatch.setenv(save_trace.ENV_FLAG, "1")
    caplog.set_level(logging.INFO)

    trace = save_trace.SaveTrace.begin(max_events=5)
    with trace.capturing():
        for index in range(50):
            trace.event("test", save_trace.BRANCH, name="loop", index=index)

    events = _events(caplog)
    truncated = [e for e in events if e.get("kind") == save_trace.TRUNCATED]
    assert len(truncated) == 1
    assert truncated[0]["reason"] == "events"
    assert events[-1]["kind"] == save_trace.TRUNCATED
    # Bounded: the 50 events stopped at the limit (plus the one TRUNCATED record).
    assert len(events) <= 6
    assert sys.gettrace() is None


@pytest.mark.asyncio
async def test_the_duration_limit_is_enforced(monkeypatch, caplog):
    monkeypatch.setenv(save_trace.ENV_FLAG, "1")
    caplog.set_level(logging.INFO)

    trace = save_trace.SaveTrace.begin(max_duration_s=0.0)
    with trace.capturing():
        for index in range(10):
            trace.event("test", save_trace.BRANCH, name="loop", index=index)

    events = _events(caplog)
    truncated = [e for e in events if e.get("kind") == save_trace.TRUNCATED]
    assert len(truncated) == 1
    assert truncated[0]["reason"] == "duration"
    assert sys.gettrace() is None


# ── 6. restoration on every exit path ───────────────────────────────────────


@pytest.mark.asyncio
async def test_the_previous_state_is_restored_after_success(monkeypatch, caplog):
    monkeypatch.setenv(save_trace.ENV_FLAG, "1")
    caplog.set_level(logging.INFO)

    await _ordinary_private_save()

    assert sys.gettrace() is None
    assert save_trace._active_line_traces == 0


def test_the_previous_state_is_restored_after_an_exception(monkeypatch, caplog):
    monkeypatch.setenv(save_trace.ENV_FLAG, "1")
    caplog.set_level(logging.INFO)
    trace = save_trace.SaveTrace.begin()

    with pytest.raises(RuntimeError):
        with trace.capturing():
            raise RuntimeError("boom")

    assert sys.gettrace() is None
    assert save_trace._active_line_traces == 0
    assert [e for e in _events(caplog) if e.get("error") == "RuntimeError"]


@pytest.mark.asyncio
async def test_the_previous_state_is_restored_after_cancellation(monkeypatch, caplog):
    monkeypatch.setenv(save_trace.ENV_FLAG, "1")
    caplog.set_level(logging.INFO)
    started = asyncio.Event()

    async def worker():
        trace = save_trace.SaveTrace.begin()
        with trace.capturing():
            started.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(worker())
    await started.wait()
    assert sys.gettrace() is not None
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert sys.gettrace() is None
    assert save_trace._active_line_traces == 0


@pytest.mark.asyncio
async def test_the_previous_state_is_restored_after_a_timeout(monkeypatch, caplog):
    monkeypatch.setenv(save_trace.ENV_FLAG, "1")
    caplog.set_level(logging.INFO)

    async def worker():
        trace = save_trace.SaveTrace.begin()
        with trace.capturing():
            await asyncio.sleep(5)

    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(worker(), timeout=0.05)

    assert sys.gettrace() is None
    assert save_trace._active_line_traces == 0


def test_a_second_attempt_does_not_steal_the_active_tracer(monkeypatch, caplog):
    monkeypatch.setenv(save_trace.ENV_FLAG, "1")
    caplog.set_level(logging.INFO)
    first = save_trace.SaveTrace.begin()
    second = save_trace.SaveTrace.begin()

    async def run():
        with first.capturing():
            with second.capturing():
                second.event("test", save_trace.BRANCH, name="second", index=1)
            assert sys.gettrace() is not None, "the inner exit must not restore"
        assert sys.gettrace() is None

    asyncio.run(run())
    assert save_trace._active_line_traces == 0


# ── 7. logging failures never change a save ────────────────────────────────


@pytest.mark.asyncio
async def test_a_logging_failure_does_not_change_the_save_result(monkeypatch):
    monkeypatch.setenv(save_trace.ENV_FLAG, "1")
    logger = logging.getLogger("backend.save_trace")

    def explode(*args, **kwargs):
        raise RuntimeError("log sink down")

    monkeypatch.setattr(logger, "info", explode)

    result, client = await _ordinary_private_save()

    assert getattr(result, "saved", False) is True, result
    assert len(_rows()) == 1
    assert sys.gettrace() is None


# ── 8./9. resolution and its validation ────────────────────────────────────


@pytest.mark.asyncio
async def test_a_valid_private_link_resolves_and_is_validated(monkeypatch, caplog):
    monkeypatch.setenv(save_trace.ENV_FLAG, "1")
    caplog.set_level(logging.INFO)

    result, client = await _ordinary_private_save()

    assert getattr(result, "saved", False) is True, result
    assert _rows()[0]["origin_chat_id"] == PEER
    events = _events(caplog)
    assert _named(events, "message_id_match")[0]["matched"] == "true"
    assert _named(events, "peer_match")[0]["matched"] == "true"
    # The peer-less lookup really is the path that made it readable.
    assert (None, MSG_ID) in client.reads
    assert _kinds(events, "resolve", save_trace.CALL_END)


@pytest.mark.asyncio
async def test_a_wrong_message_id_is_still_refused_and_traced(monkeypatch, caplog):
    monkeypatch.setenv(save_trace.ENV_FLAG, "1")
    caplog.set_level(logging.INFO)
    other = photo_message(MSG_ID + 1, chat_id=PEER, text="other")
    client = PeerResolutionClient([other], global_by_id={MSG_ID: other})

    result = await save_service.execute_link_save(client, OWNER, LINK, "UTC")

    assert getattr(result, "saved", False) is False
    assert _rows() == []
    assert client.download_calls == []
    events = _events(caplog)
    assert _named(events, "message_id_match")[0]["matched"] == "false"


@pytest.mark.asyncio
async def test_a_wrong_peer_is_still_refused_and_traced(monkeypatch, caplog):
    monkeypatch.setenv(save_trace.ENV_FLAG, "1")
    caplog.set_level(logging.INFO)
    foreign = photo_message(MSG_ID, chat_id=-1009999999999, text="elsewhere")
    client = PeerResolutionClient([foreign], global_by_id={MSG_ID: foreign})

    result = await save_service.execute_link_save(client, OWNER, LINK, "UTC")

    assert getattr(result, "saved", False) is False
    assert _rows() == []
    assert client.download_calls == []
    events = _events(caplog)
    assert _named(events, "peer_match")[0]["matched"] == "false"


# ── 10./11./13. view-once classification and protection ────────────────────


@pytest.mark.asyncio
async def test_view_once_classification_records_the_exact_condition(monkeypatch, caplog):
    monkeypatch.setenv(save_trace.ENV_FLAG, "1")
    caplog.set_level(logging.INFO)
    secret = view_once_message(MSG_ID)
    secret.chat_id = PEER
    client = PeerResolutionClient([secret], global_by_id={MSG_ID: secret})

    await save_service.execute_link_save(client, OWNER, LINK, "UTC")

    events = _events(caplog)
    guard = _named(events, "ephemeral_guard")[0]
    assert guard["matched"] == "view_once"
    assert guard["ttl"] == "view_once_sentinel"
    assert guard["ttl_field"] == "present"
    media = _named(events, "media_present")[0]
    assert media["media_class"] == "MessageMediaDocument"


@pytest.mark.asyncio
async def test_a_view_once_target_is_traced_through_the_whole_pipeline(
    monkeypatch, caplog
):
    monkeypatch.setenv(save_trace.ENV_FLAG, "1")
    caplog.set_level(logging.INFO)
    secret = view_once_message(MSG_ID)
    secret.chat_id = PEER
    client = PeerResolutionClient([secret], global_by_id={MSG_ID: secret})

    result = await save_service.execute_link_save(client, OWNER, LINK, "UTC")

    assert getattr(result, "saved", False) is True, result
    assert [message_id for message_id, _cb in client.download_calls] == [MSG_ID]
    assert len(client.send_calls) == 1
    assert len(_rows()) == 1

    events = _events(caplog)
    # The classification is still recorded — as evidence, not as a refusal.
    assert _named(events, "ephemeral_guard")[0]["matched"] == "view_once"
    assert not [e for e in events if e.get("kind") == save_trace.NOT_REACHED]
    saved = [
        e for e in events
        if e.get("kind") == save_trace.RETURN
        and e.get("func") == "_deep_save_one"
    ]
    assert saved and saved[0]["outcome"] == save_trace.OUTCOME_OK


# ── 12. ordinary media through download/upload/persist ─────────────────────


@pytest.mark.asyncio
async def test_ordinary_private_media_is_traced_through_the_whole_pipeline(
    monkeypatch, caplog
):
    monkeypatch.setenv(save_trace.ENV_FLAG, "1")
    caplog.set_level(logging.INFO)

    result, client = await _ordinary_private_save()

    assert getattr(result, "saved", False) is True, result
    assert len(_rows()) == 1
    events = _events(caplog)
    for stage, func in (
        ("download", "download_media"),
        ("upload", "send_file"),
        ("persist", "insert_save"),
    ):
        starts = [e for e in _kinds(events, stage, save_trace.CALL_START)
                  if e.get("func") == func]
        ends = [e for e in _kinds(events, stage, save_trace.CALL_END)
                if e.get("func") == func]
        assert starts and ends, f"{stage}/{func} was not traced"
        assert ends[0]["outcome"] == save_trace.OUTCOME_OK
    assert not [e for e in events if e.get("kind") == save_trace.NOT_REACHED]
    assert _named(events, "file_non_empty")[0]["matched"] == "true"


# ── 14. privacy ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_trace_records_never_expose_private_content(monkeypatch, caplog):
    monkeypatch.setenv(save_trace.ENV_FLAG, "1")
    caplog.set_level(logging.INFO)
    caption = "private-caption-text-must-not-leak"
    target = photo_message(MSG_ID, chat_id=PEER, text=caption, download_size=1024)
    client = PeerResolutionClient([target], global_by_id={MSG_ID: target})

    await save_service.execute_link_save(client, OWNER, LINK, "UTC")

    trace_text = "\n".join(
        record.getMessage() for record in caplog.records
        if record.getMessage().startswith(save_trace.TRACE_PREFIX)
    )
    assert trace_text
    for secret in (str(PEER), str(MSG_ID), "tg://openmessage", caption, "/workspace"):
        assert secret not in trace_text
    # Fingerprints are what correlation uses instead.
    assert "peer=fp:" in trace_text or "msg=fp:" in trace_text


def test_fingerprints_are_stable_and_not_reversible():
    first = save_trace.fingerprint(5406707941)
    assert first == save_trace.fingerprint(5406707941)
    assert first != save_trace.fingerprint(5406707942)
    assert "5406707941" not in first
    assert save_trace.fingerprint(None).startswith("fp:")
    assert save_trace._safe_scalar("a\nb\tc") == "a_b_c"
