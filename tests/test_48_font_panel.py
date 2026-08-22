"""
Glass UI Font panel (Settings → 🔤 Font).

Covers:
1. Default selection is deterministic.
2. A valid selection persists through settings_service and re-renders marked.
3. An invalid selection is rejected and keeps the previous font.
4. Pagination stays within bounds.
5. Exactly one font panel/action registration exists in misc.
6. Callback data never carries free-text fonts (allow-list keys only).
"""
from __future__ import annotations

import inspect

import pytest


def _reset_settings():
    import backend.services.settings_service as svc

    svc._loaded = False
    svc._cache = {}
    with __import__("unittest.mock", fromlist=["patch"]).patch(
        "backend.services.panel_settings_repository.load", return_value=None
    ):
        svc.load_all()


@pytest.fixture(autouse=True)
def _fresh_state():
    _reset_settings()
    from backend.bot.handlers import misc

    misc._font_page = 0
    yield
    _reset_settings()
    misc._font_page = 0


class TestFontPanelRendering:
    @pytest.mark.asyncio
    async def test_default_selection_is_deterministic(self):
        from backend.bot.handlers.misc import _font_panel_handler

        result = await _font_panel_handler(None, "")
        assert result is not None
        title, body, buttons = result
        assert title == "Font"
        assert "Default (system)" in body
        # The default row is the one marked as current.
        datas = [b.data for row in buttons for b in row]
        assert any(d == b"action:font_set:default" for d in datas)

    @pytest.mark.asyncio
    async def test_current_mark_follows_persisted_font(self):
        from backend.services import settings_service
        from backend.bot.handlers.misc import _font_panel_handler

        assert settings_service.set_dashboard_font("script") is True
        _title, body, _buttons = await _font_panel_handler(None, "")
        assert "Script" in body


class TestFontSetAction:
    @pytest.mark.asyncio
    async def test_valid_selection_persists(self):
        from backend.services import settings_service
        from backend.bot.handlers.misc import _font_set_action

        title, body, _buttons = await _font_set_action(None, "mono", 0)
        assert title == "Font"
        assert settings_service.dashboard_font() == "mono"
        assert "Monospace" in body

    @pytest.mark.asyncio
    async def test_invalid_selection_keeps_previous(self):
        from backend.services import settings_service
        from backend.bot.handlers.misc import _font_set_action

        assert settings_service.set_dashboard_font("mono") is True
        title, body, _buttons = await _font_set_action(None, "comic-sans !important", 0)
        assert title == "Font"
        assert "Invalid selection" in body
        assert settings_service.dashboard_font() == "mono"

    @pytest.mark.asyncio
    async def test_unknown_key_falls_back_to_default_render(self):
        from backend.services import settings_service
        from backend.helper.font_style import DEFAULT_FONT_KEY

        import backend.services.settings_service as svc

        svc._cache["dashboard_font"] = "not-a-real-key"
        from backend.bot.handlers.misc import _font_panel_handler

        _title, body, _buttons = await _font_panel_handler(None, "")
        assert "Default (system)" in body
        # An invalid persisted value reads back as the deterministic default.
        assert settings_service.dashboard_font() == "default"
        assert DEFAULT_FONT_KEY == "default"


class TestFontPagination:
    @pytest.mark.asyncio
    async def test_next_then_prev_stays_in_bounds(self):
        from backend.helper.font_style import FONT_KEYS
        from backend.bot.handlers.misc import _font_page_action, _FONT_PAGE_SIZE

        total_pages = max(
            1, (len(FONT_KEYS) + _FONT_PAGE_SIZE - 1) // _FONT_PAGE_SIZE
        )
        for _ in range(total_pages + 5):
            await _font_page_action(None, "next", 0)
        from backend.bot.handlers import misc

        assert misc._font_page == total_pages - 1
        for _ in range(total_pages + 5):
            await _font_page_action(None, "prev", 0)
        assert misc._font_page == 0


class TestRegistrationIntegrity:
    def test_single_font_panel_registration(self):
        from backend.bot.handlers import misc

        src = inspect.getsource(misc._register_panels)
        assert src.count('register_panel("font"') == 1
        assert src.count('register_action("font_set"') == 1
        assert src.count('register_action("font_page"') == 1

    def test_callback_data_carries_allow_list_keys_only(self):
        """Buttons embed font keys, never arbitrary strings."""
        from backend.bot.handlers import misc
        from backend.helper.font_style import FONT_KEYS

        src = inspect.getsource(misc._font_panel_page)
        assert 'f"action:font_set:{key}"' in src
        # Every embedded key must come from the allow-list iteration.
        assert "for key in pages[page]" in src
        assert all(isinstance(k, str) for k in FONT_KEYS)
