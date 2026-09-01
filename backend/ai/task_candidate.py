"""Untrusted structured task candidates; no persistence or execution authority."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from backend.ai.database.task_repository import MAX_ACTIONS, MAX_PAYLOAD_BYTES
from backend.ai.scheduling import ScheduleError, parse_schedule

MAX_LABEL_CHARS = 256
MAX_TIMEZONE_CHARS = 128

# ── Canonical task-execution actions ──
#
# A candidate's actions are executed later by the registered ToolExecutor, so
# every persisted action name must resolve to a registered tool. Message
# writing is the canonical scheduled action: the model may emit any alias
# below, but each is normalized to the single registered ``send_message``
# tool carrying ONLY a bounded ``text`` argument. Destination/owner identity
# are never accepted from the model — the execution tool resolves the
# owner's own chat from trusted runtime context. Anything else (unknown
# action names, destination fields) remains the existing execution-time
# registry check's responsibility and fails safely there.
MAX_SEND_TEXT_CHARS = 4096
_SEND_ACTION_ALIASES = frozenset({"send", "send_message", "write_message", "send_text"})
_SEND_TEXT_ALIASES = frozenset({"text", "content", "message", "body"})


def _canonicalize_action(action: dict[str, Any]) -> dict[str, Any]:
    """Normalize one untrusted action to the registered execution contract."""
    name = action["name"].strip()
    args = dict(action.get("arguments") or {})
    if name not in _SEND_ACTION_ALIASES:
        return {"name": name, "arguments": args}
    text = next((args[k] for k in _SEND_TEXT_ALIASES if k in args), "")
    if not isinstance(text, str) or not text.strip() or len(text) > MAX_SEND_TEXT_CHARS:
        raise TaskCandidateError("message action requires bounded nonblank text content")
    # Only the bounded text travels forward — a model can never smuggle a
    # destination, recipient, chat id, or raw Telegram method into the action.
    return {"name": "send_message", "arguments": {"text": text.strip()}}


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
        canonical: list[dict[str, Any]] = []
        for action in actions:
            if not isinstance(action, dict) or not isinstance(action.get("name"), str) or not action["name"].strip():
                raise TaskCandidateError("each action requires a tool name")
            args = action.get("arguments", {})
            if not isinstance(args, dict):
                raise TaskCandidateError("action arguments must be objects")
            canonical.append(_canonicalize_action(action))
        if len(str(value).encode()) > MAX_PAYLOAD_BYTES:
            raise TaskCandidateError("candidate exceeds bounded payload size")
        try:
            parsed = parse_schedule(value["schedule_type"], schedule)
        except (ScheduleError, TypeError, ValueError) as exc:
            raise TaskCandidateError(str(exc)) from exc
        if value["schedule_type"] != "interval" and schedule.get("timezone") != timezone:
            raise TaskCandidateError("schedule timezone must match task timezone")
        # Validate optional chat_name in notification_destination.
        if "chat_name" in destination:
            cn = destination["chat_name"]
            if not isinstance(cn, str) or not cn.strip() or len(cn) > 256:
                raise TaskCandidateError("chat_name must be a nonblank bounded string")
        return cls(label.strip(), value["schedule_type"], dict(schedule), timezone.strip(), canonical, dict(destination))

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
