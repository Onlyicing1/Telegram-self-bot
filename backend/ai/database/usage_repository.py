"""
UsageRepository — persistence interface for AI usage tracking.

Maps to the future ``ai_usage`` table. Stores per-request usage records
for billing, rate limiting, and analytics: tokens consumed, cost estimate,
provider, model, timestamp.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class UsageRecord:
    """A single usage record (maps to ``ai_usage`` row)."""
    id: str
    owner_id: int
    session_id: str = ""
    provider: str = ""
    model: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    estimated_cost_usd: float = 0.0
    latency_ms: float = 0.0
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "owner_id": self.owner_id,
            "session_id": self.session_id,
            "provider": self.provider,
            "model": self.model,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "estimated_cost_usd": self.estimated_cost_usd,
            "latency_ms": self.latency_ms,
            "created_at": self.created_at.isoformat(),
            "metadata": dict(self.metadata),
        }


class UsageRepository:
    """Abstract interface for usage tracking."""

    def create(self, record: UsageRecord) -> bool:
        raise NotImplementedError

    def total_tokens(self, owner_id: int) -> int:
        raise NotImplementedError

    def daily_tokens(self, owner_id: int, date: datetime) -> int:
        raise NotImplementedError

    def recent(self, owner_id: int, limit: int = 50) -> list[UsageRecord]:
        raise NotImplementedError


class InMemoryUsageRepository(UsageRepository):
    """In-memory fallback for usage tracking."""

    __slots__ = ("_records",)

    def __init__(self) -> None:
        self._records: list[UsageRecord] = []

    def create(self, record: UsageRecord) -> bool:
        self._records.append(record)
        return True

    def total_tokens(self, owner_id: int) -> int:
        return sum(r.total_tokens for r in self._records if r.owner_id == owner_id)

    def daily_tokens(self, owner_id: int, date: datetime) -> int:
        day_start = date.replace(hour=0, minute=0, second=0, microsecond=0)
        day_end = date.replace(hour=23, minute=59, second=59, microsecond=999999)
        return sum(
            r.total_tokens for r in self._records
            if r.owner_id == owner_id and day_start <= r.created_at <= day_end
        )

    def recent(self, owner_id: int, limit: int = 50) -> list[UsageRecord]:
        results = [r for r in self._records if r.owner_id == owner_id]
        results.sort(key=lambda r: r.created_at, reverse=True)
        return results[:limit]
