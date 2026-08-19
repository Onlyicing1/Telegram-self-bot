"""
Delete ownership enforcement tests.

The Delete system MUST be physically restricted to messages sent by the
authenticated/self Telegram account. The restriction lives in executable
code — ``delete_service.delete_verified_self_messages`` is the single
chokepoint through which every AI-driven chat deletion must pass. It
re-fetches each candidate immediately before deletion and verifies the
server ``out`` flag + sender ID against the connected account. Fail-closed:
unverifiable candidates are rejected and never reach the Telegram delete API.

These tests prove, at the exact boundary, that a mixed candidate set
(self + other-user messages) results in ONLY the self-owned IDs reaching the
delete API.
"""
from __future__ import annotations

import pytest

from backend.services import delete_service


class FakeMsg:
    def __init__(self, mid: int, out: bool, sender_id: int | None):
        self.id = mid
        self.out = out
        self.sender_id = sender_id


class FakeChatClient:
    """Fake Telethon client with a known authenticated account (ME_ID)."""

    ME_ID = 111

    def __init__(self, messages: dict[int, FakeMsg]):
        self.messages = dict(messages)
        self.deleted: list[int] = []
        self.me = type("Me", (), {"id": self.ME_ID})()

    async def get_messages(self, chat_id, ids):
        if isinstance(ids, (list, tuple)):
            return [self.messages.get(mid) for mid in ids]
        return self.messages.get(ids)

    async def delete_messages(self, chat_id, ids):
        if isinstance(ids, (list, tuple)):
            self.deleted.extend(ids)
        else:
            self.deleted.append(ids)

    async def iter_messages(self, chat_id, **kwargs):
        limit = kwargs.get("limit", len(self.messages))
        for mid in sorted(self.messages):
            yield self.messages[mid]


def _tg(client) -> object:
    tg = type("TG", (), {})()
    tg.client = client
    return tg


# ── Chokepoint: ownership verification ──


@pytest.mark.asyncio
async def test_self_message_can_be_deleted():
    client = FakeChatClient({1: FakeMsg(1, True, 111)})
    deleted, rejected = await delete_service.delete_verified_self_messages(client, -100, [1])
    assert deleted == [1]
    assert rejected == []
    assert client.deleted == [1]


@pytest.mark.asyncio
async def test_other_users_message_cannot_be_deleted():
    client = FakeChatClient({2: FakeMsg(2, False, 222)})
    deleted, rejected = await delete_service.delete_verified_self_messages(client, -100, [2])
    assert deleted == []
    assert rejected == [2]
    assert client.deleted == []


@pytest.mark.asyncio
async def test_mixed_candidate_set_executor_receives_only_self_ids():
    """THE core guarantee: candidates contain self + other-user messages and
    the delete API receives ONLY the self-owned IDs."""
    client = FakeChatClient({
        1: FakeMsg(1, True, 111),   # self-owned → deletable
        2: FakeMsg(2, False, 222),  # other participant → rejected
        3: FakeMsg(3, True, 333),   # inconsistent metadata (out but other sender) → rejected
    })
    deleted, rejected = await delete_service.delete_verified_self_messages(
        client, -100, [1, 2, 3]
    )
    assert deleted == [1]
    assert sorted(rejected) == [2, 3]
    assert client.deleted == [1]


@pytest.mark.asyncio
async def test_missing_sender_info_fails_closed():
    client = FakeChatClient({1: FakeMsg(1, True, None)})
    deleted, rejected = await delete_service.delete_verified_self_messages(client, -100, [1])
    assert deleted == []
    assert rejected == [1]
    assert client.deleted == []


@pytest.mark.asyncio
async def test_fetch_failure_fails_closed():
    class BrokenClient:
        me = type("Me", (), {"id": 111})()
        deleted = []

        async def get_messages(self, chat_id, ids):
            raise RuntimeError("network down")

        async def delete_messages(self, chat_id, ids):
            self.deleted.extend(ids)

    client = BrokenClient()
    deleted, rejected = await delete_service.delete_verified_self_messages(client, -100, [1])
    assert deleted == []
    assert rejected == [1]
    assert client.deleted == []


@pytest.mark.asyncio
async def test_not_found_id_rejected():
    client = FakeChatClient({})
    deleted, rejected = await delete_service.delete_verified_self_messages(client, -100, [99])
    assert deleted == []
    assert rejected == [99]
    assert client.deleted == []


@pytest.mark.asyncio
async def test_invalid_ids_rejected_without_fetch():
    client = FakeChatClient({1: FakeMsg(1, True, 111)})
    deleted, rejected = await delete_service.delete_verified_self_messages(client, -100, [0, -5, "x"])
    assert deleted == []
    assert rejected == [0, -5, "x"]
    assert client.deleted == []


# ── AI-generated self messages remain eligible ──


@pytest.mark.asyncio
async def test_ai_generated_self_messages_deletable():
    """Nova's own generated messages are sent by the self account, so they
    remain eligible for deletion under the existing Delete rules."""
    client = FakeChatClient({5: FakeMsg(5, True, 111)})
    deleted, rejected = await delete_service.delete_verified_self_messages(client, -100, [5])
    assert deleted == [5]
    assert rejected == []
    assert client.deleted == [5]


# ── Range operations: range defines candidates, ownership defines deletion ──


@pytest.mark.asyncio
async def test_delete_all_range_deletes_only_self():
    """'همه پیام‌ها رو پاک کن' means all deletable SELF messages, never every
    participant's messages. The whole chat is the candidate range; ownership
    filters the deletion set."""
    client = FakeChatClient({
        1: FakeMsg(1, True, 111),
        2: FakeMsg(2, False, 222),
        3: FakeMsg(3, True, 111),
        4: FakeMsg(4, False, 222),
    })
    considered, deleted, err = await delete_service.do_del_last_n_real(client, -100, 4)
    assert err is None
    assert considered == 4
    assert deleted == 2
    assert client.deleted == [1, 3]


@pytest.mark.asyncio
async def test_time_range_candidates_still_self_filtered():
    """Range = messages until a time; other-user messages inside the range are
    never deleted — only the self-owned subset is."""
    client = FakeChatClient({
        1: FakeMsg(1, True, 111),
        2: FakeMsg(2, False, 222),
        3: FakeMsg(3, True, 111),
    })
    deleted, rejected = await delete_service.delete_verified_self_messages(
        client, -100, [1, 2, 3]
    )
    assert deleted == [1, 3]
    assert rejected == [2]
    assert client.deleted == [1, 3]


@pytest.mark.asyncio
async def test_delete_last_n_outgoing_self_only():
    client = FakeChatClient({
        1: FakeMsg(1, True, 111),
        2: FakeMsg(2, False, 222),
        3: FakeMsg(3, True, 111),
    })
    deleted, err = await delete_service.do_del_n_counts(client, -100, 3)
    assert err is None
    assert deleted == 2
    assert client.deleted == [1, 3]


@pytest.mark.asyncio
async def test_delete_by_id_forward_ownership_filtered():
    """Delete-from-ID-forward with a candidate set that includes other-user
    messages: only self-owned messages are actually deleted."""
    client = FakeChatClient({
        10: FakeMsg(10, True, 111),
        11: FakeMsg(11, False, 222),
        12: FakeMsg(12, True, 111),
    })
    deleted, err = await delete_service.do_del_id_counts(client, -100, 10)
    assert err is None
    assert deleted == 2
    assert client.deleted == [10, 12]


# ── Tool-level: semantic delete / replied / by-id ──


@pytest.mark.asyncio
async def test_semantic_delete_only_self():
    from backend.ai.tools.context import ToolContext
    from backend.ai.tools.semantic import DeleteMessagesByIdsTool

    client = FakeChatClient({
        10: FakeMsg(10, True, 111),
        11: FakeMsg(11, False, 222),
        12: FakeMsg(12, True, 111),
    })
    ctx = ToolContext(telegram=_tg(client), owner_id=1, tz_str="UTC", extra={"chat_id": -100})
    result = await DeleteMessagesByIdsTool(ctx).execute(
        ctx, {"message_ids": [10, 11, 12, 99]}
    )
    assert result.success is True
    assert result.data["deleted"] == [10, 12]
    assert set(result.data["rejected"]) == {11, 99}
    assert client.deleted == [10, 12]


@pytest.mark.asyncio
async def test_replied_other_user_message_never_deleted():
    """A replied-to message is a boundary/reference — if it belongs to
    another user it must never be deleted."""
    from backend.ai.tools.context import ToolContext
    from backend.ai.tools.delete import DeleteRepliedTool

    client = FakeChatClient({55: FakeMsg(55, False, 222)})
    ctx = ToolContext(
        telegram=_tg(client),
        owner_id=1,
        tz_str="UTC",
        extra={"reply_msg": {"chat_id": -100, "message_id": 55}},
    )
    result = await DeleteRepliedTool(ctx).execute(ctx, {})
    assert result.success is False
    assert client.deleted == []


@pytest.mark.asyncio
async def test_user_supplied_id_of_other_user_rejected():
    from backend.ai.tools.context import ToolContext
    from backend.ai.tools.delete import DeleteMessageByIdTool

    client = FakeChatClient({77: FakeMsg(77, False, 222)})
    ctx = ToolContext(telegram=_tg(client), owner_id=1, tz_str="UTC", extra={"chat_id": -100})
    result = await DeleteMessageByIdTool(ctx).execute(ctx, {"message_id": 77})
    assert result.success is False
    assert client.deleted == []


@pytest.mark.asyncio
async def test_delete_transport_failure_is_not_silent_success():
    from backend.ai.tools.context import ToolContext
    from backend.ai.tools.semantic import DeleteMessagesByIdsTool

    class FailDeleteClient(FakeChatClient):
        async def delete_messages(self, chat_id, ids):
            raise RuntimeError("Telegram RPC failed")

    client = FailDeleteClient({10: FakeMsg(10, True, 111)})
    ctx = ToolContext(telegram=_tg(client), owner_id=1, tz_str="UTC", extra={"chat_id": -100})
    result = await DeleteMessagesByIdsTool(ctx).execute(ctx, {"message_ids": [10]})
    assert result.success is False
    assert "Telegram RPC failed" in result.message


# ── Silent post-delete behavior is preserved (no confirmation reply) ──


def test_delete_tool_results_still_silent():
    """The silent-delete contract from the handler layer must still treat every
    delete tool result as silent — no post-delete confirmation message."""
    from backend.bot.handlers.ai_unified import _DELETE_TOOL_NAMES

    assert {
        "delete", "delete_replied", "delete_by_id",
        "delete_message_by_id", "delete_messages_by_ids",
    } == set(_DELETE_TOOL_NAMES)
