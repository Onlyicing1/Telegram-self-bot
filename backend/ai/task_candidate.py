"""Untrusted structured task candidates; no persistence or execution authority."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from backend.ai.database.task_repository import MAX_ACTIONS, MAX_PAYLOAD_BYTES
from backend.ai.scheduling import ScheduleError, parse_schedule

MAX_LABEL_CHARS = 256
MAX_TIMEZONE_CHARS = 128


class TaskCandidateError(ValueError):
    """Candidate is malformed, incomplete, ambiguous, or unsafe."""


@dataclass(frozen=True)
class TaskCandidate:
    label: str
    schedule_type: str
    schedule: dict[str, Any]
    timezone: str
    actions: list[dict[str, Any]]
    notification_destination: dict[str, Any]

    @classmethod
    def from_untrusted(cls, value: Any) -> "TaskCandidate":
        if not isinstance(value, dict):
            raise TaskCandidateError("candidate must be an object")
        allowed = {"label", "schedule_type", "schedule", "timezone", "actions", "notification_destination"}
        if set(value) != allowed:
            raise TaskCandidateError("candidate fields are incomplete or unsupported")
        label = value["label"]
        timezone = value["timezone"]
        schedule = value["schedule"]
        actions = value["actions"]
        destination = value["notification_destination"]
        if not isinstance(label, str) or not label.strip() or len(label) > MAX_LABEL_CHARS:
            raise TaskCandidateError("label is invalid")
        if not isinstance(timezone, str) or not timezone.strip() or len(timezone) > MAX_TIMEZONE_CHARS:
            raise TaskCandidateError("timezone is invalid")
        if not isinstance(schedule, dict) or not isinstance(destination, dict):
            raise TaskCandidateError("schedule and notification destination must be objects")
        if not isinstance(actions, list) or not 1 <= len(actions) <= MAX_ACTIONS:
            raise TaskCandidateError("actions must contain 1 through 5 items")
        for action in actions:
            if not isinstance(action, dict) or not isinstance(action.get("name"), str) or not action["name"].strip():
                raise TaskCandidateError("each action requires a tool name")
            args = action.get("arguments", {})
            if not isinstance(args, dict):
                raise TaskCandidateError("action arguments must be objects")
        if len(str(value).encode()) > MAX_PAYLOAD_BYTES:
            raise TaskCandidateError("candidate exceeds bounded payload size")
        try:
            parsed = parse_schedule(value["schedule_type"], schedule)
        except (ScheduleError, TypeError, ValueError) as exc:
            raise TaskCandidateError(str(exc)) from exc
        if value["schedule_type"] != "interval" and schedule.get("timezone") != timezone:
            raise TaskCandidateError("schedule timezone must match task timezone")
        return cls(label.strip(), value["schedule_type"], dict(schedule), timezone.strip(), [dict(a) for a in actions], dict(destination))

    def as_creation_candidate(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "schedule_type": self.schedule_type,
            "schedule": dict(self.schedule),
            "timezone": self.timezone,
            "actions": [dict(action) for action in self.actions],
            "notification_destination": dict(self.notification_destination),
        }


def parse_candidate_output(value: Any) -> TaskCandidate:
    """Parse only structured data; prose or ambiguous output is rejected."""
    return TaskCandidate.from_untrusted(value)
