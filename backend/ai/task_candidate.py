"""Untrusted structured task candidates; no persistence or execution authority."""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any

from backend.ai.database.task_repository import MAX_ACTIONS, MAX_PAYLOAD_BYTES
from backend.ai.scheduling import ScheduleError, parse_schedule

MAX_LABEL_CHARS = 256
MAX_TIMEZONE_CHARS = 128

# Unit-keyed interval aliases -> seconds. `parse_schedule` requires the
# canonical {"seconds": N} form; models emit human-natural unit forms (e.g.
# {"minutes": 3}, {"interval": 3, "unit": "minutes"}). Every accepted shape
# below is a single unambiguous, bounded, deterministic representation of
# "every N <unit>"; anything ambiguous or unknown falls through to
# parse_schedule, which rejects it with the schedule structure attached.
_INTERVAL_UNIT_SECONDS = {
    "minute": 60, "minutes": 60, "min": 60, "mins": 60,
    "hour": 3600, "hours": 3600, "hr": 3600, "hrs": 3600,
    "day": 86400, "days": 86400,
    "week": 604800, "weeks": 604800,
}
# Unit STRING values accepted in (value, unit) pair shapes, incl. the Persian
# unit words a Persian-request model may echo back.
_UNIT_WORD_SECONDS = {
    **_INTERVAL_UNIT_SECONDS,
    "second": 1, "seconds": 1, "sec": 1, "secs": 1,
    "دقیقه": 60, "ساعت": 3600, "روز": 86400, "هفته": 604800,
}
_VALUE_KEYS = frozenset({"interval", "value", "every", "amount", "count", "number", "n", "repeat"})
_UNIT_VALUE_KEYS = frozenset({"unit", "units", "time_unit", "unit_name", "granularity"})
_COMPOUND_KEY_RE = re.compile(
    r"^(?:interval|every|each|repeat)_(minutes?|mins?|hours?|hrs?|days?|weeks?|seconds?|secs?)$"
)
# Persian/Arabic-Indic digits a model may emit inside numeric strings.
_DIGIT_TRANSLATION = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")


def _as_number(value: Any) -> float | None:
    """Finite float from int/float or a plain numeric string (Persian digits
    included); None for bools, non-numeric strings, and everything else."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip().translate(_DIGIT_TRANSLATION))
        except ValueError:
            return None
    return None


def _schedule_structure(schedule: Any) -> str:
    """Bounded, safe structural fingerprint for rejection diagnostics.

    Keys (sorted, truncated), value types, and at most one numeric preview —
    never message content, destinations, or the raw provider response.
    """
    if not isinstance(schedule, dict):
        return f"type={type(schedule).__name__}"
    keys = sorted(str(k)[:24] for k in schedule)[:6]
    types = ",".join(type(schedule[k]).__name__ for k in sorted(schedule, key=str)[:6])
    seconds = schedule.get("seconds")
    seconds_desc = type(seconds).__name__
    number = _as_number(seconds)
    if number is not None and math.isfinite(number):
        seconds_desc += f"={number:g}"
    return (
        f"keys={','.join(keys) or '-'}|types={types or '-'}"
        f"|has_seconds={str('seconds' in schedule).lower()}|seconds={seconds_desc}"
        f"|unit_key={str(any(k in _INTERVAL_UNIT_SECONDS for k in schedule)).lower()}"
        f"|nested={str(any(isinstance(v, dict) for v in schedule.values())).lower()}"
    )


def _convert_value_unit_schedule(schedule: dict[str, Any]) -> dict[str, Any] | None:
    """{"interval": 3, "unit": "minutes"} -> {"seconds": 180}.

    Accepts exactly one numeric value key and one string unit key from the
    bounded vocabularies; a stray timezone key is dropped (interval schedules
    carry no timezone). Any other extra key, unknown unit word, or invalid
    number leaves the shape unmatched (the schedule is then rejected
    downstream with its structure attached).
    """
    value_keys = [k for k in schedule if k in _VALUE_KEYS and _as_number(schedule[k]) is not None]
    unit_keys = [k for k in schedule if k in _UNIT_VALUE_KEYS and isinstance(schedule[k], str)]
    if len(value_keys) != 1 or len(unit_keys) != 1:
        return None
    unit = schedule[unit_keys[0]].strip().lower()
    if unit not in _UNIT_WORD_SECONDS:
        return None
    extras = set(schedule) - {value_keys[0], unit_keys[0]}
    if extras - {"timezone"}:
        return None
    number = _as_number(schedule[value_keys[0]])
    if not math.isfinite(number) or number <= 0:
        return None
    return {"seconds": number * _UNIT_WORD_SECONDS[unit]}


def _convert_compound_key_schedule(schedule: dict[str, Any]) -> dict[str, Any] | None:
    """{"interval_minutes": 3} -> {"seconds": 180}."""
    for key in schedule:
        match = _COMPOUND_KEY_RE.match(str(key).strip().lower())
        if not match:
            continue
        if set(schedule) - {key} - {"timezone"}:
            return None
        number = _as_number(schedule[key])
        if number is None or not math.isfinite(number) or number <= 0:
            return None
        unit = match.group(1)
        return {"seconds": number * _INTERVAL_UNIT_SECONDS[unit]}
    return None


def _convert_flat_unit_schedule(schedule: dict[str, Any]) -> dict[str, Any] | None:
    """{"minutes": 3} -> {"seconds": 180}; exactly one unit key allowed."""
    units = [key for key in schedule if key in _INTERVAL_UNIT_SECONDS]
    if len(units) != 1:
        return None
    extras = set(schedule) - set(units)
    if extras - {"timezone"}:
        return None
    number = _as_number(schedule[units[0]])
    if number is None or not math.isfinite(number) or number <= 0:
        return None
    return {"seconds": number * _INTERVAL_UNIT_SECONDS[units[0]]}


def _canonicalize_interval_schedule(schedule: Any) -> Any:
    """Return the canonical {"seconds": N>0} form for a recognized shape;
    otherwise return the schedule unchanged so parse_schedule rejects it and
    the rejection carries the schedule structure."""
    if not isinstance(schedule, dict):
        return schedule
    if "seconds" in schedule:
        number = _as_number(schedule["seconds"])
        if number is not None and math.isfinite(number) and number > 0:
            return {"seconds": number}
        return schedule
    for converter in (
        _convert_value_unit_schedule,
        _convert_compound_key_schedule,
        _convert_flat_unit_schedule,
    ):
        converted = converter(schedule)
        if converted is not None:
            return converted
    return schedule

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
        # Tolerated provider shape: a singular `action` alias for `actions`.
        # It is normalized BEFORE the exact field-set check so downstream
        # validation is unchanged; every other deviation stays rejected.
        if "actions" not in value and "action" in value:
            value = dict(value)
            value["actions"] = value.pop("action")
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
            # Tolerate the field-name aliases a model may emit (`tool` for
            # `name`, `parameters`/`args` for `arguments` — the same aliases
            # the execution layer already accepts) by normalizing the shape
            # first; genuinely malformed actions still fail below unchanged.
            if isinstance(action, dict) and "name" not in action and isinstance(action.get("tool"), str):
                action = {**action, "name": action["tool"]}
            if isinstance(action, dict) and "arguments" not in action:
                for arg_alias in ("parameters", "args"):
                    if isinstance(action.get(arg_alias), dict):
                        action = {**action, "arguments": action[arg_alias]}
                        break
            if not isinstance(action, dict) or not isinstance(action.get("name"), str) or not action["name"].strip():
                raise TaskCandidateError("each action requires a tool name")
            args = action.get("arguments", {})
            if not isinstance(args, dict):
                raise TaskCandidateError("action arguments must be objects")
            canonical.append(_canonicalize_action(action))
        if len(str(value).encode()) > MAX_PAYLOAD_BYTES:
            raise TaskCandidateError("candidate exceeds bounded payload size")
        if value["schedule_type"] == "interval":
            schedule = _canonicalize_interval_schedule(schedule)
        if value["schedule_type"] == "event":
            # Event triggers have no wall-clock time: the schedule must be a
            # bounded, model-facing trigger spec (names only — ids are
            # resolved later from trusted runtime context).
            from backend.ai.task_trigger import TaskTriggerError, validate_trigger_spec
            trigger = schedule.get("trigger")
            try:
                schedule = {"trigger": validate_trigger_spec(trigger)}
            except TaskTriggerError as exc:
                raise TaskCandidateError(f"invalid event trigger: {exc}") from exc
        try:
            parsed = parse_schedule(value["schedule_type"], schedule)
        except (ScheduleError, TypeError, ValueError) as exc:
            # The schedule structure rides on the rejection so one production
            # occurrence identifies the exact provider shape (keys/types
            # only — never message content or the raw response).
            raise TaskCandidateError(
                f"{exc} [{_schedule_structure(schedule)}]"
            ) from exc
        if (
            value["schedule_type"] not in ("interval", "event")
            and schedule.get("timezone") != timezone
        ):
            raise TaskCandidateError("schedule timezone must match task timezone")
        # Validate optional chat_name in notification_destination.
        if "chat_name" in destination:
            cn = destination["chat_name"]
            if not isinstance(cn, str) or not cn.strip() or len(cn) > 256:
                raise TaskCandidateError("chat_name must be a nonblank bounded string")
        # Delivery/notification flags are explicit task-definition opt-ins;
        # they must be booleans so the scheduler can never misread them.
        for flag_key in ("deliver_result", "notify_on_outcome"):
            if flag_key in destination and not isinstance(destination[flag_key], bool):
                raise TaskCandidateError(f"{flag_key} must be a boolean")
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
