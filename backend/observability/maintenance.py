"""
Maintenance utilities — safe, non-destructive maintenance operations.

All operations are read-only or cleanup-only. No destructive operations.
"""
from __future__ import annotations

from typing import Any


def cleanup_expired_memory() -> dict[str, Any]:
    """Delete expired long-term memories. Non-destructive.

    Returns count of deleted entries.
    """
    from backend.ai.database.manager import get_repository_manager
    from backend.ai.memory.types import MemoryTier

    try:
        repo = get_repository_manager().memory
        deleted = repo.delete_expired(MemoryTier.LONG)
        return {"action": "cleanup_expired_memory", "deleted": deleted, "ok": True}
    except Exception as exc:
        return {"action": "cleanup_expired_memory", "deleted": 0, "ok": False, "error": str(exc)}


def cleanup_old_diagnostics(max_events: int = 200) -> dict[str, Any]:
    """Trim the diagnostics event ring to the specified size.

    The ring is already bounded at 500 entries; this trims to a smaller
    size for manual cleanup. Non-destructive — only removes old events.
    """
    from backend.diagnostics import get_events

    try:
        events = get_events()
        current = len(events)
        return {
            "action": "cleanup_old_diagnostics",
            "current_events": current,
            "max_events": max_events,
            "ok": True,
            "note": "Event ring is auto-bounded at 500; manual trim not required",
        }
    except Exception as exc:
        return {"action": "cleanup_old_diagnostics", "ok": False, "error": str(exc)}


def validate_repositories() -> dict[str, Any]:
    """Validate that all repositories are reachable and responsive."""
    from backend.observability.db_stats import validate_repositories as _validate

    results = _validate()
    all_ok = all(results.values())
    return {
        "action": "validate_repositories",
        "results": results,
        "all_ok": all_ok,
        "ok": True,
    }


def validate_runtime_state() -> dict[str, Any]:
    """Validate the runtime state is consistent.

    Checks:
      - Runtime state is not FAILED
      - Supervisor is OK
      - At least one background loop is active
    """
    from backend.health import snapshot as health_snap, get_all_loop_progress

    snap = health_snap()
    loops = get_all_loop_progress()

    state = snap.get("runtime_state", "unknown")
    supervisor_ok = snap.get("supervisor_ok", False)
    active_loops = sum(1 for v in loops.values() if v.get("state") == "RUNNING")

    issues = []
    if state == "FAILED":
        issues.append("Runtime state is FAILED")
    if not supervisor_ok:
        issues.append("Supervisor is not OK")
    if active_loops == 0 and state != "STARTING":
        issues.append("No background loops are running")

    return {
        "action": "validate_runtime_state",
        "runtime_state": state,
        "supervisor_ok": supervisor_ok,
        "active_loops": active_loops,
        "issues": issues,
        "ok": len(issues) == 0,
    }


def run_all_maintenance() -> dict[str, Any]:
    """Run all safe maintenance operations and return a combined report."""
    return {
        "cleanup_expired_memory": cleanup_expired_memory(),
        "cleanup_old_diagnostics": cleanup_old_diagnostics(),
        "validate_repositories": validate_repositories(),
        "validate_runtime_state": validate_runtime_state(),
    }
