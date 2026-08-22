"""
Short Memory — per-request volatile context.

Short memory holds transient information that exists only for the duration
of a single request/turn. It is never persisted and never shared across
turns. When the request completes, short memory is discarded.

Use cases:
  - Storing intermediate tool results within a multi-step turn.
  - Caching the parsed user intent for the current turn.
  - Holding scratch-pad notes the AI makes during reasoning.

This tier is purely RAM-based. No database integration is needed.
"""
from __future__ import annotations

import logging
from typing import Any

from backend.ai.memory.types import MemoryEntry, MemoryTier, MemoryCategory

logger = logging.getLogger(__name__)


class ShortMemory:
    """Per-request volatile memory. Lives in RAM for one turn only."""

    __slots__ = ("_entries",)

    def __init__(self) -> None:
        self._entries: list[MemoryEntry] = []

    def add(self, entry: MemoryEntry) -> None:
        """Add a short-term memory entry."""
        if entry.tier != MemoryTier.SHORT:
            logger.warning("ShortMemory: rejecting non-short tier entry '%s'", entry.tier)
            return
        self._entries.append(entry)

    def add_simple(self, content: str, category: MemoryCategory = MemoryCategory.CONTEXT,
                   importance: float = 0.3, metadata: dict[str, Any] | None = None) -> MemoryEntry:
        """Convenience: create and store a short memory entry."""
        import uuid
        entry = MemoryEntry(
            id=str(uuid.uuid4()),
            owner_id=0,
            tier=MemoryTier.SHORT,
            category=category,
            content=content,
            importance=importance,
            metadata=metadata or {},
        )
        self._entries.append(entry)
        return entry

    def all(self) -> list[MemoryEntry]:
        """Return all short-term entries (newest last)."""
        return list(self._entries)

    def clear(self) -> None:
        """Discard all short-term entries."""
        self._entries.clear()

    def is_empty(self) -> bool:
        return len(self._entries) == 0

    def count(self) -> int:
        return len(self._entries)

    def as_text(self) -> str:
        """Render short memory as a text block for prompt injection.

        Fit to the prompt token budget (deterministic prefix, insertion
        order) so a long tool-heavy turn can never blow the memory budget.
        """
        from backend.ai.memory.limits import fit_entries_to_token_budget

        bounded = fit_entries_to_token_budget(list(self._entries))
        if not bounded:
            return ""
        lines = ["[Short Memory]"]
        for entry in bounded:
            lines.append(f"  - ({entry.category.value}) {entry.content}")
        return "\n".join(lines)
