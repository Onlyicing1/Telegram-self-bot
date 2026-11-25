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
import contextlib
import contextvars
import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, Iterator, Optional

from backend.services import panel_settings_repository as repo

try:
    from backend.db import client as _db_client
except Exception:  # noqa: BLE001
    _db_client = None

logger = logging.getLogger(__name__)

# Exact operational defaults (decimal bytes).  Do not silently change these.
DAILY_LIMIT_BYTES: int = 150_000_000
MONTHLY_LIMIT_BYTES: int = 4_000_000_000

# Cumulative staged policy, expressed as a share of the monthly budget.
# 50% -> 2.0 GB, 60% -> 2.4 GB, 70% -> 2.8 GB, 80% -> 3.2 GB,
# 90% -> 3.6 GB, 100% -> 4.0 GB.  A tier is active at OR ABOVE its threshold,
# so higher tiers never re-enable a lower tier's restriction.
POLICY_TIER_RATIOS: tuple[float, ...] = (0.50, 0.60, 0.70, 0.80, 0.90, 1.00)
POLICY_TIER_BYTES: tuple[int, ...] = tuple(
    int(MONTHLY_LIMIT_BYTES * ratio) for ratio in POLICY_TIER_RATIOS
)

ACTION_SCHEDULED_MEDIA = "scheduled_media"
ACTION_ALBUM_MEDIA = "album_media"
ACTION_AI_MEDIA = "ai_media"
ACTION_STORAGE_UPLOAD = "storage_upload"
ACTION_REUPLOAD = "reupload"
ACTION_MEDIA_TRANSFER = "media_transfer"
ACTION_INTERACTIVE_MEDIA = "interactive_media"
ACTION_VOICE = "voice"

#: The FIRST tier at which each bounded action is denied.  Denial is
#: cumulative: tier >= this value refuses the operation.
ACTION_MIN_TIER: Dict[str, int] = {
    ACTION_SCHEDULED_MEDIA: 1,
    ACTION_ALBUM_MEDIA: 2,
    ACTION_AI_MEDIA: 3,
    ACTION_STORAGE_UPLOAD: 4,
    ACTION_REUPLOAD: 4,
    ACTION_MEDIA_TRANSFER: 4,
    ACTION_INTERACTIVE_MEDIA: 5,
    ACTION_VOICE: 5,
}
#: Unknown categories are classified conservatively, never as interactive.
_CATEGORY_DEFAULT_ACTION: Dict[str, str] = {
    "telegram_media": ACTION_INTERACTIVE_MEDIA,
    "storage_upload": ACTION_STORAGE_UPLOAD,
    "gemini_ocr": ACTION_AI_MEDIA,
    "gemini_stt": ACTION_AI_MEDIA,
}

#: Render's metric is refreshed by the existing heartbeat; older values are
#: still a valid LOWER BOUND, only their trust flag changes.
RENDER_SNAPSHOT_MAX_AGE_S = 2 * 3600

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


class OutboundBandwidthPolicyError(OutboundBandwidthError, ValueError, RuntimeError):
    """Raised when the cumulative staged policy refuses an operation."""

    def __init__(
        self,
        action: str,
        tier: int,
        effective_bytes: int,
        min_tier: int,
    ) -> None:
        self.action = action
        self.tier = int(tier)
        self.effective_bytes = int(effective_bytes)
        self.min_tier = int(min_tier)
        super().__init__(
            f"outbound bandwidth policy tier {tier} refuses {action} "
            f"(effective={effective_bytes} bytes)"
        )


# Common aliases for callers/tests that use different names.
BandwidthExceededError = OutboundBandwidthExceededError
BandwidthLimitExceededError = OutboundBandwidthExceededError
BandwidthExceeded = OutboundBandwidthExceededError
BandwidthLimitExceeded = OutboundBandwidthExceededError
BandwidthUnavailableError = OutboundBandwidthUnavailableError
BandwidthUnavailable = OutboundBandwidthUnavailableError
BandwidthPolicyError = OutboundBandwidthPolicyError

#: Scheduled/batch executions run through the SAME executor; this scope is set
#: by the task coordinator so an automatic save cannot be classified as an
#: interactive one.  A contextvar is per-task, so concurrent work is unaffected.
_scheduled_scope: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "lifeos_scheduled_media", default=False
)

#: Render's last validated monthly lower bound (published by the metric client).
_render_metric: Dict[str, Any] = {
    "month": "",
    "bytes": None,
    "trusted": False,
    "coverage": "unknown",
    "latest_data_at": None,
    "updated_monotonic": 0.0,
}
_denials: Dict[str, int] = {action: 0 for action in ACTION_MIN_TIER}
_denials_month = ""


def policy_tier(effective_bytes: int) -> int:
    """The highest cumulative tier reached by ``effective_bytes`` (0 = none)."""
    tier = 0
    for index, threshold in enumerate(POLICY_TIER_BYTES, start=1):
        if effective_bytes >= threshold:
            tier = index
    return tier


def set_render_month_metric(
    month: str,
    byte_count: Optional[int],
    *,
    trusted: bool,
    coverage: str,
    latest_data_at: Optional[str],
) -> None:
    """Publish Render's validated lower bound; ``None`` means unknown, not zero."""
    global _render_metric
    value: Optional[int] = None
    if isinstance(byte_count, int) and not isinstance(byte_count, bool) and byte_count >= 0:
        value = int(byte_count)
    _render_metric = {
        "month": str(month or ""),
        "bytes": value,
        "trusted": bool(trusted),
        "coverage": str(coverage or "unknown"),
        "latest_data_at": latest_data_at,
        "updated_monotonic": time.monotonic(),
    }


def persist_render_floor(
    month: str,
    byte_count: int,
    *,
    now: Optional[datetime] = None,
) -> bool:
    """Persist Render's observed lower bound as the durable policy floor.

    Runs at the metric refresh cadence, not per operation.  The write is
    confirmed by re-reading the row, and a failed write is reported without
    changing any transfer decision (the in-memory snapshot still applies).
    """
    if isinstance(byte_count, bool) or not isinstance(byte_count, int) or byte_count < 0:
        return False
    try:
        with _lock:
            moment = _utc_now(now)
            daily_period, monthly_period = _period_keys(moment)
            if month != monthly_period:
                return False
            raw_state = _read_row_state()
            daily_used, monthly_used = _normalize_state(
                raw_state, daily_period, monthly_period
            )
            floor_bytes = _normalize_policy(raw_state, monthly_period)
            new_floor = max(floor_bytes, int(byte_count))
            if new_floor <= floor_bytes:
                return False
            _persist_state(
                "render_metric",
                _build_state(
                    daily_period, daily_used, monthly_period, monthly_used, new_floor
                ),
            )
            confirmed = _normalize_policy(
                _read_row_state(), monthly_period
            )
            if confirmed < new_floor:
                logger.warning(
                    "OUTBOUND_BANDWIDTH_FLOOR_UNCONFIRMED month=%s floor_bytes=%d",
                    monthly_period, new_floor,
                )
                return False
            return True
    except Exception as exc:  # noqa: BLE001 — a floor write never breaks the caller
        logger.warning(
            "OUTBOUND_BANDWIDTH_FLOOR_FAILED month=%s error=%s",
            month, type(exc).__name__,
        )
        return False


def _render_metric_for(month: str) -> tuple[Optional[int], Dict[str, Any]]:
    snapshot = _render_metric
    if snapshot.get("month") != month:
        return None, {
            "render_trusted": False,
            "render_coverage": "unknown" if not snapshot.get("month") else "previous_month",
            "render_latest_data_at": None,
            "snapshot_age_s": None,
        }
    updated = float(snapshot.get("updated_monotonic") or 0.0)
    age = int(max(0.0, time.monotonic() - updated)) if updated else None
    value = snapshot.get("bytes")
    if age is not None and age > RENDER_SNAPSHOT_MAX_AGE_S:
        # Still a lower bound; trust is what expires, never the bytes.
        return value, {
            "render_trusted": False,
            "render_coverage": "stale",
            "render_latest_data_at": snapshot.get("latest_data_at"),
            "snapshot_age_s": age,
        }
    return value, {
        "render_trusted": bool(snapshot.get("trusted")),
        "render_coverage": str(snapshot.get("coverage") or "unknown"),
        "render_latest_data_at": snapshot.get("latest_data_at"),
        "snapshot_age_s": age,
    }


def _note_denial(action: str, month: str) -> None:
    global _denials, _denials_month
    if month != _denials_month:
        _denials = {name: 0 for name in ACTION_MIN_TIER}
        _denials_month = month
    _denials[action] = _denials.get(action, 0) + 1


@contextlib.contextmanager
def scheduled_media_scope() -> Iterator[None]:
    """Mark every outbound payload in this block as scheduled/automatic."""
    token = _scheduled_scope.set(True)
    try:
        yield
    finally:
        _scheduled_scope.reset(token)


def in_scheduled_media_scope() -> bool:
    return bool(_scheduled_scope.get())


def _resolve_action(category: str, action: Optional[str]) -> str:
    """Validate the caller's bounded action, conservatively when unspecified."""
    if action is None:
        resolved = _CATEGORY_DEFAULT_ACTION.get(category, ACTION_MEDIA_TRANSFER)
    else:
        if action not in ACTION_MIN_TIER:
            raise ValueError("unknown outbound bandwidth action")
        resolved = action
    if _scheduled_scope.get():
        return ACTION_SCHEDULED_MEDIA
    return resolved


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


def _build_state(
    daily_period: str,
    daily_used: int,
    monthly_period: str,
    monthly_used: int,
    policy_floor_bytes: int = 0,
) -> Dict[str, Any]:
    state: Dict[str, Any] = {
        "version": _STATE_VERSION,
        "daily": {"period": daily_period, "bytes": int(daily_used)},
        "monthly": {"period": monthly_period, "bytes": int(monthly_used)},
    }
    if policy_floor_bytes > 0:
        # Same JSONB column, no schema change: the floor is the highest
        # effective monthly usage this process has ever observed.
        state["policy"] = {
            "month": monthly_period,
            "floor_bytes": int(policy_floor_bytes),
        }
    return state


def _normalize_policy(raw: Any, monthly_period: str) -> int:
    """The durable monthly policy floor, strictly validated.

    A malformed policy block is a DENIAL (the row is written by this module).
    A previous month's floor is a legitimate rollover and contributes zero.
    """
    if not isinstance(raw, dict):
        raise OutboundBandwidthUnavailableError("stored bandwidth state is malformed")
    policy = raw.get("policy")
    if policy is None:
        return 0
    if not isinstance(policy, dict):
        raise OutboundBandwidthUnavailableError("stored policy floor is malformed")
    month = policy.get("month")
    floor = policy.get("floor_bytes")
    if not isinstance(month, str) or not month:
        raise OutboundBandwidthUnavailableError("stored policy floor period is malformed")
    if isinstance(floor, bool) or not isinstance(floor, int) or floor < 0:
        raise OutboundBandwidthUnavailableError("stored policy floor byte count is malformed")
    if month != monthly_period:
        return 0
    return int(floor)


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
    action: str,
    daily_period: str,
    monthly_period: str,
) -> Dict[str, Any]:
    """Reserve under ``_lock``: read -> policy -> limits -> write -> CONFIRM."""
    with _lock:
        for attempt in range(1, _MAX_CONFIRM_ATTEMPTS + 1):
            raw_state = _read_row_state()
            daily_used, monthly_used = _normalize_state(
                raw_state, daily_period, monthly_period
            )
            floor_bytes = _normalize_policy(raw_state, monthly_period)
            render_bytes, render_meta = _render_metric_for(monthly_period)
            effective_bytes = max(monthly_used, floor_bytes, render_bytes or 0)
            tier = policy_tier(effective_bytes)
            min_tier = ACTION_MIN_TIER.get(action, ACTION_MIN_TIER[ACTION_MEDIA_TRANSFER])

            # The absolute limits are checked first: a payload that would cross
            # 100% is ALWAYS refused with the hard-limit error, before the
            # staged ladder's own (stricter) refusal.
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

            if tier >= min_tier:
                _note_denial(action, monthly_period)
                logger.warning(
                    "OUTBOUND_BANDWIDTH_POLICY_DENIED action=%s tier=%d min_tier=%d "
                    "effective_bytes=%d local_month_bytes=%d render_bytes=%s "
                    "floor_bytes=%d render_trusted=%s render_coverage=%s",
                    action, tier, min_tier, effective_bytes, monthly_used,
                    render_bytes if render_bytes is not None else "unknown",
                    floor_bytes,
                    "true" if render_meta.get("render_trusted") else "false",
                    render_meta.get("render_coverage"),
                )
                raise OutboundBandwidthPolicyError(
                    action, tier, effective_bytes, min_tier
                )

            new_daily = daily_used + byte_count
            new_monthly = monthly_used + byte_count
            new_floor = max(floor_bytes, effective_bytes)
            new_state = _build_state(
                daily_period, new_daily, monthly_period, new_monthly, new_floor
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
                    "action": action,
                    "policy_tier": tier,
                    "policy_floor_bytes": new_floor,
                    "render_bytes": render_bytes,
                    "render_trusted": bool(render_meta.get("render_trusted")),
                    "render_coverage": render_meta.get("render_coverage"),
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
    action: Optional[str] = None,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Reserve ``byte_count`` outbound bytes before a payload transfer.

    Raises :class:`OutboundBandwidthExceededError` when a limit would be
    exceeded, :class:`OutboundBandwidthPolicyError` when the cumulative staged
    policy refuses the action, and :class:`OutboundBandwidthUnavailableError`
    when persistence cannot be read, parsed or written.  Returns only after the
    reservation is confirmed in the stored state.
    """
    category = _validate_category(category)
    byte_count = _validate_byte_count(byte_count)
    resolved_action = _resolve_action(category, action)
    daily_period, monthly_period = _period_keys(_utc_now(now))
    return await asyncio.to_thread(
        _reserve_locked, category, byte_count, resolved_action,
        daily_period, monthly_period,
    )


def reserve_outbound_bytes_sync(
    category: Any,
    byte_count: Any,
    *,
    action: Optional[str] = None,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Synchronous entry point for callers already running off the event loop.

    Safe to call from any worker thread: the reservation is serialized by the
    same process-wide lock and never creates or touches an event loop.
    """
    category = _validate_category(category)
    byte_count = _validate_byte_count(byte_count)
    resolved_action = _resolve_action(category, action)
    daily_period, monthly_period = _period_keys(_utc_now(now))
    return _reserve_locked(
        category, byte_count, resolved_action, daily_period, monthly_period
    )


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
    floor_bytes = 0
    render_bytes: Optional[int] = None
    render_meta: Dict[str, Any] = {}
    try:
        raw_state = _read_row_state()
        daily_used, monthly_used = _normalize_state(
            raw_state, daily_period, monthly_period
        )
        floor_bytes = _normalize_policy(raw_state, monthly_period)
        render_bytes, render_meta = _render_metric_for(monthly_period)
    except OutboundBandwidthUnavailableError as exc:
        available = False
        detail = str(exc) or "accounting state is unavailable"
    except Exception as exc:  # noqa: BLE001
        available = False
        detail = f"accounting read failed ({type(exc).__name__})"
    daily_remaining = max(0, DAILY_LIMIT_BYTES - daily_used)
    monthly_remaining = max(0, MONTHLY_LIMIT_BYTES - monthly_used)
    effective_bytes = max(monthly_used, floor_bytes, render_bytes or 0)
    return {
        "policy_tier": policy_tier(effective_bytes) if available else 0,
        "policy_floor_bytes": floor_bytes,
        "effective_month_bytes": effective_bytes,
        "render_bytes": render_bytes,
        "render_trusted": bool(render_meta.get("render_trusted")),
        "render_coverage": render_meta.get("render_coverage"),
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


def get_policy_snapshot_sync(now: Optional[datetime] = None) -> Dict[str, Any]:
    """One sanitized policy snapshot for the existing lifecycle logs.

    Read-only and never raises: an unavailable accounting row is reported as
    unavailable, never as a zero-byte month.
    """
    moment = _utc_now(now)
    daily_period, monthly_period = _period_keys(moment)
    base: Dict[str, Any] = {
        "month": monthly_period,
        "daily_period": daily_period,
        "tier": 0,
        "effective_bytes": 0,
        "local_month_bytes": 0,
        "floor_bytes": 0,
        "render_bytes": None,
        "render_trusted": False,
        "render_coverage": "unknown",
        "render_latest_data_at": None,
        "snapshot_age_s": None,
        "available": False,
        "detail": "",
    }
    try:
        with _lock:
            raw_state = _read_row_state()
            daily_used, monthly_used = _normalize_state(
                raw_state, daily_period, monthly_period
            )
            floor_bytes = _normalize_policy(raw_state, monthly_period)
            render_bytes, render_meta = _render_metric_for(monthly_period)
            denials = dict(_denials) if _denials_month == monthly_period else {}
        effective_bytes = max(monthly_used, floor_bytes, render_bytes or 0)
        tier = policy_tier(effective_bytes)
        next_index = min(tier, len(POLICY_TIER_BYTES) - 1)
        base.update(
            {
                "tier": tier,
                "effective_bytes": effective_bytes,
                "local_month_bytes": monthly_used,
                "floor_bytes": floor_bytes,
                "render_bytes": render_bytes,
                "render_trusted": bool(render_meta.get("render_trusted")),
                "render_coverage": render_meta.get("render_coverage"),
                "render_latest_data_at": render_meta.get("render_latest_data_at"),
                "snapshot_age_s": render_meta.get("snapshot_age_s"),
                "next_threshold_bytes": POLICY_TIER_BYTES[next_index],
                "denials": ",".join(
                    f"{name}:{count}" for name, count in sorted(denials.items()) if count
                ) or "none",
                "available": True,
            }
        )
    except OutboundBandwidthUnavailableError as exc:
        base["detail"] = str(exc)[:80] or "accounting unavailable"
    except Exception as exc:  # noqa: BLE001
        base["detail"] = f"accounting read failed ({type(exc).__name__})"
    return base


async def release_reservation(*args: Any, **kwargs: Any) -> bool:
    """Conservative no-op: reservations are never refunded."""
    return False


# Backwards/alternate names.
reserve_bytes = reserve_outbound_bytes
get_usage = get_outbound_usage
get_outbound_usage_async = get_outbound_usage
reserve = reserve_outbound_bytes
prune_old_records = lambda *a, **k: None
