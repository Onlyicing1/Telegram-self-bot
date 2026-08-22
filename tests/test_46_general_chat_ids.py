"""Tests for General → Chat & Message IDs utility (Execution 24)."""

import os
import pytest


class TestGeneralIDAction:
    """Unit tests for the expanded _general_id_action handler."""

    @pytest.fixture(autouse=True)
    def _patch_env(self):
        old = os.environ.get("GHOST_ROOM_ID")
        yield
        if old is not None:
            os.environ["GHOST_ROOM_ID"] = old
        else:
            os.environ.pop("GHOST_ROOM_ID", None)

    @pytest.mark.asyncio
    async def test_basic_chat_and_message_ids_rendered(self):
        """Chat ID and message ID are both shown."""
        from unittest.mock import AsyncMock
        from backend.bot.handlers.misc import _general_id_action

        event = AsyncMock()
        event.chat_id = 12345
        event.message_id = 67890
        event._reply_to_msg_id = None
        event.message = None
        event.original_update = None

        title, body, _ = await _general_id_action(event, "", 12345)

        assert "Chat & Message IDs" in title
        assert "Chat ID:" in body
        assert "12345" in body
        assert "Message ID:" in body
        assert "67890" in body

    @pytest.mark.asyncio
    async def test_chat_id_from_action_parameter(self):
        """chat_id from action dispatch takes priority."""
        from unittest.mock import AsyncMock
        from backend.bot.handlers.misc import _general_id_action

        event = AsyncMock()
        event.chat_id = 0
        event.message_id = 11111
        event._reply_to_msg_id = None
        event.message = None
        event.original_update = None

        title, body, _ = await _general_id_action(event, "", 99999)

        assert "99999" in body

    @pytest.mark.asyncio
    async def test_no_reply_context_shown_honestly(self):
        """When there is no reply, 'No reply context.' is shown."""
        from unittest.mock import AsyncMock
        from backend.bot.handlers.misc import _general_id_action

        event = AsyncMock()
        event.chat_id = 1
        event.message_id = 2
        event._reply_to_msg_id = None
        event.message = None
        event.original_update = None

        _, body, _ = await _general_id_action(event, "", 1)

        assert "No reply context." in body

    @pytest.mark.asyncio
    async def test_reply_context_via_reply_to_msg_id(self):
        """Reply context is rendered when _reply_to_msg_id is set on event."""
        from unittest.mock import AsyncMock, patch
        from backend.bot.handlers.misc import _general_id_action

        event = AsyncMock()
        event.chat_id = 100
        event.message_id = 200
        event._reply_to_msg_id = 300
        event.message = None
        event.original_update = None

        mock_client = AsyncMock()
        mock_reply = AsyncMock()
        mock_reply.chat_id = 100
        mock_reply.sender_id = 555
        mock_reply.text = "hello"
        mock_reply.fwd_from = None
        mock_client.get_messages.return_value = mock_reply

        with patch("backend.helper.inline_engine._self_client", mock_client):
            _, body, _ = await _general_id_action(event, "", 100)

        assert "Reply To Msg ID:" in body
        assert "300" in body
        assert "Reply Chat ID:" in body
        assert "Reply Sender ID:" in body

    @pytest.mark.asyncio
    async def test_missing_message_id_displays_unavailable(self):
        """Message ID 0 renders as Unavailable."""
        from unittest.mock import AsyncMock
        from backend.bot.handlers.misc import _general_id_action

        event = AsyncMock()
        event.chat_id = 10
        event.message_id = 0
        event._reply_to_msg_id = None
        event.message = None
        event.original_update = None

        _, body, _ = await _general_id_action(event, "", 10)

        assert "Unavailable" in body

    @pytest.mark.asyncio
    async def test_chat_type_section_present(self):
        """The body includes a 'Current Chat' section."""
        from unittest.mock import AsyncMock
        from backend.bot.handlers.misc import _general_id_action

        event = AsyncMock()
        event.chat_id = 50
        event.message_id = 60
        event._reply_to_msg_id = None
        event.message = None
        event.original_update = None

        _, body, _ = await _general_id_action(event, "", 50)

        assert "Current Chat" in body
        assert "Type:" in body

    @pytest.mark.asyncio
    async def test_current_message_section_present(self):
        """The body includes a 'Current Message' section."""
        from unittest.mock import AsyncMock
        from backend.bot.handlers.misc import _general_id_action

        event = AsyncMock()
        event.chat_id = 70
        event.message_id = 80
        event._reply_to_msg_id = None
        event.message = None
        event.original_update = None

        _, body, _ = await _general_id_action(event, "", 70)

        assert "Current Message" in body

    @pytest.mark.asyncio
    async def test_reply_context_section_present(self):
        """The body includes a 'Reply Context' section."""
        from unittest.mock import AsyncMock
        from backend.bot.handlers.misc import _general_id_action

        event = AsyncMock()
        event.chat_id = 1
        event.message_id = 2
        event._reply_to_msg_id = None
        event.message = None
        event.original_update = None

        _, body, _ = await _general_id_action(event, "", 1)

        assert "Reply Context" in body

    @pytest.mark.asyncio
    async def test_no_ghost_room_routing_changed(self):
        """GHOST_ROOM_ID env is not referenced by the ID utility."""
        import inspect
        from backend.bot.handlers import misc

        src = inspect.getsource(misc._general_id_action)
        assert "GHOST_ROOM_ID" not in src

    @pytest.mark.asyncio
    async def test_existing_general_options_still_registered(self):
        """Ping, Chat & Msg IDs, and Health Dashboard actions remain."""
        from backend.bot.handlers.misc import _build_general_buttons
        buttons = _build_general_buttons()
        data_values = []
        for row in buttons:
            for btn in row:
                if hasattr(btn, "data"):
                    val = btn.data
                    if isinstance(val, bytes):
                        val = val.decode("utf-8", errors="replace")
                    data_values.append(val)
                elif len(btn) >= 1:
                    data_values.append(str(btn[0]))

        assert any("general_ping" in v for v in data_values)
        assert any("general_id" in v for v in data_values)
        assert any("general_health" in v for v in data_values)

    @pytest.mark.asyncio
    async def test_no_duplicate_handler_registration(self):
        """Only one action:general_id is registered."""
        import inspect
        from backend.bot.handlers import misc

        src = inspect.getsource(misc._register_panels)
        count = src.count('"general_id"')
        assert count == 1

    @pytest.mark.asyncio
    async def test_ids_not_truncated_or_converted(self):
        """Large IDs are rendered verbatim, not truncated."""
        from unittest.mock import AsyncMock
        from backend.bot.handlers.misc import _general_id_action

        big_id = 1234567890123456789
        event = AsyncMock()
        event.chat_id = big_id
        event.message_id = big_id + 1
        event._reply_to_msg_id = None
        event.message = None
        event.original_update = None

        _, body, _ = await _general_id_action(event, "", big_id)

        assert str(big_id) in body
        assert str(big_id + 1) in body

    @pytest.mark.asyncio
    async def test_reply_context_from_message_object(self):
        """Reply context resolved from event.message.reply_to_msg_id."""
        from unittest.mock import AsyncMock, patch
        from backend.bot.handlers.misc import _general_id_action

        cq_msg = AsyncMock()
        cq_msg.reply_to_msg_id = 999

        event = AsyncMock()
        event.chat_id = 111
        event.message_id = 222
        event._reply_to_msg_id = None
        event.message = cq_msg
        event.original_update = None

        mock_client = AsyncMock()
        mock_reply = AsyncMock()
        mock_reply.chat_id = 111
        mock_reply.sender_id = 333
        mock_reply.text = "ping"
        mock_reply.fwd_from = None
        mock_client.get_messages.return_value = mock_reply

        with patch("backend.helper.inline_engine._self_client", mock_client):
            _, body, _ = await _general_id_action(event, "", 111)

        assert "Reply To Msg ID:" in body
        assert "999" in body