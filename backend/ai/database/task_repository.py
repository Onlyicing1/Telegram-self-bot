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
MAX_ACTIONS = 5; MAX_PAYLOAD_BYTES = 32768; MAX_METADATA_BYTES = 8192; MAX_ATTEMPTS = 3; DB_TIMEOUT = 10.0
_TERMINAL_TASK_STATUSES = frozenset({"completed", "failed", "expired", "deleted"})
_TERMINAL_OCCURRENCE_STATUSES = frozenset({"succeeded", "failed", "cancelled", "expired", "interrupted"})
_ALLOWED_TASK_TRANSITIONS = {"active": {"active", "paused", "completed", "failed", "expired", "deleted"}, "paused": {"paused", "active", "deleted"}, "completed": {"completed"}, "failed": {"failed"}, "expired": {"expired"}, "deleted": {"deleted"}}
_ALLOWED_OCCURRENCE_TRANSITIONS = {"claimed": {"claimed", "running", "cancelled", "expired", "interrupted"}, "running": {"running", "succeeded", "failed", "retry_pending", "cancelled", "interrupted"}, "retry_pending": {"retry_pending", "running", "failed", "cancelled", "interrupted"}, "succeeded": {"succeeded"}, "failed": {"failed"}, "cancelled": {"cancelled"}, "expired": {"expired"}, "interrupted": {"interrupted", "retry_pending", "failed"}}

def _now(): return datetime.now(timezone.utc)
def _copy(value): return copy.deepcopy(value)
def _json_bytes(value): return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode())
def _validate_json_payload(value, *, array=False, metadata=False):
    if array and (not isinstance(value, list) or not 1 <= len(value) <= MAX_ACTIONS): raise ValueError("action payload must contain 1 through 5 actions")
    if metadata and not isinstance(value, dict): raise ValueError("metadata must be an object")
    if _json_bytes(value) > (MAX_METADATA_BYTES if metadata else MAX_PAYLOAD_BYTES): raise ValueError("JSON payload exceeds its bounded size")
def _validate_task_input(data):
    if not isinstance(data.get("owner_id"), int): raise ValueError("owner_id must be an integer")
    if not isinstance(data.get("label"), str) or not data["label"].strip() or len(data["label"]) > 256: raise ValueError("label must be a nonblank bounded string")
    if data.get("status", "active") not in TASK_STATUSES: raise ValueError("invalid task status")
    if data.get("schedule_type") not in SCHEDULE_TYPES: raise ValueError("invalid schedule type")
    if not isinstance(data.get("timezone"), str) or not data["timezone"].strip() or len(data["timezone"]) > 128: raise ValueError("timezone must be a bounded nonblank string")
    if not isinstance(data.get("schedule"), dict): raise ValueError("schedule must be an object")
    if not isinstance(data.get("notification_destination"), dict): raise ValueError("notification destination must be an object")
    _validate_json_payload(data.get("actions"), array=True)
    if _json_bytes(data["schedule"]) > 16384 or _json_bytes(data["notification_destination"]) > 4096: raise ValueError("task JSON payload exceeds its bounded size")
def _validate_occurrence_input(data):
    if not isinstance(data.get("owner_id"), int): raise ValueError("owner_id must be an integer")
    if not isinstance(data.get("occurrence_key"), str) or not data["occurrence_key"].strip() or len(data["occurrence_key"]) > 256: raise ValueError("occurrence_key must be a bounded nonblank string")
    if not isinstance(data.get("definition_version"), int) or data["definition_version"] < 1: raise ValueError("definition_version must be positive")
    if data.get("status", "claimed") not in OCCURRENCE_STATUSES: raise ValueError("invalid occurrence status")
    if not isinstance(data.get("attempt", 1), int) or not 1 <= data.get("attempt", 1) <= MAX_ATTEMPTS: raise ValueError("attempt must be between 1 and 3")
    _validate_json_payload(data.get("action_snapshot"), array=True); _validate_json_payload(data.get("error_metadata", {}), metadata=True); _validate_json_payload(data.get("result_metadata", {}), metadata=True)
    if data.get("status") == "retry_pending" and data.get("retry_at") is None: raise ValueError("retry_pending requires retry_at")
def _parse_dt(value):
    if value is None or (not isinstance(value, datetime) and not str(value).strip()): return None
    if isinstance(value, datetime): parsed = value
    else:
        try: parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError): return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
def _serialize(value): return value.isoformat() if isinstance(value, datetime) else value

@dataclass
class TaskRecord:
    id: int; owner_id: int; label: str; schedule_type: str; schedule: dict[str, Any]; timezone: str; actions: list[dict[str, Any]]; notification_destination: dict[str, Any]; status: str = "active"; version: int = 1; next_run_at: datetime | None = None; created_at: datetime = field(default_factory=_now); updated_at: datetime = field(default_factory=_now); terminal_at: datetime | None = None
    def as_dict(self): return _copy(self.__dict__)
@dataclass
class OccurrenceRecord:
    id: int; task_id: int; owner_id: int; occurrence_key: str; definition_version: int; action_snapshot: list[dict[str, Any]]; scheduled_for: datetime; attempt: int = 1; status: str = "claimed"; claimed_at: datetime | None = None; started_at: datetime | None = None; finished_at: datetime | None = None; retry_at: datetime | None = None; error_metadata: dict[str, Any] = field(default_factory=dict); result_metadata: dict[str, Any] = field(default_factory=dict); created_at: datetime = field(default_factory=_now); updated_at: datetime = field(default_factory=_now)
    def as_dict(self): return _copy(self.__dict__)
def _task_from_row(row):
    value = {**row, "id": int(row["id"]), "owner_id": int(row["owner_id"]), "version": int(row.get("version", 1))}
    for key in ("created_at", "updated_at", "next_run_at", "terminal_at"): value[key] = _parse_dt(value.get(key))
    value["schedule"] = dict(value.get("schedule") or {}); value["actions"] = list(value.get("actions") or []); value["notification_destination"] = dict(value.get("notification_destination") or {}); _validate_task_input(value)
    return TaskRecord(**{key: value[key] for key in TaskRecord.__dataclass_fields__})
def _occurrence_from_row(row):
    value = {**row, "id": int(row["id"]), "task_id": int(row["task_id"]), "owner_id": int(row["owner_id"]), "definition_version": int(row["definition_version"]), "attempt": int(row.get("attempt", 1)), "status": row.get("status", "claimed")}
    for key in ("scheduled_for", "claimed_at", "started_at", "finished_at", "retry_at", "created_at", "updated_at"): value[key] = _parse_dt(value.get(key))
    value["action_snapshot"] = list(value.get("action_snapshot") or []); value["error_metadata"] = dict(value.get("error_metadata") or {}); value["result_metadata"] = dict(value.get("result_metadata") or {}); _validate_occurrence_input(value)
    return OccurrenceRecord(**{key: value[key] for key in OccurrenceRecord.__dataclass_fields__})

class TaskRepository:
    async def create_task(self, owner_id, data): raise NotImplementedError
    async def get_task(self, owner_id, task_id): raise NotImplementedError
    async def list_tasks(self, owner_id): raise NotImplementedError
    async def list_due_tasks(self, owner_id, now, limit=10): raise NotImplementedError
    async def update_task(self, owner_id, task_id, expected_version, updates): raise NotImplementedError
    async def advance_next_run(self, owner_id, task_id, expected_version, next_run_at): raise NotImplementedError
    async def transition_task(self, owner_id, task_id, status, expected_version=None): raise NotImplementedError
    async def create_occurrence(self, owner_id, data): raise NotImplementedError
    async def get_occurrence(self, owner_id, task_id, occurrence_key): raise NotImplementedError
    async def list_occurrences(self, owner_id, task_id=None, limit=100): raise NotImplementedError
    async def list_recoverable_occurrences(self, owner_id, limit=100): raise NotImplementedError
    async def claim_occurrence(self, owner_id, task_id, occurrence_key): raise NotImplementedError
    async def transition_occurrence(self, owner_id, task_id, occurrence_key, status, **updates): raise NotImplementedError

class InMemoryTaskRepository(TaskRepository):
    def __init__(self): self._tasks={}; self._occurrences={}; self._next_task_id=1; self._next_occurrence_id=1
    async def create_task(self, owner_id, data):
        payload={**data,"owner_id":owner_id}; _validate_task_input(payload); r=TaskRecord(self._next_task_id,owner_id,payload["label"].strip(),payload["schedule_type"],_copy(payload["schedule"]),payload["timezone"].strip(),_copy(payload["actions"]),_copy(payload["notification_destination"]),payload.get("status","active"),1,payload.get("next_run_at")); self._tasks[r.id]=r; self._next_task_id+=1; return _copy(r)
    async def get_task(self, owner_id, task_id):
        r=self._tasks.get(task_id); return _copy(r) if r and r.owner_id==owner_id else None
    async def list_tasks(self, owner_id): return [_copy(r) for r in self._tasks.values() if r.owner_id==owner_id]
    async def list_due_tasks(self, owner_id, now, limit=10):
        ref=_parse_dt(now); return sorted([_copy(r) for r in self._tasks.values() if r.owner_id==owner_id and r.status=="active" and r.next_run_at is not None and r.next_run_at<=ref], key=lambda r:(r.next_run_at,r.id))[:max(0,limit)]
    async def update_task(self, owner_id, task_id, expected_version, updates):
        r=self._tasks.get(task_id)
        if not r or r.owner_id!=owner_id or r.version!=expected_version:return None
        if updates.get("status") and updates["status"] not in _ALLOWED_TASK_TRANSITIONS[r.status]:raise ValueError("invalid task status transition")
        merged=r.as_dict();merged.update(updates);merged["owner_id"]=owner_id;_validate_task_input(merged)
        for k in ("label","schedule_type","schedule","timezone","actions","notification_destination","status","next_run_at"):setattr(r,k,_copy(merged.get(k,getattr(r,k))))
        r.version+=1;r.updated_at=_now()
        if r.status in _TERMINAL_TASK_STATUSES and r.terminal_at is None:r.terminal_at=r.updated_at
        return _copy(r)
    async def advance_next_run(self, owner_id, task_id, expected_version, next_run_at): return await self.update_task(owner_id,task_id,expected_version,{"next_run_at":next_run_at})
    async def transition_task(self, owner_id, task_id, status, expected_version=None):
        r=await self.get_task(owner_id,task_id)
        if not r:return None
        if status not in TASK_STATUSES or status not in _ALLOWED_TASK_TRANSITIONS[r.status]:raise ValueError("invalid task status transition")
        return await self.update_task(owner_id,task_id,expected_version if expected_version is not None else r.version,{"status":status})
    async def create_occurrence(self, owner_id, data):
        payload={**data,"owner_id":owner_id};task=self._tasks.get(payload.get("task_id"))
        if not task or task.owner_id!=owner_id:raise ValueError("task not found for owner")
        _validate_occurrence_input(payload);key=(payload["task_id"],payload["occurrence_key"])
        if key in self._occurrences:return _copy(self._occurrences[key])
        r=OccurrenceRecord(self._next_occurrence_id,payload["task_id"],owner_id,payload["occurrence_key"].strip(),payload["definition_version"],_copy(payload["action_snapshot"]),payload["scheduled_for"],payload.get("attempt",1),payload.get("status","claimed"),payload.get("claimed_at"),payload.get("started_at"),payload.get("finished_at"),payload.get("retry_at"),_copy(payload.get("error_metadata",{})),_copy(payload.get("result_metadata",{})));self._occurrences[key]=r;self._next_occurrence_id+=1;return _copy(r)
    async def get_occurrence(self, owner_id, task_id, occurrence_key):
        r=self._occurrences.get((task_id,occurrence_key));return _copy(r) if r and r.owner_id==owner_id else None
    async def list_occurrences(self, owner_id, task_id=None, limit=100): return [_copy(r) for r in list(self._occurrences.values()) if r.owner_id==owner_id and (task_id is None or r.task_id==task_id)][:max(0,limit)]
    async def list_recoverable_occurrences(self, owner_id, limit=100): return [_copy(r) for r in self._occurrences.values() if r.owner_id==owner_id and r.status in {"claimed","running"}][:max(0,limit)]
    async def claim_occurrence(self, owner_id, task_id, occurrence_key):
        r=self._occurrences.get((task_id,occurrence_key))
        if not r or r.owner_id!=owner_id or r.status not in {"claimed","retry_pending","interrupted"}:return None
        r.status="running";r.claimed_at=r.claimed_at or _now();r.started_at=_now();r.updated_at=_now();return _copy(r)
    async def transition_occurrence(self, owner_id, task_id, occurrence_key, status, **updates):
        r=self._occurrences.get((task_id,occurrence_key))
        if not r or r.owner_id!=owner_id:return None
        if status not in OCCURRENCE_STATUSES or status not in _ALLOWED_OCCURRENCE_TRANSITIONS[r.status]:raise ValueError("invalid occurrence status transition")
        candidate={**r.as_dict(),**updates,"status":status};_validate_occurrence_input(candidate)
        for k,v in updates.items():
            if k in {"action_snapshot","occurrence_key","task_id","owner_id","definition_version","scheduled_for"}:raise ValueError("immutable occurrence field")
            setattr(r,k,_copy(v))
        r.status=status;r.updated_at=_now()
        if status in _TERMINAL_OCCURRENCE_STATUSES:r.finished_at=r.finished_at or r.updated_at
        return _copy(r)

class SupabaseTaskRepository(TaskRepository):
    def __init__(self, client, fallback=None, timeout=DB_TIMEOUT): self._client=client;self._fallback=fallback or InMemoryTaskRepository();self._timeout=timeout
    async def _run(self, fn):
        try:return await asyncio.wait_for(asyncio.to_thread(fn),timeout=self._timeout)
        except asyncio.CancelledError:raise
    def _task_payload(self, owner_id, data):
        payload={**data,"owner_id":owner_id};_validate_task_input(payload);return {k:_serialize(v) for k,v in payload.items() if k not in {"id","created_at","updated_at","terminal_at","version"}}
    def _occurrence_payload(self, owner_id, data):
        payload={**data,"owner_id":owner_id};_validate_occurrence_input(payload);return {k:_serialize(v) for k,v in payload.items() if k not in {"id","created_at","updated_at"}}
    async def create_task(self, owner_id, data):
        payload=self._task_payload(owner_id,data)
        try:
            result=await self._run(lambda:self._client.table("ai_tasks").insert(payload).execute());row=getattr(result,"data",None)
            if not row:raise RuntimeError("Supabase task insert returned no row")
            return _task_from_row(row[0] if isinstance(row,list) else row)
        except (ValueError,TypeError):raise
        except Exception as exc:logger.warning("Supabase task create failed; using fallback: %s",exc);return await self._fallback.create_task(owner_id,data)
    async def get_task(self, owner_id, task_id):
        try:
            result=await self._run(lambda:self._client.table("ai_tasks").select("*").eq("id",task_id).eq("owner_id",owner_id).maybe_single().execute());row=getattr(result,"data",None);return _task_from_row(row[0] if isinstance(row,list) else row) if row else None
        except Exception as exc:logger.warning("Supabase task read failed; using fallback: %s",exc);return await self._fallback.get_task(owner_id,task_id)
    async def list_tasks(self, owner_id):
        try:
            result=await self._run(lambda:self._client.table("ai_tasks").select("*").eq("owner_id",owner_id).order("updated_at",desc=True).execute());return [_task_from_row(row) for row in (getattr(result,"data",None) or [])]
        except Exception as exc:logger.warning("Supabase task list failed; using fallback: %s",exc);return await self._fallback.list_tasks(owner_id)
    async def list_due_tasks(self, owner_id, now, limit=10):
        try:
            result=await self._run(lambda:self._client.table("ai_tasks").select("*").eq("owner_id",owner_id).eq("status","active").lte("next_run_at",_serialize(now)).order("next_run_at").order("id").limit(limit).execute());return [_task_from_row(row) for row in (getattr(result,"data",None) or [])]
        except Exception as exc:logger.warning("Supabase due task query failed; using fallback: %s",exc);return await self._fallback.list_due_tasks(owner_id,now,limit)
    async def update_task(self, owner_id, task_id, expected_version, updates):
        current=await self.get_task(owner_id,task_id)
        if current is None or current.version!=expected_version:return None
        if "status" in updates and updates["status"] not in _ALLOWED_TASK_TRANSITIONS[current.status]:raise ValueError("invalid task status transition")
        merged=current.as_dict();merged.update(updates);merged["owner_id"]=owner_id;_validate_task_input(merged);outgoing={k:_serialize(v) for k,v in updates.items() if k not in {"id","created_at","updated_at","terminal_at","version"}};outgoing["version"]=expected_version+1
        if updates.get("status") in _TERMINAL_TASK_STATUSES:outgoing["terminal_at"]=_now().isoformat()
        try:
            result=await self._run(lambda:self._client.table("ai_tasks").update(outgoing).eq("id",task_id).eq("owner_id",owner_id).eq("version",expected_version).execute());row=getattr(result,"data",None);return _task_from_row(row[0] if isinstance(row,list) else row) if row else None
        except (ValueError,TypeError):raise
        except Exception as exc:logger.warning("Supabase task update failed; using fallback: %s",exc);return await self._fallback.update_task(owner_id,task_id,expected_version,updates)
    async def advance_next_run(self, owner_id, task_id, expected_version, next_run_at): return await self.update_task(owner_id,task_id,expected_version,{"next_run_at":next_run_at})
    async def transition_task(self, owner_id, task_id, status, expected_version=None):
        current=await self.get_task(owner_id,task_id)
        if not current:return None
        if status not in TASK_STATUSES or status not in _ALLOWED_TASK_TRANSITIONS[current.status]:raise ValueError("invalid task status transition")
        return await self.update_task(owner_id,task_id,expected_version if expected_version is not None else current.version,{"status":status})
    async def create_occurrence(self, owner_id, data):
        task=await self.get_task(owner_id,int(data.get("task_id",0)))
        if not task:raise ValueError("task not found for owner")
        payload=self._occurrence_payload(owner_id,data)
        try:
            existing=await self.get_occurrence(owner_id,payload["task_id"],payload["occurrence_key"])
            if existing:return existing
            result=await self._run(lambda:self._client.table("ai_task_occurrences").insert(payload).execute());row=getattr(result,"data",None)
            if not row:raise RuntimeError("Supabase occurrence insert returned no row")
            return _occurrence_from_row(row[0] if isinstance(row,list) else row)
        except (ValueError,TypeError):raise
        except Exception as exc:logger.warning("Supabase occurrence create failed; using fallback: %s",exc);return await self._fallback.create_occurrence(owner_id,data)
    async def get_occurrence(self, owner_id, task_id, occurrence_key):
        try:
            result=await self._run(lambda:self._client.table("ai_task_occurrences").select("*").eq("task_id",task_id).eq("occurrence_key",occurrence_key).eq("owner_id",owner_id).maybe_single().execute());row=getattr(result,"data",None);return _occurrence_from_row(row[0] if isinstance(row,list) else row) if row else None
        except Exception as exc:logger.warning("Supabase occurrence read failed; using fallback: %s",exc);return await self._fallback.get_occurrence(owner_id,task_id,occurrence_key)
    async def list_occurrences(self, owner_id, task_id=None, limit=100):
        try:
            query=self._client.table("ai_task_occurrences").select("*").eq("owner_id",owner_id)
            if task_id is not None:query=query.eq("task_id",task_id)
            result=await self._run(lambda:query.order("scheduled_for",desc=True).limit(limit).execute());return [_occurrence_from_row(row) for row in (getattr(result,"data",None) or [])]
        except Exception as exc:logger.warning("Supabase occurrence list failed; using fallback: %s",exc);return await self._fallback.list_occurrences(owner_id,task_id,limit)
    async def list_recoverable_occurrences(self, owner_id, limit=100):
        try:
            result=await self._run(lambda:self._client.table("ai_task_occurrences").select("*").eq("owner_id",owner_id).in_("status",["claimed","running"]).order("updated_at").limit(limit).execute());return [_occurrence_from_row(row) for row in (getattr(result,"data",None) or [])]
        except Exception as exc:logger.warning("Supabase recovery query failed; using fallback: %s",exc);return await self._fallback.list_recoverable_occurrences(owner_id,limit)
    async def claim_occurrence(self, owner_id, task_id, occurrence_key):
        current=await self.get_occurrence(owner_id,task_id,occurrence_key)
        if not current or current.status not in {"claimed","retry_pending","interrupted"}:return None
        now=_now().isoformat()
        try:
            result=await self._run(lambda:self._client.table("ai_task_occurrences").update({"status":"running","claimed_at":current.claimed_at.isoformat() if current.claimed_at else now,"started_at":now,"updated_at":now}).eq("task_id",task_id).eq("occurrence_key",occurrence_key).eq("owner_id",owner_id).eq("status",current.status).execute());row=getattr(result,"data",None);return _occurrence_from_row(row[0] if isinstance(row,list) else row) if row else None
        except Exception as exc:logger.warning("Supabase occurrence claim failed; using fallback: %s",exc);return await self._fallback.claim_occurrence(owner_id,task_id,occurrence_key)
    async def transition_occurrence(self, owner_id, task_id, occurrence_key, status, **updates):
        current=await self.get_occurrence(owner_id,task_id,occurrence_key)
        if not current:return None
        if status not in OCCURRENCE_STATUSES or status not in _ALLOWED_OCCURRENCE_TRANSITIONS[current.status]:raise ValueError("invalid occurrence status transition")
        immutable={"action_snapshot","occurrence_key","task_id","owner_id","definition_version","scheduled_for"}
        if immutable.intersection(updates):raise ValueError("immutable occurrence field")
        candidate={**current.as_dict(),**updates,"status":status};_validate_occurrence_input(candidate);outgoing={k:_serialize(v) for k,v in updates.items()};outgoing["status"]=status;outgoing["updated_at"]=_now().isoformat()
        if status in _TERMINAL_OCCURRENCE_STATUSES:outgoing.setdefault("finished_at",_now().isoformat())
        try:
            result=await self._run(lambda:self._client.table("ai_task_occurrences").update(outgoing).eq("task_id",task_id).eq("occurrence_key",occurrence_key).eq("owner_id",owner_id).eq("status",current.status).execute());row=getattr(result,"data",None);return _occurrence_from_row(row[0] if isinstance(row,list) else row) if row else None
        except (ValueError,TypeError):raise
        except Exception as exc:logger.warning("Supabase occurrence transition failed; using fallback: %s",exc);return await self._fallback.transition_occurrence(owner_id,task_id,occurrence_key,status,**updates)

_default_repository=None
def get_task_repository():
    global _default_repository
    if _default_repository is None:
        from backend.db.client import get_db
        client=get_db();_default_repository=SupabaseTaskRepository(client) if client is not None else InMemoryTaskRepository()
    return _default_repository
