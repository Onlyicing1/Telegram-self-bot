"""
Unified health checker — single source of truth for subsystem health.

Consolidates health checks for:
  - Telegram connection
  - Supabase
  - AI providers
  - Memory manager
  - Background workers (watchdog, heartbeat, keepalive, failsafe, diagnostics)
  - Runtime manager (conversation sessions)
  - Diagnostics system (event ring)

Avoids duplicated checks — every subsystem reports through this module.
The FastAPI /health endpoint and the .health panel both read from here.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from backend.health import snapshot as _health_snapshot, get_all_loop_progress, get_stale_loops

logger = logging.getLogger(__name__)

_STALE_LOOP_THRESHOLD = 90.0


def check_telegram() -> dict[str, Any]:
    snap = _health_snapshot()
    return {
        "name": "telegram",
        "healthy": bool(snap.get("telethon_connected", False)),
        "connected": snap.get("telethon_connected", False),
        "last_event_s": snap.get("last_telethon_event_s"),
        "last_rpc_s": snap.get("last_rpc_s"),
        "rpc_latency_ms": snap.get("rpc_latency_ms"),
        "client_generation": snap.get("client_generation", 0),
    }


def check_supabase() -> dict[str, Any]:
    try:
        from backend.db import client as db_client
        available = db_client.is_available()
        return {
            "name": "supabase",
            "healthy": available,
            "available": available,
        }
    except Exception as exc:
        return {"name": "supabase", "healthy": False, "error": str(exc)}


def check_ai_providers() -> dict[str, Any]:
    try:
        from backend.ai.engine.engine import get_engine
        engine = get_engine()
        active_name = engine.provider_manager.get_active_name()
        health = engine.engine_health()
        providers = engine.provider_manager.list_providers()
        return {
            "name": "ai_providers",
            "healthy": health == "READY",
            "active_provider": active_name,
            "provider_count": len(providers),
            "engine_health": health,
        }
    except Exception as exc:
        return {"name": "ai_providers", "healthy": False, "error": str(exc)}


def check_memory_manager() -> dict[str, Any]:
    try:
        from backend.ai.memory.manager import MemoryManager
        mgr = MemoryManager()
        status = mgr.status()
        return {
            "name": "memory_manager",
            "healthy": True,
            "short_count": status.get("short_count", 0),
            "long_available": status.get("long_available", False),
            "permanent_available": status.get("permanent_available", False),
        }
    except Exception as exc:
        return {"name": "memory_manager", "healthy": False, "error": str(exc)}


def check_background_workers() -> dict[str, Any]:
    snap = _health_snapshot()
    task_states = snap.get("task_states", {})
    workers = {
        "watchdog": snap.get("watchdog_ok", False),
        "heartbeat": snap.get("process_alive", False),
        "supervisor": snap.get("supervisor_ok", False),
        "bio_engine": snap.get("bio_cron_ok", False),
        "helper": snap.get("helper_connected", False),
    }
    stale = get_stale_loops(_STALE_LOOP_THRESHOLD)
    return {
        "name": "background_workers",
        "healthy": all(workers.values()) if workers else False,
        "workers": workers,
        "task_states": task_states,
        "stale_loops": stale,
    }


def check_runtime_manager() -> dict[str, Any]:
    try:
        from backend.ai.engine.engine import get_engine
        engine = get_engine()
        conv_mgr = engine.conversation_manager
        active = conv_mgr.active_count()
        snap = engine.metrics_snapshot()
        return {
            "name": "runtime_manager",
            "healthy": True,
            "active_sessions": active,
            "total_executions": snap.get("total_executions", 0),
            "successful_executions": snap.get("successful_executions", 0),
            "failed_executions": snap.get("failed_executions", 0),
        }
    except Exception as exc:
        return {"name": "runtime_manager", "healthy": False, "error": str(exc)}


def check_diagnostics() -> dict[str, Any]:
    try:
        from backend.diagnostics import get_events
        events = get_events()
        return {
            "name": "diagnostics",
            "healthy": True,
            "event_count": len(events),
        }
    except Exception as exc:
        return {"name": "diagnostics", "healthy": False, "error": str(exc)}


_CHECKS = [
    check_telegram,
    check_supabase,
    check_ai_providers,
    check_memory_manager,
    check_background_workers,
    check_runtime_manager,
    check_diagnostics,
]


def unified_snapshot() -> dict[str, Any]:
    """Run every health check and return a unified snapshot."""
    results = {}
    all_healthy = True
    for check_fn in _CHECKS:
        try:
            result = check_fn()
        except Exception as exc:
            result = {"name": check_fn.__name__, "healthy": False, "error": str(exc)}
        results[result["name"]] = result
        if not result.get("healthy", False):
            all_healthy = False

    base = _health_snapshot()
    return {
        "status": base.get("status", "unknown"),
        "overall_healthy": all_healthy,
        "runtime_state": base.get("runtime_state", "unknown"),
        "uptime_s": base.get("uptime_s"),
        "restart_count": base.get("restart_count", 0),
        "checks": results,
    }


def overall_status() -> str:
    """Return a single-word status: 'ok', 'degraded', or 'down'."""
    snap = unified_snapshot()
    if snap["overall_healthy"]:
        return "ok"
    base_status = snap.get("status", "unknown")
    if base_status == "down":
        return "down"
    return "degraded"
