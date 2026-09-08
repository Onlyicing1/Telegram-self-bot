"""Restart / reconnect / duplicate-execution regression tests.

Contract under test: ONE logical scheduled occurrence executes at most once
across process restarts, scheduler recovery, duplicate wake-ups, racing
recoveries, and concurrent schedulers — always through the single
Scheduler -> TaskExecutionCoordinator -> ToolExecutor authority, with
retries bounded and backoff honored across restart.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from backend.ai.database.task_repository import InMemoryTaskRepository
from backend.ai.task_scheduler import TaskScheduler, occurrence_key

NOW = datetime(2026, 9, 8, 12, tzinfo=timezone.utc)


def task_data(**overrides):
    value = {
        "label": "Interval",
        "schedule_type": "interval",
        "schedule": {"seconds": 3600},
        "timezone": "UTC",
        "next_run_at": NOW,
        "actions": [{"name": "safe", "arguments": {}}],
        "notification_destination": {"chat_id": 1},
    }
    value.update(overrides)
    return value


class RecordingCoordinator:
    """Stub execution authority: records each execution and persists success."""

    def __init__(self, repo, *, fail=False, delay=0.0):
        self.repo = repo
        self.fail = fail
        self.delay = delay
        self.executed_keys: list[str] = []

    async def execute(self, occurrence):
        self.executed_keys.append(occurrence.occurrence_key)
        if self.delay:
            import asyncio

            await asyncio.sleep(self.delay)
        status = "failed" if self.fail else "succeeded"
        await self.repo.transition_occurrence(
            1, occurrence.task_id, occurrence.occurrence_key, status
        )
        from types import SimpleNamespace

        return SimpleNamespace(status=status)


# ── Restart: claimed-before-shutdown is resolved deterministically ──────────


@pytest.mark.asyncio
async def test_recovered_occurrence_honors_backoff_before_reexecution():
    """After restart, recovery arms the retry backoff; the wake loop must NOT
    run the occurrence before its persisted retry_at."""
    repo = InMemoryTaskRepository()
    task = await repo.create_task(1, task_data(next_run_at=None))
    occurrence = await repo.create_occurrence(1, {
        "task_id": task.id, "occurrence_key": "k", "definition_version": 1,
        "action_snapshot": task.actions, "scheduled_for": NOW,
    })
    await repo.claim_occurrence(1, task.id, occurrence.occurrence_key)
    coordinator = RecordingCoordinator(repo)
    scheduler = TaskScheduler(repo, 1, execution_coordinator=coordinator)

    await scheduler.recover()
    recovered = await repo.get_occurrence(1, task.id, "k")
    assert recovered.status == "retry_pending"
    assert recovered.retry_at > NOW

    # A wake BEFORE the armed retry_at must not execute it.
    early = recovered.retry_at - timedelta(seconds=1)
    assert await scheduler.run_once(early) == 0
    assert coordinator.executed_keys == []
    assert (await repo.get_occurrence(1, task.id, "k")).status == "retry_pending"

    # A wake AT/AFTER retry_at executes it exactly once.
    assert await scheduler.run_once(recovered.retry_at) == 1
    assert coordinator.executed_keys == ["k"]
    assert (await repo.get_occurrence(1, task.id, "k")).status == "succeeded"


@pytest.mark.asyncio
async def test_wake_loop_never_executes_retry_pending_before_backoff():
    """A retry_pending occurrence existing independently of recovery must not
    be picked up by the due-task wake path ahead of its retry_at."""
    repo = InMemoryTaskRepository()
    task = await repo.create_task(1, task_data(next_run_at=None))
    occurrence = await repo.create_occurrence(1, {
        "task_id": task.id, "occurrence_key": "k", "definition_version": 1,
        "action_snapshot": task.actions, "scheduled_for": NOW,
    })
    await repo.claim_occurrence(1, task.id, occurrence.occurrence_key)
    await repo.transition_occurrence(
        1, task.id, occurrence.occurrence_key, "retry_pending",
        retry_at=NOW + timedelta(minutes=5), attempt=2,
    )
    coordinator = RecordingCoordinator(repo)
    scheduler = TaskScheduler(repo, 1, execution_coordinator=coordinator)

    assert await scheduler.run_once(NOW) == 0
    assert coordinator.executed_keys == []
    assert (await repo.get_occurrence(1, task.id, "k")).status == "retry_pending"

    assert await scheduler.run_once(NOW + timedelta(minutes=5)) == 1
    assert coordinator.executed_keys == ["k"]


@pytest.mark.asyncio
async def test_recovery_at_attempt_limit_fails_without_multiplying_retries():
    repo = InMemoryTaskRepository()
    task = await repo.create_task(1, task_data(next_run_at=None))
    occurrence = await repo.create_occurrence(1, {
        "task_id": task.id, "occurrence_key": "k", "definition_version": 1,
        "action_snapshot": task.actions, "scheduled_for": NOW, "attempt": 3,
    })
    await repo.claim_occurrence(1, task.id, occurrence.occurrence_key)
    coordinator = RecordingCoordinator(repo)
    scheduler = TaskScheduler(repo, 1, execution_coordinator=coordinator)

    assert await scheduler.recover() == 1
    final = await repo.get_occurrence(1, task.id, "k")
    assert final.status == "failed"
    assert final.attempt == 3
    assert await scheduler.run_once(NOW + timedelta(hours=1)) == 0
    assert coordinator.executed_keys == []


# ── Duplicate wake-ups / duplicate scheduling ───────────────────────────────


@pytest.mark.asyncio
async def test_repeated_wakes_execute_one_occurrence_once():
    """Duplicate wake-up (polling overlap, scheduler restart, repeated
    run_once at the same reference time) must not re-execute."""
    repo = InMemoryTaskRepository()
    task = await repo.create_task(1, task_data())
    coordinator = RecordingCoordinator(repo)
    scheduler = TaskScheduler(repo, 1, execution_coordinator=coordinator)

    for _ in range(3):
        await scheduler.run_once(NOW)
    assert coordinator.executed_keys == [occurrence_key(task.id, NOW)]
    assert (await repo.get_occurrence(1, task.id, occurrence_key(task.id, NOW))).status == "succeeded"


@pytest.mark.asyncio
async def test_two_schedulers_over_one_repository_execute_once():
    """Overlapping schedulers (recovery window where an old loop is still
    draining) share the durable repository; the claim CAS must yield exactly
    one execution."""
    repo = InMemoryTaskRepository()
    task = await repo.create_task(1, task_data())
    coordinator = RecordingCoordinator(repo)
    first = TaskScheduler(repo, 1, execution_coordinator=coordinator)
    second = TaskScheduler(repo, 1, execution_coordinator=coordinator)

    import asyncio

    await asyncio.gather(first.run_once(NOW), second.run_once(NOW))
    assert sorted(coordinator.executed_keys) == [occurrence_key(task.id, NOW)]
    assert (await repo.get_occurrence(1, task.id, occurrence_key(task.id, NOW))).status == "succeeded"


# ── Recovery serialization: attempts cannot multiply ────────────────────────


@pytest.mark.asyncio
async def test_concurrent_recoveries_do_not_multiply_attempts():
    repo = InMemoryTaskRepository()
    task = await repo.create_task(1, task_data(next_run_at=None))
    occurrence = await repo.create_occurrence(1, {
        "task_id": task.id, "occurrence_key": "k", "definition_version": 1,
        "action_snapshot": task.actions, "scheduled_for": NOW,
    })
    await repo.claim_occurrence(1, task.id, occurrence.occurrence_key)

    import asyncio

    first = TaskScheduler(repo, 1)
    second = TaskScheduler(repo, 1)
    recovered_first, recovered_second = await asyncio.gather(
        first.recover(), second.recover()
    )
    assert sorted([recovered_first, recovered_second]) == [0, 1]

    final = await repo.get_occurrence(1, task.id, "k")
    assert final.status == "retry_pending"
    assert final.attempt == 2  # exactly one attempt consumed by the restart

    # And a racing recover on an already-resolved occurrence is a no-op.
    third = TaskScheduler(repo, 1)
    assert await third.recover() == 0
    assert (await repo.get_occurrence(1, task.id, "k")).attempt == 2


@pytest.mark.asyncio
async def test_recover_is_resumable_after_previous_resolution():
    """A second recover after the occurrence was legitimately resolved must
    not push it through another interrupted/retry cycle."""
    repo = InMemoryTaskRepository()
    task = await repo.create_task(1, task_data(next_run_at=None))
    occurrence = await repo.create_occurrence(1, {
        "task_id": task.id, "occurrence_key": "k", "definition_version": 1,
        "action_snapshot": task.actions, "scheduled_for": NOW,
    })
    await repo.claim_occurrence(1, task.id, occurrence.occurrence_key)
    scheduler = TaskScheduler(repo, 1)
    await scheduler.recover()
    resolved = await repo.get_occurrence(1, task.id, "k")
    assert resolved.status == "retry_pending" and resolved.attempt == 2

    # Run to completion through the bounded retry path.
    coordinator = RecordingCoordinator(repo)
    executor_scheduler = TaskScheduler(repo, 1, execution_coordinator=coordinator)
    assert await executor_scheduler.run_once(resolved.retry_at) == 1
    assert (await repo.get_occurrence(1, task.id, "k")).status == "succeeded"

    # A later restart-style recover must not resurrect it.
    again = TaskScheduler(repo, 1)
    assert await again.recover() == 0
    final = await repo.get_occurrence(1, task.id, "k")
    assert final.status == "succeeded" and final.attempt == 2


# ── No-execution-authority path stays deterministic ────────────────────────


@pytest.mark.asyncio
async def test_due_occurrence_without_coordinator_parks_for_recovery():
    repo = InMemoryTaskRepository()
    task = await repo.create_task(1, task_data())
    scheduler = TaskScheduler(repo, 1, execution_coordinator=None)

    assert await scheduler.run_once(NOW) == 1
    stored = await repo.get_occurrence(1, task.id, occurrence_key(task.id, NOW))
    assert stored.status == "interrupted"

    # A later restart with a coordinator resolves it via the retry contract,
    # never by silently executing the stale claimed state.
    recovery = TaskScheduler(repo, 1)
    assert await recovery.recover() == 1
    resolved = await repo.get_occurrence(1, task.id, occurrence_key(task.id, NOW))
    assert resolved.status == "retry_pending" and resolved.attempt == 2


# ── Terminal occurrences are never recreated or re-executed ────────────────


@pytest.mark.asyncio
async def test_completed_occurrence_is_never_reexecuted_by_later_wakes():
    repo = InMemoryTaskRepository()
    task = await repo.create_task(1, task_data())
    coordinator = RecordingCoordinator(repo)
    scheduler = TaskScheduler(repo, 1, execution_coordinator=coordinator)
    key = occurrence_key(task.id, NOW)

    await scheduler.run_once(NOW)
    assert coordinator.executed_keys == [key]

    # Restart-style recovery finds nothing recoverable; later wakes reuse the
    # same occurrence row without executing it again.
    assert await TaskScheduler(repo, 1).recover() == 0
    await scheduler.run_once(NOW)
    await TaskScheduler(repo, 1).run_once(NOW)
    assert coordinator.executed_keys == [key]
    assert (await repo.get_occurrence(1, task.id, key)).status == "succeeded"
