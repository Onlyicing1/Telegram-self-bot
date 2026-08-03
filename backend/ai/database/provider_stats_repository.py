"""
ProviderStatsRepository — persistence interface for per-provider statistics.

Maps to the future ``ai_provider_stats`` table. Stores aggregate stats
per provider: total requests, successes, failures, average latency,
total tokens consumed.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class ProviderStatsRecord:
    """Aggregate stats for a single provider (maps to ``ai_provider_stats`` row)."""
    provider_name: str
    owner_id: int = 0
    total_requests: int = 0
    successful_requests: int = 0
    failed_requests: int = 0
    total_prompt_tokens: int = 0
    total_completion_tokens: int = 0
    avg_latency_ms: float = 0.0
    last_request_at: datetime | None = None
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider_name": self.provider_name,
            "owner_id": self.owner_id,
            "total_requests": self.total_requests,
            "successful_requests": self.successful_requests,
            "failed_requests": self.failed_requests,
            "total_prompt_tokens": self.total_prompt_tokens,
            "total_completion_tokens": self.total_completion_tokens,
            "avg_latency_ms": self.avg_latency_ms,
            "last_request_at": self.last_request_at.isoformat() if self.last_request_at else None,
            "updated_at": self.updated_at.isoformat(),
        }


class ProviderStatsRepository:
    """Abstract interface for provider stats persistence."""

    def get_or_create(self, provider_name: str, owner_id: int) -> ProviderStatsRecord:
        raise NotImplementedError

    def record_request(self, provider_name: str, owner_id: int, success: bool,
                       prompt_tokens: int, completion_tokens: int, latency_ms: float) -> bool:
        raise NotImplementedError

    def get(self, provider_name: str, owner_id: int) -> ProviderStatsRecord | None:
        raise NotImplementedError

    def list_all(self, owner_id: int) -> list[ProviderStatsRecord]:
        raise NotImplementedError


class InMemoryProviderStatsRepository(ProviderStatsRepository):
    """In-memory fallback for provider stats."""

    __slots__ = ("_stats",)

    def __init__(self) -> None:
        self._stats: dict[tuple[str, int], ProviderStatsRecord] = {}

    def get_or_create(self, provider_name: str, owner_id: int) -> ProviderStatsRecord:
        key = (provider_name, owner_id)
        if key not in self._stats:
            self._stats[key] = ProviderStatsRecord(provider_name=provider_name, owner_id=owner_id)
        return self._stats[key]

    def record_request(self, provider_name: str, owner_id: int, success: bool,
                       prompt_tokens: int, completion_tokens: int, latency_ms: float) -> bool:
        rec = self.get_or_create(provider_name, owner_id)
        rec.total_requests += 1
        if success:
            rec.successful_requests += 1
        else:
            rec.failed_requests += 1
        rec.total_prompt_tokens += prompt_tokens
        rec.total_completion_tokens += completion_tokens
        rec.avg_latency_ms = (
            (rec.avg_latency_ms * (rec.total_requests - 1) + latency_ms) / rec.total_requests
        )
        rec.last_request_at = datetime.now(timezone.utc)
        rec.updated_at = rec.last_request_at
        return True

    def get(self, provider_name: str, owner_id: int) -> ProviderStatsRecord | None:
        return self._stats.get((provider_name, owner_id))

    def list_all(self, owner_id: int) -> list[ProviderStatsRecord]:
        return [v for (pn, oid), v in self._stats.items() if oid == owner_id]
