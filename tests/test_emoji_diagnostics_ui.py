"""
The ONE traceable Emoji diagnostic workflow — the panel's primary action.

Pins the unified integration: the primary panel exposes exactly one diagnostic
button (``🧪 Run Emoji Diagnostics``), the redundant standalone diagnostic
buttons are gone while the useful product actions stay, clicking the button
runs the REAL production inline route, and the rendered trace reflects the
route's actual outcome (which stage succeeded, where it stopped and why).

No live Telegram and no Supabase call is made: the Telegram boundary is faked
at the surface the service consumes (``inline_query`` / ``click`` /
``get_messages`` / ``get_me``), exactly as the service's own suites do.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from telethon import types
from telethon.tl import custom as telethon_custom

import backend.helper.client as helper_client
from backend.bot.handlers import emoji
from backend.helper import inline_engine, input_state
from backend.helper.panels import get_action
from backend.services import premium_emoji_diagnostics as diag
from backend.services import premium_emoji_inline_service as svc
from backend.telegram_api._helpers import utf16_length

OWNER = 7770001
BOT_ID = 5550001
DOC = 5361626279781934801
ALT = "😵"
PREFIX = svc.PREFIX
SELECTION = 909
REPLY_ID = 910
SENT_ID = 4242
HELPER_USER = "nitro_selfbot"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    diag.reset_runs()
    input_state.clear_all()
    inline_engine.set_self_client(None)
    inline_engine.set_owner_id(OWNER)
    inline_engine.set_helper_username(HELPER_USER)
    inline_engine.set_helper_id(BOT_ID)
    helper_client._bot_id = BOT_ID
    monkeypatch.setattr(helper_client, "is_available", lambda: True)
    yield
    diag.reset_runs()
    input_state.clear_all()
    helper_client._bot_id = 0
    inline_engine.set_helper_username("")
    inline_engine.set_helper_id(0)
    inline_engine.set_self_client(None)


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _datas(buttons) -> list[str]:
    out = []
    for row in buttons:
        for button in row:
            raw = getattr(button, "data", button)
            out.append(raw.decode("utf-8") if isinstance(raw, bytes) else str(raw))
    return out


def _texts(buttons) -> list[str]:
    return [str(getattr(button, "text", button)) for row in buttons for button in row]


# ── telegram-level fakes ─────────────────────────────────────────────────────


def _entity(document_id: int = DOC, offset: int = 0, length: int = 2):
    return types.MessageEntityCustomEmoji(offset, length, document_id)


class _FakeMessage:
    def __init__(
        self,
        text: str,
        entities: list[Any] | None = None,
        *,
        message_id: int = SENT_ID,
        via_bot_id: int | None = None,
    ) -> None:
        self.message = text
        self.text = text
        self.entities = entities or []
        self.id = message_id
        self.via_bot_id = via_bot_id


def _bot_inline_result(text: str, entities: list[Any] | None) -> types.BotInlineResult:
    return types.BotInlineResult(
        id="0",
        type="article",
        title="Premium emoji",
        send_message=types.BotInlineMessageText(
            message=text,
            entities=list(entities) if entities is not None else None,
        ),
    )


class _FakeInlineResult(telethon_custom.InlineResult):
    """The REAL Telethon wrapper with only ``click`` stubbed."""

    def __init__(
        self,
        text: str,
        entities: list[Any] | None,
        *,
        message_id: int = SENT_ID,
        via_bot_id: int | None = BOT_ID,
    ) -> None:
        super().__init__(None, _bot_inline_result(text, entities), query_id=7)
        self.clicks = 0
        self._message_id = message_id
        self._via_bot_id = via_bot_id

    async def click(self, chat_id):
        self.clicks += 1
        send_message = self.result.send_message
        return _FakeMessage(
            getattr(send_message, "message", "") or "",
            getattr(send_message, "entities", None) or [],
            message_id=self._message_id,
            via_bot_id=self._via_bot_id,
        )


class _FakeSelfClient:
    """The surface the workflow consumes on the self client."""

    def __init__(self, *, results=None, messages=None, answer_results=None) -> None:
        self.results = results
        self.messages = messages or {}
        self.answer_results = answer_results
        self.inline_queries: list[Any] = []
        self.sent_messages: list[Any] = []

    async def inline_query(self, username, query, entity=None):
        self.inline_queries.append((username, query, entity))
        if self.answer_results is not None:
            inline_engine.record_inline_answer(
                svc.INLINE_QUERY_KEY, self.answer_results, ok=True
            )
        return self.results if self.results is not None else []

    async def get_messages(self, chat_id, ids=None):
        return self.messages.get(ids)

    async def send_message(self, chat_id, text, **kwargs):
        message = _FakeMessage(text, [], message_id=SELECTION)
        message.chat_id = OWNER
        self.sent_messages.append((chat_id, text))
        return message

    async def delete_messages(self, chat_id, ids):
        return None


def _reply(
    *,
    text: str = ALT,
    entities: list[Any] | None = None,
    reply_to: int | None = SELECTION,
    peer: Any = None,
):
    return SimpleNamespace(
        id=REPLY_ID,
        message=text,
        text=text,
        entities=list(entities) if entities is not None else [_entity()],
        reply_to=SimpleNamespace(reply_to_msg_id=reply_to, reply_to_peer_id=peer),
    )


def _patch_facts(monkeypatch, *, premium: bool = False, free: bool = True, alt: str = ALT):
    async def _fake_get_me(client):
        return {"id": OWNER, "premium": premium}

    async def _fake_docs(client, ids):
        return [
            {
                "document_id": DOC,
                "alt": alt,
                "set": None,
                "free": free,
                "text_color": None,
            }
        ]

    monkeypatch.setattr(svc, "get_me", _fake_get_me)
    monkeypatch.setattr(svc, "get_custom_emoji_documents", _fake_docs)


def _arm(client, monkeypatch) -> dict[str, Any]:
    """Point the handler at a client and capture what it renders."""
    inline_engine.set_self_client(client)
    captured: dict[str, Any] = {}

    async def _fake_edit(inline_chat_id, inline_msg_id, title, body, buttons):
        captured.update(
            chat_id=inline_chat_id,
            msg_id=inline_msg_id,
            title=title,
            body=body,
            buttons=buttons,
        )

    monkeypatch.setattr(emoji, "_edit_inline", _fake_edit)
    return captured


def _payload_text(glyph: str = ALT) -> str:
    return PREFIX + glyph


def _result_payload(glyph: str = ALT, document_id: int = DOC):
    return (
        _payload_text(glyph),
        [_entity(document_id, offset=utf16_length(PREFIX), length=utf16_length(glyph))],
    )


# ── 1. the primary panel surface ─────────────────────────────────────────────


def test_the_primary_button_label_is_exact():
    assert emoji._DIAGNOSTICS_LABEL == "🧪 Run Emoji Diagnostics"


def test_the_primary_panel_exposes_one_diagnostic_button(monkeypatch):
    from backend.db import client as db_client

    db_client._fallback["emoji_library"] = []
    _title, _body, buttons = _run(emoji._emoji_panel_handler(None, ""))
    datas = _datas(buttons)
    texts = _texts(buttons)
    assert f"action:{emoji._DIAGNOSTICS_ACTION}" in datas
    assert datas.index(f"action:{emoji._DIAGNOSTICS_ACTION}") == 0  # primary
    assert "🧪 Run Emoji Diagnostics" in texts
    # the redundant standalone diagnostic buttons are gone
    assert f"action:{emoji._PROBE_ACTION}" not in datas
    assert f"action:{emoji._DIRECT_ACTION}" not in datas
    assert not any("POC" in t or "Live test" in t for t in texts)


def test_the_useful_product_capabilities_remain(monkeypatch):
    from backend.db import client as db_client

    db_client._fallback["emoji_library"] = []
    _title, _body, buttons = _run(emoji._emoji_panel_handler(None, ""))
    datas = _datas(buttons)
    # normal product features are preserved
    assert "action:emoji_import" in datas
    assert "action:emoji_react" in datas
    assert f"action:{emoji._INLINE_ACTION}" in datas  # production send stays
    assert "panel:emoji_library" in datas
    assert "panel:emoji_categories" in datas
    assert "panel:emoji_replacement" in datas


def test_the_primary_action_is_registered_through_the_existing_registry():
    emoji.register(client=None, owner_id=OWNER)
    assert get_action(emoji._DIAGNOSTICS_ACTION) is not None


def test_the_panel_text_is_english_only(monkeypatch):
    from backend.db import client as db_client

    db_client._fallback["emoji_library"] = []
    _title, body, buttons = _run(emoji._emoji_panel_handler(None, ""))
    for text in [body, *_texts(buttons)]:
        assert not any("\u0600" <= ch <= "\u06ff" for ch in text), text


# ── 2. clicking the button runs the REAL workflow ────────────────────────────


def test_the_action_sends_the_selection_prompt_and_arms_reply_mode():
    client = _FakeSelfClient()
    inline_engine.set_self_client(client)
    input_state.clear_all()
    title, body, buttons = _run(emoji._diagnostics_action(None, "", OWNER))
    assert title == "🧪 Run Emoji Diagnostics"
    assert client.sent_messages == [("me", emoji._DIAGNOSTICS_PROMPT)]
    pending = input_state.get_pending(OWNER)
    assert pending is not None
    assert pending["panel_id"] == emoji._DIAGNOSTICS_ACTION
    assert pending["extra"] == str(SELECTION)
    assert f"action:{emoji._DIAGNOSTICS_ACTION}" in _datas(buttons)
    input_state.clear_all()


def test_the_action_fails_honestly_without_a_self_client():
    inline_engine.set_self_client(None)
    _title, body, _buttons = _run(emoji._diagnostics_action(None, "", OWNER))
    assert "not connected" in body.lower()


def test_the_reply_handler_invokes_the_real_production_service(monkeypatch):
    text, entities = _result_payload()
    stored = _FakeMessage(text, entities, message_id=SENT_ID, via_bot_id=BOT_ID)
    client = _FakeSelfClient(
        results=[_FakeInlineResult(text, entities)],
        messages={REPLY_ID: _reply(), SENT_ID: stored},
    )
    _patch_facts(monkeypatch)
    captured = _arm(client, monkeypatch)

    _run(
        emoji._diagnostics_reply_handler(
            ALT, OWNER, REPLY_ID, OWNER, 42, extra=str(SELECTION)
        )
    )
    # the real inline route actually ran: one query, one click
    assert len(client.inline_queries) == 1
    assert client.results[0].clicks == 1
    assert "Emoji Diagnostics" in captured["title"]


def test_a_reply_to_the_wrong_message_never_runs_the_workflow(monkeypatch):
    client = _FakeSelfClient(
        results=[_FakeInlineResult(*_result_payload())],
        messages={REPLY_ID: _reply(reply_to=12345)},
    )
    captured = _arm(client, monkeypatch)
    _run(
        emoji._diagnostics_reply_handler(
            ALT, OWNER, REPLY_ID, OWNER, 42, extra=str(SELECTION)
        )
    )
    assert client.inline_queries == []
    assert "not the selection message" in captured["body"]


# ── 3. the trace reflects the REAL outcome ───────────────────────────────────


def _trace_for(outcome, monkeypatch, helper_bot_id=BOT_ID):
    return diag.build_trace(outcome, started_at=0.0, finished_at=1.0, helper_bot_id=helper_bot_id)


def _stage(trace, name):
    for stage in trace["stages"]:
        if stage["name"] == name:
            return stage
    raise AssertionError(name)


def test_a_verified_run_marks_every_stage_succeeded(monkeypatch):
    text, entities = _result_payload()
    stored = _FakeMessage(text, entities, message_id=SENT_ID, via_bot_id=BOT_ID)
    client = _FakeSelfClient(
        results=[_FakeInlineResult(text, entities)],
        messages={REPLY_ID: _reply(), SENT_ID: stored},
    )
    _patch_facts(monkeypatch)
    outcome = _run(
        svc.send_premium_emoji_via_inline(client, OWNER, DOC, ALT, OWNER)
    )
    assert outcome["diagnosis"] == svc.STORED_ENTITY_VERIFIED_RENDER_UNVERIFIED
    trace = _trace_for(outcome, None)
    assert trace["outcome_class"] == "verified"
    assert trace["stage"] == "run_completed"
    assert all(s["status"] == diag.STATUS_SUCCEEDED for s in trace["stages"])
    # submission and verification are distinct facts the trace keeps separate
    assert _stage(trace, "response_received")["status"] == diag.STATUS_SUCCEEDED
    inspected = _stage(trace, "result_inspected")
    assert inspected["evidence"]["via_bot_match"] is True
    assert inspected["evidence"]["entity_present"] is True


def test_an_entitlement_drop_is_reported_as_blocked_not_success(monkeypatch):
    text, _entities = _result_payload()
    submitted = [svc.build_inline_result(DOC, ALT)]
    client = _FakeSelfClient(
        results=[_FakeInlineResult(text, None)],
        messages={REPLY_ID: _reply()},
        answer_results=submitted,
    )
    _patch_facts(monkeypatch)
    captured = _arm(client, monkeypatch)
    _run(
        emoji._diagnostics_reply_handler(
            ALT, OWNER, REPLY_ID, OWNER, 42, extra=str(SELECTION)
        )
    )
    assert "Entitlement" in captured["body"] or "Blocked" in captured["body"]
    assert "✓" not in captured["body"].splitlines()[0]
    assert "Verified" not in captured["body"].splitlines()[0]
    # the run was recorded with the entitlement classification
    record = diag.current_run(OWNER)
    assert record is not None and record["in_flight"] is False
    assert record["outcome"]["diagnosis"] == svc.INLINE_BOT_NOT_ENTITLED


def test_a_stored_entity_without_attribution_is_a_failure_not_success(monkeypatch):
    text, entities = _result_payload()
    # stored message lost the helper-bot attribution
    stored = _FakeMessage(text, entities, message_id=SENT_ID, via_bot_id=None)
    client = _FakeSelfClient(
        results=[_FakeInlineResult(text, entities)],
        messages={SENT_ID: stored},
    )
    _patch_facts(monkeypatch)
    outcome = _run(svc.send_premium_emoji_via_inline(client, OWNER, DOC, ALT, OWNER))
    assert outcome["diagnosis"] == svc.STORED_ATTRIBUTION_MISSING
    trace = _trace_for(outcome, None)
    assert trace["outcome_class"] == "attribution_missing"
    assert _stage(trace, "result_inspected")["status"] == diag.STATUS_FAILED
    assert _stage(trace, "run_completed")["status"] == diag.STATUS_SKIPPED


def test_an_empty_result_stops_at_response_received(monkeypatch):
    _patch_facts(monkeypatch)
    client = _FakeSelfClient(results=[])
    outcome = _run(svc.send_premium_emoji_via_inline(client, OWNER, DOC, ALT, OWNER))
    assert outcome["diagnosis"] == svc.INLINE_RESULT_EMPTY
    trace = _trace_for(outcome, None)
    assert trace["outcome_class"] == "empty_result"
    assert _stage(trace, "response_received")["status"] == diag.STATUS_FAILED
    # the query really was initiated (no send followed), so this stage succeeded
    assert _stage(trace, "request_initiated")["status"] == diag.STATUS_SUCCEEDED
    # nothing after the failed response stage ran
    assert _stage(trace, "result_inspected")["status"] == diag.STATUS_SKIPPED


def test_a_source_entity_missing_stops_at_entity_extracted(monkeypatch):
    # A reply with no genuine custom-emoji entity still runs the workflow.
    client = _FakeSelfClient(
        results=[_FakeInlineResult(*_result_payload())],
        messages={REPLY_ID: _reply(text="hi", entities=[])},
    )
    _patch_facts(monkeypatch)
    captured = _arm(client, monkeypatch)
    _run(
        emoji._diagnostics_reply_handler(
            "hi", OWNER, REPLY_ID, OWNER, 42, extra=str(SELECTION)
        )
    )
    assert client.inline_queries == []  # nothing sent
    assert "no genuine" in captured["body"].lower() or "no real" in captured["body"].lower()


# ── 4. stage vocabulary and statuses are real ────────────────────────────────


def test_every_trace_stage_carries_the_required_fields(monkeypatch):
    _patch_facts(monkeypatch)
    client = _FakeSelfClient(results=[])
    outcome = _run(svc.send_premium_emoji_via_inline(client, OWNER, DOC, ALT, OWNER))
    trace = _trace_for(outcome, None)
    assert [s["name"] for s in trace["stages"]] == [n for n, _ in diag.STAGES]
    for stage in trace["stages"]:
        assert stage["status"] in {
            diag.STATUS_SUCCEEDED,
            diag.STATUS_FAILED,
            diag.STATUS_SKIPPED,
            diag.STATUS_BLOCKED,
            diag.STATUS_INCONCLUSIVE,
        }
        assert isinstance(stage["label"], str) and stage["label"]
        assert isinstance(stage["elapsed_s"], float)


def test_the_trace_is_not_a_hardcoded_success(monkeypatch):
    _patch_facts(monkeypatch)
    client = _FakeSelfClient(results=[])
    outcome = _run(svc.send_premium_emoji_via_inline(client, OWNER, DOC, ALT, OWNER))
    trace = _trace_for(outcome, None)
    rendered = diag.render_trace(trace)
    assert "Failed" in rendered
    assert "succeeded" in rendered  # earlier stages really succeeded
    assert "skipped" in rendered


# ── 5. duplicate-callback safety ─────────────────────────────────────────────


def test_a_second_run_cannot_start_while_one_is_in_flight(monkeypatch):
    client = _FakeSelfClient(
        results=[_FakeInlineResult(*_result_payload())],
        messages={REPLY_ID: _reply()},
    )
    captured = _arm(client, monkeypatch)
    # simulate an in-flight run
    assert diag.begin_run(OWNER, "inflight") is True
    _run(
        emoji._diagnostics_reply_handler(
            ALT, OWNER, REPLY_ID, OWNER, 42, extra=str(SELECTION)
        )
    )
    # the duplicate did not drive any Telegram operation
    assert client.inline_queries == []
    assert "already in progress" in captured["body"]


def test_the_action_refuses_a_duplicate_while_a_run_is_in_flight():
    client = _FakeSelfClient()
    inline_engine.set_self_client(client)
    assert diag.begin_run(OWNER, "inflight") is True
    _title, body, _buttons = _run(emoji._diagnostics_action(None, "", OWNER))
    assert "already in progress" in body
    assert client.sent_messages == []  # no second selection message


def test_a_completed_run_releases_the_slot():
    assert diag.begin_run(OWNER, "one") is True
    assert diag.is_running(OWNER) is True
    diag.end_run(OWNER, outcome={"ok": False, "diagnosis": svc.INLINE_RESULT_EMPTY})
    assert diag.is_running(OWNER) is False
    assert diag.begin_run(OWNER, "two") is True  # a new run may start


# ── 6. no secrets in the trace ───────────────────────────────────────────────


def test_the_trace_exposes_no_secrets(monkeypatch):
    text, _entities = _result_payload()
    client = _FakeSelfClient(
        results=[_FakeInlineResult(text, None)],
        messages={REPLY_ID: _reply()},
        answer_results=[svc.build_inline_result(DOC, ALT)],
    )
    _patch_facts(monkeypatch)
    captured = _arm(client, monkeypatch)
    _run(
        emoji._diagnostics_reply_handler(
            ALT, OWNER, REPLY_ID, OWNER, 42, extra=str(SELECTION)
        )
    )
    body = captured["body"]
    for forbidden in ("BOT_TOKEN", "SESSION", "session_string", "access_hash", "Authorization", "api_hash"):
        assert forbidden not in body


# ── 7. architecture: no new infrastructure ───────────────────────────────────


def test_the_diagnostics_module_adds_no_second_infrastructure():
    import inspect
    from pathlib import Path

    source = Path(inspect.getfile(diag)).read_text()
    for forbidden in (
        "TelegramClient",
        "run_until_disconnected",
        "create_task",
        "immortal_create_task",
        "guarded_create_task",
        "forward_messages",
        "events.NewMessage",
        "events.CallbackQuery",
        "@client.on",
        "client.on(",
        "asyncio.Lock",
        "import asyncio",
        "import httpx",
    ):
        assert forbidden not in source, forbidden
