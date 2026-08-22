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


def _get_db():
    from backend.db.client import get_db
    return get_db()


def _row_to_usage(row: dict) -> UsageRecord | None:
    try:
        created = datetime.fromisoformat(str(row.get("created_at") or "").replace("Z", "+00:00"))
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        created = datetime.now(timezone.utc)
    try:
        return UsageRecord(
            id=str(row.get("id") or ""),
            owner_id=int(row.get("owner_id") or 0),
            session_id=str(row.get("session_id") or ""),
            provider=str(row.get("provider") or ""),
            model=str(row.get("model") or ""),
            prompt_tokens=int(row.get("prompt_tokens") or 0),
            completion_tokens=int(row.get("completion_tokens") or 0),
            total_tokens=int(row.get("total_tokens") or 0),
            estimated_cost_usd=0.0,
            latency_ms=float(row.get("latency_ms") or 0.0),
            created_at=created,
            metadata={"token_source": str(row.get("token_source") or "")},
        )
    except (TypeError, ValueError):
        return None


class SupabaseUsageRepository(UsageRepository):
    """Supabase-backed usage repository against the ``ai_usage`` table.

    Sync by design — the usage recorder runs these methods off the event
    loop via ``asyncio.to_thread``. Every method degrades to a safe result
    and logs on failure so usage persistence can never break AI execution.
    When Supabase is unavailable (``get_db()`` returns None) the methods
    return empty results without raising.
    """

    __slots__ = ()

    def create(self, record: UsageRecord) -> bool:
        db = _get_db()
        if not db:
            return False
        try:
            db.table("ai_usage").insert({
                "owner_id": record.owner_id,
                "session_id": record.session_id or "",
                "provider": record.provider,
                "model": record.model,
                "prompt_tokens": record.prompt_tokens,
                "completion_tokens": record.completion_tokens,
                "total_tokens": record.total_tokens,
                "latency_ms": record.latency_ms,
                "token_source": (record.metadata or {}).get("token_source", ""),
                "created_at": record.created_at.isoformat(),
            }).execute()
            return True
        except Exception as exc:
            logger.warning("AI usage insert failed: %s", exc)
            return False

    def total_tokens(self, owner_id: int) -> int:
        db = _get_db()
        if not db:
            return 0
        try:
            result = (
                db.table("ai_usage")
                .select("total_tokens")
                .eq("owner_id", owner_id)
                .limit(10000)
                .execute()
            )
            return sum(int(row.get("total_tokens") or 0) for row in (result.data or []))
        except Exception as exc:
            logger.warning("AI usage total_tokens failed: %s", exc)
            return 0

    def daily_tokens(self, owner_id: int, date: datetime) -> int:
        db = _get_db()
        if not db:
            return 0
        try:
            day_start = date.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
            day_end = date.replace(hour=23, minute=59, second=59, microsecond=999999).isoformat()
            result = (
                db.table("ai_usage")
                .select("total_tokens")
                .eq("owner_id", owner_id)
                .gte("created_at", day_start)
                .lte("created_at", day_end)
                .limit(10000)
                .execute()
            )
            return sum(int(row.get("total_tokens") or 0) for row in (result.data or []))
        except Exception as exc:
            logger.warning("AI usage daily_tokens failed: %s", exc)
            return 0

    def recent(self, owner_id: int, limit: int = 50) -> list[UsageRecord]:
        db = _get_db()
        if not db:
            return []
        try:
            result = (
                db.table("ai_usage")
                .select("*")
                .eq("owner_id", owner_id)
                .order("created_at", desc=True)
                .limit(limit)
                .execute()
            )
            return [r for r in (_row_to_usage(row) for row in (result.data or [])) if r is not None]
        except Exception as exc:
            logger.warning("AI usage recent failed: %s", exc)
            return []
