"""
Task 28 — Delete regression tests (restore the working path + ownership boundary).

The Delete category regressed after the ownership change: requests appeared to
stop executing. These tests prove the FULL pipeline is intact and distinguish
two very different failures:

  A. "Delete was correctly rejected because the message was not self-owned"
     (ownership rejection — correct, fail-closed security)
  B. "Delete was broken before ownership checking"
     (pipeline breakage — must never be reported as an ownership rejection)

They also lock in the semantic-delete contract: a topic/context request
("پیام‌های مربوط به دعوای اخیر رو پاک کن") must reach the AI, never be
collapsed by the deterministic parser into "delete the last message".
"""
from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.ai.actions import (
    KIND_CONVERSATIONAL,
    KIND_EXECUTABLE,
    parse_action_text,
    parse_command_intent,
)
from backend.services import delete_service


# ── Realistic Telethon-shaped fake ────────────────────────────────────────────
#
# get_messages(ids=[...]) models REAL Telethon `_IDsIter` semantics for
# non-channel chats: it is a GLOBAL GetMessagesRequest, so messages that no
# longer exist are OMITTED (the returned list is shorter), and messages that
# exist in a different chat are replaced with None (peer validation). The
# chokepoint must handle both without deleting anything foreign.

class FakeMsg:
    def __init__(self, mid: int, out: bool, sender_id: int | None, chat: int = -100):
        self.id = mid
        self.out = out
        self.sender_id = sender_id
        self.chat = chat


class FakeChatClient:
    """Fake Telethon client with a known authenticated account (ME_ID)."""

    ME_ID = 111

    def __init__(self, messages: dict[int, FakeMsg]):
        self.messages = dict(messages)
        self.deleted: list[int] = []
        self.me = type("Me", (), {"id": self.ME_ID})()

    async def get_messages(self, chat_id, ids):
        if isinstance(ids, (list, tuple)):
            out: list[FakeMsg | None] = []
            for mid in ids:
                msg = self.messages.get(mid)
                if msg is None:
                    continue  # deleted / invented IDs are omitted by GetMessagesRequest
                if msg.chat != chat_id:
                    out.append(None)  # wrong-chat message -> peer validation -> None
                    continue
                out.append(msg)
            return out
        msg = self.messages.get(ids)
        return msg if msg is not None and msg.chat == chat_id else None

    async def delete_messages(self, chat_id, ids):
        if isinstance(ids, (list, tuple)):
            self.deleted.extend(ids)
        else:
            self.deleted.append(ids)

    async def iter_messages(self, chat_id, **kwargs):
        limit = kwargs.get("limit")
        newest_first = sorted(self.messages, reverse=True)
        if limit is None:
            limit = len(newest_first)
        for mid in newest_first[:limit]:
            yield self.messages[mid]


def _tg(client) -> object:
    tg = type("TG", (), {})()
    tg.client = client
    return tg


# ── 1/2/5. Intent recognition + action resolution (Persian/English) ──────────


@pytest.mark.parametrize(
    "text,has_reply,expected_tool,expected_count",
    [
        ("این پیام رو پاک کن", True, "delete_replied", None),
        ("آخرین پیامم رو پاک کن", False, "delete", 1),
        ("۵ پیام آخر خودم رو پاک کن", False, "delete", 5),
        ("ده پیام آخر رو پاک کن", False, "delete", 10),
        ("پنج پیام آخر رو پاک کن", False, "delete", 5),
        ("delete last 10 messages", False, "delete", 10),
        ("delete this message", True, "delete_replied", None),
    ],
)
def test_delete_intent_recognized(text, has_reply, expected_tool, expected_count):
    r = parse_command_intent(text, has_reply=has_reply)
    assert r.kind == KIND_EXECUTABLE
    assert r.action == "delete_messages"
    assert [t["name"] for t in r.tool_calls] == [expected_tool]
    if expected_count is not None:
        assert r.tool_calls[0]["arguments"]["count"] == expected_count


def test_delete_action_resolves_from_structured_json():
    r = parse_action_text('{"action": "delete_messages", "target": "recent_messages", "count": 5}')
    assert r.kind == KIND_EXECUTABLE
    assert r.tool_calls == [{"name": "delete", "arguments": {"count": 5}}]


def test_delete_action_resolves_replied():
    r = parse_action_text('{"action": "delete_messages", "target": "replied_message"}')
    assert r.kind == KIND_EXECUTABLE
    assert r.tool_calls == [{"name": "delete_replied", "arguments": {}}]


def test_ambiguous_delete_clarifies_not_executes():
    r = parse_command_intent("پاک کن", has_reply=False)
    assert r.kind != KIND_EXECUTABLE


# ── Semantic deletes must reach the AI, never be hijacked ─────────────────────


@pytest.mark.parametrize(
    "text",
    [
        "پیام‌های مربوط به دعوای اخیر رو پاک کن",
        "پیام‌های مربوط به دعوای دیروز رو حذف کن",
        "هرچی از بحث قبلیمون مونده رو پاک کن",
        "پیام‌های مربوط به بحث پروژه رو پاک کن",
        "پیام‌هایی که درباره فلان موضوع بودن رو حذف کن",
        "delete messages about the argument",
        "delete messages related to the argument",
        "پیام‌های دعوای اخیر رو پاک کن",
    ],
)
def test_semantic_delete_is_not_hijacked_into_last_message(text):
    """A topic/context delete must NOT collapse into 'delete last message'.

    Before this fix, \"اخیر\" inside \"دعوای اخیر\" matched the last-message
    vocabulary and the deterministic parser returned `delete count=1`,
    overriding the model's semantic resolution. It must be conversational so
    the AI resolves WHICH messages to delete.
    """
    r = parse_command_intent(text, has_reply=False)
    assert r.kind == KIND_CONVERSATIONAL, (text, r.kind, r.action)


def test_deterministic_last_message_still_parses():
    """'آخرین پیامم رو پاک کن' stays deterministic (no topic reference)."""
    r = parse_command_intent("آخرین پیامم رو پاک کن", has_reply=False)
    assert r.kind == KIND_EXECUTABLE
    assert r.tool_calls == [{"name": "delete", "arguments": {"count": 1}}]


# ── Ownership boundary: self-only deletion at the executor ───────────────────


@pytest.mark.asyncio
async def test_self_message_passes_ownership_and_reaches_executor():
    client = FakeChatClient({1: FakeMsg(1, True, 111)})
    deleted, rejected = await delete_service.delete_verified_self_messages(client, -100, [1])
    assert deleted == [1]
    assert rejected == []
    assert client.deleted == [1]  # the Telegram delete API was actually reached


@pytest.mark.asyncio
async def test_other_users_message_fails_ownership():
    client = FakeChatClient({2: FakeMsg(2, False, 222)})
    deleted, rejected = await delete_service.delete_verified_self_messages(client, -100, [2])
    assert deleted == []
    assert rejected == [2]
    assert client.deleted == []


@pytest.mark.asyncio
async def test_mixed_candidates_only_self_reaches_executor():
    client = FakeChatClient({
        1: FakeMsg(1, True, 111),
        2: FakeMsg(2, False, 222),
        3: FakeMsg(3, True, 333),   # inconsistent metadata (out but foreign sender)
    })
    deleted, rejected = await delete_service.delete_verified_self_messages(client, -100, [1, 2, 3])
    assert deleted == [1]
    assert sorted(rejected) == [2, 3]
    assert client.deleted == [1]


@pytest.mark.asyncio
async def test_ai_generated_self_messages_remain_deletable():
    """Nova's own messages are sent by the self account — eligible for deletion."""
    client = FakeChatClient({9: FakeMsg(9, True, 111)})
    deleted, rejected = await delete_service.delete_verified_self_messages(client, -100, [9])
    assert deleted == [9]
    assert client.deleted == [9]


@pytest.mark.asyncio
async def test_unknown_ownership_fails_closed():
    client = FakeChatClient({1: FakeMsg(1, True, None)})
    deleted, rejected = await delete_service.delete_verified_self_messages(client, -100, [1])
    assert deleted == []
    assert rejected == [1]
    assert client.deleted == []


@pytest.mark.asyncio
async def test_chokepoint_handles_realistic_shortened_fetch_list():
    """Real GetMessagesRequest omits deleted IDs (shorter list) and replaces
    wrong-chat messages with None. Neither may delete or crash."""
    client = FakeChatClient({
        1: FakeMsg(1, True, 111),
        2: FakeMsg(2, False, 222),
        3: FakeMsg(3, True, 111),
    })
    # 99 was deleted server-side -> omitted from the fetch result entirely.
    deleted, rejected = await delete_service.delete_verified_self_messages(client, -100, [1, 2, 99, 3])
    assert deleted == [1, 3]
    assert sorted(rejected) == [2, 99]
    assert client.deleted == [1, 3]


@pytest.mark.asyncio
async def test_partial_batch_failure_attempts_all_and_surfaces_error():
    """A failing batch must not silently abort the remaining verified batches;
    the failure is reported honestly (transport failure propagates)."""
    messages = {i: FakeMsg(i, True, 111) for i in range(1, 102)}
    client = FakeChatClient(messages)

    real_delete = client.delete_messages

    async def flaky_delete(chat_id, ids):
        if 1 in ids:
            raise RuntimeError("Telegram RPC failed")
        return await real_delete(chat_id, ids)

    client.delete_messages = flaky_delete
    with pytest.raises(RuntimeError, match="Telegram delete failed"):
        await delete_service.delete_verified_self_messages(client, -100, list(range(1, 102)))
    # Batch 2 was still attempted — only the failing batch was skipped.
    assert 101 in client.deleted


# ── Delete-N / reply-target execution ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_delete_last_n_real_deletes_only_self_among_candidates():
    client = FakeChatClient({
        10: FakeMsg(10, True, 111),
        9: FakeMsg(9, False, 222),
        8: FakeMsg(8, True, 111),
        7: FakeMsg(7, False, 222),
    })
    considered, deleted, err = await delete_service.do_del_last_n_real(client, -100, 3)
    assert err is None
    assert considered == 3
    assert deleted == 2
    assert client.deleted == [10, 8]


@pytest.mark.asyncio
async def test_reply_target_delete_works_and_reaches_executor():
    from backend.ai.tools.context import ToolContext
    from backend.ai.tools.delete import DeleteRepliedTool

    client = FakeChatClient({55: FakeMsg(55, True, 111)})
    ctx = ToolContext(
        telegram=_tg(client),
        owner_id=1,
        tz_str="UTC",
        extra={"reply_msg": {"chat_id": -100, "message_id": 55}},
    )
    result = await DeleteRepliedTool(ctx).execute(ctx, {})
    assert result.success is True
    assert client.deleted == [55]


@pytest.mark.asyncio
async def test_ownership_rejection_is_not_pipeline_breakage():
    """Rejecting a foreign message must read as an ownership rejection,
    never as a generic delete failure — the two are different failures."""
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
    assert "not sent by the owner" in result.message
    assert not result.message.lower().startswith("delete failed")
    assert client.deleted == []


@pytest.mark.asyncio
async def test_fetch_failure_is_breakage_not_ownership_rejection():
    from backend.ai.tools.context import ToolContext
    from backend.ai.tools.delete import DeleteRepliedTool

    class BrokenClient(FakeChatClient):
        async def get_messages(self, chat_id, ids):
            raise RuntimeError("network down")

    client = BrokenClient({55: FakeMsg(55, True, 111)})
    ctx = ToolContext(
        telegram=_tg(client),
        owner_id=1,
        tz_str="UTC",
        extra={"reply_msg": {"chat_id": -100, "message_id": 55}},
    )
    result = await DeleteRepliedTool(ctx).execute(ctx, {})
    assert result.success is False
    assert "Could not fetch" in result.message
    assert client.deleted == []


# ── Fast path: provider-independent delete execution ─────────────────────────


class _FakeProvider:
    def __init__(self, name: str = "test"):
        self._name = name
        self.calls = 0
        self.config = type("Cfg", (), {"default_model": "m", "model": "m"})()

    @property
    def name(self) -> str:
        return self._name

    async def chat(self, messages, **kwargs):
        self.calls += 1
        from backend.ai.providers.base.contract import ProviderResponse
        return ProviderResponse(text="ok", provider_name=self._name, success=True)

    def health(self):
        return {"healthy": True}


def _make_dispatcher(mock_te, provider):
    from backend.ai.engine.dispatcher import Dispatcher
    from backend.ai.engine.hooks import NOOP_HOOKS
    from backend.ai.engine.metrics import EngineMetrics
    from backend.ai.providers.manager.manager import ProviderManager

    pm = ProviderManager()
    pm.register_provider(provider)
    pm.switch_provider(provider.name)
    pm._fallback_chain = []

    mock_conv = MagicMock()
    mock_sess = MagicMock()
    mock_sess.session_id = "s"
    mock_sess.owner_id = 123
    mock_sess.active_provider = provider.name
    mock_conv.get_session.return_value = mock_sess
    mock_conv.restore_history = AsyncMock()
    mock_conv.get_history.return_value = []

    mock_pb = MagicMock()
    pp = MagicMock()
    pp.system_prompt = "sys"
    pp.runtime_context = ""
    pp.conversation_context = ""
    pp.tool_context = ""
    pp.user_input = "do it"
    pp.estimated_tokens.estimated_input_tokens = 50
    pp.estimated_tokens.prompt_size_chars = 100
    mock_pb.build.return_value = pp

    return Dispatcher(
        mock_conv, mock_pb, pm, NOOP_HOOKS, EngineMetrics(), tool_executor=mock_te,
    )


def _mock_executor(results):
    from backend.ai.tools.executor import ToolExecutionResult

    mock_te = MagicMock()
    mock_te.execute_calls = AsyncMock(return_value=[
        ToolExecutionResult(tool_name=r[0], success=r[1], message=r[2], data=r[3])
        for r in results
    ])
    c = MagicMock()
    c.extra = {}
    c.telegram = None
    c.tz_str = "UTC"
    c.client = None
    mock_te._context = c
    return mock_te


@pytest.mark.asyncio
async def test_fast_path_executes_last_message_without_provider():
    """'آخرین پیامم رو پاک کن' must execute deterministically even when every
    provider is down — it is a scope-free positional delete."""
    from backend.ai.session.request import AIRequest

    mock_te = _mock_executor([
        ("delete", True, "Deleted 1 outgoing message(s).", {"count": 1}),
    ])
    provider = _FakeProvider()
    d = _make_dispatcher(mock_te, provider)

    result = await d.dispatch(AIRequest(
        session_id="s1", message_id=1, owner_id=123,
        user_message="آخرین پیامم رو پاک کن", chat_id=456,
    ))

    assert result.success is True
    assert result.metadata["finish_state"] == "local_fast_path"
    tool_calls = mock_te.execute_calls.call_args.args[0]
    assert tool_calls == [{"name": "delete", "arguments": {"count": 1}}]
    assert provider.calls == 0


@pytest.mark.asyncio
async def test_fast_path_skips_semantic_delete_to_provider():
    """A topic/context delete must reach the AI (never a fast-path delete)."""
    from backend.ai.session.request import AIRequest

    mock_te = _mock_executor([
        ("list_recent_messages", True, "", {"messages": []}),
    ])
    provider = _FakeProvider()
    d = _make_dispatcher(mock_te, provider)

    result = await d.dispatch(AIRequest(
        session_id="s1", message_id=1, owner_id=123,
        user_message="پیام‌های مربوط به دعوای اخیر رو پاک کن", chat_id=456,
    ))

    assert result.metadata.get("finish_state") != "local_fast_path"
    assert provider.calls >= 1


# ── End-to-end: real fast path + real tools + real chokepoint ────────────────


@pytest.mark.asyncio
async def test_end_to_end_fast_path_delete_reaches_telegram_executor():
    """The full deterministic pipeline — parse → fast path → real DeleteTool →
    delete_service chokepoint → client.delete_messages — deletes ONLY the
    self-owned messages. The result metadata must classify as a silent delete
    (never a Telegram confirmation)."""
    from backend.ai.engine.dispatcher import Dispatcher
    from backend.ai.engine.hooks import NOOP_HOOKS
    from backend.ai.engine.metrics import EngineMetrics
    from backend.ai.providers.manager.manager import ProviderManager
    from backend.ai.session.request import AIRequest
    from backend.ai.tools.context import ToolContext
    from backend.ai.tools.executor import ToolExecutor
    from backend.ai.tools.registry import create_default_registry

    client = FakeChatClient({
        5: FakeMsg(5, True, 111),    # self-owned
        4: FakeMsg(4, False, 222),   # other participant
        3: FakeMsg(3, True, 111),    # self-owned
        2: FakeMsg(2, False, 222),   # other participant
        1: FakeMsg(1, True, 111),    # self-owned
    })
    ctx = ToolContext(telegram=None, owner_id=111, tz_str="UTC", client=client, extra={})
    registry = create_default_registry(ctx)
    executor = ToolExecutor(registry, ctx)

    pm = ProviderManager()
    provider = _FakeProvider()
    pm.register_provider(provider)
    pm.switch_provider(provider.name)
    pm._fallback_chain = []

    mock_conv = MagicMock()
    mock_sess = MagicMock()
    mock_sess.session_id = "s"
    mock_sess.owner_id = 111
    mock_sess.active_provider = provider.name
    mock_conv.get_session.return_value = mock_sess
    mock_conv.restore_history = AsyncMock()
    mock_conv.get_history.return_value = []

    mock_pb = MagicMock()
    pp = MagicMock()
    pp.system_prompt = "sys"
    pp.runtime_context = ""
    pp.conversation_context = ""
    pp.tool_context = ""
    pp.user_input = "do it"
    pp.estimated_tokens.estimated_input_tokens = 50
    pp.estimated_tokens.prompt_size_chars = 100
    mock_pb.build.return_value = pp

    d = Dispatcher(
        mock_conv, mock_pb, pm, NOOP_HOOKS, EngineMetrics(),
        tool_registry=registry, tool_executor=executor,
    )

    result = await d.dispatch(AIRequest(
        session_id="s1", message_id=1, owner_id=111,
        user_message="پنج پیام آخر رو پاک کن", chat_id=-100,
    ))

    assert result.success is True
    assert result.metadata["finish_state"] == "local_fast_path"
    # The current request (message 1) is explicitly excluded; only the
    # previous self-owned/Nova messages reach the Telegram delete API.
    assert sorted(client.deleted) == [3, 5]
    # The execution result classifies as a silent delete — no confirmation
    # message is ever produced for a successful delete round.
    from backend.bot.handlers.ai_unified import _is_silent_delete
    assert _is_silent_delete(result) is True
    # The provider was never called — fully deterministic execution.
    assert provider.calls == 0


@pytest.mark.asyncio
async def test_successful_delete_is_silent_at_handler_contract():
    """A successful pure-delete EngineResult must never produce a Telegram
    confirmation (the deletion itself is the only visible effect)."""
    from backend.bot.handlers.ai_unified import _is_silent_delete

    result = type("R", (), {
        "metadata": {
            "tool_results": [
                {"tool_name": "delete", "success": True, "message": "Deleted 5."},
            ],
        },
    })()
    assert _is_silent_delete(result) is True

    failed = type("R", (), {
        "metadata": {
            "tool_results": [
                {"tool_name": "delete", "success": False, "message": "Delete failed: boom"},
            ],
        },
    })()
    # A failed delete is NOT silent — the error must reach the user.
    assert _is_silent_delete(failed) is False


@pytest.mark.asyncio
async def test_semantic_delete_through_tool_still_self_only():
    """Even when the AI resolves a semantic delete into concrete IDs, the
    executor re-validates ownership: only self-owned IDs are deleted."""
    from backend.ai.tools.context import ToolContext
    from backend.ai.tools.semantic import DeleteMessagesByIdsTool

    client = FakeChatClient({
        10: FakeMsg(10, True, 111),
        11: FakeMsg(11, False, 222),
        12: FakeMsg(12, True, 111),
    })
    ctx = ToolContext(telegram=_tg(client), owner_id=1, tz_str="UTC", extra={"chat_id": -100})
    result = await DeleteMessagesByIdsTool(ctx).execute(ctx, {"message_ids": [10, 11, 12, 99]})
    assert result.success is True
    assert result.data["deleted"] == [10, 12]
    assert set(result.data["rejected"]) == {11, 99}
    assert client.deleted == [10, 12]

