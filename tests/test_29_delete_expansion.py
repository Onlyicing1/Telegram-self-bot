"""Focused coverage for expanded self-owned Delete candidate selection."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from backend.ai.actions import KIND_EXECUTABLE, parse_command_intent, parse_action_text
from backend.ai.tools.context import ToolContext
from backend.ai.tools.delete import DeleteTool
from backend.ai.tools.semantic import DeleteMessagesByIdsTool, ListRecentMessagesTool
from backend.services import delete_service


class Message:
    def __init__(
        self,
        mid: int,
        *,
        out: bool,
        sender_id: int | None,
        text: str = "",
        date: datetime | None = None,
    ) -> None:
        self.id = mid
        self.out = out
        self.sender_id = sender_id
        self.message = text
        self.text = text
        self.date = date
        self.media = None


class Client:
    me = type("Me", (), {"id": 111})()

    def __init__(self, messages: list[Message]) -> None:
        self.messages = {message.id: message for message in messages}
        self.deleted: list[int] = []

    async def iter_messages(self, chat_id, **kwargs):
        for message in sorted(self.messages.values(), key=lambda item: item.id, reverse=True):
            yield message

    async def get_messages(self, chat_id, ids):
        if isinstance(ids, (list, tuple)):
            return [self.messages.get(mid) for mid in ids]
        return self.messages.get(ids)

    async def delete_messages(self, chat_id, ids):
        self.deleted.extend(ids if isinstance(ids, (list, tuple)) else [ids])


def context(client: Client, *, request_id: int | None = None, reply_id: int | None = None) -> ToolContext:
    extra = {"chat_id": -100}
    if request_id is not None:
        extra["request_message_id"] = request_id
    if reply_id is not None:
        extra["reply_msg"] = {"chat_id": -100, "message_id": reply_id}
    return ToolContext(telegram=type("TG", (), {"client": client})(), owner_id=111, tz_str="UTC", extra=extra)


@pytest.mark.asyncio
async def test_last_self_message_includes_current_request_when_in_scope():
    client = Client([
        Message(30, out=True, sender_id=111),  # active Delete request
        Message(29, out=False, sender_id=222),
        Message(28, out=True, sender_id=111),
    ])
    result = await DeleteTool(context(client, request_id=30)).execute(
        context(client, request_id=30), {"count": 1}
    )
    assert result.success is True
    assert client.deleted == [30]


@pytest.mark.asyncio
async def test_nova_message_is_self_owned_and_counted():
    client = Client([
        Message(40, out=True, sender_id=111),  # current request
        Message(39, out=True, sender_id=111),  # Nova response
        Message(38, out=False, sender_id=222),
        Message(37, out=True, sender_id=111),
    ])
    result = await DeleteTool(context(client, request_id=40)).execute(
        context(client, request_id=40), {"count": 2}
    )
    assert result.success is True
    assert client.deleted == [40, 39]


@pytest.mark.asyncio
async def test_delete_all_self_owned_excludes_other_users_and_request():
    client = Client([
        Message(50, out=True, sender_id=111),
        Message(49, out=False, sender_id=222),
        Message(48, out=True, sender_id=111),
        Message(47, out=False, sender_id=222),
    ])
    result = await DeleteTool(context(client, request_id=50)).execute(
        context(client, request_id=50), {"mode": "all"}
    )
    assert result.success is True
    assert client.deleted == [50, 48]


@pytest.mark.asyncio
async def test_until_time_filters_before_ownership_and_excludes_request():
    date = lambda hour: datetime(2026, 8, 20, hour, tzinfo=timezone.utc)
    client = Client([
        Message(60, out=True, sender_id=111, date=date(19)),  # current request
        Message(59, out=True, sender_id=111, date=date(18)),
        Message(58, out=False, sender_id=222, date=date(17)),
        Message(57, out=True, sender_id=111, date=date(16)),
        Message(56, out=True, sender_id=111, date=date(12)),
    ])
    result = await DeleteTool(context(client, request_id=60)).execute(
        context(client, request_id=60),
        {"mode": "until_time", "until_time": "2026-08-20T17:00:00+00:00"},
    )
    assert result.success is True
    assert client.deleted == [57, 56]


@pytest.mark.asyncio
async def test_foreign_boundary_is_reference_only():
    client = Client([
        Message(70, out=True, sender_id=111),  # current request
        Message(69, out=True, sender_id=111),  # newer than boundary: excluded by scope
        Message(68, out=False, sender_id=222),  # replied-to boundary
        Message(67, out=True, sender_id=111),  # older self message: eligible
    ])
    result = await DeleteTool(context(client, request_id=70, reply_id=68)).execute(
        context(client, request_id=70, reply_id=68), {"mode": "until_message"}
    )
    assert result.success is True
    assert client.deleted == [67]


@pytest.mark.asyncio
async def test_semantic_query_and_time_filter_remain_self_only():
    date = lambda hour: datetime(2026, 8, 20, hour, tzinfo=timezone.utc)
    client = Client([
        Message(80, out=True, sender_id=111, text="delete request", date=date(19)),
        Message(79, out=True, sender_id=111, text="argument details", date=date(16)),
        Message(78, out=False, sender_id=222, text="argument details", date=date(16)),
        Message(77, out=True, sender_id=111, text="argument details", date=date(12)),
        Message(76, out=True, sender_id=111, text="unrelated", date=date(12)),
    ])
    considered, deleted, error = await delete_service.do_del_self_filtered(
        client,
        -100,
        until_time="2026-08-20T17:00:00+00:00",
        query="argument",
        exclude_message_id=80,
    )
    assert error is None
    assert considered == 2
    assert deleted == 2
    assert client.deleted == [79, 77]


@pytest.mark.asyncio
async def test_n_plus_semantic_filter_selects_only_latest_matching_self():
    client = Client([
        Message(90, out=True, sender_id=111),
        Message(89, out=True, sender_id=111, text="topic"),
        Message(88, out=False, sender_id=222, text="topic"),
        Message(87, out=True, sender_id=111, text="topic"),
    ])
    considered, deleted, error = await delete_service.do_del_self_filtered(
        client, -100, count=1, query="topic", exclude_message_id=90
    )
    assert error is None
    assert considered == 1
    assert deleted == 1
    assert client.deleted == [89]


@pytest.mark.asyncio
async def test_semantic_tool_can_delete_current_request_when_selected():
    client = Client([
        Message(100, out=True, sender_id=111),
        Message(99, out=True, sender_id=111),
        Message(98, out=False, sender_id=222),
    ])
    result = await DeleteMessagesByIdsTool(context(client, request_id=100)).execute(
        context(client, request_id=100), {"message_ids": [100, 99, 98]}
    )
    assert result.success is True
    assert result.data["deleted"] == [100, 99]
    assert client.deleted == [100, 99]


@pytest.mark.asyncio
async def test_semantic_history_includes_current_request():
    client = Client([
        Message(110, out=True, sender_id=111, text="delete all"),
        Message(109, out=True, sender_id=111, text="keep"),
    ])
    result = await ListRecentMessagesTool(context(client, request_id=110)).execute(
        context(client, request_id=110), {"limit": 50}
    )
    assert result.success is True
    assert [item["id"] for item in result.data["messages"]] == [109, 110]


def test_expanded_persian_and_english_intents_resolve_to_delete_scopes():
    cases = [
        ("همه پیام‌های خودم رو پاک کن", "all"),
        ("delete all my messages", "all"),
        ("تا ساعت ۶ همه پیام‌هام رو پاک کن", "until_time"),
        ("delete my messages until 18", "until_time"),
    ]
    for text, mode in cases:
        result = parse_command_intent(text, has_reply=False)
        assert result.kind == KIND_EXECUTABLE
        assert result.mode == mode
        assert result.tool_calls[0]["arguments"]["mode"] == mode


def test_boundary_intent_uses_existing_reply_boundary_tool():
    result = parse_command_intent("تا این پیام هرچی پیام خودم هست پاک کن", has_reply=True)
    assert result.kind == KIND_EXECUTABLE
    assert result.mode == "until_message"
    assert result.tool_calls == [{"name": "delete", "arguments": {"mode": "until_message"}}]


def test_structured_delete_scope_is_validated_and_exposed():
    result = parse_action_text(
        '{"action":"delete_messages","target":"recent_messages",'
        '"mode":"until_time","until_time":"18:00","count":2}'
    )
    assert result.kind == KIND_EXECUTABLE
    assert result.tool_calls == [{
        "name": "delete",
        "arguments": {"count": 2, "mode": "until_time", "until_time": "18:00"},
    }]


@pytest.mark.asyncio
async def test_unknown_selection_identity_fails_closed():
    client = Client([Message(120, out=True, sender_id=None)])
    client.me = None
    considered, deleted, error = await delete_service.do_del_self_filtered(
        client, -100, count=1, exclude_message_id=None
    )
    assert considered == 0
    assert deleted == 0
    assert error is not None
    assert client.deleted == []
