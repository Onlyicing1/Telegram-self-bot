"""
Ghost Seen flows — Execution 26/28 focused regression tests.

Covers:
1. Authoritative private-human-only source validation (registry boundary)
2. Incoming listener delegates to the service validator (no inline bypass)    3. Reply-flow state machine (anchor → context N → disclosure → automatic
   generation; single use; every step explicit)

4. Context window counts exactly N messages ending at the anchor
5. REPLY TARGET banner is unambiguous and honest
6. Actions / context menus encode state in callback data
7. Manual removal clears registry + local state only
8. Opening a chat sets the working chat (state-tracking bug regression)
9. AI delivery: response verbatim (never suffixed/styled); legacy
   multi-select input unchanged; fail-closed on missing GHOST_ROOM_ID
10. No second dispatcher: engine.execute remains the only execution path
"""
from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest


CHAT = 4242


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


def _engine_mock(response="Here you go."):
    engine = AsyncMock()
    engine.execute.return_value = SimpleNamespace(
        success=True, response=response, errors=[],
    )
    return engine


def _button_datas(buttons) -> list[str]:
    out = []
    for row in buttons or []:
        for btn in row:
            data = getattr(btn, "data", b"")
            if isinstance(data, bytes):
                out.append(data.decode("utf-8", errors="replace"))
    return out


def _fake_sender(**kw):
    defaults = dict(id=CHAT, first_name="Alice", last_name="",
                    username="alice", bot=False)
    defaults.update(kw)
    return SimpleNamespace(**defaults)


@pytest.fixture(autouse=True)
def _ghost_env(monkeypatch):
    monkeypatch.setenv("GHOST_ROOM_ID", "88888")
    yield
    from backend.services.ghost_seen_service import (
        clear_selection,
        cancel_reply_flow,
        reset_chat_state,
    )
    reset_chat_state(CHAT)
    clear_selection(CHAT)
    cancel_reply_flow(CHAT)


# ── 1. Source validation ──


class TestValidatePrivateSource:
    def test_human_private_sender_accepted(self):
        from backend.services.ghost_seen_service import validate_private_source
        assert validate_private_source(CHAT, _fake_sender(), 12345) == "Alice"

    def test_bot_rejected(self):
        from backend.services.ghost_seen_service import validate_private_source
        assert validate_private_source(CHAT, _fake_sender(bot=True), 12345) is None

    def test_owner_self_chat_rejected(self):
        from backend.services.ghost_seen_service import validate_private_source
        assert validate_private_source(12345, _fake_sender(id=12345), 12345) is None

    def test_group_or_channel_id_rejected(self):
        from backend.services.ghost_seen_service import validate_private_source
        assert validate_private_source(-1001234, _fake_sender(), 12345) is None
        assert validate_private_source(None, _fake_sender(), 12345) is None
        assert validate_private_source(0, _fake_sender(), 12345) is None

    def test_chat_sender_mismatch_rejected(self):
        from backend.services.ghost_seen_service import validate_private_source
        assert validate_private_source(CHAT, _fake_sender(id=999), 12345) is None

    def test_missing_sender_rejected(self):
        from backend.services.ghost_seen_service import validate_private_source
        assert validate_private_source(CHAT, None, 12345) is None

    def test_listener_uses_service_validator_not_inline_logic(self):
        import inspect
        from pathlib import Path
        src = Path("backend/bot/handlers/ghost_seen.py").read_text(encoding="utf-8")
        listener_src = src.split("async def _ghost_incoming_listener")[1]
        assert "validate_private_source" in listener_src
        # Bots must be rejected by the shared validator — no inline bypass.
        assert 'getattr(sender, "bot"' not in listener_src


# ── 3. Reply-flow state machine ──


class TestReplyFlowMachine:
    def test_start_requires_positive_anchor(self):
        from backend.services.ghost_seen_service import (
            start_reply_flow, get_reply_flow, cancel_reply_flow,
        )
        cancel_reply_flow(CHAT)
        start_reply_flow(CHAT, 0)
        assert get_reply_flow(CHAT) is None
        start_reply_flow(CHAT, 777)
        flow = get_reply_flow(CHAT)
        assert flow == {"anchor": 777, "context_n": None, "informed": None}
        cancel_reply_flow(CHAT)

    def test_context_count_only_from_allowed_set(self):
        from backend.services.ghost_seen_service import (
            start_reply_flow, set_reply_context_count, cancel_reply_flow,
            ALLOWED_CONTEXT_COUNTS,
        )
        cancel_reply_flow(CHAT)
        start_reply_flow(CHAT, 5)
        for bad in (-1, 0, 2, 3, 4, 99):
            assert not set_reply_context_count(CHAT, bad)
        for good in ALLOWED_CONTEXT_COUNTS:
            assert set_reply_context_count(CHAT, good)

    def test_disclosure_requires_context_choice(self):
        """Disclosure is recorded only after the context size is chosen;
        non-boolean choices are rejected."""
        from backend.services.ghost_seen_service import (
            start_reply_flow, set_reply_context_count, set_reply_disclosure,
            get_reply_flow, cancel_reply_flow,
        )
        cancel_reply_flow(CHAT)
        start_reply_flow(CHAT, 55)
        assert not set_reply_disclosure(CHAT, True)      # no context yet
        assert not set_reply_disclosure(CHAT, "yes")    # non-bool rejected
        assert set_reply_context_count(CHAT, 5)
        assert set_reply_disclosure(CHAT, True)
        assert get_reply_flow(CHAT)["informed"] is True
        cancel_reply_flow(CHAT)

    def test_consume_is_single_use_and_rejects_incomplete(self):
        """An incomplete flow is never returned by consume (it is discarded),
        and a complete flow is returned exactly once."""
        from backend.services.ghost_seen_service import (
            start_reply_flow, set_reply_context_count, set_reply_disclosure,
            get_reply_flow, consume_reply_flow, cancel_reply_flow,
        )
        cancel_reply_flow(CHAT)
        start_reply_flow(CHAT, 9)
        assert get_reply_flow(CHAT)["context_n"] is None
        assert set_reply_context_count(CHAT, 10)
        assert get_reply_flow(CHAT)["informed"] is None  # disclosure missing
        assert set_reply_disclosure(CHAT, False)
        flow = consume_reply_flow(CHAT)
        assert flow["anchor"] == 9 and flow["context_n"] == 10
        assert flow["informed"] is False
        assert consume_reply_flow(CHAT) is None  # consumed once

        # An incomplete flow is discarded by the single-use consumer.
        start_reply_flow(CHAT, 9)
        set_reply_context_count(CHAT, 10)
        assert consume_reply_flow(CHAT) is None  # disclosure missing
        assert get_reply_flow(CHAT) is None


# ── 4. Context window counting ──


class TestContextWindow:
    @staticmethod
    def _fake_client(anchor_id=100, older=(99, 98, 97, 96)):
        anchor = SimpleNamespace(id=anchor_id, out=False, text="anchor",
                                 sender_id=55, date=None, media=None)

        async def _iter(chat_id, offset_id=None, limit=None):
            for oid in list(older)[: max(0, (limit or 0))]:
                yield SimpleNamespace(id=oid, out=True, text="old",
                                      sender_id=55, date=None, media=None)

        class C:
            async def get_input_entity(self, chat_id):
                # Entity already cached — no sweep needed.
                return SimpleNamespace(user_id=chat_id)

            async def get_messages(self, chat_id, ids=None):
                return anchor

            def iter_messages(self, chat_id, **kw):
                return _iter(chat_id, **kw)

        return C()

    @pytest.mark.asyncio
    async def test_exact_n_messages_ending_at_anchor(self):
        from backend.services.ghost_seen_service import fetch_context_window
        with patch(
            "backend.services.ghost_seen_service.serialize_message",
            lambda m: {"id": m.id, "out": m.out, "text": m.text,
                       "sender_name": "X", "date": None},
        ):
            window = await fetch_context_window(self._fake_client(), CHAT, 100, 5)
        assert [m["id"] for m in window] == [96, 97, 98, 99, 100]

    @pytest.mark.asyncio
    async def test_single_message_context_is_anchor_only(self):
        from backend.services.ghost_seen_service import fetch_context_window
        with patch(
            "backend.services.ghost_seen_service.serialize_message",
            lambda m: {"id": m.id, "out": m.out, "text": m.text,
                       "sender_name": "X", "date": None},
        ):
            window = await fetch_context_window(self._fake_client(), CHAT, 100, 1)
        assert [m["id"] for m in window] == [100]

    @pytest.mark.asyncio
    async def test_window_caps_at_available_history(self):
        from backend.services.ghost_seen_service import fetch_context_window
        with patch(
            "backend.services.ghost_seen_service.serialize_message",
            lambda m: {"id": m.id, "out": m.out, "text": m.text,
                       "sender_name": "X", "date": None},
        ):
            window = await fetch_context_window(self._fake_client(), CHAT, 100, 20)
        assert len(window) == 5  # anchor + 4 available older messages

    @pytest.mark.asyncio
    async def test_unresolvable_anchor_returns_empty(self):
        from backend.services.ghost_seen_service import fetch_context_window

        class C:
            async def get_input_entity(self, chat_id):
                return SimpleNamespace(user_id=chat_id)

            async def get_messages(self, chat_id, ids=None):
                return None

        assert await fetch_context_window(C(), CHAT, 404, 5) == []


# ── 5. Reply-target banner ──


class TestReplyTargetBanner:
    def test_banner_names_target_sender_direction_and_content(self):
        from backend.services.ghost_seen_service import format_reply_target
        banner = format_reply_target(
            {"id": 77, "sender_name": "Bob", "out": False,
             "text": "hello there", "date": None},
            owner_id=12345,
        )
        assert "#77" in banner
        assert "FROM THEM" in banner
        assert "Bob" in banner
        assert '"hello there"' in banner

    def test_banner_marks_own_messages_as_from_me(self):
        from backend.services.ghost_seen_service import format_reply_target
        banner = format_reply_target(
            {"id": 78, "sender_name": "", "out": True, "text": "", "date": None},
            owner_id=12345,
        )
        assert "FROM ME" in banner
        assert '""' not in banner  # never renders an empty quoted blob


# ── Handler wiring ──


class TestHandlerWiring:
    @pytest.mark.asyncio
    async def test_open_sets_working_chat(self, monkeypatch):
        """Regression: ghost_open previously never recorded the open chat."""
        _register_once()
        from backend.bot.handlers import ghost_seen as gr
        from unittest.mock import patch as _p

        monkeypatch.setenv("GHOST_ROOM_ID", "1")

        class _EmptyClient:
            def iter_messages(self, chat_id, limit=None):
                async def _gen():
                    return
                    yield
                return _gen()

        gr.configure(_EmptyClient(), 12345, "UTC")
        with _p("backend.db.client.get_db", return_value=None):
            await gr._ghost_open_action(None, str(CHAT), 0)
        assert gr._current_chat() == CHAT

    @pytest.mark.asyncio
    async def test_actions_menu_shows_banner_and_choices(self, monkeypatch):
        _register_once()
        from backend.bot.handlers import ghost_seen as gr
        from backend.services.ghost_seen_service import (
            toggle_selection, get_reply_flow, cancel_reply_flow,
        )
        gr.configure(AsyncMock(), 12345, "UTC")
        gr._set_current_chat(CHAT)
        toggle_selection(CHAT, 777)
        with patch(
            "backend.telegram_api.messages.get_messages",
            new=AsyncMock(return_value=[{"id": 777, "sender_name": "Bob",
                                         "out": False, "text": "hi",
                                         "date": None}]),
        ):
            title, body, buttons = await gr._ghost_actions_action(None, "", 0)
        assert "Reply target" in body and "#777" in body
        datas = _button_datas(buttons)
        assert "input:ghost_chat:reply" in datas
        assert "input:ghost_chat:reply_no_quote" in datas
        assert "action:ghost_ctx" in datas
        flow = get_reply_flow(CHAT)
        assert flow and flow["anchor"] == 777
        cancel_reply_flow(CHAT)

    @pytest.mark.asyncio
    async def test_actions_menu_requires_exactly_one_selection(self):
        _register_once()
        from backend.bot.handlers import ghost_seen as gr
        from backend.services.ghost_seen_service import toggle_selection, cancel_reply_flow
        gr.configure(AsyncMock(), 12345, "UTC")
        gr._set_current_chat(CHAT)
        toggle_selection(CHAT, 1)
        toggle_selection(CHAT, 2)
        title, body, buttons = await gr._ghost_actions_action(None, "", 0)
        assert "exactly one" in body.lower()
        from backend.services.ghost_seen_service import get_reply_flow
        assert get_reply_flow(CHAT) is None
        cancel_reply_flow(CHAT)

    @pytest.mark.asyncio
    async def test_ctx_menu_offers_allow_list_then_disclosure(self):
        """Choosing a size opens the disclosure choice — nothing executes
        and no prompt is armed at this step."""
        _register_once()
        from backend.bot.handlers import ghost_seen as gr
        from backend.services.ghost_seen_service import (
            start_reply_flow, get_reply_flow, cancel_reply_flow,
        )
        client = AsyncMock()
        gr.configure(client, 12345, "UTC")
        gr._set_current_chat(CHAT)
        cancel_reply_flow(CHAT)
        start_reply_flow(CHAT, 777)

        title, body, buttons = await gr._ghost_ctx_action(None, "", 0)
        datas = _button_datas(buttons)
        for n in (1, 5, 10, 20):
            assert f"action:ghost_ctx:{n}" in datas

        title, body, buttons = await gr._ghost_ctx_action(None, "5", 0)
        datas = _button_datas(buttons)
        assert "action:ghost_inform:yes" in datas
        assert "action:ghost_inform:no" in datas
        client.send_message.assert_not_called()
        flow = get_reply_flow(CHAT)
        assert flow and flow["context_n"] == 5 and flow["informed"] is None

        title, body, _ = await gr._ghost_ctx_action(None, "7", 0)
        assert "Invalid context size" in body
        assert get_reply_flow(CHAT) is None  # flow cancelled on invalid size
        cancel_reply_flow(CHAT)

    @pytest.mark.asyncio
    async def test_inform_executes_immediately_without_prompt(self):
        """Disclosure starts the fixed AI reply; no owner prompt is armed."""
        _register_once()
        from backend.bot.handlers import ghost_seen as gr
        from backend.services.ghost_seen_service import (
            start_reply_flow, set_reply_context_count, get_reply_flow,
            cancel_reply_flow,
        )
        client = AsyncMock()
        gr.configure(client, 12345, "UTC")
        gr._set_current_chat(CHAT)
        cancel_reply_flow(CHAT)
        start_reply_flow(CHAT, 11)
        set_reply_context_count(CHAT, 1)

        title, body, buttons = await gr._ghost_inform_action(None, "maybe", 0)
        assert "Invalid disclosure choice" in body
        assert get_reply_flow(CHAT) is None

        start_reply_flow(CHAT, 11)
        set_reply_context_count(CHAT, 1)
        ctx = [{"id": 11, "out": False, "text": "hello", "sender_name": "Bob"}]
        with patch("backend.ai.engine.engine.get_engine",
                   return_value=_engine_mock("Automatic reply.")), \
             patch("backend.services.ghost_seen_service.fetch_context_window",
                   new=AsyncMock(return_value=ctx)):
            title, body, buttons = await gr._ghost_inform_action(None, "yes", 0)
        assert "Type your AI prompt" not in body
        assert "generated and delivered" in body
        assert get_reply_flow(CHAT) is None
        client.send_message.assert_called_once()

    @pytest.mark.asyncio
    async def test_remove_clears_registry_row_and_local_state_only(self, monkeypatch):
        _register_once()
        from backend.bot.handlers import ghost_seen as gr
        from backend.services.ghost_seen_service import (
            toggle_selection, remove_chat, get_selection,
        )
        gr.configure(AsyncMock(), 12345, "UTC")
        gr._set_current_chat(CHAT)
        toggle_selection(CHAT, 5)

        sent = {}
        removed = {}

        async def fake_remove(chat_id):
            removed["chat_id"] = chat_id
            return True

        with patch("backend.services.ghost_seen_service.remove_chat",
                   side_effect=fake_remove), \
             patch("backend.db.client.get_db", return_value=None):
            result = await gr._ghost_remove_action(None, "", 0)

        assert removed["chat_id"] == CHAT
        title, body, buttons = result
        assert "Ghost Seen" in title  # returned to the chat list, edit-in-place
        # Telegram-side client was never touched.
        gr._self_client.send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_remove_service_degrades_without_db(self, monkeypatch):
        from backend.services.ghost_seen_service import (
            toggle_selection, remove_chat, get_selection, reset_chat_state,
        )
        reset_chat_state(CHAT)
        toggle_selection(CHAT, 9)
        with patch("backend.db.client.get_db", return_value=None):
            ok = await remove_chat(CHAT)
        assert ok is False
        assert get_selection(CHAT) == set()  # local UI state still cleared


# ── AI delivery honesty ──


class TestAIDelivery:
    @staticmethod
    def _engine_mock(response="Here you go."):
        engine = AsyncMock()
        engine.execute.return_value = SimpleNamespace(
            success=True, response=response, errors=[],
        )
        return engine

    @pytest.mark.asyncio
    async def test_ai_reply_delivered_verbatim_to_ghost_room(self):
        """The full flow (context → disclosure → automatic generation) delivers the AI
        response byte-exact to GHOST_ROOM_ID when disclosure is declined."""
        _register_once()
        from backend.bot.handlers import ghost_seen as gr
        from backend.services.ghost_seen_service import (
            start_reply_flow, set_reply_context_count, set_reply_disclosure,
        )
        client = AsyncMock()
        gr.configure(client, 12345, "UTC")
        gr._set_current_chat(CHAT)
        start_reply_flow(CHAT, 500)
        set_reply_context_count(CHAT, 1)
        set_reply_disclosure(CHAT, False)

        ctx = [{"id": 500, "out": False, "text": "anchor",
                "sender_name": "Bob", "date": None}]
        with patch("backend.ai.engine.engine.get_engine",
                   return_value=self._engine_mock("Natural reply text.")), \
             patch("backend.services.ghost_seen_service.fetch_context_window",
                   new=AsyncMock(return_value=ctx)):
            await gr._execute_single_ghost_ai_reply(CHAT)

        client.send_message.assert_called_once()
        args, _ = client.send_message.call_args
        assert args[0] == 88888  # GHOST_ROOM_ID, never the source chat
        assert args[1] == "Natural reply text."  # verbatim, no suffix

    @pytest.mark.asyncio
    async def test_ai_reply_appends_disclosure_suffix_when_opted_in(self):
        """When the owner opts to inform the recipient, the disclosure
        suffix is appended to the AI text before delivery."""
        _register_once()
        from backend.bot.handlers import ghost_seen as gr
        from backend.services.ghost_seen_service import (
            start_reply_flow, set_reply_context_count, set_reply_disclosure,
            AI_DISCLOSURE_SUFFIX,
        )
        client = AsyncMock()
        gr.configure(client, 12345, "UTC")
        gr._set_current_chat(CHAT)
        start_reply_flow(CHAT, 501)
        set_reply_context_count(CHAT, 1)
        set_reply_disclosure(CHAT, True)

        ctx = [{"id": 501, "out": False, "text": "anchor",
                "sender_name": "Bob", "date": None}]
        with patch("backend.ai.engine.engine.get_engine",
                   return_value=self._engine_mock("Reply text.")), \
             patch("backend.services.ghost_seen_service.fetch_context_window",
                   new=AsyncMock(return_value=ctx)):
            await gr._execute_single_ghost_ai_reply(CHAT)

        client.send_message.assert_called_once()
        args, _ = client.send_message.call_args
        assert args[0] == 88888
        assert args[1] == "Reply text." + AI_DISCLOSURE_SUFFIX

    @pytest.mark.asyncio
    async def test_legacy_multi_select_path_unchanged_verbatim(self, monkeypatch):
        _register_once()
        from backend.bot.handlers import ghost_seen as gr
        from backend.services.ghost_seen_service import toggle_selection
        client = AsyncMock()
        gr.configure(client, 12345, "UTC")
        gr._set_current_chat(CHAT)
        toggle_selection(CHAT, 1)
        toggle_selection(CHAT, 2)

        msgs = [{"id": 1, "out": False, "text": "a", "sender_name": "B",
                 "date": None},
                {"id": 2, "out": True, "text": "b", "sender_name": "",
                 "date": None}]
        with patch("backend.ai.engine.engine.get_engine",
                   return_value=self._engine_mock()), \
             patch("backend.telegram_api.messages.get_messages",
                   new=AsyncMock(return_value=msgs)):
            await gr._ghost_ai_input("summarize", 0, 0, 0, 0)

        client.send_message.assert_called_once()
        args, _ = client.send_message.call_args
        assert args[0] == 88888
        assert args[1] == "Here you go."  # verbatim

    @pytest.mark.asyncio
    async def test_missing_ghost_room_id_fails_closed_and_cancels_flow(self, monkeypatch):
        """With GHOST_ROOM_ID missing, the prompt input never sends and the
        pending flow is consumed without delivery."""
        _register_once()
        from backend.bot.handlers import ghost_seen as gr
        from backend.services.ghost_seen_service import (
            start_reply_flow, set_reply_context_count, set_reply_disclosure,
            get_reply_flow,
        )
        monkeypatch.delenv("GHOST_ROOM_ID", raising=False)
        client = AsyncMock()
        gr.configure(client, 12345, "UTC")
        gr._set_current_chat(CHAT)
        start_reply_flow(CHAT, 600)
        set_reply_context_count(CHAT, 1)
        set_reply_disclosure(CHAT, False)

        ctx = [{"id": 600, "out": False, "text": "m", "sender_name": "B",
                "date": None}]
        with patch("backend.ai.engine.engine.get_engine",
                   return_value=self._engine_mock()), \
             patch("backend.services.ghost_seen_service.fetch_context_window",
                   new=AsyncMock(return_value=ctx)):
            await gr._execute_single_ghost_ai_reply(CHAT)

        client.send_message.assert_not_called()   # no fallback destination ever
        assert get_reply_flow(CHAT) is None       # flow consumed, nothing sent

    @pytest.mark.asyncio
    async def test_failed_context_fetch_blocks_send(self):
        """An unresolvable context window blocks delivery — no message is
        ever sent with partial context."""
        _register_once()
        from backend.bot.handlers import ghost_seen as gr
        from backend.services.ghost_seen_service import (
            start_reply_flow, set_reply_context_count, set_reply_disclosure,
        )
        client = AsyncMock()
        gr.configure(client, 12345, "UTC")
        gr._set_current_chat(CHAT)
        start_reply_flow(CHAT, 700)
        set_reply_context_count(CHAT, 1)
        set_reply_disclosure(CHAT, False)

        with patch("backend.ai.engine.engine.get_engine",
                   return_value=self._engine_mock()), \
             patch("backend.services.ghost_seen_service.fetch_context_window",
                   new=AsyncMock(side_effect=RuntimeError("rpc down"))):
            await gr._execute_single_ghost_ai_reply(CHAT)

        client.send_message.assert_not_called()


class TestNoSecondArchitecture:
    def test_engine_execute_remains_the_only_ai_path(self):
        import inspect
        import backend.services.ghost_seen_service as svc
        src = inspect.getsource(svc.execute_ghost_seen_ai)
        assert "get_engine" in src and "engine.execute" in src
        assert "ProviderManager" not in src
        assert "Dispatcher" not in src
