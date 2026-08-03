"""
MemoryRepository — persistence interface for long and permanent memory.

This is an interface-only module. It defines the contract that a future
Supabase-backed implementation will fulfill. The actual implementation
will be created when the ``ai_memories`` table migration is applied.

For now, an in-memory fallback implementation is provided so the memory
system works end-to-end without a database.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from backend.ai.memory.types import MemoryEntry, MemoryQuery, MemoryTier

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
        results.sort(key=lambda e: e.importance, reverse=True)
        return results[:query.limit]

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
