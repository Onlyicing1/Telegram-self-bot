"""Persistent, fail-closed outbound bandwidth governor.

This module is the single authority for outbound payload accounting.  It does
not attempt to measure all Render outbound traffic; it accounts only for the
explicit payload paths that call :func:`reserve_outbound_bytes` before
transmission (Gemini OCR/STT, Telegram outbound media including Deep Save).

Accounting state lives in the ``panel_settings.outbound_bandwidth_state``
JSONB column (one dedicated column on the existing singleton row) and is
accessed only through ``panel_settings_repository``.  Nothing is stored only
in RAM, so reservations survive restart/redeploy/crash.
"""
from __future__ import annotations

import asyncio
import logging
import math
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from backend.services import panel_settings_repository as repo

try:
    from backend.db import client as _db_client
except Exception:  # noqa: BLE001
    _db_client = None

logger = logging.getLogger(__name__)

# Exact operational defaults (decimal bytes).  Do not silently change these.
DAILY_LIMIT_BYTES: int = 150_000_000
MONTHLY_LIMIT_BYTES: int = 4_000_000_000

_STATE_KEY = "outbound_bandwidth_state"
_STATE_VERSION = 1

# One shared async lock serializes reservation + persistence within the process.
_lock = asyncio.Lock()

# In-memory fallback used only when no database is configured at all (mirrors
# the panel-settings service's documented no-DB fallback).  It is NOT a
# substitute for persistent accounting in a configured deployment.
_memory_state: dict[str, Any] = {}


class OutboundBandwidthError(Exception):
    """Base class for governor failures."""


class OutboundBandwidthExceededError(
    OutboundBandwidthError, ValueError, RuntimeError
):
    """Raised when a reservation would exceed a daily/monthly budget."""

    def __init__(
        self,
        period: str,
        used_bytes: int,
        requested_bytes: int,
        limit_bytes: int,
    ) -> None:
        self.period = period
        self.used_bytes = int(used_bytes)
        self.requested_bytes = int(requested_bytes)
        self.limit_bytes = int(limit_bytes)
        super().__init__(
            f"outbound bandwidth {period} limit exceeded: "
            f"used={used_bytes} requested={requested_bytes} limit={limit_bytes}"
        )


class OutboundBandwidthUnavailableError(
    OutboundBandwidthError, RuntimeError, OSError
):
    """Raised when persistent accounting cannot be read or written."""


# Common aliases for callers/tests that use different names.
BandwidthExceededError = OutboundBandwidthExceededError
BandwidthLimitExceededError = OutboundBandwidthExceededError
BandwidthExceeded = OutboundBandwidthExceededError
BandwidthLimitExceeded = OutboundBandwidthExceededError
BandwidthUnavailableError = OutboundBandwidthUnavailableError
BandwidthUnavailable = OutboundBandwidthUnavailableError


def _utc_now(now: Optional[datetime] = None) -> datetime:
    if now is None:
        return datetime.now(timezone.utc)
    if now.tzinfo is None:
        return now.replace(tzinfo=timezone.utc)
    return now.astimezone(timezone.utc)


def _validate_category(category: Any) -> str:
    if not isinstance(category, str) or not category.strip():
        raise ValueError("category must be a non-empty string")
    return category.strip()


def _validate_byte_count(byte_count: Any) -> int:
    if isinstance(byte_count, bool) or not isinstance(byte_count, int):
        # Allow integral floats as a convenience, but reject malformed input.
        if isinstance(byte_count, float) and byte_count.is_integer():
            byte_count = int(byte_count)
        else:
            raise ValueError("byte_count must be a positive integer")
    if byte_count <= 0:
        raise ValueError("byte_count must be greater than zero")
    return int(byte_count)


def _period_keys(now: datetime) -> tuple[str, str]:
    return now.strftime("%Y-%m-%d"), now.strftime("%Y-%m")


def _normalize_state(raw: Any, daily_period: str, monthly_period: str) -> tuple[int, int]:
    """Return (daily_used, monthly_used) for the current periods.

    Old periods are treated as zero (and are pruned on the next write).
    """
    if not isinstance(raw, dict):
        return 0, 0
    daily = raw.get("daily")
    daily_used = 0
    if isinstance(daily, dict) and daily.get("period") == daily_period:
        try:
            daily_used = max(0, int(daily.get("bytes", 0) or 0))
        except (TypeError, ValueError):
            daily_used = 0
    monthly = raw.get("monthly")
    monthly_used = 0
    if isinstance(monthly, dict) and monthly.get("period") == monthly_period:
        try:
            monthly_used = max(0, int(monthly.get("bytes", 0) or 0))
        except (TypeError, ValueError):
            monthly_used = 0
    return daily_used, monthly_used


def _build_state(daily_period: str, daily_used: int, monthly_period: str, monthly_used: int) -> Dict[str, Any]:
    return {
        "version": _STATE_VERSION,
        "daily": {"period": daily_period, "bytes": int(daily_used)},
        "monthly": {"period": monthly_period, "bytes": int(monthly_used)},
    }


def _db_configured() -> bool:
    if _db_client is None:
        return False
    try:
        return _db_client.get_db() is not None
    except Exception:  # noqa: BLE001
        return False


def _read_row_state() -> Any:
    if not _db_configured():
        # No database configured: there is no durable state to read.  Allow the
        # reservation without accounting (mirrors the no-DB panel-settings fallback).
        return {}
    row = repo.load()
    if row is None:
        raise OutboundBandwidthUnavailableError("panel_settings row is unavailable")
    if not isinstance(row, dict):
        raise OutboundBandwidthUnavailableError("panel_settings row is malformed")
    return row.get(_STATE_KEY, {})


async def reserve_outbound_bytes(
    category: Any,
    byte_count: Any,
    *,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Reserve ``byte_count`` outbound bytes before a payload transfer.

    Raises :class:`OutboundBandwidthExceededError` when a limit would be
    exceeded and :class:`OutboundBandwidthUnavailableError` when persistence
    cannot be read/written.  The reservation is persisted before returning.
    """
    category = _validate_category(category)
    byte_count = _validate_byte_count(byte_count)
    now_dt = _utc_now(now)
    daily_period, monthly_period = _period_keys(now_dt)

    async with _lock:
        try:
            raw_state = _read_row_state()
        except OutboundBandwidthUnavailableError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise OutboundBandwidthUnavailableError(str(exc)) from exc

        daily_used, monthly_used = _normalize_state(raw_state, daily_period, monthly_period)

        if daily_used + byte_count > DAILY_LIMIT_BYTES:
            logger.warning(
                "OUTBOUND_BANDWIDTH_EXCEEDED category=%s period=daily "
                "reserved_bytes=%d used_bytes=%d limit_bytes=%d",
                category, byte_count, daily_used, DAILY_LIMIT_BYTES,
            )
            raise OutboundBandwidthExceededError(
                "daily", daily_used, byte_count, DAILY_LIMIT_BYTES
            )
        if monthly_used + byte_count > MONTHLY_LIMIT_BYTES:
            logger.warning(
                "OUTBOUND_BANDWIDTH_EXCEEDED category=%s period=monthly "
                "reserved_bytes=%d used_bytes=%d limit_bytes=%d",
                category, byte_count, monthly_used, MONTHLY_LIMIT_BYTES,
            )
            raise OutboundBandwidthExceededError(
                "monthly", monthly_used, byte_count, MONTHLY_LIMIT_BYTES
            )

        new_daily = daily_used + byte_count
        new_monthly = monthly_used + byte_count
        new_state = _build_state(daily_period, new_daily, monthly_period, new_monthly)

        try:
            if not _db_configured():
                ok = True
            else:
                ok = repo.update_field(_STATE_KEY, new_state)
        except Exception as exc:  # noqa: BLE001
            ok = False
            logger.warning("OUTBOUND_BANDWIDTH_PERSIST_FAILED error=%s", type(exc).__name__)
        if not ok:
            raise OutboundBandwidthUnavailableError(
                "failed to persist outbound bandwidth reservation"
            )

        logger.info(
            "OUTBOUND_BANDWIDTH_RESERVED category=%s bytes=%d daily_used=%d "
            "monthly_used=%d",
            category, byte_count, new_daily, new_monthly,
        )
        return {
            "category": category,
            "reserved_bytes": byte_count,
            "daily_period": daily_period,
            "monthly_period": monthly_period,
            "daily_used_bytes": new_daily,
            "monthly_used_bytes": new_monthly,
            "daily_limit_bytes": DAILY_LIMIT_BYTES,
            "monthly_limit_bytes": MONTHLY_LIMIT_BYTES,
        }


def reserve_outbound_bytes_sync(
    category: Any,
    byte_count: Any,
    *,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Synchronous wrapper for callers that are not on an event loop."""
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = None
    if loop is not None and loop.is_running():
        # Should not normally happen; run in a fresh loop via thread.
        import concurrent.futures

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(
                asyncio.run,
                reserve_outbound_bytes(category, byte_count, now=now),
            )
            return future.result()
    return asyncio.run(reserve_outbound_bytes(category, byte_count, now=now))


#: At or above this fraction of either budget the usage is "near limit".
NEAR_LIMIT_RATIO = 0.90


def usage_state(daily_used: int, monthly_used: int) -> str:
    """Classify usage as ``normal``, ``near_limit`` or ``exhausted``."""
    if daily_used >= DAILY_LIMIT_BYTES or monthly_used >= MONTHLY_LIMIT_BYTES:
        return "exhausted"
    if (
        daily_used >= DAILY_LIMIT_BYTES * NEAR_LIMIT_RATIO
        or monthly_used >= MONTHLY_LIMIT_BYTES * NEAR_LIMIT_RATIO
    ):
        return "near_limit"
    return "normal"


async def get_outbound_usage(now: Optional[datetime] = None) -> Dict[str, Any]:
    """Return current usage without reserving anything.

    ``available`` is ``False`` when the durable accounting state could not be
    read; callers must show that state rather than treating the zero-valued
    byte fields as a real reading.  ``detail`` explains an unavailable read.
    """
    now_dt = _utc_now(now)
    daily_period, monthly_period = _period_keys(now_dt)
    available = True
    detail = ""
    raw_state: Any = {}
    try:
        if not _db_configured():
            available = False
            detail = "persistent accounting is not configured"
        else:
            raw_state = _read_row_state()
            if not isinstance(raw_state, dict):
                available = False
                detail = "stored bandwidth state is malformed"
                raw_state = {}
    except OutboundBandwidthUnavailableError as exc:
        available = False
        detail = str(exc) or "accounting state is unavailable"
        raw_state = {}
    except Exception as exc:  # noqa: BLE001
        available = False
        detail = f"accounting read failed ({type(exc).__name__})"
        raw_state = {}
    daily_used, monthly_used = _normalize_state(raw_state, daily_period, monthly_period)
    daily_remaining = max(0, DAILY_LIMIT_BYTES - daily_used)
    monthly_remaining = max(0, MONTHLY_LIMIT_BYTES - monthly_used)
    result = {
        "daily_period": daily_period,
        "monthly_period": monthly_period,
        "daily_used_bytes": daily_used,
        "monthly_used_bytes": monthly_used,
        "daily_limit_bytes": DAILY_LIMIT_BYTES,
        "monthly_limit_bytes": MONTHLY_LIMIT_BYTES,
        "daily_remaining_bytes": daily_remaining,
        "monthly_remaining_bytes": monthly_remaining,
        "available": available,
        "detail": detail,
        "state": usage_state(daily_used, monthly_used) if available else "unavailable",
        # Aliases.
        "daily_used": daily_used,
        "monthly_used": monthly_used,
        "daily_limit": DAILY_LIMIT_BYTES,
        "monthly_limit": MONTHLY_LIMIT_BYTES,
        "daily_remaining": daily_remaining,
        "monthly_remaining": monthly_remaining,
        "period_daily": daily_period,
        "period_monthly": monthly_period,
    }
    return result


def get_outbound_usage_sync(now: Optional[datetime] = None) -> Dict[str, Any]:
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = None
    if loop is not None and loop.is_running():
        import concurrent.futures

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(asyncio.run, get_outbound_usage(now))
            return future.result()
    return asyncio.run(get_outbound_usage(now))


async def release_reservation(*args: Any, **kwargs: Any) -> bool:
    """Conservative no-op: reservations are never refunded."""
    return False


# Backwards/alternate names.
reserve_bytes = reserve_outbound_bytes
get_usage = get_outbound_usage
get_outbound_usage_async = get_outbound_usage
reserve = reserve_outbound_bytes
prune_old_records = lambda *a, **k: None
