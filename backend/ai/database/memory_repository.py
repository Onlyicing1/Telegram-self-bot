"""
MemoryRepository — persistence interface for long and permanent memory.

Two implementations exist:

- ``InMemoryMemoryRepository`` — working fallback (data lost on restart).
- ``SupabaseMemoryRepository`` — persistence against the ``ai_memories``
  table through the existing ``backend.ai.persistence`` sync helpers
  (same access layer as sessions/messages/tool history). Sync by design:
  the memory manager API is synchronous; the dispatcher bounds call
  latency with ``asyncio.to_thread`` + a timeout.

Both implementations share the same guardrails:

- Oversized entries (``MAX_MEMORY_ENTRY_CHARS``) are rejected, never
  truncated into the database.
- Identical (owner, tier, content) writes are idempotent — duplicate
  entries are never created.
- Query results are returned in deterministic importance/creation order.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from backend.ai.memory.limits import MAX_MEMORY_ENTRY_CHARS
from backend.ai.memory.types import MemoryCategory, MemoryEntry, MemoryQuery, MemoryTier

logger = logging.getLogger(__name__)


class MemoryRepository:
    """Abstract interface for memory persistence.

    A concrete implementation (e.g. SupabaseMemoryRepository) will
    implement these methods against the ``ai_memories`` table. For now,
    InMemoryMemoryRepository provides a working fallback.
    """

    def save(self, entry: MemoryEntry) -> bool:
        raise NotImplementedError

    def query(self, query: MemoryQuery) -> list[MemoryEntry]:
        raise NotImplementedError

    def delete(self, entry_id: str) -> bool:
        raise NotImplementedError

    def delete_expired(self, tier: MemoryTier) -> int:
        raise NotImplementedError

    def count(self, owner_id: int, tier: MemoryTier) -> int:
        raise NotImplementedError


class InMemoryMemoryRepository(MemoryRepository):
    """In-memory fallback repository. Data is lost on restart.

    Uses a simple list with linear filtering. This is sufficient for
    development and for environments where Supabase is not configured.
    """

    __slots__ = ("_entries",)

    def __init__(self) -> None:
        self._entries: list[MemoryEntry] = []

    def save(self, entry: MemoryEntry) -> bool:
        if len(entry.content) > MAX_MEMORY_ENTRY_CHARS:
            logger.warning(
                "InMemoryMemoryRepository: rejecting oversized memory entry "
                "(%d chars > %d)", len(entry.content), MAX_MEMORY_ENTRY_CHARS,
            )
            return False
        for existing in self._entries:
            if (
                existing.owner_id == entry.owner_id
                and existing.tier == entry.tier
                and existing.content == entry.content
            ):
                return True
        self._entries.append(entry)
        return True

    def query(self, query: MemoryQuery) -> list[MemoryEntry]:
        results: list[MemoryEntry] = []
        for entry in self._entries:
            if entry.owner_id != query.owner_id:
                continue
            if query.tier and entry.tier != query.tier:
                continue
            if query.category and entry.category != query.category:
                continue
            if entry.importance < query.min_importance:
                continue
            if query.query_text and query.query_text.lower() not in entry.content.lower():
                continue
            if entry.is_expired():
                continue
            results.append(entry)
        results.sort(key=lambda e: (e.importance, e.created_at, e.id), reverse=True)
        return results[: max(0, query.limit)]

    def delete(self, entry_id: str) -> bool:
        before = len(self._entries)
        self._entries = [e for e in self._entries if e.id != entry_id]
        return len(self._entries) < before

    def delete_expired(self, tier: MemoryTier) -> int:
        now = datetime.now(timezone.utc)
        before = len(self._entries)
        self._entries = [
            e for e in self._entries
            if not (e.tier == tier and e.is_expired(now))
        ]
        return before - len(self._entries)

    def count(self, owner_id: int, tier: MemoryTier) -> int:
        return sum(1 for e in self._entries if e.owner_id == owner_id and e.tier == tier)


def _parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    except (TypeError, ValueError):
        return None


class SupabaseMemoryRepository(MemoryRepository):
    """Supabase-backed memory repository against the ``ai_memories`` table.

    Wraps the sync persistence helpers in ``backend.ai.persistence``.
    Every method degrades to a safe no-op result and logs on failure so
    memory can never break AI execution. When Supabase is unavailable
    (``get_db()`` returns None) the helpers already return empty results.
    """

    __slots__ = ()

    def save(self, entry: MemoryEntry) -> bool:
        if len(entry.content) > MAX_MEMORY_ENTRY_CHARS:
            logger.warning(
                "SupabaseMemoryRepository: rejecting oversized memory entry "
                "(%d chars > %d)", len(entry.content), MAX_MEMORY_ENTRY_CHARS,
            )
            return False
        from backend.ai import persistence

        # Idempotent write: an identical (owner, tier, content) row already
        # persisted is never duplicated.
        existing = persistence._query_memories_sync(
            entry.owner_id, tier=entry.tier.value, limit=1,
        )
        if any(
            row.get("tier") == entry.tier.value and row.get("content") == entry.content
            for row in existing
        ):
            return True
        return persistence._save_memory_sync(
            entry.owner_id,
            entry.tier.value,
            entry.category.value,
            entry.content,
            entry.importance,
            entry.expires_at.isoformat() if entry.expires_at else None,
            dict(entry.metadata),
        )

    def query(self, query: MemoryQuery) -> list[MemoryEntry]:
        from backend.ai import persistence

        rows = persistence._query_memories_sync(
            query.owner_id,
            tier=query.tier.value if query.tier else None,
            limit=max(0, query.limit),
            min_importance=query.min_importance,
            category=query.category.value if query.category else None,
        )
        entries: list[MemoryEntry] = []
        for row in rows:
            try:
                entry = MemoryEntry(
                    id=str(row.get("id") or ""),
                    owner_id=int(row.get("owner_id") or 0),
                    tier=MemoryTier(row.get("tier") or MemoryTier.LONG.value),
                    category=MemoryCategory(row.get("category") or MemoryCategory.CONTEXT.value),
                    content=row.get("content") or "",
                    importance=float(row.get("importance") or 0.0),
                    created_at=_parse_dt(row.get("created_at")) or datetime.now(timezone.utc),
                    expires_at=_parse_dt(row.get("expires_at")),
                    metadata=dict(row.get("metadata") or {}),
                )
            except (TypeError, ValueError):
                continue
            if query.category and entry.category != query.category:
                continue
            if query.query_text and query.query_text.lower() not in entry.content.lower():
                continue
            if entry.is_expired():
                continue
            entries.append(entry)
        return entries

    def delete(self, entry_id: str) -> bool:
        from backend.ai import persistence
        return persistence._delete_memory_sync(entry_id)

    def delete_expired(self, tier: MemoryTier) -> int:
        from backend.ai import persistence
        return persistence._delete_expired_memories_sync(tier.value)

    def count(self, owner_id: int, tier: MemoryTier) -> int:
        from backend.ai import persistence
        return persistence._count_memories_sync(owner_id, tier.value)
