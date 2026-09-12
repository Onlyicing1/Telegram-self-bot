"""Natural-language → Taskloom wizard bridge.

Production symptom this file locks down: a structurally task-like request the
natural-language interpreter could not turn into a COMPLETE task definition
was answered with only
``"... is not supported yet, so I did not create the task."`` and the owner
was never offered the existing structured creation wizard.

Contract under test:

- A fully representable request still creates the task directly (unchanged).
- An unsupported capability, or a candidate-level interpretation failure,
  carries the wizard signal; provider/timeout/persistence failures do NOT.
- The delivery layer opens the SAME Taskloom wizard panel (``taskloom_new``)
  for the owner, with a fresh draft and no prefilled values.
- Opening the wizard persists NOTHING (no TaskCreationService call).
- When the panel cannot be sent, the text reply still carries an actionable
  hint to the same wizard.

In-process only: no live Telegram, no live Supabase, no provider HTTP.
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from backend.ai.providers.base.capabilities import ProviderCapabilities
from backend.ai.providers.base.config import ProviderConfig
from backend.ai.providers.base.contract import BaseProvider, ProviderResponse
from backend.ai.providers.manager.manager import ProviderManager
from backend.ai.tools.context import ToolContext
from backend.bot.handlers import taskloom

OWNER = 6161
CHAT_ID = -100777
TZ = "Asia/Tehran"


class _FakeProvider(BaseProvider):
    def __init__(self, text: str = "", *, success: bool = True):
        super().__init__(ProviderConfig(provider_name="fake", enabled=True, default_model="m"))
        self.text = text
        self.success = success
        self.calls = 0

    @property
    def name(self) -> str:
        return "fake"

    @property
    def capabilities(self):
        return ProviderCapabilities(supports_tools=True, supports_function_call=True)

    async def chat(self, messages, **kwargs):
        self.calls += 1
        return ProviderResponse(text=self.text, provider_name="fake", success=self.success)

    def initialize(self):
        return None

    def shutdown(self):
        return None

    def count_tokens(self, text):
        return max(1, len(text) // 4)

    def health(self):
        return {"healthy": True}


def _provider_manager(text: str = "", *, success: bool = True) -> ProviderManager:
    pm = ProviderManager()
    pm.register_provider(_FakeProvider(text, success=success))
    pm.switch_provider("fake")
    pm._fallback_chain = []
    return pm


def _ctx(pm: ProviderManager) -> ToolContext:
    return ToolContext(
        telegram=None, owner_id=OWNER, tz_str=TZ, client=None,
        extra={"provider_manager": pm, "chat_id": CHAT_ID},
    )


async def _create(pm: ProviderManager, request: str):
    from backend.ai.database import manager as dbm
    from backend.ai.tools.task import CreateTaskTool

    manager = dbm.RepositoryManager(supabase_available=False)
    with patch.object(dbm, "get_repository_manager", return_value=manager):
        result = await CreateTaskTool(_ctx(pm)).execute(_ctx(pm), {"request": request})
    return result, manager


def _good_candidate() -> str:
    return json.dumps({
        "label": "Bio update",
        "schedule_type": "interval",
        "schedule": {"seconds": 120},
        "timezone": TZ,
        "actions": [{"name": "bio_set_text", "arguments": {"text": ""}}],
        "notification_destination": {},
        "ai_instruction": "update my bio with a randomly generated dialogue every 2 minutes",
    })


# ── the tool-level signal ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_unsupported_capability_requests_the_existing_wizard():
    pm = _provider_manager(json.dumps({"unsupported": "bio sync task"}))
    result, manager = await _create(pm, "هر ۲ دقیقه بیو رو سینک کن")
    assert result.success is False
    assert result.data.get("open_taskloom_wizard") is True
    assert result.data.get("wizard_reason") == "unsupported_capability"
    assert result.data.get("capability") == "bio sync task"
    # Nothing was persisted by asking for the wizard.
    assert await manager.task.list_tasks(OWNER) == []


@pytest.mark.asyncio
async def test_incomplete_candidate_requests_the_wizard():
    """The interpreter could not derive a full task (null / invalid candidate)."""
    pm = _provider_manager("null")
    # "every 10 minutes" keeps the request past the deterministic
    # completeness gate so the candidate-level failure itself is what signals.
    result, manager = await _create(pm, "something about my bio every 10 minutes")
    assert result.success is False
    assert result.data.get("open_taskloom_wizard") is True
    assert str(result.data.get("wizard_reason", "")).startswith("candidate_invalid")
    assert await manager.task.list_tasks(OWNER) == []


@pytest.mark.asyncio
async def test_provider_failure_never_requests_the_wizard():
    """A provider/transport failure is a retry problem, not a form to fill in."""
    pm = _provider_manager("rate limited", success=False)
    result, _ = await _create(pm, "every 2 minutes update my bio")
    assert result.success is False
    assert "open_taskloom_wizard" not in (result.data or {})


@pytest.mark.asyncio
async def test_fully_representable_request_still_creates_directly():
    """The existing natural-language creation path is unchanged."""
    pm = _provider_manager(_good_candidate())
    result, manager = await _create(pm, "every 2 minutes update my bio")
    assert result.success is True
    assert "open_taskloom_wizard" not in (result.data or {})
    tasks = await manager.task.list_tasks(OWNER)
    assert len(tasks) == 1
    assert tasks[0].schedule == {"seconds": 120.0}


# ── the deterministic completeness gate (live production regression) ─────


@pytest.mark.asyncio
async def test_live_underspecified_request_never_creates_and_signals_the_wizard():
    """THE live reproduction: "یه تسک برای بیو بساز" carries no schedule
    expression, so the provider must never be asked (and never fill one in).
    The existing Taskloom wizard is surfaced with the structured signal."""
    pm = _provider_manager(_good_candidate())  # model WOULD fill the schedule
    result, manager = await _create(pm, "یه تسک برای بیو بساز")
    assert result.success is False
    assert result.data.get("open_taskloom_wizard") is True
    assert result.data.get("wizard_reason") == "incomplete_request"
    assert await manager.task.list_tasks(OWNER) == []


@pytest.mark.asyncio
async def test_invented_schedule_for_underspecified_request_is_never_persisted():
    """Even a schema-valid candidate with an invented interval cannot become a
    task when the owner's request expressed no schedule: the gate runs BEFORE
    the provider, so the invented schedule is never persisted."""
    pm = _provider_manager(_good_candidate())
    result, manager = await _create(pm, "update my bio please")
    assert result.success is False
    assert result.data.get("open_taskloom_wizard") is True
    assert await manager.task.list_tasks(OWNER) == []


@pytest.mark.asyncio
async def test_english_underspecified_request_signals_the_wizard():
    pm = _provider_manager(_good_candidate())
    result, manager = await _create(pm, "make a task for my bio")
    assert result.success is False
    assert result.data.get("open_taskloom_wizard") is True
    assert await manager.task.list_tasks(OWNER) == []


# ── the delivery-layer bridge ─────────────────────────────────────────────


class _FakeMessage:
    id = 4321


class _FakeEvent:
    chat_id = CHAT_ID
    message = _FakeMessage()

    def __init__(self):
        self.edits: list[str] = []
        self.replies: list[str] = []
        self.deleted = False

    async def edit(self, text, buttons=None):
        self.edits.append(text)

    async def reply(self, text, buttons=None):
        self.replies.append(text)

    async def delete(self):
        self.deleted = True


def _wizard_result(message: str, data: dict) -> object:
    from backend.ai.engine.result import EngineResult

    return EngineResult(
        success=True,
        provider="dummy",
        model="dummy",
        latency=0.01,
        response=message,
        metadata={
            "tool_results": [{
                "tool_name": "create_task",
                "success": False,
                "message": message,
                "data": data,
            }],
        },
    )


@pytest.mark.asyncio
async def test_open_task_wizard_sends_the_existing_panel_and_persists_nothing(monkeypatch):
    import backend.bot.handlers.ai_unified as ai_unified

    sent: list[tuple] = []

    async def _fake_send(client, chat_id, query):
        sent.append((client, chat_id, query))
        return True

    monkeypatch.setattr("backend.helper.send_inline_panel", _fake_send)
    created: list = []
    monkeypatch.setattr(
        "backend.ai.task_creation.TaskCreationService",
        MagicMock(side_effect=lambda *a, **k: created.append(a) or MagicMock()),
    )

    event = _FakeEvent()
    opened = await ai_unified._open_task_wizard(
        event, None, OWNER,
        {"wizard_reason": "unsupported_capability", "capability": "bio sync task"},
    )

    assert opened is True
    assert sent == [(None, CHAT_ID, "taskloom_new")]
    assert taskloom.WIZARD_PANEL_QUERY == "taskloom_new"
    # The owner/session context is the owner-scoped draft, reset with a notice
    # and NO prefilled values.
    draft = taskloom._drafts[OWNER]
    assert draft.step == taskloom.STEP_ACTION
    assert draft.action == "" and draft.schedule_type == "" and draft.max_length is None
    assert "bio sync task" in draft.notice
    # Opening the wizard never persists a task.
    assert created == []
    assert event.deleted is True


@pytest.mark.asyncio
async def test_open_task_wizard_falls_back_when_the_panel_cannot_be_sent(monkeypatch):
    import backend.bot.handlers.ai_unified as ai_unified

    async def _no_helper(client, chat_id, query):
        return False

    monkeypatch.setattr("backend.helper.send_inline_panel", _no_helper)
    opened = await ai_unified._open_task_wizard(
        _FakeEvent(), None, OWNER, {"wizard_reason": "candidate_invalid:null"},
    )
    assert opened is False


@pytest.mark.asyncio
async def test_execute_ai_opens_the_wizard_instead_of_only_refusing(monkeypatch):
    import backend.bot.handlers.ai_unified as ai_unified

    sent: list[str] = []

    async def _fake_send(client, chat_id, query):
        sent.append(query)
        return True

    monkeypatch.setattr("backend.helper.send_inline_panel", _fake_send)

    async def _no_restore(owner_id, config=None):
        return None

    monkeypatch.setattr(ai_unified, "_restore_config", _no_restore)
    monkeypatch.setattr(
        "backend.runtime.task_guard.guarded_create_task", MagicMock(return_value=None)
    )
    monkeypatch.setattr(
        "backend.ai.config_store.record_request", lambda owner_id, latency_ms: None
    )

    refusal = (
        "I understood your request, but bio sync task is not supported yet, "
        "so I did not create the task."
    )
    ai_unified._engine = _FakeEngine(
        _wizard_result(refusal, {"open_taskloom_wizard": True, "wizard_reason": "unsupported_capability"})
    )
    try:
        event = _FakeEvent()
        await ai_unified._execute_ai(event, OWNER, "sync my bio", "Nova", TZ)
    finally:
        ai_unified._engine = None

    assert sent == ["taskloom_new"]
    # The refusal text is NOT delivered as the only response.
    delivered = event.edits + event.replies
    assert all(refusal != text for text in delivered)


@pytest.mark.asyncio
async def test_execute_ai_falls_back_to_text_with_a_hint(monkeypatch):
    import backend.bot.handlers.ai_unified as ai_unified

    async def _no_helper(client, chat_id, query):
        return False

    monkeypatch.setattr("backend.helper.send_inline_panel", _no_helper)

    async def _no_restore(owner_id, config=None):
        return None

    monkeypatch.setattr(ai_unified, "_restore_config", _no_restore)
    monkeypatch.setattr(
        "backend.runtime.task_guard.guarded_create_task", MagicMock(return_value=None)
    )
    monkeypatch.setattr(
        "backend.ai.config_store.record_request", lambda owner_id, latency_ms: None
    )

    refusal = "I understood your request, but bio sync task is not supported yet, so I did not create the task."
    ai_unified._engine = _FakeEngine(
        _wizard_result(refusal, {"open_taskloom_wizard": True, "wizard_reason": "unsupported_capability"})
    )
    try:
        event = _FakeEvent()
        await ai_unified._execute_ai(event, OWNER, "sync my bio", "Nova", TZ)
    finally:
        ai_unified._engine = None

    delivered = "\n".join(event.edits + event.replies)
    assert refusal in delivered
    assert "Taskloom" in delivered and "New task" in delivered


@pytest.mark.asyncio
async def test_whitespace_only_ai_response_is_not_delivered_as_a_shell(monkeypatch, caplog):
    """Live evidence: response == " " passed truthiness, normalization raised
    ValueError, and the request message was left as the header-only shell.
    The delivery layer now treats whitespace-only output as NO response."""
    import logging

    from backend.ai.tools import delivery as delivery_mod

    edits, replies = [], []

    async def edit(text):
        edits.append(text)

    async def reply(text):
        replies.append(text)

    event = SimpleNamespace(edit=edit, reply=reply)
    with caplog.at_level(logging.WARNING, logger=delivery_mod.logger.name):
        result = await delivery_mod.deliver_response(event, "یه تسک برای بیو بساز", "Nova", "   ")

    assert result.success is True
    assert len(edits) == 1
    assert replies == []
    assert "AI returned no response." in edits[0]
    # No normalization fallback noise for a response that is simply empty.
    assert "AI_OUTPUT_NORMALIZATION_FALLBACK" not in caplog.text


@pytest.mark.asyncio
async def test_normalization_failure_is_logged_and_still_delivered(monkeypatch, caplog):
    """A non-empty response whose normalization fails is logged with a
    content-free error classification, then delivered as-is (never hidden)."""
    import logging

    from backend.ai.tools import delivery as delivery_mod

    edits, replies = [], []

    async def edit(text):
        edits.append(text)

    async def reply(text):
        replies.append(text)

    event = SimpleNamespace(edit=edit, reply=reply)
    # Force the normalization failure deterministically: the output pipeline
    # raises ValueError on empty/whitespace input, which only happens here
    # when the pre-checked response renders to whitespace.
    from backend.ai.tools.delivery import process_output as _po

    calls = {"n": 0}

    def _boom(_text):
        calls["n"] += 1
        raise ValueError("AI output became empty after rendering")

    monkeypatch.setattr(delivery_mod, "process_output", _boom)
    text = "a real response"
    with caplog.at_level(logging.WARNING, logger=delivery_mod.logger.name):
        result = await delivery_mod.deliver_response(event, "msg", "Nova", text)

    assert calls["n"] == 1
    assert result.success is True
    assert "AI_OUTPUT_NORMALIZATION_FALLBACK" in caplog.text
    assert "ValueError" in caplog.text
    assert "nonempty_after_strip=True" in caplog.text
    # The response still reached the owner (raw fallback), never hidden.
    assert edits == ["msg\n────────────\n🤖 Nova\n" + text]


@pytest.mark.asyncio
async def test_incomplete_request_delivery_preference(monkeypatch):
    """The gate's refusal carries the wizard signal the delivery layer needs;
    the refusal text itself is only shown when the panel cannot be sent."""
    import backend.bot.handlers.ai_unified as ai_unified

    async def _no_helper(client, chat_id, query):
        return False

    monkeypatch.setattr("backend.helper.send_inline_panel", _no_helper)

    async def _no_restore(owner_id, config=None):
        return None

    monkeypatch.setattr(ai_unified, "_restore_config", _no_restore)
    monkeypatch.setattr(
        "backend.runtime.task_guard.guarded_create_task", MagicMock(return_value=None)
    )
    monkeypatch.setattr(
        "backend.ai.config_store.record_request", lambda owner_id, latency_ms: None
    )

    refusal = "I need a few structured choices for this task — pick them in the creation form below."
    ai_unified._engine = _FakeEngine(
        _wizard_result(refusal, {"open_taskloom_wizard": True, "wizard_reason": "incomplete_request"})
    )
    try:
        event = _FakeEvent()
        await ai_unified._execute_ai(event, OWNER, "یه تسک برای بیو بساز", "Nova", TZ)
    finally:
        ai_unified._engine = None

    delivered = "\n".join(event.edits + event.replies)
    assert refusal in delivered
    assert "Taskloom" in delivered and "New task" in delivered


class _FakeEngine:
    def __init__(self, result):
        self._result = result
        pm = MagicMock()
        pm.get_active_name.return_value = "dummy"
        pm.get_active.return_value = MagicMock(config=MagicMock(default_model="dummy"))
        self.provider_manager = pm

    async def execute(self, request, status_callback=None):
        return self._result


# ── the shared wizard entry is unchanged and owner-scoped ─────────────────


def test_wizard_draft_is_owner_scoped_and_starts_at_action():
    manager = MagicMock()
    taskloom._drafts.clear()
    draft = taskloom.reset_wizard_draft(OWNER, notice="⚠ note")
    assert draft.step == taskloom.STEP_ACTION
    assert taskloom._drafts[OWNER] is draft
    assert OWNER + 1 not in taskloom._drafts
    title, body, buttons = taskloom._wizard_render(draft)
    assert "Step 1/4" in body and "Action" in body
    assert manager.task.call_count == 0
