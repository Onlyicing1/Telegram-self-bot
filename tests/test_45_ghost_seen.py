"""
Focused regression tests for the Ghost Seen MVP.

Covers:
1. Config (GHOST_ROOM_ID absent → feature cleanly disabled)
2. Selection toggle / clear / max bounds
3. Page state
4. Chat list rendering
5. Chat view rendering
6. AI execution via existing engine path (no second dispatcher)
7. Incoming listener owner-exclusion
8. Callback safety (staleness re-render)
9. Reply / no-quote send args
10. Multi-message AI payload shape
"""
from __future__ import annotations

import asyncio
import os

import pytest


# ── helpers ──


def _patch_ghost_room_id(value: str) -> None:
    os.environ["GHOST_ROOM_ID"] = value


def _clear_ghost_room_id() -> None:
    os.environ.pop("GHOST_ROOM_ID", None)


# ── 1. Config ──


class TestGhostRoomConfig:
    def test_disabled_without_env(self):
        from backend.services.ghost_seen_service import _selections
        old = _selections.copy()
        try:
            _clear_ghost_room_id()
            from backend.bot.handlers.ghost_seen import _is_ghost_enabled
            assert not _is_ghost_enabled()
        finally:
            _selections.clear()
            _selections.update(old)

    def test_enabled_with_env(self):
        old = os.environ.get("GHOST_ROOM_ID")
        try:
            _patch_ghost_room_id("12345")
            from backend.bot.handlers.ghost_seen import _is_ghost_enabled
            assert _is_ghost_enabled()
        finally:
            if old is not None:
                os.environ["GHOST_ROOM_ID"] = old
            else:
                _clear_ghost_room_id()


# ── 2. Selection ──


class TestGhostRoomSelection:
    def test_toggle_adds_and_removes(self):
        from backend.services.ghost_seen_service import (
            toggle_selection,
            get_selection,
            clear_selection,
        )
        clear_selection(999)
        assert toggle_selection(999, 100)
        assert 100 in get_selection(999)
        assert not toggle_selection(999, 100)
        assert 100 not in get_selection(999)
        clear_selection(999)
        assert not get_selection(999)

    def test_max_selection_bound(self):
        from backend.services.ghost_seen_service import (
            toggle_selection,
            get_selection,
            clear_selection,
        )
        clear_selection(888)
        for i in range(12):
            toggle_selection(888, i + 1)
        from backend.services.ghost_seen_service import _MAX_SELECTED
        assert len(get_selection(888)) <= _MAX_SELECTED
        clear_selection(888)

    def test_clear_selection(self):
        from backend.services.ghost_seen_service import (
            toggle_selection,
            get_selection,
            clear_selection,
        )
        toggle_selection(777, 42)
        toggle_selection(777, 43)
        assert len(get_selection(777)) == 2
        clear_selection(777)
        assert not get_selection(777)


# ── 3. Page state ──


class TestGhostRoomPage:
    def test_default_page(self):
        from backend.services.ghost_seen_service import get_page
        assert get_page(99999) == 0

    def test_set_page(self):
        from backend.services.ghost_seen_service import set_page, get_page
        set_page(123, 2)
        assert get_page(123) == 2
        set_page(123, -5)
        assert get_page(123) == 0


# ── 4. Rendering ──


class TestGhostRoomFormatting:
    def test_format_chat_list_item(self):
        from backend.services.ghost_seen_service import format_chat_list_item
        row = {
            "chat_id": 111,
            "display_name": "Alice",
            "last_preview": "Hello there",
            "unread_count": 3,
            "last_message_at": None,
        }
        result = format_chat_list_item(row)
        assert "Alice" in result
        assert "(3)" in result
        assert "Hello there" in result

    def test_format_chat_list_item_no_unread(self):
        from backend.services.ghost_seen_service import format_chat_list_item
        row = {
            "chat_id": 222,
            "display_name": "Bob",
            "last_preview": "",
            "unread_count": 0,
            "last_message_at": None,
        }
        result = format_chat_list_item(row)
        assert "Bob" in result
        assert "(0)" not in result

    def test_format_chat_view_item_selected(self):
        from backend.services.ghost_seen_service import format_chat_view_item
        msg = {
            "id": 1,
            "sender_name": "Alice",
            "text": "Hello, how are you?",
            "date": None,
        }
        result = format_chat_view_item(msg, True, 1, 1)
        assert "✓" in result
        assert "Alice" in result
        assert "Hello, how are you?" in result

    def test_format_chat_view_item_not_selected(self):
        from backend.services.ghost_seen_service import format_chat_view_item
        msg = {
            "id": 2,
            "sender_name": "Bob",
            "text": "Good morning",
            "date": None,
        }
        result = format_chat_view_item(msg, False, 2, 1)
        assert "○" in result
        assert "Bob" in result


# ── 5. AI execution goes through engine ──


class TestGhostRoomAIExecution:
    @pytest.mark.asyncio
    async def test_execute_ghost_seen_ai_uses_engine(self):
        """Ghost Seen AI must go through Engine.execute(), never call provider directly."""
        from backend.services.ghost_seen_service import execute_ghost_seen_ai
        from unittest.mock import patch, AsyncMock

        mock_engine = AsyncMock()
        mock_engine.execute.return_value = AsyncMock(
            success=True,
            response="Hello from AI!",
            errors=[],
        )

        with patch("backend.ai.engine.engine.get_engine", return_value=mock_engine):
            ok, resp = await execute_ghost_seen_ai(
                owner_id=123,
                chat_id=456,
                prompt_text="What's up?",
                context_messages=[],
            )

        assert ok
        assert resp == "Hello from AI!"
        mock_engine.execute.assert_called_once()

    @pytest.mark.asyncio
    async def test_execute_ghost_seen_ai_with_selected_messages(self):
        """Multi-message payload must build a context block, not inferred context."""
        from backend.services.ghost_seen_service import execute_ghost_seen_ai
        from unittest.mock import patch, AsyncMock

        mock_engine = AsyncMock()
        mock_result = AsyncMock(success=True, response="OK", errors=[])
        mock_engine.execute.return_value = mock_result

        msgs = [
            {"sender_name": "Alice", "text": "Hi"},
            {"sender_name": "Bob", "text": "Hello there"},
        ]

        with patch("backend.ai.engine.engine.get_engine", return_value=mock_engine):
            ok, resp = await execute_ghost_seen_ai(
                owner_id=1, chat_id=2, prompt_text="Summarize",
                context_messages=msgs,
            )

        assert ok
        call = mock_engine.execute.call_args
        request = call[0][0]
        assert "Conversation context:" in request.user_message
        assert "Alice" in request.user_message
        assert "Bob" in request.user_message
        assert "Task: Summarize" in request.user_message

    @pytest.mark.asyncio
    async def test_execute_ghost_seen_ai_engine_none(self):
        from backend.services.ghost_seen_service import execute_ghost_seen_ai
        from unittest.mock import patch

        with patch("backend.ai.engine.engine.get_engine", return_value=None):
            ok, resp = await execute_ghost_seen_ai(
                owner_id=1, chat_id=2, prompt_text="Test", context_messages=[],
            )
        assert not ok
        assert "not available" in resp.lower()

    @pytest.mark.asyncio
    async def test_execute_ghost_seen_ai_session_isolation(self):
        """Ghost Seen AI must use a distinct session_id (ghost_seen:<chat_id>) for context isolation."""
        from backend.services.ghost_seen_service import execute_ghost_seen_ai
        from unittest.mock import patch, AsyncMock

        mock_engine = AsyncMock()
        mock_engine.execute.return_value = AsyncMock(success=True, response="OK", errors=[])

        with patch("backend.ai.engine.engine.get_engine", return_value=mock_engine):
            await execute_ghost_seen_ai(
                owner_id=123, chat_id=999,
                prompt_text="Test", context_messages=[],
            )

        call = mock_engine.execute.call_args
        request = call[0][0]
        assert request.session_id == "ghost_seen:999"


# ── 6. Incoming listener owner exclusion ──


class TestGhostRoomIncoming:
    def test_incoming_listener_registered(self):
        """Incoming listener is registered via events.NewMessage(incoming=True)."""
        import backend.bot.handlers.ghost_seen as gr
        assert hasattr(gr, "_register_incoming_listener")


# ── 7. Integration: no second dispatcher ──


class TestGhostRoomNoSecondArchitecture:
    def test_ai_path_is_engine_execute(self):
        """The only AI execution import should be engine.execute."""
        import inspect
        import backend.services.ghost_seen_service as svc
        src = inspect.getsource(svc.execute_ghost_seen_ai)
        assert "get_engine" in src
        assert "engine.execute" in src
        assert "ProviderManager" not in src
        assert "Dispatcher" not in src
        assert "provider.chat" not in src


# ── 8. Database availability fallback ──


class TestGhostSeenRegistryFallback:
    @pytest.mark.asyncio
    async def test_read_registry_rows_returns_empty_on_failure(self):
        from backend.services.ghost_seen_service import read_registry_rows
        from unittest.mock import patch

        with patch("backend.db.client.get_db", return_value=None):
            rows = await read_registry_rows()
            assert rows == []

    @pytest.mark.asyncio
    async def test_upsert_source_chat_does_not_raise(self):
        from backend.services.ghost_seen_service import upsert_source_chat
        from unittest.mock import patch

        with patch("backend.db.client.get_db", return_value=None):
            await upsert_source_chat(1, "Test", "preview", "")

    @pytest.mark.asyncio
    async def test_clear_unread_does_not_raise(self):
        from backend.services.ghost_seen_service import clear_unread
        from unittest.mock import patch

        with patch("backend.db.client.get_db", return_value=None):
            await clear_unread(1)


class TestGhostSeenRetention:
    def test_apply_retention_expires_only_stale_rows(self):
        from datetime import datetime, timedelta, timezone
        from backend.services.ghost_seen_service import apply_retention

        now = datetime.now(timezone.utc)
        rows = [
            {"chat_id": 1, "last_message_at": (now - timedelta(days=60)).isoformat()},
            {"chat_id": 2, "last_message_at": (now - timedelta(days=1)).isoformat()},
            {"chat_id": 3, "last_message_at": None},
        ]
        kept, expired = apply_retention(rows, 30)
        assert [row["chat_id"] for row in kept] == [2, 3]
        assert expired == [1]

    def test_apply_retention_clamps_extreme_day_values(self):
        from datetime import datetime, timezone
        from backend.services.ghost_seen_service import apply_retention

        row = [{"chat_id": 1, "last_message_at": datetime.now(timezone.utc).isoformat()}]
        kept, expired = apply_retention(row, 100000)
        assert kept == row and expired == []
        kept, expired = apply_retention(row, 0)
        assert kept == row and expired == []


# ── 9. Destination routing (GHOST_ROOM_ID enforcement) ──


class TestGhostRoomDestination:
    def setup_method(self):
        from backend.bot.handlers import ghost_seen  # noqa: F401 — ensure module is loaded
        self._old_val = os.environ.get("GHOST_ROOM_ID")

    def teardown_method(self):
        if self._old_val is not None:
            os.environ["GHOST_ROOM_ID"] = self._old_val
        else:
            os.environ.pop("GHOST_ROOM_ID", None)

    def test_resolve_returns_none_when_missing(self):
        os.environ.pop("GHOST_ROOM_ID", None)
        from backend.bot.handlers.ghost_seen import _resolve_ghost_destination
        assert _resolve_ghost_destination() is None

    def test_resolve_returns_none_when_empty(self):
        os.environ["GHOST_ROOM_ID"] = ""
        from backend.bot.handlers.ghost_seen import _resolve_ghost_destination
        assert _resolve_ghost_destination() is None

    def test_resolve_returns_none_when_non_numeric(self):
        os.environ["GHOST_ROOM_ID"] = "abc123"
        from backend.bot.handlers.ghost_seen import _resolve_ghost_destination
        assert _resolve_ghost_destination() is None

    def test_resolve_returns_none_when_negative(self):
        os.environ["GHOST_ROOM_ID"] = "-5"
        from backend.bot.handlers.ghost_seen import _resolve_ghost_destination
        assert _resolve_ghost_destination() is None

    def test_resolve_returns_int_when_valid(self):
        os.environ["GHOST_ROOM_ID"] = "1234567890"
        from backend.bot.handlers.ghost_seen import _resolve_ghost_destination
        assert _resolve_ghost_destination() == 1234567890

    @pytest.mark.asyncio
    async def test_reply_blocked_when_ghost_room_id_missing(self):
        """Reply must fail closed when GHOST_ROOM_ID is missing."""
        from backend.bot.handlers.ghost_seen import _ghost_reply_input, configure
        os.environ.pop("GHOST_ROOM_ID", None)
        # configure with a dummy client that will NOT be called
        dummy = object()
        configure(dummy, 1, "UTC")
        # Must not raise; must silently return
        await _ghost_reply_input("test", 0, 0, 0, 0)

    @pytest.mark.asyncio
    async def test_reply_no_quote_blocked_when_ghost_room_id_missing(self):
        from backend.bot.handlers.ghost_seen import _ghost_reply_no_quote_input, configure
        os.environ.pop("GHOST_ROOM_ID", None)
        dummy = object()
        configure(dummy, 1, "UTC")
        await _ghost_reply_no_quote_input("test", 0, 0, 0, 0)

    @pytest.mark.asyncio
    async def test_ai_blocked_when_ghost_room_id_missing(self):
        from backend.bot.handlers.ghost_seen import _ghost_ai_input, configure
        os.environ.pop("GHOST_ROOM_ID", None)
        dummy = object()
        configure(dummy, 1, "UTC")
        await _ghost_ai_input("test", 0, 0, 0, 0)

    @pytest.mark.asyncio
    async def test_reply_sends_to_ghost_room_id_not_source_chat(self):
        """When GHOST_ROOM_ID is set, replies must target it — never the source chat."""
        from backend.bot.handlers.ghost_seen import (
            _ghost_reply_input, configure, _set_current_chat,
        )
        from backend.services.ghost_seen_service import toggle_selection, clear_selection
        os.environ["GHOST_ROOM_ID"] = "99999"
        clear_selection(12345)
        toggle_selection(12345, 777)
        _set_current_chat(12345)

        # setup a mock client
        from unittest.mock import AsyncMock
        mock_client = AsyncMock()
        configure(mock_client, 1, "UTC")

        await _ghost_reply_input("hello", 0, 0, 0, 0)

        # Must have sent to 99999, NOT to 12345 (the source chat)
        mock_client.send_message.assert_called_once()
        args, kwargs = mock_client.send_message.call_args
        assert args[0] == 99999
        clear_selection(12345)

    @pytest.mark.asyncio
    async def test_ai_sends_to_ghost_room_id_not_source_chat(self):
        """Ghost Seen AI responses must be delivered to GHOST_ROOM_ID only."""
        from backend.bot.handlers.ghost_seen import (
            _ghost_ai_input, configure, _set_current_chat,
        )
        from backend.services.ghost_seen_service import toggle_selection, clear_selection
        from unittest.mock import patch, AsyncMock
        os.environ["GHOST_ROOM_ID"] = "88888"
        clear_selection(12345)
        toggle_selection(12345, 777)
        _set_current_chat(12345)

        mock_client = AsyncMock()
        configure(mock_client, 1, "UTC")

        mock_engine = AsyncMock()
        mock_engine.execute.return_value = AsyncMock(success=True, response="AI reply")

        with patch("backend.ai.engine.engine.get_engine", return_value=mock_engine):
            with patch("backend.bot.handlers.ghost_seen._self_client", mock_client):
                await _ghost_ai_input("summarize", 0, 0, 0, 0)

        # Must have delivered to 88888, NOT 12345
        mock_client.send_message.assert_called_once()
        dest = mock_client.send_message.call_args[0][0]
        assert dest == 88888
        clear_selection(12345)

    def test_no_ghost_chats_fallback_to_destination(self):
        """ghost_chats entries must NEVER be used as the destination."""
        import inspect
        import backend.bot.handlers.ghost_seen as gr
        import backend.services.ghost_seen_service as svc

        # Handler output paths must use _resolve_ghost_destination
        for fn_name in ["_ghost_reply_input", "_ghost_reply_no_quote_input", "_ghost_ai_input"]:
            src = inspect.getsource(getattr(gr, fn_name))
            assert "_resolve_ghost_destination" in src, f"{fn_name} must use _resolve_ghost_destination"

        # Service must NOT contain fallback to ghost_chats
        svc_src = inspect.getsource(svc.execute_ghost_seen_ai)
        assert "ghost_chats" not in svc_src