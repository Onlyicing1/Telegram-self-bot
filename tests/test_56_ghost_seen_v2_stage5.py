"""Ghost Seen v2 Stage 5 — real manual Reply flow (no AI)."""
from __future__ import annotations

import inspect
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from backend.bot.handlers import ghost_seen_v2 as handler_module
from backend.services import ghost_seen_v2 as service_module
from backend.services.ghost_seen_v2 import (
    allow_chat, begin_reply, clear_all_replies, clear_reply, clear_selection,
    consume_reply, disallow_chat, get_reply, get_selected_ids, reply_target,
    reset_allowed_chats, toggle_selection,
)


# ── Service: reply target cardinality ──


def test_reply_target_requires_exactly_one_selection():
    clear_selection(10)
    assert reply_target(10) is None
    toggle_selection(10, 4)
    assert reply_target(10) == 4
    toggle_selection(10, 5)
    assert reply_target(10) is None
    clear_selection(10)
    assert reply_target(10) is None


# ── Service: reply state is panel-chat keyed and validated ──


def test_begin_reply_arms_only_for_exact_single_selection():
    clear_all_replies()
    clear_selection(20)
    assert begin_reply(100, 20, 7, 55) is False
    toggle_selection(20, 7)
    toggle_selection(20, 8)
    assert begin_reply(100, 20, 7, 55) is False
    clear_selection(20)
    toggle_selection(20, 7)
    assert begin_reply(100, 20, 7, 55) is True
    source, mid, pid, mode = get_reply(100)
    assert (source, mid, pid, mode) == (20, 7, 55, "reply")


def test_begin_reply_with_send_mode():
    clear_all_replies()
    clear_selection(20)
    toggle_selection(20, 7)
    assert begin_reply(100, 20, 7, 55, mode="send") is True
    source, mid, pid, mode = get_reply(100)
    assert mode == "send"


def test_reply_state_is_isolated_by_panel_chat():
    clear_all_replies()
    clear_selection(30)
    toggle_selection(30, 1)
    begin_reply(100, 30, 1, 1)
    begin_reply(200, 30, 1, 1)
    assert get_reply(100)[:3] == (30, 1, 1)
    assert get_reply(200)[:3] == (30, 1, 1)
    consumed = consume_reply(100)
    assert consumed[:3] == (30, 1, 1)
    assert get_reply(100) is None
    assert get_reply(200)[:3] == (30, 1, 1)
    clear_all_replies()
    assert get_reply(200) is None


def test_clear_reply_removes_only_that_panel_chat():
    clear_all_replies()
    clear_selection(35)
    toggle_selection(35, 2)
    begin_reply(100, 35, 2, 1)
    begin_reply(200, 35, 2, 1)
    clear_reply(100)
    assert get_reply(100) is None
    assert get_reply(200)[:3] == (35, 2, 1)


# ── Privacy model ──


def test_privacy_default_blocks_all_chats():
    reset_allowed_chats()
    assert service_module.is_chat_allowed(12345) is False
    assert service_module.is_chat_allowed(99999) is False


def test_allow_and_disallow_chat():
    reset_allowed_chats()
    allow_chat(100)
    assert service_module.is_chat_allowed(100) is True
    assert service_module.is_chat_allowed(200) is False
    disallow_chat(100)
    assert service_module.is_chat_allowed(100) is False


def test_chat_a_permission_does_not_affect_chat_b():
    reset_allowed_chats()
    allow_chat(100)
    allow_chat(200)
    disallow_chat(100)
    assert service_module.is_chat_allowed(100) is False
    assert service_module.is_chat_allowed(200) is True


# ── Destination config ──


def test_destination_config_reads_env(monkeypatch):
    monkeypatch.setenv("GHOST_SEEN_DESTINATION_CHAT_ID", "-100123456")
    monkeypatch.setenv("GHOST_SEEN_DESTINATION_CHAT_NAME", "Ghost Watch")
    assert service_module.get_destination_chat_id() == -100123456
    assert service_module.get_destination_chat_name() == "Ghost Watch"


def test_destination_config_defaults():
    import os
    os.environ.pop("GHOST_SEEN_DESTINATION_CHAT_ID", None)
    os.environ.pop("GHOST_SEEN_DESTINATION_CHAT_NAME", None)
    assert service_module.get_destination_chat_id() == 0
    assert service_module.get_destination_chat_name() == ""


# ── Helpers ──


def _event(chat_id: int, message_id: int):
    return SimpleNamespace(chat_id=chat_id, message_id=message_id)


def _action_state(source: int, name: str = "Ali") -> str:
    return f"source={source}&name={name}&page=1"


class FakeClient:
    def __init__(self, iter_messages=()):
        self._iter_messages = iter_messages
        self.deleted = []
        self.edited = None

    async def iter_messages(self, chat_id, limit):
        for message in self._iter_messages:
            yield message

    async def delete_messages(self, chat_id, ids):
        self.deleted.append((chat_id, ids))

    async def edit_message(self, chat_id, msg_id, message, buttons=None):
        self.edited = (message, buttons)


# ── Handler: Reply action ──


@pytest.mark.asyncio
async def test_reply_action_arms_input_for_exact_one_selection(monkeypatch):
    from backend.helper import inline_engine, input_state

    owner = 999
    monkeypatch.setattr(inline_engine, "_owner_id", owner)
    monkeypatch.setattr(inline_engine, "_self_client", None)
    input_state.clear_all()
    reset_allowed_chats()
    allow_chat(40)
    clear_all_replies()
    clear_selection(40)
    toggle_selection(40, 9)

    with patch.object(handler_module, "_session_extra", return_value=_action_state(40)):
        title, body, buttons = await handler_module._reply_action(_event(777, 123), "40", 777)

    assert "Reply" in body
    assert "Type your instruction" not in body
    assert "input:ghost_chat:ai_prompt" not in body
    assert get_reply(777)[:3] == (40, 9, 123)
    pending = input_state.get_pending(owner)
    assert pending is not None
    assert pending["panel_id"] == "ghost_seen_v2"
    cancel_callback = "action:ghost_seen_v2_reply_cancel:40"
    assert any(
        cancel_callback.encode() in (b.data if isinstance(getattr(b, "data", None), bytes) else str(getattr(b, "data", "")).encode())
        for row in buttons for b in row
    )


@pytest.mark.asyncio
async def test_reply_action_fails_closed_for_two_selected(monkeypatch):
    from backend.helper import inline_engine, input_state

    owner = 999
    monkeypatch.setattr(inline_engine, "_owner_id", owner)
    monkeypatch.setattr(inline_engine, "_self_client", None)
    input_state.clear_all()
    reset_allowed_chats()
    allow_chat(40)
    clear_all_replies()
    clear_selection(40)
    toggle_selection(40, 9)
    toggle_selection(40, 10)

    with patch.object(handler_module, "_session_extra", return_value=_action_state(40)):
        title, body, buttons = await handler_module._reply_action(_event(777, 123), "40", 777)

    assert "exactly one" in body
    assert input_state.get_pending(owner) is None
    assert get_reply(777) is None


@pytest.mark.asyncio
async def test_reply_action_stale_zero_selection_returns_viewer(monkeypatch):
    from backend.helper import inline_engine, input_state

    owner = 999
    client = FakeClient((SimpleNamespace(id=3, message="hi"),))
    monkeypatch.setattr(inline_engine, "_owner_id", owner)
    monkeypatch.setattr(inline_engine, "_self_client", client)
    input_state.clear_all()
    reset_allowed_chats()
    clear_all_replies()
    clear_selection(40)
    with patch.object(handler_module, "_session_extra", return_value=_action_state(40)):
        title, body, buttons = await handler_module._reply_action(_event(777, 123), "40", 777)
    assert "Ghost" in body
    assert input_state.get_pending(owner) is None
    assert get_reply(777) is None


# ── Handler: send-without-reply ──


@pytest.mark.asyncio
async def test_send_plain_action_arms_input(monkeypatch):
    from backend.helper import inline_engine, input_state

    owner = 999
    monkeypatch.setattr(inline_engine, "_owner_id", owner)
    monkeypatch.setattr(inline_engine, "_self_client", None)
    input_state.clear_all()
    reset_allowed_chats()
    allow_chat(40)
    clear_all_replies()
    clear_selection(40)
    toggle_selection(40, 9)

    with patch.object(handler_module, "_session_extra", return_value=_action_state(40)):
        title, body, buttons = await handler_module._send_plain_action(_event(777, 123), "40", 777)

    assert "Send" in body
    assert "without" in body.lower() or "reply quote" in body.lower()
    assert get_reply(777)[:3] == (40, 9, 123)
    _, _, _, mode = consume_reply(777)
    assert mode == "send"


# ── Handler: input consumption ──


@pytest.mark.asyncio
async def test_reply_input_sends_to_source_with_reply_to_and_clears_selection(monkeypatch):
    from backend.helper import inline_engine

    client = FakeClient()
    monkeypatch.setattr(inline_engine, "_self_client", client)
    reset_allowed_chats()
    allow_chat(50)
    clear_all_replies()
    clear_selection(50)
    toggle_selection(50, 11)
    begin_reply(50, 50, 11, 5, mode="reply")

    send_mock = AsyncMock(return_value={"id": 99})
    with patch.object(handler_module, "send_reply", send_mock), patch.object(handler_module, "get_client", return_value=None):
        await handler_module._ghost_reply_input_handler("👋 salam", 50, 321, 0, 0)

    send_mock.assert_awaited_once()
    call = send_mock.await_args
    assert (call.args[0], call.args[1], call.args[2]) == (client, 50, 11)
    assert call.args[3] == "👋 salam"
    assert get_reply(50) is None
    assert get_selected_ids(50) == ()


@pytest.mark.asyncio
async def test_send_plain_input_sends_without_reply_to(monkeypatch):
    from backend.helper import inline_engine

    client = FakeClient()
    monkeypatch.setattr(inline_engine, "_self_client", client)
    clear_all_replies()
    clear_selection(50)
    toggle_selection(50, 11)
    begin_reply(50, 50, 11, 5, mode="send")

    send_mock = AsyncMock(return_value={"id": 99})
    with patch.object(handler_module, "send_message_plain", send_mock), patch.object(handler_module, "get_client", return_value=None):
        await handler_module._ghost_reply_input_handler("test msg", 50, 321, 0, 0)

    send_mock.assert_awaited_once()
    call = send_mock.await_args
    assert (call.args[0], call.args[1]) == (client, 50)
    assert call.args[2] == "test msg"
    assert get_reply(50) is None


@pytest.mark.asyncio
async def test_reply_input_failure_is_honest_and_consumes_state(monkeypatch):
    from backend.helper import inline_engine

    client = FakeClient()
    monkeypatch.setattr(inline_engine, "_self_client", client)
    reset_allowed_chats()
    allow_chat(60)
    clear_all_replies()
    clear_selection(60)
    toggle_selection(60, 12)
    begin_reply(60, 60, 12, 5, mode="reply")

    with patch.object(handler_module, "send_reply", AsyncMock(side_effect=RuntimeError("boom"))), \
         patch.object(handler_module, "get_client", return_value=client):
        await handler_module._ghost_reply_input_handler("hi", 60, 321, 60, 5)

    assert client.edited is not None
    assert client.edited[0].startswith("❌ Send failed:")
    assert get_reply(60) is None


@pytest.mark.asyncio
async def test_reply_input_stale_selection_fails_closed(monkeypatch):
    from backend.helper import inline_engine

    client = FakeClient()
    monkeypatch.setattr(inline_engine, "_self_client", client)
    clear_all_replies()
    clear_selection(60)
    toggle_selection(60, 12)
    begin_reply(60, 60, 12, 5, mode="reply")
    clear_selection(60)
    toggle_selection(60, 99)

    with patch.object(handler_module, "send_reply") as send_mock, \
         patch.object(handler_module, "get_client", return_value=client):
        await handler_module._ghost_reply_input_handler("hi", 60, 321, 60, 5)

    send_mock.assert_not_called()
    assert "Nothing was sent" in client.edited[0]
    assert get_selected_ids(60) == (99,)


@pytest.mark.asyncio
async def test_reply_input_without_state_fails_closed(monkeypatch):
    from backend.helper import inline_engine

    client = FakeClient()
    monkeypatch.setattr(inline_engine, "_self_client", client)
    clear_all_replies()

    with patch.object(handler_module, "send_reply") as send_mock, \
         patch.object(handler_module, "get_client", return_value=client):
        await handler_module._ghost_reply_input_handler("hi", 61, 321, 61, 5)

    send_mock.assert_not_called()
    assert "expired" in client.edited[0].lower()


# ── Handler: cancel ──


@pytest.mark.asyncio
async def test_reply_cancel_clears_pending_and_state(monkeypatch):
    from backend.helper import inline_engine, input_state

    owner = 999
    client = FakeClient((SimpleNamespace(id=3, message="hi"),))
    monkeypatch.setattr(inline_engine, "_owner_id", owner)
    monkeypatch.setattr(inline_engine, "_self_client", client)
    input_state.clear_all()
    reset_allowed_chats()
    allow_chat(70)
    clear_all_replies()
    clear_selection(70)
    toggle_selection(70, 3)
    begin_reply(70, 70, 3, 5)

    with patch.object(handler_module, "_session_extra", return_value=_action_state(70)), \
         patch.object(handler_module, "send_reply") as send_mock:
        title, body, buttons = await handler_module._reply_cancel_action(_event(70, 123), "70", 70)

    send_mock.assert_not_called()
    assert get_reply(70) is None
    assert input_state.get_pending(owner) is None
    assert "selected" in body


# ── AI Reply stays placeholder ──


@pytest.mark.asyncio
async def test_ai_reply_remains_inert_placeholder(monkeypatch):
    from backend.helper import inline_engine

    monkeypatch.setattr(inline_engine, "_self_client", None)
    reset_allowed_chats()
    allow_chat(80)
    clear_all_replies()
    clear_selection(80)
    toggle_selection(80, 1)
    title, body, buttons = await handler_module._placeholder_action(_event(80, 1), "ai_reply:80", 80)
    assert "Coming in the next stage." in body
    assert "Type your instruction" not in body


# ── Legacy path absent ──


def test_no_legacy_prompt_or_path_in_v2_source():
    for module in (service_module, handler_module):
        source = inspect.getsource(module)
        assert "Type your instruction for the selected messages." not in source
        assert "input:ghost_chat:ai_prompt" not in source
        assert "ai_prompt" not in source
        assert "GHOST_ROOM_ID" not in source
        assert "ghost_actions" not in source
        assert "ghost_ctx" not in source
        assert "ghost_inform" not in source
