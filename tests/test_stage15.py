from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from backend.ai.database.task_repository import InMemoryTaskRepository
from backend.ai.task_scheduler import TaskScheduler


NOW = datetime(2030, 1, 1, 12, tzinfo=timezone.utc)


def task_data(next_run=NOW):
    return {
        "label": "scheduled",
        "schedule_type": "once",
        "schedule": {"at": "2030-01-01T12:00:00", "timezone": "UTC"},
        "timezone": "UTC",
        "actions": [{"name": "safe", "arguments": {}}],
        "notification_destination": {},
        "next_run_at": next_run,
    }


class Coordinator:
    def __init__(self):
        self.seen = []

    async def execute(self, occurrence):
        self.seen.append(occurrence)


@pytest.mark.asyncio
async def test_due_occurrence_is_claimed_before_execution():
    repo = InMemoryTaskRepository()
    task = await repo.create_task(7, task_data())
    coordinator = Coordinator()
    scheduler = TaskScheduler(repo, 7, coordinator)

    assert await scheduler.run_once(NOW) == 1
    assert len(coordinator.seen) == 1
    assert coordinator.seen[0].status == "running"
    assert coordinator.seen[0].owner_id == 7


@pytest.mark.asyncio
async def test_duplicate_wake_does_not_execute_same_once_twice():
    repo = InMemoryTaskRepository()
    await repo.create_task(7, task_data())
    coordinator = Coordinator()
    scheduler = TaskScheduler(repo, 7, coordinator)

    await scheduler.run_once(NOW)
    await scheduler.run_once(NOW)
    assert len(coordinator.seen) == 1


@pytest.mark.asyncio
async def test_retry_pending_waits_until_retry_at_then_is_claimed():
    repo = InMemoryTaskRepository()
    task = await repo.create_task(7, task_data(next_run=None))
    occurrence = await repo.create_occurrence(7, {
        "task_id": task.id,
        "occurrence_key": "retry-1",
        "definition_version": 1,
        "action_snapshot": task.actions,
        "scheduled_for": NOW,
    })
    await repo.claim_occurrence(7, task.id, occurrence.occurrence_key)
    await repo.transition_occurrence(
        7, task.id, occurrence.occurrence_key, "retry_pending",
        retry_at=NOW + timedelta(seconds=30),
    )
    coordinator = Coordinator()
    scheduler = TaskScheduler(repo, 7, coordinator)

    assert await scheduler.run_once(NOW) == 0
    assert await scheduler.run_once(NOW + timedelta(seconds=30)) == 1
    assert len(coordinator.seen) == 1


@pytest.mark.asyncio
async def test_retry_owner_isolation():
    repo = InMemoryTaskRepository()
    task = await repo.create_task(7, task_data(next_run=None))
    occurrence = await repo.create_occurrence(7, {
        "task_id": task.id,
        "occurrence_key": "retry-2",
        "definition_version": 1,
        "action_snapshot": task.actions,
        "scheduled_for": NOW,
    })
    await repo.claim_occurrence(7, task.id, occurrence.occurrence_key)
    await repo.transition_occurrence(7, task.id, occurrence.occurrence_key, "retry_pending", retry_at=NOW)
    coordinator = Coordinator()
    assert await TaskScheduler(repo, 8, coordinator).run_once(NOW) == 0
    assert coordinator.seen == []


def test_scheduler_does_not_execute_action_json_directly():
    source = open("backend/ai/task_scheduler.py", encoding="utf-8").read()
    assert "execute_calls" not in source
    assert "execute_calls" not in source
    assert "execution_coordinator" in source
