"""
Display-font registry expansion — focused tests for the styles added to the
canonical registry (``backend/helper/font_style.py``).

Covers:
1. Registry integrity: default first, keys unique, every style a genuinely
   distinct transform (no aliases, no near-duplicates).
2. The four added combining-mark styles: exact expected Unicode output for
   uppercase, lowercase, digits and mixed text.
3. Preservation: punctuation, whitespace, emoji, Persian and other
   unsupported characters, code spans, URLs and alphanumeric IDs.
4. Regression: every pre-existing style's output is byte-identical to the
   registry as it stood before the expansion, and its position is unchanged.
5. Selection and persistence: a new key persists, survives a reload, is
   exposed by the Font panel and is selectable through the existing action.
6. Frontend/backend consistency: every key the dashboard offers is a key of
   the authoritative allow-list.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest


#: The four added styles: key → (label, combining mark applied per character).
NEW_STYLES: dict[str, tuple[str, str]] = {
    "double_underline": ("Double Underline", "\u0333"),
    "slash_through": ("Slashed", "\u0338"),
    "tilde_overlay": ("Tilde Overlay", "\u0334"),
    "dot_above": ("Dotted", "\u0307"),
}

#: The registry as it stood before the expansion — exact expected output per
#: style, so a later edit to any of them is a visible failure, not a silent one.
PRE_EXPANSION_STYLES: tuple[tuple[str, str, str], ...] = (
    ("default", "Hello", "Hello"),
    ("serif_bold", "Az", "\U0001D400\U0001D433"),
    ("serif_italic", "Hh", "\U0001D43B\u210E"),
    ("serif_bold_italic", "Az", "\U0001D468\U0001D49B"),
    ("sans", "Az", "\U0001D5A0\U0001D5D3"),
    ("sans_bold", "Az", "\U0001D5D4\U0001D607"),
    ("sans_italic", "Az", "\U0001D608\U0001D63B"),
    ("sans_bold_italic", "Az", "\U0001D63C\U0001D66F"),
    ("script", "Be", "\u212C\u212F"),
    ("script_bold", "Az", "\U0001D4D0\U0001D503"),
    ("fraktur", "Ca", "\u212D\U0001D51E"),
    ("fraktur_bold", "Az", "\U0001D56C\U0001D59F"),
    ("double_struck", "Cx", "\u2102\U0001D569"),
    ("mono", "A 0", "\U0001D670 \U0001D7F6"),
    ("small_caps", "Ab", "\u1D00\u0299"),
    ("circled", "A 1", "\u24B6 \u2460"),
    ("circled_dark", "A 1", "\U0001F150 \u2776"),
    ("fullwidth", "A 1", "\uFF21 \uFF11"),
    ("parenthesized", "a", "\u249C"),
    ("underline", "A", "A\u0332"),
    ("strikethrough", "A", "A\u0336"),
    ("overline", "A", "A\u0305"),
    ("wavy_underline", "A", "A\u0330"),
)

PRE_EXPANSION_KEYS: tuple[str, ...] = tuple(k for k, _t, _e in PRE_EXPANSION_STYLES)

SAMPLE = "Hi Bob! Save `S0001` at https://t.me/x 123 سلام 😀"


@pytest.fixture(autouse=True)
def _fresh_settings():
    """Deterministic in-memory settings state (no DB): defaults loaded,
    exactly as startup leaves them after ``settings_service.load_all()``."""
    from unittest.mock import patch

    import backend.services.settings_service as svc
    from backend.services import panel_settings_repository as repo

    def _reset():
        svc._loaded = False
        svc._cache = {}

    _reset()
    with patch.object(repo, "load", return_value=None):
        svc.load_all()
    yield
    _reset()


# ── 1. Registry integrity ──


class TestRegistryIntegrity:
    def test_default_is_first_and_every_key_is_unique(self):
        from backend.helper.font_style import DEFAULT_FONT_KEY, FONT_KEYS

        assert FONT_KEYS[0] == DEFAULT_FONT_KEY == "default"
        assert len(set(FONT_KEYS)) == len(FONT_KEYS)

    def test_expansion_added_exactly_the_new_styles(self):
        from backend.helper.font_style import FONT_KEYS

        assert len(FONT_KEYS) == len(PRE_EXPANSION_KEYS) + len(NEW_STYLES)
        # The pre-existing keys keep their exact positions: a persisted key
        # can never be silently re-pointed at a different style.
        assert FONT_KEYS[: len(PRE_EXPANSION_KEYS)] == PRE_EXPANSION_KEYS
        assert set(FONT_KEYS) - set(PRE_EXPANSION_KEYS) == set(NEW_STYLES)

    def test_new_styles_are_not_aliases_of_any_other_style(self):
        from backend.helper.font_style import FONT_KEYS, apply_font

        rendered = {key: apply_font("Hello Abc 123", key) for key in FONT_KEYS}
        for key in NEW_STYLES:
            collisions = [
                other for other, out in rendered.items()
                if other != key and out == rendered[key]
            ]
            assert collisions == [], f"{key} duplicates {collisions}"
            assert rendered[key] != "Hello Abc 123"  # actually transforms

    def test_new_labels_are_distinct_and_descriptive(self):
        from backend.helper.font_style import FONT_KEYS, font_option_label

        labels = {}
        for key in FONT_KEYS:
            label = font_option_label(key).split(" · ")[0]
            assert label, key
            labels[key] = label
        assert len(set(labels.values())) == len(labels)
        for key, (label, _mark) in NEW_STYLES.items():
            assert labels[key] == label

    def test_new_styles_report_digit_glyphs(self):
        from backend.helper.font_style import font_has_digit_glyphs

        for key in NEW_STYLES:
            assert font_has_digit_glyphs(key) is True, key


# ── 2. Exact conversion per new style ──


class TestAddedStyleConversions:
    @pytest.mark.parametrize("key", sorted(NEW_STYLES))
    def test_case_preserving_latin_exact(self, key):
        from backend.helper.font_style import apply_font, style_char

        mark = NEW_STYLES[key][1]
        assert apply_font("Ab", key) == f"A{mark}b{mark}"
        assert apply_font("AB cd", key) == f"A{mark}B{mark} c{mark}d{mark}"
        assert style_char("A", key) == f"A{mark}"
        assert style_char("a", key) == f"a{mark}"
        assert style_char("Z", key) == f"Z{mark}"

    @pytest.mark.parametrize("key", sorted(NEW_STYLES))
    def test_digits_exact(self, key):
        from backend.helper.font_style import apply_font, style_char

        mark = NEW_STYLES[key][1]
        assert apply_font("12", key) == f"1{mark}2{mark}"
        assert apply_font("12:34", key) == f"1{mark}2{mark}:3{mark}4{mark}"
        assert style_char("7", key) == f"7{mark}"

    @pytest.mark.parametrize("key", sorted(NEW_STYLES))
    def test_punctuation_whitespace_and_emoji_are_preserved(self, key):
        from backend.helper.font_style import apply_font

        mark = NEW_STYLES[key][1]
        assert apply_font("😀 wow! (hi)", key) == (
            f"😀 w{mark}o{mark}w{mark}! (h{mark}i{mark})"
        )
        assert apply_font("  \n\t", key) == "  \n\t"
        assert apply_font("...--==++", key) == "...--==++"

    @pytest.mark.parametrize("key", sorted(NEW_STYLES))
    def test_persian_is_never_styled_and_english_still_is(self, key):
        from backend.helper.font_style import apply_font

        mark = NEW_STYLES[key][1]
        assert apply_font("سلام", key) == "سلام"
        # Persian digits are not ASCII digits, so they pass through intact.
        assert apply_font("سلام ۱۲۳ ABC", key) == f"سلام ۱۲۳ A{mark}B{mark}C{mark}"

    @pytest.mark.parametrize("key", sorted(NEW_STYLES))
    def test_protected_tokens_survive(self, key):
        from backend.helper.font_style import apply_font

        mark = NEW_STYLES[key][1]
        out = apply_font(SAMPLE, key)
        assert "`S0001`" in out                      # code span untouched
        assert "https://t.me/x" in out               # URL untouched
        assert "سلام" in out                         # Persian untouched
        assert "😀" in out
        assert f"1{mark}2{mark}3{mark}" in out       # standalone digits styled
        out_ids = apply_font("code S0001 and @user99", key)
        assert "S0001" in out_ids and "@user99" in out_ids


# ── 3. Fallback behaviour is unchanged ──


class TestFallback:
    def test_invalid_keys_still_fall_back_to_default(self):
        from backend.helper.font_style import (
            apply_font, normalize_font_key, style_char,
        )

        for bad in (None, 7, "", "bogus", "double_underline!", "<script>"):
            assert normalize_font_key(bad) == "default"
        assert apply_font("Hello", "bogus") == "Hello"
        assert style_char("a", "bogus") == "a"

    def test_default_style_is_a_no_op(self):
        from backend.helper.font_style import apply_font

        assert apply_font(SAMPLE, "default") == SAMPLE


# ── 4. Existing styles are byte-identical ──


class TestExistingStylesUnchanged:
    @pytest.mark.parametrize("key,text,expected", PRE_EXPANSION_STYLES)
    def test_expected_output_per_existing_style(self, key, text, expected):
        from backend.helper.font_style import apply_font

        assert apply_font(text, key) == expected

    def test_persian_and_protected_tokens_survive_every_style(self):
        from backend.helper.font_style import FONT_KEYS, apply_font

        for key in FONT_KEYS:
            out = apply_font(SAMPLE, key)
            assert "`S0001`" in out, key
            assert "https://t.me/x" in out, key
            assert "سلام" in out, key
            assert "😀" in out, key


# ── 5. Selection and persistence ──


class TestSelectionAndPersistence:
    @pytest.mark.parametrize("key", sorted(NEW_STYLES))
    def test_new_key_persists_and_reloads(self, key, monkeypatch):
        from unittest.mock import patch

        import backend.services.settings_service as svc
        from backend.services import panel_settings_repository as repo

        row: dict = {"key": "global", "dashboard_font": "default"}
        written: dict = {}

        def fake_update_field(field, value):
            written[field] = value
            row[field] = value
            return True

        with patch.object(repo, "update_field", side_effect=fake_update_field), \
             patch.object(repo, "load", return_value=row):
            assert svc.set_dashboard_font(key) is True
            assert written["dashboard_font"] == key
            assert svc.dashboard_font() == key

            svc._loaded = False
            svc._cache = {}
            svc.load_all()

        assert svc.dashboard_font() == key
        assert svc.get_all()["dashboard_font"] == key

    @pytest.mark.asyncio
    @pytest.mark.parametrize("key", sorted(NEW_STYLES))
    async def test_new_key_is_selectable_through_the_font_action(self, key):
        from backend.bot.handlers.misc import _font_set_action
        from backend.services import settings_service

        title, body, _buttons = await _font_set_action(None, key, 0)
        assert title == "Font"
        assert NEW_STYLES[key][0] in body
        assert settings_service.dashboard_font() == key

    @pytest.mark.asyncio
    async def test_font_panel_exposes_every_new_key(self):
        from backend.bot.handlers import misc
        from backend.bot.handlers.misc import _font_panel_page
        from backend.helper.font_style import FONT_KEYS

        collected: list[str] = []
        total_pages = max(
            1, (len(FONT_KEYS) + misc._FONT_PAGE_SIZE - 1) // misc._FONT_PAGE_SIZE
        )
        for page in range(total_pages):
            misc._font_page = page
            _title, _body, buttons = _font_panel_page("default")
            collected += [
                b.data.decode().rsplit("action:font_set:", 1)[1]
                for row in buttons for b in row
                if b.data.startswith(b"action:font_set:")
            ]
        misc._font_page = 0
        assert set(NEW_STYLES) <= set(collected)
        assert sorted(collected) == sorted(FONT_KEYS)

    def test_new_key_is_on_every_derived_allow_list(self):
        """One registry, every consumer: settings, the AI send tool's enum,
        the task wizard's validator and preview all follow ``FONT_KEYS``."""
        from backend.ai.task_wizard import font_preview, font_label, valid_font_key
        from backend.ai.tools.context import ToolContext
        from backend.ai.tools.registry import create_default_registry
        from backend.helper.font_style import FONT_KEYS
        from backend.services.settings_service import DASHBOARD_FONTS

        assert DASHBOARD_FONTS == FONT_KEYS
        registry = create_default_registry(
            ToolContext(telegram=None, owner_id=1, tz_str="UTC")
        )
        enum = registry.get("send_message").parameters["font"]["enum"]
        assert set(enum) == set(FONT_KEYS)
        for key in NEW_STYLES:
            assert valid_font_key(key) is True, key
            assert font_preview(key) != "Abc 123", key
            assert font_label(key) == key


# ── 6. Frontend / backend allow-list consistency ──


def _dashboard_option_keys() -> list[str]:
    source = Path("src/App.tsx").read_text(encoding="utf-8")
    block = source.split("const DASHBOARD_FONT_OPTIONS", 1)[1].split("];", 1)[0]
    return re.findall(r"key: '([^']+)'", block)


class TestFrontendBackendConsistency:
    def test_every_dashboard_option_is_a_registry_key(self):
        from backend.helper.font_style import FONT_KEYS

        keys = _dashboard_option_keys()
        assert keys, "no dashboard font options parsed"
        assert len(set(keys)) == len(keys)
        off_registry = [k for k in keys if k not in FONT_KEYS]
        assert off_registry == [], f"dashboard offers off-registry keys: {off_registry}"

    def test_dashboard_does_not_offer_the_rejected_pseudo_keys(self):
        from backend.services.settings_service import set_setting

        keys = _dashboard_option_keys()
        for pseudo in ("system", "serif"):
            assert pseudo not in keys
            assert set_setting("dashboard_font", pseudo) is False

    def test_dashboard_default_option_is_the_registry_default(self):
        from backend.helper.font_style import DEFAULT_FONT_KEY

        assert _dashboard_option_keys()[0] == DEFAULT_FONT_KEY
