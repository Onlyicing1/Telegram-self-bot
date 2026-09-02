"""Deterministic task creation boundary for authorized callers."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from backend.ai.database.task_repository import TaskRecord, TaskRepository
from backend.ai.scheduling import (
    ScheduleError,
    advance_interval,
    next_occurrence,
    parse_schedule,
)
from backend.ai.task_candidate import TaskCandidate
from backend.ai.task_contract import AIInstruction, validate_ai_instruction
from backend.ai.task_trace import bound_text, task_trace

logger = logging.getLogger(__name__)


def _creation_trace(stage: str, **fields: Any) -> None:
    # Correlated AI_TASK_TRACE record when a create_task request is bound;
    # silent for direct service callers (tests, .task command without a trace).
    task_trace(stage, **fields)


class TaskCreationError(ValueError):
    """Candidate task data is invalid or cannot be scheduled."""


class TaskCreationService:
    def __init__(self, repository: TaskRepository, owner_id: int) -> None:
        if not isinstance(owner_id, int) or owner_id <= 0:
            raise TaskCreationError("owner identity is required")
        self.repository = repository
        self.owner_id = owner_id

    async def create(self, candidate: dict[str, Any], reference: datetime) -> TaskRecord:
        started = time.perf_counter()

        def _invalid(reason: str) -> TaskCreationError:
            _creation_trace(
                "rejected", reason=reason,
                elapsed_s=round(time.perf_counter() - started, 2),
            )
            return TaskCreationError(reason)

        _creation_trace(
            "task_validation_start", repo_type=type(self.repository).__name__,
            candidate_fields=len(candidate) if isinstance(candidate, dict) else 0,
        )
        if isinstance(candidate, TaskCandidate):
            candidate = candidate.as_creation_candidate()
        if not isinstance(candidate, dict):
            raise _invalid("task candidate must be an object")
        if not isinstance(reference, datetime) or reference.tzinfo is None:
            raise TaskCreationError("reference datetime must be timezone-aware")
        required = {"label", "schedule_type", "schedule", "timezone", "actions", "notification_destination"}
        allowed = required | {"next_run_at", "ai_instruction"}
        if set(candidate) - allowed:
            raise _invalid(f"unsupported task fields: {sorted(set(candidate) - allowed)}")
        if required - set(candidate):
            raise _invalid(f"missing required task fields: {sorted(required - set(candidate))}")
        if candidate.get("timezone") != candidate["schedule"].get("timezone") and candidate["schedule_type"] != "interval":
            raise _invalid(
                "task and schedule timezones must match "
                f"(task={candidate.get('timezone')} schedule={candidate['schedule'].get('timezone')})"
            )
        try:
            schedule = parse_schedule(candidate["schedule_type"], candidate["schedule"])
            initial = candidate.get("next_run_at")
            if initial is None:
                if candidate["schedule_type"] == "interval":
                    # A brand-new recurring interval task has no previous
                    # occurrence, so `next_occurrence` (which requires one for
                    # intervals) cannot anchor it. Schedule the first run one
                    # interval after the reference time; the scheduler advances
                    # subsequent occurrences through its normal catch-up path.
                    interval = getattr(schedule, "interval", None)
                    if not isinstance(interval, timedelta) or interval <= timedelta(0):
                        raise ScheduleError("interval must be positive")
                    initial = advance_interval(reference, interval, reference)
                else:
                    initial = next_occurrence(schedule, reference)
        except (ScheduleError, TypeError, ValueError) as exc:
            _creation_trace("schedule_invalid", schedule_type=str(candidate.get("schedule_type")), error=str(exc)[:120])
            raise TaskCreationError(str(exc)) from exc
        payload = {key: candidate[key] for key in required}
        if candidate.get("ai_instruction") is not None:
            instruction = candidate["ai_instruction"]
            if isinstance(instruction, dict):
                if instruction.get("kind") != "ai_instruction":
                    raise TaskCreationError("AI instruction kind is invalid")
                instruction = instruction.get("text")
            payload["ai_instruction"] = validate_ai_instruction(instruction)
        logger.info(
            "TASK_CREATE_PERSIST_ATTEMPT repository=%s owner_id=%s has_ai_instruction=%s",
            type(self.repository).__name__, self.owner_id, bool(payload.get("ai_instruction")),
        )
        payload["next_run_at"] = initial.astimezone(timezone.utc) if isinstance(initial, datetime) and initial.tzinfo else initial
        _creation_trace(
            "task_validation_result", success="true", schema_version="1",
            schedule_type=str(candidate["schedule_type"]),
            actions=len(candidate["actions"]),
            payload_bytes=len(json.dumps(payload, ensure_ascii=False, default=str)),
            next_run_at=(payload["next_run_at"].isoformat() if isinstance(payload["next_run_at"], datetime) else "none"),
        )
        _creation_trace(
            "repository_call", repo_type=type(self.repository).__name__,
            schedule_type=str(candidate["schedule_type"]),
            payload_bytes=len(json.dumps(payload, ensure_ascii=False, default=str)),
        )
        try:
            task = await self.repository.create_task(self.owner_id, payload)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            _creation_trace(
                "repository_error", repo_type=type(self.repository).__name__,
                error_type=type(exc).__name__, error=str(exc)[:200],
            )
            raise
        _creation_trace(
            "persisted", repo_type=type(self.repository).__name__,
            task_id=int(task.id), version=int(task.version),
            next_run_at=(task.next_run_at.isoformat() if task.next_run_at else "none"),
            elapsed_s=round(time.perf_counter() - started, 2),
        )
        return task
