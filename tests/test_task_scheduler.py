from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from backend.ai.database.task_repository import InMemoryTaskRepository
from backend.ai.scheduling import ScheduleError, OneShotState, one_shot_state
from backend.ai.task_scheduler import TaskScheduler, occurrence_key


def task_data(**overrides):
    value = {"label": "Hourly", "schedule_type": "interval", "schedule": {"seconds": 3600}, "timezone": "UTC", "next_run_at": datetime(2026, 8, 29, 9, tzinfo=timezone.utc), "actions": [{"name": "safe", "arguments": {}}], "notification_destination": {"chat_id": 1}}
    value.update(overrides); return value


@pytest.mark.asyncio
async def test_due_active_task_creates_one_idempotent_occurrence_and_advances():
    repo = InMemoryTaskRepository(); task = await repo.create_task(1, task_data())
    scheduler = TaskScheduler(repo, 1); now = datetime(2026, 8, 29, 9, 5, tzinfo=timezone.utc)
    assert await scheduler.run_once(now) == 1
    assert await scheduler.run_once(now) == 0
    rows = await repo.list_occurrences(1, task.id); assert len(rows) == 1
    assert (await repo.get_task(1, task.id)).next_run_at == datetime(2026, 8, 29, 10, tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_due_discovery_filters_owner_status_and_orders_bounded():
    repo = InMemoryTaskRepository()
    await repo.create_task(1, task_data(label="later", next_run_at=datetime(2026, 8, 29, 10, tzinfo=timezone.utc)))
    await repo.create_task(1, task_data(label="paused", status="paused"))
    await repo.create_task(1, task_data(label="due", next_run_at=datetime(2026, 8, 29, 8, tzinfo=timezone.utc)))
    await repo.create_task(2, task_data(label="other"))
    due = await repo.list_due_tasks(1, datetime(2026, 8, 29, 9, tzinfo=timezone.utc))
    assert [item.label for item in due] == ["due"]


@pytest.mark.asyncio
async def test_snapshot_and_version_are_preserved():
    repo = InMemoryTaskRepository(); task = await repo.create_task(1, task_data())
    scheduler = TaskScheduler(repo, 1); await scheduler.run_once(datetime(2026, 8, 29, 9, 1, tzinfo=timezone.utc))
    await repo.update_task(1, task.id, 1, {"actions": [{"name": "changed", "arguments": {}}]})
    row = (await repo.list_occurrences(1, task.id))[0]
    assert row.definition_version == 1 and row.action_snapshot[0]["name"] == "safe"


@pytest.mark.asyncio
async def test_recovery_resolves_claimed_and_running_occurrences():
    repo = InMemoryTaskRepository(); task = await repo.create_task(1, task_data())
    claimed = await repo.create_occurrence(1, {"task_id": task.id, "occurrence_key": "claimed", "definition_version": 1, "action_snapshot": task.actions, "scheduled_for": task.next_run_at})
    running = await repo.create_occurrence(1, {"task_id": task.id, "occurrence_key": "running", "definition_version": 1, "action_snapshot": task.actions, "scheduled_for": task.next_run_at})
    await repo.claim_occurrence(1, task.id, running.occurrence_key)
    scheduler = TaskScheduler(repo, 1); assert await scheduler.recover() == 2
    for key in (claimed.occurrence_key, running.occurrence_key):
        resolved = await repo.get_occurrence(1, task.id, key)
        assert resolved.status == "retry_pending"
        assert resolved.attempt == 2 and resolved.retry_at is not None
        assert resolved.finished_at is None
        assert resolved.error_metadata.get("error_class") == "restart_interrupted"


@pytest.mark.asyncio
async def test_preexisting_interrupted_occurrence_is_resolved_once():
    repo = InMemoryTaskRepository(); task = await repo.create_task(1, task_data())
    await repo.create_occurrence(1, {"task_id": task.id, "occurrence_key": "k", "definition_version": 1, "action_snapshot": task.actions, "scheduled_for": task.next_run_at, "status": "interrupted"})
    scheduler = TaskScheduler(repo, 1)
    assert await scheduler.recover() == 1
    resolved = await repo.get_occurrence(1, task.id, "k")
    assert resolved.status == "retry_pending" and resolved.attempt == 2
    assert await scheduler.recover() == 0


@pytest.mark.asyncio
async def test_recovery_resolves_interrupted_at_attempt_limit_to_failed_without_fourth_attempt():
    repo = InMemoryTaskRepository(); task = await repo.create_task(1, task_data())
    row = await repo.create_occurrence(1, {"task_id": task.id, "occurrence_key": "k", "definition_version": 1, "action_snapshot": task.actions, "scheduled_for": task.next_run_at, "attempt": 3})
    await repo.claim_occurrence(1, task.id, row.occurrence_key)
    scheduler = TaskScheduler(repo, 1); assert await scheduler.recover() == 1
    resolved = await repo.get_occurrence(1, task.id, "k")
    assert resolved.status == "failed"
    assert resolved.attempt == 3
    assert resolved.finished_at is not None
    assert resolved.retry_at is None
    assert resolved.error_metadata.get("error_class") == "restart_interrupted"
    assert await scheduler.recover() == 0


@pytest.mark.asyncio
async def test_recovered_retry_pending_occurrence_is_picked_up_by_run_once():
    repo = InMemoryTaskRepository(); task = await repo.create_task(1, task_data())
    row = await repo.create_occurrence(1, {"task_id": task.id, "occurrence_key": "k", "definition_version": 1, "action_snapshot": task.actions, "scheduled_for": task.next_run_at})
    await repo.claim_occurrence(1, task.id, row.occurrence_key)
    scheduler = TaskScheduler(repo, 1, execution_coordinator=None)
    await scheduler.recover()
    later = datetime.now(timezone.utc) + timedelta(seconds=60)
    assert await scheduler.run_once(later) == 1
    final = await repo.get_occurrence(1, task.id, "k")
    assert final.status == "retry_pending"
    due = await repo.list_due_retry_occurrences(1, later, 10)
    assert [o.occurrence_key for o in due] == ["k"]


@pytest.mark.asyncio
async def test_recovered_occurrence_is_claimed_and_executed_once():
    repo = InMemoryTaskRepository(); task = await repo.create_task(1, task_data(next_run_at=None))
    scheduled_for = datetime.now(timezone.utc)
    row = await repo.create_occurrence(1, {"task_id": task.id, "occurrence_key": "k", "definition_version": 1, "action_snapshot": task.actions, "scheduled_for": scheduled_for})
    await repo.claim_occurrence(1, task.id, row.occurrence_key)

    class Coordinator:
        def __init__(self):
            self.calls = 0

        async def execute(self, occurrence):
            self.calls += 1
            await repo.transition_occurrence(1, occurrence.task_id, occurrence.occurrence_key, "succeeded")
            return SimpleNamespace(status="succeeded")

    coordinator = Coordinator()
    scheduler = TaskScheduler(repo, 1, execution_coordinator=coordinator)
    await scheduler.recover()
    recovered = await repo.get_occurrence(1, task.id, "k")
    assert await scheduler.run_once(recovered.retry_at) == 1
    assert await scheduler.run_once(recovered.retry_at) == 0
    assert coordinator.calls == 1
    assert (await repo.get_occurrence(1, task.id, "k")).status == "succeeded"


@pytest.mark.asyncio
async def test_recovery_is_owner_scoped():
    repo = InMemoryTaskRepository()
    owner_task = await repo.create_task(1, task_data())
    other_task = await repo.create_task(2, task_data())
    await repo.create_occurrence(1, {"task_id": owner_task.id, "occurrence_key": "owner", "definition_version": 1, "action_snapshot": owner_task.actions, "scheduled_for": owner_task.next_run_at})
    await repo.create_occurrence(2, {"task_id": other_task.id, "occurrence_key": "other", "definition_version": 1, "action_snapshot": other_task.actions, "scheduled_for": other_task.next_run_at})
    scheduler = TaskScheduler(repo, 1)
    assert await scheduler.recover() == 1
    assert (await repo.get_occurrence(1, owner_task.id, "owner")).status == "retry_pending"
    assert (await repo.get_occurrence(2, other_task.id, "other")).status == "claimed"


@pytest.mark.asyncio
async def test_scheduler_lifecycle_is_idempotent_and_cancel_safe():
    scheduler = TaskScheduler(InMemoryTaskRepository(), 1)
    await scheduler.start(); first = scheduler._task; await scheduler.start(); assert scheduler._task is first
    await scheduler.stop(); await scheduler.stop(); assert not scheduler.running


def test_occurrence_key_and_one_shot_grace_are_deterministic():
    instant = datetime(2026, 8, 29, 9, tzinfo=timezone.utc)
    assert occurrence_key(4, instant) == "4:2026-08-29T09:00:00+00:00"
    assert one_shot_state(instant, instant, __import__("datetime").timedelta(minutes=5)) == OneShotState.DUE
    with pytest.raises(ScheduleError): occurrence_key(4, datetime(2026, 8, 29, 9))
