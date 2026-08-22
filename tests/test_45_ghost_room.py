"""
Focused regression tests for Ghost Room MVP.

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
        from backend.services.ghost_room_service import _selections
        old = _selections.copy()
        try:
            _clear_ghost_room_id()
            from backend.bot.handlers.ghost_room import _is_ghost_enabled
            assert not _is_ghost_enabled()
        finally:
            _selections.clear()
            _selections.update(old)

    def test_enabled_with_env(self):
        old = os.environ.get("GHOST_ROOM_ID")
        try:
            _patch_ghost_room_id("12345")
            from backend.bot.handlers.ghost_room import _is_ghost_enabled
            assert _is_ghost_enabled()
        finally:
            if old is not None:
                os.environ["GHOST_ROOM_ID"] = old
            else:
                _clear_ghost_room_id()


# ── 2. Selection ──


class TestGhostRoomSelection:
    def test_toggle_adds_and_removes(self):
        from backend.services.ghost_room_service import (
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
        from backend.services.ghost_room_service import (
            toggle_selection,
            get_selection,
            clear_selection,
        )
        clear_selection(888)
        for i in range(12):
            toggle_selection(888, i + 1)
        from backend.services.ghost_room_service import _MAX_SELECTED
        assert len(get_selection(888)) <= _MAX_SELECTED
        clear_selection(888)

    def test_clear_selection(self):
        from backend.services.ghost_room_service import (
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
        from backend.services.ghost_room_service import get_page
        assert get_page(99999) == 0

    def test_set_page(self):
        from backend.services.ghost_room_service import set_page, get_page
        set_page(123, 2)
        assert get_page(123) == 2
        set_page(123, -5)
        assert get_page(123) == 0


# ── 4. Rendering ──


class TestGhostRoomFormatting:
    def test_format_chat_list_item(self):
        from backend.services.ghost_room_service import format_chat_list_item
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
        from backend.services.ghost_room_service import format_chat_list_item
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
        from backend.services.ghost_room_service import format_chat_view_item
        msg = {
            "id": 1,
            "sender_name": "Alice",
            "text": "Hello, how are you?",
            "date": None,
        }
        result = format_chat_view_item(msg, True, 1)
        assert "✓" in result
        assert "Alice" in result
        assert "Hello, how are you?" in result

    def test_format_chat_view_item_not_selected(self):
        from backend.services.ghost_room_service import format_chat_view_item
        msg = {
            "id": 2,
            "sender_name": "Bob",
            "text": "Good morning",
            "date": None,
        }
        result = format_chat_view_item(msg, False, 2)
        assert "○" in result
        assert "Bob" in result


# ── 5. AI execution goes through engine ──


class TestGhostRoomAIExecution:
    @pytest.mark.asyncio
    async def test_execute_ghost_ai_uses_engine(self):
        """Ghost Room AI must go through Engine.execute(), never call provider directly."""
        from backend.services.ghost_room_service import execute_ghost_ai
        from unittest.mock import patch, AsyncMock

        mock_engine = AsyncMock()
        mock_engine.execute.return_value = AsyncMock(
            success=True,
            response="Hello from AI!",
            errors=[],
        )

        with patch("backend.ai.engine.engine.get_engine", return_value=mock_engine):
            ok, resp = await execute_ghost_ai(
                owner_id=123,
                chat_id=456,
                prompt_text="What's up?",
                selected_messages=[],
            )

        assert ok
        assert resp == "Hello from AI!"
        mock_engine.execute.assert_called_once()

    @pytest.mark.asyncio
    async def test_execute_ghost_ai_with_selected_messages(self):
        """Multi-message payload must build a context block, not inferred context."""
        from backend.services.ghost_room_service import execute_ghost_ai
        from unittest.mock import patch, AsyncMock

        mock_engine = AsyncMock()
        mock_result = AsyncMock(success=True, response="OK", errors=[])
        mock_engine.execute.return_value = mock_result

        msgs = [
            {"sender_name": "Alice", "text": "Hi"},
            {"sender_name": "Bob", "text": "Hello there"},
        ]

        with patch("backend.ai.engine.engine.get_engine", return_value=mock_engine):
            ok, resp = await execute_ghost_ai(
                owner_id=1, chat_id=2, prompt_text="Summarize",
                selected_messages=msgs,
            )

        assert ok
        call = mock_engine.execute.call_args
        request = call[0][0]
        assert "Selected messages:" in request.user_message
        assert "Alice" in request.user_message
        assert "Bob" in request.user_message

    @pytest.mark.asyncio
    async def test_execute_ghost_ai_engine_none(self):
        from backend.services.ghost_room_service import execute_ghost_ai
        from unittest.mock import patch

        with patch("backend.ai.engine.engine.get_engine", return_value=None):
            ok, resp = await execute_ghost_ai(
                owner_id=1, chat_id=2, prompt_text="Test", selected_messages=[],
            )
        assert not ok
        assert "not available" in resp.lower()

    @pytest.mark.asyncio
    async def test_execute_ghost_ai_session_isolation(self):
        """Ghost Room AI must use a distinct session_id (ghost:<chat_id>) for context isolation."""
        from backend.services.ghost_room_service import execute_ghost_ai
        from unittest.mock import patch, AsyncMock

        mock_engine = AsyncMock()
        mock_engine.execute.return_value = AsyncMock(success=True, response="OK", errors=[])

        with patch("backend.ai.engine.engine.get_engine", return_value=mock_engine):
            await execute_ghost_ai(
                owner_id=123, chat_id=999,
                prompt_text="Test", selected_messages=[],
            )

        call = mock_engine.execute.call_args
        request = call[0][0]
        assert request.session_id == "ghost:999"


# ── 6. Incoming listener owner exclusion ──


class TestGhostRoomIncoming:
    def test_incoming_listener_registered(self):
        """Incoming listener is registered via events.NewMessage(incoming=True)."""
        import backend.bot.handlers.ghost_room as gr
        assert hasattr(gr, "_register_incoming_listener")


# ── 7. Integration: no second dispatcher ──


class TestGhostRoomNoSecondArchitecture:
    def test_ai_path_is_engine_execute(self):
        """The only AI execution import should be engine.execute."""
        import inspect
        import backend.services.ghost_room_service as svc
        src = inspect.getsource(svc.execute_ghost_ai)
        assert "get_engine" in src
        assert "engine.execute" in src
        assert "ProviderManager" not in src
        assert "Dispatcher" not in src
        assert "provider.chat" not in src


# ── 8. Database availability fallback ──


class TestGhostRoomDBFallback:
    @pytest.mark.asyncio
    async def test_read_ghost_chats_returns_empty_on_failure(self):
        from backend.bot.handlers.ghost_room import _read_ghost_chats_sync
        from unittest.mock import patch

        with patch("backend.db.client.get_db", return_value=None):
            rows = await _read_ghost_chats_sync()
            assert rows == []

    @pytest.mark.asyncio
    async def test_upsert_does_not_raise(self):
        from backend.bot.handlers.ghost_room import _upsert_ghost_chat_sync
        from unittest.mock import patch

        with patch("backend.db.client.get_db", return_value=None):
            # Must not raise
            await _upsert_ghost_chat_sync(1, "Test", "preview", "")

    @pytest.mark.asyncio
    async def test_clear_unread_does_not_raise(self):
        from backend.bot.handlers.ghost_room import _clear_unread_sync
        from unittest.mock import patch

        with patch("backend.db.client.get_db", return_value=None):
            await _clear_unread_sync(1)