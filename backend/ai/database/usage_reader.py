"""
Usage reader — async read-side access to persisted AI usage data.

Consumes ONLY the repository abstractions (``UsageRepository``,
``ProviderStatsRepository``) through ``RepositoryManager`` — never
Supabase directly. Sync repository calls (Supabase HTTP) run off the
event loop with a bounded timeout. Every failure degrades to a safe
empty result and logs, so observability can never affect AI execution.

Token honesty is preserved in aggregation: ``UsageSummary`` splits
total tokens by ``token_source`` (actual / estimated / unavailable) so
a mixed-source sum never masquerades as a single exact number.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

_READ_TIMEOUT = 3.0


@dataclass
class TokenSourceBreakdown:
    """Total tokens split by honesty label — never merged silently."""

    actual: int = 0
    estimated: int = 0
    unavailable: int = 0

    @property
    def total(self) -> int:
        return self.actual + self.estimated + self.unavailable

    @property
    def sources(self) -> list[str]:
        out = []
        if self.actual:
            out.append("actual")
        if self.estimated:
            out.append("estimated")
        if self.unavailable:
            out.append("unavailable")
        return out


@dataclass
class UsageSummary:
    """Windowed aggregate over persisted ``ai_usage`` records."""

    requests: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    token_source: TokenSourceBreakdown = field(default_factory=TokenSourceBreakdown)
    sources: tuple[str, ...] = ()
    available: bool = False


_TOKEN_SOURCE_ORDER = ("actual", "estimated", "unavailable")


def _repository_manager() -> Any:
    from backend.ai.database.manager import get_repository_manager
    return get_repository_manager()


async def _run(fn, *args) -> Any:
    """Run a sync repository call off the loop with a bounded timeout.

    Returns ``None`` when the call failed or timed out (the safe
    degradation signal), so callers can distinguish failure from an
    empty result.
    """
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(fn, _repository_manager(), *args),
            timeout=_READ_TIMEOUT,
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.warning("AI usage read failed: %r", exc)
        return None


async def total_tokens(owner_id: int) -> int:
    """All-time persisted token total for the owner (0 when unavailable)."""
    value = await _run(lambda m: m.usage.total_tokens(owner_id))
    return int(value or 0)


async def daily_tokens(owner_id: int, date: datetime | None = None) -> int:
    """Persisted token total for a UTC calendar day (default: today)."""
    date = date or datetime.now(timezone.utc)
    value = await _run(lambda m: m.usage.daily_tokens(owner_id, date))
    return int(value or 0)


async def recent(owner_id: int, limit: int = 10) -> list[Any]:
    """Most recent persisted usage records (newest first; [] on failure)."""
    value = await _run(lambda m: m.usage.recent(owner_id, limit))
    return list(value or [])


async def provider_stats(owner_id: int) -> list[Any]:
    """Persisted per-provider aggregates for the owner (all-time)."""
    value = await _run(lambda m: m.provider_stats.list_all(owner_id))
    return list(value or [])


def _token_source_of(record: Any) -> str:
    meta = getattr(record, "metadata", None) or {}
    return str(meta.get("token_source", "") or "")


async def summary(
    owner_id: int,
    *,
    since: datetime | None = None,
    limit: int = 200,
) -> UsageSummary:
    """Windowed persisted usage aggregate with token-source honesty.

    Reads recent records once (bounded), filters by ``since``, and sums
    tokens per source. ``available`` is True only when the read actually
    succeeded — a failure returns an all-zero summary rather than
    raising, so callers can distinguish "no data" from "read failed".
    ``sources`` lists every token_source label actually seen (even at
    zero counts), so "unavailable" is never lost from aggregation.
    """
    raw = await _run(lambda m: m.usage.recent(owner_id, limit))
    if raw is None:
        return UsageSummary()

    breakdown = TokenSourceBreakdown()
    seen: set[str] = set()
    input_tokens = 0
    output_tokens = 0
    requests = 0
    for record in raw:
        created = getattr(record, "created_at", None)
        if since is not None and (created is None or created < since):
            continue
        source = _token_source_of(record)
        seen.add(source if source in _TOKEN_SOURCE_ORDER else "unavailable")
        total = int(getattr(record, "total_tokens", 0) or 0)
        if source == "actual":
            breakdown.actual += total
        elif source == "estimated":
            breakdown.estimated += total
        elif source == "unavailable":
            breakdown.unavailable += total
        else:
            # Unknown source label — count it as unlabeled, never actual.
            breakdown.unavailable += total
        input_tokens += int(getattr(record, "prompt_tokens", 0) or 0)
        output_tokens += int(getattr(record, "completion_tokens", 0) or 0)
        requests += 1

    return UsageSummary(
        requests=requests,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=breakdown.total,
        token_source=breakdown,
        sources=tuple(s for s in _TOKEN_SOURCE_ORDER if s in seen),
        available=True,
    )
