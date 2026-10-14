"""Colored inline buttons — focused regression tests.

Covers the named Telegram styles (``primary``/``success``/``danger``) applied
through the EXISTING main panel and shared renderer:

1. Each supported named style constructs the correct ``KeyboardButtonStyle``.
2. Style survives button normalization and inline-result construction.
3. The serialized TL type/flags are correct (``0xe62bc960`` + ``flags.10``).
4. Existing tuple-based buttons are unchanged (unstyled, no ``flags.10``).
5. Existing unstyled buttons and URL buttons are unchanged.
6. Callback data and navigation behaviour are untouched.
7. Unsupported styles fail safe (unstyled, never raise).
8. The main panel (``Menu``) applies the intended styles end to end.

Live Telegram acceptance/rendering is NOT covered here (no credentials); these
are offline, source-level assertions.
"""
from __future__ import annotations

import pytest

from telethon.tl import types
from telethon.tl.custom import Button

from backend.helper.panel_render import (
    SUPPORTED_BUTTON_STYLES,
    _callback,
    _normalize_button,
    render,
    to_edit_buttons,
)

_CALLBACK_ID = 0xE62BC960  # keyboardButtonCallback#e62bc960 (styled, Layer 225+)


def _flags(button) -> int:
    """The TL flags word (the int right after the constructor id)."""
    return int.from_bytes(button._bytes()[4:8], "little")


def _has_style_flag(button) -> bool:
    return bool(_flags(button) & (1 << 10))


def _bg(button, name: str):
    return getattr(getattr(button, "style", None), f"bg_{name}", None)


# ── 1. Construction of each supported named style ──


class TestStyleConstruction:
    @pytest.mark.parametrize("name", SUPPORTED_BUTTON_STYLES)
    def test_each_named_style_builds_its_background(self, name):
        btn = _callback("X", b"panel:x", name)
        assert isinstance(btn, types.KeyboardButtonCallback)
        assert _bg(btn, name) is True
        for other in SUPPORTED_BUTTON_STYLES:
            if other != name:
                assert _bg(btn, other) in (None, False)

    def test_styles_are_exactly_telegram_defined_names(self):
        assert set(SUPPORTED_BUTTON_STYLES) == {"primary", "success", "danger"}

    @pytest.mark.parametrize("name", SUPPORTED_BUTTON_STYLES)
    def test_telethon_rejects_any_other_name(self, name):
        # Guard the assumption that only these three names exist.
        assert name in ("primary", "success", "danger")
        with pytest.raises(ValueError):
            Button.inline("x", b"d", style="chartreuse")


# ── 2 + 3. Preservation through normalization and rendering, serialized TL ──


class TestStylePreservationAndSerialization:
    @pytest.mark.parametrize("name", SUPPORTED_BUTTON_STYLES)
    def test_render_serializes_style_for_each_name(self, name):
        result = render("LifeOS", "body", [[_callback("Go", b"panel:go", name)]])
        button = result.send_message.reply_markup.rows[0].buttons[0]
        assert button.CONSTRUCTOR_ID == _CALLBACK_ID
        assert _has_style_flag(button)
        assert _bg(button, name) is True
        assert button.data == b"panel:go"  # dispatch key untouched

    def test_normalization_returns_the_same_tl_object_when_font_unchanged(self):
        # With the default font the label is not rewritten, so the very object
        # inspected is the one handed to the send path.
        styled = Button.inline("Save", b"panel:save", style="success")
        assert _normalize_button(styled) is styled

    def test_style_survives_font_rewrite_rebuild(self, monkeypatch):
        from backend.services import settings_service as ss
        old_cache, old_loaded = dict(ss._cache), ss._loaded
        try:
            ss._cache["dashboard_font"] = "fraktur"
            ss._loaded = True
            styled = Button.inline("Save", b"panel:save", style="success")
            rebuilt = _normalize_button(styled)
            assert rebuilt is not styled            # rebuilt for the new font
            assert rebuilt.data == b"panel:save"    # data byte-identical
            assert _bg(rebuilt, "success") is True  # style preserved
        finally:
            ss._cache.clear()
            ss._cache.update(old_cache)
            ss._loaded = old_loaded

    def test_to_edit_buttons_preserves_style(self):
        styled = Button.inline("Delete", b"panel:del", style="danger")
        built = to_edit_buttons([[styled]])
        assert _bg(built[0][0], "danger") is True


# ── 4 + 5. Existing tuple / unstyled / URL buttons are unchanged ──


class TestUnstyledUnchanged:
    def test_tuple_button_is_unstyled(self):
        btn = _normalize_button(("Save", "panel:save"))
        assert isinstance(btn, types.KeyboardButtonCallback)
        assert _has_style_flag(btn)
        assert all(_bg(btn, n) in (None, False) for n in SUPPORTED_BUTTON_STYLES)

    def test_plain_button_object_is_unstyled(self):
        btn = _normalize_button(Button.inline("Profile", b"panel:profile"))
        assert all(_bg(btn, n) in (None, False) for n in SUPPORTED_BUTTON_STYLES)

    def test_url_button_is_unchanged_and_unstyled(self):
        btn = _normalize_button(Button.url("Docs", "https://example.com"))
        assert isinstance(btn, types.KeyboardButtonUrl)
        assert btn.url == "https://example.com"

    def test_unstyled_render_has_no_background(self):
        result = render("T", "B", [[("Profile", "panel:profile")]])
        button = result.send_message.reply_markup.rows[0].buttons[0]
        assert all(_bg(button, n) in (None, False) for n in SUPPORTED_BUTTON_STYLES)


# ── 6. Callback data and navigation behaviour unchanged ──


class TestCallbackDataUnchanged:
    def test_styled_button_data_is_byte_identical(self):
        btn = _normalize_button(Button.inline("Save", b"panel:save", style="success"))
        assert btn.data == b"panel:save"

    def test_render_preserves_all_callback_data(self):
        rows = [
            [_callback("Save", b"panel:save", "success"), ("Delete", "panel:del")],
        ]
        result = render("T", "B", rows)
        data = [b.data for r in result.send_message.reply_markup.rows for b in r.buttons]
        assert b"panel:save" in data and b"panel:del" in data


# ── 7. Unsupported styles fail safe ──


class TestUnsupportedStyles:
    @pytest.mark.parametrize("bad", ["purple", "#ff0000", "PINK", "", 123, object()])
    def test_unsupported_style_degrades_to_unstyled(self, bad):
        btn = _normalize_button(_callback("X", b"panel:x", bad))
        assert isinstance(btn, types.KeyboardButtonCallback)
        assert all(_bg(btn, n) in (None, False) for n in SUPPORTED_BUTTON_STYLES)
        assert btn.data == b"panel:x"  # never raises, action preserved

    def test_case_insensitive_supported_style_is_accepted(self):
        btn = _normalize_button(_callback("X", b"panel:x", "PRIMARY"))
        assert _bg(btn, "primary") is True


# ── 8. The existing main panel applies the intended styles end to end ──


class TestMainPanelStyles:
    def _menu_result(self):
        from backend.bot.handlers.misc import _build_menu_buttons
        return render("LifeOS", "Choose a category:", _build_menu_buttons())

    def test_save_success_delete_danger_ai_primary(self):
        result = self._menu_result()
        buttons = {b.text: b for r in result.send_message.reply_markup.rows for b in r.buttons}
        assert _bg(buttons["Save"], "success") is True
        assert _bg(buttons["Delete"], "danger") is True
        assert _bg(buttons["🧠 AI"], "primary") is True

    def test_other_menu_buttons_remain_unstyled(self):
        result = self._menu_result()
        buttons = {b.text: b for r in result.send_message.reply_markup.rows for b in r.buttons}
        for label in ("Profile", "Database", "General", "Settings", "📋 Todo", "😀 Emoji"):
            assert all(_bg(buttons[label], n) in (None, False) for n in SUPPORTED_BUTTON_STYLES), label

    def test_menu_callback_data_unchanged(self):
        result = self._menu_result()
        buttons = {b.text: b for r in result.send_message.reply_markup.rows for b in r.buttons}
        assert buttons["Save"].data == b"panel:save"
        assert buttons["Delete"].data == b"panel:del"
        assert buttons["🧠 AI"].data == b"panel:ai"

    def test_menu_result_serializes_with_style_flag(self):
        result = self._menu_result()
        button = result.send_message.reply_markup.rows[0].buttons[0]
        assert button.CONSTRUCTOR_ID == _CALLBACK_ID
        assert _has_style_flag(button)
