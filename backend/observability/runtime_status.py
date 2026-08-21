"""
Runtime status service — exposes process, worker, and resource metrics.

Reuses backend.health.snapshot() and backend.health.get_all_loop_progress()
as the single source of truth. No duplicated health checks.
"""
from __future__ import annotations

import os
import resource
import time
from typing import Any

_PROCESS_START = time.monotonic()


def _process_uptime() -> float:
    return round(time.monotonic() - _PROCESS_START, 1)


def _memory_usage_mb() -> float:
    try:
        rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        if hasattr(resource, "RUSAGE_SELF"):
            return round(rss / 1024, 1)
        return round(rss / (1024 * 1024), 1)
    except Exception:
        return 0.0


def _cpu_time_s() -> float:
    try:
        usage = resource.getrusage(resource.RUSAGE_SELF)
        return round(usage.ru_utime + usage.ru_stime, 2)
    except Exception:
        return 0.0


def _pending_tasks() -> int:
    import asyncio
    try:
        return len(asyncio.all_tasks())
    except RuntimeError:
        return 0


def _ai_status() -> dict[str, Any]:
    try:
        from backend.ai.engine.engine import get_engine
        engine = get_engine()
        return {
            "available": True,
            "engine_health": engine.engine_health(),
            "active_provider": engine.provider_manager.get_active_name(),
            "active_model": _get_active_model(engine.provider_manager),
            "provider_count": len(engine.provider_manager.list_providers()),
            "total_requests": engine.metrics_snapshot().get("total_executions", 0),
        }
    except Exception:
        return {"available": False}


def _get_active_model(provider_mgr: Any) -> str:
    try:
        config = provider_mgr.get_provider_config()
        return config.model if config and config.model else ""
    except Exception:
        return ""


def _queue_sizes() -> dict[str, int]:
    sizes: dict[str, int] = {}
    try:
        from backend.helper.inline_engine import _self_client
        client = _self_client
        if client is not None and hasattr(client, "_updates"):
            upd = client._updates
            if hasattr(upd, "_pending"):
                sizes["telegram_updates"] = len(upd._pending)
    except Exception:
        pass
    return sizes


def runtime_status() -> dict[str, Any]:
    """Return a runtime status snapshot.

    Aggregates from:
      - backend.health.snapshot()  (Telegram, supervisor, workers)
      - backend.health.get_all_loop_progress()  (background loops)
      - resource module  (memory, CPU)
      - backend.ai.engine  (AI status, active provider/model)
    """
    from backend.health import snapshot as health_snap, get_all_loop_progress

    snap = health_snap()
    loops = get_all_loop_progress()

    return {
        "telegram_connected": snap.get("telethon_connected", False),
        "runtime_state": snap.get("runtime_state", "unknown"),
        "supervisor_ok": snap.get("supervisor_ok", False),
        "uptime_s": snap.get("uptime_s") or _process_uptime(),
        "restart_count": snap.get("restart_count", 0),
        "client_generation": snap.get("client_generation", 0),
        "memory_mb": _memory_usage_mb(),
        "cpu_time_s": _cpu_time_s(),
        "pending_tasks": _pending_tasks(),
        "task_states": snap.get("task_states", {}),
        "background_loops": {
            name: {
                "state": info.get("state", "unknown"),
                "last_success_s": info.get("last_success_s"),
            }
            for name, info in loops.items()
        },
        "heartbeat_age_s": snap.get("heartbeat_age_s"),
        "rpc_latency_ms": snap.get("rpc_latency_ms"),
        "last_rpc_s": snap.get("last_rpc_s"),
        "last_command_s": snap.get("last_command_s"),
        "ai_status": _ai_status(),
        "queue_sizes": _queue_sizes(),
    }
