"""Durable task scheduling coordination and execution handoff."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Awaitable, Callable

from backend.ai.database.task_repository import OccurrenceRecord, TaskRepository
from backend.ai.scheduling import ScheduleError, catch_up_occurrence, parse_schedule

logger = logging.getLogger(__name__)

MAX_TASKS_PER_WAKE = 10
MAX_RECOVERY_PER_START = 100
MAX_RETRIES_PER_WAKE = 10
WAKE_INTERVAL_SECONDS = 60.0


def occurrence_key(task_id: int, scheduled_for: datetime) -> str:
    if scheduled_for.tzinfo is None:
        raise ScheduleError("scheduled occurrence must be timezone-aware")
    return f"{task_id}:{scheduled_for.astimezone(timezone.utc).isoformat()}"


class TaskScheduler:
    """One process-local coordinator backed by durable repository state."""

    def __init__(
        self,
        repository: TaskRepository,
        owner_id: int,
        execution_coordinator=None,
    ) -> None:
        self.repository = repository
        self.owner_id = owner_id
        self.execution_coordinator = execution_coordinator
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        await self.recover()
        self._task = asyncio.create_task(self.run(), name="lifeos-task-scheduler")

    async def stop(self) -> None:
        self._stop.set()
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def recover(self) -> int:
        recovered = 0
        for occurrence in await self.repository.list_recoverable_occurrences(self.owner_id, MAX_RECOVERY_PER_START):
            if occurrence.status in {"claimed", "running"}:
                result = await self.repository.transition_occurrence(
                    self.owner_id, occurrence.task_id, occurrence.occurrence_key, "interrupted"
                )
                recovered += result is not None
        return recovered

    async def _execute_claimed(self, occurrence: OccurrenceRecord) -> bool:
        coordinator = self.execution_coordinator
        if coordinator is None:
            return False
        claimed = await self.repository.claim_occurrence(
            self.owner_id, occurrence.task_id, occurrence.occurrence_key
        )
        if claimed is None or claimed.status != "running":
            return False
        await coordinator.execute(claimed)
        return True

    async def _run_due_retries(self, reference: datetime) -> int:
        processed = 0
        for occurrence in await self.repository.list_due_retry_occurrences(
            self.owner_id, reference, MAX_RETRIES_PER_WAKE
        ):
            try:
                processed += await self._execute_claimed(occurrence)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Retry execution failed for occurrence %s", occurrence.occurrence_key)
        return processed

    async def run_once(self, now: datetime | None = None) -> int:
        reference = now or datetime.now(timezone.utc)
        processed = await self._run_due_retries(reference)
        for task in await self.repository.list_due_tasks(self.owner_id, reference, MAX_TASKS_PER_WAKE):
            try:
                schedule = parse_schedule(task.schedule_type, task.schedule)
                scheduled, following = catch_up_occurrence(schedule, task.next_run_at, reference)
                key = occurrence_key(task.id, scheduled)
                occurrence = await self.repository.create_occurrence(self.owner_id, {
                    "task_id": task.id, "occurrence_key": key, "definition_version": task.version,
                    "action_snapshot": task.actions, "scheduled_for": scheduled,
                })
                claimed = True
                if self.execution_coordinator is not None:
                    claimed = await self._execute_claimed(occurrence)
                if following is None or task.schedule_type == "once":
                    next_run = None
                else:
                    next_run = following
                await self.repository.advance_next_run(self.owner_id, task.id, task.version, next_run)
                processed += occurrence is not None and claimed
            except (ScheduleError, ValueError) as exc:
                logger.warning("Task %s was not scheduled: %s", task.id, exc)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Task %s scheduling failed", task.id)
        return processed

    async def run(self) -> None:
        try:
            while not self._stop.is_set():
                await self.run_once()
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=WAKE_INTERVAL_SECONDS)
                except asyncio.TimeoutError:
                    continue
        except asyncio.CancelledError:
            raise
