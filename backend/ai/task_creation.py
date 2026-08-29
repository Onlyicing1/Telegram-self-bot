"""Deterministic task creation boundary for authorized callers."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from backend.ai.database.task_repository import TaskRecord, TaskRepository
from backend.ai.scheduling import ScheduleError, parse_schedule, next_occurrence


class TaskCreationError(ValueError):
    """Candidate task data is invalid or cannot be scheduled."""


class TaskCreationService:
    def __init__(self, repository: TaskRepository, owner_id: int) -> None:
        if not isinstance(owner_id, int) or owner_id <= 0:
            raise TaskCreationError("owner identity is required")
        self.repository = repository
        self.owner_id = owner_id

    async def create(self, candidate: dict[str, Any], reference: datetime) -> TaskRecord:
        if not isinstance(candidate, dict):
            raise TaskCreationError("task candidate must be an object")
        if not isinstance(reference, datetime) or reference.tzinfo is None:
            raise TaskCreationError("reference datetime must be timezone-aware")
        required = {"label", "schedule_type", "schedule", "timezone", "actions", "notification_destination"}
        if set(candidate) - required:
            raise TaskCreationError("unsupported task fields")
        if required - set(candidate):
            raise TaskCreationError("missing required task fields")
        if candidate.get("timezone") != candidate["schedule"].get("timezone") and candidate["schedule_type"] != "interval":
            raise TaskCreationError("task and schedule timezones must match")
        try:
            schedule = parse_schedule(candidate["schedule_type"], candidate["schedule"])
            initial = candidate.get("next_run_at")
            if initial is None:
                initial = next_occurrence(schedule, reference)
        except (ScheduleError, TypeError, ValueError) as exc:
            raise TaskCreationError(str(exc)) from exc
        payload = {key: candidate[key] for key in required}
        payload["next_run_at"] = initial.astimezone(timezone.utc) if isinstance(initial, datetime) and initial.tzinfo else initial
        return await self.repository.create_task(self.owner_id, payload)
