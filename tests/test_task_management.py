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
