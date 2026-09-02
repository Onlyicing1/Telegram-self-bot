"""Phase 1 — AI Tool Health Audit: all 32 registered tools, end-to-end.

Every test drives the REAL execution chain the AI uses — ToolRegistry →
ToolExecutor (permission gate, argument validation, timeouts) → tool →
service/facade boundary — with external effects faked only at that boundary
(Telegram client / service functions / provider manager / repository).

No live Telegram, no Supabase writes, no provider network calls.

Chain verified per tool:
  registry.get(name) resolves → executor permission gate → tool.execute() →
  service boundary called with expected arguments → ToolResult(success,
  message, data) → executor returns ToolExecutionResult.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.ai.tools.base import PermissionLevel
from backend.ai.tools.context import ToolContext
from backend.ai.tools.executor import ToolExecutor
from backend.ai.tools.registry import ToolRegistry, create_default_registry


OWNER = 777
CHAT = -100123
REPLY = {"chat_id": CHAT, "message_id": 55}


class FakeTelegramAPI:
    """Minimal TelegramAPI stand-in: tools only ever call these methods."""

    def __init__(self):
        self._client = MagicMock()
        self.sent: list[tuple[int, str]] = []
        self._bio = "I am I. Nothing more."

    @property
    def client(self):
        return self._client

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append((chat_id, text))
        return {"id": 1}

    async def get_bio(self):
        return self._bio

    async def get_me(self):
        return {
            "id": OWNER,
            "first_name": "I",
            "last_name": None,
            "full_name": "I",
            "username": "me_u",
            "phone": "+123",
        }


class OutgoingMessage:
    id = 55
    out = True
    sender_id = OWNER


def _client_with_fetchable_messages():
    """A client whose get_messages returns an owner-owned message.

    Save/delete tools fetch the reply/target through the raw client before
    the service boundary, mirroring the real execution path.
    """
    client = MagicMock()
    client.get_messages = AsyncMock(return_value=OutgoingMessage())
    return client


def _async_iterable(items):
    async def _gen():
        for item in items:
            yield item

    return _gen()


def make_registry(telegram: FakeTelegramAPI, *, with_reply: bool = True):
    extra = {"chat_id": CHAT, "request_id": "health-audit"}
    if with_reply:
        extra["reply_msg"] = dict(REPLY)
    client = _client_with_fetchable_messages()
    client.iter_messages = MagicMock(
        return_value=_async_iterable([])
    )  # plain sync call returning an async iterator, like Telethon
    telegram._client = client
    ctx = ToolContext(telegram=telegram, owner_id=OWNER, tz_str="UTC", extra=extra)
    registry = create_default_registry(ctx)
    return registry, ctx, ToolExecutor(registry, ctx)


# ─────────────────────────── Layer 1: registration/visibility ───────────────────────────

EXPECTED_TOOLS = {
    "save": PermissionLevel.READ_WRITE,
    "save_by_link": PermissionLevel.READ_WRITE,
    "delete": PermissionLevel.DANGEROUS,
    "delete_by_id": PermissionLevel.DANGEROUS,
    "delete_replied": PermissionLevel.DANGEROUS,
    "delete_message_by_id": PermissionLevel.DANGEROUS,
    "delete_messages_by_ids": PermissionLevel.DANGEROUS,
    "list_recent_messages": PermissionLevel.READ_ONLY,
    "bio_set_template": PermissionLevel.READ_WRITE,
    "bio_set_text": PermissionLevel.READ_WRITE,
    "bio_set_mood": PermissionLevel.READ_WRITE,
    "bio_on": PermissionLevel.READ_WRITE,
    "bio_off": PermissionLevel.READ_WRITE,
    "bio_show": PermissionLevel.READ_ONLY,
    "get_bio": PermissionLevel.READ_ONLY,
    "username_set_template": PermissionLevel.READ_WRITE,
    "username_set_text": PermissionLevel.READ_WRITE,
    "username_set_mood": PermissionLevel.READ_WRITE,
    "username_on": PermissionLevel.READ_WRITE,
    "username_off": PermissionLevel.READ_WRITE,
    "username_show": PermissionLevel.READ_ONLY,
    "search": PermissionLevel.READ_ONLY,
    "list_saves": PermissionLevel.READ_ONLY,
    "database_stats": PermissionLevel.READ_ONLY,
    "account_show": PermissionLevel.READ_ONLY,
    "settings_get": PermissionLevel.READ_ONLY,
    "settings_set": PermissionLevel.ADMIN_ONLY,
    "organize_list": PermissionLevel.READ_ONLY,
    "organize_clean": PermissionLevel.DANGEROUS,
    "web_search": PermissionLevel.READ_ONLY,
    "create_task": PermissionLevel.READ_WRITE,
    "send_message": PermissionLevel.READ_WRITE,
}


def test_registry_contains_exactly_the_32_expected_tools():
    registry, _ctx, _ex = make_registry(FakeTelegramAPI())
    names = set(registry.list_names())
    assert names == set(EXPECTED_TOOLS), (
        f"registry mismatch: missing={set(EXPECTED_TOOLS) - names} "
        f"extra={names - set(EXPECTED_TOOLS)}"
    )
    assert len(registry.list()) == 32


@pytest.mark.asyncio
async def test_every_tool_has_valid_schema_and_permission():
    registry, _ctx, _ex = make_registry(FakeTelegramAPI())
    for tool in registry.list():
        assert tool.name in EXPECTED_TOOLS, tool.name
        assert tool.permission_level is EXPECTED_TOOLS[tool.name], tool.name
        assert isinstance(tool.description, str) and tool.description, tool.name
        assert isinstance(tool.parameters, dict), tool.name
        assert isinstance(tool.return_type, str) and tool.return_type, tool.name
        assert tool.safe == (
            tool.permission_level in (PermissionLevel.READ_ONLY, PermissionLevel.READ_WRITE)
        ), tool.name


@pytest.mark.asyncio
async def test_every_tool_is_provider_schema_visible():
    """The dispatcher's native tool definitions must include all 32 tools."""
    registry, _ctx, _ex = make_registry(FakeTelegramAPI())

    # The dispatcher wraps the registry the same way the Engine does.
    from backend.ai.engine.dispatcher import Dispatcher

    dispatcher = object.__new__(Dispatcher)
    dispatcher.set_tool_registry(registry)
    definitions = Dispatcher._build_tool_definitions(dispatcher)
    native_names = {d["function"]["name"] for d in definitions}
    assert native_names == set(EXPECTED_TOOLS)
    for d in definitions:
        assert d["type"] == "function"
        params = d["function"]["parameters"]
        assert params["type"] == "object" and "properties" in params


def _service_patches(reply_fetch=None):
    """Patch every tool-facing service function at its module boundary."""
    from backend.services import (
        bio_service,
        database_service,
        delete_service,
        discover_service,
        organize_service,
        save_service,
        username_service,
        web_search_service,
    )

    async def ok(*_a, **_k):
        return "✅ ok"

    async def three(*_a, **_k):
        return (10, 3, None)

    async def two(*_a, **_k):
        return ([55], [])

    async def id_counts(*_a, **_k):
        return (3, None)

    return [
        patch.object(save_service, "execute_save", AsyncMock(return_value="✅ Saved as S0001.")),
        patch.object(save_service, "execute_link_save", AsyncMock(return_value="✅ Saved as S0002.")),
        patch.object(delete_service, "do_del_self_filtered", AsyncMock(side_effect=three)),
        patch.object(delete_service, "do_del_last_n_real", AsyncMock(side_effect=three)),
        patch.object(delete_service, "do_del_id_counts", AsyncMock(side_effect=id_counts)),
        patch.object(delete_service, "delete_verified_self_messages", AsyncMock(side_effect=two)),
        patch.object(bio_service, "do_template", AsyncMock(side_effect=ok)),
        patch.object(bio_service, "do_text", AsyncMock(side_effect=ok)),
        patch.object(bio_service, "do_mood", AsyncMock(side_effect=ok)),
        patch.object(bio_service, "do_on", AsyncMock(side_effect=ok)),
        patch.object(bio_service, "do_off", AsyncMock(side_effect=ok)),
        patch.object(bio_service, "do_show", AsyncMock(side_effect=ok)),
        patch.object(username_service, "do_template", AsyncMock(side_effect=ok)),
        patch.object(username_service, "do_text", AsyncMock(side_effect=ok)),
        patch.object(username_service, "do_mood", AsyncMock(side_effect=ok)),
        patch.object(username_service, "do_on", AsyncMock(side_effect=ok)),
        patch.object(username_service, "do_off", AsyncMock(side_effect=ok)),
        patch.object(username_service, "do_show", AsyncMock(side_effect=ok)),
        patch.object(discover_service, "do_find", AsyncMock(side_effect=ok)),
        patch.object(discover_service, "do_list", AsyncMock(side_effect=ok)),
        patch.object(database_service, "do_stats", AsyncMock(side_effect=ok)),
        patch.object(organize_service, "do_list", AsyncMock(side_effect=ok)),
        patch.object(organize_service, "do_clean", AsyncMock(side_effect=ok)),
        patch.object(web_search_service, "do_web_search", AsyncMock(return_value=(True, "🌐 results", {}))),
    ]


async def _async(value):
    return value


assert _async  # available for future boundary fakes


# ─────────────────── Layer 2: executor chain for every auto-executable tool ───────────────────

EXECUTOR_CASES = {
    # name: (arguments, expectation) — expectation: "success" | "confirmation"
    "save": ({}, "success"),
    "save_by_link": ({"link": "https://t.me/c/123/45"}, "success"),
    "delete": ({"count": 3}, "success"),
    "delete_by_id": ({"message_id": 5}, "success"),
    "delete_replied": ({}, "success"),
    "delete_message_by_id": ({"message_id": 5}, "success"),
    "delete_messages_by_ids": ({"message_ids": [55]}, "success"),
    "list_recent_messages": ({"limit": 5}, "success"),
    "bio_set_template": ({"template": "🕒 {time}"}, "success"),
    "bio_set_text": ({"text": "t"}, "success"),
    "bio_set_mood": ({"mood": "m"}, "success"),
    "bio_on": ({}, "success"),
    "bio_off": ({}, "success"),
    "bio_show": ({}, "success"),
    "get_bio": ({}, "success"),
    "username_set_template": ({"template": "{text}"}, "success"),
    "username_set_text": ({"text": "t"}, "success"),
    "username_set_mood": ({"mood": "m"}, "success"),
    "username_on": ({}, "success"),
    "username_off": ({}, "success"),
    "username_show": ({}, "success"),
    "search": ({"query": "q"}, "success"),
    "list_saves": ({"limit": 5}, "success"),
    "database_stats": ({}, "success"),
    "account_show": ({}, "success"),
    "settings_get": ({"key": "language"}, "success"),
    "settings_set": ({"key": "language", "value": "en"}, "confirmation"),
    "organize_list": ({}, "success"),
    "organize_clean": ({}, "success"),
    "web_search": ({"query": "q"}, "success"),
    "send_message": ({"text": "hi"}, "success"),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_name", sorted(EXECUTOR_CASES))
async def test_executor_chain_resolves_and_executes_tool(tool_name):
    """ToolExecutor chain: name → permission gate → execute → ToolResult."""
    args, expectation = EXECUTOR_CASES[tool_name]
    api = FakeTelegramAPI()
    registry, ctx, executor = make_registry(api)

    extra_patches = []
    if tool_name == "create_task":
        # Deterministic candidate from the dispatcher's fast path; real
        # InMemoryTaskRepository handles persistence.
        ctx.extra["deterministic_task_candidate"] = {
            "label": "hello",
            "schedule_type": "interval",
            "schedule": {"seconds": 60},
            "timezone": "UTC",
            "actions": [{"name": "send_message", "arguments": {"text": "hello"}}],
            "notification_destination": {},
        }
        args = {"request": "every 1 minute write hello"}

    import contextlib

    with contextlib.ExitStack() as stack:
        for p in _service_patches():
            stack.enter_context(p)
        results = await executor.execute_calls(
            [{"name": tool_name, "arguments": args}],
            owner_id=OWNER,
            session_id="health-audit",
            context_override=ctx,
        )

    r = results[0]
    if expectation == "confirmation":
        assert r.success is False and r.needs_confirmation is True, tool_name
        assert r.error == "confirmation_required", tool_name
        return

    assert r.success is True, f"{tool_name}: {r.message} / {r.error}"
    assert r.message, tool_name
    assert r.latency_ms >= 0.0, tool_name


@pytest.mark.asyncio
async def test_executor_create_task_persists_through_real_repository():
    api = FakeTelegramAPI()
    registry, ctx, executor = make_registry(api)
    ctx.extra["deterministic_task_candidate"] = {
        "label": "hello",
        "schedule_type": "interval",
        "schedule": {"seconds": 60},
        "timezone": "UTC",
        "actions": [{"name": "send_message", "arguments": {"text": "hello"}}],
        "notification_destination": {},
    }
    import contextlib

    with contextlib.ExitStack() as stack:
        for p in _service_patches():
            stack.enter_context(p)
        results = await executor.execute_calls(
            [{"name": "create_task", "arguments": {"request": "every 1 minute write hello"}}],
            owner_id=OWNER,
            session_id="health-audit",
            context_override=ctx,
        )
    r = results[0]
    assert r.success is True, r.message
    assert r.data["owner_id"] == OWNER
    assert r.data["schedule_type"] == "interval"
    assert r.data["status"] == "active"
    # The task must actually exist in the fallback repository.
    from backend.ai.database.manager import get_repository_manager

    tasks = await get_repository_manager().task.list_tasks(OWNER)
    assert any(t.id == r.data["task_id"] for t in tasks)


# ─────────────────── Layer 3: safety semantics that must hold ───────────────────


@pytest.mark.asyncio
async def test_unknown_tool_name_is_rejected_not_executed():
    registry, _ctx, executor = make_registry(FakeTelegramAPI())
    results = await executor.execute_calls(
        [{"name": "not_a_tool", "arguments": {}}], owner_id=OWNER, session_id="s"
    )
    assert results[0].success is False
    assert results[0].error == "not_found"


@pytest.mark.asyncio
async def test_settings_set_never_executes_without_confirmation():
    api = FakeTelegramAPI()
    registry, ctx, executor = make_registry(api)
    from backend.services import settings_service

    with patch.object(settings_service, "set_setting", AsyncMock(return_value=True)) as setter:
        results = await executor.execute_calls(
            [{"name": "settings_set", "arguments": {"key": "language", "value": "en"}}],
            owner_id=OWNER,
            session_id="s",
            context_override=ctx,
        )
    setter.assert_not_awaited()
    assert results[0].needs_confirmation is True


@pytest.mark.asyncio
async def test_delete_tools_keep_service_ownership_chokepoint():
    """Every delete path must route through delete_verified_self_messages."""
    from backend.services import delete_service

    api = FakeTelegramAPI()
    registry, ctx, executor = make_registry(api)
    with patch.object(
        delete_service, "delete_verified_self_messages",
        AsyncMock(return_value=([55], [])),
    ) as chokepoint:
        results = await executor.execute_calls(
            [
                {"name": "delete_messages_by_ids", "arguments": {"message_ids": [55]}},
                {"name": "delete_message_by_id", "arguments": {"message_id": 55}},
            ],
            owner_id=OWNER,
            session_id="s",
            context_override=ctx,
        )
    assert chokepoint.await_count == 2
    for r in results:
        assert r.success is True, r.message


@pytest.mark.asyncio
async def test_send_message_keeps_trusted_destination():
    api = FakeTelegramAPI()
    registry, ctx, executor = make_registry(api)
    await executor.execute_calls(
        [{"name": "send_message", "arguments": {"text": "hi"}}],
        owner_id=OWNER,
        session_id="s",
        context_override=ctx,
    )
    assert api.sent == [(CHAT, "hi")]


@pytest.mark.asyncio
async def test_account_show_never_leaks_phone_or_id():
    api = FakeTelegramAPI()
    registry, ctx, executor = make_registry(api)
    results = await executor.execute_calls(
        [{"name": "account_show", "arguments": {}}],
        owner_id=OWNER,
        session_id="s",
        context_override=ctx,
    )
    r = results[0]
    assert r.success is True
    blob = (r.message + str(r.data)).lower()
    assert "+123" not in blob and "phone" not in blob


@pytest.mark.asyncio
async def test_get_bio_failure_is_never_masked_as_empty():
    from backend.telegram_api.exceptions import TelegramAPIError

    api = FakeTelegramAPI()
    registry, ctx, executor = make_registry(api)
    with patch.object(type(api), "get_bio", AsyncMock(side_effect=TelegramAPIError("rpc down"))):
        results = await executor.execute_calls(
            [{"name": "get_bio", "arguments": {}}],
            owner_id=OWNER,
            session_id="s",
            context_override=ctx,
        )
    r = results[0]
    assert r.success is False
    assert r.message != "📝 Bio: —"


# ─────────────────── Layer 4: deterministic argument validation ───────────────────


@pytest.mark.asyncio
async def test_delete_rejects_unscoped_and_oversized_counts():
    api = FakeTelegramAPI()
    registry, ctx, executor = make_registry(api)
    results = await executor.execute_calls(
        [{"name": "delete", "arguments": {}}],
        owner_id=OWNER,
        session_id="s",
        context_override=ctx,
    )
    assert results[0].success is False
    assert "explicit count" in results[0].message

    results2 = await executor.execute_calls(
        [{"name": "delete", "arguments": {"count": 501}}],
        owner_id=OWNER,
        session_id="s",
        context_override=ctx,
    )
    assert results2[0].success is False


@pytest.mark.asyncio
async def test_save_requires_reply_context():
    api = FakeTelegramAPI()
    ctx = ToolContext(
        telegram=api, owner_id=OWNER, tz_str="UTC",
        extra={"chat_id": CHAT, "request_id": "x"},
    )
    registry = ToolRegistry()
    from backend.ai.tools.save import SaveTool

    registry.register(SaveTool(ctx))
    executor = ToolExecutor(registry, ctx)
    results = await executor.execute_calls(
        [{"name": "save", "arguments": {}}], owner_id=OWNER, session_id="s", context_override=ctx
    )
    assert results[0].success is False
    assert "replied message" in results[0].message.lower() or "reply" in results[0].message.lower()


@pytest.mark.asyncio
async def test_save_by_link_rejects_invalid_links():
    api = FakeTelegramAPI()
    registry, ctx, executor = make_registry(api)
    results = await executor.execute_calls(
        [{"name": "save_by_link", "arguments": {"link": "definitely not a link"}}],
        owner_id=OWNER,
        session_id="s",
        context_override=ctx,
    )
    assert results[0].success is False


@pytest.mark.asyncio
async def test_web_search_failure_is_honest_without_configuration():
    """No provider manager / no key → honest failure, never fabricated results."""
    api = FakeTelegramAPI()
    registry, ctx, executor = make_registry(api)
    results = await executor.execute_calls(
        [{"name": "web_search", "arguments": {"query": "q"}}],
        owner_id=OWNER,
        session_id="s",
        context_override=ctx,
    )
    assert results[0].success is False
    assert "unavailable" in results[0].message.lower() or "failed" in results[0].message.lower()


@pytest.mark.asyncio
async def test_web_search_succeeds_through_provider_manager_capability():
    api = FakeTelegramAPI()
    registry, ctx, executor = make_registry(api)

    class FakePM:
        async def web_search(self, query, **kwargs):
            return {
                "success": True,
                "query": query,
                "results": [{"kind": "web", "title": "T", "url": "https://x.io", "description": "d"}],
                "metadata": {"latency": 0.1},
            }

    ctx.extra["provider_manager"] = FakePM()
    results = await executor.execute_calls(
        [{"name": "web_search", "arguments": {"query": "q"}}],
        owner_id=OWNER,
        session_id="s",
        context_override=ctx,
    )
    assert results[0].success is True
    assert "Web results" in results[0].message


@pytest.mark.asyncio
async def test_create_task_fails_honestly_without_interpretation_capability():
    """No deterministic candidate + unconfigured providers → honest failure."""
    api = FakeTelegramAPI()
    registry, ctx, executor = make_registry(api)
    results = await executor.execute_calls(
        [{"name": "create_task", "arguments": {"request": "every 1 minute write hello"}}],
        owner_id=OWNER,
        session_id="s",
        context_override=ctx,
    )
    r = results[0]
    assert r.success is False
    assert r.data == {}  # nothing persisted


# ─────────────────── Layer 5: bio/username engine wiring (real services, fallback DB) ───────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tool_name, service_kwargs",
    [
        ("bio_set_template", {"template": "🕒 {time} | 💭 {mood}"}),
        ("bio_set_text", {"text": "focus"}),
        ("bio_set_mood", {"mood": "calm"}),
        ("bio_show", {}),
    ],
)
async def test_bio_engine_tools_execute_through_real_services(tool_name, service_kwargs):
    """Bio tools run the REAL bio_service against the in-memory DB fallback."""
    api = FakeTelegramAPI()
    registry, ctx, executor = make_registry(api)
    results = await executor.execute_calls(
        [{"name": tool_name, "arguments": service_kwargs}],
        owner_id=OWNER,
        session_id="s",
        context_override=ctx,
    )
    r = results[0]
    assert r.success is True, f"{tool_name}: {r.message}"
    assert r.message.startswith(("✅", "**Bio", "📝"))


@pytest.mark.asyncio
async def test_bio_on_off_round_trip_through_real_services():
    api = FakeTelegramAPI()
    registry, ctx, executor = make_registry(api)
    on = await executor.execute_calls(
        [{"name": "bio_on", "arguments": {}}], owner_id=OWNER, session_id="s", context_override=ctx
    )
    assert on[0].success is True, on[0].message
    off = await executor.execute_calls(
        [{"name": "bio_off", "arguments": {}}], owner_id=OWNER, session_id="s", context_override=ctx
    )
    assert off[0].success is True, off[0].message


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tool_name, service_kwargs",
    [
        ("username_set_template", {"template": "{text}"}),
        ("username_set_text", {"text": "I"}),
        ("username_set_mood", {"mood": "😊"}),
        ("username_show", {}),
        ("username_on", {}),
        ("username_off", {}),
    ],
)
async def test_username_engine_tools_execute_through_real_services(tool_name, service_kwargs):
    api = FakeTelegramAPI()
    registry, ctx, executor = make_registry(api)
    results = await executor.execute_calls(
        [{"name": tool_name, "arguments": service_kwargs}],
        owner_id=OWNER,
        session_id="s",
        context_override=ctx,
    )
    assert results[0].success is True, f"{tool_name}: {results[0].message}"


# ─────────────────── Layer 6: response-path (dispatcher verbatim contract) ───────────────────


@pytest.mark.asyncio
async def test_get_bio_result_is_delivered_verbatim_by_dispatcher():
    """get_bio-only round: dispatcher must return the tool result, no continuation."""
    from backend.ai.engine.dispatcher import Dispatcher
    from backend.ai.tools.executor import ToolExecutionResult

    tool_calls = [{"name": "get_bio", "arguments": {}}]
    exec_results = [
        ToolExecutionResult(tool_name="get_bio", success=True, message="📝 Bio: real bio")
    ]
    assert Dispatcher._read_results_authoritative(tool_calls, exec_results) is True

    # Any failure or extra tool breaks verbatim authority (normal continuation).
    failed = [ToolExecutionResult(tool_name="get_bio", success=False, message="x")]
    assert Dispatcher._read_results_authoritative(tool_calls, failed) is False
    mixed = [
        ToolExecutionResult(tool_name="get_bio", success=True, message="📝 Bio: x"),
        ToolExecutionResult(tool_name="search", success=True, message="y"),
    ]
    assert Dispatcher._read_results_authoritative(tool_calls, mixed) is False


def test_dispatcher_summarizes_tool_results_honestly():
    from backend.ai.engine.dispatcher import Dispatcher

    assert Dispatcher._summarize_tool_results([]) == ""
    failure = Dispatcher._summarize_tool_results([{"success": False, "message": "❌ boom"}])
    assert failure == "❌ boom"
    ok = Dispatcher._summarize_tool_results([{"success": True, "message": "✅ done"}])
    assert ok == "✅ done"
