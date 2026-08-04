"""
Unified health snapshot generator.

Reuses backend.runtime.health_check.unified_snapshot() as the single
source of truth. Adds observability-specific sections on top.
"""
from __future__ import annotations

from typing import Any


def health_snapshot() -> dict[str, Any]:
    """Return a unified health snapshot covering all subsystems.

    Aggregates from:
      - health_check.unified_snapshot()  (Telegram, AI, Supabase, workers, etc.)
      - observability.runtime_status()  (memory, CPU, pending tasks)
      - observability.ai_statistics()  (AI execution metrics)
      - observability.database_statistics()  (repository counts)
    """
    from backend.runtime.health_check import unified_snapshot
    from backend.observability.runtime_status import runtime_status
    from backend.observability.ai_stats import ai_statistics
    from backend.observability.db_stats import database_statistics

    base = unified_snapshot()
    runtime = runtime_status()
    ai = ai_statistics()
    db = database_statistics()

    return {
        "status": base.get("status", "unknown"),
        "overall_healthy": base.get("overall_healthy", False),
        "runtime_state": base.get("runtime_state", "unknown"),
        "uptime_s": base.get("uptime_s"),
        "restart_count": base.get("restart_count", 0),
        "checks": base.get("checks", {}),
        "runtime": runtime,
        "ai": ai,
        "database": db,
    }
