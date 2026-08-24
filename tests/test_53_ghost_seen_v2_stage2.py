from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from backend.services.ghost_seen_v2 import (
    MESSAGE_PAGE_SIZE,
    MessageViewerPage,
    ViewerMessage,
    load_viewer_messages,
    render_message_viewer,
)


class FakeClient:
    def __init__(self, messages):
        self.messages = messages
        self.requested = None

    async def iter_messages(self, chat_id, limit):
        self.requested = (chat_id, limit)
        for message in self.messages:
            yield message


@pytest.mark.asyncio
async def test_viewer_uses_selected_source_and_bounds_messages():
    client = FakeClient([SimpleNamespace(id=i, message=f"message {i}") for i in range(1, 30)])
    viewer = await load_viewer_messages(client, 12345, 1)
    assert client.requested == (12345, MESSAGE_PAGE_SIZE * 20)
    assert viewer.source_chat_id == 12345
    assert len(viewer.messages) == MESSAGE_PAGE_SIZE
    assert viewer.total_pages > 1
    assert all(item.source_chat_id == 12345 for item in viewer.messages)


@pytest.mark.asyncio
async def test_viewer_empty_state_does_not_fake_messages_or_use_panel_chat():
    client = FakeClient([])
    viewer = await load_viewer_messages(client, 777, 1)
    assert client.requested[0] == 777
    assert viewer.messages == ()
    assert "nothing to see" in render_message_viewer("Ali", viewer).lower()


def test_viewer_truncates_long_text_and_handles_media_or_empty_messages():
    messages = (
        ViewerMessage(1, 4, "x" * 180, datetime.now(timezone.utc)),
        ViewerMessage(2, 4, "Media", datetime.now(timezone.utc)),
        ViewerMessage(3, 4, "Unsupported message", None),
    )
    body = render_message_viewer("Ali", MessageViewerPage(4, messages, 1, 1))
    assert len(body) < 900
    assert "Unsupported message" in body


def test_viewer_has_no_ai_or_refresh_ui():
    body = render_message_viewer("Ali", MessageViewerPage(4, (), 1, 1))
    assert "AI" not in body
    assert "Reply" not in body
    assert "Refresh" not in body
    assert "Ghost Seen" in body
