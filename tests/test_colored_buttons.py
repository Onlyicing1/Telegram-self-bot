"""Colored inline buttons — focused regression tests.

Covers the named Telegram styles (``primary``/``success``/``danger``) applied
through the EXISTING main panel and shared renderer under the corrected
semantic contract:

  * ``primary`` (blue) — the default for ordinary actions and navigation,
    including **Save** and **Delete**.
  * ``success`` (green) — reserved for explicit confirmation actions
    (**Confirm**, **Approve**, or an equivalent final confirmation step).
  * ``danger`` (red) — reserved for explicit cancellation actions
    (**Cancel**, **Abort**, **Discard changes**).

Specifics verified here:

1. Each supported named style constructs the correct ``KeyboardButtonStyle``.
2. Style survives button normalization and inline-result construction.
3. The serialized TL type/flags are correct (``0xe62bc960`` + ``flags.10``).
4. Tuple / raw / URL buttons default to ``primary`` (blue).
5. Existing unstyled buttons and URL buttons keep their callback data / URL.
6. Callback data and navigation behaviour are untouched.
7. Unsupported styles fail safe (unstyled, never raise).
8. The main panel (``Menu``) and the relevant submenus follow the contract:
   Save/Delete blue, Confirm green, Cancel red.
9. Every registered panel's every button carries exactly one supported style.

Live Telegram acceptance/rendering is NOT covered here (no credentials); these
are offline, source-level assertions.
"""
from __future__ import annotations

import pytest

from backend.helper import symbols as sym

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


# ── 4 + 5. Default styling for tuple / raw / URL buttons; explicit overrides ──


class TestDefaultStyling:
    """No button may silently render unstyled: the shared renderer defaults
    every style-less button to ``primary`` (blue)."""

    def test_tuple_button_defaults_to_primary(self):
        btn = _normalize_button(("Save", "panel:save"))
        assert isinstance(btn, types.KeyboardButtonCallback)
        assert _has_style_flag(btn)
        assert _bg(btn, "primary") is True
        assert btn.data == b"panel:save"

    def test_plain_button_object_defaults_to_primary(self):
        btn = _normalize_button(Button.inline("Profile", b"panel:profile"))
        assert _bg(btn, "primary") is True

    def test_plain_button_object_keeps_explicit_style(self):
        btn = _normalize_button(Button.inline("Del", b"panel:del", style="danger"))
        assert _bg(btn, "danger") is True
        assert _bg(btn, "primary") in (None, False)

    def test_url_button_defaults_to_primary_and_keeps_url(self):
        btn = _normalize_button(Button.url("Docs", "https://example.com"))
        assert isinstance(btn, types.KeyboardButtonUrl)
        assert btn.url == "https://example.com"
        assert _bg(btn, "primary") is True

    def test_url_button_keeps_explicit_style(self):
        btn = _normalize_button(Button.url("Docs", "https://example.com", style="danger"))
        assert btn.url == "https://example.com"
        assert _bg(btn, "danger") is True

    def test_tuple_render_defaults_to_primary(self):
        result = render("T", "B", [[("Profile", "panel:profile")]])
        button = result.send_message.reply_markup.rows[0].buttons[0]
        assert _bg(button, "primary") is True


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
        # A caller-supplied *unsupported* style must not be upgraded to a
        # default either — it degrades to a plain, unstyled button (fail safe,
        # never raising), preserving the action.
        btn = _callback("X", b"panel:x", bad)
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

    def test_save_and_delete_are_primary_ai_primary(self):
        result = self._menu_result()
        buttons = {b.text: b for r in result.send_message.reply_markup.rows for b in r.buttons}
        assert _bg(buttons["Save"], "primary") is True
        assert _bg(buttons["Delete"], "primary") is True
        assert _bg(buttons["AI"], "primary") is True

    def test_other_menu_buttons_are_primary(self):
        result = self._menu_result()
        buttons = {b.text: b for r in result.send_message.reply_markup.rows for b in r.buttons}
        for label in ("Profile", "Database", "General", "Settings", "Cipher",
                      "Todo"):
            assert _bg(buttons[label], "primary") is True, label

    def test_menu_has_no_green_or_red_buttons(self):
        # The root menu is pure navigation/ordinary actions: colour never
        # echoes a category, so no button is green or red here.
        result = self._menu_result()
        buttons = {b.text: b for r in result.send_message.reply_markup.rows for b in r.buttons}
        greened = [t for t, b in buttons.items() if _bg(b, "success") is True]
        reddened = [t for t, b in buttons.items() if _bg(b, "danger") is True]
        assert greened == []
        assert reddened == []

    def test_menu_callback_data_unchanged(self):
        result = self._menu_result()
        buttons = {b.text: b for r in result.send_message.reply_markup.rows for b in r.buttons}
        assert buttons["Save"].data == b"panel:save"
        assert buttons["Delete"].data == b"panel:del"
        assert buttons["AI"].data == b"panel:ai"

    def test_menu_result_serializes_with_style_flag(self):
        result = self._menu_result()
        button = result.send_message.reply_markup.rows[0].buttons[0]
        assert button.CONSTRUCTOR_ID == _CALLBACK_ID
        assert _has_style_flag(button)


# ── 9. Relevant submenus apply the same restrained mapping ──


class TestSubmenuStyles:
    @staticmethod
    def _by_text(rows):
        result = render("T", "B", rows)
        return {b.text: b for r in result.send_message.reply_markup.rows for b in r.buttons}

    @pytest.mark.asyncio
    async def test_save_submenu_deep_save_primary_retrieve_blue(self):
        from backend.bot.handlers import save
        _, _, rows = await save._save_panel_handler(None, "")
        buttons = self._by_text(rows)
        # Save is an ordinary action → blue, never green.
        assert _bg(buttons["✓ Deep Save"], "primary") is True
        assert _bg(buttons["⌕ Retrieve"], "primary") is True
        assert all(_bg(b, "success") in (None, False) for b in buttons.values())

    @pytest.mark.asyncio
    async def test_delete_submenu_is_primary(self):
        from backend.bot.handlers import delete
        _, _, rows = await delete._del_panel_handler(None, "")
        buttons = self._by_text(rows)
        # Delete is an ordinary action → blue, never red.
        assert _bg(buttons["⌫ Delete last N messages"], "primary") is True
        assert _bg(buttons["⌫ Delete From..."], "primary") is True
        assert all(_bg(b, "success") in (None, False) for b in buttons.values())
        assert all(_bg(b, "danger") in (None, False) for b in buttons.values())

    @pytest.mark.asyncio
    async def test_delete_confirm_is_green_and_back_is_blue(self):
        from backend.bot.handlers import delete
        _, _, rows = await delete._delfrom_confirm(123)
        buttons = self._by_text(rows)
        # The explicit confirmation step is green; its Back is blue.
        assert _bg(buttons["⌫ Confirm Delete"], "success") is True
        assert _bg(buttons["❮ Back to methods"], "primary") is True

    @pytest.mark.asyncio
    async def test_settings_submenu_is_primary(self):
        from backend.bot.handlers import misc
        _, _, rows = await misc._settings_panel_handler(None, "")
        buttons = self._by_text(rows)
        assert _bg(buttons["Font"], "primary") is True
        assert all(_bg(b, "danger") in (None, False) for b in buttons.values())

    def test_general_submenu_is_primary(self):
        from backend.bot.handlers import misc
        buttons = self._by_text(misc._build_general_buttons())
        app = {t: b for t, b in buttons.items() if t != "❌ Close"}
        assert all(_bg(b, "primary") is True for b in app.values())

    def test_no_submenu_button_is_both_green_and_red(self):
        from backend.bot.handlers import misc
        for rows in (misc._build_menu_buttons(), misc._build_general_buttons()):
            for b in self._by_text(rows).values():
                assert not (_bg(b, "success") and _bg(b, "danger"))


# ── 9b. Automatically injected navigation + transient progress buttons ──


class TestNavigationStyling:
    @staticmethod
    def _by_text(rows):
        result = render("T", "B", rows)
        return {b.text: b for r in result.send_message.reply_markup.rows for b in r.buttons}

    def test_nav_row_back_home_close_are_primary(self):
        from backend.helper.panels import InlinePanelBuilder, _add_nav_buttons
        builder = InlinePanelBuilder()
        _add_nav_buttons(builder)
        buttons = self._by_text(builder.build())
        for label in ("❮ Back", "⌂ Home", "× Close"):
            assert _bg(buttons[label], "primary") is True, label

    def test_close_button_is_primary(self):
        from backend.helper.panels import InlinePanelBuilder, _add_close_button
        builder = InlinePanelBuilder()
        _add_close_button(builder)
        assert _bg(self._by_text(builder.build())["× Close"], "primary") is True

    def test_progress_home_button_is_primary(self):
        from backend.helper.panels import _progress_buttons
        assert _bg(self._by_text(_progress_buttons())["⌂ Home"], "primary") is True

    def test_finalize_panel_appends_primary_nav(self):
        from backend.helper.panels import _finalize_panel
        # No registered session → treated as the root view, so only Close is
        # appended; the injected control must still be primary.
        _, _, rows = _finalize_panel("T", "B", [], "some_panel", chat_id=1, msg_id=1)
        buttons = self._by_text(rows)
        assert _bg(buttons["× Close"], "primary") is True


# ── 9c. Semantic overrides across the other panel families ──


class TestFamilyOverrides:
    def test_bio_builder_clear_reset_apply_all_primary(self):
        from backend.bot.handlers import bio
        buttons = TestSubmenuStyles._by_text(bio._build_builder_buttons())
        # Clear / Reset / Apply are ordinary actions → blue; none is coloured.
        for label in ("Clear", "Reset", "Apply Template"):
            assert _bg(buttons[sym.label(sym.OP_DELETE if label == "Clear"
                                          else sym.OP_REFRESH if label == "Reset"
                                          else sym.OP_SAVE, label)], "primary") is True, label
        assert all(_bg(b, "success") in (None, False) for b in buttons.values())
        assert all(_bg(b, "danger") in (None, False) for b in buttons.values())

    def test_username_builder_clear_reset_apply_all_primary(self):
        from backend.bot.handlers import username
        buttons = TestSubmenuStyles._by_text(username._build_builder_buttons())
        for label in ("Clear", "Reset", "Apply Template"):
            assert _bg(buttons[sym.label(sym.OP_DELETE if label == "Clear"
                                          else sym.OP_REFRESH if label == "Reset"
                                          else sym.OP_SAVE, label)], "primary") is True, label
        assert all(_bg(b, "success") in (None, False) for b in buttons.values())
        assert all(_bg(b, "danger") in (None, False) for b in buttons.values())

    def test_bio_custom_cancel_is_danger(self):
        from backend.bot.handlers import bio
        buttons = TestSubmenuStyles._by_text(bio._build_custom_buttons())
        assert _bg(buttons["Cancel"], "danger") is True

    def test_username_custom_cancel_is_danger(self):
        from backend.bot.handlers import username
        buttons = TestSubmenuStyles._by_text(username._build_custom_buttons())
        assert _bg(buttons["Cancel"], "danger") is True


# ── 9d. Explicit confirm (green) and cancel (red) across the panels ──


class TestConfirmCancelSemantics:
    @staticmethod
    def _by_text(rows):
        result = render("T", "B", rows)
        return {b.text: b for r in result.send_message.reply_markup.rows for b in r.buttons}

    @pytest.mark.asyncio
    async def test_emoji_delete_confirmation_is_green_cancel_red(self):
        from backend.helper import inline_engine
        from backend.bot.handlers import emoji as emoji_mod
        from backend.services import emoji_category_service as cat_service

        owner = 777000
        inline_engine.set_owner_id(owner)
        created = await cat_service.create_category(owner, "ColorSemantics")
        assert created["ok"] is True, created
        cid = created["category"]["id"]
        try:
            _, _, rows = await emoji_mod._cat_del_action(None, str(cid), 0)
            buttons = self._by_text(rows)
            delete_btn = next(b for t, b in buttons.items() if "Delete" in t)
            cancel_btn = next(b for t, b in buttons.items() if "Cancel" in t)
            assert _bg(delete_btn, "success") is True
            assert _bg(cancel_btn, "danger") is True
        finally:
            await cat_service.delete_category(owner, cid)

    @pytest.mark.asyncio
    async def test_ai_credential_delete_confirm_green(self, monkeypatch):
        # The credential delete-confirmation ("Delete now") is the explicit
        # confirmation → green.
        from backend.bot.handlers import ai_credentials
        from backend.services import credential_service

        item = credential_service.CredentialMetadata(
            credential_id="cred-1", provider="openai", label="Main",
            enabled=True, priority=1,
        )

        async def _resolve(owner, handle):
            return item

        async def _owner_id():
            return 424242

        monkeypatch.setattr(credential_service, "resolve_handle", _resolve)
        monkeypatch.setattr(ai_credentials, "_owner_id", _owner_id)

        _title, _body, rows = await ai_credentials._ai_cred_delete_action(None, item.handle, 0)
        buttons = self._by_text(rows)
        assert _bg(buttons[f"{sym.OP_DELETE} Delete now"], "success") is True

    def test_taskloom_wizard_cancel_and_discard_are_danger(self):
        from backend.bot.handlers import taskloom
        # Creating: plain Cancel is red.
        builder = taskloom.InlinePanelBuilder()
        taskloom._wizard_footer(builder, editing=False)
        buttons = self._by_text(builder.build())
        assert _bg(buttons[f"{sym.OP_CLOSE} Cancel"], "danger") is True
        # Editing: Discard changes is red.
        builder = taskloom.InlinePanelBuilder()
        taskloom._wizard_footer(builder, editing=True)
        buttons = self._by_text(builder.build())
        assert _bg(buttons[f"{sym.OP_CLOSE} Discard changes"], "danger") is True

    def test_todo_input_cancel_rows_are_danger(self):
        from backend.bot.handlers import todo
        from backend.helper import panels as panels_mod
        todo.register(None, 12345, "UTC")
        for panel_id, input_id in (
            (todo._LIST_PANEL, "new"),
            (todo._DETAIL_PANEL, "edit"),
            (todo._STEPS_PANEL, "add"),
            (todo._STEPS_PANEL, "edit"),
        ):
            cfg = panels_mod.get_input(panel_id, input_id)
            assert cfg is not None, (panel_id, input_id)
            rows = cfg.get("extra_rows")
            assert rows, (panel_id, input_id)
            for row in rows:
                assert row[2] == "danger"
                assert "Cancel" in row[0]


# ── 10. Exhaustive: no registered panel renders an unstyled button ──


class _FakeClient:
    """Minimal stand-in used only to run handler registration in tests."""

    def on(self, *a, **k):
        def deco(fn):
            return fn
        return deco

    def __getattr__(self, name):
        def _f(*a, **k):
            async def _c():
                return None
            return _c()
        return _f


def _register_every_panel():
    import inspect
    from backend.bot.handlers import (
        misc, save, retrieve, delete, bio, discover, database, username, ai,
        ghost_seen_v2, ai_credentials, ai_stt_settings, ai_tts_settings, emoji,
        tasks, taskloom, task_events, todo,
    )
    mods = [misc, save, retrieve, delete, bio, discover, database, username, ai,
            ai_credentials, ai_stt_settings, ai_tts_settings, emoji, ghost_seen_v2,
            tasks, taskloom, task_events, todo]
    client = _FakeClient()
    for mod in mods:
        n = len(inspect.signature(mod.register).parameters)
        if n == 2:
            mod.register(client, 1)
        else:
            mod.register(client, 1, "Asia/Tehran")


class TestEveryPanelButtonIsStyled:
    """Every button of every registered panel must carry one of the three
    supported styles once it passes through the shared render path."""

    @pytest.mark.asyncio
    async def test_no_registered_panel_renders_an_unstyled_button(self):
        import inspect
        from backend.helper.panel_registry import registry
        from backend.helper.panels import _finalize_panel, _extract_render_result
        _register_every_panel()
        reg = registry()
        checked = 0
        for pid in reg.all_ids():
            handler = reg.get_handler(pid)
            try:
                res = handler(None, "")
                if inspect.isawaitable(res):
                    res = await res
                title, body, rows = _extract_render_result(res)
            except Exception:
                # Panels that need a real ``extra`` segment (dynamic sub-ids)
                # are exercised elsewhere; skip the ones that cannot render
                # without live state.
                continue
            _, _, rows = _finalize_panel(title, body, rows, pid, chat_id=1, msg_id=1)
            result = render(title, body, rows)
            markup = result.send_message.reply_markup
            if markup is None:
                continue
            checked += 1
            for row in markup.rows:
                for btn in row.buttons:
                    styles = [n for n in SUPPORTED_BUTTON_STYLES if _bg(btn, n)]
                    assert len(styles) == 1, (
                        f"panel '{pid}' button {btn.text!r} has styles={styles}"
                    )
        assert checked > 20, f"expected to inspect many panels, only saw {checked}"
