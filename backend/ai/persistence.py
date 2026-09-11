"""
Supabase-backed AI persistence layer.

Wraps the existing in-memory repositories with Supabase persistence.
When Supabase is available, conversation history and memories persist
across restarts. When unavailable, falls back to in-memory storage.

This module provides thin async wrappers that run synchronous
supabase-py calls on the bounded shared pool owned by
backend/db/client.py (``run_sync_db``). ``schedule_audit`` is the only
way best-effort audit writes are dispatched: it bounds the number of
in-flight records so audit persistence can never accumulate without
limit behind the primary execution path.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

_DB_TIMEOUT = 10.0


def _get_db():
    from backend.db.client import get_db
    return get_db()


async def _run_sync(fn, *args, **kwargs):
    # The same single bounded Supabase pool the DB and task layers use, so audit
    # writes share one bounded resource instead of each drawing its own worker.
    from backend.db.client import run_sync_db
    return await run_sync_db(fn, *args, timeout=_DB_TIMEOUT, **kwargs)


# Best-effort audit persistence must never multiply without bound: every
# in-flight record holds a worker thread and a pooled Supabase connection for
# the whole HTTP round trip. The primary execution path must stay non-blocking,
# so saturation drops the record (counted and logged) instead of queueing it.
_AUDIT_MAX_INFLIGHT = 8
_audit_inflight = 0
_audit_dropped = 0


def audit_inflight() -> int:
    """Number of audit persistence records currently in flight."""
    return _audit_inflight


def audit_dropped() -> int:
    """Number of audit records dropped because the in-flight bound was full."""
    return _audit_dropped


def schedule_audit(factory, *, name: str) -> bool:
    """Schedule one best-effort audit coroutine under a hard in-flight bound.

    ``factory`` is a zero-arg callable returning a FRESH coroutine, so a record
    dropped for saturation never leaves an unawaited coroutine behind. Returns
    True only when the coroutine was actually scheduled; a failure to schedule
    is never raised at the caller.
    """
    global _audit_inflight, _audit_dropped
    if _audit_inflight >= _AUDIT_MAX_INFLIGHT:
        _audit_dropped += 1
        logger.warning(
            "AI audit persistence saturated — dropped record name=%s inflight=%d dropped_total=%d",
            name, _audit_inflight, _audit_dropped,
        )
        return False
    _audit_inflight += 1

    async def _runner() -> None:
        global _audit_inflight
        try:
            await factory()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - audit must never crash
            logger.warning("AI audit persistence failed name=%s: %r", name, exc)
        finally:
            _audit_inflight -= 1

    # Resolved at call time so the guard stays patchable by existing tests.
    from backend.runtime.task_guard import guarded_create_task
    guarded_create_task(_runner(), name=name)
    return True


# ── Session persistence ──

def _create_session_sync(session_id: str, owner_id: int) -> bool:
    db = _get_db()
    if not db:
        return False
    try:
        db.table("ai_sessions").insert({
            "session_id": session_id,
            "owner_id": owner_id,
            "status": "active",
        }).execute()
        return True
    except Exception as exc:
        logger.warning("AI session insert failed: %s", exc)
        return False


async def create_session(session_id: str, owner_id: int) -> bool:
    try:
        return await _run_sync(_create_session_sync, session_id, owner_id)
    except Exception as exc:
        logger.warning("AI create_session failed: %s", exc)
        return False


def _update_session_sync(session_id: str, updates: dict[str, Any]) -> bool:
    db = _get_db()
    if not db:
        return False
    try:
        db.table("ai_sessions").update(updates).eq("session_id", session_id).execute()
        return True
    except Exception as exc:
        logger.warning("AI update_session failed: %s", exc)
        return False


async def update_session(session_id: str, updates: dict[str, Any]) -> bool:
    try:
        return await _run_sync(_update_session_sync, session_id, updates)
    except Exception as exc:
        logger.warning("AI update_session failed: %s", exc)
        return False


def _get_session_sync(session_id: str) -> dict | None:
    db = _get_db()
    if not db:
        return None
    try:
        result = db.table("ai_sessions").select("*").eq("session_id", session_id).maybe_single().execute()
        return result.data
    except Exception as exc:
        logger.warning("AI get_session failed: %s", exc)
        return None


async def get_session(session_id: str) -> dict | None:
    try:
        return await _run_sync(_get_session_sync, session_id)
    except Exception as exc:
        logger.warning("AI get_session failed: %s", exc)
        return None


# ── Message persistence ──

def _add_message_sync(session_id: str, owner_id: int, role: str, content: str,
                       token_count: int = 0, provider: str = "", model: str = "") -> bool:
    db = _get_db()
    if not db:
        return False
    try:
        db.table("ai_messages").insert({
            "session_id": session_id,
            "owner_id": owner_id,
            "role": role,
            "content": content[:8000],
            "token_count": token_count,
            "provider": provider,
            "model": model,
        }).execute()
        db.table("ai_sessions").update({
            "message_count": db.table("ai_messages").select("id", count="exact").eq("session_id", session_id).execute().count or 0,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }).eq("session_id", session_id).execute()
        return True
    except Exception as exc:
        logger.warning("AI add_message failed: %s", exc)
        return False


async def add_message(session_id: str, owner_id: int, role: str, content: str,
                      token_count: int = 0, provider: str = "", model: str = "") -> bool:
    try:
        return await _run_sync(_add_message_sync, session_id, owner_id, role, content, token_count, provider, model)
    except Exception as exc:
        logger.warning("AI add_message failed: %s", exc)
        return False


def _get_messages_sync(session_id: str, limit: int = 20) -> list[dict]:
    db = _get_db()
    if not db:
        return []
    try:
        result = (
            db.table("ai_messages")
            .select("*")
            .eq("session_id", session_id)
            .order("created_at", desc=False)
            .limit(limit)
            .execute()
        )
        return result.data or []
    except Exception as exc:
        logger.warning("AI get_messages failed: %s", exc)
        return []


async def get_messages(session_id: str, limit: int = 20) -> list[dict]:
    try:
        return await _run_sync(_get_messages_sync, session_id, limit)
    except Exception as exc:
        logger.warning("AI get_messages failed: %s", exc)
        return []


# ── Memory persistence ──

def _save_memory_sync(owner_id: int, tier: str, category: str, content: str,
                      importance: float, expires_at: str | None, metadata: dict) -> bool:
    db = _get_db()
    if not db:
        return False
    try:
        db.table("ai_memories").insert({
            "owner_id": owner_id,
            "tier": tier,
            "category": category,
            "content": content[:8000],
            "importance": importance,
            "expires_at": expires_at,
            "metadata": metadata,
        }).execute()
        return True
    except Exception as exc:
        logger.warning("AI save_memory failed: %s", exc)
        return False


async def save_memory(owner_id: int, tier: str, category: str, content: str,
                       importance: float = 0.5, expires_at: str | None = None,
                       metadata: dict | None = None) -> bool:
    try:
        return await _run_sync(_save_memory_sync, owner_id, tier, category, content, importance, expires_at, metadata or {})
    except Exception as exc:
        logger.warning("AI save_memory failed: %s", exc)
        return False


def _query_memories_sync(owner_id: int, tier: str | None = None, limit: int = 20,
                         min_importance: float = 0.0,
                         category: str | None = None,
                         content: str | None = None) -> list[dict]:
    db = _get_db()
    if not db:
        return []
    try:
        q = db.table("ai_memories").select("*").eq("owner_id", owner_id)
        if tier:
            q = q.eq("tier", tier)
        if category:
            q = q.eq("category", category)
        if content is not None:
            q = q.eq("content", content)
        if min_importance > 0:
            q = q.gte("importance", min_importance)
        # Deterministic order: importance first, then recency, then id.
        result = (
            q.order("importance", desc=True)
            .order("created_at", desc=True)
            .order("id")
            .limit(limit)
            .execute()
        )
        return result.data or []
    except Exception as exc:
        logger.warning("AI query_memories failed: %s", exc)
        return []


async def query_memories(owner_id: int, tier: str | None = None, limit: int = 20,
                          min_importance: float = 0.0,
                          category: str | None = None,
                          content: str | None = None) -> list[dict]:
    try:
        return await _run_sync(
            _query_memories_sync, owner_id, tier, limit, min_importance, category, content,
        )
    except Exception as exc:
        logger.warning("AI query_memories failed: %s", exc)
        return []


def _delete_expired_memories_sync(tier: str) -> int:
    db = _get_db()
    if not db:
        return 0
    try:
        now = datetime.now(timezone.utc).isoformat()
        result = db.table("ai_memories").delete().eq("tier", tier).lt("expires_at", now).execute()
        return len(result.data) if result.data else 0
    except Exception as exc:
        logger.warning("AI delete_expired_memories failed: %s", exc)
        return 0


async def delete_expired_memories(tier: str) -> int:
    try:
        return await _run_sync(_delete_expired_memories_sync, tier)
    except Exception as exc:
        logger.warning("AI delete_expired_memories failed: %s", exc)
        return 0


def _delete_memory_sync(entry_id: str) -> bool:
    db = _get_db()
    if not db:
        return False
    try:
        db.table("ai_memories").delete().eq("id", entry_id).execute()
        return True
    except Exception as exc:
        logger.warning("AI delete_memory failed: %s", exc)
        return False


def _count_memories_sync(owner_id: int, tier: str) -> int:
    db = _get_db()
    if not db:
        return 0
    try:
        result = (
            db.table("ai_memories")
            .select("id", count="exact")
            .eq("owner_id", owner_id)
            .eq("tier", tier)
            .execute()
        )
        return result.count or 0
    except Exception as exc:
        logger.warning("AI count_memories failed: %s", exc)
        return 0


# ── Tool history persistence ──

def _record_tool_call_sync(owner_id: int, session_id: str, tool_name: str,
                           arguments: dict, success: bool, message: str,
                           latency_ms: float) -> bool:
    db = _get_db()
    if not db:
        return False
    try:
        db.table("ai_tool_history").insert({
            "owner_id": owner_id,
            "session_id": session_id,
            "tool_name": tool_name,
            "arguments": arguments,
            "result_success": success,
            "result_message": message[:2000],
            "latency_ms": latency_ms,
        }).execute()
        return True
    except Exception as exc:
        logger.warning("AI record_tool_call failed: %s", exc)
        return False


async def record_tool_call(owner_id: int, session_id: str, tool_name: str,
                            arguments: dict, success: bool, message: str,
                            latency_ms: float) -> bool:
    try:
        return await _run_sync(_record_tool_call_sync, owner_id, session_id, tool_name, arguments, success, message, latency_ms)
    except Exception as exc:
        logger.warning("AI record_tool_call failed: %s", exc)
        return False
