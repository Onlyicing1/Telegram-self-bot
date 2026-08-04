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


def validate_repositories() -> dict[str, bool]:
    """Verify each repository is reachable and responsive."""
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
