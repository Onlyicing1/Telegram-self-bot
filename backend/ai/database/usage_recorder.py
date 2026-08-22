"""
Usage recorder — persists one normalized AI execution record exactly once.

Consumes the ``AIExecutionRecord`` produced by ``telemetry.record_execution``
(the single source of truth) and writes:

- one ``ai_usage`` row (per-request token/latency log), and
- one ``ai_provider_stats`` aggregate update per (provider, owner).

Token honesty is preserved verbatim: the row carries the record's
``token_source`` (actual / estimated / unavailable) and the exact
provider-reported input/output/total counts — nothing is ever invented.

The repository calls are synchronous (Supabase HTTP), so they run off the
event loop with a bounded timeout. Failures are logged and reported as
``False`` — the caller (dispatcher) schedules this via ``guarded_create_task``
and never lets persistence affect the AI response.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

_DB_TIMEOUT = 5.0


async def record_usage(
    record: Any,
    session_id: str = "",
    *,
    repos: Any | None = None,
) -> bool:
    """Persist ``record`` exactly once. Returns True on success.

    ``repos`` is injectable for tests; defaults to the process-wide
    ``RepositoryManager``. Never raises — failures are logged.
    """
    try:
        manager = repos if repos is not None else _repository_manager()
        await asyncio.wait_for(
            asyncio.to_thread(_write_sync, manager, record, session_id),
            timeout=_DB_TIMEOUT,
        )
        return True
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.warning("AI usage persistence failed: %r", exc)
        return False


def _repository_manager():
    from backend.ai.database.manager import get_repository_manager
    return get_repository_manager()


def _write_sync(manager: Any, record: Any, session_id: str) -> None:
    from backend.ai.database.usage_repository import UsageRecord

    timestamp = _parse_timestamp(getattr(record, "timestamp", ""))
    latency_ms = round(float(getattr(record, "latency", 0.0) or 0.0) * 1000, 3)

    usage = UsageRecord(
        id=str(uuid.uuid4()),
        owner_id=int(getattr(record, "owner_id", 0) or 0),
        session_id=session_id or "",
        provider=str(getattr(record, "provider", "") or ""),
        model=str(getattr(record, "model", "") or ""),
        prompt_tokens=int(getattr(record, "input_tokens", 0) or 0),
        completion_tokens=int(getattr(record, "output_tokens", 0) or 0),
        total_tokens=int(getattr(record, "total_tokens", 0) or 0),
        estimated_cost_usd=0.0,
        latency_ms=latency_ms,
        created_at=timestamp,
        metadata={
            "token_source": str(getattr(record, "token_source", "") or ""),
            "status": str(getattr(record, "status", "") or ""),
            "retry_count": int(getattr(record, "retry_count", 0) or 0),
            "fallback_used": bool(getattr(record, "fallback_used", False)),
        },
    )
    manager.usage.create(usage)

    provider = usage.provider
    if provider:
        manager.provider_stats.record_request(
            provider,
            usage.owner_id,
            success=(str(getattr(record, "status", "")) == "success"),
            prompt_tokens=usage.prompt_tokens,
            completion_tokens=usage.completion_tokens,
            latency_ms=latency_ms,
        )


def _parse_timestamp(iso_value: str) -> datetime:
    if not iso_value:
        return datetime.now(timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(iso_value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    except (TypeError, ValueError):
        return datetime.now(timezone.utc)
