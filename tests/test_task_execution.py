from __future__ import annotations

import pytest
from backend.ai.database.task_repository import InMemoryTaskRepository
from backend.ai.task_execution import TaskExecutionCoordinator
from backend.ai.tools.base import PermissionLevel, ToolResult
from backend.ai.tools.context import ToolContext
from backend.ai.tools.executor import ToolExecutor
from backend.ai.tools.registry import ToolRegistry


class FakeTool:
    permission_level = PermissionLevel.READ_WRITE
    long_running = False
    safe = True
    return_type = "object"
    description = "test"
    parameters = {}

    def __init__(self, name, calls, success=True):
        self.name, self.calls, self.success = name, calls, success

    async def execute(self, context, arguments):
        self.calls.append((self.name, arguments, context.owner_id))
        return ToolResult(self.success, "ok" if self.success else "failed")


def task_data(attempt=None):
    return {
        "label": "scheduled",
        "schedule_type": "once",
        "schedule": {"at": "2026-01-01T12:00:00", "timezone": "UTC"},
        "timezone": "UTC",
        "actions": [{"name": "first", "arguments": {}}] if attempt is None else attempt,
        "notification_destination": {},
    }


@pytest.mark.asyncio
async def test_claimed_occurrence_executes_in_order_and_persists_success():
    repo = InMemoryTaskRepository()
    task = await repo.create_task(99, task_data())
    occurrence = await repo.create_occurrence(99, {
        "task_id": task.id, "occurrence_key": "k", "definition_version": task.version,
        "action_snapshot": [{"name": "first", "arguments": {}}, {"name": "second", "arguments": {}}],
        "scheduled_for": task.next_run_at or __import__("datetime").datetime(2026, 1, 1, 12, tzinfo=__import__("datetime").timezone.utc),
    })
    await repo.claim_occurrence(99, task.id, "k")
    calls = []
    registry = ToolRegistry()
    registry.register(FakeTool("first", calls))
    registry.register(FakeTool("second", calls))
    coordinator = TaskExecutionCoordinator(repo, ToolExecutor(registry, ToolContext(None, 99, "UTC")), 99, ToolContext(None, 99, "UTC"))
    result = await coordinator.execute(await repo.get_occurrence(99, task.id, "k"))
    assert result.success and result.status == "succeeded"
    assert [call[0] for call in calls] == ["first", "second"]
    assert (await repo.get_occurrence(99, task.id, "k")).status == "succeeded"


@pytest.mark.asyncio
async def test_unregistered_action_fails_without_execution():
    from datetime import datetime, timezone
    repo = InMemoryTaskRepository()
    task = await repo.create_task(7, task_data())
    await repo.create_occurrence(7, {"task_id": task.id, "occurrence_key": "k", "definition_version": 1, "action_snapshot": [{"name": "missing", "arguments": {}}], "scheduled_for": datetime.now(timezone.utc)})
    await repo.claim_occurrence(7, task.id, "k")
    registry = ToolRegistry()
    ctx = ToolContext(None, 7, "UTC")
    result = await TaskExecutionCoordinator(repo, ToolExecutor(registry, ctx), 7, ctx).execute(await repo.get_occurrence(7, task.id, "k"))
    assert not result.success
    assert (await repo.get_occurrence(7, task.id, "k")).status == "failed"


@pytest.mark.asyncio
async def test_owner_mismatch_does_not_execute():
    from datetime import datetime, timezone
    repo = InMemoryTaskRepository()
    task = await repo.create_task(1, task_data())
    occurrence = await repo.create_occurrence(1, {"task_id": task.id, "occurrence_key": "k", "definition_version": 1, "action_snapshot": [{"name": "x", "arguments": {}}], "scheduled_for": datetime.now(timezone.utc)})
    calls = []
    registry = ToolRegistry(); registry.register(FakeTool("x", calls))
    ctx = ToolContext(None, 2, "UTC")
    result = await TaskExecutionCoordinator(repo, ToolExecutor(registry, ctx), 2, ctx).execute(occurrence)
    assert not result.success and calls == []
