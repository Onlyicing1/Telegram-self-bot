"""Delete RPC timeout and partial-failure guarantees."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from backend.ai.tools.context import ToolContext
from backend.ai.tools.delete import DeleteTool
from backend.services import delete_service


def msg(mid: int, *, out: bool = True, sender_id: int | None = 111, date=None):
    return SimpleNamespace(
        id=mid,
        out=out,
        sender_id=sender_id,
        date=date,
        message="",
        text="",
    )


class SlowHistoryClient:
    me = SimpleNamespace(id=111)

    def __init__(self):
        self.deleted: list[int] = []

    async def iter_messages(self, chat_id, **kwargs):
        await asyncio.sleep(0.1)
        yield msg(1)

    async def get_messages(self, chat_id, ids):
        return [msg(mid) for mid in ids] if isinstance(ids, list) else msg(ids)

    async def delete_messages(self, chat_id, ids):
        self.deleted.extend(ids)


def ctx(client, *, request_id: int | None = None):
    extra = {"chat_id": -100}
    if request_id is not None:
        extra["request_message_id"] = request_id
    return ToolContext(client=client, telegram=None, owner_id=111, tz_str="UTC", extra=extra)


@pytest.mark.asyncio
async def test_history_rpc_timeout_returns_before_tool_executor_deadline(monkeypatch):
    monkeypatch.setattr(delete_service, "_DELETE_RPC_TIMEOUT_SECONDS", 0.01)
    client = SlowHistoryClient()
    context = ctx(client, request_id=1)

    result = await DeleteTool(context).execute(context, {"mode": "all"})

    assert result.success is False
    assert "timed out" in result.message.lower()
    assert client.deleted == []


class PartialDeleteClient:
    me = SimpleNamespace(id=111)

    def __init__(self, count: int = 205):
        self.messages = {mid: msg(mid) for mid in range(1, count + 1)}
        self.batches: list[list[int]] = []
        self.deleted: list[int] = []

    async def iter_messages(self, chat_id, **kwargs):
        for item in sorted(self.messages.values(), key=lambda value: value.id, reverse=True):
            yield item

    async def get_messages(self, chat_id, ids):
        return [self.messages.get(mid) for mid in ids]

    async def delete_messages(self, chat_id, ids):
        batch = list(ids)
        self.batches.append(batch)
        if len(self.batches) == 2:
            raise RuntimeError("temporary Telegram delete failure")
        self.deleted.extend(batch)


@pytest.mark.asyncio
async def test_partial_batch_failure_is_bounded_and_attempts_each_verified_batch():
    client = PartialDeleteClient()

    with pytest.raises(RuntimeError, match="Telegram delete failed"):
        await delete_service.delete_verified_self_messages(
            client, -100, list(client.messages), request_id="partial-batch"
        )

    assert len(client.batches) == 3
    assert client.batches[0]
    assert client.batches[1]
    assert client.batches[2]
    assert client.deleted == client.batches[0] + client.batches[2]


@pytest.mark.asyncio
async def test_delete_tool_surfaces_partial_batch_failure_without_repeating_batches():
    client = PartialDeleteClient()
    context = ctx(client, request_id=999)

    result = await DeleteTool(context).execute(context, {"mode": "all"})

    assert result.success is False
    assert "Telegram delete failed" in result.message
    assert len(client.batches) == 3
    assert client.deleted == client.batches[0] + client.batches[2]


@pytest.mark.asyncio
async def test_tehran_local_cutoff_is_converted_against_message_timezone():
    client = type("Client", (), {})()
    client.me = SimpleNamespace(id=111)
    client.deleted = []
    messages = [
        msg(3, date=datetime(2026, 8, 20, 6, 0, tzinfo=timezone.utc)),  # 10:30 Tehran
        msg(2, date=datetime(2026, 8, 20, 4, 0, tzinfo=timezone.utc)),  # 08:30 Tehran
        msg(1, date=datetime(2026, 8, 20, 3, 0, tzinfo=timezone.utc)),  # 07:30 Tehran
    ]

    async def iter_messages(chat_id, **kwargs):
        for item in messages:
            yield item

    async def get_messages(chat_id, ids):
        return [item for item in messages if item.id in ids]

    async def delete_messages(chat_id, ids):
        client.deleted.extend(ids)

    client.iter_messages = iter_messages
    client.get_messages = get_messages
    client.delete_messages = delete_messages

    considered, deleted, error = await delete_service.do_del_self_filtered(
        client,
        -100,
        until_time="09:00",
        tz_name="Asia/Tehran",
    )

    assert error is None
    assert considered == 2
    assert deleted == 2
    assert client.deleted == [2, 1]
