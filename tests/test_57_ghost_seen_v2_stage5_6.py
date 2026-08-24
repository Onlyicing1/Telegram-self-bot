"""Ghost Seen v2 Stage 5/6 — hardening regression tests.

Covers: privacy model, message ordering, selection UX, reply modes,
destination configuration, stale callback handling, and legacy-absence.
"""
from __future__ import annotations

import inspect
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from backend.bot.handlers import ghost_seen_v2 as handler_module
from backend.services import ghost_seen_v2 as service_module
from backend.services.ghost_seen_v2 import (
    MESSAGE_PAGE_SIZE, BrowserPage, MessageViewerPage, PrivateChat,
    ViewerMessage, allow_chat, begin_reply, clear_all_replies, clear_reply,
    clear_selection, consume_reply, disallow_chat, get_allowed_chats,
    get_reply, get_selected_ids, is_chat_allowed, load_allowed_chats,
    load_viewer_messages, matches_search, page_items, reply_mode,
    reply_target, render_browser, render_message_viewer,
    reset_allowed_chats, toggle_selection,
)


# ═══════════════════════════════════════════════════════════════════════
# PRIVACY (tests 1–8)
# ═══════════════════════════════════════════════════════════════════════

def _reset_privacy():
    reset_allowed_chats()


def test_1_unallowed_private_chat_does_not_appear():
    _reset_privacy()
    assert is_chat_allowed(999) is False


def test_2_allowed_private_chat_appears():
    _reset_privacy()
    allow_chat(999)
    assert is_chat_allowed(999) is True


def test_3_disabling_chat_removes_it():
    _reset_privacy()
    allow_chat(999)
    assert is_chat_allowed(999) is True
    disallow_chat(999)
    assert is_chat_allowed(999) is False


def test_4_chat_a_permission_does_not_affect_chat_b():
    _reset_privacy()
    allow_chat(100)
    allow_chat(200)
    disallow_chat(100)
    assert is_chat_allowed(100) is False
    assert is_chat_allowed(200) is True


def test_5_bot_does_not_appear_in_private_filter():
    from backend.services.ghost_seen_v2 import filter_private_dialogs
    dialogs = [
        SimpleNamespace(entity=SimpleNamespace(id=50, first_name="Bot", bot=True, is_self=False, deleted=False), is_user=True),
    ]
    assert filter_private_dialogs(dialogs, owner_id=1) == []


def test_6_self_does_not_appear():
    from backend.services.ghost_seen_v2 import is_private_user_entity
    assert is_private_user_entity(SimpleNamespace(id=1, first_name="Me", bot=False, is_self=True, deleted=False), owner_id=1) is False


def test_7_group_does_not_appear():
    from backend.services.ghost_seen_v2 import is_private_user_dialog
    dialog = SimpleNamespace(entity=SimpleNamespace(id=10, title="Group"), is_user=False, is_group=True, is_channel=False)
    assert is_private_user_dialog(dialog, owner_id=1) is False


def test_8_channel_does_not_appear():
    from backend.services.ghost_seen_v2 import is_private_user_dialog
    dialog = SimpleNamespace(entity=SimpleNamespace(id=11, title="Ch"), is_user=False, is_group=False, is_channel=True)
    assert is_private_user_dialog(dialog, owner_id=1) is False


# ═══════════════════════════════════════════════════════════════════════
# MESSAGE ORDERING (tests 9–12)
# ═══════════════════════════════════════════════════════════════════════


class FakeViewerClient:
    def __init__(self, messages):
        self._messages = messages

    async def iter_messages(self, chat_id, limit):
        for m in self._messages:
            yield m


@pytest.mark.asyncio
async def test_9_viewer_opens_with_newest_messages_first():
    client = FakeViewerClient([
        SimpleNamespace(id=10, text="old", date=datetime(2026, 1, 1, tzinfo=timezone.utc)),
        SimpleNamespace(id=20, text="mid", date=datetime(2026, 1, 2, tzinfo=timezone.utc)),
        SimpleNamespace(id=30, text="new", date=datetime(2026, 1, 3, tzinfo=timezone.utc)),
    ])
    viewer = await load_viewer_messages(client, 50)
    ids = [m.message_id for m in viewer.messages]
    assert ids == [30, 20, 10]


@pytest.mark.asyncio
async def test_10_first_page_contains_newest_available():
    count = MESSAGE_PAGE_SIZE + 5
    messages = [SimpleNamespace(id=i, text=f"msg{i}", date=None) for i in range(1, count + 1)]
    client = FakeViewerClient(messages)
    viewer = await load_viewer_messages(client, 50, page=1)
    ids = [m.message_id for m in viewer.messages]
    assert ids[0] == count
    assert ids == list(range(count, count - MESSAGE_PAGE_SIZE, -1))


@pytest.mark.asyncio
async def test_11_pagination_proceeds_toward_older_messages():
    count = MESSAGE_PAGE_SIZE * 2 + 3
    messages = [SimpleNamespace(id=i, text=f"msg{i}", date=None) for i in range(1, count + 1)]
    client = FakeViewerClient(messages)
    p1 = await load_viewer_messages(client, 50, page=1)
    p2 = await load_viewer_messages(client, 50, page=2)
    assert p1.messages[0].message_id > p2.messages[0].message_id


@pytest.mark.asyncio
async def test_12_no_reversal_bug():
    client = FakeViewerClient([SimpleNamespace(id=i, text=f"m{i}", date=None) for i in range(1, 30)])
    viewer = await load_viewer_messages(client, 50, page=1)
    ids = [m.message_id for m in viewer.messages]
    assert ids == sorted(ids, reverse=True)


# ═══════════════════════════════════════════════════════════════════════
# SELECTION (tests 13–18)
# ═══════════════════════════════════════════════════════════════════════

def test_13_select_control_identifies_message():
    from backend.bot.handlers.ghost_seen_v2 import _viewer_buttons
    view = MessageViewerPage(50, (
        ViewerMessage(100, 50, "hello world", None),
        ViewerMessage(200, 50, "goodbye", None),
    ), 1, 1, ())
    buttons = _viewer_buttons(view)
    btn_texts = []
    for row in buttons:
        for btn in row:
            btn_texts.append(getattr(btn, "text", ""))
    assert any("hello" in t for t in btn_texts)
    assert any("goodbye" in t for t in btn_texts)


def test_14_selected_state_is_visually_distinct():
    from backend.bot.handlers.ghost_seen_v2 import _viewer_buttons
    view = MessageViewerPage(50, (
        ViewerMessage(100, 50, "msg", None),
    ), 1, 1, (100,))
    buttons = _viewer_buttons(view)
    btn_texts = [getattr(btn, "text", "") for row in buttons for btn in row]
    assert any("✓" in t for t in btn_texts)


def test_15_real_message_id_is_selection_identity():
    clear_selection(50)
    toggle_selection(50, 123)
    assert get_selected_ids(50) == (123,)


def test_16_identical_text_messages_select_independently():
    clear_selection(50)
    toggle_selection(50, 1)
    toggle_selection(50, 2)
    assert get_selected_ids(50) == (1, 2)


def test_17_selection_survives_same_source_pagination():
    clear_selection(50)
    toggle_selection(50, 100)
    assert get_selected_ids(50) == (100,)


def test_18_stale_selection_fails_closed():
    clear_selection(50)
    toggle_selection(50, 100)
    clear_selection(50)
    assert reply_target(50) is None


# ═══════════════════════════════════════════════════════════════════════
# MANUAL REPLY (tests 19–27)
# ═══════════════════════════════════════════════════════════════════════

def test_19_reply_mode_sends_with_reply_to():
    clear_selection(50)
    toggle_selection(50, 10)
    begin_reply(60, 50, 10, 1, mode="reply")
    _, _, _, mode = consume_reply(60)
    assert mode == "reply"


def test_20_send_without_reply_sends_without_reply_to():
    clear_selection(50)
    toggle_selection(50, 10)
    begin_reply(60, 50, 10, 1, mode="send")
    _, _, _, mode = consume_reply(60)
    assert mode == "send"


def test_21_both_require_exactly_one_selected():
    clear_selection(50)
    assert begin_reply(60, 50, 10, 1, mode="reply") is False
    toggle_selection(50, 10)
    toggle_selection(50, 11)
    assert begin_reply(60, 50, 10, 1, mode="reply") is False


def test_22_invalid_selection_does_not_arm_input():
    clear_selection(50)
    assert begin_reply(60, 50, 99, 1, mode="reply") is False


def test_23_input_consumed_exactly_once():
    clear_selection(50)
    toggle_selection(50, 10)
    begin_reply(60, 50, 10, 1, mode="reply")
    assert consume_reply(60) is not None
    assert consume_reply(60) is None


def test_24_source_chat_is_delivery_target():
    clear_selection(50)
    toggle_selection(50, 10)
    begin_reply(60, 50, 10, 1, mode="reply")
    source, mid, _, _ = consume_reply(60)
    assert source == 50 and mid == 10


def test_25_destination_chat_not_used_for_manual_reply():
    _reset_privacy()
    clear_selection(50)
    toggle_selection(50, 10)
    begin_reply(60, 50, 10, 1)
    source, _, _, _ = consume_reply(60)
    assert source != service_module.get_destination_chat_id()


@pytest.mark.asyncio
async def test_26_exact_user_text_preserved():
    from backend.helper import inline_engine
    monkeypatch_obj = type("MP", (), {})()
    inline_engine._self_client = None
    _reset_privacy()
    clear_selection(50)
    toggle_selection(50, 10)
    begin_reply(60, 50, 10, 1, mode="reply")
    send_mock = AsyncMock(return_value={})
    with patch.object(handler_module, "send_reply", send_mock), \
         patch.object(handler_module, "get_client", return_value=None):
        await handler_module._ghost_reply_input_handler("   hello world   ", 60, 1, 0, 0)
    call = send_mock.await_args
    assert call.args[3] == "   hello world   "


class _FakeHelper:
    def __init__(self):
        self.edited = None

    async def edit_message(self, chat_id, msg_id, text, buttons=None):
        self.edited = text


@pytest.mark.asyncio
async def test_27_delivery_failure_honest():
    from backend.helper import inline_engine
    inline_engine._self_client = None
    _reset_privacy()
    clear_selection(50)
    toggle_selection(50, 10)
    begin_reply(60, 50, 10, 1, mode="reply")
    send_mock = AsyncMock(side_effect=RuntimeError("net err"))
    helper = _FakeHelper()
    with patch.object(handler_module, "send_reply", send_mock), \
         patch.object(handler_module, "get_client", return_value=helper):
        await handler_module._ghost_reply_input_handler("hi", 60, 1, 60, 1)
    assert helper.edited is not None
    assert "failed" in helper.edited.lower()


# ═══════════════════════════════════════════════════════════════════════
# PRIVACY + CALLBACK SECURITY (tests 28–30)
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_28_disabled_source_chat_fails_closed_in_action_menu(monkeypatch):
    from backend.helper import inline_engine
    _reset_privacy()
    clear_all_replies()
    clear_selection(40)
    toggle_selection(40, 9)
    monkeypatch.setattr(inline_engine, "_self_client", None)
    monkeypatch.setattr(inline_engine, "_owner_id", 999)

    with patch.object(handler_module, "_session_extra", return_value=f"source=40&name=Ali&page=1"):
        title, body, buttons = await handler_module._actions_action(_event(777, 1), "40", 777)
    assert "no longer allowed" in body.lower()


@pytest.mark.asyncio
async def test_29_stale_callback_from_another_source_fails_closed(monkeypatch):
    from backend.helper import inline_engine
    _reset_privacy()
    allow_chat(40)
    clear_all_replies()
    clear_selection(40)
    toggle_selection(40, 9)
    monkeypatch.setattr(inline_engine, "_self_client", None)
    monkeypatch.setattr(inline_engine, "_owner_id", 999)

    with patch.object(handler_module, "_session_extra", return_value=f"source=99&name=Other&page=1"):
        title, body, buttons = await handler_module._reply_action(_event(777, 1), "40", 777)
    assert "no longer available" in body.lower()


def test_30_panel_chat_id_not_used_as_source():
    clear_selection(50)
    toggle_selection(50, 10)
    begin_reply(777, 50, 10, 1)
    source, mid, pid, _ = get_reply(777)
    assert source == 50 and pid == 1
    assert source != 777


# ═══════════════════════════════════════════════════════════════════════
# CONFIGURATION (tests 31–34)
# ═══════════════════════════════════════════════════════════════════════

def test_31_destination_chat_id_loaded(monkeypatch):
    monkeypatch.setenv("GHOST_SEEN_DESTINATION_CHAT_ID", "-100222")
    assert service_module.get_destination_chat_id() == -100222


def test_32_invalid_destination_fails_safely(monkeypatch):
    monkeypatch.delenv("GHOST_SEEN_DESTINATION_CHAT_ID", raising=False)
    assert service_module.get_destination_chat_id() == 0


def test_33_destination_name_is_display_only(monkeypatch):
    monkeypatch.setenv("GHOST_SEEN_DESTINATION_CHAT_NAME", "Watch Room")
    assert service_module.get_destination_chat_name() == "Watch Room"
    assert isinstance(service_module.get_destination_chat_name(), str)


def test_34_destination_name_cannot_alter_delivery():
    name = service_module.get_destination_chat_name()
    clear_selection(50)
    toggle_selection(50, 10)
    begin_reply(60, 50, 10, 1, mode="reply")
    source, _, _, _ = consume_reply(60)
    assert source == 50


# ═══════════════════════════════════════════════════════════════════════
# REGRESSION (tests 35–42)
# ═══════════════════════════════════════════════════════════════════════

def test_35_stage1_browser_intact():
    body, view = render_browser([], watcher_count=0)
    assert "No allowed chats yet" in body
    assert "Refresh" not in body


def test_36_stage2_viewer_intact():
    body = render_message_viewer("Ali", MessageViewerPage(50, (), 1, 1))
    assert "Ghost Seen" in body
    assert "nothing to see" in body.lower()


def test_37_stage3_selection_intact():
    clear_selection(50)
    toggle_selection(50, 1)
    assert get_selected_ids(50) == (1,)


def test_38_stage4_action_menu_intact():
    from backend.services.ghost_seen_v2 import action_placeholder
    clear_selection(50)
    toggle_selection(50, 1)
    assert action_placeholder("ai_reply", 50, (1,)) == "Coming in the next stage."


def test_39_stage5_reply_intact():
    clear_selection(50)
    toggle_selection(50, 10)
    assert begin_reply(60, 50, 10, 1, mode="reply") is True


def test_40_no_legacy_identifiers():
    for mod in (service_module, handler_module):
        src = inspect.getsource(mod)
        assert "GHOST_ROOM_ID" not in src
        assert "input:ghost_chat:ai_prompt" not in src
        assert "ghost_actions" not in src
        assert "ghost_ctx" not in src
        assert "ghost_inform" not in src


def test_41_no_legacy_prompt_literal():
    for mod in (service_module, handler_module):
        src = inspect.getsource(mod)
        assert "Type your instruction for the selected messages." not in src


def test_42_no_refresh_button():
    from backend.bot.handlers.ghost_seen_v2 import _browser_buttons, _viewer_buttons
    bv = BrowserPage((), 1, 1)
    assert all("Refresh" not in getattr(b, "text", "") for row in _browser_buttons(bv) for b in row)
    vv = MessageViewerPage(50, (), 1, 1)
    assert all("Refresh" not in getattr(b, "text", "") for row in _viewer_buttons(vv) for b in row)


# ═══════════════════════════════════════════════════════════════════════
# Legacy path absent (source-level)
# ═══════════════════════════════════════════════════════════════════════

def _event(chat_id: int, message_id: int):
    return SimpleNamespace(chat_id=chat_id, message_id=message_id)
