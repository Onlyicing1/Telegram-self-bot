"""
MemoryManager — the unified entry point for all three memory tiers.

The MemoryManager owns ShortMemory, LongMemory, and PermanentMemory
instances and provides a single API for the Engine and Prompt Builder
to interact with memory.

Architecture (AI_MASTER_DESIGN.md §5):
    - ShortMemory:     per-request, RAM-only, cleared after each turn.
    - LongMemory:      cross-session, 90-day retention, repository-backed.
    - PermanentMemory: always in prompt, never expires, repository-backed.

The manager is constructed once and injected into the Engine. It never
touches the database directly — repositories are injected for Long and
Permanent tiers. When no repositories are provided, in-memory fallbacks
are used (data is lost on restart, matching the existing DB client pattern).

Public API:
    short          → ShortMemory instance (per-request)
    long           → LongMemory instance (cross-session)
    permanent      → PermanentMemory instance (always-in-prompt)
    retrieve_for_prompt(owner_id) → dict with "permanent" and "long" text blocks
    new_turn()     → clear short memory for a new request
    store_long(...)         → convenience proxy
    store_permanent(...)    → convenience proxy
"""
from __future__ import annotations

import logging
from typing import Any

from backend.ai.memory.long import LongMemory
from backend.ai.memory.permanent import PermanentMemory
from backend.ai.memory.short import ShortMemory
from backend.ai.memory.types import MemoryCategory, MemoryQuery, MemoryTier

logger = logging.getLogger(__name__)


class MemoryManager:
    """Unified manager for all three memory tiers.

    Constructed once with optional repositories for long and permanent
tiers. The Engine receives this manager and calls ``retrieve_for_prompt()``
    to get memory text blocks for the Prompt Builder.
    """

    __slots__ = ("_short", "_long", "_permanent")

    def __init__(
        self,
        long_repository: Any | None = None,
        permanent_repository: Any | None = None,
        retention_days: int = 90,
    ) -> None:
        self._short = ShortMemory()
        self._long = LongMemory(repository=long_repository, retention_days=retention_days)
        self._permanent = PermanentMemory(repository=permanent_repository)

    @property
    def short(self) -> ShortMemory:
        return self._short

    @property
    def long(self) -> LongMemory:
        return self._long

    @property
    def permanent(self) -> PermanentMemory:
        return self._permanent

    def new_turn(self) -> None:
        """Clear short memory at the start of a new request/turn."""
        self._short.clear()

    def retrieve_for_prompt(self, owner_id: int, query_text: str = "") -> dict[str, str]:
        """Retrieve memory text blocks for prompt injection.

        Returns a dict with:
            "permanent":  Always-in-prompt facts text block.
            "long":       Relevant long-term memories text block.
            "short":      Current short-term memory text block.
        """
        permanent_text = self._permanent.as_text(owner_id)

        long_entries = self._long.retrieve(MemoryQuery(
            owner_id=owner_id,
            tier=MemoryTier.LONG,
            query_text=query_text,
            limit=10,
            min_importance=0.3,
        ))
        long_text = self._long.as_text(long_entries)

        short_text = self._short.as_text()

        return {
            "permanent": permanent_text,
            "long": long_text,
            "short": short_text,
        }

    def store_long(self, owner_id: int, content: str, category: MemoryCategory = MemoryCategory.SUMMARY,
                   importance: float = 0.5, metadata: dict[str, Any] | None = None) -> Any:
        """Convenience proxy for LongMemory.store()."""
        return self._long.store(owner_id, content, category, importance, metadata)

    def store_permanent(self, owner_id: int, content: str, category: MemoryCategory = MemoryCategory.FACT,
                        importance: float = 1.0, metadata: dict[str, Any] | None = None) -> Any:
        """Convenience proxy for PermanentMemory.store()."""
        return self._permanent.store(owner_id, content, category, importance, metadata)

    def status(self) -> dict[str, Any]:
        """Return a status snapshot for diagnostics."""
        return {
            "short_count": self._short.count(),
            "long_available": self._long._repository is not None,
            "permanent_available": self._permanent._repository is not None,
        }
