from datetime import datetime, timezone

import pytest

from backend.ai.database.task_repository import InMemoryTaskRepository
from backend.ai.notifications import TaskNotificationService
from backend.ai.task_creation import TaskCreationError, TaskCreationService
from backend.ai.task_notifications import TaskOutcomeNotifier


REF = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)


def candidate():
    return {
        "label": "daily digest",
        "schedule_type": "daily",
        "schedule": {"hour": 13, "minute": 0, "timezone": "UTC"},
        "timezone": "UTC",
        "actions": [{"name": "account_show", "arguments": {}}],
        "notification_destination": {"kind": "owner"},
    }


@pytest.mark.asyncio
async def test_authorized_creation_persists_owner_and_next_run():
    repo = InMemoryTaskRepository()
    task = await TaskCreationService(repo, 42).create(candidate(), REF)
    assert task.owner_id == 42
    assert task.next_run_at == datetime(2026, 1, 1, 13, 0, tzinfo=timezone.utc)
    assert (await repo.get_task(7, task.id)) is None


@pytest.mark.asyncio
async def test_creation_rejects_malformed_or_unsupported_candidate():
    repo = InMemoryTaskRepository()
    with pytest.raises(TaskCreationError):
        await TaskCreationService(repo, 42).create({**candidate(), "owner_id": 99}, REF)
    with pytest.raises(TaskCreationError):
        await TaskCreationService(repo, 42).create({**candidate(), "schedule": {"hour": 2, "timezone": "Not/AZone"}}, REF)


@pytest.mark.asyncio
async def test_notifications_require_actual_persisted_status_and_isolate_sender_failure():
    repo = InMemoryTaskRepository()
    task = await TaskCreationService(repo, 42).create(candidate(), REF)
    occurrence = await repo.create_occurrence(42, {
        "task_id": task.id, "occurrence_key": "k", "definition_version": task.version,
        "action_snapshot": task.actions, "scheduled_for": task.next_run_at,
    })
    sent = []
    service = TaskNotificationService(lambda owner, message: _record(sent, owner, message), 42)
    notifier = TaskOutcomeNotifier(repo, service, 42)
    assert not await notifier.notify_persisted(task.id, "k", "succeeded")
    await repo.transition_occurrence(42, task.id, "k", "running")
    await repo.transition_occurrence(42, task.id, "k", "succeeded")
    assert await notifier.notify_persisted(task.id, "k", "succeeded")
    assert sent


async def _record(sent, owner, message):
    sent.append((owner, message))
