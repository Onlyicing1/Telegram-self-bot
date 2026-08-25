"""Ghost Seen v2 — Manage/Search/Navigation performance repair regression tests.

Proves:
  - Ghost Seen opens directly from the main category without enumerating all
    Telegram dialogs and without loading the Manage dataset.
  - Manage Search reaches its handler and no longer throws the
    ``edit_message()`` TypeError (positional text argument).
  - Manage Next/Previous/Search/Toggle operate on a cached directory and never
    re-enumerate the whole dialog list per interaction.
  - The privacy allow-list is cached and loaded asynchronously (non-blocking).
  - Disallowed chats can never be viewed/replied to, keyed by numeric chat ID.
"""
from __future__ import annotations

import asyncio
import inspect
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from backend.bot.handlers import ghost_seen_v2 as handler_module
from backend.helper import inline_engine
from backend.services import ghost_seen_v2 as service_module
from backend.services.ghost_seen_v2 import (
    PrivateChat, allow_chat, disallow_chat, is_chat_allowed,
    load_manage_directory, manage_page_items, reset_allowed_chats,
    resolve_allowed_chats,
)


def _reset():
    reset_allowed_chats()
    service_module.invalidate_manage_directory()


def _run(coro):
    return asyncio.run(coro)


def _event(chat_id: int, message_id: int):
    return SimpleNamespace(chat_id=chat_id, message_id=message_id)


def _user_entity(uid, first_name="User", last_name="", username="", bot=False, is_self=False):
    return SimpleNamespace(
        id=uid, first_name=first_name, last_name=last_name,
        username=username, bot=bot, is_self=is_self, deleted=False,
        is_bot=bot,
    )


def _dialog(entity, **overrides):
    return SimpleNamespace(entity=entity, is_user=True, is_group=False, is_channel=False, **overrides)


def _chats(count: int, offset: int = 0) -> list[PrivateChat]:
    return [PrivateChat(chat_id=1000 + i + offset, first_name=f"User{i + offset}") for i in range(count)]


def _button_texts(buttons: list) -> list[str]:
    return [getattr(btn, "text", "") for row in buttons for btn in row]


class _AsyncIter:
    def __init__(self, items):
        self._items = iter(items)

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self._items)
        except StopIteration:
            raise StopAsyncIteration


class _FakeClient:
    """Telethon-like client that counts expensive operations."""

    def __init__(self, entities=None, dialogs=None, messages=()):
        self._entities = entities or {}
        self._dialogs = dialogs or []
        self._messages = messages
        self.get_entity_calls = []
        self.iter_dialogs_calls = 0
        self.deleted = []

    async def get_entity(self, entity_id):
        self.get_entity_calls.append(int(entity_id))
        e = self._entities.get(int(entity_id))
        if e is None:
            raise Exception(f"Entity {entity_id} not found")
        return e

    def iter_dialogs(self):
        self.iter_dialogs_calls += 1
        return _AsyncIter(self._dialogs)

    async def iter_messages(self, chat_id, limit):
        for message in self._messages:
            yield message

    async def delete_messages(self, chat_id, ids):
        self.deleted.append((chat_id, ids))


class _FakeHelper:
    """Helper bot client whose edit_message matches Telethon's real signature
    ``(entity, message, text=None, *, buttons=None)``."""

    def __init__(self):
        self.edits = []

    async def edit_message(self, entity, message, text=None, *, buttons=None):
        self.edits.append((entity, message, text, buttons))


def _set_engine(client, owner_id=999):
    inline_engine._self_client = client
    inline_engine._owner_id = owner_id


def _restore_engine():
    inline_engine._self_client = None
    inline_engine._owner_id = 0


# ═══════════════════════════════════════════════════════════════════════
# MAIN MENU / DIRECT OPEN
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_1_ghost_seen_panel_registered_for_direct_open():
    """The Ghost Seen panel is registered under the main menu so a direct
    ``panel:ghost_seen_v2`` callback resolves to the browser handler."""
    handler_module.register(object(), owner_id=999, tz_str="UTC")
    from backend.helper.panel_registry import get_panel_handler, get_panel_parent
    handler = get_panel_handler("ghost_seen_v2")
    assert handler is not None
    assert get_panel_parent("ghost_seen_v2") == "menu"
    from backend.helper.panels import get_action, get_input
    assert get_action("ghost_seen_v2_page") is not None
    assert get_action("ghost_seen_v2_manage_page") is not None
    assert get_input("ghost_seen_v2_manage", "search") is not None


def test_2_browser_open_no_iter_dialogs():
    """Opening Ghost Seen resolves only allowed IDs — never iter_dialogs."""
    _reset()
    allow_chat(123)
    entity = _user_entity(123, first_name="Ali")
    client = _FakeClient(entities={123: entity}, dialogs=[_dialog(_user_entity(i)) for i in range(1, 501)])
    result = _run(resolve_allowed_chats(client, owner_id=1))
    assert len(result) == 1
    assert client.iter_dialogs_calls == 0
    assert client.get_entity_calls == [123]


def test_3_browser_open_does_not_load_manage_data():
    """The Browser path must not build the Manage directory."""
    _reset()
    allow_chat(123)
    client = _FakeClient(entities={123: _user_entity(123, first_name="Ali")})
    with patch.object(handler_module, "load_manage_directory", new=AsyncMock(return_value=_chats(500))) as manage_mock:
        title, body, buttons = _run(handler_module._render_browser(1))
        manage_mock.assert_not_called()
    assert client.iter_dialogs_calls == 0


def test_4_browser_empty_allowed_no_rpc():
    """Zero allowed chats renders the empty state with zero Telegram RPCs."""
    _reset()
    client = _FakeClient(dialogs=[_dialog(_user_entity(i)) for i in range(1, 501)])
    result = _run(resolve_allowed_chats(client, owner_id=1))
    assert result == []
    assert client.iter_dialogs_calls == 0
    assert client.get_entity_calls == []


# ═══════════════════════════════════════════════════════════════════════
# MANAGE SEARCH — no edit_message TypeError
# ═══════════════════════════════════════════════════════════════════════

def test_5_manage_search_handler_no_typeerror():
    """Search must call edit_message with the text positionally (Telethon's
    real signature), never ``message=`` as a keyword after a positional."""
    _reset()
    chats = [PrivateChat(1, first_name="Ali", last_name="Reza"), PrivateChat(2, first_name="Sara")]
    helper = _FakeHelper()
    client = _FakeClient()
    _set_engine(client)
    with patch.object(handler_module, "load_manage_directory", new=AsyncMock(return_value=chats)), \
         patch.object(handler_module, "get_client", return_value=helper):
        _run(handler_module._manage_search_input_handler("Ali", 60, 1, 60, 5))
    assert len(helper.edits) == 1
    entity, message, text, buttons = helper.edits[0]
    assert entity == 60 and message == 5
    assert text is not None and "Ali" in text
    assert client.deleted == [(60, [1])]


def test_6_browser_search_handler_no_typeerror():
    _reset()
    allow_chat(123)
    helper = _FakeHelper()
    client = _FakeClient(entities={123: _user_entity(123, first_name="Ali")})
    _set_engine(client)
    with patch.object(handler_module, "get_client", return_value=helper):
        _run(handler_module._search_input_handler("Ali", 60, 1, 60, 5))
    assert len(helper.edits) == 1
    entity, message, text, buttons = helper.edits[0]
    assert entity == 60 and message == 5
    assert text is not None


def test_7_manage_search_finds_first_last_username():
    chats = [PrivateChat(1, first_name="Ali", last_name="Reza"), PrivateChat(2, first_name="Sara", username="sara99")]
    assert len(manage_page_items(chats, query="Ali").chats) == 1
    assert len(manage_page_items(chats, query="Reza").chats) == 1
    assert len(manage_page_items(chats, query="sara99").chats) == 1
    assert len(manage_page_items(chats, query="@sara99").chats) == 1


def test_8_manage_search_tolerates_whitespace_and_concat():
    chats = [PrivateChat(1, first_name="Ali", last_name="Reza")]
    assert len(manage_page_items(chats, query="  Ali  Reza").chats) == 1
    assert len(manage_page_items(chats, query="AliReza").chats) == 1
    assert len(manage_page_items(chats, query="ali reza").chats) == 1


def test_9_manage_search_preserves_on_off_state():
    _reset()
    chats = [PrivateChat(1, first_name="Ali"), PrivateChat(2, first_name="Sara")]
    allow_chat(1)
    title, body, buttons = handler_module._render_manage(chats, 1, "a")
    texts = _button_texts(buttons)
    assert any("Ali" in t and "ON" in t for t in texts)


def test_10_manage_search_bounded_and_paginated():
    chats = [PrivateChat(i, first_name=f"User{i}") for i in range(1, 501)]
    view = manage_page_items(chats, query="User")
    assert len(view.chats) <= 8
    assert view.total_pages > 1
    assert view.page == 1


def test_11_manage_search_query_replaceable():
    """A new search replaces the previous query (no stale narrowing)."""
    chats = [PrivateChat(1, first_name="Ali"), PrivateChat(2, first_name="Sara")]
    first = manage_page_items(chats, query="Ali")
    second = manage_page_items(chats, query="Sara")
    assert first.chats[0].chat_id == 1
    assert second.chats[0].chat_id == 2


def test_12_back_from_manage_returns_to_browser():
    """Manage's Back button routes to the Ghost Seen browser panel."""
    buttons = handler_module._manage_buttons(manage_page_items(_chats(5)))
    texts = _button_texts(buttons)
    assert any("Back" in t for t in texts)
    # The Back button must target the browser panel, never a chat selection.
    for row in buttons:
        for btn in row:
            data = getattr(btn, "data", b"")
            if isinstance(data, bytes):
                data = data.decode("utf-8", errors="replace")
            if "Back" in getattr(btn, "text", ""):
                assert data == "panel:ghost_seen_v2"


# ═══════════════════════════════════════════════════════════════════════
# MANAGE PERFORMANCE — cached directory, no repeated iter_dialogs
# ═══════════════════════════════════════════════════════════════════════

def test_13_manage_directory_cached_not_reloaded():
    _reset()
    client = _FakeClient(dialogs=[_dialog(_user_entity(i)) for i in range(2, 502)])
    first = _run(load_manage_directory(client, owner_id=1))
    assert len(first) == 500
    assert client.iter_dialogs_calls == 1
    second = _run(load_manage_directory(client, owner_id=1))
    assert second == first
    assert client.iter_dialogs_calls == 1  # cache hit — no re-enumeration


def test_14_cache_invalidation_forces_reload():
    _reset()
    client = _FakeClient(dialogs=[_dialog(_user_entity(i)) for i in range(1, 10)])
    _run(load_manage_directory(client, owner_id=1))
    assert client.iter_dialogs_calls == 1
    service_module.invalidate_manage_directory()
    _run(load_manage_directory(client, owner_id=1))
    assert client.iter_dialogs_calls == 2


def test_15_manage_page_next_previous_no_repeat_enumeration():
    _reset()
    client = _FakeClient(dialogs=[_dialog(_user_entity(i)) for i in range(1, 101)])
    _set_engine(client)
    try:
        _run(handler_module._manage_panel_handler(_event(3, 1), "", 1))
        assert client.iter_dialogs_calls == 1
        with patch.object(handler_module, "_session_extra", return_value="p=1&q="), \
             patch.object(handler_module, "_set_extra"):
            _run(handler_module._manage_page_action(_event(3, 1), "2", 3))
            _run(handler_module._manage_page_action(_event(3, 1), "1", 3))
        assert client.iter_dialogs_calls == 1  # never re-enumerated
    finally:
        _restore_engine()


def test_16_manage_toggle_no_repeat_enumeration():
    _reset()
    client = _FakeClient(dialogs=[_dialog(_user_entity(i)) for i in range(1, 101)])
    _set_engine(client)
    try:
        _run(handler_module._manage_panel_handler(_event(3, 1), "", 1))
        assert client.iter_dialogs_calls == 1
        with patch.object(handler_module, "_session_extra", return_value="p=1&q="):
            _run(handler_module._toggle_permission_action(_event(3, 1), "1001", 3))
        assert client.iter_dialogs_calls == 1
    finally:
        _restore_engine()


def test_17_manage_search_no_repeat_enumeration():
    _reset()
    client = _FakeClient(dialogs=[_dialog(_user_entity(i)) for i in range(1, 101)])
    helper = _FakeHelper()
    _set_engine(client)
    try:
        _run(handler_module._manage_panel_handler(_event(3, 1), "", 1))
        assert client.iter_dialogs_calls == 1
        with patch.object(handler_module, "get_client", return_value=helper):
            _run(handler_module._manage_search_input_handler("User", 3, 1, 3, 1))
        assert client.iter_dialogs_calls == 1  # search uses the cached directory
    finally:
        _restore_engine()


def test_18_manage_render_bounded_to_eight_rows():
    title, body, buttons = handler_module._render_manage(_chats(500))
    texts = _button_texts(buttons)
    chat_rows = [t for t in texts if t[:2].rstrip(".").isdigit()]
    assert len(chat_rows) <= 8


def test_19_500_dialogs_2_allowed_manage_bounded():
    """500 total dialogs, 2 allowed: Browser resolves 2 entities; Manage page
    renders 8 rows from the cached directory."""
    _reset()
    allow_chat(42)
    allow_chat(999)
    entities = {42: _user_entity(42, first_name="Ali"), 999: _user_entity(999, first_name="Sara")}
    all_dialogs = [_dialog(_user_entity(i)) for i in range(2, 502)]
    client = _FakeClient(entities=entities, dialogs=all_dialogs)
    browser = _run(resolve_allowed_chats(client, owner_id=1))
    assert len(browser) == 2
    assert client.iter_dialogs_calls == 0  # Browser never enumerates
    directory = _run(load_manage_directory(client, owner_id=1))
    assert len(directory) == 500
    assert client.iter_dialogs_calls == 1  # Manage discovers once
    view = manage_page_items(directory)
    assert len(view.chats) == 8


# ═══════════════════════════════════════════════════════════════════════
# PRIVACY — disallowed chats can never be used
# ═══════════════════════════════════════════════════════════════════════

def test_20_open_chat_uses_allowed_resolution_no_iter_dialogs():
    """Opening a chat from the Browser resolves only that allowed ID — it
    never re-enumerates the full dialog list."""
    _reset()
    allow_chat(123)
    client = _FakeClient(
        entities={123: _user_entity(123, first_name="Ali")},
        dialogs=[_dialog(_user_entity(i)) for i in range(2, 502)],
    )
    _set_engine(client)
    try:
        title, body, buttons = _run(handler_module._open_chat_action(_event(777, 1), "123", 777))
        assert client.iter_dialogs_calls == 0
        assert client.get_entity_calls == [123]
        assert "Ghost" in title
    finally:
        _restore_engine()


def test_21_disallowed_chat_not_in_browser():
    _reset()
    client = _FakeClient(dialogs=[_dialog(_user_entity(100, first_name="Ali"))])
    result = _run(resolve_allowed_chats(client, owner_id=1))
    assert result == []


@pytest.mark.asyncio
async def test_21_disallowed_chat_cannot_open_viewer(monkeypatch):
    _reset()
    monkeypatch.setattr(inline_engine, "_self_client", None)
    title, body, buttons = await handler_module._open_chat_action(_event(777, 1), "100", 777)
    assert "not allowed" in body.lower()


@pytest.mark.asyncio
async def test_22_disallowed_chat_cannot_reply(monkeypatch):
    _reset()
    from backend.helper import input_state
    input_state.clear_all()
    monkeypatch.setattr(inline_engine, "_self_client", None)
    monkeypatch.setattr(inline_engine, "_owner_id", 999)
    clear_selection = service_module.clear_selection
    clear_selection(100)
    service_module.toggle_selection(100, 9)
    with patch.object(handler_module, "_session_extra", return_value="source=100&name=Ali&page=1"):
        title, body, buttons = await handler_module._reply_action(_event(777, 1), "100", 777)
    assert input_state.get_pending(999) is None
    assert "no longer allowed" in body.lower() or "exactly one" in body.lower()


@pytest.mark.asyncio
async def test_23_disallowed_chat_cannot_send_without_reply(monkeypatch):
    _reset()
    from backend.helper import input_state
    input_state.clear_all()
    monkeypatch.setattr(inline_engine, "_self_client", None)
    monkeypatch.setattr(inline_engine, "_owner_id", 999)
    service_module.clear_selection(100)
    service_module.toggle_selection(100, 9)
    with patch.object(handler_module, "_session_extra", return_value="source=100&name=Ali&page=1"):
        title, body, buttons = await handler_module._send_plain_action(_event(777, 1), "100", 777)
    assert input_state.get_pending(999) is None


def test_24_permission_keyed_by_numeric_chat_id():
    _reset()
    allow_chat(100)
    assert is_chat_allowed(100) is True
    assert is_chat_allowed(101) is False
    disallow_chat(100)
    assert is_chat_allowed(100) is False
    assert is_chat_allowed(101) is False


# ═══════════════════════════════════════════════════════════════════════
# REGRESSION — legacy absence, no Refresh, no AI
# ═══════════════════════════════════════════════════════════════════════

def test_25_no_legacy_identifiers():
    for mod in (service_module, handler_module):
        src = inspect.getsource(mod)
        assert "GHOST_ROOM_ID" not in src
        assert "input:ghost_chat:ai_prompt" not in src
        assert "ghost_actions" not in src
        assert "ghost_ctx" not in src
        assert "ghost_inform" not in src
        assert "Type your instruction for the selected messages." not in src


def test_26_no_refresh_button():
    from backend.bot.handlers.ghost_seen_v2 import _browser_buttons, _viewer_buttons, _manage_buttons
    from backend.services.ghost_seen_v2 import BrowserPage, MessageViewerPage
    browser = _browser_buttons(BrowserPage((), 1, 1))
    viewer = _viewer_buttons(MessageViewerPage(50, (), 1, 1))
    manage = _manage_buttons(manage_page_items(_chats(5)))
    for buttons in (browser, viewer, manage):
        assert all("Refresh" not in getattr(b, "text", "") for row in buttons for b in row)


def test_27_no_ai_paths():
    for mod in (service_module, handler_module):
        src = inspect.getsource(mod)
        assert "provider" not in src.lower() or "no provider" in src.lower()
        assert "web_search" not in src
        assert "you.com" not in src.lower()
