"""
Memory type definitions — the data objects used across all memory tiers.

These types are shared by ShortMemory, LongMemory, and PermanentMemory.
Each memory tier stores and retrieves ``MemoryEntry`` objects, differing
only in persistence, retention, and injection strategy.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class MemoryTier(str, Enum):
    """The three memory tiers from AI_MASTER_DESIGN.md §5."""
    SHORT = "short"
    LONG = "long"
    PERMANENT = "permanent"


class MemoryCategory(str, Enum):
    """Semantic category of a memory entry."""
    FACT = "fact"
    PREFERENCE = "preference"
    CONTEXT = "context"
    SUMMARY = "summary"
    INSTRUCTION = "instruction"


@dataclass(frozen=True)
class MemoryEntry:
    """A single memory record, immutable once created.

    Attributes:
        id:            Unique identifier (UUID string or DB row ID).
        owner_id:      Telegram user ID of the bot owner.
        tier:          Which memory tier this entry lives in.
        category:      Semantic category for scoring and filtering.
        content:       The memory text.
        importance:    Score 0.0–1.0 (higher = more relevant).
        created_at:    UTC timestamp when the memory was created.
        expires_at:    Optional UTC timestamp when the memory should expire.
        metadata:      Arbitrary extra metadata (source, tags, etc.).
    """
    id: str
    owner_id: int
    tier: MemoryTier
    category: MemoryCategory
    content: str
    importance: float = 0.5
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    expires_at: datetime | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def is_expired(self, now: datetime | None = None) -> bool:
        """Check whether this memory has expired."""
        if self.expires_at is None:
            return False
        check = now or datetime.now(timezone.utc)
        return check >= self.expires_at

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "owner_id": self.owner_id,
            "tier": self.tier.value,
            "category": self.category.value,
            "content": self.content,
            "importance": self.importance,
            "created_at": self.created_at.isoformat(),
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class MemoryQuery:
    """Parameters for querying memory.

    Attributes:
        owner_id:     Filter by owner.
        tier:         Optional tier filter (None = all tiers).
        category:     Optional category filter.
        query_text:   Text to match against content (simple substring).
        limit:        Max results to return.
        min_importance: Only entries with importance >= this value.
    """
    owner_id: int
    tier: MemoryTier | None = None
    category: MemoryCategory | None = None
    query_text: str = ""
    limit: int = 20
    min_importance: float = 0.0
