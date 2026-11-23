"""Persistent, fail-closed outbound bandwidth governor.

This module is the single authority for outbound payload accounting.  It does
not attempt to measure all Render outbound traffic; it accounts only for the
explicit payload paths that call :func:`reserve_outbound_bytes` before
transmission (Gemini OCR/STT, Telegram outbound media including Deep Save,
voice notes, AI result attachments, file-name re-uploads and Storage uploads).

Accounting state lives in the ``panel_settings.outbound_bandwidth_state``
JSONB column (one dedicated column on the existing singleton row) and is
accessed only through ``panel_settings_repository``.  Nothing is stored only
in RAM, so reservations survive restart/redeploy/crash.

Fail-closed contract: an unconfigured, unreadable, malformed or unwritable
persistent state DENIES the reservation.  No caller receives a successful
reservation that was not confirmed in the stored row, and no in-process
accounting is used as a production substitute.

Concurrency: ``_lock`` serializes the read -> write -> confirm sequence inside
ONE process, across its event loops and worker threads.  It is NOT a
distributed lock.  A write that another process overwrote between our read and
our write cannot be detected by any read-modify-write scheme, so cross-process
atomicity would require a database-side compare-and-set (a SQL function or a
migration); that is deliberately not done here.  Within the supported
single-runtime deployment this serializer is the enforceable guarantee.
"""
from __future__ import annotations

import asyncio
import logging
import threading
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

#: Bounded retries for a write that could not be CONFIRMED in stored state.
_MAX_CONFIRM_ATTEMPTS = 3

# One process-wide lock serializes reservation + persistence across every event
# loop and worker thread of this runtime.  It is not a distributed lock.
_lock = threading.Lock()


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
    """Raised when persistent accounting cannot be read, parsed or written."""


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
        # An unknown/unmeasurable payload is never treated as zero bytes.
        raise ValueError("byte_count must be greater than zero")
    return int(byte_count)


def _period_keys(now: datetime) -> tuple[str, str]:
    return now.strftime("%Y-%m-%d"), now.strftime("%Y-%m")


def _window_bytes(raw: Any, name: str) -> tuple[int, str]:
    """One stored daily/monthly window as ``(bytes, recorded_period)``.

    Anything that is not exactly the shape this module writes is a DENIAL: a
    malformed window is never read as zero usage.
    """
    if not isinstance(raw, dict):
        raise OutboundBandwidthUnavailableError(f"stored {name} window is malformed")
    period = raw.get("period")
    value = raw.get("bytes", 0)
    if not isinstance(period, str) or not period:
        raise OutboundBandwidthUnavailableError(f"stored {name} period is malformed")
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise OutboundBandwidthUnavailableError(f"stored {name} byte count is malformed")
    return int(value), period


def _normalize_state(raw: Any, daily_period: str, monthly_period: str) -> tuple[int, int]:
    """Return (daily_used, monthly_used) for the current periods.

    ``{}`` is the documented fresh-row value and means a real zero.  Any other
    unreadable or malformed value raises instead of fabricating zero usage.
    Old periods are a legitimate rollover and count as zero.
    """
    if raw is None or raw == {}:
        return 0, 0
    if not isinstance(raw, dict):
        raise OutboundBandwidthUnavailableError("stored bandwidth state is malformed")
    if raw.get("version") != _STATE_VERSION:
        raise OutboundBandwidthUnavailableError(
            "stored bandwidth state version is unknown"
        )
    if "daily" not in raw or "monthly" not in raw:
        raise OutboundBandwidthUnavailableError("stored bandwidth state is incomplete")
    daily_bytes, daily_recorded = _window_bytes(raw["daily"], "daily")
    monthly_bytes, monthly_recorded = _window_bytes(raw["monthly"], "monthly")
    daily_used = daily_bytes if daily_recorded == daily_period else 0
    monthly_used = monthly_bytes if monthly_recorded == monthly_period else 0
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
    """The persisted accounting state, or a denial when it cannot be read.

    There is no no-database branch that allows a reservation: without durable
    accounting there is no accounting, so the transfer is denied.
    """
    if not _db_configured():
        raise OutboundBandwidthUnavailableError(
            "persistent accounting is not configured"
        )
    try:
        row = repo.load()
    except Exception as exc:  # noqa: BLE001 — a failed read is a denial
        raise OutboundBandwidthUnavailableError(
            "outbound bandwidth accounting could not be read"
        ) from exc
    if row is None:
        raise OutboundBandwidthUnavailableError("panel_settings row is unavailable")
    if not isinstance(row, dict):
        raise OutboundBandwidthUnavailableError("panel_settings row is malformed")
    return row.get(_STATE_KEY, {})


def _persist_state(category: str, new_state: Dict[str, Any]) -> None:
    try:
        ok = repo.update_field(_STATE_KEY, new_state)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "OUTBOUND_BANDWIDTH_PERSIST_FAILED category=%s error=%s",
            category, type(exc).__name__,
        )
        raise OutboundBandwidthUnavailableError(
            "failed to persist outbound bandwidth reservation"
        ) from exc
    if not ok:
        logger.warning(
            "OUTBOUND_BANDWIDTH_PERSIST_FAILED category=%s error=write_refused",
            category,
        )
        raise OutboundBandwidthUnavailableError(
            "failed to persist outbound bandwidth reservation"
        )


def _reserve_locked(
    category: str,
    byte_count: int,
    daily_period: str,
    monthly_period: str,
) -> Dict[str, Any]:
    """Reserve under ``_lock``: read -> limit check -> write -> CONFIRM."""
    with _lock:
        for attempt in range(1, _MAX_CONFIRM_ATTEMPTS + 1):
            raw_state = _read_row_state()
            daily_used, monthly_used = _normalize_state(
                raw_state, daily_period, monthly_period
            )

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
            new_state = _build_state(
                daily_period, new_daily, monthly_period, new_monthly
            )
            _persist_state(category, new_state)

            confirmed_daily, confirmed_monthly = _normalize_state(
                _read_row_state(), daily_period, monthly_period
            )
            if confirmed_daily == new_daily and confirmed_monthly == new_monthly:
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
            logger.warning(
                "OUTBOUND_BANDWIDTH_WRITE_UNCONFIRMED category=%s attempt=%d",
                category, attempt,
            )

    raise OutboundBandwidthUnavailableError(
        "outbound bandwidth reservation could not be confirmed in persistent state"
    )


async def reserve_outbound_bytes(
    category: Any,
    byte_count: Any,
    *,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Reserve ``byte_count`` outbound bytes before a payload transfer.

    Raises :class:`OutboundBandwidthExceededError` when a limit would be
    exceeded and :class:`OutboundBandwidthUnavailableError` when persistence
    cannot be read, parsed or written.  Returns only after the reservation is
    confirmed in the stored state.
    """
    category = _validate_category(category)
    byte_count = _validate_byte_count(byte_count)
    daily_period, monthly_period = _period_keys(_utc_now(now))
    return await asyncio.to_thread(
        _reserve_locked, category, byte_count, daily_period, monthly_period
    )


def reserve_outbound_bytes_sync(
    category: Any,
    byte_count: Any,
    *,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Synchronous entry point for callers already running off the event loop.

    Safe to call from any worker thread: the reservation is serialized by the
    same process-wide lock and never creates or touches an event loop.
    """
    category = _validate_category(category)
    byte_count = _validate_byte_count(byte_count)
    daily_period, monthly_period = _period_keys(_utc_now(now))
    return _reserve_locked(category, byte_count, daily_period, monthly_period)


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


def _usage_snapshot(now: datetime) -> Dict[str, Any]:
    daily_period, monthly_period = _period_keys(now)
    available = True
    detail = ""
    daily_used = 0
    monthly_used = 0
    try:
        daily_used, monthly_used = _normalize_state(
            _read_row_state(), daily_period, monthly_period
        )
    except OutboundBandwidthUnavailableError as exc:
        available = False
        detail = str(exc) or "accounting state is unavailable"
    except Exception as exc:  # noqa: BLE001
        available = False
        detail = f"accounting read failed ({type(exc).__name__})"
    daily_remaining = max(0, DAILY_LIMIT_BYTES - daily_used)
    monthly_remaining = max(0, MONTHLY_LIMIT_BYTES - monthly_used)
    return {
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


async def get_outbound_usage(now: Optional[datetime] = None) -> Dict[str, Any]:
    """Return current usage without reserving anything.

    ``available`` is ``False`` when the durable accounting state could not be
    read or parsed; callers must show that state rather than treating the
    zero-valued byte fields as a real reading.  ``detail`` explains why.
    """
    return await asyncio.to_thread(_usage_snapshot, _utc_now(now))


def get_outbound_usage_sync(now: Optional[datetime] = None) -> Dict[str, Any]:
    return _usage_snapshot(_utc_now(now))


async def release_reservation(*args: Any, **kwargs: Any) -> bool:
    """Conservative no-op: reservations are never refunded."""
    return False


# Backwards/alternate names.
reserve_bytes = reserve_outbound_bytes
get_usage = get_outbound_usage
get_outbound_usage_async = get_outbound_usage
reserve = reserve_outbound_bytes
prune_old_records = lambda *a, **k: None
