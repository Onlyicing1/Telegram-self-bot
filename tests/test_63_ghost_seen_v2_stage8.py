from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import asyncio
import pytest

from backend.ai import diagnostics as ai_diag
from backend.bot.handlers import ghost_seen_v2 as handler
from backend.services import ghost_seen_v2 as service


def _message(message_id, text):
    return SimpleNamespace(id=message_id, text=text, message=text, date=None)


class _Client:
    def iter_messages(self, chat_id, **kwargs):
        async def stream():
            yield _message(9, "previous")
        return stream()


@pytest.fixture(autouse=True)
def clean():
    service.reset_allowed_chats()
    service.clear_selection(123)
    handler._ai_states.clear()
    handler._ai_locks.clear()
    ai_diag._active.clear()
    ai_diag._request_details.clear()
    yield
    service.clear_selection(123)
    handler._ai_states.clear()
    handler._ai_locks.clear()
    ai_diag._active.clear()
    ai_diag._request_details.clear()


def _arm():
    service.allow_chat(123)
    service.toggle_selection(123, 10)


def _result(text="reply", success=True):
    return SimpleNamespace(success=success, response=text)


def _patch_run(engine, send=None, target=None):
    return (
        patch.object(handler.inline_engine, "get_self_client", return_value=_Client()),
        patch.object(handler.inline_engine, "get_owner_id", return_value=99),
        patch.object(handler, "_load_target_message", AsyncMock(return_value=target or service.ViewerMessage(10, 123, "target"))),
        patch("backend.ai.engine.engine.get_engine", return_value=engine),
        patch.object(handler, "send_reply", send or AsyncMock(return_value={})),
    )


@pytest.mark.asyncio
async def test_duplicate_rejection_does_not_clear_active_operation():
    _arm()
    started = asyncio.Event()
    release = asyncio.Event()

    async def generate(_):
        started.set()
        await release.wait()
        return _result()

    send = AsyncMock(return_value={})
    contexts = _patch_run(SimpleNamespace(execute=generate), send)
    for context in contexts:
        context.start()
    try:
        first = asyncio.create_task(handler._run_ai_reply(50, 123, 10, 1, False, "A"))
        await started.wait()
        duplicate = await handler._run_ai_reply(50, 123, 10, 1, False, "A")
        assert "already" in duplicate[1]
        assert 50 in handler._ai_states
        release.set()
        await first
        send.assert_awaited_once()
    finally:
        for context in contexts:
            context.stop()


@pytest.mark.asyncio
async def test_timeout_discards_cancellation_resistant_late_result():
    _arm()
    release = asyncio.Event()

    async def cancellation_resistant(_):
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            await release.wait()
            return _result("late")

    send = AsyncMock(return_value={})
    contexts = _patch_run(SimpleNamespace(execute=cancellation_resistant), send)
    with contexts[0], contexts[1], contexts[2], contexts[3], contexts[4], patch.object(handler, "_AI_TIMEOUT_S", 0.001), patch.object(handler, "_AI_CANCEL_GRACE_S", 0.001):
        outcome = await handler._run_ai_reply(50, 123, 10, 1, False, "A")
    assert "timed out" in outcome[1]
    assert send.await_count == 0
    assert 50 not in handler._ai_states
    release.set()
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_timeout_then_fresh_operation_can_run():
    _arm()
    async def slow(_):
        await asyncio.sleep(0.05)
    send = AsyncMock(return_value={})
    contexts = _patch_run(SimpleNamespace(execute=slow), send)
    with contexts[0], contexts[1], contexts[2], contexts[3], contexts[4], patch.object(handler, "_AI_TIMEOUT_S", 0.001):
        outcome = await handler._run_ai_reply(50, 123, 10, 1, False, "A")
    assert "timed out" in outcome[1]
    assert send.await_count == 0
    details = next(iter(ai_diag.request_details().values()))
    assert details["timeout_occurred"] is True
    assert details["cancellation_requested"] is True
    assert details["cancellation_completed"] is True
    assert details["engine_result_status"] == "timeout"
    assert details["delivery_reached"] is False
    assert details["final_failure_reason"] == "ai_timeout"
    service.toggle_selection(123, 10)

    engine = SimpleNamespace(execute=AsyncMock(return_value=_result("fresh")))
    contexts = _patch_run(engine, send)
    with contexts[0], contexts[1], contexts[2], contexts[3], contexts[4]:
        outcome = await handler._run_ai_reply(50, 123, 10, 1, False, "A")
    assert outcome[1] == "✓ Reply sent."
    send.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("response", [None, 123, "", "   ", "x" * 4097])
async def test_unusable_engine_results_are_not_delivered(response):
    _arm()
    engine = SimpleNamespace(execute=AsyncMock(return_value=_result(response)))
    send = AsyncMock(return_value={})
    contexts = _patch_run(engine, send)
    with contexts[0], contexts[1], contexts[2], contexts[3], contexts[4]:
        outcome = await handler._run_ai_reply(50, 123, 10, 1, False, "A")
    assert "Reply sent" not in outcome[1]
    send.assert_not_awaited()
    assert 50 not in handler._ai_states


@pytest.mark.asyncio
async def test_delivery_failure_is_honest_and_not_retried():
    _arm()
    engine = SimpleNamespace(execute=AsyncMock(return_value=_result()))
    send = AsyncMock(side_effect=RuntimeError("send failed"))
    contexts = _patch_run(engine, send)
    with contexts[0], contexts[1], contexts[2], contexts[3], contexts[4]:
        outcome = await handler._run_ai_reply(50, 123, 10, 1, False, "A")
    assert "send" in outcome[1].lower()
    assert send.await_count == 1
    assert not handler._ai_states


@pytest.mark.asyncio
@pytest.mark.parametrize("disclosure", [False, True])
async def test_disclosure_only_changes_delivery_suffix(disclosure):
    _arm()
    engine = SimpleNamespace(execute=AsyncMock(return_value=_result("same")))
    send = AsyncMock(return_value={})
    contexts = _patch_run(engine, send)
    with contexts[0], contexts[1], contexts[2], contexts[3], contexts[4]:
        await handler._run_ai_reply(50, 123, 10, 1, disclosure, "A")
    prompt = engine.execute.await_args.args[0].user_message
    delivered = send.await_args.args[3]
    assert "same" not in prompt
    assert ("Written with AI assistance" in delivered) is disclosure


def test_sender_direction_is_explicit():
    viewer = service.MessageViewerPage(
        123,
        (
            service.ViewerMessage(10, 123, "incoming", outgoing=False),
            service.ViewerMessage(11, 123, "outgoing", outgoing=True),
        ),
        1,
        1,
    )
    rendered = service.render_message_viewer("Alice", viewer)
    assert "Alice (incoming): «incoming»" in rendered
    assert "You (outgoing): «outgoing»" in rendered


def test_context_and_disclosure_controls_remain_bounded():
    context_buttons = handler._context_buttons(123)
    callbacks = [getattr(button, "data", b"").decode() for row in context_buttons for button in row]
    counts = {int(callback.split(":")[-1]) for callback in callbacks if callback.startswith("action:ghost_seen_v2_ai_generate:")}
    assert counts == {1, 5, 10, 20}
    assert "ai_prompt" not in repr(callbacks)
