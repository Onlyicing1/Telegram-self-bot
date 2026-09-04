"""Regression tests for the six production task-system problems.

1. Deleted tasks must never be counted in normal task totals.
2. Delete must persist to Supabase through the CAS update (status, version,
   terminal_at) — and fallback-to-memory must be visible, never silent.
3. \"No tasks\" may only be reported when no eligible tasks exist; a Supabase
   read failure degrades to the shared in-memory fallback and is flagged.
4. The scheduler must reconstruct stored registered actions (task_list, ...)
   and execute them through the real ToolExecutor; results are delivered
   only when the task definition explicitly opts in (deliver_result).
5. User-facing task times render in Asia/Tehran via zoneinfo, and
   next_run_at follows lifecycle semantics (cleared on pause/terminal,
   recomputed on resume).
6. Scheduled execution is silent by default — no Saved Messages spam —
   and outcome notifications require an explicit notify_on_outcome opt-in.

Every scheduler test drives the REAL chain: TaskScheduler -> occurrence
claim -> TaskExecutionCoordinator -> ToolExecutor -> registered tool ->
service -> repository. No live Telegram, no live Supabase.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.ai.database.task_repository import InMemoryTaskRepository, SupabaseTaskRepository
from backend.ai.notifications import TaskNotificationService
from backend.ai.task_candidate import TaskCandidate, TaskCandidateError
from backend.ai.task_execution import TaskExecutionCoordinator
from backend.ai.task_management import TaskManagementService
from backend.ai.task_management_interface import DISPLAY_TIMEZONE, _format_datetime, list_text
from backend.ai.task_notifications import TaskOutcomeNotifier
from backend.ai.task_scheduler import TaskScheduler
from backend.ai.tools.context import ToolContext
from backend.ai.tools.executor import ToolExecutor
from backend.ai.tools.registry import create_default_registry

OWNER = 777
CHAT = -100123


def task_data(**overrides):
    data = {
        "label": "hardening",
        "schedule_type": "interval",
        "schedule": {"seconds": 3600},
        "timezone": "Asia/Tehran",
        "actions": [{"name": "send_message", "arguments": {"text": "hello"}}],
        "notification_destination": {},
    }
    data.update(overrides)
    return data


# ── Problem 1: deleted tasks are not counted ────────────────────────────────


@pytest.mark.asyncio
async def test_counts_exclude_deleted_and_normal_list_is_authoritative():
    repo = InMemoryTaskRepository()
    service = TaskManagementService(repo, 1)
    await repo.create_task(1, task_data(label="active one"))
    paused = await repo.create_task(1, task_data(label="paused one"))
    await service.pause(paused.id, paused.version)
    done = await repo.create_task(1, task_data(label="done one"))
    await service.complete(done.id, done.version)
    doomed = await repo.create_task(1, task_data(label="doomed one"))
    await service.delete(doomed.id, doomed.version)

    counts = await service.counts()
    assert counts == {
        "active": 1, "paused": 1, "completed": 1,
        "failed": 0, "expired": 0, "deleted": 1,
    }

    listed = await service.list_tasks()
    assert len(listed) == 3
    assert all(t.status != "deleted" for t in listed)
    assert sum(1 for t in listed if t.status == "active") == 1
    assert sum(1 for t in listed if t.status == "paused") == 1
    assert sum(1 for t in listed if t.status == "completed") == 1


@pytest.mark.asyncio
async def test_task_list_tool_reports_authoritative_count():
    from backend.ai.database import manager as dbm

    manager = dbm.RepositoryManager(supabase_available=False)
    repo = manager.task
    await repo.create_task(OWNER, task_data(label="counted"))
    doomed = await repo.create_task(OWNER, task_data(label="doomed"))
    await TaskManagementService(repo, OWNER).delete(doomed.id, doomed.version)

    ctx = ToolContext(telegram=None, owner_id=OWNER, tz_str="UTC", extra={})
    registry = create_default_registry(ctx)
    executor = ToolExecutor(registry, ctx)
    with patch.object(dbm, "get_repository_manager", return_value=manager):
        results = await executor.execute_calls(
            [{"name": "task_list", "arguments": {}}], owner_id=OWNER,
        )
    assert results[0].success is True
    assert results[0].data["task_count"] == 1
    assert results[0].data["fallback_active"] is False
    assert "counted" in results[0].message
    assert "doomed" not in results[0].message


# ── Problem 2: delete persistence through the repository CAS update ─────────


class _FakeQuery:
    def __init__(self, client, table_name):
        self.client, self.table_name = client, table_name
        self.filters, self.payload, self.operation = [], {}, None
        self.limit_value, self.single = None, False

    def select(self, *_args, **_kwargs):
        self.operation = "select"
        return self

    def insert(self, payload):
        self.payload, self.operation = payload, "insert"
        return self

    def update(self, payload):
        self.payload, self.operation = payload, "update"
        return self

    def eq(self, key, value):
        self.filters.append((key, value))
        return self

    def in_(self, key, values):
        self.filters.append((key, set(values)))
        return self

    def order(self, *_args, **_kwargs):
        return self

    def limit(self, value):
        self.limit_value = value
        return self

    def maybe_single(self):
        self.single = True
        return self

    def execute(self):
        self.client.last_query = self
        if self.client.error:
            raise self.client.error
        rows = self.client.rows[self.table_name]
        matches = [
            r for r in rows
            if all((r.get(k) in v if isinstance(v, set) else r.get(k) == v) for k, v in self.filters)
        ]
        if self.limit_value is not None:
            matches = matches[: self.limit_value]
        if self.operation == "insert":
            row = dict(self.payload)
            row.setdefault("id", self.client.next_id[self.table_name])
            self.client.next_id[self.table_name] += 1
            rows.append(row)
            matches = [row]
        elif self.operation == "update":
            for row in matches:
                row.update(self.payload)
        data = matches[0] if self.single else (matches[:1] if self.operation in ("insert", "update") else matches)
        return SimpleNamespace(data=data)


class _FakeClient:
    def __init__(self, task_rows=None, occurrence_rows=None, error=None):
        self.rows = {
            "ai_tasks": [dict(r) for r in (task_rows or [])],
            "ai_task_occurrences": [dict(r) for r in (occurrence_rows or [])],
        }
        self.next_id = {"ai_tasks": 100, "ai_task_occurrences": 200}
        self.error = error
        self.last_query = None

    def table(self, name):
        return _FakeQuery(self, name)


def row_task(**overrides):
    row = {
        "id": 7, "owner_id": 10, "label": "hardening", "status": "active", "version": 1,
        "schedule_type": "interval", "schedule": {"seconds": 3600}, "timezone": "Asia/Tehran",
        "next_run_at": None,
        "actions": [{"name": "send_message", "arguments": {"text": "hello"}}],
        "notification_destination": {},
        "created_at": "2026-08-29T08:00:00+00:00",
        "updated_at": "2026-08-29T08:00:00+00:00",
        "terminal_at": None,
    }
    row.update(overrides)
    return row


@pytest.mark.asyncio
async def test_supabase_delete_persists_cas_update_and_terminal_at():
    client = _FakeClient([row_task()])
    repo = SupabaseTaskRepository(client, InMemoryTaskRepository())

    deleted = await repo.transition_task(10, 7, "deleted", expected_version=1)
    assert deleted is not None and deleted.status == "deleted"
    assert deleted.version == 2 and deleted.terminal_at is not None

    query = client.last_query
    assert query.table_name == "ai_tasks" and query.operation == "update"
    assert ("id", 7) in query.filters
    assert ("owner_id", 10) in query.filters
    assert ("version", 1) in query.filters  # atomic CAS on the row version
    assert query.payload["status"] == "deleted"
    assert query.payload["version"] == 2
    assert "terminal_at" in query.payload

    # The row in the "database" actually changed — persistence, not UI-only.
    stored = await repo.get_task(10, 7)
    assert stored.status == "deleted" and stored.version == 2

    # Idempotent deleted -> deleted never overwrites the original terminal_at.
    again = await repo.transition_task(10, 7, "deleted", expected_version=2)
    assert again is not None and again.status == "deleted"
    assert "terminal_at" not in client.last_query.payload
    assert again.terminal_at == deleted.terminal_at


@pytest.mark.asyncio
async def test_supabase_stale_version_delete_does_not_persist():
    client = _FakeClient([row_task()])
    repo = SupabaseTaskRepository(client, InMemoryTaskRepository())
    assert await repo.transition_task(10, 7, "deleted", expected_version=99) is None
    assert (await repo.get_task(10, 7)).status == "active"
    assert client.last_query.operation == "select"  # no update was issued


# ── Problems 2/3: fallback is visible and never silently durable ────────────


@pytest.mark.asyncio
async def test_supabase_failure_marks_fallback_and_recovers():
    fallback = InMemoryTaskRepository()
    client = _FakeClient([row_task()], error=RuntimeError("unreachable"))
    repo = SupabaseTaskRepository(client, fallback)

    created = await repo.create_task(10, task_data(label="memory only"))
    assert created.fallback_backend == "InMemoryTaskRepository"
    assert repo.fallback_active is True

    rows = await repo.list_tasks(10)
    assert [r.label for r in rows] == ["memory only"]

    client.error = None
    await repo.get_task(10, 7)
    assert repo.fallback_active is False


@pytest.mark.asyncio
async def test_task_list_marks_memory_fallback_honestly():
    fallback = InMemoryTaskRepository()
    await fallback.create_task(10, task_data(label="in-memory only"))
    repo = SupabaseTaskRepository(_FakeClient([], error=RuntimeError("down")), fallback)
    service = TaskManagementService(repo, 10)

    text = await list_text(service)
    assert "in-memory only" in text
    assert "Memory fallback" in text

    # Empty fallback + Supabase down: "no tasks" is honest AND flagged.
    empty = SupabaseTaskRepository(_FakeClient([], error=RuntimeError("down")), InMemoryTaskRepository())
    text2 = await list_text(TaskManagementService(empty, 10))
    assert "No tasks found." in text2
    assert "Memory fallback" in text2


@pytest.mark.asyncio
async def test_get_task_repository_is_the_manager_singleton():
    from backend.ai.database import manager as mgr
    from backend.ai.database.task_repository import get_task_repository

    holder = SimpleNamespace(task=object())
    with patch.object(mgr, "get_repository_manager", return_value=holder):
        assert get_task_repository() is holder.task
        assert get_task_repository() is holder.task


# ── Problem 3: "no tasks" only when no eligible tasks exist ─────────────────


@pytest.mark.asyncio
async def test_no_tasks_only_when_no_eligible_tasks_exist():
    repo = InMemoryTaskRepository()
    service = TaskManagementService(repo, 1)
    doomed = await repo.create_task(1, task_data())
    await service.delete(doomed.id, doomed.version)

    # Only terminal deleted rows exist -> "no tasks" is the correct answer.
    assert await service.list_tasks() == []
    assert "No tasks found." in await list_text(service)

    # An active task makes the list non-empty again.
    await repo.create_task(1, task_data(label="visible"))
    listed = await service.list_tasks()
    assert len(listed) == 1 and listed[0].label == "visible"
    text = await list_text(service)
    assert "No tasks found." not in text and "visible" in text


# ── Problem 4: stored registered actions execute through the ToolExecutor ───


def _coordinator_and_repo(telegram, destination=None, actions=None):
    from backend.ai.database import manager as dbm

    manager = dbm.RepositoryManager(supabase_available=False)
    repo = manager.task
    ctx = ToolContext(telegram=telegram, owner_id=OWNER, tz_str="Asia/Tehran", extra={})
    registry = create_default_registry(ctx)
    executor = ToolExecutor(registry, ctx)
    coordinator = TaskExecutionCoordinator(repo, executor, OWNER, ctx)
    return manager, repo, coordinator, ctx


async def _create_due_task(repo, *, destination, actions):
    return await repo.create_task(OWNER, {
        "label": "daily report",
        "schedule_type": "interval",
        "schedule": {"seconds": 3600},
        "timezone": "Asia/Tehran",
        "actions": actions,
        "notification_destination": destination,
        "next_run_at": datetime.now(timezone.utc) - timedelta(seconds=5),
    })


@pytest.mark.asyncio
async def test_scheduler_executes_stored_registered_action_and_delivers_result():
    from backend.ai.database import manager as dbm

    telegram = MagicMock()
    telegram.send_message = AsyncMock(return_value={"id": 1})
    manager, repo, coordinator, _ctx = _coordinator_and_repo(telegram)
    task = await _create_due_task(
        repo,
        destination={"chat_id": CHAT, "deliver_result": True},
        actions=[{"name": "task_list", "arguments": {}}],
    )
    with patch.object(dbm, "get_repository_manager", return_value=manager):
        scheduler = TaskScheduler(repo, OWNER, coordinator, outcome_notifier=None)
        processed = await scheduler.run_once(datetime.now(timezone.utc))

    assert processed == 1
    occurrence = (await repo.list_occurrences(OWNER, task.id))[0]
    assert occurrence.status == "succeeded"
    assert occurrence.result_metadata.get("terminal_status") == "succeeded"

    # next_run_at advanced to the following occurrence.
    updated = await repo.get_task(OWNER, task.id)
    assert updated.next_run_at is not None
    assert updated.next_run_at > datetime.now(timezone.utc)

    # The task_list result reached the task's destination chat — the task
    # is an execution surface, not a silent timer.
    telegram.send_message.assert_awaited_once()
    chat, text = telegram.send_message.await_args.args
    assert chat == CHAT
    assert "Task #" in text and "daily report" in text


@pytest.mark.asyncio
async def test_scheduler_execution_is_silent_without_deliver_result():
    from backend.ai.database import manager as dbm

    telegram = MagicMock()
    telegram.send_message = AsyncMock(return_value={"id": 1})
    manager, repo, coordinator, _ctx = _coordinator_and_repo(telegram)
    task = await _create_due_task(
        repo,
        destination={"chat_id": CHAT},
        actions=[{"name": "task_list", "arguments": {}}],
    )
    with patch.object(dbm, "get_repository_manager", return_value=manager):
        scheduler = TaskScheduler(repo, OWNER, coordinator, outcome_notifier=None)
        processed = await scheduler.run_once(datetime.now(timezone.utc))

    assert processed == 1
    assert (await repo.list_occurrences(OWNER, task.id))[0].status == "succeeded"
    telegram.send_message.assert_not_awaited()


# ── Problem 6: no Saved Messages spam by default ────────────────────────────


@pytest.mark.asyncio
async def test_outcome_notification_requires_explicit_opt_in():
    repo = InMemoryTaskRepository()
    sent = []

    async def sender(owner, message):
        sent.append((owner, message))
        return True

    notifier = TaskOutcomeNotifier(repo, TaskNotificationService(sender, 1), 1)

    def _occurrence(task, key):
        return repo.create_occurrence(1, {
            "task_id": task.id, "occurrence_key": key, "definition_version": task.version,
            "action_snapshot": task.actions, "scheduled_for": datetime(2026, 1, 1, tzinfo=timezone.utc),
        })

    async def _claim_and_succeed(task, key):
        await repo.claim_occurrence(1, task.id, key)
        await repo.transition_occurrence(1, task.id, key, "succeeded")

    # Explicit opt-in notifies.
    opted = await repo.create_task(
        1, task_data(label="opted", notification_destination={"notify_on_outcome": True})
    )
    await _occurrence(opted, "k1")
    await _claim_and_succeed(opted, "k1")
    assert await notifier.notify_persisted(opted.id, "k1", "succeeded") is True
    assert len(sent) == 1 and sent[0][0] == 1 and "succeeded" in sent[0][1]

    # Default destination: silent — no Saved Messages execution spam.
    quiet = await repo.create_task(1, task_data(label="quiet", notification_destination={}))
    await _occurrence(quiet, "k2")
    await _claim_and_succeed(quiet, "k2")
    assert await notifier.notify_persisted(quiet.id, "k2", "succeeded") is False
    assert len(sent) == 1

    # Deleted task never notifies, even with the flag.
    doomed = await repo.create_task(
        1, task_data(label="doomed", notification_destination={"notify_on_outcome": True})
    )
    await _occurrence(doomed, "k3")
    await repo.transition_task(1, doomed.id, "deleted", expected_version=doomed.version)
    await _claim_and_succeed(doomed, "k3")
    assert await notifier.notify_persisted(doomed.id, "k3", "succeeded") is False
    assert len(sent) == 1


@pytest.mark.asyncio
async def test_scheduler_execution_does_not_notify_by_default():
    from backend.ai.database import manager as dbm

    telegram = MagicMock()
    telegram.send_message = AsyncMock(return_value={"id": 1})
    manager, repo, coordinator, _ctx = _coordinator_and_repo(telegram)
    task = await _create_due_task(
        repo,
        destination={},  # no chat, no flags — full silence expected
        actions=[{"name": "task_list", "arguments": {}}],
    )
    sent = []

    async def sender(owner, message):
        sent.append((owner, message))
        return True

    notifier = TaskOutcomeNotifier(repo, TaskNotificationService(sender, OWNER), OWNER)
    with patch.object(dbm, "get_repository_manager", return_value=manager):
        scheduler = TaskScheduler(repo, OWNER, coordinator, outcome_notifier=notifier)
        processed = await scheduler.run_once(datetime.now(timezone.utc))

    assert processed == 1
    assert (await repo.list_occurrences(OWNER, task.id))[0].status == "succeeded"
    assert sent == []  # no status notification
    telegram.send_message.assert_not_awaited()  # no result delivery


# ── Problem 5: Asia/Tehran display + next_run lifecycle ─────────────────────


def test_task_times_display_in_tehran_local_time_via_zoneinfo():
    from zoneinfo import ZoneInfo

    assert DISPLAY_TIMEZONE == "Asia/Tehran"

    june = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
    expected_june = june.astimezone(ZoneInfo(DISPLAY_TIMEZONE))
    assert _format_datetime(june, empty="") == (
        f"{expected_june.strftime('%Y-%m-%d %H:%M')} {expected_june.tzname()}"
    )

    january = datetime(2026, 1, 2, 9, 30, tzinfo=timezone.utc)
    expected_january = january.astimezone(ZoneInfo(DISPLAY_TIMEZONE))
    assert _format_datetime(january, empty="") == (
        f"{expected_january.strftime('%Y-%m-%d %H:%M')} {expected_january.tzname()}"
    )

    # Naive datetimes are treated as UTC, then converted — never left as-is.
    naive = datetime(2026, 6, 1, 12, 0)
    assert _format_datetime(naive, empty="") == _format_datetime(june, empty="")

    # None and empty markers are preserved.
    assert _format_datetime(None, empty="Not scheduled") == "Not scheduled"


def test_taskloom_formats_times_in_tehran():
    from backend.bot.handlers import taskloom

    value = datetime(2026, 1, 2, 9, 30, tzinfo=timezone.utc)
    assert taskloom._fmt_dt(value) == "01-02 13:00"
    assert taskloom._fmt_dt(None) == "—"


@pytest.mark.asyncio
async def test_pause_clears_next_run_and_resume_recomputes():
    repo = InMemoryTaskRepository()
    future = datetime.now(timezone.utc) + timedelta(hours=1)
    task = await repo.create_task(1, task_data(next_run_at=future))
    service = TaskManagementService(repo, 1)

    paused = await service.pause(task.id, task.version)
    assert paused.status == "paused"
    assert paused.next_run_at is None

    resumed = await service.resume(paused.id, paused.version)
    assert resumed.status == "active"
    assert resumed.next_run_at is not None
    assert resumed.next_run_at > datetime.now(timezone.utc)


@pytest.mark.asyncio
async def test_terminal_transitions_clear_next_run():
    repo = InMemoryTaskRepository()
    future = datetime.now(timezone.utc) + timedelta(hours=1)
    service = TaskManagementService(repo, 1)
    for status in ("completed", "deleted"):
        task = await repo.create_task(1, task_data(label=status, next_run_at=future))
        changed = await service.set_status(task.id, status, task.version)
        assert changed.status == status
        assert changed.next_run_at is None


@pytest.mark.asyncio
async def test_resume_of_unreschedulable_schedule_fails_closed():
    # A non-canonical interval schedule cannot be parsed on resume; the task
    # still resumes (status change) and simply stays unscheduled instead of
    # advertising a wrong run time.
    repo = InMemoryTaskRepository()
    task = await repo.create_task(1, {
        "label": "odd schedule",
        "schedule_type": "interval",
        "schedule": {"interval_seconds": 60},
        "timezone": "Asia/Tehran",
        "actions": [{"name": "send_message", "arguments": {"text": "x"}}],
        "notification_destination": {},
        "next_run_at": datetime.now(timezone.utc) + timedelta(hours=1),
    })
    service = TaskManagementService(repo, 1)
    paused = await service.pause(task.id, task.version)
    assert paused.next_run_at is None
    resumed = await service.resume(paused.id, paused.version)
    assert resumed.status == "active" and resumed.next_run_at is None


# ── Candidate contract: destination flags are validated ─────────────────────


def _candidate_dict(destination):
    return {
        "label": "flag check",
        "schedule_type": "interval",
        "schedule": {"seconds": 60},
        "timezone": "Asia/Tehran",
        "actions": [{"name": "task_list", "arguments": {}}],
        "notification_destination": destination,
    }


def test_candidate_destination_flags_must_be_booleans():
    with pytest.raises(TaskCandidateError):
        TaskCandidate.from_untrusted(_candidate_dict({"deliver_result": "yes"}))
    with pytest.raises(TaskCandidateError):
        TaskCandidate.from_untrusted(_candidate_dict({"notify_on_outcome": 1}))

    ok = TaskCandidate.from_untrusted(
        _candidate_dict({"deliver_result": True, "notify_on_outcome": False})
    )
    assert ok.notification_destination == {"deliver_result": True, "notify_on_outcome": False}