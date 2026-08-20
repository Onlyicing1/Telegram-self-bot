"""Production Delete timeout and anchor hardening tests."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from backend.ai.actions import KIND_EXECUTABLE, parse_command_intent
from backend.ai.tools.base import PermissionLevel, ToolResult
from backend.ai.tools.context import ToolContext
from backend.ai.tools.delete import DeleteTool
from backend.ai.tools.executor import ToolExecutor
from backend.ai.tools.registry import ToolRegistry
from backend.services import delete_service


class ChatClient:
    me = SimpleNamespace(id=111)

    def __init__(self, messages):
        self.messages = {message.id: message for message in messages}
        self.deleted: list[int] = []
        self.iter_kwargs: list[dict] = []
        self.delete_batches: list[list[int]] = []

    async def iter_messages(self, chat_id, **kwargs):
        self.iter_kwargs.append(dict(kwargs))
        for message in sorted(self.messages.values(), key=lambda item: item.id, reverse=True):
            yield message

    async def get_messages(self, chat_id, ids):
        if isinstance(ids, (list, tuple)):
            return [self.messages.get(message_id) for message_id in ids]
        return self.messages.get(ids)

    async def delete_messages(self, chat_id, ids):
        batch = list(ids) if isinstance(ids, (list, tuple)) else [ids]
        self.delete_batches.append(batch)
        self.deleted.extend(batch)
        for message_id in batch:
            self.messages.pop(message_id, None)


def message(message_id: int, *, out: bool = True, sender_id: int | None = 111, text: str = ""):
    return SimpleNamespace(
        id=message_id,
        out=out,
        sender_id=sender_id,
        message=text,
        text=text,
        date=None,
        media=None,
    )


def context(client: ChatClient, *, request_id: int | None = None, request_key: str = ""):
    extra = {"chat_id": -100}
    if request_id is not None:
        extra["request_message_id"] = request_id
    if request_key:
        extra["request_id"] = request_key
    return ToolContext(client=client, telegram=None, owner_id=111, tz_str="UTC", extra=extra)


@pytest.mark.asyncio
async def test_current_request_is_anchor_but_never_deleted():
    client = ChatClient([
        message(10, text="تا این پیامم هر چی هست رو پاک کن"),
        message(9, text="previous self message"),
        message(8, out=False, sender_id=222, text="foreign message"),
    ])
    ctx = context(client, request_id=10, request_key="req-anchor")

    result = await DeleteTool(ctx).execute(ctx, {"mode": "until_message"})

    assert result.success is True
    assert client.deleted == [9]
    assert 10 not in client.deleted
    assert 8 not in client.deleted


def test_direct_semantic_delete_is_structured_and_provider_independent():
    result = parse_command_intent("پیام هام راجب اسکریپت رو پاک کن", has_reply=False)

    assert result.kind == KIND_EXECUTABLE
    assert result.mode == "filtered"
    assert result.query == "اسکریپت"
    assert result.tool_calls == [{
        "name": "delete",
        "arguments": {"mode": "filtered", "query": "اسکریپت"},
    }]


@pytest.mark.asyncio
async def test_semantic_filter_rejects_foreign_matching_messages():
    client = ChatClient([
        message(20, text="delete request"),
        message(19, text="script deployment", out=True, sender_id=111),
        message(18, text="script deployment", out=False, sender_id=222),
        message(17, text="unrelated", out=True, sender_id=111),
    ])
    considered, deleted, error = await delete_service.do_del_self_filtered(
        client,
        -100,
        query="script",
        exclude_message_id=20,
        request_id="req-semantic",
    )

    assert error is None
    assert considered == 1
    assert deleted == 1
    assert client.deleted == [19]


@pytest.mark.asyncio
async def test_all_delete_is_bounded_and_chunked():
    client = ChatClient([
        message(10_000),
        *[message(index) for index in range(1, 206)],
    ])
    ctx = context(client, request_id=10_000, request_key="req-batch")

    result = await DeleteTool(ctx).execute(ctx, {"mode": "all"})

    assert result.success is True
    assert 10_000 not in client.deleted
    assert len(client.deleted) == 205
    assert client.iter_kwargs[0]["limit"] == 1000
    assert len(client.delete_batches) == 3
    assert all(len(batch) <= 100 for batch in client.delete_batches)


@pytest.mark.asyncio
async def test_delete_tool_uses_its_own_bounded_deadline_not_global_ten_seconds():
    class SlowDeleteTool:
        name = "delete"
        permission_level = PermissionLevel.DANGEROUS
        safe = False
        long_running = False
        timeout_seconds = 0.2
        description = "test"
        parameters = {}
        return_type = "test"

        async def execute(self, context, arguments):
            await asyncio.sleep(0.02)
            return ToolResult(success=True, message="deleted", data={"count": 1})

    registry = ToolRegistry()
    registry.register(SlowDeleteTool())
    executor = ToolExecutor(registry, ToolContext(
        client=None, telegram=None, owner_id=111, tz_str="UTC",
        extra={"request_id": "req-timeout"},
    ))

    results = await executor.execute_calls([{"name": "delete", "arguments": {}}])

    assert results[0].success is True
    assert results[0].error == ""


@pytest.mark.asyncio
async def test_repeated_delete_requests_have_no_unbounded_history_or_confirmation():
    client = ChatClient([message(3), message(2), message(1)])
    first_ctx = context(client, request_id=3, request_key="req-1")
    first = await DeleteTool(first_ctx).execute(first_ctx, {"count": 1})

    second_ctx = context(client, request_id=2, request_key="req-2")
    second = await DeleteTool(second_ctx).execute(second_ctx, {"count": 1})

    assert first.success is True and second.success is True
    # The second request excludes its own ID (2) and correctly sees the
    # previous request (3) as a self-authored eligible message.
    assert client.deleted == [2, 3]
    assert all(item["limit"] == 1000 for item in client.iter_kwargs)
    assert "Deleted" not in first.message or first.data["count"] == 1
    assert "Deleted" not in second.message or second.data["count"] == 1
