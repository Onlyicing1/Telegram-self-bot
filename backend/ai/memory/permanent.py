"""
Permanent Memory — always-in-prompt facts confirmed by the owner.

Permanent memory holds critical, unchanging facts that the AI must always
know: the owner's name, preferences, core instructions, identity facts.
These entries are never expired and always injected into the prompt
(per AI_MASTER_DESIGN.md §5.4).

Permanent memories should be kept under 500 tokens total to avoid
consuming too much of the prompt budget. The manager enforces a soft
cap and logs a warning when exceeded.

Like LongMemory, the repository is injected. When no repository is
provided, an in-memory list is used.
"""
from __future__ import annotations

import logging
import uuid
from typing import Any

from backend.ai.memory.types import MemoryCategory, MemoryEntry, MemoryQuery, MemoryTier
from backend.ai.prompt.budget import estimate_tokens

logger = logging.getLogger(__name__)

SOFT_TOKEN_CAP = 500


class PermanentMemory:
    """Always-in-prompt memory for critical, owner-confirmed facts.

    Entries are never expired. The total token footprint is kept under
    a soft cap (500 tokens). When the cap is exceeded, a warning is
    logged — the caller is responsible for pruning.
    """

    __slots__ = ("_repository",)

    def __init__(self, repository: Any | None = None) -> None:
        self._repository = repository

    def store(self, owner_id: int, content: str, category: MemoryCategory = MemoryCategory.FACT,
              importance: float = 1.0, metadata: dict[str, Any] | None = None) -> MemoryEntry | None:
        """Create and persist a permanent memory entry.

        Permanent entries have no ``expires_at`` — they never expire.
        Returns the created entry, or None if persistence failed.
        """
        entry = MemoryEntry(
            id=str(uuid.uuid4()),
            owner_id=owner_id,
            tier=MemoryTier.PERMANENT,
            category=category,
            content=content,
            importance=importance,
            expires_at=None,
            metadata=metadata or {},
        )
        if self._repository is not None:
            try:
                self._repository.save(entry)
            except Exception as exc:
                logger.warning("PermanentMemory: repository save failed: %s", exc)
                return None
        return entry

    def retrieve_all(self, owner_id: int) -> list[MemoryEntry]:
        """Retrieve all permanent memories for an owner."""
        query = MemoryQuery(
            owner_id=owner_id,
            tier=MemoryTier.PERMANENT,
            limit=100,
        )
        if self._repository is not None:
            try:
                return self._repository.query(query)
            except Exception as exc:
                logger.warning("PermanentMemory: repository query failed: %s", exc)
                return []
        return []

    def remove(self, entry_id: str) -> bool:
        """Remove a permanent memory by ID."""
        if self._repository is None:
            return False
        try:
            return self._repository.delete(entry_id)
        except Exception as exc:
            logger.warning("PermanentMemory: remove failed: %s", exc)
            return False

    def count(self, owner_id: int) -> int:
        """Return the total number of permanent memories for an owner."""
        if self._repository is None:
            return 0
        try:
            return self._repository.count(owner_id, MemoryTier.PERMANENT)
        except Exception as exc:
            logger.warning("PermanentMemory: count failed: %s", exc)
            return 0

    def token_footprint(self, owner_id: int) -> int:
        """Estimate the total token count of all permanent memories."""
        entries = self.retrieve_all(owner_id)
        total = sum(estimate_tokens(e.content) for e in entries)
        if total > SOFT_TOKEN_CAP:
            logger.warning(
                "PermanentMemory: token footprint %d exceeds soft cap %d",
                total, SOFT_TOKEN_CAP,
            )
        return total

    def as_text(self, owner_id: int) -> str:
        """Render permanent memories as a text block for prompt injection.

        Entries are fit to the prompt token budget (deterministic prefix of
        the importance/recency-ranked list) so memory can never silently
        exceed the allowed context budget.
        """
        from backend.ai.memory.limits import fit_entries_to_token_budget

        entries = fit_entries_to_token_budget(self.retrieve_all(owner_id))
        if not entries:
            return ""
        lines = ["[Permanent Memory]"]
        for entry in entries:
            lines.append(f"  - ({entry.category.value}) {entry.content}")
        return "\n".join(lines)
