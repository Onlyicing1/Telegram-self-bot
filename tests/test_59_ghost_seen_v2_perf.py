"""Ghost Seen v2 — performance regression tests.

Proves the Browser resolves only allowed chat IDs (O(allowed)), never
enumerates all Telegram dialogs via ``iter_dialogs()``, and the watcher
count comes from the allowed set without broad dialog loading.
"""
from __future__ import annotations

import asyncio
import inspect
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.bot.handlers import ghost_seen_v2 as handler_module
from backend.services import ghost_seen_v2 as service_module
from backend.services.ghost_seen_v2 import (
    PrivateChat, allow_chat, get_allowed_chats, is_chat_allowed,
    load_allowed_chats, load_private_chats, render_browser,
    reset_allowed_chats, resolve_allowed_chats,
)


def _reset():
    reset_allowed_chats()


def _run(coro):
    return asyncio.run(coro)


class _FakeClient:
    """Fake Telethon client that tracks calls."""

    def __init__(self, entities=None, dialogs=None):
        self._entities = entities or {}
        self._dialogs = dialogs or []
        self.get_entity_calls = []
        self.iter_dialogs_calls = 0

    async def get_entity(self, entity_id):
        self.get_entity_calls.append(int(entity_id))
        e = self._entities.get(int(entity_id))
        if e is None:
            raise Exception(f"Entity {entity_id} not found")
        return e

    def iter_dialogs(self):
        self.iter_dialogs_calls += 1
        return _AsyncIter(self._dialogs)


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


def _user_entity(uid, first_name="User", last_name="", username="", bot=False, is_self=False):
    return SimpleNamespace(
        id=uid, first_name=first_name, last_name=last_name,
        username=username, bot=bot, is_self=is_self, deleted=False,
        is_bot=bot,
    )


def _dialog(entity, **overrides):
    return SimpleNamespace(entity=entity, is_user=True, is_group=False, is_channel=False, **overrides)


# ═══════════════════════════════════════════════════════════════════════
# Case 1 — zero allowed chats: no iter_dialogs, no get_entity
# ═══════════════════════════════════════════════════════════════════════

def test_1_zero_allowed_no_rpc_calls():
    _reset()
    client = _FakeClient()
    result = _run(resolve_allowed_chats(client, owner_id=1))
    assert result == []
    assert client.get_entity_calls == []
    assert client.iter_dialogs_calls == 0


# ═══════════════════════════════════════════════════════════════════════
# Case 2 — one allowed chat: resolve only that ID
# ═══════════════════════════════════════════════════════════════════════

def test_2_one_allowed_resolves_only_that_id():
    _reset()
    entity = _user_entity(123, first_name="Ali")
    allow_chat(123)
    client = _FakeClient(entities={123: entity})
    result = _run(resolve_allowed_chats(client, owner_id=1))
    assert len(result) == 1
    assert result[0].chat_id == 123
    assert result[0].display_name == "Ali"
    assert client.get_entity_calls == [123]
    assert client.iter_dialogs_calls == 0


# ═══════════════════════════════════════════════════════════════════════
# Case 3 — several allowed chats: only those are resolved
# ═══════════════════════════════════════════════════════════════════════

def test_3_several_allowed_resolve_only_those_ids():
    _reset()
    allow_chat(100)
    allow_chat(200)
    allow_chat(300)
    entities = {
        100: _user_entity(100, first_name="Ali"),
        200: _user_entity(200, first_name="Sara"),
        300: _user_entity(300, first_name="Reza"),
    }
    client = _FakeClient(entities=entities)
    result = _run(resolve_allowed_chats(client, owner_id=1))
    assert len(result) == 3
    ids = {r.chat_id for r in result}
    assert ids == {100, 200, 300}
    assert client.get_entity_calls == [100, 200, 300]
    assert client.iter_dialogs_calls == 0


# ═══════════════════════════════════════════════════════════════════════
# Case 4 — 500 dialogs + 2 allowed: bounded by allowed set
# ═══════════════════════════════════════════════════════════════════════

def test_4_500_dialogs_2_allowed_stays_bounded():
    _reset()
    allow_chat(42)
    allow_chat(999)
    entities = {
        42: _user_entity(42, first_name="Ali"),
        999: _user_entity(999, first_name="Sara"),
    }
    # 500 total dialogs, but Browser should NOT enumerate them
    all_dialogs = [_dialog(_user_entity(i)) for i in range(1, 501)]
    client = _FakeClient(entities=entities, dialogs=all_dialogs)
    result = _run(resolve_allowed_chats(client, owner_id=1))
    assert len(result) == 2
    assert client.get_entity_calls == [42, 999]
    assert client.iter_dialogs_calls == 0


# ═══════════════════════════════════════════════════════════════════════
# Watcher count from allowed set, not dialog enumeration
# ═══════════════════════════════════════════════════════════════════════

def test_5_watcher_count_from_allowed_set():
    _reset()
    allow_chat(10)
    allow_chat(20)
    assert len(get_allowed_chats()) == 2
    # Browser uses len(get_allowed_chats()) — no Telegram calls needed
    assert get_allowed_chats() == frozenset({10, 20})


# ═══════════════════════════════════════════════════════════════════════
# Bot entity is excluded even if allowed
# ═══════════════════════════════════════════════════════════════════════

def test_6_bot_entity_excluded():
    _reset()
    allow_chat(50)
    entities = {50: _user_entity(50, first_name="Bot", bot=True)}
    client = _FakeClient(entities=entities)
    result = _run(resolve_allowed_chats(client, owner_id=1))
    assert result == []


# ═══════════════════════════════════════════════════════════════════════
# Self entity is excluded
# ═══════════════════════════════════════════════════════════════════════

def test_7_self_entity_excluded():
    _reset()
    allow_chat(1)
    entities = {1: _user_entity(1, first_name="Me", is_self=True)}
    client = _FakeClient(entities=entities)
    result = _run(resolve_allowed_chats(client, owner_id=1))
    assert result == []


# ═══════════════════════════════════════════════════════════════════════
# Failed entity resolution gracefully skipped
# ═══════════════════════════════════════════════════════════════════════

def test_8_failed_entity_resolution_skipped():
    _reset()
    allow_chat(999)
    # No entity for 999 → exception → skipped
    client = _FakeClient(entities={})
    result = _run(resolve_allowed_chats(client, owner_id=1))
    assert result == []
    assert client.get_entity_calls == [999]


# ═══════════════════════════════════════════════════════════════════════
# None client returns empty immediately
# ═══════════════════════════════════════════════════════════════════════

def test_9_none_client_returns_empty():
    _reset()
    allow_chat(100)
    result = _run(resolve_allowed_chats(None, owner_id=1))
    assert result == []


# ═══════════════════════════════════════════════════════════════════════
# load_allowed_chats still works (Manage path)
# ═══════════════════════════════════════════════════════════════════════

def test_10_load_allowed_still_uses_iter_dialogs():
    _reset()
    allow_chat(10)
    dialog = _dialog(_user_entity(10, first_name="Ali"))
    client = _FakeClient(dialogs=[dialog])
    result = _run(load_allowed_chats(client, owner_id=1))
    assert len(result) == 1
    assert client.iter_dialogs_calls == 1


# ═══════════════════════════════════════════════════════════════════════
# Search works against resolved allowed chats
# ═══════════════════════════════════════════════════════════════════════

def test_11_search_works_against_resolved_chats():
    chats = [
        PrivateChat(1, first_name="Ali", last_name="Reza"),
        PrivateChat(2, first_name="Sara", username="sara99"),
    ]
    body, view = render_browser(chats, page=1, query="Ali", watcher_count=2)
    assert len(view.chats) == 1
    assert view.chats[0].chat_id == 1


# ═══════════════════════════════════════════════════════════════════════
# Browser render text never mentions iter_dialogs
# ═══════════════════════════════════════════════════════════════════════

def test_12_iter_dialogs_not_in_resolve_allowed_src():
    src = inspect.getsource(resolve_allowed_chats)
    # The function must never call iter_dialogs() or load_private_chats().
    # Check for actual call patterns (not docstring mentions).
    assert "await client.iter_dialogs" not in src
    assert "await load_private_chats" not in src


# ═══════════════════════════════════════════════════════════════════════
# No Refresh button in browser
# ═══════════════════════════════════════════════════════════════════════

def test_13_no_refresh_button():
    from backend.bot.handlers.ghost_seen_v2 import _browser_buttons
    view = PrivateChat(1, first_name="Ali")
    buttons = _browser_buttons(render_browser([view])[1])
    assert all("Refresh" not in getattr(b, "text", "") for row in buttons for b in row)


# ═══════════════════════════════════════════════════════════════════════
# Disallowing chat removes it from resolution
# ═══════════════════════════════════════════════════════════════════════

def test_14_disallow_removes_from_resolution():
    _reset()
    allow_chat(50)
    assert is_chat_allowed(50)
    from backend.services.ghost_seen_v2 import disallow_chat
    disallow_chat(50)
    assert not is_chat_allowed(50)
    assert 50 not in get_allowed_chats()


# ═══════════════════════════════════════════════════════════════════════
# No legacy Ghost Seen identifiers
# ═══════════════════════════════════════════════════════════════════════

def test_15_no_legacy_identifiers():
    for mod in (service_module, handler_module):
        src = inspect.getsource(mod)
        assert "GHOST_ROOM_ID" not in src
        assert "input:ghost_chat:ai_prompt" not in src
        assert "ghost_actions" not in src
        assert "ghost_ctx" not in src
        assert "ghost_inform" not in src
        assert "Type your instruction for the selected messages." not in src
