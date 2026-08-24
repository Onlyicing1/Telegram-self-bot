"""
Execution 27 — focused regression tests.

Covers:
1. Ghost Seen AI reply flow has NO redundant confirmation step
   (disclosure choice arms the input; context count + disclosure kept)
2. Retention: preset acceptance/persistence/reload, sub-day windows,
   expired-row removal touching ONLY the registry (never Telegram),
   invalid values failing safely, Glass UI panel behavior
3. Bio / Username surfaces carry the selected UI font; templates stay
   protected; AI response text is NEVER restyled (default font only)
4. `Menu` replaces `.menu` as the single textual command and stays
   font-independent; no duplicate menu handler registrations
5. Missing-PV-messages root cause: session entity-cache miss after
   restart → passive dialogs sweep repopulates it; fetch failures render
   honest error/empty states instead of a fake empty conversation
6. GHOST_ROOM_ID remains the only output destination on every path
"""
from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest


CHAT = 4242



def _engine_mock(response="Here you go."):
    engine = AsyncMock()
    engine.execute.return_value = SimpleNamespace(
        success=True, response=response, errors=[],
    )
    return engine


def _register_once() -> None:
    from backend.helper.panel_registry import get_panel_def
    from backend.bot.handlers import ghost_seen

    if get_panel_def("ghost_seen") is not None:
        return

    class DummyClient:
        def on(self, *args, **kwargs):
            def deco(fn):
                return fn
            return deco

    ghost_seen.register(DummyClient(), 12345, "UTC")


# ── 1. Streamlined AI reply flow (removed with Ghost Seen) ──


@pytest.mark.skip(reason="Ghost Seen AI Reply was removed")
class TestStreamlinedAIReplyFlow:
    @pytest.mark.asyncio
    async def test_disclosure_step_precedes_immediate_execution(self):
        """Context-size choice opens disclosure; disclosure starts execution."""
        import inspect
        _register_once()
        from backend.bot.handlers import ghost_seen as gr
        from backend.services import ghost_seen_service as svc

        # The disclosure API exists and gates the dedicated prompt input.
        assert hasattr(svc, "set_reply_disclosure")
        pytest.skip("Ghost Seen legacy AI flow was intentionally removed")

        src = inspect.getsource(gr._ghost_ctx_action)
        assert "set_pending" not in src, (
            "context choice must never arm a manual prompt input"
        )
        assert "consume_reply_flow" not in src, (
            "context choice must not execute — disclosure + prompt come next"
        )

    @pytest.mark.asyncio
    async def test_full_flow_context_disclosure_then_automatic_delivery(self):
        """select → AI Reply → context size → disclosure → automatic delivery."""
        _register_once()
        from unittest.mock import patch
        from backend.bot.handlers import ghost_seen as gr
        from backend.services.ghost_seen_service import start_reply_flow, cancel_reply_flow

        client = AsyncMock()
        gr.configure(client, 12345, "UTC")
        gr._set_current_chat(CHAT)
        cancel_reply_flow(CHAT)
        start_reply_flow(CHAT, 400)

        title, body, buttons = await gr._ghost_ctx_action(None, "", 0)
        assert "action:ghost_ctx:1" in _datas(buttons)
        assert "action:ghost_ctx:10" in _datas(buttons)
        client.send_message.assert_not_called()

        title, body, buttons = await gr._ghost_ctx_action(None, "10", 0)
        assert "action:ghost_inform:yes" in _datas(buttons)
        assert "action:ghost_inform:no" in _datas(buttons)
        client.send_message.assert_not_called()

        ctx = [{"id": i, "out": False, "text": "m", "sender_name": "B"}
               for i in range(391, 401)]
        with patch("backend.ai.engine.engine.get_engine",
                   return_value=_engine_mock()), \
             patch("backend.services.ghost_seen_service.fetch_context_window",
                   new=AsyncMock(return_value=ctx)):
            title, body, buttons = await gr._ghost_inform_action(None, "no", 0)

        assert "Type your AI prompt" not in body
        assert "generated and delivered" in body
        client.send_message.assert_called_once()
        args, _ = client.send_message.call_args
        assert args[0] == 88888          # GHOST_ROOM_ID destination
        assert args[1] == "Here you go."  # verbatim AI output


# ── 2. Retention as a user-configurable duration ──


@pytest.mark.skip(reason="Ghost Seen implementation was removed")
class TestRetentionSetting:
    def test_all_presets_accepted_and_persisted(self, monkeypatch):
        from backend.services import settings_service as ss

        old_loaded, old_cache = ss._loaded, dict(ss._cache)
        try:
            _patch_settings_repo(monkeypatch)
            ss._loaded = False  # force a clean reload under the fake repo
            for label, seconds in ss.RETENTION_PRESETS:
                assert ss.is_retention_preset(seconds), label
                assert ss.set_ghost_seen_retention_seconds(seconds) is True
                assert ss.ghost_seen_retention_seconds() == seconds, label
        finally:
            ss._loaded, ss._cache = old_loaded, old_cache

    def test_preset_examples_from_spec_supported(self):
        from backend.services.settings_service import RETENTION_PRESETS
        seconds = {s for _, s in RETENTION_PRESETS}
        assert {300, 1800, 3600, 86_400, 604_800, 2_592_000} <= seconds
        assert 0 in seconds  # "Never" is an explicit preset
        assert len(seconds) == len(RETENTION_PRESETS)  # no duplicates

    def test_invalid_values_fail_safely(self):
        from backend.services import settings_service as ss

        old_cache = dict(ss._cache)
        try:
            # Only enumerated preset values are accepted; everything else
            # (arbitrary ints, strings, non-ints) is rejected deterministically.
            for bad in (59, 301, 31_536_001, -1, 100, "30m", None, True):
                assert ss.set_ghost_seen_retention_seconds(bad) is False, repr(bad)
            # "Never" (0) IS valid and reads back as Never.
            assert ss.set_ghost_seen_retention_seconds(0) is True
            assert ss.ghost_seen_retention_seconds() == 0
            ss._cache["ghost_seen_retention_seconds"] = 3_600
            assert ss.ghost_seen_retention_seconds() == 3_600
            # Corrupted persisted value reads back deterministically.
            ss._cache["ghost_seen_retention_seconds"] = object()
            assert ss.ghost_seen_retention_seconds() == 2_592_000
        finally:
            ss._cache.clear()
            ss._cache.update(old_cache)

    def test_reload_returns_persisted_seconds(self):
        from unittest.mock import patch
        from backend.services import settings_service as ss

        old_loaded, old_cache = ss._loaded, dict(ss._cache)
        try:
            row = {"key": "global", "ghost_seen_retention_seconds": 7200}
            with patch(
                "backend.services.panel_settings_repository.load",
                return_value=row,
            ):
                ss.load_all()
            assert ss.ghost_seen_retention_seconds() == 7200
            assert ss.format_duration(7200) == "2 hours"
        finally:
            ss._loaded, ss._cache = old_loaded, old_cache

    def test_expired_rows_removed_only_from_registry(self):
        """Retention deletes ghost_chats rows ONLY — no Telegram call can
        even be imported into the retention path."""
        import inspect
        import backend.services.ghost_seen_service as svc

        for fn in ("apply_retention", "delete_expired_rows"):
            src = inspect.getsource(getattr(svc, fn))
            assert "send_message" not in src
            assert "delete_messages" not in src
            assert "iter_messages" not in src
            assert "ReadHistory" not in src

        src_list_handler = inspect.getsource(svc.read_registry_rows)
        assert "delete_messages" not in src_list_handler

    @pytest.mark.asyncio
    async def test_delete_expired_targets_ghost_chats_table_only(self):
        from unittest.mock import MagicMock
        from backend.services.ghost_seen_service import delete_expired_rows

        fake_db = MagicMock()

        async def _fake_run_sync(fn, *args, **kwargs):
            return fn()

        with patch("backend.db.client.get_db", return_value=fake_db), \
             patch("backend.db.client._run_sync", new=_fake_run_sync):
            await delete_expired_rows([1, 2])

        fake_db.table.assert_called_once_with("ghost_chats")
        chain = fake_db.table.return_value
        chain.delete.assert_called_once()
        chain.delete.return_value.in_.assert_called_once()
        args = chain.delete.return_value.in_.call_args[0]
        assert args[1] == [1, 2]

    @pytest.mark.asyncio
    async def test_glass_ui_panel_lists_presets_and_marks_current(self):
        _register_misc()
        from backend.bot.handlers.misc import _ghostret_panel_handler
        from backend.services import settings_service as ss

        old_cache = dict(ss._cache)
        try:
            ss._cache["ghost_seen_retention_seconds"] = 604_800
            title, body, buttons = await _ghostret_panel_handler(None, "")
            assert title == "Ghost Seen Retention"
            assert "7 days" in body
            datas = _datas(buttons)
            for _, s in ss.RETENTION_PRESETS:
                assert f"action:ghostret_set:{s}" in datas
            assert "✓ 7 days" in str([getattr(b, "text", "") for row in buttons for b in row])
        finally:
            ss._cache.clear()
            ss._cache.update(old_cache)

    @pytest.mark.asyncio
    async def test_panel_survives_garbage_setting(self):
        """A corrupted retention value must never crash the panel."""
        _register_misc()
        from backend.bot.handlers.misc import _ghostret_panel_handler
        from backend.services import settings_service as ss

        old_cache = dict(ss._cache)
        try:
            ss._cache["ghost_seen_retention_seconds"] = "not-a-number"
            title, body, buttons = await _ghostret_panel_handler(None, "")
            assert title == "Ghost Seen Retention"
            assert "Current:" in body
        finally:
            ss._cache.clear()
            ss._cache.update(old_cache)

    @pytest.mark.asyncio
    async def test_rejects_non_preset_action_values(self):
        _register_misc()
        from backend.bot.handlers.misc import _ghostret_set_action

        title, body, buttons = await _ghostret_set_action(None, "1234", 0)
        assert "Invalid duration" in body


def _register_misc() -> None:
    from backend.helper.panel_registry import get_panel_def
    from backend.bot.handlers import misc

    # Guard on the panel this suite actually needs — other suites may have
    # run only misc._register_panels(), which registers "settings" without
    # ever registering "menu".
    if get_panel_def("menu") is not None:
        return

    class DummyClient:
        def on(self, *args, **kwargs):
            def deco(fn):
                return fn
            return deco

    misc.register(DummyClient(), 12345)


def _datas(buttons) -> list[str]:
    out = []
    for row in buttons or []:
        for btn in row:
            data = getattr(btn, "data", b"")
            if isinstance(data, bytes):
                out.append(data.decode("utf-8", errors="replace"))
    return out


def _patch_settings_repo(monkeypatch):
    """Isolate settings writes from any live DB (mirrors test_50 fixtures)."""
    state: dict = {}

    def fake_update_field(field, value):
        state[field] = value
        return True

    def fake_load():
        return {"key": "global", **state} if state else None

    monkeypatch.setattr(
        "backend.services.panel_settings_repository.update_field",
        fake_update_field,
    )
    monkeypatch.setattr(
        "backend.services.panel_settings_repository.load", fake_load,
    )


async def _async_call(fn):
    return fn()


# ── 3. Font coverage: Bio / Username styled, AI output exempt ──


class TestFontCoverageBioUsername:
    @pytest.mark.asyncio
    async def test_bio_state_values_render_without_code_spans(self):
        from unittest.mock import patch
        from backend.services import bio_service

        state = {
            "template": "🕒 {time} | 💭 {mood}",
            "mood": "😊",
            "custom_text": "Working hard",
            "last_bio": "🕒 14:30 | 😊",
        }
        with patch(
            "backend.db.client.get_or_create_bio_state",
            new=AsyncMock(return_value=state),
        ), patch.object(bio_service.bio_engine, "is_running", return_value=False):
            result = await bio_service.do_show(1, "UTC")

        assert "Mood: 😊" in result          # plain → font-stylable
        assert "`😊`" not in result
        assert "Text: Working hard" in result
        assert "Preview: 🕒" in result
        # Template keeps its code span: {var} tokens must never be styled.
        assert "`🕒 {time} | 💭 {mood}`" in result

    @pytest.mark.asyncio
    async def test_username_state_values_render_without_code_spans(self):
        from unittest.mock import patch
        from backend.services import username_service

        state = {
            "template": "{time} | {mood}",
            "mood": "🔥",
            "custom_text": "Focus mode",
            "last_name": "14:00 | 🔥",
        }
        with patch(
            "backend.db.client.get_or_create_username_state",
            new=AsyncMock(return_value=state),
        ), patch.object(username_service.username_engine, "is_running", return_value=False):
            result = await username_service.do_show(1, "UTC")

        assert "Mood: 🔥" in result
        assert "`🔥`" not in result
        assert "Text: Focus mode" in result
        assert "`{time} | {mood}`" in result  # template protected

    def test_selected_font_actually_applies_to_bio_display_text(self):
        from backend.helper.font_style import apply_font
        from backend.helper.panel_render import _style
        from backend.services import settings_service as ss

        old_cache = dict(ss._cache)
        try:
            ss._cache["dashboard_font"] = "script"
            body = "**Bio State**\n\nMood: ok Text: Working hard"
            styled = _style(body)
            expected_mood = apply_font("Mood", "script")
            expected_working = apply_font("Working", "script")
            assert expected_mood in styled
            assert expected_working in styled
            assert styled != body
        finally:
            ss._cache.clear()
            ss._cache.update(old_cache)

    @pytest.mark.asyncio
    @pytest.mark.skip(reason="Ghost Seen AI Reply was removed")
    async def test_ai_response_never_receives_decorative_font(self):
        """AI-generated text must reach the destination byte-identical —
        the selected UI font applies to Glass UI only."""
        _register_once()
        from backend.bot.handlers import ghost_seen as gr
        from backend.services import settings_service as ss
        from backend.services.ghost_seen_service import toggle_selection

        old_cache = dict(ss._cache)
        try:
            ss._cache["dashboard_font"] = "script"

            client = AsyncMock()
            gr.configure(client, 12345, "UTC")
            gr._set_current_chat(CHAT)
            toggle_selection(CHAT, 1)
            toggle_selection(CHAT, 2)

            engine = AsyncMock()
            engine.execute.return_value = SimpleNamespace(
                success=True, response="Hello world, this is AI prose.",
                errors=[],
            )
            msgs = [
                {"id": 1, "out": False, "text": "a", "sender_name": "B", "date": None},
                {"id": 2, "out": True, "text": "b", "sender_name": "", "date": None},
            ]
            with patch("backend.ai.engine.engine.get_engine", return_value=engine), \
                 patch("backend.telegram_api.messages.get_messages",
                       new=AsyncMock(return_value=msgs)):
                await gr._ghost_ai_input("summarize", 0, 0, 0, 0)

            args, _ = client.send_message.call_args
            assert args[0] == 88888
            assert args[1] == "Hello world, this is AI prose."
            # No mathematical-alphanumeric glyphs may leak into AI text.
            assert all(ord(ch) < 0x1D400 or ord(ch) > 0x1D7FF for ch in args[1])
        finally:
            ss._cache.clear()
            ss._cache.update(old_cache)


# ── 4. Menu command rename ──


class TestMenuCommandRename:
    def test_pattern_is_literal_menu_word(self):
        from pathlib import Path
        src = Path("backend/bot/handlers/misc.py").read_text(encoding="utf-8")
        assert 'pattern=r"^Menu$"' in src
        assert r'pattern=r"^\.menu$"' not in src

    def test_no_hidden_dot_menu_alias_anywhere_active(self):
        from pathlib import Path
        handlers = Path("backend/bot/handlers")
        offenders = [
            p.name for p in handlers.glob("*.py")
            if "\\.menu" in p.read_text(encoding="utf-8")
        ]
        assert offenders == []

    def test_command_matching_is_independent_of_selected_font(self):
        """The trigger is matched against raw outgoing text; changing the
        decorative font cannot influence whether `Menu` opens the panel."""
        import re
        from backend.helper.font_style import apply_font, DEFAULT_FONT_KEY
        from backend.services import settings_service as ss

        pattern = re.compile(r"^Menu$")
        old_cache = dict(ss._cache)
        try:
            for font in (DEFAULT_FONT_KEY, "script", "fraktur_bold"):
                ss._cache["dashboard_font"] = font
                # Raw typed text always matches, whatever font is selected.
                assert pattern.fullmatch("Menu") is not None
                assert pattern.fullmatch(".menu") is None
                styled = apply_font("Menu", font)
                if styled != "Menu":
                    # A visually-styled rendering of the word never matches.
                    assert pattern.fullmatch(styled) is None
        finally:
            ss._cache.clear()
            ss._cache.update(old_cache)

    def test_menu_registered_exactly_once(self):
        _register_misc()
        from backend.bot.handlers import misc
        from backend.helper.panel_registry import get_panel_def

        assert get_panel_def("menu") is not None
        # Re-registering must not duplicate (idempotent single definition).
        definition = get_panel_def("menu")
        _register_misc()
        assert get_panel_def("menu") is definition


# ── 5. Missing-PV-messages root cause ──


class _UnresolvableClient:
    """Simulates a fresh StringSession: bare-ID lookups fail until a
    dialogs sweep repopulates the entity cache."""

    def __init__(self, resolvable_after_sweep=True, fail_iter=False):
        self.sweeps = 0
        self.resolvable_after_sweep = resolvable_after_sweep
        self.fail_iter = fail_iter

    async def get_input_entity(self, chat_id):
        if self.sweeps == 0 or not self.resolvable_after_sweep:
            raise ValueError(f"Could not find the input entity for {chat_id}")
        return SimpleNamespace(user_id=chat_id)

    def iter_dialogs(self, limit=None, archived=None):
        async def _gen():
            self.sweeps += 1
            return
            yield
        return _gen()

    def iter_messages(self, chat_id, limit=None):
        async def _gen():
            if self.fail_iter:
                raise RuntimeError("rpc down")
            for i in range(min(limit or 0, 3)):
                yield SimpleNamespace(id=i + 1, out=False, text=f"m{i}",
                                      sender_id=55, date=None, media=None)
        return _gen()


@pytest.mark.skip(reason="Ghost Seen implementation was removed")
class TestEntityResolutionRootCause:
    @pytest.mark.asyncio
    async def test_unresolvable_chat_resolves_after_dialogs_sweep(self):
        from backend.services.ghost_seen_service import ensure_entity

        client = _UnresolvableClient(resolvable_after_sweep=True)
        assert await ensure_entity(client, CHAT) is True
        assert client.sweeps >= 1  # passive sweep happened exactly when needed

    @pytest.mark.asyncio
    async def test_truly_unresolvable_chat_fails_closed(self):
        from backend.services.ghost_seen_service import ensure_entity

        client = _UnresolvableClient(resolvable_after_sweep=False)
        assert await ensure_entity(client, CHAT) is False

    @pytest.mark.asyncio
    async def test_fetch_chunk_reports_entity_failure_honestly(self):
        from backend.services.ghost_seen_service import fetch_chunk

        client = _UnresolvableClient(resolvable_after_sweep=False)
        msgs, error = await fetch_chunk(client, CHAT, 0)
        assert msgs == [] and error == "entity"

    @pytest.mark.asyncio
    async def test_fetch_chunk_reports_iteration_failure_honestly(self):
        from backend.services.ghost_seen_service import fetch_chunk

        client = _UnresolvableClient(fail_iter=True)
        msgs, error = await fetch_chunk(client, CHAT, 0)
        assert msgs == [] and error == "fetch"

    @pytest.mark.asyncio
    async def test_fetch_chunk_success_after_sweep(self):
        from backend.services.ghost_seen_service import fetch_chunk

        client = _UnresolvableClient()
        msgs, error = await fetch_chunk(client, CHAT, 0)
        assert error == ""
        # iter_messages yields newest-first (Telegram order); the page is
        # reversed into oldest→newest for display.
        assert [m["id"] for m in msgs] == [3, 2, 1]

    @pytest.mark.asyncio
    async def test_previously_failing_private_chat_now_renders(self):
        """Regression: a valid private chat whose entity was not cached
        (e.g. after restart) renders its messages instead of an empty page."""
        _register_once()
        from backend.bot.handlers import ghost_seen as gr

        gr.configure(_UnresolvableClient(), 12345, "UTC")
        title, body, buttons = await gr._ghost_chat_panel_handler(None, str(CHAT))
        assert "m0" in body and "m1" in body
        assert "temporarily unavailable" not in body

    @pytest.mark.asyncio
    async def test_chat_panel_shows_honest_error_state(self):
        _register_once()
        from backend.bot.handlers import ghost_seen as gr

        gr.configure(_UnresolvableClient(resolvable_after_sweep=False),
                     12345, "UTC")
        title, body, buttons = await gr._ghost_chat_panel_handler(None, str(CHAT))
        assert "temporarily unavailable" in body
        assert "No messages" not in body  # failure is NOT rendered as empty

    @pytest.mark.asyncio
    async def test_empty_conversation_rendered_honestly(self):
        _register_once()
        from backend.bot.handlers import ghost_seen as gr

        gr.configure(_UnresolvableClient(), 12345, "UTC")
        title, body, buttons = await gr._ghost_chat_panel_handler(None, str(CHAT))
        # The stub yields 3 messages; force a genuinely-empty chat instead.
        class EmptyClient(_UnresolvableClient):
            def iter_messages(self, chat_id, limit=None):
                async def _gen():
                    return
                    yield
                return _gen()

        gr.configure(EmptyClient(), 12345, "UTC")
        title, body, buttons = await gr._ghost_chat_panel_handler(None, str(CHAT))
        assert "No messages in this conversation yet." in body

    @pytest.mark.asyncio
    @pytest.mark.skip(reason="Ghost Seen AI Reply action was removed")
    async def test_reply_target_banner_resolves_entity_first(self):
        """The anchor fetch also benefits from the entity-resolution fix."""
        _register_once()
        from backend.bot.handlers import ghost_seen as gr
        from backend.services.ghost_seen_service import toggle_selection

        client = AsyncMock()
        client.get_input_entity = AsyncMock(return_value=SimpleNamespace())
        client.get_messages = AsyncMock(return_value=SimpleNamespace(
            id=777, out=False, text="hello", sender_id=55, date=None,
        ))
        gr.configure(client, 12345, "UTC")
        gr._set_current_chat(CHAT)
        toggle_selection(CHAT, 777)

        pytest.skip("Ghost Seen legacy AI flow was intentionally removed")
        assert "#777" in body and "hello" in body


# ── 6. Destination invariants unchanged ──


@pytest.mark.skip(reason="Ghost Seen AI Reply was removed")
@pytest.mark.skip(reason="Ghost Seen implementation was removed")
class TestDestinationInvariants:
    @pytest.mark.asyncio
    async def test_every_output_path_uses_ghost_room_id(self):
        import inspect
        from pathlib import Path
        from backend.bot.handlers import ghost_seen as gr

        for fn_name in ("_ghost_reply_input", "_ghost_reply_no_quote_input",
                        "_ghost_ai_input", "_execute_single_ghost_ai_reply"):
            src = inspect.getsource(getattr(gr, fn_name))
            assert "_resolve_ghost_destination" in src, fn_name

        src = Path("backend/bot/handlers/ghost_seen.py").read_text(encoding="utf-8")
        # No fallback destination mechanism exists.
        assert "get_dialogs()" not in src.split("def register")[0].split("_resolve_ghost_destination")[0]

    @pytest.mark.asyncio
    async def test_missing_ghost_room_id_blocks_ai_reply_flow(self):
        _register_once()
        from backend.bot.handlers import ghost_seen as gr
        from backend.services.ghost_seen_service import (
            start_reply_flow, set_reply_context_count, set_reply_disclosure,
            get_reply_flow,
        )

        old_val = os.environ.pop("GHOST_ROOM_ID", None)
        try:
            client = AsyncMock()
            gr.configure(client, 12345, "UTC")
            gr._set_current_chat(CHAT)
            start_reply_flow(CHAT, 900)
            set_reply_context_count(CHAT, 1)
            set_reply_disclosure(CHAT, False)

            ctx = [{"id": 900, "out": False, "text": "m", "sender_name": "B",
                    "date": None}]
            with patch("backend.ai.engine.engine.get_engine",
                       return_value=_engine_mock()), \
                 patch("backend.services.ghost_seen_service.fetch_context_window",
                       new=AsyncMock(return_value=ctx)):
                await gr._execute_single_ghost_ai_reply(CHAT)

            client.send_message.assert_not_called()   # fail closed, always
            assert get_reply_flow(CHAT) is None
        finally:
            if old_val is not None:
                os.environ["GHOST_ROOM_ID"] = old_val
