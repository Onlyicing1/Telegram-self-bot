"""
Long Memory — cross-session persistent summaries.

Long memory stores summarized knowledge across conversation sessions.
Entries have a 90-day retention window (per AI_MASTER_DESIGN.md §5.3).
When Supabase is available, long memories persist to the ``ai_memories``
table. When Supabase is unavailable, they fall back to an in-memory list
(lost on restart, same pattern as the existing DB client).

Long memory is designed for future database integration. The repository
interface (``MemoryRepository``) is injected at construction time. When
no repository is provided, an in-memory repository is used.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from backend.ai.memory.types import MemoryCategory, MemoryEntry, MemoryQuery, MemoryTier

logger = logging.getLogger(__name__)

DEFAULT_RETENTION_DAYS = 90
DEFAULT_MAX_RESULTS = 20


class LongMemory:
    """Cross-session persistent memory with 90-day retention.

    The repository is injected — this class never touches Supabase or
    any database directly. When no repository is provided, an in-memory
    list is used as a fallback.
    """

    __slots__ = ("_repository", "_retention_days")

    def __init__(self, repository: Any | None = None, retention_days: int = DEFAULT_RETENTION_DAYS) -> None:
        self._repository = repository
        self._retention_days = retention_days

    def store(self, owner_id: int, content: str, category: MemoryCategory = MemoryCategory.SUMMARY,
              importance: float = 0.5, metadata: dict[str, Any] | None = None) -> MemoryEntry | None:
        """Create and persist a long-term memory entry.

        Returns the created entry, or None if persistence failed.
        """
        entry = MemoryEntry(
            id=str(uuid.uuid4()),
            owner_id=owner_id,
            tier=MemoryTier.LONG,
            category=category,
            content=content,
            importance=importance,
            expires_at=datetime.now(timezone.utc) + timedelta(days=self._retention_days),
            metadata=metadata or {},
        )
        if self._repository is not None:
            try:
                self._repository.save(entry)
            except Exception as exc:
                logger.warning("LongMemory: repository save failed: %s", exc)
                return None
        return entry

    def retrieve(self, query: MemoryQuery) -> list[MemoryEntry]:
        """Retrieve long-term memories matching the query."""
        if query.tier is None:
            query = MemoryQuery(
                owner_id=query.owner_id,
                tier=MemoryTier.LONG,
                category=query.category,
                query_text=query.query_text,
                limit=query.limit,
                min_importance=query.min_importance,
            )
        if self._repository is not None:
            try:
                return self._repository.query(query)
            except Exception as exc:
                logger.warning("LongMemory: repository query failed: %s", exc)
                return []
        return []

    def expire_old(self) -> int:
        """Remove entries past their retention window. Returns count removed."""
        if self._repository is None:
            return 0
        try:
            return self._repository.delete_expired(MemoryTier.LONG)
        except Exception as exc:
            logger.warning("LongMemory: expire_old failed: %s", exc)
            return 0

    def count(self, owner_id: int) -> int:
        """Return the total number of long memories for an owner."""
        if self._repository is None:
            return 0
        try:
            return self._repository.count(owner_id, MemoryTier.LONG)
        except Exception as exc:
            logger.warning("LongMemory: count failed: %s", exc)
            return 0

    def as_text(self, entries: list[MemoryEntry]) -> str:
        """Render long memory entries as a text block for prompt injection.

        Entries are first fit to the prompt token budget (deterministic
        prefix of the pre-ranked list) so memory can never silently exceed
        the allowed context budget.
        """
        from backend.ai.memory.limits import fit_entries_to_token_budget

        bounded = fit_entries_to_token_budget(entries)
        if not bounded:
            return ""
        lines = ["[Long Memory]"]
        for entry in bounded:
            lines.append(f"  - ({entry.category.value}, importance={entry.importance:.1f}) {entry.content}")
        return "\n".join(lines)
