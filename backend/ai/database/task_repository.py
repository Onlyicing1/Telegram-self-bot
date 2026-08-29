"""Durable AI task and occurrence repository contracts.

This module stores scheduler state only. It never parses schedules or
executes actions; callers must provide already validated structured data.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

TASK_STATUSES = frozenset({"active", "paused", "completed", "failed", "expired", "deleted"})
OCCURRENCE_STATUSES = frozenset({"claimed", "running", "succeeded", "failed", "retry_pending", "cancelled", "expired", "interrupted"})
SCHEDULE_TYPES = frozenset({"once", "interval", "daily", "weekly"})
MAX_ACTIONS = 5
MAX_PAYLOAD_BYTES = 32768
MAX_METADATA_BYTES = 8192
MAX_ATTEMPTS = 3

_TERMINAL_TASK_STATUSES = frozenset({"completed", "failed", "expired", "deleted"})
_TERMINAL_OCCURRENCE_STATUSES = frozenset({"succeeded", "failed", "cancelled", "expired", "interrupted"})
_ALLOWED_TASK_TRANSITIONS = {
    "active": {"active", "paused", "completed", "failed", "expired", "deleted"},
    "paused": {"paused", "active", "deleted"},
    "completed": {"completed"},
    "failed": {"failed"},
    "expired": {"expired"},
    "deleted": {"deleted"},
}
_ALLOWED_OCCURRENCE_TRANSITIONS = {
    "claimed": {"claimed", "running", "cancelled", "expired", "interrupted"},
    "running": {"running", "succeeded", "failed", "retry_pending", "cancelled", "interrupted"},
    "retry_pending": {"retry_pending", "running", "failed", "cancelled", "interrupted"},
    "succeeded": {"succeeded"},
    "failed": {"failed"},
    "cancelled": {"cancelled"},
    "expired": {"expired"},
    "interrupted": {"interrupted", "retry_pending", "failed"},
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _json_bytes(value: Any) -> int:
    import json
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def _validate_json_payload(value: Any, *, array: bool = False, metadata: bool = False) -> None:
    if array and (not isinstance(value, list) or not 1 <= len(value) <= MAX_ACTIONS):
        raise ValueError("action payload must contain 1 through 5 actions")
    if metadata and not isinstance(value, dict):
        raise ValueError("metadata must be an object")
    limit = MAX_METADATA_BYTES if metadata else MAX_PAYLOAD_BYTES
    if _json_bytes(value) > limit:
        raise ValueError("JSON payload exceeds its bounded size")


def _validate_task_input(data: dict[str, Any]) -> None:
    if not isinstance(data.get("owner_id"), int):
        raise ValueError("owner_id must be an integer")
    if not isinstance(data.get("label"), str) or not data["label"].strip() or len(data["label"]) > 256:
        raise ValueError("label must be a nonblank bounded string")
    if data.get("status", "active") not in TASK_STATUSES:
        raise ValueError("invalid task status")
    if data.get("schedule_type") not in SCHEDULE_TYPES:
        raise ValueError("invalid schedule type")
    if not isinstance(data.get("timezone"), str) or not data["timezone"].strip() or len(data["timezone"]) > 128:
        raise ValueError("timezone must be a bounded nonblank string")
    if not isinstance(data.get("schedule"), dict):
        raise ValueError("schedule must be an object")
    if not isinstance(data.get("notification_destination"), dict):
        raise ValueError("notification destination must be an object")
    _validate_json_payload(data.get("actions"), array=True)
    if _json_bytes(data["schedule"]) > 16384 or _json_bytes(data["notification_destination"]) > 4096:
        raise ValueError("task JSON payload exceeds its bounded size")


def _validate_occurrence_input(data: dict[str, Any]) -> None:
    if not isinstance(data.get("owner_id"), int):
        raise ValueError("owner_id must be an integer")
    if not isinstance(data.get("occurrence_key"), str) or not data["occurrence_key"].strip() or len(data["occurrence_key"]) > 256:
        raise ValueError("occurrence_key must be a bounded nonblank string")
    if data.get("definition_version", 0) < 1:
        raise ValueError("definition_version must be positive")
    if data.get("status", "claimed") not in OCCURRENCE_STATUSES:
        raise ValueError("invalid occurrence status")
    attempt = data.get("attempt", 1)
    if not isinstance(attempt, int) or not 1 <= attempt <= MAX_ATTEMPTS:
        raise ValueError("attempt must be between 1 and 3")
    _validate_json_payload(data.get("action_snapshot"), array=True)
    _validate_json_payload(data.get("error_metadata", {}), metadata=True)
    _validate_json_payload(data.get("result_metadata", {}), metadata=True)
    if data.get("status") == "retry_pending" and data.get("retry_at") is None:
        raise ValueError("retry_pending requires retry_at")


def _copy(value: Any) -> Any:
    return copy.deepcopy(value)


@dataclass
class TaskRecord:
    id: int
    owner_id: int
    label: str
    schedule_type: str
    schedule: dict[str, Any]
    timezone: str
    actions: list[dict[str, Any]]
    notification_destination: dict[str, Any]
    status: str = "active"
    version: int = 1
    next_run_at: datetime | None = None
    created_at: datetime = field(default_factory=_now)
    updated_at: datetime = field(default_factory=_now)
    terminal_at: datetime | None = None

    def as_dict(self) -> dict[str, Any]:
        return _copy(self.__dict__)


@dataclass
class OccurrenceRecord:
    id: int
    task_id: int
    owner_id: int
    occurrence_key: str
    definition_version: int
    action_snapshot: list[dict[str, Any]]
    scheduled_for: datetime
    attempt: int = 1
    status: str = "claimed"
    claimed_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    retry_at: datetime | None = None
    error_metadata: dict[str, Any] = field(default_factory=dict)
    result_metadata: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=_now)
    updated_at: datetime = field(default_factory=_now)

    def as_dict(self) -> dict[str, Any]:
        return _copy(self.__dict__)


class TaskRepository:
    async def create_task(self, owner_id: int, data: dict[str, Any]) -> TaskRecord: raise NotImplementedError
    async def get_task(self, owner_id: int, task_id: int) -> TaskRecord | None: raise NotImplementedError
    async def list_tasks(self, owner_id: int) -> list[TaskRecord]: raise NotImplementedError
    async def update_task(self, owner_id: int, task_id: int, expected_version: int, updates: dict[str, Any]) -> TaskRecord | None: raise NotImplementedError
    async def transition_task(self, owner_id: int, task_id: int, status: str, expected_version: int | None = None) -> TaskRecord | None: raise NotImplementedError
    async def create_occurrence(self, owner_id: int, data: dict[str, Any]) -> OccurrenceRecord: raise NotImplementedError
    async def get_occurrence(self, owner_id: int, task_id: int, occurrence_key: str) -> OccurrenceRecord | None: raise NotImplementedError
    async def list_occurrences(self, owner_id: int, task_id: int | None = None) -> list[OccurrenceRecord]: raise NotImplementedError
    async def claim_occurrence(self, owner_id: int, task_id: int, occurrence_key: str) -> OccurrenceRecord | None: raise NotImplementedError
    async def transition_occurrence(self, owner_id: int, task_id: int, occurrence_key: str, status: str, **updates: Any) -> OccurrenceRecord | None: raise NotImplementedError


class SupabaseTaskRepository(TaskRepository):
    """Supabase-backed repository placeholder for the later database wiring phase."""

    def __init__(self, client: Any) -> None:
        self._client = client

    async def _unsupported(self) -> None:
        raise NotImplementedError("Supabase task repository wiring is not available yet")

    async def create_task(self, owner_id: int, data: dict[str, Any]) -> TaskRecord:
        await self._unsupported()

    async def get_task(self, owner_id: int, task_id: int) -> TaskRecord | None:
        await self._unsupported()

    async def list_tasks(self, owner_id: int) -> list[TaskRecord]:
        await self._unsupported()

    async def update_task(self, owner_id: int, task_id: int, expected_version: int, updates: dict[str, Any]) -> TaskRecord | None:
        await self._unsupported()

    async def transition_task(self, owner_id: int, task_id: int, status: str, expected_version: int | None = None) -> TaskRecord | None:
        await self._unsupported()

    async def create_occurrence(self, owner_id: int, data: dict[str, Any]) -> OccurrenceRecord:
        await self._unsupported()

    async def get_occurrence(self, owner_id: int, task_id: int, occurrence_key: str) -> OccurrenceRecord | None:
        await self._unsupported()

    async def list_occurrences(self, owner_id: int, task_id: int | None = None) -> list[OccurrenceRecord]:
        await self._unsupported()

    async def claim_occurrence(self, owner_id: int, task_id: int, occurrence_key: str) -> OccurrenceRecord | None:
        await self._unsupported()

    async def transition_occurrence(self, owner_id: int, task_id: int, occurrence_key: str, status: str, **updates: Any) -> OccurrenceRecord | None:
        await self._unsupported()


class InMemoryTaskRepository(TaskRepository):
    """Owner-scoped fallback with the same uniqueness and CAS semantics."""

    def __init__(self) -> None:
        self._tasks: dict[int, TaskRecord] = {}
        self._occurrences: dict[tuple[int, str], OccurrenceRecord] = {}
        self._next_task_id = 1
        self._next_occurrence_id = 1

    async def create_task(self, owner_id: int, data: dict[str, Any]) -> TaskRecord:
        payload = {**data, "owner_id": owner_id}
        _validate_task_input(payload)
        record = TaskRecord(id=self._next_task_id, owner_id=owner_id, label=payload["label"].strip(), schedule_type=payload["schedule_type"], schedule=_copy(payload["schedule"]), timezone=payload["timezone"].strip(), actions=_copy(payload["actions"]), notification_destination=_copy(payload["notification_destination"]), status=payload.get("status", "active"), next_run_at=payload.get("next_run_at"))
        self._tasks[record.id] = record
        self._next_task_id += 1
        return _copy(record)

    async def get_task(self, owner_id: int, task_id: int) -> TaskRecord | None:
        record = self._tasks.get(task_id)
        return _copy(record) if record and record.owner_id == owner_id else None

    async def list_tasks(self, owner_id: int) -> list[TaskRecord]:
        return [_copy(r) for r in self._tasks.values() if r.owner_id == owner_id]

    async def update_task(self, owner_id: int, task_id: int, expected_version: int, updates: dict[str, Any]) -> TaskRecord | None:
        record = self._tasks.get(task_id)
        if not record or record.owner_id != owner_id or record.version != expected_version:
            return None
        merged = record.as_dict()
        merged.update(updates)
        merged["owner_id"] = owner_id
        _validate_task_input(merged)
        if updates.get("status") and updates["status"] not in _ALLOWED_TASK_TRANSITIONS[record.status]:
            raise ValueError("invalid task status transition")
        for key in ("id", "created_at", "terminal_at"):
            merged[key] = getattr(record, key)
        record.label = merged["label"].strip()
        record.schedule_type = merged["schedule_type"]
        record.schedule = _copy(merged["schedule"])
        record.timezone = merged["timezone"].strip()
        record.actions = _copy(merged["actions"])
        record.notification_destination = _copy(merged["notification_destination"])
        record.status = merged.get("status", record.status)
        record.next_run_at = merged.get("next_run_at")
        record.version += 1
        record.updated_at = _now()
        if record.status in _TERMINAL_TASK_STATUSES and record.terminal_at is None:
            record.terminal_at = record.updated_at
        return _copy(record)

    async def transition_task(self, owner_id: int, task_id: int, status: str, expected_version: int | None = None) -> TaskRecord | None:
        record = await self.get_task(owner_id, task_id)
        if not record:
            return None
        if status not in TASK_STATUSES or status not in _ALLOWED_TASK_TRANSITIONS[record.status]:
            raise ValueError("invalid task status transition")
        return await self.update_task(owner_id, task_id, expected_version if expected_version is not None else record.version, {"status": status})

    async def create_occurrence(self, owner_id: int, data: dict[str, Any]) -> OccurrenceRecord:
        payload = {**data, "owner_id": owner_id}
        task = self._tasks.get(payload.get("task_id"))
        if not task or task.owner_id != owner_id:
            raise ValueError("task not found for owner")
        _validate_occurrence_input(payload)
        key = (payload["task_id"], payload["occurrence_key"])
        if key in self._occurrences:
            return _copy(self._occurrences[key])
        record = OccurrenceRecord(id=self._next_occurrence_id, task_id=payload["task_id"], owner_id=owner_id, occurrence_key=payload["occurrence_key"].strip(), definition_version=payload["definition_version"], action_snapshot=_copy(payload["action_snapshot"]), scheduled_for=payload["scheduled_for"], attempt=payload.get("attempt", 1), status=payload.get("status", "claimed"), claimed_at=payload.get("claimed_at"), started_at=payload.get("started_at"), retry_at=payload.get("retry_at"), error_metadata=_copy(payload.get("error_metadata", {})), result_metadata=_copy(payload.get("result_metadata", {})))
        self._occurrences[key] = record
        self._next_occurrence_id += 1
        return _copy(record)

    async def get_occurrence(self, owner_id: int, task_id: int, occurrence_key: str) -> OccurrenceRecord | None:
        record = self._occurrences.get((task_id, occurrence_key))
        return _copy(record) if record and record.owner_id == owner_id else None

    async def list_occurrences(self, owner_id: int, task_id: int | None = None) -> list[OccurrenceRecord]:
        return [_copy(r) for r in self._occurrences.values() if r.owner_id == owner_id and (task_id is None or r.task_id == task_id)]

    async def claim_occurrence(self, owner_id: int, task_id: int, occurrence_key: str) -> OccurrenceRecord | None:
        record = self._occurrences.get((task_id, occurrence_key))
        if not record or record.owner_id != owner_id:
            return None
        if record.status not in {"claimed", "retry_pending", "interrupted"}:
            return None
        record.status = "running"
        record.claimed_at = record.claimed_at or _now()
        record.started_at = _now()
        record.updated_at = _now()
        return _copy(record)

    async def transition_occurrence(self, owner_id: int, task_id: int, occurrence_key: str, status: str, **updates: Any) -> OccurrenceRecord | None:
        record = self._occurrences.get((task_id, occurrence_key))
        if not record or record.owner_id != owner_id:
            return None
        if status not in OCCURRENCE_STATUSES or status not in _ALLOWED_OCCURRENCE_TRANSITIONS[record.status]:
            raise ValueError("invalid occurrence status transition")
        candidate = {**record.as_dict(), **updates, "status": status}
        if status == "retry_pending" and candidate.get("retry_at") is None:
            raise ValueError("retry_pending requires retry_at")
        if "attempt" in candidate and not 1 <= candidate["attempt"] <= MAX_ATTEMPTS:
            raise ValueError("attempt limit exceeded")
        _validate_occurrence_input(candidate)
        for key, value in updates.items():
            if key in {"action_snapshot", "occurrence_key", "task_id", "owner_id", "definition_version", "scheduled_for"}:
                raise ValueError("immutable occurrence field")
            setattr(record, key, _copy(value))
        record.status = status
        record.updated_at = _now()
        if status in _TERMINAL_OCCURRENCE_STATUSES:
            record.finished_at = record.finished_at or record.updated_at
        return _copy(record)


_default_repository: InMemoryTaskRepository | None = None


def get_task_repository() -> TaskRepository:
    global _default_repository
    if _default_repository is None:
        _default_repository = InMemoryTaskRepository()
    return _default_repository
