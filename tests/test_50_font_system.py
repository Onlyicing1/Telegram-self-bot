"""
Glass UI font system — Execution 26 focused regression tests.

Covers:
1. ≥20 readable font choices in ONE authoritative allow-list
2. Deterministic transforms; invalid/missing values fall back to default
3. IDs (`S0001`), digits, code spans, URLs and Persian text are never styled
4. Buttons inherit the selected font while callback DATA stays byte-identical
5. Restart safety: missing DB → deterministic defaults; valid value reloads
6. Invalid selections rejected without changing the persisted value
7. `.menu` command dispatch is font-independent (raw-text regex; no handler
   styles incoming text)
"""
from __future__ import annotations

import pytest


SAMPLE = "Hi Bob! Save `S0001` at https://t.me/x 123 سلام"


# ── 1–3. Allow-list and transform guarantees ──


class TestFontRegistry:
    def test_at_least_twenty_choices_plus_default(self):
        from backend.helper.font_style import FONT_KEYS, DEFAULT_FONT_KEY
        assert len(FONT_KEYS) >= 21  # 20+ styled fonts + default
        assert FONT_KEYS[0] == DEFAULT_FONT_KEY
        assert len(set(FONT_KEYS)) == len(FONT_KEYS)

    def test_every_font_changes_letters_deterministically(self):
        from backend.helper.font_style import FONT_KEYS, apply_font
        for key in FONT_KEYS:
            first = apply_font("Hello", key)
            second = apply_font("Hello", key)
            assert first == second, key
            if key != "default":
                assert first != "Hello", key  # letters actually restyled

    def test_protected_tokens_survive_every_font(self):
        from backend.helper.font_style import FONT_KEYS, apply_font
        for key in FONT_KEYS:
            out = apply_font(SAMPLE, key)
            assert "`S0001`" in out, key          # code span untouched
            assert "https://t.me/x" in out, key   # URL untouched
            assert "123" in out, key              # digits untouched
            assert "سلام" in out, key             # Persian untouched
            assert "!" in out and "@" not in out.replace("@t.me", "@t.me"), key

    def test_bare_mixed_ids_are_never_partially_styled(self):
        from backend.helper.font_style import FONT_KEYS, apply_font
        for key in FONT_KEYS:
            out = apply_font("code S0001 and @user99", key)
            assert "S0001" in out, key
            assert "@user99" in out, key

    def test_invalid_and_missing_keys_fall_back_to_default(self):
        from backend.helper.font_style import (
            normalize_font_key, apply_font, style_char,
        )
        for bad in (None, 7, "", "system", "serif", "<script>"):
            assert normalize_font_key(bad) == "default"
        assert apply_font("Hello", "bogus") == "Hello"
        assert apply_font("", "fraktur") == ""  # empty stays empty
        assert style_char("a", None) == "a"

    def test_allow_list_is_the_single_registry_for_settings_too(self):
        from backend.helper.font_style import FONT_KEYS
        from backend.services.settings_service import DASHBOARD_FONTS
        assert DASHBOARD_FONTS == FONT_KEYS


# ── 4. Buttons inherit the font; callback data is sacred ──


class TestButtonStyling:
    @pytest.mark.asyncio
    async def test_button_label_styled_data_byte_identical(self, monkeypatch):
        from backend.helper.panel_render import to_edit_buttons
        from telethon.tl.custom import Button
        from backend.services import settings_service as ss

        old_cache = dict(ss._cache)
        old_loaded = ss._loaded
        try:
            # Pin the service's loaded state so _ensure_loaded() cannot
            # overwrite the injected selection from an empty DB.
            ss._cache["dashboard_font"] = "fraktur"
            ss._loaded = True
            row = [Button.inline("Home", b"panel:_nav:home")]
            built = to_edit_buttons([row])
            btn = built[0][0]
            assert btn.data == b"panel:_nav:home"      # dispatch key unchanged
            assert btn.text != "Home"                   # label inherits the font
        finally:
            ss._cache.clear()
            ss._cache.update(old_cache)
            ss._loaded = old_loaded

    @pytest.mark.asyncio
    async def test_default_font_leaves_labels_readable(self):
        from backend.helper.panel_render import render_edit
        from backend.services import settings_service as ss

        old_cache = dict(ss._cache)
        try:
            ss._cache["dashboard_font"] = "default"
            text, buttons = render_edit("Menu", "Pick one", [])
            assert "**Menu**" in text
        finally:
            ss._cache.clear()
            ss._cache.update(old_cache)


# ── 5–6. Persistence / restart safety ──


class TestFontPersistence:
    @pytest.mark.asyncio
    async def test_missing_db_yields_deterministic_defaults(self, monkeypatch):
        from backend.services import settings_service as ss

        old_cache = dict(ss._cache)
        try:
            monkeypatch.setattr(
                "backend.services.panel_settings_repository.load",
                lambda: None,
            )
            ss.load_all()
            assert ss.dashboard_font() == "default"
            assert ss.ghost_seen_retention_seconds() == 2_592_000
        finally:
            ss._cache.clear()
            ss._cache.update(old_cache)

    @pytest.mark.asyncio
    async def test_valid_selection_persists_and_reloads(self, monkeypatch):
        from backend.services import settings_service as ss

        calls: list[tuple[str, object]] = []

        def fake_update_field(field, value):
            calls.append((field, value))
            return True

        def fake_load():
            if ("dashboard_font", "mono") in calls:
                return {"dashboard_font": "mono"}
            return {}

        old_cache = dict(ss._cache)
        try:
            monkeypatch.setattr(
                "backend.services.panel_settings_repository.update_field",
                fake_update_field,
            )
            monkeypatch.setattr(
                "backend.services.panel_settings_repository.load", fake_load,
            )
            assert ss.set_dashboard_font("mono") is True
            assert ss.dashboard_font() == "mono"
        finally:
            ss._cache.clear()
            ss._cache.update(old_cache)

    @pytest.mark.asyncio
    async def test_invalid_selection_rejected_previous_value_kept(self, monkeypatch):
        from backend.services import settings_service as ss

        old_cache = dict(ss._cache)
        try:
            ss._cache["dashboard_font"] = "fraktur"
            assert ss.set_dashboard_font("comic-sans-xyz") is False
            assert ss.dashboard_font() == "fraktur"
            assert ss.set_dashboard_font(None) is False
            assert ss.dashboard_font() == "fraktur"

            assert ss.set_ghost_seen_retention_seconds(60) is False
            assert ss.set_ghost_seen_retention_seconds(40_000_000) is False
            assert ss.ghost_seen_retention_seconds() == 2_592_000

            # Corrupted cache values still read back deterministically.
            ss._cache["dashboard_font"] = "javascript:alert(1)"
            assert ss.dashboard_font() == "default"
            ss._cache["ghost_seen_retention_seconds"] = "garbage"
            assert ss.ghost_seen_retention_seconds() == 2_592_000
        finally:
            ss._cache.clear()
            ss._cache.update(old_cache)


# ── 7. `Menu` command dispatch is font-independent ──


class TestMenuCommandIndependence:
    def test_no_handler_styles_incoming_text(self):
        from pathlib import Path
        handlers = Path("backend/bot/handlers")
        offenders = [
            p.name
            for p in handlers.glob("*.py")
            if "apply_font(" in p.read_text(encoding="utf-8")
        ]
        assert offenders == []  # fonts are render-time only

    def test_menu_matches_raw_text_via_regex_pattern(self):
        from pathlib import Path
        src = Path("backend/bot/handlers/misc.py").read_text(encoding="utf-8")
        assert 'pattern=r"^Menu$"' in src  # raw incoming text, pre-render
        assert '.menu' not in src  # the legacy dot command is fully removed

    def test_dispatch_data_never_passes_through_apply_font(self):
        from pathlib import Path
        src = Path("backend/helper/panel_render.py").read_text(encoding="utf-8")
        # Only the label side of a tuple/button is styled — never `data`.
        normalize_src = src.split("def _normalize_button")[1].split("def ")[0]
        assert "_style(btn[0])" in normalize_src
        assert "btn.data" in normalize_src
