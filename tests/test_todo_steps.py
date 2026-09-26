"""Multi-step Todos — the ordered step model, its lifecycle and its storage.

A step belongs to ONE todo (an ``ai_tasks`` row with ``schedule_type='todo'``)
and is ordered inside it by ``position``. These tests pin the model the AI
surface and the Telegram surface both build on:

  * creation of a todo with zero, one or several steps — in ONE operation,
    all-or-nothing, so no half-created structure can exist;
  * ordered persistence and the stable step NUMBER (the ordinal in position
    order, even after a middle step is deleted);
  * complete / reopen / rename / delete, each CAS-guarded on the STEP's own
    version, and never touching the todo's own title;
  * the parent/step lifecycle invariant: a completed todo has no remaining
    step, and a completed todo accepts neither a new step nor a reopened one;
  * deletion: removing a step never removes the todo, and removing the todo
    never leaves an orphan step behind;
  * owner scoping on every single operation;
  * the durable (Supabase) path: ONE multi-row insert, positions continuing
    from the current maximum, CAS updates and the FK that cascades the parent.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.ai.database.task_repository import (
    MAX_STEPS_PER_ADD,
    MAX_STEPS_PER_TODO,
    InMemoryTaskRepository,
    SupabaseTaskRepository,
)
from backend.ai.task_creation import TaskCreationError, TaskCreationService
from backend.ai.task_management import (
    STEP_TARGET_AMBIGUOUS,
    STEP_TARGET_INVALID,
    STEP_TARGET_NOT_FOUND,
    STEP_TARGET_OK,
    TaskManagementService,
)

OWNER = 4242
OTHER = 9999
NOW = datetime(2026, 9, 27, 9, 0, tzinfo=timezone.utc)
STEPS = ("جمع‌آوری منابع", "نوشتن گزارش", "آماده‌سازی ارائه")


async def _todo(repo, label="پروژه دانشگاه", owner=OWNER, steps=STEPS):
    return await TaskCreationService(repo, owner).create_todo(
        label, "Asia/Tehran", NOW, steps=steps
    )


def _service(repo, owner=OWNER) -> TaskManagementService:
    return TaskManagementService(repo, owner)


# ── creation ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_todo_is_created_with_zero_steps_and_reports_none():
    repo = InMemoryTaskRepository()
    task = await _todo(repo, steps=())
    service = _service(repo)
    assert await service.list_steps(task.id) == []
    progress = await service.step_progress(task.id)
    assert progress.total == 0 and progress.completed == 0
    assert progress.remaining == 0 and progress.next_step is None
    assert progress.label == "0 / 0 steps completed"


@pytest.mark.asyncio
async def test_a_todo_is_created_with_all_of_its_steps_in_one_operation():
    repo = InMemoryTaskRepository()
    task = await _todo(repo)
    steps = await _service(repo).list_steps(task.id)
    assert [s.title for s in steps] == list(STEPS)
    assert [s.position for s in steps] == [1, 2, 3]
    assert [s.status for s in steps] == ["active", "active", "active"]
    assert [s.version for s in steps] == [1, 1, 1]
    assert all(s.task_id == task.id and s.owner_id == OWNER for s in steps)
    # The todo itself is the SAME unscheduled row Part 1 creates.
    assert task.schedule_type == "todo" and task.actions == [] and task.next_run_at is None


@pytest.mark.asyncio
async def test_a_rejected_step_removes_the_todo_instead_of_half_creating_it():
    repo = InMemoryTaskRepository()
    with pytest.raises(TaskCreationError):
        await _todo(repo, label="rollback", steps=["ok", "   "])
    assert await repo.list_tasks(OWNER) == []
    assert repo._steps == {}


@pytest.mark.asyncio
async def test_step_bounds_are_enforced_at_creation():
    repo = InMemoryTaskRepository()
    with pytest.raises(TaskCreationError):
        await _todo(repo, steps=["x"] * (MAX_STEPS_PER_ADD + 1))
    with pytest.raises(TaskCreationError):
        await _todo(repo, steps=["y" * 257])
    assert await repo.list_tasks(OWNER) == []


@pytest.mark.asyncio
async def test_steps_are_appended_in_order_and_never_renumbered():
    repo = InMemoryTaskRepository()
    task = await _todo(repo, steps=("one",))
    service = _service(repo)
    added = await service.add_steps(task.id, ["two", "three"])
    assert [s.title for s in added] == ["two", "three"]
    assert [s.position for s in added] == [2, 3]

    all_steps = await service.list_steps(task.id)
    second = all_steps[1]
    # Deleting the MIDDLE step leaves the remaining positions untouched: the
    # user-facing step NUMBER is the (contiguous) ordinal in position order.
    assert await service.delete_step(second.id, second.version) is True
    remaining = await service.list_steps(task.id)
    assert [s.position for s in remaining] == [1, 3]
    target = await service.resolve_step(task.id, number=2)
    assert target.status == STEP_TARGET_OK and target.step.title == "three"
    assert target.ordinal == 2


@pytest.mark.asyncio
async def test_add_steps_refuses_more_than_the_bounds_allow():
    repo = InMemoryTaskRepository()
    task = await _todo(repo, steps=())
    service = _service(repo)
    with pytest.raises(ValueError):
        await service.add_steps(task.id, ["x"] * (MAX_STEPS_PER_ADD + 1))
    with pytest.raises(ValueError):
        await service.add_steps(task.id, [])
    await service.add_steps(task.id, ["a"])
    with pytest.raises(ValueError):
        await service.add_steps(task.id, ["b"] * (MAX_STEPS_PER_TODO))
    assert len(await service.list_steps(task.id)) == 1


# ── lifecycle ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_complete_and_reopen_one_step_under_cas():
    repo = InMemoryTaskRepository()
    task = await _todo(repo)
    service = _service(repo)
    steps = await service.list_steps(task.id)
    first = steps[0]

    done = await service.complete_step(first.id, first.version)
    assert done.status == "completed" and done.completed_at is not None
    assert done.version == first.version + 1
    progress = await service.step_progress(task.id)
    assert progress.label == "1 / 3 steps completed"
    assert progress.next_step.title == STEPS[1]

    # A stale version changes nothing.
    assert await service.complete_step(first.id, first.version) is None
    assert (await service.get_step(first.id)).version == done.version

    reopened = await service.reopen_step(first.id, done.version)
    assert reopened.status == "active" and reopened.completed_at is None
    assert (await service.step_progress(task.id)).completed == 0


@pytest.mark.asyncio
async def test_renaming_a_step_never_renames_the_todo():
    repo = InMemoryTaskRepository()
    task = await _todo(repo)
    service = _service(repo)
    first = (await service.list_steps(task.id))[0]

    renamed = await service.rename_step(first.id, first.version, "  جمع‌آوری   منابع  ")
    assert renamed.title == "جمع‌آوری منابع"
    assert (await repo.get_task(OWNER, task.id)).label == "پروژه دانشگاه"
    assert (await service.list_steps(task.id))[0].title == "جمع‌آوری منابع"

    # The todo rename and the step title stay independent in the other
    # direction too: renaming the todo leaves every step title alone.
    await service.edit_todo_title(task.id, task.version, "پروژه ترم")
    assert [s.title for s in await service.list_steps(task.id)] == list(STEPS)

    with pytest.raises(ValueError):
        await service.rename_step(first.id, renamed.version, "   ")


@pytest.mark.asyncio
async def test_deleting_a_step_never_deletes_the_todo():
    repo = InMemoryTaskRepository()
    task = await _todo(repo)
    service = _service(repo)
    steps = await service.list_steps(task.id)
    assert await service.delete_step(steps[1].id, steps[1].version) is True
    assert (await repo.get_task(OWNER, task.id)) is not None
    assert len(await service.list_steps(task.id)) == 2
    # A stale delete removes nothing.
    assert await service.delete_step(steps[1].id, steps[1].version) is False


@pytest.mark.asyncio
async def test_deleting_the_todo_removes_its_steps_without_orphans():
    repo = InMemoryTaskRepository()
    task = await _todo(repo)
    other = await _todo(repo, label="other", steps=("keep me",))
    assert len(repo._steps) == 4

    result = await repo.delete_task(OWNER, task.id, task.version)
    assert result.deleted is True
    assert [s.task_id for s in repo._steps.values()] == [other.id]
    assert list(await _service(repo).list_steps(other.id)) != []
    assert await _service(repo).list_steps(task.id) is None


def test_the_migration_cascades_step_deletion_from_the_parent():
    """The durable side of the orphan guarantee is the FOREIGN KEY itself."""
    sql = (
        Path(__file__).resolve().parent.parent
        / "supabase"
        / "migrations"
        / "20260927000001_add_todo_steps.sql"
    ).read_text(encoding="utf-8")
    assert "CREATE TABLE IF NOT EXISTS todo_steps" in sql
    assert "REFERENCES ai_tasks(id) ON DELETE CASCADE" in sql
    assert "UNIQUE (task_id, position)" in sql
    assert "CHECK (status IN ('active', 'completed'))" in sql
    assert "ENABLE ROW LEVEL SECURITY" in sql
    # Additive successor: the EXECUTABLE statements create and constrain, they
    # never destroy (the header documents the manual rollback for the owner).
    without_comments = re.sub(r"/\*.*?\*/", " ", sql, flags=re.S)
    executable = "\n".join(
        line
        for line in without_comments.splitlines()
        if line.strip() and not line.strip().startswith("--")
    )
    for forbidden in ("DROP TABLE", "DROP COLUMN", "TRUNCATE", "DELETE FROM"):
        assert forbidden not in executable, forbidden


# ── parent / step lifecycle invariants ───────────────────────────────────────


@pytest.mark.asyncio
async def test_a_todo_with_remaining_steps_cannot_be_completed():
    repo = InMemoryTaskRepository()
    task = await _todo(repo)
    service = _service(repo)
    steps = await service.list_steps(task.id)
    done = await service.complete_step(steps[0].id, steps[0].version)
    assert done is not None

    with pytest.raises(ValueError) as excinfo:
        await service.complete(task.id, task.version)
    message = str(excinfo.value)
    assert "2 of 3 steps remain" in message
    assert STEPS[1] in message
    # Nothing was written by the refused completion.
    assert (await repo.get_task(OWNER, task.id)).status == "active"


@pytest.mark.asyncio
async def test_completing_a_todo_with_its_remaining_steps_is_explicit():
    repo = InMemoryTaskRepository()
    task = await _todo(repo)
    service = _service(repo)
    steps = await service.list_steps(task.id)
    await service.complete_step(steps[0].id, steps[0].version)

    completed = await service.complete_todo_with_steps(task.id, task.version)
    assert completed is not None and completed.status == "completed"
    progress = await service.step_progress(task.id)
    assert progress.remaining == 0 and progress.label == "3 / 3 steps completed"
    # A stale todo version is refused before anything is written.
    assert await service.complete_todo_with_steps(task.id, 1) is None


@pytest.mark.asyncio
async def test_a_completed_todo_accepts_no_new_step_and_no_reopened_step():
    repo = InMemoryTaskRepository()
    task = await _todo(repo)
    service = _service(repo)
    steps = await service.list_steps(task.id)
    for step in steps:
        await service.complete_step(step.id, step.version)
    completed = await service.complete(task.id, task.version)
    assert completed.status == "completed"

    with pytest.raises(ValueError) as add_error:
        await service.add_steps(task.id, ["extra"])
    assert "completed — reopen it before adding steps" in str(add_error.value)
    with pytest.raises(ValueError) as reopen_error:
        await service.reopen_step(steps[0].id, steps[0].version + 1)
    assert "reopen the todo first" in str(reopen_error.value)
    assert len(await service.list_steps(task.id)) == 3

    # After reopening the todo, both work again (one tap, no hidden state).
    reopened_todo = await service.reopen(task.id, completed.version)
    assert reopened_todo.status == "active"
    added = await service.add_steps(task.id, ["extra"])
    assert [s.title for s in added] == ["extra"]
    step_again = await service.reopen_step(steps[0].id, steps[0].version + 1)
    assert step_again.status == "active"


# ── deterministic step resolution ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_step_resolves_by_ordinal_and_by_its_own_words():
    repo = InMemoryTaskRepository()
    task = await _todo(repo)
    service = _service(repo)
    for query, expected in (
        ("منابع", STEPS[0]),
        ("گزارش", STEPS[1]),
        ("ارائه", STEPS[2]),
        ("آماده‌سازی ارائه", STEPS[2]),
    ):
        target = await service.resolve_step(task.id, query=query)
        assert target.status == STEP_TARGET_OK, query
        assert target.step.title == expected
    by_number = await service.resolve_step(task.id, number="۲")
    assert by_number.status == STEP_TARGET_OK and by_number.step.title == STEPS[1]
    assert by_number.ordinal == 2


@pytest.mark.asyncio
async def test_step_resolution_reports_zero_matches_honestly():
    repo = InMemoryTaskRepository()
    task = await _todo(repo)
    service = _service(repo)
    missing = await service.resolve_step(task.id, query="خرید ماشین")
    assert missing.status == STEP_TARGET_NOT_FOUND
    assert "No step matching" in missing.message
    assert (await service.resolve_step(task.id, number=9)).status == STEP_TARGET_NOT_FOUND
    assert (await service.resolve_step(task.id, number=0)).status == STEP_TARGET_INVALID
    both = await service.resolve_step(task.id, number=1, query="منابع")
    assert both.status == STEP_TARGET_INVALID


@pytest.mark.asyncio
async def test_step_resolution_refuses_to_choose_between_two_matches():
    repo = InMemoryTaskRepository()
    task = await _todo(repo, steps=("گزارش دانشگاه — شنبه", "گزارش دانشگاه — ترم"))
    target = await _service(repo).resolve_step(task.id, query="گزارش دانشگاه")
    assert target.status == STEP_TARGET_AMBIGUOUS
    assert {c.ordinal for c in target.candidates} == {1, 2}
    assert "which one do you mean?" in target.message
    assert target.step is None


@pytest.mark.asyncio
async def test_steps_of_a_foreign_or_missing_todo_are_never_in_scope():
    repo = InMemoryTaskRepository()
    task = await _todo(repo)
    service = _service(repo)
    assert await service.list_steps(task.id + 1000) is None
    assert await service.step_progress(task.id + 1000) is None

    # The OTHER owner's todo lives in the SAME store, so "not mine" is the only
    # reason a step of it can be out of scope.
    other_service = TaskManagementService(repo, OTHER)
    foreign_task = await _todo(repo, label="other owner", owner=OTHER)
    foreign_step = (await other_service.list_steps(foreign_task.id))[0]
    assert await service.get_step(foreign_step.id) is None
    assert await service.complete_step(foreign_step.id, foreign_step.version) is None
    assert await service.reopen_step(foreign_step.id, foreign_step.version) is None
    assert await service.rename_step(foreign_step.id, foreign_step.version, "x") is None
    assert await service.delete_step(foreign_step.id, foreign_step.version) is False
    assert (await service.resolve_step(foreign_task.id, number=1)).status == (
        STEP_TARGET_NOT_FOUND
    )
    assert await service.add_steps(foreign_task.id, ["hijacked"]) is None
    # And nothing was touched on the other side.
    stored = (await other_service.list_steps(foreign_task.id))[0]
    assert stored.status == "active" and stored.title == STEPS[0]
    assert len(await other_service.list_steps(foreign_task.id)) == len(STEPS)


@pytest.mark.asyncio
async def test_a_scheduled_task_is_never_a_step_owner():
    repo = InMemoryTaskRepository()
    scheduled = await repo.create_task(
        OWNER,
        {
            "label": "scheduled",
            "schedule_type": "interval",
            "schedule": {"seconds": 60},
            "timezone": "UTC",
            "actions": [{"name": "send_message", "arguments": {"text": "x"}}],
            "notification_destination": {},
        },
    )
    service = _service(repo)
    assert await service.list_steps(scheduled.id) is None
    assert await service.add_steps(scheduled.id, ["step"]) is None
    assert (await service.resolve_step(scheduled.id, number=1)).status == (
        STEP_TARGET_NOT_FOUND
    )


# ── the durable (Supabase) path ──────────────────────────────────────────────


class _FakeQuery:
    def __init__(self, client, table):
        self.client = client
        self.table = table
        self.filters: list[tuple[str, object]] = []
        self.payload = None
        self.operation = "select"
        self.single = False
        self._limit = None

    def select(self, *_args, **_kwargs):
        self.operation = "select"
        return self

    def insert(self, payload):
        self.payload = payload
        self.operation = "insert"
        return self

    def update(self, payload):
        self.payload = payload
        self.operation = "update"
        return self

    def delete(self):
        self.operation = "delete"
        return self

    def eq(self, key, value):
        self.filters.append((key, value))
        return self

    def order(self, *_args, **_kwargs):
        return self

    def limit(self, value):
        self._limit = value
        return self

    def maybe_single(self):
        self.single = True
        return self

    def execute(self):
        rows = self.client.rows.setdefault(self.table, [])
        matched = [
            row
            for row in rows
            if all(row.get(key) == value for key, value in self.filters)
        ]
        if self._limit is not None:
            matched = matched[: self._limit]
        if self.operation == "insert":
            payloads = self.payload if isinstance(self.payload, list) else [self.payload]
            inserted = []
            for payload in payloads:
                row = dict(payload)
                row.setdefault("id", self.client.next_step_id())
                row.setdefault("status", "active")
                row.setdefault("version", 1)
                row.setdefault("completed_at", None)
                row.setdefault("created_at", "2026-09-27T09:00:00+00:00")
                row.setdefault("updated_at", "2026-09-27T09:00:00+00:00")
                rows.append(row)
                inserted.append(row)
            self.client.queries.append((self.table, "insert", payloads))
            data = inserted
        elif self.operation == "update":
            for row in matched:
                row.update(self.payload)
            self.client.queries.append((self.table, "update", self.payload))
            data = matched
        elif self.operation == "delete":
            for row in matched:
                rows.remove(row)
            self.client.queries.append((self.table, "delete", None))
            data = matched
        else:
            data = matched
        if self.single:
            return SimpleNamespace(data=data[0] if data else None)
        return SimpleNamespace(data=data)


class _FakeClient:
    def __init__(self):
        self.rows: dict[str, list[dict]] = {"ai_tasks": [], "todo_steps": []}
        self.queries: list[tuple] = []
        self._next_id = 100

    def next_step_id(self) -> int:
        self._next_id += 1
        return self._next_id

    def table(self, name):
        return _FakeQuery(self, name)


def _durable_repo() -> tuple[SupabaseTaskRepository, _FakeClient]:
    client = _FakeClient()
    client.rows["ai_tasks"].append(
        {
            "id": 7,
            "owner_id": OWNER,
            "label": "پروژه دانشگاه",
            "status": "active",
            "version": 1,
            "schedule_type": "todo",
            "schedule": {},
            "timezone": "UTC",
            "next_run_at": None,
            "actions": [],
            "notification_destination": {},
            "created_at": "2026-09-27T09:00:00+00:00",
            "updated_at": "2026-09-27T09:00:00+00:00",
            "terminal_at": None,
        }
    )
    return SupabaseTaskRepository(client, fallback=InMemoryTaskRepository()), client


@pytest.mark.asyncio
async def test_the_durable_path_appends_steps_in_one_statement():
    repo, client = _durable_repo()
    created = await repo.create_steps(OWNER, 7, list(STEPS[:2]))
    assert [s.position for s in created] == [1, 2]
    inserts = [entry for entry in client.queries if entry[1] == "insert"]
    assert len(inserts) == 1, "the steps must travel in ONE insert statement"
    assert isinstance(inserts[0][2], list) and len(inserts[0][2]) == 2

    more = await repo.create_steps(OWNER, 7, ["third"])
    assert [s.position for s in more] == [3]
    stored = await repo.list_steps(OWNER, 7)
    assert [s.title for s in stored] == [STEPS[0], STEPS[1], "third"]
    assert await repo.list_steps(OTHER, 7) == []


@pytest.mark.asyncio
async def test_the_durable_path_updates_and_deletes_one_step_under_cas():
    repo, _client = _durable_repo()
    step = (await repo.create_steps(OWNER, 7, ["only"]))[0]
    updated = await repo.update_step(OWNER, step.id, step.version, {"status": "completed"})
    assert updated.status == "completed" and updated.completed_at is not None
    assert updated.version == step.version + 1
    assert await repo.update_step(OWNER, step.id, step.version, {"status": "active"}) is None
    reopened = await repo.update_step(
        OWNER, step.id, updated.version, {"status": "active"}
    )
    assert reopened.completed_at is None and reopened.status == "active"
    assert await repo.delete_step(OWNER, step.id, step.version) is False
    assert await repo.delete_step(OWNER, step.id, reopened.version) is True
    assert await repo.list_steps(OWNER, 7) == []


@pytest.mark.asyncio
async def test_a_degraded_durable_write_is_never_reported_as_durable():
    client = _FakeClient()
    # The degraded store holds the same todo (as it does in production: the
    # todo itself was created while the durable store was reachable).
    fallback = InMemoryTaskRepository()
    task = await fallback.create_task(
        OWNER,
        {
            "label": "x",
            "schedule_type": "todo",
            "schedule": {},
            "timezone": "UTC",
            "actions": [],
            "notification_destination": {},
        },
    )
    repo = SupabaseTaskRepository(client, fallback=fallback)

    def _boom(*_args, **_kwargs):
        raise RuntimeError("supabase down")

    client.table = _boom  # type: ignore[assignment]
    created = await repo.create_steps(OWNER, task.id, ["step"])
    # The degraded write still "works" (the shared in-memory fallback holds the
    # step) but it is NOT durable — and the repository says so, which is what
    # every step tool reads before it claims a step was saved.
    assert repo.fallback_active is True
    assert repo.fallback_reason == "unavailable"
    assert [s.fallback_backend for s in created] == ["InMemoryTaskRepository"]
    assert await repo.list_steps(OWNER, task.id) != []


@pytest.mark.asyncio
async def test_the_step_sweep_uses_the_repository_cas_per_step():
    repo, client = _durable_repo()
    await repo.create_steps(OWNER, 7, list(STEPS))
    service = TaskManagementService(repo, OWNER)
    completed = await service.complete_todo_with_steps(7, 1)
    assert completed is not None and completed.status == "completed"
    updates = [entry for entry in client.queries if entry[1] == "update"]
    assert len(updates) >= 4  # three steps + the todo itself
    assert (await service.step_progress(7)).remaining == 0
