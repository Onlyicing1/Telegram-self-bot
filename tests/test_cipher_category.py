"""Cipher category — one navigation home for the private-message features.

The category is pure navigation: it groups the features whose primary purpose
is managing private messages, private-chat access, or replies to private chats.
Nothing about a feature is re-implemented here; the tests drive the REAL panel
registry, the REAL panel handlers and the REAL Back/Home navigation.

Everything runs offline: no Telegram client, no helper bot, no database.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.bot.handlers import ghost_seen_v2, misc
from backend.bot.handlers import private_message as pm
from backend.helper import inline_engine
from backend.helper.panel_registry import registry as get_registry
from backend.helper.panels import _finalize_panel, get_panel
from backend.services import private_message_service

OWNER = 7770001
CHAT_ID = 6100
MSG_ID = 42


class _FakeClient:
    """Minimal client so ``misc.register()`` can attach its outgoing handler."""

    def on(self, *args, **kwargs):
        def _decorator(fn):
            return fn

        return _decorator


@pytest.fixture(autouse=True)
def _owner():
    inline_engine.set_owner_id(OWNER)


@pytest.fixture()
def registered(monkeypatch):
    # Registration runs outside a running loop here; close the preload
    # coroutines instead of leaving them un-awaited.
    def _no_task(coro, name, **kwargs):
        coro.close()

    monkeypatch.setattr(
        "backend.runtime.task_guard.guarded_create_task", _no_task
    )
    misc.register(client=_FakeClient(), owner_id=OWNER)
    pm.register(client=None, owner_id=OWNER, tz_str="Asia/Tehran")
    ghost_seen_v2.register(client=None, owner_id=OWNER, tz_str="Asia/Tehran")


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


def _panel(panel_id: str, extra: str = ""):
    handler = get_registry().get_handler(panel_id)
    assert handler is not None, f"panel '{panel_id}' is not registered"
    return _run(handler(SimpleNamespace(), extra))


# ── the main menu ────────────────────────────────────────────────────────────


def test_the_main_menu_shows_one_cipher_category(registered):
    buttons = misc._build_menu_buttons()
    assert "Cipher" in _texts(buttons)
    assert _datas(buttons).count("panel:cipher") == 1


def test_private_message_features_are_no_longer_top_level_entries(registered):
    datas = _datas(misc._build_menu_buttons())
    texts = _texts(misc._build_menu_buttons())
    assert "panel:pm" not in datas
    assert "panel:ghost_seen_v2" not in datas
    assert "Private Messages" not in texts
    assert "Ghost Seen" not in texts


def test_unrelated_top_level_entries_are_unchanged(registered):
    datas = _datas(misc._build_menu_buttons())
    for data in (
        "panel:save",
        "panel:del",
        "panel:profile",
        "panel:db",
        "panel:general",
        "panel:settings",
        "panel:todo",
        "panel:ai",
        "panel:archive",
    ):
        assert data in datas, data


def test_no_menu_or_category_callback_data_is_duplicated(registered):
    menu = _datas(misc._build_menu_buttons())
    cipher = _datas(_panel("cipher")[2])
    assert len(set(menu)) == len(menu)
    assert len(set(cipher)) == len(cipher)
    assert not set(menu) & set(cipher)


# ── the category panel ───────────────────────────────────────────────────────


def test_cipher_is_a_first_class_panel_below_the_menu(registered):
    assert get_registry().get_parent("cipher") == "menu"
    assert get_registry().get_title("cipher") == "Cipher"


def test_cipher_describes_itself_as_private_communications(registered):
    title, body, _buttons = _panel("cipher")
    assert title == "Cipher"
    assert "Private Communications" in body


def test_every_child_of_cipher_is_reachable(registered):
    _title, _body, buttons = _panel("cipher")
    datas = _datas(buttons)
    assert datas == ["panel:pm", "panel:ghost_seen_v2"]
    for data in datas:
        panel_id = data.split(":", 1)[1]
        assert get_panel(panel_id) is not None, panel_id


def test_moved_features_keep_their_own_callback_identifiers(registered):
    assert pm.PANEL_ID == "pm"
    assert ghost_seen_v2._PANEL_ID == "ghost_seen_v2"
    assert get_registry().get_parent("pm") == "cipher"
    assert get_registry().get_parent("ghost_seen_v2") == "cipher"


def test_the_moved_features_keep_their_existing_sub_panels(registered):
    assert get_registry().get_parent("pm_policy") == "pm"
    assert get_registry().get_parent("pm_allow") == "pm"
    assert get_registry().get_parent("pm_except") == "pm"
    assert get_registry().get_parent("pm_temp") == "pm"
    assert get_registry().get_parent("pm_sched") == "pm"
    assert get_registry().get_parent("pm_rule") == "pm_sched"
    assert get_registry().get_parent("ghost_seen_v2_viewer") == "ghost_seen_v2"
    assert get_registry().get_parent("ghost_seen_v2_actions") == "ghost_seen_v2_viewer"
    assert get_registry().get_parent("ghost_seen_v2_manage") == "ghost_seen_v2"


# ── Back / Home ──────────────────────────────────────────────────────────────


def _navigate(monkeypatch, action: str, chat_id: int = CHAT_ID, msg_id: int = MSG_ID):
    from backend.helper import panels

    edits: list = []
    monkeypatch.setattr(
        panels,
        "_safe_edit",
        AsyncMock(side_effect=lambda *a, **k: edits.append(a) or True),
    )
    _run(panels._handle_navigation(AsyncMock(), action, chat_id, msg_id, OWNER))
    assert edits, f"{action} must re-render through the existing edit path"
    return edits[-1]


def test_cipher_renders_back_home_and_close_below_the_menu(registered):
    from backend.helper.lifecycle import get_lifecycle

    lifecycle = get_lifecycle()
    lifecycle.sessions.reset_nav(CHAT_ID, MSG_ID, "menu", "")
    lifecycle.sessions.push_nav(CHAT_ID, MSG_ID, "cipher", "")
    _title, _body, buttons = _finalize_panel(
        "Cipher", "Private Communications", [], "cipher", CHAT_ID, MSG_ID
    )
    datas = _datas(buttons)
    assert "panel:_nav:back" in datas
    assert "panel:_nav:home" in datas
    assert "panel:_nav:close" in datas


def test_back_from_a_cipher_child_returns_to_cipher(registered, monkeypatch):
    from backend.helper.lifecycle import get_lifecycle

    lifecycle = get_lifecycle()
    lifecycle.sessions.reset_nav(CHAT_ID, MSG_ID, "menu", "")
    lifecycle.sessions.push_nav(CHAT_ID, MSG_ID, "cipher", "")
    lifecycle.sessions.push_nav(CHAT_ID, MSG_ID, "pm", "")

    container = _navigate(monkeypatch, "back")
    assert lifecycle.sessions.current_nav(CHAT_ID, MSG_ID)[0] == "cipher"
    datas = _datas(container[2])
    assert "panel:pm" in datas and "panel:ghost_seen_v2" in datas


def test_back_from_cipher_returns_to_the_main_menu(registered, monkeypatch):
    from backend.helper.lifecycle import get_lifecycle

    lifecycle = get_lifecycle()
    lifecycle.sessions.reset_nav(CHAT_ID, MSG_ID, "menu", "")
    lifecycle.sessions.push_nav(CHAT_ID, MSG_ID, "cipher", "")

    _navigate(monkeypatch, "back")
    assert lifecycle.sessions.current_nav(CHAT_ID, MSG_ID)[0] == "menu"


def test_home_from_a_cipher_child_returns_to_the_main_menu(registered, monkeypatch):
    from backend.helper.lifecycle import get_lifecycle

    lifecycle = get_lifecycle()
    lifecycle.sessions.reset_nav(CHAT_ID, MSG_ID, "menu", "")
    lifecycle.sessions.push_nav(CHAT_ID, MSG_ID, "cipher", "")
    lifecycle.sessions.push_nav(CHAT_ID, MSG_ID, "ghost_seen_v2", "")

    container = _navigate(monkeypatch, "home")
    assert lifecycle.sessions.current_nav(CHAT_ID, MSG_ID)[0] == "menu"
    assert "panel:cipher" in _datas(container[2])


def test_back_without_a_session_falls_back_to_the_registry_parent(registered, monkeypatch):
    from backend.helper.lifecycle import get_lifecycle

    get_lifecycle().sessions.clear_all()
    _navigate(monkeypatch, "back", chat_id=99999, msg_id=88888)
    assert get_registry().get_parent("pm") == "cipher"


# ── behaviour preserved ──────────────────────────────────────────────────────


def test_opening_cipher_and_its_children_never_mutates_the_pv_policy(registered):
    before = dict(private_message_service.state())
    _panel("cipher")
    _panel("pm")
    assert dict(private_message_service.state()) == before
    assert callable(pm.enforce_incoming)


def test_the_private_message_panel_is_reachable_and_unchanged(registered):
    title, body, buttons = _panel("pm")
    assert title == "Private Messages"
    assert "direct private chats** only" in body
    datas = _datas(buttons)
    assert "panel:pm_policy" in datas
    assert "panel:pm_allow" in datas


def test_the_cipher_panel_is_registered_once_for_each_child(registered):
    panel_ids = get_registry().all_ids()
    assert panel_ids.count("cipher") == 1
    assert panel_ids.count("pm") == 1
    assert panel_ids.count("ghost_seen_v2") == 1
