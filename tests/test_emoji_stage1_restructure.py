"""Emoji panel restructure — Auto-Reaction setting entry + Stage 2 cleanup.

Pins the panel scope:

* The Emoji primary panel remains the single-button diagnostic workflow;
* The Emoji sub-panels keep their standard Back/Home/Close navigation, wired
  to the existing navigation handlers, and a missing/expired nav session
  fails safe (no crash);
* General exposes an Auto-Reaction ON/OFF setting that toggles through the
  EXISTING ``settings_service`` persistence path, re-renders the stored
  value, and never starts a reaction engine;
* Stage 2 removed the "Custom Emoji" category feature (ROADMAP §26 — the
  custom/composed category UI and its Compose/source machinery), so no such
  entry point, panel or action remains registered.

Everything runs offline: panels are driven directly and the settings service
uses its in-memory cache (no Supabase, no Telegram).
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from backend.bot.handlers import emoji, misc
from backend.helper import inline_engine
from backend.helper.panels import get_action, get_panel
from backend.services import settings_service

OWNER = 7770001


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    from backend.db import client as db_client

    before = dict(settings_service.get_all())
    db_client._fallback["emoji_library"] = []
    inline_engine.set_owner_id(OWNER)
    settings_service.load_all()
    yield
    settings_service._cache.clear()
    settings_service._cache.update(before)
    db_client._fallback["emoji_library"] = []


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class _FakeClient:
    """Minimal client so misc.register() can attach its outgoing handler."""

    def on(self, *args, **kwargs):
        def _decorator(fn):
            return fn

        return _decorator


def _register_misc():
    misc.register(client=_FakeClient(), owner_id=OWNER)


def _datas(buttons) -> list[str]:
    out = []
    for row in buttons:
        for button in row:
            raw = getattr(button, "data", button)
            out.append(raw.decode("utf-8") if isinstance(raw, bytes) else str(raw))
    return out


def _texts(buttons) -> list[str]:
    return [str(getattr(button, "text", button)) for row in buttons for button in row]


# ── 1. The Emoji primary panel: no Custom Emoji category entry point ─────────


def test_no_custom_emoji_category_entry_point_exists():
    """There is no 'Custom Emoji category' anywhere in the Emoji panel surface.

    Stage 2 removed the §26 Custom Category (custom/composed) feature, so the
    primary panel carries no such entry point and none exists elsewhere.
    """
    emoji.register(client=None, owner_id=OWNER)
    _title, _body, buttons = _run(emoji._emoji_panel_handler(None, ""))
    datas = _datas(buttons)
    texts = _texts(buttons)
    for token in datas:
        assert "custom" not in token.lower(), token
    for label in texts:
        assert "custom emoji" not in label.lower(), label


def test_primary_panel_stays_single_button_diagnostic():
    """The prior single-button acceptance test is not regressed."""
    _title, _body, buttons = _run(emoji._emoji_panel_handler(None, ""))
    assert _datas(buttons) == [f"action:{emoji._DIAGNOSTICS_ACTION}"]


def test_custom_category_feature_is_removed():
    """Stage 2 removed the Custom Category feature end to end.

    No custom-creation entry point, no compose panel, no compose/refresh
    actions, and the category service no longer exposes the composition API.
    """
    emoji.register(client=None, owner_id=OWNER)
    assert get_action("emoji_cat_new_custom") is None
    assert get_action("emoji_compose") is None
    assert get_action("emoji_compose_add") is None
    assert get_action("emoji_compose_rm") is None
    assert get_action("emoji_compose_apply") is None
    assert get_action("emoji_compose_confirm") is None
    assert get_action("emoji_refresh") is None
    assert get_panel("emoji_compose") is None
    assert get_panel("emoji_sources") is None
    assert not hasattr(emoji, "_COMPOSE_PANEL")
    assert not hasattr(emoji, "_SOURCE_PICKER_PANEL")
    from backend.services import emoji_category_service as cat_service
    for name in (
        "compose_category", "refresh_category", "plan_composition",
        "category_sources", "is_custom_category",
    ):
        assert not hasattr(cat_service, name), name
    assert not hasattr(cat_service, "E_NOT_CUSTOM")


def test_ordinary_categories_remain_accessible():
    """Ordinary category/mapping/reaction capabilities stay registered."""
    emoji.register(client=None, owner_id=OWNER)
    assert get_panel(emoji._LIBRARY_PANEL) is not None
    assert get_panel(emoji._CATEGORIES_PANEL) is not None
    assert get_panel(emoji._REPLACEMENT_PANEL) is not None
    assert get_action("emoji_cat_new") is not None
    assert get_action("emoji_import") is not None
    assert get_action("emoji_react") is not None


# ── 2. Back / Home / Close on the Emoji sub-panels ───────────────────────────


def test_emoji_sub_panel_gets_back_home_close_through_finalize():
    """A sub-panel gains the standard nav row from the shared injector."""
    from backend.helper.lifecycle import get_lifecycle
    from backend.helper.panels import _finalize_panel

    emoji.register(client=None, owner_id=OWNER)
    lifecycle = get_lifecycle()
    chat_id, msg_id = 4300, 10
    # menu -> emoji: a non-root view, so the injector adds Back/Home/Close.
    lifecycle.sessions.reset_nav(chat_id, msg_id, "menu", "")
    lifecycle.sessions.push_nav(chat_id, msg_id, emoji._CATEGORIES_PANEL, "")
    _title, _body, buttons = _finalize_panel(
        "Categories", "body", [], emoji._CATEGORIES_PANEL, chat_id, msg_id
    )
    datas = _datas(buttons)
    assert "panel:_nav:back" in datas
    assert "panel:_nav:home" in datas
    assert "panel:_nav:close" in datas


def test_general_panel_gets_back_home_close_through_finalize():
    from backend.helper.lifecycle import get_lifecycle
    from backend.helper.panels import _finalize_panel

    _register_misc()
    lifecycle = get_lifecycle()
    chat_id, msg_id = 4301, 11
    lifecycle.sessions.reset_nav(chat_id, msg_id, "menu", "")
    lifecycle.sessions.push_nav(chat_id, msg_id, "general", "")
    _title, _body, buttons = _finalize_panel(
        "General", "body", misc._build_general_buttons(), "general", chat_id, msg_id
    )
    datas = _datas(buttons)
    assert "panel:_nav:back" in datas
    assert "panel:_nav:home" in datas
    assert "panel:_nav:close" in datas


def test_back_navigation_uses_the_existing_handler(monkeypatch):
    """Back pops the existing nav stack and re-renders the parent panel."""
    from backend.helper import panels
    from backend.helper.lifecycle import get_lifecycle

    _register_misc()
    emoji.register(client=None, owner_id=OWNER)
    lifecycle = get_lifecycle()
    chat_id, msg_id = 4242, 99
    # menu -> emoji (parent chain registered in the panel registry)
    lifecycle.sessions.reset_nav(chat_id, msg_id, "menu", "")
    lifecycle.sessions.push_nav(chat_id, msg_id, "emoji", "")

    edits: list = []
    monkeypatch.setattr(
        panels, "_safe_edit", AsyncMock(side_effect=lambda *a, **k: edits.append(a) or True)
    )
    event = AsyncMock()
    _run(panels._handle_navigation(event, "back", chat_id, msg_id, OWNER))
    assert edits, "back must re-render through the existing edit path"
    # the nav stack popped back to the parent (menu)
    assert lifecycle.sessions.current_nav(chat_id, msg_id)[0] == "menu"


def test_home_navigation_resets_to_menu(monkeypatch):
    from backend.helper import panels
    from backend.helper.lifecycle import get_lifecycle

    _register_misc()
    emoji.register(client=None, owner_id=OWNER)
    lifecycle = get_lifecycle()
    chat_id, msg_id = 4243, 100
    lifecycle.sessions.reset_nav(chat_id, msg_id, "menu", "")
    lifecycle.sessions.push_nav(chat_id, msg_id, "emoji", "")

    monkeypatch.setattr(panels, "_safe_edit", AsyncMock(return_value=True))
    event = AsyncMock()
    _run(panels._handle_navigation(event, "home", chat_id, msg_id, OWNER))
    assert lifecycle.sessions.current_nav(chat_id, msg_id)[0] == "menu"


def test_close_navigation_calls_the_existing_close_path(monkeypatch):
    from backend.helper import panels
    from backend.helper.lifecycle import PanelLifecycleManager, get_lifecycle

    _register_misc()
    lifecycle = get_lifecycle()
    chat_id, msg_id = 4244, 101
    lifecycle.sessions.reset_nav(chat_id, msg_id, "menu", "")

    called = {}

    async def _fake_close_panel(self, cid, mid, event=None):
        called["close"] = (cid, mid)

    monkeypatch.setattr(PanelLifecycleManager, "close_panel", _fake_close_panel)
    event = AsyncMock()
    _run(panels._handle_navigation(event, "close", chat_id, msg_id, OWNER))
    assert called.get("close") == (chat_id, msg_id)


def test_expired_nav_state_fails_safe(monkeypatch):
    """A Back with no session must not raise; it falls back to the registry parent."""
    from backend.helper import panels
    from backend.helper.lifecycle import get_lifecycle

    _register_misc()
    emoji.register(client=None, owner_id=OWNER)
    get_lifecycle().sessions.clear_all()  # simulate expired/unknown state

    monkeypatch.setattr(panels, "_safe_edit", AsyncMock(return_value=True))
    event = AsyncMock()
    # No exception expected even with no session for this chat/msg.
    _run(panels._handle_navigation(event, "back", 99999, 88888, OWNER))


# ── 3. General → Auto-Reaction setting ───────────────────────────────────────


def test_general_shows_the_auto_reaction_setting():
    buttons = misc._build_general_buttons()
    texts = _texts(buttons)
    datas = _datas(buttons)
    assert any("Auto-Reaction" in t for t in texts)
    assert "action:general_toggle_auto_reaction" in datas


def test_general_auto_reaction_label_reflects_stored_off_by_default():
    settings_service.set_auto_reaction_enabled(False)
    texts = _texts(misc._build_general_buttons())
    label = next(t for t in texts if "Auto-Reaction" in t)
    assert "OFF" in label


def test_toggle_enables_through_settings_service():
    settings_service.set_auto_reaction_enabled(False)
    assert settings_service.is_auto_reaction_enabled() is False
    _run(misc._general_toggle_auto_reaction_action(None, "", 0))
    assert settings_service.is_auto_reaction_enabled() is True
    # the re-rendered label reflects the new stored value
    label = next(t for t in _texts(misc._build_general_buttons()) if "Auto-Reaction" in t)
    assert "ON" in label


def test_toggle_disables_again():
    settings_service.set_auto_reaction_enabled(True)
    _run(misc._general_toggle_auto_reaction_action(None, "", 0))
    assert settings_service.is_auto_reaction_enabled() is False


def test_toggle_action_reports_feedback():
    settings_service.set_auto_reaction_enabled(False)
    title, body, buttons = _run(misc._general_toggle_auto_reaction_action(None, "", 0))
    assert title == "General"
    assert "Auto-Reaction" in body
    assert "ON" in body


def test_reopening_general_reflects_stored_setting():
    """A stored True is reflected on reopen (never a hardcoded OFF)."""
    settings_service.set_auto_reaction_enabled(True)
    label = next(t for t in _texts(misc._build_general_buttons()) if "Auto-Reaction" in t)
    assert "ON" in label
    settings_service.set_auto_reaction_enabled(False)
    label = next(t for t in _texts(misc._build_general_buttons()) if "Auto-Reaction" in t)
    assert "OFF" in label


def test_toggle_uses_the_existing_settings_path(monkeypatch):
    """The toggle must go through settings_service, not a parallel store."""
    calls: list = []
    real = settings_service.set_setting

    def _spy(key, value):
        calls.append((key, value))
        return real(key, value)

    monkeypatch.setattr(settings_service, "set_setting", _spy)
    settings_service.set_auto_reaction_enabled(False)
    calls.clear()
    _run(misc._general_toggle_auto_reaction_action(None, "", 0))
    assert calls == [("auto_reaction_enabled", True)]


def test_setting_is_a_known_settings_key():
    assert "auto_reaction_enabled" in settings_service.known_keys()
    assert settings_service.is_valid_key("auto_reaction_enabled")


def test_setting_validator_rejects_non_bool():
    assert settings_service.set_setting("auto_reaction_enabled", "yes") is False


def test_action_is_registered_through_the_existing_registry():
    _register_misc()
    assert get_action("general_toggle_auto_reaction") is not None


def test_toggle_starts_no_reaction_engine(monkeypatch):
    """Flipping the setting must not call the reaction service or send anything."""
    import backend.services.reaction_service as reaction_service

    def _boom(*a, **k):
        raise AssertionError("the toggle must not start a reaction engine")

    monkeypatch.setattr(reaction_service, "react_to_message", _boom)
    settings_service.set_auto_reaction_enabled(False)
    _run(misc._general_toggle_auto_reaction_action(None, "", 0))
    assert settings_service.is_auto_reaction_enabled() is True
