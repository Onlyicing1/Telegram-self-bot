"""Durable task scheduling coordination and execution handoff."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from backend.ai.database.task_repository import OccurrenceRecord, TaskRepository
from backend.ai.retry import can_retry, retry_delay
from backend.ai.scheduling import ScheduleError, catch_up_occurrence, parse_schedule

logger = logging.getLogger(__name__)

MAX_TASKS_PER_WAKE = 10
MAX_RECOVERY_PER_START = 100
MAX_RETRIES_PER_WAKE = 10
WAKE_INTERVAL_SECONDS = 60.0
# Prepare-ahead: AI-assisted occurrences whose boundary is within this
# horizon are prepared (content generated + validated, NO side effects)
# during the interval BEFORE the boundary. The wake loop does not wait for
# preparation; each preparation is one tracked, bounded task per occurrence
# key, cancelled on stop. Static tasks never enter this path.
PREPARE_AHEAD_HORIZON_SECONDS = 120.0
# Worst case: bounded preparation rounds x per-round provider timeout.
PREPARE_AHEAD_TIMEOUT_SECONDS = 150.0


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
        outcome_notifier=None,
    ) -> None:
        self.repository = repository
        self.owner_id = owner_id
        self.execution_coordinator = execution_coordinator
        self.outcome_notifier = outcome_notifier
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._recovery_lock = asyncio.Lock()
        self._preparations: dict[str, asyncio.Task] = {}

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
        pending = list(self._preparations.values())
        self._preparations.clear()
        for preparation in pending:
            preparation.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    async def recover(self) -> int:
        """Resolve occurrences left unfinished by a previous process.

        Serialized and single-pass: a concurrent recovery returns 0 instead
        of re-resolving the same occurrences, so attempt counts can never
        multiply across restarts or racing recoveries. A ValueError from a
        transition means another writer moved the occurrence first — the
        persisted state already decides the outcome, so it is skipped.
        """
        if self._recovery_lock.locked():
            return 0
        async with self._recovery_lock:
            recovered = 0
            now = datetime.now(timezone.utc)
            for occurrence in await self.repository.list_recoverable_occurrences(self.owner_id, MAX_RECOVERY_PER_START):
                try:
                    # A pre-created occurrence whose boundary is still in the
                    # future was never started — it must stay untouched so the
                    # wake loop executes it exactly once AT its boundary (with
                    # its durably prepared action if one was persisted).
                    # Recovery must not arm a backoff on it or execute early.
                    if (
                        occurrence.status == "claimed"
                        and occurrence.scheduled_for is not None
                        and occurrence.scheduled_for > now
                    ):
                        continue
                    if occurrence.status in {"claimed", "running"}:
                        occurrence = await self.repository.transition_occurrence(
                            self.owner_id, occurrence.task_id, occurrence.occurrence_key, "interrupted"
                        )
                        if occurrence is None:
                            continue
                    if occurrence.status == "interrupted":
                        resolved = await self._resolve_interrupted(occurrence)
                        recovered += resolved is not None
                except ValueError:
                    continue
            return recovered

    async def _resolve_interrupted(self, occurrence: OccurrenceRecord) -> OccurrenceRecord | None:
        metadata = {"error_class": "restart_interrupted", "attempt": occurrence.attempt}
        if can_retry(occurrence.attempt):
            return await self.repository.transition_occurrence(
                self.owner_id, occurrence.task_id, occurrence.occurrence_key,
                "retry_pending",
                retry_at=occurrence.updated_at + retry_delay(occurrence.attempt),
                attempt=occurrence.attempt + 1,
                finished_at=None,
                error_metadata=metadata,
            )
        return await self.repository.transition_occurrence(
            self.owner_id, occurrence.task_id, occurrence.occurrence_key,
            "failed",
            retry_at=None,
            error_metadata=metadata,
        )

    async def _notify_outcome(self, task_id: int, occurrence_key: str, status: str) -> None:
        notifier = self.outcome_notifier
        if notifier is None:
            return
        if status not in ("succeeded", "failed", "retry_pending", "cancelled"):
            return
        try:
            await notifier.notify_persisted(task_id, occurrence_key, status)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Task notification failed for %s", occurrence_key)

    async def _execute_claimed(self, occurrence: OccurrenceRecord) -> bool:
        coordinator = self.execution_coordinator
        if coordinator is None:
            return False
        claimed = await self.repository.claim_occurrence(
            self.owner_id, occurrence.task_id, occurrence.occurrence_key
        )
        if claimed is None or claimed.status != "running":
            return False
        result = await coordinator.execute(claimed)
        await self._notify_outcome(
            occurrence.task_id, occurrence.occurrence_key, getattr(result, "status", "unknown")
        )
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
                claimed = False
                if self.execution_coordinator is not None:
                    # Only an occurrence whose persisted state proves it was
                    # never started may be executed here. retry_pending is
                    # owned by the bounded retry path (which honors retry_at —
                    # critical after a restart, where recovery has just armed
                    # the backoff), and running/interrupted/terminal states
                    # belong to recovery or are already finished. The claim
                    # CAS below remains the final duplicate guard.
                    if occurrence.status == "claimed":
                        claimed = await self._execute_claimed(occurrence)
                elif occurrence.status == "claimed":
                    # No execution authority: park the occurrence for
                    # deterministic recovery (same contract as the event
                    # dispatcher) instead of leaving it silently claimed.
                    try:
                        await self.repository.transition_occurrence(
                            self.owner_id, task.id, key, "interrupted"
                        )
                    except ValueError:
                        pass
                    claimed = True
                if following is None or task.schedule_type == "once":
                    next_run = None
                else:
                    next_run = following
                await self.repository.advance_next_run(self.owner_id, task.id, task.version, next_run)
                processed += occurrence is not None and claimed
                if following is not None and task.schedule_type != "once":
                    await self._prepare_next_ahead(task, following, reference)
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

    async def _prepare_next_ahead(self, task, boundary: datetime, reference: datetime) -> None:
        """Ensure the NEXT occurrence of a recurring AI task is prepared before
        its boundary (content only — never a Telegram side effect).

        The occurrence is created idempotently (deterministic key). Only an
        unstarted (``claimed``) occurrence without durable preparation is
        prepared, and at most one bounded tracked task per occurrence key
        runs at a time; the wake loop never waits for it. If preparation
        misses the boundary, execution still happens through the existing
        occurrence-time preparation path — honestly, never as a guessed run.
        """
        coordinator = self.execution_coordinator
        if coordinator is None or not hasattr(coordinator, "prepare_ahead"):
            return
        instruction = getattr(task, "ai_instruction", None)
        if not isinstance(instruction, str) or not instruction.strip():
            return
        if (boundary - reference).total_seconds() > PREPARE_AHEAD_HORIZON_SECONDS:
            return
        try:
            occurrence = await self.repository.create_occurrence(self.owner_id, {
                "task_id": task.id,
                "occurrence_key": occurrence_key(task.id, boundary),
                "definition_version": task.version,
                "action_snapshot": task.actions,
                "scheduled_for": boundary,
            })
        except (ValueError, TypeError):
            return
        if occurrence is None or occurrence.status != "claimed":
            return
        if occurrence.preparation_metadata:
            return
        key = occurrence.occurrence_key
        existing = self._preparations.get(key)
        if existing is not None and not existing.done():
            return
        self._preparations[key] = asyncio.create_task(
            self._run_preparation(occurrence), name=f"lifeos-task-prepare:{key}"
        )

    async def _run_preparation(self, occurrence) -> None:
        try:
            await asyncio.wait_for(
                self.execution_coordinator.prepare_ahead(occurrence),
                timeout=PREPARE_AHEAD_TIMEOUT_SECONDS,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Prepare-ahead failed for occurrence %s", occurrence.occurrence_key)
        finally:
            self._preparations.pop(occurrence.occurrence_key, None)