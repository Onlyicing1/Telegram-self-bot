from datetime import datetime, timezone

import pytest

from backend.ai.database.task_repository import InMemoryTaskRepository
from backend.ai.task_creation import TaskCreationService
from backend.ai.task_management import TaskManagementService
from backend.ai.task_management_interface import inspect_text, list_text


REF = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)


def candidate(label="task"):
    return {
        "label": label,
        "schedule_type": "daily",
        "schedule": {"hour": 13, "minute": 0, "timezone": "UTC"},
        "timezone": "UTC",
        "actions": [{"name": "account_show", "arguments": {}}],
        "notification_destination": {"kind": "owner"},
    }


@pytest.mark.asyncio
async def test_management_presentation_is_owner_scoped_and_bounded():
    repo = InMemoryTaskRepository()
    task = await TaskCreationService(repo, 42).create(candidate("visible"), REF)
    await TaskCreationService(repo, 7).create(candidate("hidden"), REF)
    service = TaskManagementService(repo, 42)
    text = await list_text(service)
    assert "visible" in text and "hidden" not in text
    detail = await inspect_text(service, task.id)
    assert f"#{task.id}" in detail
    assert await inspect_text(TaskManagementService(repo, 7), task.id) == "Task not found."


@pytest.mark.asyncio
async def test_stale_management_mutation_does_not_report_success():
    repo = InMemoryTaskRepository()
    task = await TaskCreationService(repo, 42).create(candidate(), REF)
    service = TaskManagementService(repo, 42)
    changed = await service.pause(task.id, task.version)
    assert changed is not None and changed.status == "paused"
    assert await service.resume(task.id, task.version) is None
