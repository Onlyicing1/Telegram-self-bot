"""Deterministic task creation boundary for authorized callers."""
from __future__ import annotations

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
import logging

logger = logging.getLogger(__name__)


class TaskCreationError(ValueError):
    """Candidate task data is invalid or cannot be scheduled."""


class TaskCreationService:
    def __init__(self, repository: TaskRepository, owner_id: int) -> None:
        if not isinstance(owner_id, int) or owner_id <= 0:
            raise TaskCreationError("owner identity is required")
        self.repository = repository
        self.owner_id = owner_id

    async def create(self, candidate: dict[str, Any], reference: datetime) -> TaskRecord:
        if isinstance(candidate, TaskCandidate):
            candidate = candidate.as_creation_candidate()
        if not isinstance(candidate, dict):
            raise TaskCreationError("task candidate must be an object")
        if not isinstance(reference, datetime) or reference.tzinfo is None:
            raise TaskCreationError("reference datetime must be timezone-aware")
        required = {"label", "schedule_type", "schedule", "timezone", "actions", "notification_destination"}
        allowed = required | {"next_run_at", "ai_instruction"}
        if set(candidate) - allowed:
            raise TaskCreationError("unsupported task fields")
        if required - set(candidate):
            raise TaskCreationError("missing required task fields")
        if candidate.get("timezone") != candidate["schedule"].get("timezone") and candidate["schedule_type"] != "interval":
            raise TaskCreationError("task and schedule timezones must match")
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
        return await self.repository.create_task(self.owner_id, payload)
