"""Regression tests for real (row-removing) task deletion.

The owner requirement: deleting a task must physically remove its
``ai_tasks`` row — never a ``status="deleted"`` lifecycle write — and the
AI must express deletion through the dedicated ``task_delete`` operation
(tool + structured action), not through ``task_transition``.

Every test drives the REAL chain: structured action / tool -> ToolExecutor
-> registered tool -> TaskManagementService -> repository. No live Telegram,
no live Supabase.
"""
from __future__ import annotations

import inspect
from unittest.mock import patch

import pytest

from backend.ai import actions as actions_module
from backend.ai.actions import EXECUTABLE_ACTION_NAMES, parse_action_text
from backend.ai.database import manager as dbm
from backend.ai.database.task_repository import (
    InMemoryTaskRepository,
    SupabaseTaskRepository,
)
from backend.ai.tools.context import ToolContext
from backend.ai.tools.executor import ToolExecutor
from backend.ai.tools.registry import create_default_registry
from backend.ai.tools.task_management_tools import TaskTransitionTool

OWNER = 4242


def task_data(**overrides):
    data = {
        "label": "deletable",
        "schedule_type": "interval",
        "schedule": {"seconds": 300},
        "timezone": "Asia/Tehran",
        "actions": [{"name": "send_message", "arguments": {"text": "hello"}}],
        "notification_destination": {},
    }
    data.update(overrides)
    return data


class _DownClient:
    """Supabase client whose every query fails (durable store unreachable)."""

    def table(self, name):
        raise RuntimeError("supabase unreachable")


def _manager_with(repository):
    manager = dbm.RepositoryManager(supabase_available=False)
    manager._task = repository
    return manager


async def _run_tool(manager, calls):
    ctx = ToolContext(telegram=None, owner_id=OWNER, tz_str="UTC", extra={})
    registry = create_default_registry(ctx)
    executor = ToolExecutor(registry, ctx)
    with patch.object(dbm, "get_repository_manager", return_value=manager):
        return await executor.execute_calls(calls, owner_id=OWNER)


# ── the dedicated tool really removes the row ───────────────────────────────


@pytest.mark.asyncio
async def test_task_delete_tool_removes_the_durable_row_through_the_executor():
    repo = InMemoryTaskRepository()
    manager = _manager_with(repo)
    doomed = await repo.create_task(OWNER, task_data())
    await repo.create_occurrence(
        OWNER,
        {
            "task_id": doomed.id,
            "occurrence_key": "2026-01-01T12:00:00+00:00",
            "definition_version": doomed.version,
            "action_snapshot": doomed.actions,
            "scheduled_for": "2026-01-01T12:00:00+00:00",
        },
    )

    results = await _run_tool(
        manager,
        [{"name": "task_delete", "arguments": {"task_id": doomed.id, "expected_version": doomed.version}}],
    )

    assert results[0].success is True
    assert results[0].data["deleted"] is True
    assert results[0].data["durable"] is True
    # The row (and its occurrences) are physically gone.
    assert await repo.get_task(OWNER, doomed.id) is None
    assert await repo.list_occurrences(OWNER, doomed.id) == []
    # And the next list no longer contains it.
    listed = await _run_tool(manager, [{"name": "task_list", "arguments": {}}])
    assert listed[0].data["task_ids"] == []


@pytest.mark.asyncio
async def test_task_delete_tool_reports_stale_and_missing_honestly():
    repo = InMemoryTaskRepository()
    manager = _manager_with(repo)
    task = await repo.create_task(OWNER, task_data())

    stale = await _run_tool(
        manager,
        [{"name": "task_delete", "arguments": {"task_id": task.id, "expected_version": 999}}],
    )
    assert stale[0].success is False and "stale" in stale[0].message
    assert await repo.get_task(OWNER, task.id) is not None

    missing = await _run_tool(
        manager,
        [{"name": "task_delete", "arguments": {"task_id": 99999, "expected_version": 1}}],
    )
    assert missing[0].success is False and "not found" in missing[0].message


@pytest.mark.asyncio
async def test_task_delete_tool_never_reports_a_degraded_delete_as_durable():
    fallback = InMemoryTaskRepository()
    local = await fallback.create_task(OWNER, task_data())
    manager = _manager_with(SupabaseTaskRepository(_DownClient(), fallback))

    results = await _run_tool(
        manager,
        [{"name": "task_delete", "arguments": {"task_id": local.id, "expected_version": local.version}}],
    )

    assert results[0].success is True
    assert results[0].data["durable"] is False
    assert results[0].data["fallback_backend"] == "InMemoryTaskRepository"
    assert "Memory fallback" in results[0].message


@pytest.mark.asyncio
async def test_task_delete_tool_cannot_claim_deletion_when_the_durable_store_is_down():
    fallback = InMemoryTaskRepository()  # holds no matching task
    manager = _manager_with(SupabaseTaskRepository(_DownClient(), fallback))

    results = await _run_tool(
        manager,
        [{"name": "task_delete", "arguments": {"task_id": 7, "expected_version": 1}}],
    )

    assert results[0].success is False
    assert "durable" in results[0].message


# ── task_transition is no longer a deletion path ────────────────────────────


@pytest.mark.asyncio
async def test_task_transition_tool_rejects_deleted():
    repo = InMemoryTaskRepository()
    manager = _manager_with(repo)
    task = await repo.create_task(OWNER, task_data())
    ctx = ToolContext(telegram=None, owner_id=OWNER, tz_str="UTC", extra={})

    with patch.object(dbm, "get_repository_manager", return_value=manager):
        result = await TaskTransitionTool(ctx).execute(
            ctx,
            {"task_id": task.id, "action": "deleted", "expected_version": task.version},
        )

    assert result.success is False
    assert "task_delete" in result.message
    # The task is untouched: still active, still present.
    stored = await repo.get_task(OWNER, task.id)
    assert stored is not None and stored.status == "active"


def test_transition_tool_schema_has_no_deleted_status():
    ctx = ToolContext(telegram=None, owner_id=OWNER, tz_str="UTC", extra={})
    tool = TaskTransitionTool(ctx)
    assert "deleted" not in tool.parameters["action"]["enum"]


# ── the structured action path resolves task_delete end to end ──────────────


@pytest.mark.asyncio
async def test_structured_task_delete_action_reaches_the_repository():
    assert "task_delete" in EXECUTABLE_ACTION_NAMES
    parsed = parse_action_text(
        '{"action":"task_delete","task_id":3,"expected_version":1}'
    )
    assert parsed.kind == "executable"
    assert parsed.tool_calls == [
        {"name": "task_delete", "arguments": {"task_id": 3, "expected_version": 1}}
    ]

    repo = InMemoryTaskRepository()
    manager = _manager_with(repo)
    task = await repo.create_task(OWNER, task_data())
    results = await _run_tool(
        manager,
        [{"name": "task_delete", "arguments": {"task_id": task.id, "expected_version": task.version}}],
    )
    assert results[0].success is True
    assert await repo.get_task(OWNER, task.id) is None


def test_prompt_contract_advertises_task_delete_not_a_deleted_transition():
    from backend.ai.prompt import template as prompt_template

    source = inspect.getsource(prompt_template)
    assert "task_delete" in source
    assert '"action":"task_transition","task_id":3,"action_status":"deleted"' not in source


def test_registry_exposes_the_dedicated_delete_tool():
    ctx = ToolContext(telegram=None, owner_id=OWNER, tz_str="UTC", extra={})
    registry = create_default_registry(ctx)
    names = set(registry.list_names())
    assert "task_delete" in names
    assert "task_transition" in names


def test_action_vocabulary_keeps_transition_statuses_and_adds_delete():
    assert actions_module._TASK_TRANSITION_STATUS_VOCABULARY == {"paused", "active", "completed"}
    assert "deleted" not in actions_module._TASK_LIST_STATUS_VOCABULARY
