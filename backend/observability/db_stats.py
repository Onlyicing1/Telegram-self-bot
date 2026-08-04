"""
Database statistics aggregator — aggregates repository row counts and health.

Reuses RepositoryManager and its repositories. No duplicated state.
"""
from __future__ import annotations

from typing import Any


def database_statistics(owner_id: int = 0) -> dict[str, Any]:
    """Return aggregated database statistics.

    Reads from:
      - RepositoryManager.status()  (Supabase availability)
      - SessionRepository.list_sessions()  (session count)
      - MessageRepository  (message count per session)
      - MemoryRepository.count()  (memory count per tier)
      - ToolHistoryRepository.count()  (tool history size)
      - ProviderStatsRepository.list_all()  (provider stats)
    """
    from backend.ai.database.manager import get_repository_manager
    from backend.ai.memory.types import MemoryTier

    try:
        mgr = get_repository_manager()
    except Exception:
        return {"available": False, "error": "repository manager not initialized"}

    try:
        status = mgr.status()
        sessions = mgr.session.list_sessions(owner_id, limit=10000)
        total_messages = 0
        for s in sessions:
            try:
                total_messages += mgr.message.count(s.session_id)
            except Exception:
                pass

        long_count = _safe_count(mgr.memory, owner_id, MemoryTier.LONG)
        permanent_count = _safe_count(mgr.memory, owner_id, MemoryTier.PERMANENT)
        tool_history_count = _safe_tool_count(mgr.tool_history, owner_id)
        provider_stats = _safe_provider_stats(mgr.provider_stats, owner_id)

        return {
            "available": True,
            "supabase_available": status.get("supabase_available", False),
            "total_sessions": len(sessions),
            "total_messages": total_messages,
            "long_term_memories": long_count,
            "permanent_memories": permanent_count,
            "tool_history_size": tool_history_count,
            "provider_stats": provider_stats,
            "database_latency_ms": _measure_db_latency(),
            "slow_queries": _extract_slow_queries(),
        }
    except Exception as exc:
        return {"available": False, "error": str(exc)}


def _safe_count(repo: Any, owner_id: int, tier: Any) -> int:
    try:
        return repo.count(owner_id, tier)
    except Exception:
        return 0


def _safe_tool_count(repo: Any, owner_id: int) -> int:
    try:
        return repo.count(owner_id)
    except Exception:
        return 0


def _safe_provider_stats(repo: Any, owner_id: int) -> list[dict[str, Any]]:
    try:
        records = repo.list_all(owner_id)
        return [r.__dict__ if hasattr(r, "__dict__") else dict(r) for r in records]
    except Exception:
        return []


def _measure_db_latency() -> float | None:
    """Measure a quick DB round-trip latency in milliseconds.

    Uses the diagnostics event ring to find the most recent DB operation
    duration. Returns None if no DB events have been recorded.
    """
    try:
        from backend.diagnostics import get_events
        events = get_events()
        for e in reversed(events):
            if e.get("module") == "database" and e.get("duration_ms", 0) > 0:
                return round(e["duration_ms"], 1)
    except Exception:
        pass
    return None


def _extract_slow_queries(threshold_ms: float = 500.0) -> list[dict[str, Any]]:
    """Extract slow database operations from the diagnostics ring.

    Returns operations with duration_ms above the threshold, newest first.
    """
    try:
        from backend.diagnostics import get_events
        events = get_events()
        slow = [
            {
                "module": e.get("module", ""),
                "action": e.get("action", ""),
                "duration_ms": e.get("duration_ms", 0),
                "result": e.get("result", ""),
                "details": e.get("details", ""),
            }
            for e in events
            if e.get("module") == "database"
            and isinstance(e.get("duration_ms"), (int, float))
            and e["duration_ms"] >= threshold_ms
        ]
        slow.sort(key=lambda x: x["duration_ms"], reverse=True)
        return slow[:10]
    except Exception:
        return []


def validate_repositories() -> dict[str, bool]:
    from backend.ai.database.manager import get_repository_manager

    try:
        mgr = get_repository_manager()
    except Exception:
        return {"repository_manager": False}

    results = {}
    for name in ("session", "message", "memory", "tool_history", "provider_stats"):
        try:
            repo = getattr(mgr, name, None)
            results[name] = repo is not None
        except Exception:
            results[name] = False
    return results
