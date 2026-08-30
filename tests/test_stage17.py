from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from backend.ai.database.task_repository import InMemoryTaskRepository
from backend.ai.task_execution import TaskExecutionCoordinator
from backend.ai.task_scheduler import TaskScheduler
from backend.ai.tools.base import PermissionLevel, ToolResult
from backend.ai.tools.context import ToolContext
from backend.ai.tools.executor import ToolExecutor
from backend.ai.tools.registry import ToolRegistry


NOW = datetime(2030, 1, 1, 12, tzinfo=timezone.utc)


def task_data(next_run=None):
    return {
        "label": "retry",
        "schedule_type": "once",
        "schedule": {"at": "2030-01-01T12:00:00", "timezone": "UTC"},
        "timezone": "UTC",
        "actions": [{"name": "flaky", "arguments": {}}],
        "notification_destination": {},
        "next_run_at": next_run,
    }


class FailingTool:
    permission_level = PermissionLevel.READ_WRITE
    long_running = False
    safe = True
    return_type = "object"
    description = "test"
    parameters = {}

    def __init__(self, error="timeout"):
        self.name = "flaky"
        self.error = error
        self.calls = 0

    async def execute(self, context, arguments):
        self.calls += 1
        if isinstance(self.error, BaseException):
            raise self.error
        return ToolResult(False, self.error)


def coordinator(repo, owner, tool):
    registry = ToolRegistry()
    registry.register(tool)
    context = ToolContext(None, owner, "UTC")
    return TaskExecutionCoordinator(repo, ToolExecutor(registry, context), owner, context)


async def running_occurrence(repo, owner=7, attempt=1):
    task = await repo.create_task(owner, task_data())
    occurrence = await repo.create_occurrence(owner, {
        "task_id": task.id,
        "occurrence_key": "retry-key",
        "definition_version": task.version,
        "action_snapshot": task.actions,
        "scheduled_for": NOW,
        "attempt": attempt,
    })
    return task, await repo.claim_occurrence(owner, task.id, occurrence.occurrence_key)


@pytest.mark.asyncio
async def test_normal_tool_result_failure_enters_durable_retry_pending():
    repo = InMemoryTaskRepository()
    task, occurrence = await running_occurrence(repo)
    result = await coordinator(repo, 7, FailingTool("timeout")).execute(occurrence)

    assert result.status == "retry_pending"
    stored = await repo.get_occurrence(7, task.id, occurrence.occurrence_key)
    assert stored.status == "retry_pending"
    assert stored.attempt == 2
    assert stored.retry_at is not None
    assert stored.error_metadata["error_class"] == "timeout"


@pytest.mark.asyncio
async def test_normal_executor_exception_enters_durable_retry_pending():
    repo = InMemoryTaskRepository()
    task, occurrence = await running_occurrence(repo)
    result = await coordinator(repo, 7, FailingTool(TimeoutError("slow"))).execute(occurrence)

    assert result.status == "retry_pending"
    stored = await repo.get_occurrence(7, task.id, occurrence.occurrence_key)
    assert stored.status == "retry_pending"
    assert stored.attempt == 2
    assert stored.retry_at is not None


@pytest.mark.asyncio
async def test_final_retryable_attempt_becomes_terminal_failed():
    repo = InMemoryTaskRepository()
    task, occurrence = await running_occurrence(repo, attempt=3)
    result = await coordinator(repo, 7, FailingTool("timeout")).execute(occurrence)

    assert result.status == "failed"
    stored = await repo.get_occurrence(7, task.id, occurrence.occurrence_key)
    assert stored.status == "failed"
    assert stored.attempt == 3
    assert stored.retry_at is None


@pytest.mark.asyncio
async def test_permanent_and_unknown_failures_are_terminal():
    for error in ("invalid payload", "unsupported action"):
        repo = InMemoryTaskRepository()
        task, occurrence = await running_occurrence(repo)
        result = await coordinator(repo, 7, FailingTool(error)).execute(occurrence)
        assert result.status == "failed"
        assert (await repo.get_occurrence(7, task.id, occurrence.occurrence_key)).status == "failed"


@pytest.mark.asyncio
async def test_cancellation_propagates_and_is_not_persisted_as_retry():
    repo = InMemoryTaskRepository()
    task, occurrence = await running_occurrence(repo)
    with pytest.raises(asyncio.CancelledError):
        await coordinator(repo, 7, FailingTool(asyncio.CancelledError())).execute(occurrence)
    stored = await repo.get_occurrence(7, task.id, occurrence.occurrence_key)
    assert stored.status == "running"


@pytest.mark.asyncio
async def test_existing_scheduler_picks_up_durable_retry_without_duplicate_execution():
    repo = InMemoryTaskRepository()
    task, occurrence = await running_occurrence(repo)
    first_tool = FailingTool("timeout")
    first = await coordinator(repo, 7, first_tool).execute(occurrence)
    assert first.status == "retry_pending"

    second_tool = FailingTool("invalid payload")
    scheduler = TaskScheduler(repo, 7, coordinator(repo, 7, second_tool))
    stored = await repo.get_occurrence(7, task.id, occurrence.occurrence_key)
    assert await scheduler.run_once(stored.retry_at) == 1
    assert second_tool.calls == 1
    assert (await repo.get_occurrence(7, task.id, occurrence.occurrence_key)).status == "failed"
    assert await scheduler.run_once(stored.retry_at) == 0
