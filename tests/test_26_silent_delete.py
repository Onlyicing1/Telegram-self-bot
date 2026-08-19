"""
TASK 26 — Silent Delete Regression Tests

Delete executions must end silently: the Telegram deletion is the only
visible effect. A successful pure-delete round must NEVER produce a
confirmation message ("deleted", "Deleted successfully", counts, ...) —
especially when the delete removed the request message itself, where the
delivery fallback would otherwise reply with a brand-new confirmation.

The tool result stays internal (logs, history, telemetry). Failed deletes
are NOT silent (the error must reach the user and can never be mistaken
for a success confirmation), and non-delete AI actions are unaffected.
"""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from backend.ai.engine.result import EngineResult


# ── fixtures / helpers ───────────────────────────────────────────────────────

def _tool_result(tool_name: str, success: bool, message: str) -> dict:
    return {
        "tool_name": tool_name,
        "success": success,
        "message": message,
        "data": {"count": 5} if success else {"count": 0},
        "error": "" if success else "boom",
    }


def _delete_result(*, success: bool = True, tool_name: str = "delete") -> EngineResult:
    message = (
        "Deleted 5 outgoing message(s) from the last 6 message(s) in this chat."
        if success else "Delete failed: boom"
    )
    return EngineResult(
        success=True,
        provider="local",
        model="deterministic",
        latency=0.05,
        response=message,
        metadata={
            "tool_results": [_tool_result(tool_name, success, message)],
            "ai_action": {
                "action": "delete_messages",
                "kind": "executable",
                "target": "recent_messages",
            },
        },
    )


def _non_delete_result() -> EngineResult:
    return EngineResult(
        success=True,
        provider="local",
        model="deterministic",
        latency=0.05,
        response="First Name: Parham",
        metadata={
            "tool_results": [
                {
                    "tool_name": "account_show",
                    "success": True,
                    "message": "First Name: Parham",
                    "data": {"first_name": "Parham"},
                    "error": "",
                }
            ],
        },
    )


class _FakeProviderCfg:
    default_model = "dummy"
    model = "dummy"


class _FakeProvider:
    config = _FakeProviderCfg()


class _FakePM:
    def get_active_name(self) -> str:
        return "dummy"

    def get_active(self) -> _FakeProvider:
        return _FakeProvider()


class _FakeEngine:
    provider_manager = _FakePM()
    conversation_manager = MagicMock()

    def __init__(self, result: EngineResult) -> None:
        self._result = result

    async def execute(self, request, status_callback=None) -> EngineResult:
        return self._result


def _make_event(*, revert_fails: bool = False):
    event = MagicMock()
    event.chat_id = 123
    event.message = MagicMock(id=456)
    event.raw_text = "Nova ده پیام آخر رو پاک کن"

    if revert_fails:
        async def _edit(*args, **kwargs):
            if args and args[0] == "ده پیام آخر رو پاک کن":
                raise RuntimeError("message not found (deleted)")
        event.edit = AsyncMock(side_effect=_edit)
    else:
        event.edit = AsyncMock()
    event.reply = AsyncMock()
    return event


async def _run_execute_ai(event, result: EngineResult):
    """Drive ``_execute_ai`` with a fake engine/event and hermetic patches."""
    fake_engine = _FakeEngine(result)
    with (
        patch("backend.bot.handlers.ai_unified._engine", fake_engine),
        patch(
            "backend.ai.config_store.get_config",
            new=AsyncMock(return_value={}),
        ),
        patch(
            "backend.ai.config_store.record_request",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "backend.runtime.task_guard.guarded_create_task",
            new=lambda coro, **kw: __import__("asyncio").ensure_future(coro),
        ),
        patch(
            "backend.ai.tools.delivery.deliver_response",
            new=AsyncMock(return_value=MagicMock(
                success=True, chunks_delivered=1, total_chunks=1,
            )),
        ) as deliver_mock,
    ):
        from backend.bot.handlers.ai_unified import _execute_ai
        await _execute_ai(
            event,
            owner_id=1,
            prompt_text="ده پیام آخر رو پاک کن",
            trigger_word="Nova",
            tz_str="UTC",
        )
    return deliver_mock


# ── helper: _is_silent_delete ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_is_silent_delete_true_for_successful_delete_round():
    from backend.bot.handlers.ai_unified import _is_silent_delete
    assert _is_silent_delete(_delete_result(success=True, tool_name="delete")) is True


@pytest.mark.asyncio
async def test_is_silent_delete_true_for_all_delete_tool_names():
    from backend.bot.handlers.ai_unified import _is_silent_delete
    for name in ("delete", "delete_replied", "delete_by_id",
                 "delete_message_by_id", "delete_messages_by_ids"):
        assert _is_silent_delete(_delete_result(success=True, tool_name=name)) is True, name


@pytest.mark.asyncio
async def test_is_silent_delete_false_when_delete_failed():
    from backend.bot.handlers.ai_unified import _is_silent_delete
    assert _is_silent_delete(_delete_result(success=False)) is False


@pytest.mark.asyncio
async def test_is_silent_delete_false_when_non_delete_tool_included():
    from backend.bot.handlers.ai_unified import _is_silent_delete
    result = _delete_result(success=True)
    result.metadata["tool_results"].append(
        _tool_result("save", True, "Saved S1234.")
    )
    assert _is_silent_delete(result) is False


@pytest.mark.asyncio
async def test_is_silent_delete_false_without_tool_results():
    from backend.bot.handlers.ai_unified import _is_silent_delete
    result = EngineResult(success=True, response="hello", metadata={})
    assert _is_silent_delete(result) is False


# ── _execute_ai integration ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_silent_delete_sends_no_telegram_message():
    """A successful delete delivers nothing: no confirmation edit, no reply,
    no fallback — the request message reverts to the owner's original text."""
    event = _make_event()
    deliver_mock = await _run_execute_ai(event, _delete_result(success=True))

    deliver_mock.assert_not_called()
    event.reply.assert_not_called()
    # Last edit must be the revert to the original prompt, never a
    # confirmation text.
    last_edit = event.edit.call_args_list[-1]
    assert last_edit.args[0] == "ده پیام آخر رو پاک کن"
    assert "Deleted" not in str(event.edit.call_args_list)


@pytest.mark.asyncio
async def test_silent_delete_when_request_message_was_deleted_no_reply():
    """When the delete removed the request message itself, the revert edit
    fails — and the handler must NOT fall back to replying a confirmation."""
    event = _make_event(revert_fails=True)
    deliver_mock = await _run_execute_ai(event, _delete_result(success=True))

    deliver_mock.assert_not_called()
    event.reply.assert_not_called()


@pytest.mark.asyncio
async def test_failed_delete_shows_error_not_success_confirmation():
    """A failed delete is not suppressed as a success: the failure message is
    delivered (edit-in-place) and never reads like a success confirmation."""
    event = _make_event()
    deliver_mock = await _run_execute_ai(event, _delete_result(success=False))

    deliver_mock.assert_awaited_once()
    args = deliver_mock.call_args.args
    assert args[3] == "Delete failed: boom"
    assert "Deleted 5" not in args[3]
    event.reply.assert_not_called()


@pytest.mark.asyncio
async def test_non_delete_actions_still_deliver():
    """Non-delete AI results keep the normal delivery path untouched."""
    event = _make_event()
    deliver_mock = await _run_execute_ai(event, _non_delete_result())

    deliver_mock.assert_awaited_once()
    args = deliver_mock.call_args.args
    assert args[3] == "First Name: Parham"
