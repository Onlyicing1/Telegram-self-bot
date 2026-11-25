"""Focused tests for the opt-in outbound egress attribution layer.

Everything here is offline: no Telegram, Supabase or provider request is made.
"""
from __future__ import annotations

import asyncio
import logging
import pathlib
import threading
import time

import httpx
import pytest

from backend.observability import egress_attribution as egress


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    monkeypatch.delenv(egress.ENV_FLAG, raising=False)
    egress.reset()
    yield
    egress.reset()


def _enable(monkeypatch) -> None:
    monkeypatch.setenv(egress.ENV_FLAG, "1")


def _totals() -> dict:
    return egress.snapshot()["totals"]


# ── opt-in flag ─────────────────────────────────────────────────────────────


def test_measurement_is_disabled_by_default():
    assert egress.enabled() is False

    egress.record_exact("ai_provider", 100, label="openai")
    egress.record_unmeasured("stt_provider", label="groq")
    egress.record_estimated("tts_provider", 50, label="gemini")

    assert _totals() == {
        "exact_payload_bytes": 0,
        "estimated_payload_bytes": 0,
        "exact_inbound_bytes": 0,
        "operation_count": 0,
        "inbound_operation_count": 0,
        "failed_operation_count": 0,
        "unmeasured_operations": 0,
        "unmeasured_inbound_operations": 0,
        "incomplete_inbound_streams": 0,
    }


@pytest.mark.parametrize("value", ["", "0", "true", "TRUE", "yes", "on", " 1", "1 ", "2"])
def test_only_exactly_one_enables_measurement(monkeypatch, value):
    monkeypatch.setenv(egress.ENV_FLAG, value)

    assert egress.enabled() is False
    assert egress.async_client_kwargs("ai_provider", "openai") == {}
    assert egress.sync_client_kwargs("supabase_rest", "postgrest") == {}
    assert egress.request_category("ai_discovery", "gemini") == {}


def test_exactly_one_enables_measurement(monkeypatch):
    _enable(monkeypatch)

    assert egress.enabled() is True
    assert egress.async_client_kwargs("ai_provider", "openai")


# ── aggregation, flush interval, reset ──────────────────────────────────────


def test_aggregation_flushes_at_most_once_per_interval(monkeypatch, caplog):
    _enable(monkeypatch)
    caplog.set_level(logging.INFO, logger=egress.__name__)

    egress.record_exact("ai_provider", 100, label="openai")
    egress.record_exact("ai_provider", 100, label="openai")

    # Inside the interval: no summary yet, counters keep aggregating.
    assert [r for r in caplog.records if "[RENDER_EGRESS]" in r.getMessage()] == []
    assert _totals()["exact_payload_bytes"] == 200

    # Push the window past the interval; the NEXT recorded event flushes.
    egress._last_flush = time.monotonic() - (egress.FLUSH_INTERVAL_SECONDS + 1)
    egress.record_exact("ai_provider", 50, label="openai")

    lines = [r.getMessage() for r in caplog.records if "[RENDER_EGRESS]" in r.getMessage()]
    assert len(lines) == 2  # one category/label line + one totals line
    assert "category=ai_provider" in lines[0]
    assert "label=openai" in lines[0]
    assert "exact_payload_bytes=250" in lines[0]
    assert "operation_count=3" in lines[0]
    assert "totals" in lines[1]
    assert "note=application_payload_only_not_render_bandwidth" in lines[1]

    # The window was reset by the flush.
    assert _totals()["exact_payload_bytes"] == 0


def test_no_summary_is_emitted_without_activity(monkeypatch, caplog):
    _enable(monkeypatch)
    caplog.set_level(logging.INFO, logger=egress.__name__)
    egress._last_flush = time.monotonic() - (egress.FLUSH_INTERVAL_SECONDS + 1)

    assert egress.flush() is True

    assert [r for r in caplog.records if "[RENDER_EGRESS]" in r.getMessage()] == []


def test_one_line_per_category_and_label_not_per_operation(monkeypatch, caplog):
    _enable(monkeypatch)
    caplog.set_level(logging.INFO, logger=egress.__name__)

    for _ in range(100):
        egress.record_exact("telegram_self_upload", 10, label="deep_save")

    assert egress.flush() is True

    lines = [r.getMessage() for r in caplog.records if "[RENDER_EGRESS]" in r.getMessage()]
    assert len(lines) == 2
    assert "exact_payload_bytes=1000" in lines[0]
    assert "operation_count=100" in lines[0]


def test_snapshot_is_process_scoped_and_does_not_reset(monkeypatch):
    _enable(monkeypatch)
    egress.record_exact("supabase_rest", 7, label="postgrest")

    first = egress.snapshot()
    second = egress.snapshot()

    assert first["counters_scope"] == "process"
    assert first["flush_interval_seconds"] == 300.0
    assert first["window_started_utc"].endswith("Z")
    assert first["captured_at_utc"].endswith("Z")
    assert first["categories"]["supabase_rest"]["postgrest"]["exact_payload_bytes"] == 7
    assert second["totals"]["exact_payload_bytes"] == 7


def test_counters_reset_on_demand(monkeypatch):
    _enable(monkeypatch)
    egress.record_exact("render_api", 3, label="render")

    egress.reset()

    assert _totals()["exact_payload_bytes"] == 0
    assert _totals()["operation_count"] == 0


def test_counter_updates_are_thread_safe(monkeypatch):
    _enable(monkeypatch)
    workers = 8
    per_worker = 200

    def _work():
        for _ in range(per_worker):
            egress.record_exact("telegram_self_upload", 10, label="deep_save")

    threads = [threading.Thread(target=_work) for _ in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    totals = _totals()
    assert totals["exact_payload_bytes"] == workers * per_worker * 10
    assert totals["operation_count"] == workers * per_worker


def test_cardinality_is_bounded_by_a_closed_label_vocabulary(monkeypatch):
    _enable(monkeypatch)

    egress.record_exact(
        "ai_provider", 5, label="https://api.example.com/v1?key=SECRET-CHAT-ID-42"
    )
    egress.record_exact("ai_provider", 5, label="../../etc/passwd")
    egress.record_exact("not_a_category", 5, label="openai")

    categories = egress.snapshot()["categories"]
    assert set(categories) == {"ai_provider", "other_known_http"}
    assert set(categories["ai_provider"]) == {"other"}
    assert set(categories["other_known_http"]) == {"openai"}


# ── Telegram uploads ────────────────────────────────────────────────────────


def test_cumulative_progress_is_counted_once(monkeypatch):
    _enable(monkeypatch)
    tracker = egress.telegram_upload("telegram_self_upload", "deep_save")

    with tracker:
        tracker.callback(100, 300)
        tracker.callback(200, 300)
        tracker.callback(300, 300)

    bucket = egress.snapshot()["categories"]["telegram_self_upload"]["deep_save"]
    assert bucket["exact_payload_bytes"] == 300
    assert bucket["operation_count"] == 1
    assert bucket["failed_operation_count"] == 0


def test_a_restarted_upload_is_not_double_counted(monkeypatch):
    _enable(monkeypatch)
    tracker = egress.telegram_upload("telegram_self_upload", "deep_save")

    with tracker:
        tracker.callback(100, 300)
        tracker.callback(200, 300)
        tracker.callback(50, 300)   # Telethon restarted the upload from zero
        tracker.callback(300, 300)

    bucket = egress.snapshot()["categories"]["telegram_self_upload"]["deep_save"]
    assert bucket["exact_payload_bytes"] == 300
    assert bucket["operation_count"] == 1


def test_a_failed_upload_keeps_its_bytes_and_is_marked_failed(monkeypatch):
    _enable(monkeypatch)
    tracker = egress.telegram_upload("telegram_self_upload", "album_member")

    with pytest.raises(RuntimeError):
        with tracker:
            tracker.callback(120, 1000)
            raise RuntimeError("upload failed")

    bucket = egress.snapshot()["categories"]["telegram_self_upload"]["album_member"]
    assert bucket["exact_payload_bytes"] == 120
    assert bucket["operation_count"] == 1
    assert bucket["failed_operation_count"] == 1


def test_a_cancelled_upload_is_marked_failed(monkeypatch):
    _enable(monkeypatch)
    tracker = egress.telegram_upload("telegram_self_upload", "voice")

    with pytest.raises(asyncio.CancelledError):
        with tracker:
            tracker.callback(64, 128)
            raise asyncio.CancelledError()

    bucket = egress.snapshot()["categories"]["telegram_self_upload"]["voice"]
    assert bucket["exact_payload_bytes"] == 64
    assert bucket["failed_operation_count"] == 1


def test_an_attempt_without_progress_is_unmeasured(monkeypatch):
    _enable(monkeypatch)
    tracker = egress.telegram_upload("telegram_self_upload", "rename_reupload")

    with pytest.raises(OSError):
        with tracker:
            raise OSError("never started")

    bucket = egress.snapshot()["categories"]["telegram_self_upload"]["rename_reupload"]
    assert bucket["exact_payload_bytes"] == 0
    assert bucket["unmeasured_operations"] == 1
    assert bucket["failed_operation_count"] == 1


@pytest.mark.asyncio
async def test_the_tracker_composes_with_the_callers_own_callback(monkeypatch):
    _enable(monkeypatch)
    seen = []

    async def inner(current, total=None):
        seen.append((current, total))

    tracker = egress.telegram_upload("telegram_self_upload", "deep_save")
    combined = tracker.wrap(inner)

    await combined(10, 10)

    assert seen == [(10, 10)]
    assert egress.snapshot()["categories"]["telegram_self_upload"]["deep_save"][
        "exact_payload_bytes"
    ] == 10


def test_disabled_tracking_returns_the_caller_callback_untouched():
    async def inner(current, total=None):
        return None

    tracker = egress.telegram_upload("telegram_self_upload", "voice")

    assert tracker.wrap(inner) is inner
    assert tracker.wrap(None) is None


# ── HTTP request bodies ─────────────────────────────────────────────────────


def _async_transport(handler):
    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_provider_request_body_is_measured_exactly(monkeypatch):
    _enable(monkeypatch)
    sent = {}

    def handler(request):
        sent["body"] = request.content
        return httpx.Response(200, json={"ok": True})

    async with httpx.AsyncClient(
        transport=_async_transport(handler),
        **egress.async_client_kwargs("ai_provider", "openai"),
    ) as client:
        await client.post("https://provider.test/chat/completions", json={"a": 1})

    bucket = egress.snapshot()["categories"]["ai_provider"]["openai"]
    assert sent["body"] == b'{"a": 1}'
    assert bucket["exact_payload_bytes"] == len(sent["body"])
    assert bucket["operation_count"] == 1


@pytest.mark.asyncio
async def test_response_bytes_are_never_counted_as_outbound(monkeypatch):
    _enable(monkeypatch)

    def handler(request):
        return httpx.Response(200, content=b"x" * 500_000)

    async with httpx.AsyncClient(
        transport=_async_transport(handler),
        **egress.async_client_kwargs("ai_provider", "gemini"),
    ) as client:
        response = await client.post("https://provider.test/generate", json={"a": 1})

    bucket = egress.snapshot()["categories"]["ai_provider"]["gemini"]
    assert len(response.content) == 500_000
    assert bucket["exact_payload_bytes"] == 8
    assert bucket["operation_count"] == 1


@pytest.mark.asyncio
async def test_multipart_upload_body_length_is_measured(monkeypatch):
    _enable(monkeypatch)
    lengths = []

    def handler(request):
        lengths.append(int(request.headers["content-length"]))
        return httpx.Response(200, json={"ok": True})

    async with httpx.AsyncClient(
        transport=_async_transport(handler),
        **egress.async_client_kwargs("stt_provider", "groq"),
    ) as client:
        await client.post(
            "https://provider.test/transcribe",
            files={"file": ("audio.ogg", b"0123456789", "audio/ogg")},
        )

    bucket = egress.snapshot()["categories"]["stt_provider"]["groq"]
    assert bucket["exact_payload_bytes"] == lengths[0]
    assert bucket["unmeasured_operations"] == 0


@pytest.mark.asyncio
async def test_a_streamed_request_body_is_preserved_and_reported_unmeasured(monkeypatch):
    _enable(monkeypatch)
    received = {}

    class _Transport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            chunks = [chunk async for chunk in request.stream]
            received["body"] = b"".join(chunks)
            return httpx.Response(200, json={"ok": True})

    async def _body():
        yield b"abc"
        yield b"def"

    async with httpx.AsyncClient(
        transport=_Transport(),
        **egress.async_client_kwargs("ai_provider", "openai"),
    ) as client:
        await client.post("https://provider.test/stream", content=_body())

    bucket = egress.snapshot()["categories"]["ai_provider"]["openai"]
    assert received["body"] == b"abcdef"
    assert bucket["exact_payload_bytes"] == 0
    assert bucket["unmeasured_operations"] == 1
    assert bucket["operation_count"] == 1


@pytest.mark.asyncio
async def test_an_error_response_marks_the_operation_failed(monkeypatch):
    _enable(monkeypatch)

    def handler(request):
        return httpx.Response(500, json={"error": "boom"})

    async with httpx.AsyncClient(
        transport=_async_transport(handler),
        **egress.async_client_kwargs("ai_provider", "openai"),
    ) as client:
        await client.post("https://provider.test/chat", json={"a": 1})

    bucket = egress.snapshot()["categories"]["ai_provider"]["openai"]
    assert bucket["failed_operation_count"] == 1
    assert bucket["operation_count"] == 1


@pytest.mark.asyncio
async def test_a_shared_client_can_override_its_category_per_request(monkeypatch):
    _enable(monkeypatch)

    def handler(request):
        return httpx.Response(200, json={"data": []})

    async with httpx.AsyncClient(
        transport=_async_transport(handler),
        **egress.async_client_kwargs("ai_provider", "openai"),
    ) as client:
        await client.post("https://provider.test/chat", json={"a": 1})
        await client.get(
            "https://provider.test/models",
            **egress.request_category("ai_discovery", "openai"),
        )

    categories = egress.snapshot()["categories"]
    assert categories["ai_provider"]["openai"]["exact_payload_bytes"] == 8
    assert categories["ai_discovery"]["openai"]["exact_payload_bytes"] == 0
    assert categories["ai_discovery"]["openai"]["operation_count"] == 1


def test_supabase_postgrest_session_is_http1_with_the_egress_hooks(monkeypatch):
    from backend.db.transport import HTTP1PostgrestClient, HTTP1Session

    _enable(monkeypatch)
    client = HTTP1PostgrestClient("http://localhost:54321", schema="public")
    session = client.session

    assert isinstance(session, HTTP1Session)
    assert session._event_hooks["request"]

    measured = {}

    def handler(request):
        measured["body"] = request.content
        return httpx.Response(201, json=[])

    session._transport = httpx.MockTransport(handler)
    session.post("/rest/v1/saved_items", json={"a": 1})

    bucket = egress.snapshot()["categories"]["supabase_rest"]["postgrest"]
    assert measured["body"] == b'{"a": 1}'
    assert bucket["exact_payload_bytes"] == 8
    assert bucket["operation_count"] == 1


def test_supabase_postgrest_session_has_no_hooks_when_disabled():
    from backend.db.transport import HTTP1PostgrestClient

    client = HTTP1PostgrestClient("http://localhost:54321", schema="public")

    assert client.session._event_hooks["request"] == []


# ── no behavior change, no lifecycle, no leakage ───────────────────────────


@pytest.mark.asyncio
async def test_disabled_instrumentation_changes_nothing_about_the_request():
    def handler(request):
        return httpx.Response(200, json={"ok": True})

    kwargs = egress.async_client_kwargs("ai_provider", "openai")
    assert kwargs == {}

    async with httpx.AsyncClient(transport=_async_transport(handler), **kwargs) as client:
        response = await client.post("https://provider.test/chat", json={"a": 1})

    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert _totals()["operation_count"] == 0


def test_no_background_task_or_persistence_is_created():
    source = pathlib.Path(egress.__file__).read_text(encoding="utf-8").lower()

    for forbidden in (
        "asyncio",
        "create_task",
        "ensure_future",
        "call_later",
        "call_at",
        "threading.thread",
        "sqlite",
    ):
        assert forbidden not in source


def test_no_secret_or_user_content_reaches_a_log_line(monkeypatch, caplog):
    _enable(monkeypatch)
    caplog.set_level(logging.INFO, logger=egress.__name__)

    egress.record_exact("ai_provider", 12, label="Bearer sk-live-SECRET-TOKEN")
    egress.record_exact("telegram_self_upload", 34, label="chat-123456789")
    assert egress.flush() is True

    text = caplog.text
    assert "SECRET" not in text
    assert "chat-123456789" not in text
    assert "Bearer" not in text
    assert text.count("label=other") == 2


# ── downloads, inbound responses, lifecycle, month reconciliation ──────────


def test_telegram_download_path_and_bytes_are_counted_once(monkeypatch, tmp_path):
    _enable(monkeypatch)
    payload = tmp_path / "media.bin"
    payload.write_bytes(b"x" * 123)

    egress.record_telegram_download(payload, label="save_download")
    egress.record_telegram_download(b"abcde", label="download")

    bucket = egress.snapshot()["categories"][egress.TELEGRAM_DOWNLOAD]
    assert bucket["save_download"]["exact_inbound_bytes"] == 123
    assert bucket["save_download"]["inbound_operation_count"] == 1
    assert bucket["save_download"]["exact_payload_bytes"] == 0
    assert bucket["download"]["exact_inbound_bytes"] == 5


def test_a_missing_download_is_unmeasured_not_zero(monkeypatch):
    _enable(monkeypatch)

    egress.record_telegram_download(
        "/definitely/not/a/file", label="download", failed=1
    )

    bucket = egress.snapshot()["categories"][egress.TELEGRAM_DOWNLOAD]["download"]
    assert bucket["exact_inbound_bytes"] == 0
    assert bucket["unmeasured_inbound_operations"] == 1
    assert bucket["incomplete_inbound_streams"] == 1
    assert bucket["failed_operation_count"] == 1


def test_a_repeated_download_is_a_new_operation(monkeypatch):
    _enable(monkeypatch)

    egress.record_telegram_download(b"12345", label="download")
    egress.record_telegram_download(b"12345", label="download")

    bucket = egress.snapshot()["categories"][egress.TELEGRAM_DOWNLOAD]["download"]
    assert bucket["exact_inbound_bytes"] == 10
    assert bucket["inbound_operation_count"] == 2


@pytest.mark.asyncio
async def test_response_body_is_counted_as_inbound_not_outbound(monkeypatch):
    _enable(monkeypatch)

    def handler(request):
        return httpx.Response(200, content=b"y" * 500_000)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        **egress.async_client_kwargs("ai_provider", "gemini"),
    ) as client:
        await client.post("https://provider.test/generate", json={"a": 1})

    bucket = egress.snapshot()["categories"]["ai_provider"]["gemini"]
    assert bucket["exact_payload_bytes"] == 8
    assert bucket["exact_inbound_bytes"] == 500_000
    assert bucket["inbound_operation_count"] == 1
    assert bucket["incomplete_inbound_streams"] == 0


class _ChunkedAsyncStream(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield b"abc"
        yield b"def"


class _StreamingTransport(httpx.AsyncBaseTransport):
    async def handle_async_request(self, request):
        return httpx.Response(200, stream=_ChunkedAsyncStream())


@pytest.mark.asyncio
async def test_a_consumed_stream_is_measured_without_being_buffered(monkeypatch):
    _enable(monkeypatch)

    async with httpx.AsyncClient(
        transport=_StreamingTransport(),
        **egress.async_client_kwargs("ai_provider", "openai"),
    ) as client:
        async with client.stream("GET", "https://provider.test/stream") as response:
            body = b"".join([chunk async for chunk in response.aiter_bytes()])

    bucket = egress.snapshot()["categories"]["ai_provider"]["openai"]
    assert body == b"abcdef"
    assert bucket["exact_inbound_bytes"] == 6
    assert bucket["incomplete_inbound_streams"] == 0


@pytest.mark.asyncio
async def test_an_early_closed_stream_records_only_consumed_bytes(monkeypatch):
    _enable(monkeypatch)

    async with httpx.AsyncClient(
        transport=_StreamingTransport(),
        **egress.async_client_kwargs("ai_provider", "openai"),
    ) as client:
        async with client.stream("GET", "https://provider.test/stream") as response:
            iterator = response.aiter_bytes()
            assert await iterator.__anext__() == b"abc"

    bucket = egress.snapshot()["categories"]["ai_provider"]["openai"]
    assert bucket["exact_inbound_bytes"] == 3
    assert bucket["incomplete_inbound_streams"] == 1


def test_month_totals_survive_a_window_flush(monkeypatch):
    _enable(monkeypatch)

    egress.record_exact("telegram_self_upload", 10, label="deep_save")
    assert egress.flush() is True

    snapshot = egress.snapshot()
    assert snapshot["totals"]["exact_payload_bytes"] == 0
    assert snapshot["month_totals"]["exact_payload_bytes"] == 10
    assert snapshot["month"] == egress._month_period


def test_idle_heartbeat_emits_one_bounded_status(monkeypatch, caplog):
    _enable(monkeypatch)
    caplog.set_level(logging.INFO, logger=egress.__name__)
    egress._last_status = time.monotonic() - (egress.STATUS_INTERVAL_SECONDS + 1)

    assert egress.heartbeat_tick() is True
    assert egress.heartbeat_tick() is False

    lines = [r.getMessage() for r in caplog.records if "[RENDER_EGRESS]" in r.getMessage()]
    assert len(lines) == 1
    assert "status=enabled_no_activity" in lines[0]
    assert "scope=process" in lines[0]


def test_heartbeat_flushes_activity_with_the_policy_snapshot(monkeypatch, caplog):
    _enable(monkeypatch)
    caplog.set_level(logging.INFO, logger=egress.__name__)
    egress.record_exact("ai_provider", 42, label="openai")
    egress._last_flush = time.monotonic() - (egress.FLUSH_INTERVAL_SECONDS + 1)

    assert egress.heartbeat_tick({"tier": 50, "trusted": True}) is True

    text = caplog.text
    assert "category=ai_provider" in text
    assert "policy_tier=50" in text
    assert "policy_trusted=true" in text


def test_startup_and_shutdown_logs_are_sanitized(monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger=egress.__name__)

    egress.log_startup()
    assert "startup enabled=false status=disabled" in caplog.text

    _enable(monkeypatch)
    egress.log_startup()
    assert "startup enabled=true status=recording" in caplog.text

    egress.record_exact("render_api", 3, label="render")
    assert egress.shutdown() is True
    assert "shutdown flushed=true" in caplog.text


def test_snapshot_distinguishes_the_measurement_states(monkeypatch):
    _enable(monkeypatch)
    assert egress.snapshot()["status"] == "enabled_no_activity"

    egress.record_exact("telegram_download", 5, label="download")
    assert egress.snapshot()["status"] == "recording"

    egress.reset()
    _enable(monkeypatch)
    egress.record_unmeasured("telegram_download", label="download")
    assert egress.snapshot()["status"] == "measurement_incomplete"

    egress.reset()
    _enable(monkeypatch)
    egress.record_measurement_failure()
    assert egress.snapshot()["status"] == "measurement_failure"


def test_flush_reports_month_reconciliation_without_claiming_a_cause(
    monkeypatch, caplog
):
    _enable(monkeypatch)
    caplog.set_level(logging.INFO, logger=egress.__name__)
    egress.record_exact("telegram_self_upload", 100, label="deep_save")

    assert egress.flush({
        "render_monthly_bytes": 250,
        "governor_monthly_bytes": 80,
        "policy_floor_bytes": 0,
        "tier": 0,
        "trusted": True,
        "render_partial": False,
    }) is True

    text = caplog.text
    assert "render_monthly_bytes=250" in text
    assert "governor_monthly_bytes=80" in text
    assert "month_exact_payload_bytes=100" in text
    assert "unattributed_difference_bytes=150" in text
    assert "policy_tier=0" in text
    assert "policy_trusted=true" in text
    assert "render_partial=false" in text
