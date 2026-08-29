import pytest

from backend.ai.database.task_repository import InMemoryTaskRepository


def task_data(**overrides):
    data = {
        "label": "Daily check",
        "schedule_type": "daily",
        "schedule": {"hour": 8, "minute": 30},
        "timezone": "Asia/Tehran",
        "next_run_at": None,
        "actions": [{"name": "list_saves", "arguments": {}}],
        "notification_destination": {"chat_id": 123},
    }
    data.update(overrides)
    return data


def occurrence_data(task_id, **overrides):
    data = {
        "task_id": task_id,
        "occurrence_key": "2026-08-29T08:30:00+00:00",
        "definition_version": 1,
        "action_snapshot": [{"name": "list_saves", "arguments": {}}],
        "scheduled_for": "2026-08-29T08:30:00+00:00",
    }
    data.update(overrides)
    return data


@pytest.mark.asyncio
async def test_task_creation_owner_isolation_and_cas():
    repo = InMemoryTaskRepository()
    task = await repo.create_task(10, task_data())
    assert task.version == 1
    assert await repo.get_task(11, task.id) is None
    updated = await repo.update_task(10, task.id, 1, {"label": "Updated"})
    assert updated.version == 2
    assert updated.label == "Updated"
    assert await repo.update_task(10, task.id, 1, {"label": "stale"}) is None


@pytest.mark.asyncio
async def test_occurrence_uniqueness_owner_isolation_and_snapshot():
    repo = InMemoryTaskRepository()
    task = await repo.create_task(10, task_data())
    first = await repo.create_occurrence(10, occurrence_data(task.id))
    duplicate = await repo.create_occurrence(10, occurrence_data(task.id, action_snapshot=[{"name": "changed"}]))
    assert duplicate.id == first.id
    assert duplicate.action_snapshot == first.action_snapshot
    assert await repo.get_occurrence(11, task.id, first.occurrence_key) is None
    with pytest.raises(ValueError):
        await repo.create_occurrence(11, occurrence_data(task.id))


@pytest.mark.asyncio
async def test_attempt_limit_retry_and_status_transitions():
    repo = InMemoryTaskRepository()
    task = await repo.create_task(10, task_data())
    for attempt in (1, 2, 3):
        record = await repo.create_occurrence(10, occurrence_data(task.id, occurrence_key=f"k{attempt}", attempt=attempt))
        assert record.attempt == attempt
    with pytest.raises(ValueError):
        await repo.create_occurrence(10, occurrence_data(task.id, occurrence_key="k4", attempt=4))
    record = await repo.get_occurrence(10, task.id, "k1")
    claimed = await repo.claim_occurrence(10, task.id, record.occurrence_key)
    assert claimed.status == "running"
    retry = await repo.transition_occurrence(10, task.id, record.occurrence_key, "retry_pending", retry_at="2026-08-29T09:30:00+00:00", attempt=2)
    assert retry.status == "retry_pending"
    interrupted = await repo.transition_occurrence(10, task.id, record.occurrence_key, "interrupted")
    assert interrupted.status == "interrupted"


@pytest.mark.asyncio
async def test_terminal_transitions_and_retry_timing_are_rejected():
    repo = InMemoryTaskRepository()
    task = await repo.create_task(10, task_data())
    record = await repo.create_occurrence(10, occurrence_data(task.id))
    with pytest.raises(ValueError):
        await repo.transition_occurrence(10, task.id, record.occurrence_key, "retry_pending")
    await repo.claim_occurrence(10, task.id, record.occurrence_key)
    await repo.transition_occurrence(10, task.id, record.occurrence_key, "succeeded")
    with pytest.raises(ValueError):
        await repo.transition_occurrence(10, task.id, record.occurrence_key, "running")
    await repo.transition_task(10, task.id, "deleted")
    history = await repo.get_occurrence(10, task.id, record.occurrence_key)
    assert history.status == "succeeded"


@pytest.mark.asyncio
async def test_snapshot_is_independent_and_terminal_task_is_not_reactivated():
    repo = InMemoryTaskRepository()
    task = await repo.create_task(10, task_data())
    occurrence = await repo.create_occurrence(10, occurrence_data(task.id))
    await repo.update_task(10, task.id, 1, {"actions": [{"name": "changed", "arguments": {}}]})
    stored = await repo.get_occurrence(10, task.id, occurrence.occurrence_key)
    assert stored.action_snapshot[0]["name"] == "list_saves"
    terminal = await repo.transition_task(10, task.id, "completed")
    assert terminal.terminal_at is not None
    with pytest.raises(ValueError):
        await repo.transition_task(10, task.id, "active")


@pytest.mark.asyncio
async def test_bounded_json_and_malformed_payloads_are_rejected():
    repo = InMemoryTaskRepository()
    with pytest.raises(ValueError):
        await repo.create_task(10, task_data(actions=[]))
    with pytest.raises(ValueError):
        await repo.create_task(10, task_data(actions=[{"name": "x"}] * 6))
    task = await repo.create_task(10, task_data())
    with pytest.raises(ValueError):
        await repo.create_occurrence(10, occurrence_data(task.id, action_snapshot={"bad": True}))
    with pytest.raises(ValueError):
        await repo.create_occurrence(10, occurrence_data(task.id, error_metadata=[]))
