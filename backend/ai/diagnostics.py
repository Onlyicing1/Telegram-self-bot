"""
AI request diagnostics — lightweight in-memory tracker for AI lifecycle
observability.

This module provides:
  - A registry of active AI requests (start time, current stage, request_id).
  - Timestamps for the last successful completion of each major stage.
  - A correlation ID generator.
  - Snapshot helpers consumed by the runtime heartbeat.

It is purely diagnostic. It never raises, never blocks, and never cancels
any real operation. All public functions swallow exceptions so that
diagnostics can never break the AI path.
"""
from __future__ import annotations

import logging
import time
import uuid
from typing import Any

logger = logging.getLogger(__name__)

# ── Active request registry ──
# request_id -> {"started_at": float, "stage": str, "owner_id": int}
_active: dict[str, dict[str, Any]] = {}

# ── Last successful stage timestamps (wall-clock) ──
_last_success: dict[str, float] = {}

# ── Stage names ──
STAGES = (
    "CONFIG_LOAD",
    "SESSION_OPERATION",
    "MEMORY_OPERATION",
    "DB_OPERATION",
    "PROVIDER_REQUEST",
    "TELEGRAM_REPLY",
)


def new_request_id() -> str:
    """Generate a short correlation ID for an AI request."""
    try:
        return uuid.uuid4().hex[:12]
    except Exception:
        return f"ai-{int(time.time() * 1000) % 1000000:06d}"


def register_start(request_id: str, owner_id: int = 0) -> None:
    try:
        _active[request_id] = {
            "started_at": time.monotonic(),
            "stage": "INIT",
            "owner_id": owner_id,
        }
    except Exception:
        pass


def set_stage(request_id: str, stage: str) -> None:
    try:
        entry = _active.get(request_id)
        if entry is not None:
            entry["stage"] = stage
    except Exception:
        pass


def mark_success(stage: str) -> None:
    """Record that a stage completed successfully (wall-clock time)."""
    try:
        _last_success[stage] = time.time()
    except Exception:
        pass


def register_end(request_id: str) -> None:
    try:
        _active.pop(request_id, None)
    except Exception:
        pass


def active_count() -> int:
    try:
        return len(_active)
    except Exception:
        return 0


def oldest_active_age_s() -> float:
    """Return age in seconds of the oldest active AI request, or 0."""
    try:
        if not _active:
            return 0.0
        now = time.monotonic()
        return max(0.0, now - min(e["started_at"] for e in _active.values()))
    except Exception:
        return 0.0


def oldest_active_stage() -> str:
    """Return the stage of the oldest active request, or empty string."""
    try:
        if not _active:
            return ""
        oldest_id = min(_active, key=lambda rid: _active[rid]["started_at"])
        return _active[oldest_id].get("stage", "")
    except Exception:
        return ""


def last_success_age_s(stage: str) -> float:
    """Return age in seconds since last successful completion of *stage*."""
    try:
        ts = _last_success.get(stage)
        if not ts:
            return -1.0
        return max(0.0, time.time() - ts)
    except Exception:
        return -1.0


def snapshot() -> dict[str, Any]:
    """Compact snapshot for heartbeat consumption."""
    try:
        return {
            "ai_active": len(_active),
            "ai_oldest_age_s": round(oldest_active_age_s(), 1),
            "ai_stage": oldest_active_stage() or "-",
            "ai_last_provider_s": round(last_success_age_s("PROVIDER_REQUEST"), 0),
            "ai_last_db_s": round(last_success_age_s("DB_OPERATION"), 0),
            "ai_last_tg_reply_s": round(last_success_age_s("TELEGRAM_REPLY"), 0),
        }
    except Exception:
        return {
            "ai_active": 0,
            "ai_oldest_age_s": 0.0,
            "ai_stage": "-",
            "ai_last_provider_s": -1,
            "ai_last_db_s": -1,
            "ai_last_tg_reply_s": -1,
        }
