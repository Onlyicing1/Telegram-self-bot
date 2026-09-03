"""Health tests for the newly connected AI tools.

Covers the task lifecycle capability (task_list, task_inspect,
task_transition) and the saved-item retrieval capability (retrieve_save).

Each test drives the REAL chain: ToolRegistry → ToolExecutor (permission
gate, argument validation) → tool → authoritative existing service
(TaskManagementService / retrieve_service) → in-memory repository or a
faked raw-Telegram boundary. No live Telegram, no Supabase, no providers.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.ai.tools.context import ToolContext
from backend.ai.tools.executor import ToolExecutor
from backend.ai.tools.registry import create_default_registry


OWNER = 777
CHAT = -100123


def _task_data():
    return {
        "label": "water reminder",
        "schedule_type": "interval",
        "schedule": {"seconds": 60},
        "timezone": "UTC",
        "actions": [{"name": "send_message", "arguments": {"text": "hello"}}],
        "notification_destination": {},
    }


def make_chain(extra: dict | None = None):
    ctx = ToolContext(
        telegram=None, owner_id=OWNER, tz_str="UTC",
        extra={"chat_id": CHAT, "request_id": "connect-audit", **(extra or {})},
    )
    registry = create_default_registry(ctx)
    return registry, ctx, ToolExecutor(registry, ctx)


async def run_tool(executor, ctx, name, arguments):
    results = await executor.execute_calls(
        [{"name": name, "arguments": arguments}],
        owner_id=OWNER, session_id="connect-audit", context_override=ctx,
    )
    return results[0]


# ── task_list ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_task_list_registered_and_reachable():
    registry, _ctx, _executor = make_chain()
    assert registry.has("task_list")
    tool = registry.get("task_list")
    assert tool.permission_level.value == "read_only" and tool.safe is True


@pytest.mark.asyncio
async def test_task_list_executor_path_lists_persisted_tasks():
    registry, ctx, executor = make_chain()
    from backend.ai.database.manager import get_repository_manager

    repo = get_repository_manager().task
    created = await repo.create_task(OWNER, _task_data())

    result = await run_tool(executor, ctx, "task_list", {})
    assert result.success is True
    assert f"#{created.id}" in result.message
    assert "water reminder" in result.message


@pytest.mark.asyncio
async def test_task_list_is_owner_scoped():
    registry, ctx, executor = make_chain()
    from backend.ai.database.manager import get_repository_manager

    repo = get_repository_manager().task
    created = await repo.create_task(2, _task_data())  # another owner

    result = await run_tool(executor, ctx, "task_list", {})
    assert result.success is True
    assert f"#{created.id}" not in result.message


# ── task_inspect ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_task_inspect_registered_and_reachable():
    registry, _ctx, _executor = make_chain()
    tool = registry.get("task_inspect")
    assert tool is not None and tool.permission_level.value == "read_only"


@pytest.mark.asyncio
async def test_task_inspect_executor_path_returns_detail():
    registry, ctx, executor = make_chain()
    from backend.ai.database.manager import get_repository_manager

    repo = get_repository_manager().task
    created = await repo.create_task(OWNER, _task_data())

    result = await run_tool(executor, ctx, "task_inspect", {"task_id": created.id})
    assert result.success is True
    assert "interval" in result.message
    assert "occurrences" in result.message


@pytest.mark.asyncio
async def test_task_inspect_failure_path_missing_and_invalid():
    registry, ctx, executor = make_chain()
    missing = await run_tool(executor, ctx, "task_inspect", {"task_id": 424242})
    assert missing.success is False and "not found" in missing.message.lower()

    invalid = await run_tool(executor, ctx, "task_inspect", {"task_id": -1})
    assert invalid.success is False

    no_args = await run_tool(executor, ctx, "task_inspect", {})
    assert no_args.success is False


# ── task_transition ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_task_transition_registered_and_reachable():
    registry, _ctx, _executor = make_chain()
    tool = registry.get("task_transition")
    assert tool is not None
    assert tool.permission_level.value == "read_write"
    assert sorted(tool.parameters["action"]["enum"]) == ["active", "completed", "paused"]


@pytest.mark.asyncio
async def test_task_transition_executor_path_pause_resume_complete():
    registry, ctx, executor = make_chain()
    from backend.ai.database.manager import get_repository_manager

    repo = get_repository_manager().task
    created = await repo.create_task(OWNER, _task_data())

    paused = await run_tool(
        executor, ctx, "task_transition",
        {"task_id": created.id, "action": "paused", "expected_version": created.version},
    )
    assert paused.success is True and paused.data["status"] == "paused"
    assert paused.data["version"] == created.version + 1

    resumed = await run_tool(
        executor, ctx, "task_transition",
        {"task_id": created.id, "action": "active", "expected_version": paused.data["version"]},
    )
    assert resumed.success is True and resumed.data["status"] == "active"

    completed = await run_tool(
        executor, ctx, "task_transition",
        {"task_id": created.id, "action": "completed", "expected_version": resumed.data["version"]},
    )
    assert completed.success is True and completed.data["status"] == "completed"


@pytest.mark.asyncio
async def test_task_transition_cas_stale_version_fails_honestly():
    registry, ctx, executor = make_chain()
    from backend.ai.database.manager import get_repository_manager

    repo = get_repository_manager().task
    created = await repo.create_task(OWNER, _task_data())

    stale = await run_tool(
        executor, ctx, "task_transition",
        {"task_id": created.id, "action": "paused", "expected_version": 999},
    )
    assert stale.success is False
    assert "stale" in stale.message.lower() or "not found" in stale.message.lower()
    # Task unchanged.
    current = await repo.get_task(OWNER, created.id)
    assert current.version == created.version and current.status == "active"


@pytest.mark.asyncio
async def test_task_transition_is_owner_scoped():
    registry, ctx, executor = make_chain()
    from backend.ai.database.manager import get_repository_manager

    repo = get_repository_manager().task
    foreign = await repo.create_task(2, _task_data())

    result = await run_tool(
        executor, ctx, "task_transition",
        {"task_id": foreign.id, "action": "paused", "expected_version": foreign.version},
    )
    assert result.success is False


@pytest.mark.asyncio
async def test_task_transition_validates_arguments():
    registry, ctx, executor = make_chain()
    for bad in (
        {"task_id": 1, "action": "deleted", "expected_version": 1},   # status outside enum
        {"task_id": 1, "action": "paused"},                            # missing version
        {"action": "paused", "expected_version": 1},                   # missing id
    ):
        result = await run_tool(executor, ctx, "task_transition", bad)
        assert result.success is False, bad


# ── retrieve_save ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_retrieve_save_registered_and_reachable():
    registry, _ctx, _executor = make_chain()
    tool = registry.get("retrieve_save")
    assert tool is not None and tool.permission_level.value == "read_write" and tool.safe is True


@pytest.mark.asyncio
async def test_retrieve_save_executor_path_forwards_through_service():
    from backend.services import retrieve_service

    class FakeTelegram:
        def __init__(self):
            self.client = MagicMock()

    ctx = ToolContext(
        telegram=FakeTelegram(), owner_id=OWNER, tz_str="UTC",
        extra={"chat_id": CHAT, "request_id": "connect-audit"},
    )
    registry = create_default_registry(ctx)
    executor = ToolExecutor(registry, ctx)

    with patch.object(
        retrieve_service, "do_retrieve",
        AsyncMock(return_value="✅ Retrieved `S0001` to this chat."),
    ) as svc:
        result = await run_tool(executor, ctx, "retrieve_save", {"save_code": "s0001"})
    svc.assert_awaited_once()
    args = svc.await_args.args
    # The tool boundary canonicalizes the model's echo to the stored
    # upper-case code form before the service sees it (service re-normalizes).
    assert args[1] == OWNER and args[2] == "S0001" and args[3] == CHAT
    assert result.success is True and "Retrieved" in result.message
    assert result.data == {"save_code": "S0001", "chat_id": CHAT}


@pytest.mark.asyncio
async def test_retrieve_save_destination_is_trusted_context_not_arguments():
    """The tool never forwards to a model-chosen destination."""
    from backend.services import retrieve_service

    class FakeTelegram:
        def __init__(self):
            self.client = MagicMock()

    ctx = ToolContext(
        telegram=FakeTelegram(), owner_id=OWNER, tz_str="UTC",
        extra={"chat_id": CHAT, "request_id": "connect-audit"},
    )
    registry = create_default_registry(ctx)
    executor = ToolExecutor(registry, ctx)

    with patch.object(
        retrieve_service, "do_retrieve", AsyncMock(return_value="✅ ok")
    ) as svc:
        await run_tool(
            executor, ctx, "retrieve_save",
            {"save_code": "S0001", "destination": 999999, "chat_id": 999999},
        )
    assert svc.await_args.args[3] == CHAT


@pytest.mark.asyncio
async def test_retrieve_save_failure_paths_are_honest():
    from backend.services import retrieve_service

    class FakeTelegram:
        def __init__(self):
            self.client = MagicMock()

    # No trusted chat in context.
    ctx_no_chat = ToolContext(
        telegram=FakeTelegram(), owner_id=OWNER, tz_str="UTC",
        extra={"request_id": "connect-audit"},
    )
    registry1 = create_default_registry(ctx_no_chat)
    executor1 = ToolExecutor(registry1, ctx_no_chat)
    result = await run_tool(executor1, ctx_no_chat, "retrieve_save", {"save_code": "S0001"})
    assert result.success is False and "destination" in result.message.lower()

    # Service failure string propagates as failure.
    ctx = ToolContext(
        telegram=FakeTelegram(), owner_id=OWNER, tz_str="UTC",
        extra={"chat_id": CHAT, "request_id": "connect-audit"},
    )
    registry2 = create_default_registry(ctx)
    executor2 = ToolExecutor(registry2, ctx)
    with patch.object(
        retrieve_service, "do_retrieve",
        AsyncMock(return_value="❌ No item found for `S9999`"),
    ):
        failed = await run_tool(executor2, ctx, "retrieve_save", {"save_code": "S9999"})
    assert failed.success is False and "No item found" in failed.message

    # Missing save code.
    with patch.object(retrieve_service, "do_retrieve", AsyncMock(return_value="✅ ok")) as svc:
        no_code = await run_tool(executor2, ctx, "retrieve_save", {})
    svc.assert_not_awaited()
    assert no_code.success is False


# ── dispatcher/provider-schema integration ──────────────────────────────────


@pytest.mark.asyncio
async def test_new_tools_are_provider_schema_visible():
    registry, _ctx, _executor = make_chain()
    from backend.ai.engine.dispatcher import Dispatcher

    dispatcher = object.__new__(Dispatcher)
    dispatcher.set_tool_registry(registry)
    names = {d["function"]["name"] for d in Dispatcher._build_tool_definitions(dispatcher)}
    for expected in ("task_list", "task_inspect", "task_transition", "retrieve_save"):
        assert expected in names


def test_no_duplicate_registrations():
    registry, _ctx, _executor = make_chain()
    names = registry.list_names()
    assert len(names) == len(set(names)) == 36
