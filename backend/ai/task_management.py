"""Owner-scoped operational management for durable tasks."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from backend.ai.database.task_repository import (
    TASK_STATUSES,
    TaskDeletionResult,
    TaskRecord,
    TaskRepository,
)
from backend.ai.scheduling import ScheduleError, advance_interval, next_occurrence, parse_schedule

logger = logging.getLogger(__name__)

# next_run_at must never advertise an execution for these states: the
# scheduler only runs active tasks, so a paused/completed/deleted/... task
# with a future next_run_at would lie in the UI. Resume recomputes the next
# occurrence from the stored schedule.
_NEXT_RUN_CLEAR_STATUSES = frozenset({"paused", "completed", "failed", "expired", "deleted"})


@dataclass(frozen=True)
class TaskView:
    task: TaskRecord
    occurrences: list[Any]


@dataclass(frozen=True)
class TaskListSnapshot:
    """One authoritative read of the owner's task list.

    ``tasks`` and ``fallback_active`` come from the SAME repository call with
    no await in between, so a concurrent operation can never clear the
    degraded marker while the content it describes is still rendered. Callers
    that both render and count the list must use one snapshot instead of two
    independent reads.
    """

    tasks: list[TaskRecord]
    fallback_active: bool


class TaskManagementService:
    def __init__(self, repository: TaskRepository, owner_id: int) -> None:
        self.repository = repository
        self.owner_id = owner_id

    async def list_tasks(self, status: str | None = None) -> list[TaskRecord]:
        """List the owner's tasks, optionally filtered by status.

        Filtering happens here (owner-scoped) so repository interfaces stay
        unchanged; task volumes are small. Status values are the record's
        canonical strings (active / paused / completed / ...).

        Deletion is a real row removal, so a deleted task is simply absent
        from the repository. The unfiltered list still excludes a
        ``deleted`` status defensively: that status is no longer produced,
        but a pre-existing legacy row that carries it must not reappear in
        the normal collection. An explicit ``status`` filter matches the
        record's exact status and is never widened.
        """
        tasks = await self.repository.list_tasks(self.owner_id)
        if status is None:
            return [
                t for t in tasks if str(getattr(t, "status", "") or "") != "deleted"
            ]
        return [t for t in tasks if str(getattr(t, "status", "") or "") == status]

    async def snapshot(self, status: str | None = None) -> TaskListSnapshot:
        """Read the owner's task list once, with the fallback marker bound to it."""
        tasks = await self.list_tasks(status=status)
        return TaskListSnapshot(
            tasks=tasks,
            fallback_active=bool(getattr(self.repository, "fallback_active", False)),
        )

    async def counts(self) -> dict[str, int]:
        """Per-status counts of the owner's durable tasks.

        ``deleted`` is a terminal state: it is never part of the normal task
        collection, so every normal summary (active / paused / completed /
        failed / expired totals) derives from this method and can never
        inflate with deleted tasks. The ``deleted`` key is still reported so
        diagnostics can see the terminal population separately.
        """
        tasks = await self.repository.list_tasks(self.owner_id)
        result = {status: 0 for status in TASK_STATUSES}
        for task in tasks:
            status = str(getattr(task, "status", "") or "")
            if status in result:
                result[status] += 1
        return result

    async def inspect(self, task_id: int, occurrence_limit: int = 100) -> TaskView | None:
        task = await self.repository.get_task(self.owner_id, task_id)
        if task is None:
            return None
        occurrences = await self.repository.list_occurrences(self.owner_id, task_id, occurrence_limit)
        return TaskView(task, occurrences)

    async def set_status(self, task_id: int, status: str, expected_version: int) -> TaskRecord | None:
        task = await self.repository.get_task(self.owner_id, task_id)
        if task is None:
            return None
        updates: dict[str, Any] = {"status": status}
        if status in _NEXT_RUN_CLEAR_STATUSES and task.next_run_at is not None:
            # Pause and terminal states must not advertise another run; the
            # scheduler only executes active tasks anyway.
            updates["next_run_at"] = None
        elif status == "active" and task.status == "paused" and task.next_run_at is None:
            # Resume: recompute the next occurrence from the stored schedule
            # so the task actually runs again. Interval tasks have no anchor
            # (the previous occurrence was never created), so schedule the
            # first run one interval from now.
            recomputed = await self._resume_next_run(task)
            if recomputed is not None:
                updates["next_run_at"] = recomputed
        return await self.repository.update_task(
            self.owner_id, task_id, expected_version, updates
        )

    async def _resume_next_run(self, task: TaskRecord) -> datetime | None:
        if task.schedule_type == "event":
            # Event-triggered tasks have no wall-clock schedule; resuming
            # them never fabricates a next run time.
            return None
        try:
            schedule = parse_schedule(task.schedule_type, task.schedule)
            now = datetime.now(timezone.utc)
            if task.schedule_type == "interval":
                interval = getattr(schedule, "interval", None)
                if interval is None or interval.total_seconds() <= 0:
                    return None
                return advance_interval(now, interval, now)
            return next_occurrence(schedule, now)
        except (ScheduleError, TypeError, ValueError):
            logger.warning(
                "Task %s could not be rescheduled on resume; next_run_at stays unset",
                task.id,
            )
            return None

    async def pause(self, task_id: int, expected_version: int) -> TaskRecord | None:
        return await self.set_status(task_id, "paused", expected_version)

    async def resume(self, task_id: int, expected_version: int) -> TaskRecord | None:
        return await self.set_status(task_id, "active", expected_version)

    async def complete(self, task_id: int, expected_version: int) -> TaskRecord | None:
        return await self.set_status(task_id, "completed", expected_version)

    async def fail(self, task_id: int, expected_version: int) -> TaskRecord | None:
        return await self.set_status(task_id, "failed", expected_version)

    async def expire(self, task_id: int, expected_version: int) -> TaskRecord | None:
        return await self.set_status(task_id, "expired", expected_version)

    async def delete(self, task_id: int, expected_version: int) -> TaskDeletionResult:
        """Physically delete the owner's task row (never a status write).

        Deletion is a real repository removal (``delete_task``): the durable
        row and its occurrences are gone, so the task leaves every list
        because it no longer exists. The CAS ``expected_version`` still
        guards stale writers, and the result distinguishes a durable removal
        from a missing task, a stale version, and a degraded (in-memory)
        deletion that must never be reported as durable.
        """
        return await self.repository.delete_task(
            self.owner_id, task_id, expected_version
        )
