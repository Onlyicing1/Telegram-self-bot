from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from backend.ai.database.task_repository import InMemoryTaskRepository
from backend.ai.notifications import TaskNotification, TaskNotificationService
from backend.ai.task_notifications import TaskOutcomeNotifier
from backend.ai.task_scheduler import TaskScheduler


NOW = datetime(2030, 1, 1, 12, tzinfo=timezone.utc)


def task_data(next_run=NOW):
    return {
        "label": "notify",
        "schedule_type": "once",
        "schedule": {"at": "2030-01-01T12:00:00", "timezone": "UTC"},
        "timezone": "UTC",
        "actions": [{"name": "safe", "arguments": {}}],
        "notification_destination": {"kind": "owner"},
        "next_run_at": next_run,
    }


class SimpleResult:
    def __init__(self, status):
        self.status = status


class PersistingCoordinator:
    """Mimics TaskExecutionCoordinator: persists the outcome, then returns it."""

    def __init__(self, status="succeeded", retry_in=30):
        self.status = status
        self.retry_in = retry_in
        self.seen = []

    async def execute(self, occurrence):
        self.seen.append(occurrence)
        if self.status == "succeeded":
            await self.repo.transition_occurrence(
                occurrence.owner_id, occurrence.task_id,
                occurrence.occurrence_key, "succeeded",
            )
        elif self.status == "failed":
            await self.repo.transition_occurrence(
                occurrence.owner_id, occurrence.task_id,
                occurrence.occurrence_key, "failed",
                error_metadata={"error_class": "boom", "attempt": occurrence.attempt},
            )
        elif self.status == "retry_pending":
            await self.repo.transition_occurrence(
                occurrence.owner_id, occurrence.task_id,
                occurrence.occurrence_key, "retry_pending",
                retry_at=occurrence.updated_at + timedelta(seconds=self.retry_in),
                error_metadata={"error_class": "transient", "attempt": occurrence.attempt},
            )
        elif self.status == "cancelled":
            await self.repo.transition_occurrence(
                occurrence.owner_id, occurrence.task_id,
                occurrence.occurrence_key, "cancelled",
            )
        return SimpleResult(self.status)


def make_scheduler(repo, status, sent, owner=7, sender_fail=False):
    async def sender(owner_id, message):
        if sender_fail:
            raise RuntimeError("telegram down")
        sent.append((owner_id, message))
        return {}

    service = TaskNotificationService(sender, owner)
    notifier = TaskOutcomeNotifier(repo, service, owner)
    coordinator = PersistingCoordinator(status)
    coordinator.repo = repo
    scheduler = TaskScheduler(repo, owner, coordinator, notifier)
    return scheduler, coordinator


@pytest.mark.asyncio
async def test_persisted_succeeded_notifies():
    repo = InMemoryTaskRepository()
    await repo.create_task(7, task_data())
    sent = []
    scheduler, _ = make_scheduler(repo, "succeeded", sent)
    assert await scheduler.run_once(NOW) == 1
    assert len(sent) == 1
    owner, message = sent[0]
    assert owner == 7
    assert "succeeded" in message


@pytest.mark.asyncio
async def test_persisted_failed_notifies():
    repo = InMemoryTaskRepository()
    await repo.create_task(7, task_data())
    sent = []
    scheduler, _ = make_scheduler(repo, "failed", sent)
    assert await scheduler.run_once(NOW) == 1
    assert len(sent) == 1
    assert "failed" in sent[0][1]


@pytest.mark.asyncio
async def test_persisted_retry_pending_notifies_with_retry_time():
    repo = InMemoryTaskRepository()
    await repo.create_task(7, task_data())
    sent = []
    scheduler, _ = make_scheduler(repo, "retry_pending", sent)
    assert await scheduler.run_once(NOW) == 1
    assert len(sent) == 1
    assert "retry" in sent[0][1]


@pytest.mark.asyncio
async def test_persisted_cancelled_notifies():
    repo = InMemoryTaskRepository()
    task = await repo.create_task(7, task_data())
    occurrence = await repo.create_occurrence(7, {
        "task_id": task.id, "occurrence_key": "c1", "definition_version": task.version,
        "action_snapshot": task.actions, "scheduled_for": NOW,
    })
    sent = []
    scheduler, _ = make_scheduler(repo, "cancelled", sent)
    await scheduler._execute_claimed(occurrence)
    assert len(sent) == 1
    assert "cancelled" in sent[0][1]


@pytest.mark.asyncio
async def test_no_notification_when_outcome_not_persisted():
    class NoReadRepo(InMemoryTaskRepository):
        async def get_occurrence(self, owner_id, task_id, occurrence_key):
            raise RuntimeError("db down")

    repo = NoReadRepo()
    await repo.create_task(7, task_data())
    sent = []
    scheduler, _ = make_scheduler(repo, "succeeded", sent)
    # Notifier cannot verify persistence -> must not send; scheduler still counts it.
    assert await scheduler.run_once(NOW) == 1
    assert sent == []


@pytest.mark.asyncio
async def test_no_notification_when_persisted_status_differs():
    repo = InMemoryTaskRepository()
    await repo.create_task(7, task_data())
    sent = []

    # Coordinator reports succeeded but persists failed -> verifier must reject.
    class LyingCoordinator(PersistingCoordinator):
        async def execute(self, occurrence):
            self.seen.append(occurrence)
            await self.repo.transition_occurrence(
                occurrence.owner_id, occurrence.task_id,
                occurrence.occurrence_key, "failed",
                error_metadata={"error_class": "boom"},
            )
            return SimpleResult("succeeded")

    async def sender(owner_id, message):
        sent.append((owner_id, message))
        return {}

    service = TaskNotificationService(sender, 7)
    notifier = TaskOutcomeNotifier(repo, service, 7)
    coordinator = LyingCoordinator()
    coordinator.repo = repo
    scheduler = TaskScheduler(repo, 7, coordinator, notifier)
    await scheduler.run_once(NOW)
    assert sent == []


@pytest.mark.asyncio
async def test_sender_failure_isolated_and_state_untouched():
    repo = InMemoryTaskRepository()
    task = await repo.create_task(7, task_data())
    sent = []
    scheduler, coordinator = make_scheduler(repo, "succeeded", sent, sender_fail=True)
    assert await scheduler.run_once(NOW) == 1
    assert sent == []
    occurrence = await repo.get_occurrence(7, task.id, coordinator.seen[0].occurrence_key)
    assert occurrence is not None
    assert occurrence.status == "succeeded"


@pytest.mark.asyncio
async def test_sender_cancellation_propagates():
    repo = InMemoryTaskRepository()
    await repo.create_task(7, task_data())

    async def cancelling_sender(owner_id, message):
        raise asyncio.CancelledError()

    service = TaskNotificationService(cancelling_sender, 7)
    notifier = TaskOutcomeNotifier(repo, service, 7)
    coordinator = PersistingCoordinator("succeeded")
    coordinator.repo = repo
    scheduler = TaskScheduler(repo, 7, coordinator, notifier)
    with pytest.raises(asyncio.CancelledError):
        await scheduler.run_once(NOW)


@pytest.mark.asyncio
async def test_owner_mismatch_notification_rejected():
    sent = []

    async def sender(owner_id, message):
        sent.append((owner_id, message))
        return {}

    service = TaskNotificationService(sender, 7)
    ok = await service.send(TaskNotification(
        owner_id=99, task_id=1, occurrence_key="k", kind="succeeded", message="x",
    ))
    assert ok is False
    assert sent == []


@pytest.mark.asyncio
async def test_duplicate_wake_does_not_duplicate_notification():
    repo = InMemoryTaskRepository()
    await repo.create_task(7, task_data())
    sent = []
    scheduler, _ = make_scheduler(repo, "succeeded", sent)
    assert await scheduler.run_once(NOW) == 1
    assert await scheduler.run_once(NOW) == 0
    assert len(sent) == 1


@pytest.mark.asyncio
async def test_retry_pending_not_notified_on_rediscovery_only_on_reexecution():
    repo = InMemoryTaskRepository()
    task = await repo.create_task(7, task_data(next_run=None))
    occurrence = await repo.create_occurrence(7, {
        "task_id": task.id, "occurrence_key": "r1", "definition_version": task.version,
        "action_snapshot": task.actions, "scheduled_for": NOW,
    })
    await repo.claim_occurrence(7, task.id, occurrence.occurrence_key)
    await repo.transition_occurrence(
        7, task.id, occurrence.occurrence_key, "retry_pending",
        retry_at=NOW + timedelta(seconds=30),
        error_metadata={"error_class": "transient", "attempt": 1},
    )
    sent = []
    scheduler, coordinator = make_scheduler(repo, "succeeded", sent)

    # Not yet due: retry_pending rediscovered but no notification.
    assert await scheduler.run_once(NOW) == 0
    assert sent == []

    # Due: reclaimed, re-executed to success; exactly one notification for the
    # new persisted outcome (not a repeat of retry_pending).
    assert await scheduler.run_once(NOW + timedelta(seconds=30)) == 1
    assert len(sent) == 1
    assert "succeeded" in sent[0][1]
    assert len(coordinator.seen) == 1


@pytest.mark.asyncio
async def test_notification_does_not_execute_actions_or_mutate_state():
    repo = InMemoryTaskRepository()
    task = await repo.create_task(7, task_data())
    occurrence = await repo.create_occurrence(7, {
        "task_id": task.id, "occurrence_key": "n1", "definition_version": task.version,
        "action_snapshot": task.actions, "scheduled_for": NOW,
    })
    await repo.transition_occurrence(7, task.id, "n1", "running")
    await repo.transition_occurrence(7, task.id, "n1", "succeeded")

    sent = []

    async def sender(owner_id, message):
        sent.append((owner_id, message))
        return {}

    notifier = TaskOutcomeNotifier(repo, TaskNotificationService(sender, 7), 7)
    assert await notifier.notify_persisted(task.id, "n1", "succeeded") is True
    assert len(sent) == 1

    after = await repo.get_occurrence(7, task.id, "n1")
    assert after.status == "succeeded"
    assert after.action_snapshot == task.actions


def test_supervisor_wires_notification_transport_with_lazy_client():
    """Source-level guard: supervisor constructs the notification stack and the
    sender resolves the CURRENT self client at call time (rebuild-safe)."""
    source = Path("backend/runtime/supervisor.py").read_text()
    assert "TaskNotificationService(_notify_sender, self.owner_id)" in source
    assert "TaskOutcomeNotifier(" in source
    assert "TelegramAPI(self.client).send_message(owner, message)" in source
    scheduler_src = Path("backend/ai/task_scheduler.py").read_text()
    assert "outcome_notifier" in scheduler_src
    assert "notify_persisted" in scheduler_src
