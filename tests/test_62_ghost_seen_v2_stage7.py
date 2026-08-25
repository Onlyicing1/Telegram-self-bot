import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from backend.bot.handlers import ghost_seen_v2 as handler
from backend.services import ghost_seen_v2 as service


def msg(i, text):
    return SimpleNamespace(id=i, text=text, message=text, date=None)


class Client:
    def __init__(self, items):
        self.items = items
        self.calls = []

    def iter_messages(self, chat_id, **kwargs):
        self.calls.append((chat_id, kwargs))
        async def stream():
            for item in self.items:
                yield item
        return stream()


@pytest.fixture(autouse=True)
def reset_state():
    service.reset_allowed_chats()
    service.clear_selection(123)
    handler._ai_states.clear()
    handler._ai_locks.clear()
    yield
    service.clear_selection(123)
    handler._ai_states.clear()
    handler._ai_locks.clear()


def arm():
    service.allow_chat(123)
    service.toggle_selection(123, 10)


def result(text="reply", success=True):
    return SimpleNamespace(success=success, response=text)


@pytest.mark.asyncio
async def test_timeout_has_zero_delivery_and_cleans_state():
    arm()
    client = Client([msg(10, "target"), msg(9, "before")])
    async def slow(_):
        await asyncio.sleep(0.05)
    with patch.object(handler.inline_engine, "get_self_client", return_value=client), \
         patch.object(handler.inline_engine, "get_owner_id", return_value=99), \
         patch.object(handler, "_load_target_message", AsyncMock(return_value=service.ViewerMessage(10, 123, "target"))), \
         patch.object(handler, "_AI_TIMEOUT_S", 0.001), \
         patch("backend.ai.engine.engine.get_engine", return_value=SimpleNamespace(execute=slow)), \
         patch.object(handler, "send_reply", AsyncMock()) as send:
        title, body, _ = await handler._run_ai_reply(50, 123, 10, 1, False, "A")
    assert "timed out" in body
    send.assert_not_awaited()
    assert 50 not in handler._ai_states


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["", "   "])
async def test_invalid_response_has_zero_delivery(text):
    arm()
    client = Client([msg(9, "before")])
    engine = SimpleNamespace(execute=AsyncMock(return_value=result(text)))
    with patch.object(handler.inline_engine, "get_self_client", return_value=client), \
         patch.object(handler.inline_engine, "get_owner_id", return_value=99), \
         patch.object(handler, "_load_target_message", AsyncMock(return_value=service.ViewerMessage(10, 123, "target"))), \
         patch("backend.ai.engine.engine.get_engine", return_value=engine), \
         patch.object(handler, "send_reply", AsyncMock()) as send:
        _, body, _ = await handler._run_ai_reply(50, 123, 10, 5, False, "A")
    assert "Couldn't generate" in body
    send.assert_not_awaited()


@pytest.mark.asyncio
async def test_success_delivers_source_and_target_and_disclosure():
    arm()
    client = Client([msg(9, "before")])
    engine = SimpleNamespace(execute=AsyncMock(return_value=result("reply")))
    with patch.object(handler.inline_engine, "get_self_client", return_value=client), \
         patch.object(handler.inline_engine, "get_owner_id", return_value=99), \
         patch.object(handler, "_load_target_message", AsyncMock(return_value=service.ViewerMessage(10, 123, "target"))), \
         patch("backend.ai.engine.engine.get_engine", return_value=engine), \
         patch.object(handler, "send_reply", AsyncMock(return_value={})) as send:
        _, body, _ = await handler._run_ai_reply(50, 123, 10, 1, True, "A")
    assert body == "✓ Reply sent."
    send.assert_awaited_once()
    assert send.await_args.args[1:3] == (123, 10)
    assert "Written with AI assistance" in send.await_args.args[3]
    assert not handler._ai_states


@pytest.mark.asyncio
async def test_duplicate_concurrent_execution_delivers_once():
    arm()
    client = Client([msg(9, "before")])
    async def generate(_):
        await asyncio.sleep(0.01)
        return result("reply")
    engine = SimpleNamespace(execute=generate)
    with patch.object(handler.inline_engine, "get_self_client", return_value=client), \
         patch.object(handler.inline_engine, "get_owner_id", return_value=99), \
         patch.object(handler, "_load_target_message", AsyncMock(return_value=service.ViewerMessage(10, 123, "target"))), \
         patch("backend.ai.engine.engine.get_engine", return_value=engine), \
         patch.object(handler, "send_reply", AsyncMock(return_value={})) as send:
        outcomes = await asyncio.gather(
            handler._run_ai_reply(50, 123, 10, 1, False, "A"),
            handler._run_ai_reply(50, 123, 10, 1, False, "A"),
        )
    assert send.await_count == 1
    assert any("already" in body for _, body, _ in outcomes)


@pytest.mark.asyncio
async def test_permission_removed_before_delivery_fails_closed():
    arm()
    client = Client([msg(9, "before")])
    async def generate(_):
        service.disallow_chat(123)
        return result("reply")
    with patch.object(handler.inline_engine, "get_self_client", return_value=client), \
         patch.object(handler.inline_engine, "get_owner_id", return_value=99), \
         patch.object(handler, "_load_target_message", AsyncMock(return_value=service.ViewerMessage(10, 123, "target"))), \
         patch("backend.ai.engine.engine.get_engine", return_value=SimpleNamespace(execute=generate)), \
         patch.object(handler, "send_reply", AsyncMock()) as send:
        _, body, _ = await handler._run_ai_reply(50, 123, 10, 1, False, "A")
    assert "changed" in body
    send.assert_not_awaited()


def test_context_choices_are_fixed_and_no_prompt_input_exists():
    buttons = handler._context_buttons(123)
    callbacks = [getattr(button, "data", b"").decode() for row in buttons for button in row]
    assert all(any(f":{n}" in callback for callback in callbacks) for n in (1, 5, 10, 20))
    assert "ai_prompt" not in repr(callbacks)
    assert "Type your instruction" not in repr(buttons)
