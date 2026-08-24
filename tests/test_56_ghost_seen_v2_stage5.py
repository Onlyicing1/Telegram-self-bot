"""Ghost Seen v2 Stage 5 — real manual Reply flow (no AI)."""
from __future__ import annotations

import inspect
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from backend.bot.handlers import ghost_seen_v2 as handler_module
from backend.services import ghost_seen_v2 as service_module
from backend.services.ghost_seen_v2 import (
    begin_reply,
    clear_all_replies,
    clear_reply,
    clear_selection,
    consume_reply,
    get_reply,
    get_selected_ids,
    reply_target,
    toggle_selection,
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
    assert begin_reply(100, 20, 7, 55) is False  # nothing selected
    toggle_selection(20, 7)
    toggle_selection(20, 8)
    assert begin_reply(100, 20, 7, 55) is False  # two selected
    clear_selection(20)
    toggle_selection(20, 7)
    assert begin_reply(100, 20, 7, 55) is True
    assert get_reply(100) == (20, 7, 55)


def test_reply_state_is_isolated_by_panel_chat():
    clear_all_replies()
    clear_selection(30)
    toggle_selection(30, 1)
    begin_reply(100, 30, 1, 1)
    begin_reply(200, 30, 1, 1)
    assert get_reply(100) == (30, 1, 1)
    assert get_reply(200) == (30, 1, 1)
    consumed = consume_reply(100)
    assert consumed == (30, 1, 1)
    assert get_reply(100) is None
    assert get_reply(200) == (30, 1, 1)  # other panel chat untouched
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
    assert get_reply(200) == (35, 2, 1)


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
    clear_all_replies()
    clear_selection(40)
    toggle_selection(40, 9)

    with patch.object(handler_module, "_session_extra", return_value=_action_state(40)):
        title, body, buttons = await handler_module._reply_action(_event(777, 123), "40", 777)

    assert "Reply" in body
    assert "Type your instruction" not in body
    assert "input:ghost_chat:ai_prompt" not in body
    assert get_reply(777) == (40, 9, 123)  # armed, keyed by panel chat
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
    clear_all_replies()
    clear_selection(40)
    with patch.object(handler_module, "_session_extra", return_value=_action_state(40)):
        title, body, buttons = await handler_module._reply_action(_event(777, 123), "40", 777)
    assert "Ghost" in body
    assert input_state.get_pending(owner) is None
    assert get_reply(777) is None


# ── Handler: input consumption → Telegram delivery ──


@pytest.mark.asyncio
async def test_reply_input_sends_to_source_with_reply_to_and_clears_selection(monkeypatch):
    from backend.helper import inline_engine

    client = FakeClient()
    monkeypatch.setattr(inline_engine, "_self_client", client)
    clear_all_replies()
    clear_selection(50)
    toggle_selection(50, 11)
    begin_reply(50, 50, 11, 5)

    send_mock = AsyncMock(return_value={"id": 99})
    with patch.object(handler_module, "send_reply", send_mock), patch.object(handler_module, "get_client", return_value=None):
        await handler_module._ghost_reply_input_handler("👋 salam", 50, 321, 0, 0)

    send_mock.assert_awaited_once()
    call = send_mock.await_args
    assert (call.args[0], call.args[1], call.args[2]) == (client, 50, 11)
    assert call.args[3] == "👋 salam"
    assert get_reply(50) is None
    assert get_selected_ids(50) == ()
    assert client.deleted == [(50, [321])]


@pytest.mark.asyncio
async def test_reply_input_failure_is_honest_and_consumes_state(monkeypatch):
    from backend.helper import inline_engine

    client = FakeClient()
    monkeypatch.setattr(inline_engine, "_self_client", client)
    clear_all_replies()
    clear_selection(60)
    toggle_selection(60, 12)
    begin_reply(60, 60, 12, 5)

    with patch.object(handler_module, "send_reply", AsyncMock(side_effect=RuntimeError("boom"))), \
         patch.object(handler_module, "get_client", return_value=client):
        await handler_module._ghost_reply_input_handler("hi", 60, 321, 60, 5)

    assert client.edited is not None
    assert client.edited[0].startswith("❌ Reply failed:")
    assert get_reply(60) is None  # consumed — never double-send


@pytest.mark.asyncio
async def test_reply_input_stale_selection_fails_closed(monkeypatch):
    from backend.helper import inline_engine

    client = FakeClient()
    monkeypatch.setattr(inline_engine, "_self_client", client)
    clear_all_replies()
    clear_selection(60)
    toggle_selection(60, 12)
    begin_reply(60, 60, 12, 5)
    # selection changes while waiting
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


# ── Handler: cancel / back clear pending reply ──


@pytest.mark.asyncio
async def test_reply_cancel_clears_pending_and_state_without_sending(monkeypatch):
    from backend.helper import inline_engine, input_state

    owner = 999
    client = FakeClient((SimpleNamespace(id=3, message="hi"),))
    monkeypatch.setattr(inline_engine, "_owner_id", owner)
    monkeypatch.setattr(inline_engine, "_self_client", client)
    input_state.clear_all()
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
    assert "selected" in body  # back at the action menu with selection preserved


@pytest.mark.asyncio
async def test_actions_back_clears_pending_reply_state(monkeypatch):
    from backend.helper import inline_engine

    client = FakeClient((SimpleNamespace(id=3, message="hi"),))
    monkeypatch.setattr(inline_engine, "_self_client", client)
    clear_all_replies()
    clear_selection(48)
    toggle_selection(48, 2)
    begin_reply(71, 48, 2, 5)
    with patch.object(handler_module, "_session_extra", return_value=_action_state(48)):
        await handler_module._actions_back(_event(71, 123), "48", 71)
    assert get_reply(71) is None


# ── AI Reply stays a placeholder ──


@pytest.mark.asyncio
async def test_ai_reply_remains_inert_placeholder(monkeypatch):
    from backend.helper import inline_engine

    monkeypatch.setattr(inline_engine, "_self_client", None)
    clear_all_replies()
    clear_selection(80)
    toggle_selection(80, 1)
    title, body, buttons = await handler_module._placeholder_action(_event(80, 1), "ai_reply:80", 80)
    assert "Coming in the next stage." in body
    assert "Type your instruction" not in body


# ── Legacy path absent from v2 source ──


def test_no_legacy_prompt_or_path_in_v2_source():
    for module in (service_module, handler_module):
        source = inspect.getsource(module)
        assert "Type your instruction for the selected messages." not in source
        assert "input:ghost_chat:ai_prompt" not in source
        assert "ai_prompt" not in source
        assert "ghost_actions" not in source
        assert "ghost_ctx" not in source
        assert "ghost_inform" not in source
        assert "GHOST_ROOM_ID" not in source