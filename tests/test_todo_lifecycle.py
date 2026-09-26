"""Basic Todo lifecycle (Part 1) on the EXISTING durable task foundation.

A todo is the unscheduled kind of task (``schedule_type='todo'``): a title,
a status (active/completed), and nothing the scheduler could ever run. These
tests pin the contract of that row and the seven basic operations over it —
add / list / inspect / edit / complete / reopen / delete — driving the REAL
service and repository (never a stub): no live Telegram, no Supabase.

What is deliberately NOT here: a second store, a second state model, a
priority/category/due-date field, subtasks and multi-step tasks (Part 2+).
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from backend.ai.database.task_repository import (
    DELETION_DELETED,
    DELETION_NOT_FOUND,
    DELETION_STALE,
    InMemoryTaskRepository,
)
from backend.ai.task_creation import TaskCreationError, TaskCreationService
from backend.ai.task_management import TaskManagementService
from backend.ai.task_management_interface import todo_detail_text

OWNER = 4242
OTHER = 9999


def _now():
    return datetime(2026, 9, 26, 9, 30, tzinfo=timezone.utc)


async def _todo(repo, owner=OWNER, label="گزارش دانشگاه رو بنویسم"):
    return await TaskCreationService(repo, owner).create_todo(label, "Asia/Tehran", _now())


def _scheduled_task(**overrides):
    data = {
        "label": "scheduled task",
        "schedule_type": "interval",
        "schedule": {"seconds": 300},
        "timezone": "UTC",
        "actions": [{"name": "send_message", "arguments": {"text": "hello"}}],
        "notification_destination": {},
    }
    data.update(overrides)
    return data


# ── 1. create ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_todo_stores_an_unscheduled_row():
    repo = InMemoryTaskRepository()
    todo = await _todo(repo)

    assert todo.schedule_type == "todo"
    assert todo.label == "گزارش دانشگاه رو بنویسم"
    assert todo.status == "active" and todo.version == 1
    # Nothing a scheduler could ever run: no boundary, no action, no
    # destination, no fabricated schedule payload.
    assert todo.next_run_at is None
    assert todo.actions == []
    assert todo.schedule == {}
    assert todo.notification_destination == {}
    assert todo.ai_instruction is None
    assert todo.owner_id == OWNER


@pytest.mark.asyncio
async def test_create_todo_normalizes_whitespace_and_requires_a_title():
    repo = InMemoryTaskRepository()
    todo = await _todo(repo, label="  نوشیدن   آب  ")
    assert todo.label == "نوشیدن آب"

    for blank in ("", "   ", "\n", None):
        with pytest.raises(TaskCreationError):
            await TaskCreationService(repo, OWNER).create_todo(blank, "UTC", _now())
    with pytest.raises(TaskCreationError):
        await TaskCreationService(repo, OWNER).create_todo("x" * 257, "UTC", _now())
    # Nothing was fabricated for the refused titles.
    assert [t.label for t in await repo.list_tasks(OWNER)] == ["نوشیدن آب"]


@pytest.mark.asyncio
async def test_repository_keeps_the_scheduled_action_invariant():
    """Only a todo may store no action; a scheduled task still may not."""
    repo = InMemoryTaskRepository()
    with pytest.raises(ValueError):
        await repo.create_task(OWNER, _scheduled_task(actions=[]))
    with pytest.raises(ValueError):
        await repo.create_task(OWNER, _scheduled_task(actions=[{"name": "a"}] * 6))
    # A todo can never hold a due instant — that would make it executable.
    with pytest.raises(ValueError):
        await repo.create_task(
            OWNER,
            _scheduled_task(
                schedule_type="todo",
                schedule={},
                actions=[],
                next_run_at=_now(),
            ),
        )


# ── 2. list ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_splits_active_and_completed_and_stays_owner_scoped():
    repo = InMemoryTaskRepository()
    active = await _todo(repo, label="active todo")
    done = await _todo(repo, label="done todo")
    other = await _todo(repo, owner=OTHER, label="other owner todo")
    scheduled = await repo.create_task(OWNER, _scheduled_task())

    service = TaskManagementService(repo, OWNER)
    await service.complete(done.id, done.version)

    assert [t.id for t in await service.list_todos("active")] == [active.id]
    assert [t.id for t in await service.list_todos("completed")] == [done.id]
    assert [t.id for t in await service.list_todos()] == [active.id, done.id]
    # A scheduled task is not a todo, here or in another owner's view.
    assert scheduled.id not in [t.id for t in await service.list_todos()]
    assert [t.id for t in await TaskManagementService(repo, OTHER).list_todos()] == [other.id]

    snapshot = await service.snapshot(todos_only=True)
    assert sorted(t.id for t in snapshot.tasks) == [active.id, done.id]
    assert snapshot.counts()["active"] == 1 and snapshot.counts()["completed"] == 1


# ── 3. inspect ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_inspect_shows_only_basic_todo_facts():
    repo = InMemoryTaskRepository()
    todo = await _todo(repo)
    view = await TaskManagementService(repo, OWNER).inspect(todo.id)
    assert view is not None and view.task.id == todo.id
    assert view.occurrences == []

    text = todo_detail_text(view.task)
    assert f"Todo #{todo.id}" in text
    assert "گزارش دانشگاه رو بنویسم" in text
    assert "Active" in text
    assert "Version: v1" in text
    assert "unscheduled" in text

    # Another owner can never inspect it; a foreign id is indistinguishable
    # from a missing one.
    assert await TaskManagementService(repo, OTHER).inspect(todo.id) is None


# ── 4. edit ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_edit_todo_title_uses_the_version_and_can_be_stale():
    repo = InMemoryTaskRepository()
    todo = await _todo(repo)
    service = TaskManagementService(repo, OWNER)

    edited = await service.edit_todo_title(todo.id, todo.version, "  گزارش ترم  ")
    assert edited is not None
    assert edited.label == "گزارش ترم"
    assert edited.version == todo.version + 1

    # A stale version changes nothing.
    stale = await service.edit_todo_title(todo.id, todo.version, "overwritten")
    assert stale is None
    assert (await repo.get_task(OWNER, todo.id)).label == "گزارش ترم"

    # A blank/over-long title is refused, and nothing is written.
    with pytest.raises(ValueError):
        await service.edit_todo_title(todo.id, edited.version, "   ")
    with pytest.raises(ValueError):
        await service.edit_todo_title(todo.id, edited.version, "x" * 257)
    assert (await repo.get_task(OWNER, todo.id)).label == "گزارش ترم"

    # Another owner can never edit it.
    assert await TaskManagementService(repo, OTHER).edit_todo_title(
        todo.id, edited.version, "hijacked"
    ) is None
    assert (await repo.get_task(OWNER, todo.id)).label == "گزارش ترم"

    # A scheduled task is not editable through the todo surface.
    scheduled = await repo.create_task(OWNER, _scheduled_task())
    assert await service.edit_todo_title(scheduled.id, scheduled.version, "renamed") is None
    assert (await repo.get_task(OWNER, scheduled.id)).label == "scheduled task"


# ── 5/6. complete + reopen ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_complete_then_reopen_a_todo_under_cas():
    repo = InMemoryTaskRepository()
    todo = await _todo(repo)
    service = TaskManagementService(repo, OWNER)

    completed = await service.complete(todo.id, todo.version)
    assert completed is not None and completed.status == "completed"
    assert completed.version == todo.version + 1
    assert completed.next_run_at is None

    # Stale transition changes nothing.
    assert await service.complete(todo.id, todo.version) is None
    assert (await repo.get_task(OWNER, todo.id)).status == "completed"

    reopened = await service.reopen(todo.id, completed.version)
    assert reopened is not None and reopened.status == "active"
    assert reopened.version == completed.version + 1
    assert reopened.next_run_at is None

    # Stale reopen changes nothing, and a non-completed todo cannot reopen.
    assert await service.reopen(todo.id, completed.version) is None
    assert await service.reopen(todo.id, reopened.version) is None
    assert (await repo.get_task(OWNER, todo.id)).status == "active"


@pytest.mark.asyncio
async def test_a_todo_is_never_pausable_and_a_scheduled_task_is_never_reopenable():
    repo = InMemoryTaskRepository()
    todo = await _todo(repo)
    service = TaskManagementService(repo, OWNER)
    with pytest.raises(ValueError):
        await service.pause(todo.id, todo.version)
    assert (await repo.get_task(OWNER, todo.id)).status == "active"

    scheduled = await repo.create_task(OWNER, _scheduled_task())
    completed = await service.complete(scheduled.id, scheduled.version)
    assert completed is not None
    # A completed SCHEDULED task stays terminal: reopening it would have to
    # invent a new execution boundary for an occurrence that already ran.
    assert await service.reopen(scheduled.id, completed.version) is None
    with pytest.raises(ValueError):
        await service.set_status(scheduled.id, "active", completed.version)
    assert (await repo.get_task(OWNER, scheduled.id)).status == "completed"


@pytest.mark.asyncio
async def test_other_owners_cannot_transition_a_todo():
    repo = InMemoryTaskRepository()
    todo = await _todo(repo)
    foreign = TaskManagementService(repo, OTHER)
    assert await foreign.complete(todo.id, todo.version) is None
    assert await foreign.reopen(todo.id, todo.version) is None
    assert (await repo.get_task(OWNER, todo.id)).status == "active"


# ── 7. delete ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_delete_removes_the_todo_row_and_its_occurrences():
    repo = InMemoryTaskRepository()
    todo = await _todo(repo)
    await repo.create_occurrence(
        OWNER,
        {
            "task_id": todo.id,
            "occurrence_key": "k",
            "definition_version": todo.version,
            "action_snapshot": [{"name": "send_message", "arguments": {"text": "x"}}],
            "scheduled_for": _now(),
        },
    )
    service = TaskManagementService(repo, OWNER)

    stale = await service.delete(todo.id, expected_version=todo.version + 5)
    assert stale.outcome == DELETION_STALE and stale.deleted is False
    assert (await repo.get_task(OWNER, todo.id)) is not None

    foreign = await TaskManagementService(repo, OTHER).delete(todo.id, todo.version)
    assert foreign.outcome == DELETION_NOT_FOUND and foreign.deleted is False

    result = await service.delete(todo.id, expected_version=todo.version)
    assert result.outcome == DELETION_DELETED and result.durable is True
    assert await repo.get_task(OWNER, todo.id) is None
    assert await repo.list_occurrences(OWNER, todo.id) == []
    assert [t.id for t in await service.list_todos()] == []


# ── the scheduler can never see a todo ───────────────────────────────────────


@pytest.mark.asyncio
async def test_todo_is_invisible_to_every_scheduler_query():
    repo = InMemoryTaskRepository()
    todo = await _todo(repo)
    scheduled = await repo.create_task(
        OWNER, _scheduled_task(schedule_type="event", schedule={"trigger": {"type": "telegram_message", "contains": ["x"]}})
    )
    assert await repo.list_due_tasks(OWNER, _now()) == []
    assert [t.id for t in await repo.list_event_tasks(OWNER)] == [scheduled.id]
    assert await repo.next_run_hint(OWNER) is None
    assert (await repo.get_task(OWNER, todo.id)).next_run_at is None
