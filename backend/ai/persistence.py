"""
Supabase-backed AI persistence layer.

Wraps the existing in-memory repositories with Supabase persistence.
When Supabase is available, conversation history and memories persist
across restarts. When unavailable, falls back to in-memory storage.

This module provides thin async wrappers that run synchronous
supabase-py calls via asyncio.to_thread with bounded timeouts,
matching the pattern in backend/db/client.py.
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
    return await asyncio.wait_for(
        asyncio.to_thread(fn, *args, **kwargs),
        timeout=_DB_TIMEOUT,
    )


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
                         min_importance: float = 0.0) -> list[dict]:
    db = _get_db()
    if not db:
        return []
    try:
        q = db.table("ai_memories").select("*").eq("owner_id", owner_id)
        if tier:
            q = q.eq("tier", tier)
        if min_importance > 0:
            q = q.gte("importance", min_importance)
        result = q.order("importance", desc=True).limit(limit).execute()
        return result.data or []
    except Exception as exc:
        logger.warning("AI query_memories failed: %s", exc)
        return []


async def query_memories(owner_id: int, tier: str | None = None, limit: int = 20,
                          min_importance: float = 0.0) -> list[dict]:
    try:
        return await _run_sync(_query_memories_sync, owner_id, tier, limit, min_importance)
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
