from __future__ import annotations

from datetime import datetime, timezone

import pytest

from backend.ai.database.task_repository import InMemoryTaskRepository
from backend.ai.notifications import TaskNotification, TaskNotificationService
from backend.ai.task_management import TaskManagementService


def data():
    return {
        "label": "managed",
        "schedule_type": "once",
        "schedule": {"at": "2026-01-01T12:00:00", "timezone": "UTC"},
        "timezone": "UTC",
        "actions": [{"name": "safe", "arguments": {}}],
        "notification_destination": {},
    }


@pytest.mark.asyncio
async def test_management_is_owner_scoped_and_uses_cas():
    repo = InMemoryTaskRepository()
    task = await repo.create_task(1, data())
    service = TaskManagementService(repo, 1)
    assert await service.inspect(task.id) is not None
    assert await TaskManagementService(repo, 2).inspect(task.id) is None
    assert await service.pause(task.id, expected_version=999) is None
    paused = await service.pause(task.id, expected_version=1)
    assert paused.status == "paused" and paused.version == 2
    assert await service.resume(task.id, expected_version=1) is None


@pytest.mark.asyncio
async def test_delete_removes_the_row_and_its_occurrences():
    repo = InMemoryTaskRepository()
    task = await repo.create_task(1, data())
    await repo.create_occurrence(
        1,
        {
            "task_id": task.id,
            "occurrence_key": "2026-01-01T12:00:00+00:00",
            "definition_version": task.version,
            "action_snapshot": task.actions,
            "scheduled_for": "2026-01-01T12:00:00+00:00",
        },
    )
    service = TaskManagementService(repo, 1)

    result = await service.delete(task.id, expected_version=task.version)
    assert result.deleted is True
    assert result.task_id == task.id

    # The row is really gone — not a hidden "deleted" status — so the normal
    # list and inspection both lose it, and its occurrences went with it.
    assert await repo.get_task(1, task.id) is None
    assert await service.inspect(task.id) is None
    assert [t.id for t in await service.list_tasks()] == []
    assert await repo.list_occurrences(1, task.id) == []


@pytest.mark.asyncio
async def test_delete_respects_cas_owner_and_leaves_the_row_on_failure():
    repo = InMemoryTaskRepository()
    task = await repo.create_task(1, data())
    service = TaskManagementService(repo, 1)

    # Stale version fails safely; the row is untouched.
    stale = await service.delete(task.id, expected_version=999)
    assert stale.outcome == "stale" and stale.deleted is False
    assert (await repo.get_task(1, task.id)).status == "active"

    # Foreign owner can neither read nor delete.
    foreign = TaskManagementService(repo, 2)
    missing = await foreign.delete(task.id, expected_version=task.version)
    assert missing.outcome == "not_found" and missing.deleted is False
    assert (await repo.get_task(1, task.id)) is not None

    # A real delete removes the task, so nothing can reactivate or inspect it.
    assert (await service.delete(task.id, expected_version=task.version)).deleted is True
    assert await repo.get_task(1, task.id) is None
    assert await service.resume(task.id, expected_version=task.version + 1) is None


@pytest.mark.asyncio
async def test_explicit_status_filter_still_matches_exact_status():
    repo = InMemoryTaskRepository()
    task = await repo.create_task(1, data())
    await repo.transition_task(1, task.id, "completed", expected_version=task.version)
    service = TaskManagementService(repo, 1)
    assert [t.id for t in await service.list_tasks(status="completed")] == [task.id]
    assert [t.id for t in await service.list_tasks(status="deleted")] == []


@pytest.mark.asyncio
async def test_notification_failure_is_isolated_and_owner_checked():
    sent = []
    async def sender(owner, message):
        sent.append((owner, message))
        raise RuntimeError("transport down")
    service = TaskNotificationService(sender, 1)
    notification = TaskNotification(1, 4, "k", "failed", "failure")
    assert await service.send(notification) is False
    assert sent == [(1, "failure")]
    assert await service.send(TaskNotification(2, 4, "k", "failed", "x")) is False
    assert await service.send(TaskNotification(1, 4, "unknown", "x", "x")) is False
