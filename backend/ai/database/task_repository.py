from __future__ import annotations

import asyncio
import copy
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

logger = logging.getLogger(__name__)

TASK_STATUSES = frozenset({"active", "paused", "completed", "failed", "expired", "deleted"})
OCCURRENCE_STATUSES = frozenset({"claimed", "running", "succeeded", "failed", "retry_pending", "cancelled", "expired", "interrupted"})
SCHEDULE_TYPES = frozenset({"once", "interval", "daily", "weekly"})
MAX_ACTIONS = 5
MAX_PAYLOAD_BYTES = 32768
MAX_METADATA_BYTES = 8192
MAX_ATTEMPTS = 3
DB_TIMEOUT = 10.0

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
    if not isinstance(data.get("definition_version"), int) or data["definition_version"] < 1:
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


def _parse_dt(value: Any) -> datetime | None:
    if value is None or (not isinstance(value, datetime) and not str(value).strip()):
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _serialize(value: Any) -> Any:
    return value.isoformat() if isinstance(value, datetime) else value


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


def _task_from_row(row: dict[str, Any]) -> TaskRecord:
    value = {**row, "id": int(row["id"]), "owner_id": int(row["owner_id"]), "version": int(row.get("version", 1))}
    for key in ("created_at", "updated_at", "next_run_at", "terminal_at"):
        value[key] = _parse_dt(value.get(key))
    value["schedule"] = dict(value.get("schedule") or {})
    value["actions"] = list(value.get("actions") or [])
    value["notification_destination"] = dict(value.get("notification_destination") or {})
    _validate_task_input(value)
    return TaskRecord(**{key: value[key] for key in TaskRecord.__dataclass_fields__})


def _occurrence_from_row(row: dict[str, Any]) -> OccurrenceRecord:
    value = {**row, "id": int(row["id"]), "task_id": int(row["task_id"]), "owner_id": int(row["owner_id"]), "definition_version": int(row["definition_version"]), "attempt": int(row.get("attempt", 1))}
    for key in ("scheduled_for", "claimed_at", "started_at", "finished_at", "retry_at", "created_at", "updated_at"):
        value[key] = _parse_dt(value.get(key))
    value["status"] = value.get("status", "claimed")
    value["action_snapshot"] = list(value.get("action_snapshot") or [])
    value["error_metadata"] = dict(value.get("error_metadata") or {})
    value["result_metadata"] = dict(value.get("result_metadata") or {})
    _validate_occurrence_input(value)
    return OccurrenceRecord(**{key: value[key] for key in OccurrenceRecord.__dataclass_fields__})


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


class SupabaseTaskRepository(TaskRepository):
    """Async owner-scoped adapter over the existing synchronous Supabase client."""

    def __init__(self, client: Any, fallback: InMemoryTaskRepository | None = None, timeout: float = DB_TIMEOUT) -> None:
        self._client = client
        self._fallback = fallback or InMemoryTaskRepository()
        self._timeout = timeout

    async def _run(self, fn: Callable[[], Any]) -> Any:
        try:
            return await asyncio.wait_for(asyncio.to_thread(fn), timeout=self._timeout)
        except asyncio.CancelledError:
            raise

    def _task_payload(self, owner_id: int, data: dict[str, Any]) -> dict[str, Any]:
        payload = {**data, "owner_id": owner_id}
        _validate_task_input(payload)
        return {k: _serialize(v) for k, v in payload.items() if k not in {"id", "created_at", "updated_at", "terminal_at", "version"}}

    def _occurrence_payload(self, owner_id: int, data: dict[str, Any]) -> dict[str, Any]:
        payload = {**data, "owner_id": owner_id}
        _validate_occurrence_input(payload)
        return {k: _serialize(v) for k, v in payload.items() if k not in {"id", "created_at", "updated_at"}}

    async def create_task(self, owner_id: int, data: dict[str, Any]) -> TaskRecord:
        payload = self._task_payload(owner_id, data)
        try:
            result = await self._run(lambda: self._client.table("ai_tasks").insert(payload).execute())
            row = getattr(result, "data", None)
            if not row:
                raise RuntimeError("Supabase task insert returned no row")
            return _task_from_row(row[0] if isinstance(row, list) else row)
        except (ValueError, TypeError):
            raise
        except Exception as exc:
            logger.warning("Supabase task create failed; using fallback: %s", exc)
            return await self._fallback.create_task(owner_id, data)

    async def get_task(self, owner_id: int, task_id: int) -> TaskRecord | None:
        try:
            result = await self._run(lambda: self._client.table("ai_tasks").select("*").eq("id", task_id).eq("owner_id", owner_id).maybe_single().execute())
            row = getattr(result, "data", None)
            return _task_from_row(row[0] if isinstance(row, list) else row) if row else None
        except Exception as exc:
            logger.warning("Supabase task read failed; using fallback: %s", exc)
            return await self._fallback.get_task(owner_id, task_id)

    async def list_tasks(self, owner_id: int) -> list[TaskRecord]:
        try:
            result = await self._run(lambda: self._client.table("ai_tasks").select("*").eq("owner_id", owner_id).order("updated_at", desc=True).execute())
            return [_task_from_row(row) for row in (getattr(result, "data", None) or [])]
        except Exception as exc:
            logger.warning("Supabase task list failed; using fallback: %s", exc)
            return await self._fallback.list_tasks(owner_id)

    async def update_task(self, owner_id: int, task_id: int, expected_version: int, updates: dict[str, Any]) -> TaskRecord | None:
        current = await self.get_task(owner_id, task_id)
        if current is None or current.version != expected_version:
            return None
        if "status" in updates and updates["status"] not in _ALLOWED_TASK_TRANSITIONS[current.status]:
            raise ValueError("invalid task status transition")
        merged = current.as_dict(); merged.update(updates); merged["owner_id"] = owner_id; _validate_task_input(merged)
        outgoing = {k: _serialize(v) for k, v in updates.items() if k not in {"id", "created_at", "updated_at", "terminal_at", "version"}}
        outgoing["version"] = expected_version + 1
        if updates.get("status") in _TERMINAL_TASK_STATUSES:
            outgoing["terminal_at"] = _now().isoformat()
        try:
            result = await self._run(lambda: self._client.table("ai_tasks").update(outgoing).eq("id", task_id).eq("owner_id", owner_id).eq("version", expected_version).execute())
            row = getattr(result, "data", None)
            return _task_from_row(row[0] if isinstance(row, list) else row) if row else None
        except (ValueError, TypeError):
            raise
        except Exception as exc:
            logger.warning("Supabase task update failed; using fallback: %s", exc)
            return await self._fallback.update_task(owner_id, task_id, expected_version, updates)

    async def transition_task(self, owner_id: int, task_id: int, status: str, expected_version: int | None = None) -> TaskRecord | None:
        current = await self.get_task(owner_id, task_id)
        if not current:
            return None
        if status not in TASK_STATUSES or status not in _ALLOWED_TASK_TRANSITIONS[current.status]:
            raise ValueError("invalid task status transition")
        return await self.update_task(owner_id, task_id, expected_version if expected_version is not None else current.version, {"status": status})

    async def create_occurrence(self, owner_id: int, data: dict[str, Any]) -> OccurrenceRecord:
        task = await self.get_task(owner_id, int(data.get("task_id", 0)))
        if not task:
            raise ValueError("task not found for owner")
        payload = self._occurrence_payload(owner_id, data)
        try:
            existing = await self.get_occurrence(owner_id, payload["task_id"], payload["occurrence_key"])
            if existing:
                return existing
            result = await self._run(lambda: self._client.table("ai_task_occurrences").insert(payload).execute())
            row = getattr(result, "data", None)
            if not row:
                raise RuntimeError("Supabase occurrence insert returned no row")
            return _occurrence_from_row(row[0] if isinstance(row, list) else row)
        except (ValueError, TypeError):
            raise
        except Exception as exc:
            logger.warning("Supabase occurrence create failed; using fallback: %s", exc)
            return await self._fallback.create_occurrence(owner_id, data)

    async def get_occurrence(self, owner_id: int, task_id: int, occurrence_key: str) -> OccurrenceRecord | None:
        try:
            result = await self._run(lambda: self._client.table("ai_task_occurrences").select("*").eq("task_id", task_id).eq("occurrence_key", occurrence_key).eq("owner_id", owner_id).maybe_single().execute())
            row = getattr(result, "data", None)
            return _occurrence_from_row(row[0] if isinstance(row, list) else row) if row else None
        except Exception as exc:
            logger.warning("Supabase occurrence read failed; using fallback: %s", exc)
            return await self._fallback.get_occurrence(owner_id, task_id, occurrence_key)

    async def list_occurrences(self, owner_id: int, task_id: int | None = None) -> list[OccurrenceRecord]:
        try:
            query = self._client.table("ai_task_occurrences").select("*").eq("owner_id", owner_id)
            if task_id is not None:
                query = query.eq("task_id", task_id)
            result = await self._run(lambda: query.order("scheduled_for", desc=True).execute())
            return [_occurrence_from_row(row) for row in (getattr(result, "data", None) or [])]
        except Exception as exc:
            logger.warning("Supabase occurrence list failed; using fallback: %s", exc)
            return await self._fallback.list_occurrences(owner_id, task_id)

    async def claim_occurrence(self, owner_id: int, task_id: int, occurrence_key: str) -> OccurrenceRecord | None:
        current = await self.get_occurrence(owner_id, task_id, occurrence_key)
        if not current or current.status not in {"claimed", "retry_pending", "interrupted"}:
            return None
        now = _now().isoformat()
        try:
            result = await self._run(lambda: self._client.table("ai_task_occurrences").update({"status": "running", "claimed_at": current.claimed_at.isoformat() if current.claimed_at else now, "started_at": now, "updated_at": now}).eq("task_id", task_id).eq("occurrence_key", occurrence_key).eq("owner_id", owner_id).eq("status", current.status).execute())
            row = getattr(result, "data", None)
            return _occurrence_from_row(row[0] if isinstance(row, list) else row) if row else None
        except Exception as exc:
            logger.warning("Supabase occurrence claim failed; using fallback: %s", exc)
            return await self._fallback.claim_occurrence(owner_id, task_id, occurrence_key)

    async def transition_occurrence(self, owner_id: int, task_id: int, occurrence_key: str, status: str, **updates: Any) -> OccurrenceRecord | None:
        current = await self.get_occurrence(owner_id, task_id, occurrence_key)
        if not current:
            return None
        if status not in OCCURRENCE_STATUSES or status not in _ALLOWED_OCCURRENCE_TRANSITIONS[current.status]:
            raise ValueError("invalid occurrence status transition")
        immutable = {"action_snapshot", "occurrence_key", "task_id", "owner_id", "definition_version", "scheduled_for"}
        if immutable.intersection(updates):
            raise ValueError("immutable occurrence field")
        candidate = {**current.as_dict(), **updates, "status": status}; _validate_occurrence_input(candidate)
        outgoing = {k: _serialize(v) for k, v in updates.items()}; outgoing["status"] = status; outgoing["updated_at"] = _now().isoformat()
        if status in _TERMINAL_OCCURRENCE_STATUSES:
            outgoing.setdefault("finished_at", _now().isoformat())
        try:
            result = await self._run(lambda: self._client.table("ai_task_occurrences").update(outgoing).eq("task_id", task_id).eq("occurrence_key", occurrence_key).eq("owner_id", owner_id).eq("status", current.status).execute())
            row = getattr(result, "data", None)
            return _occurrence_from_row(row[0] if isinstance(row, list) else row) if row else None
        except (ValueError, TypeError):
            raise
        except Exception as exc:
            logger.warning("Supabase occurrence transition failed; using fallback: %s", exc)
            return await self._fallback.transition_occurrence(owner_id, task_id, occurrence_key, status, **updates)


_default_repository: TaskRepository | None = None


def get_task_repository() -> TaskRepository:
    global _default_repository
    if _default_repository is None:
        from backend.db.client import get_db
        client = get_db()
        _default_repository = SupabaseTaskRepository(client) if client is not None else InMemoryTaskRepository()
    return _default_repository
