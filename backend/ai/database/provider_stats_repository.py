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


def _get_db():
    from backend.db.client import get_db
    return get_db()


def _row_to_stats(row: dict) -> ProviderStatsRecord | None:
    try:
        return ProviderStatsRecord(
            provider_name=str(row.get("provider_name") or ""),
            owner_id=int(row.get("owner_id") or 0),
            total_requests=int(row.get("total_requests") or 0),
            successful_requests=int(row.get("successful_requests") or 0),
            failed_requests=int(row.get("failed_requests") or 0),
            total_prompt_tokens=int(row.get("total_prompt_tokens") or 0),
            total_completion_tokens=int(row.get("total_completion_tokens") or 0),
            avg_latency_ms=float(row.get("avg_latency_ms") or 0.0),
            last_request_at=_parse_dt(row.get("last_request_at")),
            updated_at=_parse_dt(row.get("updated_at")) or datetime.now(timezone.utc),
        )
    except (TypeError, ValueError):
        return None


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


class SupabaseProviderStatsRepository(ProviderStatsRepository):
    """Supabase-backed provider stats against the ``ai_provider_stats`` table.

    Sync by design — the usage recorder runs these methods off the event
    loop via ``asyncio.to_thread``. ``record_request`` is a read-modify-write
    upsert (safe for this single-owner self-bot). Every method degrades to
    a safe result and logs on failure so persistence can never break AI
    execution. When Supabase is unavailable (``get_db()`` returns None) the
    methods return empty results without raising.
    """

    __slots__ = ()

    def get_or_create(self, provider_name: str, owner_id: int) -> ProviderStatsRecord:
        found = self.get(provider_name, owner_id)
        if found is not None:
            return found
        return ProviderStatsRecord(provider_name=provider_name, owner_id=owner_id)

    def record_request(self, provider_name: str, owner_id: int, success: bool,
                       prompt_tokens: int, completion_tokens: int, latency_ms: float) -> bool:
        db = _get_db()
        if not db:
            return False
        try:
            existing = self.get(provider_name, owner_id)
            now = datetime.now(timezone.utc)
            if existing is None:
                existing = ProviderStatsRecord(
                    provider_name=provider_name, owner_id=owner_id,
                )
            existing.total_requests += 1
            if success:
                existing.successful_requests += 1
            else:
                existing.failed_requests += 1
            existing.total_prompt_tokens += int(prompt_tokens or 0)
            existing.total_completion_tokens += int(completion_tokens or 0)
            existing.avg_latency_ms = (
                (existing.avg_latency_ms * (existing.total_requests - 1) + float(latency_ms))
                / existing.total_requests
            )
            existing.last_request_at = now
            existing.updated_at = now
            db.table("ai_provider_stats").upsert(existing.as_dict(), on_conflict="provider_name,owner_id").execute()
            return True
        except Exception as exc:
            logger.warning("AI provider_stats record_request failed: %s", exc)
            return False

    def get(self, provider_name: str, owner_id: int) -> ProviderStatsRecord | None:
        db = _get_db()
        if not db:
            return None
        try:
            result = (
                db.table("ai_provider_stats")
                .select("*")
                .eq("provider_name", provider_name)
                .eq("owner_id", owner_id)
                .maybe_single()
                .execute()
            )
            return _row_to_stats(result.data) if result and result.data else None
        except Exception as exc:
            logger.warning("AI provider_stats get failed: %s", exc)
            return None

    def list_all(self, owner_id: int) -> list[ProviderStatsRecord]:
        db = _get_db()
        if not db:
            return []
        try:
            result = (
                db.table("ai_provider_stats")
                .select("*")
                .eq("owner_id", owner_id)
                .execute()
            )
            return [r for r in (_row_to_stats(row) for row in (result.data or [])) if r is not None]
        except Exception as exc:
            logger.warning("AI provider_stats list_all failed: %s", exc)
            return []
