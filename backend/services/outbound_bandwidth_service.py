"""Persistent, fail-closed outbound bandwidth governor.

This module is the single authority for outbound payload accounting AND the
staged monthly policy. It does not attempt to measure all Render outbound
traffic; it accounts only for the explicit payload paths that call
:func:`reserve_outbound_bytes` before transmission (Gemini OCR/STT, Telegram
outbound media including Deep Save, voice notes, AI result attachments,
file-name re-uploads and Storage uploads), and evaluates the policy against the
higher of Render's service-scoped monthly egress and that local ledger.

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
import copy
import logging
import threading
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
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
MONTHLY_TARGET_BYTES: int = 4_000_000_000

# Staged monthly policy. These are cumulative: a higher tier never re-enables
# an action denied by a lower one.
POLICY_AUTOMATIC_MEDIA_PCT = 50
POLICY_BULK_MEDIA_PCT = 60
POLICY_PROVIDER_MEDIA_PCT = 70
POLICY_NONESSENTIAL_UPLOAD_PCT = 80
POLICY_STRICT_TEXT_ONLY_PCT = 90

POLICY_THRESHOLD_BYTES = {
    50: 2_000_000_000,
    60: 2_400_000_000,
    70: 2_800_000_000,
    80: 3_200_000_000,
    90: 3_600_000_000,
}

# A 32 KiB explicitly-accounted text operation remains text-only. Anything
# larger does not inherit the text exemption.
TEXT_EXEMPT_MAX_BYTES = 32 * 1024

# Render publishes hourly points with a delay. Five minutes is the maximum
# accepted cache age; a point older than three hours is too stale to trust.
RENDER_CACHE_TTL_SECONDS = 300
RENDER_MAX_STALENESS_SECONDS = 3 * 60 * 60

# Action classes are deliberately explicit. An unknown category is NEVER text.
ACTION_TEXT = "text"
ACTION_AUTOMATIC_MEDIA = "automatic_media"
ACTION_SINGLE_MEDIA = "single_media"
ACTION_BULK_MEDIA = "bulk_media"
ACTION_PROVIDER_MEDIA = "provider_media"
ACTION_FILE_UPLOAD = "file_upload"
ACTION_REUPLOAD = "reupload"
ACTION_STORAGE_UPLOAD = "storage_upload"
ACTION_MEDIA_FORWARD = "media_forward"
ACTION_UNKNOWN = "unknown"

_CATEGORY_ACTIONS = {
    "text": ACTION_TEXT,
    "telegram_media": ACTION_SINGLE_MEDIA,
    "telegram_voice": ACTION_SINGLE_MEDIA,
    "voice": ACTION_SINGLE_MEDIA,
    "bulk_media": ACTION_BULK_MEDIA,
    "album_media": ACTION_BULK_MEDIA,
    "ai_attachment": ACTION_FILE_UPLOAD,
    "file_upload": ACTION_FILE_UPLOAD,
    "rename_reupload": ACTION_REUPLOAD,
    "reupload": ACTION_REUPLOAD,
    "storage_upload": ACTION_STORAGE_UPLOAD,
    "gemini_ocr": ACTION_PROVIDER_MEDIA,
    "gemini_stt": ACTION_PROVIDER_MEDIA,
    "provider_media": ACTION_PROVIDER_MEDIA,
    "stt_upload": ACTION_PROVIDER_MEDIA,
    "media_forward": ACTION_MEDIA_FORWARD,
    "automatic_media": ACTION_AUTOMATIC_MEDIA,
}

_ACTION_MIN_TIER = {
    ACTION_AUTOMATIC_MEDIA: POLICY_AUTOMATIC_MEDIA_PCT,
    ACTION_BULK_MEDIA: POLICY_BULK_MEDIA_PCT,
    ACTION_PROVIDER_MEDIA: POLICY_PROVIDER_MEDIA_PCT,
    ACTION_FILE_UPLOAD: POLICY_NONESSENTIAL_UPLOAD_PCT,
    ACTION_REUPLOAD: POLICY_NONESSENTIAL_UPLOAD_PCT,
    ACTION_STORAGE_UPLOAD: POLICY_NONESSENTIAL_UPLOAD_PCT,
    ACTION_MEDIA_FORWARD: POLICY_NONESSENTIAL_UPLOAD_PCT,
    ACTION_SINGLE_MEDIA: POLICY_STRICT_TEXT_ONLY_PCT,
    # Unknown payloads are treated as potentially automatic media: they are
    # never allowed once any staged restriction is active.
    ACTION_UNKNOWN: POLICY_AUTOMATIC_MEDIA_PCT,
}

#: Set only around a claimed scheduled occurrence. Interactive owner actions
#: are never marked automatic.
_AUTOMATIC_MEDIA: ContextVar[bool] = ContextVar(
    "lifeos_automatic_outbound_media", default=False
)

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


class OutboundBandwidthPolicyError(OutboundBandwidthError, RuntimeError):
    """Raised when a staged monthly policy restricts an outbound action.

    The error is deliberately bounded and content-free: it names the action
    class, the active tier, the effective monthly bytes and the limit, never a
    payload, a filename, a chat id or a credential.
    """

    def __init__(
        self,
        action: str,
        tier: int,
        effective_monthly_bytes: int,
        reason: str = "",
    ) -> None:
        self.action = str(action)
        self.tier = int(tier)
        self.effective_monthly_bytes = int(effective_monthly_bytes)
        self.limit_bytes = MONTHLY_TARGET_BYTES
        self.reason = " ".join(str(reason or "").split())[:240]
        detail = self.reason or "the active monthly policy tier denies it"
        super().__init__(
            f"outbound bandwidth policy denied {self.action} "
            f"(tier={self.tier}% effective={self.effective_monthly_bytes} "
            f"limit={self.limit_bytes}): {detail}"
        )


# Alternate names for callers that classify the failure as a policy refusal.
BandwidthPolicyError = OutboundBandwidthPolicyError
OutboundPolicyError = OutboundBandwidthPolicyError


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


def _policy_floor_bytes(raw: Any, monthly_period: str) -> Optional[int]:
    """The persisted current-month high-water mark, if one is present.

    This is not a second accounting ledger. It only remembers the highest
    effective monthly reading already observed in THIS UTC month, so a delayed
    Render sample or a restart cannot lower an active restriction tier. A new
    month ignores the old period. A malformed floor is a denial, never zero.
    """
    if not isinstance(raw, dict):
        return None
    floor = raw.get("policy_floor")
    if floor is None:
        return None
    if not isinstance(floor, dict):
        raise OutboundBandwidthUnavailableError("stored policy floor is malformed")
    period = floor.get("period")
    value = floor.get("bytes")
    if not isinstance(period, str) or not period:
        raise OutboundBandwidthUnavailableError("stored policy floor period is malformed")
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise OutboundBandwidthUnavailableError("stored policy floor is malformed")
    if period != monthly_period:
        return None
    return int(value)


def _build_state(
    daily_period: str,
    daily_used: int,
    monthly_period: str,
    monthly_used: int,
    *,
    policy_floor: Any = None,
) -> Dict[str, Any]:
    state = {
        "version": _STATE_VERSION,
        "daily": {"period": daily_period, "bytes": int(daily_used)},
        "monthly": {"period": monthly_period, "bytes": int(monthly_used)},
    }
    if policy_floor is not None:
        # The floor is forward-compatible JSONB, not a new column or table.
        # Preserve it byte-for-byte across ordinary reservations.
        state["policy_floor"] = copy.deepcopy(policy_floor)
    return state


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


# ── staged monthly outbound policy ─────────────────────────────────────────
#
# The reservation ledger is only a subset of service egress. The policy therefore
# evaluates the HIGHER of the persisted governor month and Render's own
# service-scoped month. They are never added: an upload counted twice would
# otherwise look like twice as much traffic.

_policy_snapshot: Optional[Dict[str, Any]] = None
_policy_refresh_lock = asyncio.Lock()


def _policy_untrusted(reason: str, *, tier: int = 0, effective: int = 0) -> Dict[str, Any]:
    return {
        "trusted": False,
        "state": "unavailable",
        "detail": reason,
        "tier": int(tier),
        "effective_monthly_bytes": int(effective),
        "render_monthly_bytes": None,
        "governor_monthly_bytes": None,
        "render_available": False,
        "render_latest_data_at": None,
        "render_partial": False,
        "checked_at": None,
        "monthly_period": None,
    }


def _parse_utc(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _policy_cache_valid(snapshot: Optional[Dict[str, Any]], current: datetime) -> bool:
    if not isinstance(snapshot, dict) or snapshot.get("checked_at") is None:
        return False
    if snapshot.get("monthly_period") != current.strftime("%Y-%m"):
        return False
    checked = _parse_utc(snapshot.get("checked_at"))
    if checked is None:
        return False
    age = (current - checked).total_seconds()
    return 0 <= age <= RENDER_CACHE_TTL_SECONDS


def _render_monthly(
    render: Any, current: datetime,
) -> tuple[Optional[int], str, bool, bool]:
    """Validate one Render response: (bytes, reason, trusted, partial)."""
    if not isinstance(render, dict):
        return None, "Render returned no policy observation.", False, False
    if render.get("available") is not True:
        return None, str(render.get("detail") or "Render metric is unavailable."), False, False
    if render.get("monthly_period") != current.strftime("%Y-%m"):
        return None, "Render metric belongs to another UTC month.", False, False
    monthly = render.get("monthly_used_bytes")
    if isinstance(monthly, bool) or not isinstance(monthly, int) or monthly < 0:
        return None, "Render returned a malformed monthly total.", False, False
    latest = _parse_utc(render.get("latest_data_at"))
    if latest is None:
        return None, "Render returned no trustworthy latest_data_at.", False, False
    age = (current - latest).total_seconds()
    if age < -120:
        return None, "Render latest_data_at is in the future.", False, False
    if age > RENDER_MAX_STALENESS_SECONDS:
        hours = RENDER_MAX_STALENESS_SECONDS // 3600
        return None, f"Render metric is older than {hours}h.", False, False
    return int(monthly), "", True, bool(render.get("monthly_is_partial"))


def _policy_tier(effective: int) -> int:
    tier = 0
    for pct in (50, 60, 70, 80, 90):
        if effective >= POLICY_THRESHOLD_BYTES[pct]:
            tier = pct
    return tier


def _record_policy_floor(
    monthly_period: str,
    effective: int,
    daily_period: str,
) -> bool:
    """Persist the monthly high-water mark in the EXISTING JSONB state.

    This is a JSONB field on ``panel_settings.outbound_bandwidth_state``, not a
    new table/column. It is what stops a restart or a delayed/lower Render
    reading from lowering the tier before the next UTC month.
    """
    with _lock:
        raw = _read_row_state()
        existing = _policy_floor_bytes(raw, monthly_period)
        if existing is not None and existing >= effective:
            return True
        # Preserve the CURRENT reservation counters from the same locked read;
        # the observation that produced ``effective`` may be older than a
        # reservation completed while Render was being queried. Never rebuild
        # the ledger from that older snapshot.
        daily_used, monthly_used = _normalize_state(
            raw, daily_period, monthly_period
        )
        new_state = _build_state(
            daily_period,
            daily_used,
            monthly_period,
            monthly_used,
            policy_floor={"period": monthly_period, "bytes": int(effective)},
        )
        _persist_state("policy_floor", new_state)
        confirmed = _policy_floor_bytes(_read_row_state(), monthly_period)
        return confirmed is not None and confirmed >= effective


def _current_governor_monthly(now: datetime) -> Dict[str, Any]:
    try:
        return _usage_snapshot(now)
    except Exception as exc:  # noqa: BLE001 — policy must fail closed, not raise
        return {
            "available": False,
            "detail": f"accounting read failed ({type(exc).__name__})",
            "monthly_used_bytes": 0,
            "policy_floor_bytes": None,
        }


async def _default_render_usage_reader(now: Optional[datetime] = None) -> Any:
    from backend.services import render_bandwidth_service

    return await render_bandwidth_service.get_render_bandwidth_usage(now=now)


#: Indirection is the test seam: production always calls the real Render
#: integration; tests may replace this reader without changing the Render
#: service's own public behavior.
_render_usage_reader = _default_render_usage_reader


async def refresh_policy_snapshot(
    now: Optional[datetime] = None,
    *,
    force: bool = False,
) -> Dict[str, Any]:
    """Refresh the one policy snapshot used by every upload decision.

    Render publishes hourly points with a delay, so the snapshot is cached for
    five minutes. The async path is the only one that calls the Render API; the
    synchronous path consumes this same snapshot and never starts an event loop.
    """
    global _policy_snapshot
    current = _utc_now(now)
    cached = _policy_snapshot
    if not force and _policy_cache_valid(cached, current):
        return dict(cached)

    async with _policy_refresh_lock:
        cached = _policy_snapshot
        if not force and _policy_cache_valid(cached, current):
            return dict(cached)

        render: Any = None
        try:
            render = await _render_usage_reader(now=current)
        except Exception as exc:  # noqa: BLE001 — external observation is optional
            render = {"available": False, "detail": f"Render API failed ({type(exc).__name__})."}

        governor = await asyncio.to_thread(_current_governor_monthly, current)
        render_bytes, render_reason, render_trusted, partial = _render_monthly(render, current)
        governor_available = governor.get("available") is True
        governor_bytes = (
            int(governor.get("monthly_used_bytes") or 0)
            if governor_available else None
        )
        floor = governor.get("policy_floor_bytes") if governor_available else None
        if isinstance(floor, bool) or (floor is not None and not isinstance(floor, int)):
            floor = None
        floor = int(floor or 0)

        effective = max(
            value for value in (render_bytes or 0, governor_bytes or 0, floor)
        )
        trusted = bool(render_trusted and governor_available)
        reason = ""
        if not render_trusted:
            trusted = False
            reason = render_reason
        if not governor_available:
            trusted = False
            reason = str(governor.get("detail") or "accounting state is unavailable.")

        # A real reading at or above a tier becomes a durable floor. A failure
        # to persist that floor is itself a reason to fail closed for media: a
        # later restart must not be able to re-enable uploads mid-month.
        if governor_available and effective > floor:
            try:
                persisted = await asyncio.to_thread(
                    _record_policy_floor,
                    current.strftime("%Y-%m"),
                    effective,
                    current.strftime("%Y-%m-%d"),
                )
            except Exception as exc:  # noqa: BLE001
                persisted = False
                reason = f"policy floor could not be persisted ({type(exc).__name__})."
            if not persisted:
                trusted = False
                reason = reason or "policy floor could not be persisted."

        snapshot = {
            "trusted": trusted,
            "state": "available" if trusted else "unavailable",
            "detail": "" if trusted else reason or "policy observation is unavailable.",
            "tier": _policy_tier(effective),
            "effective_monthly_bytes": int(effective),
            "render_monthly_bytes": render_bytes,
            "governor_monthly_bytes": governor_bytes,
            "render_available": bool(render_trusted),
            "render_latest_data_at": (render or {}).get("latest_data_at") if isinstance(render, dict) else None,
            "render_partial": bool(partial),
            "checked_at": current.isoformat(),
            "monthly_period": current.strftime("%Y-%m"),
            "monthly_limit_bytes": MONTHLY_TARGET_BYTES,
        }
        _policy_snapshot = snapshot
        log = logger.info if trusted else logger.warning
        log(
            "OUTBOUND_POLICY_SNAPSHOT tier=%d effective=%d render=%s governor=%s "
            "trusted=%s partial=%s detail=%s",
            snapshot["tier"], snapshot["effective_monthly_bytes"],
            render_bytes if render_bytes is not None else "-",
            governor_bytes if governor_bytes is not None else "-",
            str(trusted).lower(), str(partial).lower(), snapshot["detail"] or "-",
        )
        return dict(snapshot)


def get_policy_snapshot(now: Optional[datetime] = None) -> Dict[str, Any]:
    """Return the last snapshot without refreshing (safe for worker threads)."""
    current = _utc_now(now)
    cached = _policy_snapshot
    if _policy_cache_valid(cached, current):
        return dict(cached)
    if isinstance(cached, dict):
        stale = dict(cached)
        stale["trusted"] = False
        stale["state"] = "unavailable"
        stale["detail"] = "policy snapshot is stale; waiting for a bounded refresh."
        return stale
    return _policy_untrusted("policy snapshot has not been refreshed.")


async def get_policy_status(now: Optional[datetime] = None) -> Dict[str, Any]:
    return await refresh_policy_snapshot(now=now)


def classify_outbound_action(category: Any, action: Optional[str] = None) -> str:
    """Classify an outbound payload explicitly, never by its current size."""
    if action is not None:
        action = str(action).strip()
        if action in _ACTION_MIN_TIER or action == ACTION_TEXT:
            resolved = action
        else:
            resolved = ACTION_UNKNOWN
    else:
        key = str(category or "").strip()
        resolved = _CATEGORY_ACTIONS.get(key, ACTION_UNKNOWN)
    if (
        _AUTOMATIC_MEDIA.get()
        and resolved != ACTION_TEXT
        and resolved != ACTION_UNKNOWN
    ):
        # Any media/file/provider class started by a scheduled occurrence is
        # automatic egress; the 50% tier must deny it regardless of which tool
        # or provider wrapper happens to be executing.
        return ACTION_AUTOMATIC_MEDIA
    return resolved


def automatic_media_scope():
    """Mark media started by an already-claimed scheduled occurrence.

    This is a context only; it never authorizes an operation by itself. The
    central policy still evaluates the current usage tier.
    """
    @contextlib.contextmanager
    def _scope():
        token = _AUTOMATIC_MEDIA.set(True)
        try:
            yield
        finally:
            _AUTOMATIC_MEDIA.reset(token)

    return _scope()


def _assert_policy_allows(
    snapshot: Dict[str, Any], action: str, byte_count: int,
) -> None:
    if action == ACTION_TEXT:
        if 0 < int(byte_count) <= TEXT_EXEMPT_MAX_BYTES:
            return
        # A payload above the strict text bound never inherits the exemption,
        # even at tier 0: it is an unclassified outbound payload.
        raise OutboundBandwidthPolicyError(
            ACTION_UNKNOWN,
            int(snapshot.get("tier") or 0),
            int(snapshot.get("effective_monthly_bytes") or 0),
            f"text payload exceeds the {TEXT_EXEMPT_MAX_BYTES}-byte exemption.",
        )
    if snapshot.get("trusted") is not True:
        raise OutboundBandwidthPolicyError(
            action, 0, int(snapshot.get("effective_monthly_bytes") or 0),
            str(snapshot.get("detail") or "the effective monthly metric is unavailable."),
        )
    tier = int(snapshot.get("tier") or 0)
    minimum = _ACTION_MIN_TIER.get(action, POLICY_AUTOMATIC_MEDIA_PCT)
    if tier >= minimum:
        raise OutboundBandwidthPolicyError(
            action, tier, int(snapshot.get("effective_monthly_bytes") or 0),
            f"the {tier}% tier denies this action class.",
        )


async def ensure_action_allowed(
    action: str,
    byte_count: int = 1,
    *,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Refresh if needed and enforce the staged policy for one action."""
    action = classify_outbound_action(action, action)
    if action == ACTION_TEXT and 0 < int(byte_count) <= TEXT_EXEMPT_MAX_BYTES:
        return get_policy_snapshot(now=now)
    snapshot = await refresh_policy_snapshot(now=now)
    _assert_policy_allows(snapshot, action, int(byte_count))
    return snapshot


def ensure_action_allowed_sync(
    action: str,
    byte_count: int = 1,
    *,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Enforce the SAME snapshot from a worker thread without starting a loop."""
    action = classify_outbound_action(action, action)
    if action == ACTION_TEXT and 0 < int(byte_count) <= TEXT_EXEMPT_MAX_BYTES:
        return get_policy_snapshot(now=now)
    snapshot = get_policy_snapshot(now=now)
    _assert_policy_allows(snapshot, action, int(byte_count))
    return snapshot


def effective_media_action(base: str = ACTION_SINGLE_MEDIA) -> str:
    """Return the automatic class for a scheduled context, else ``base``."""
    if _AUTOMATIC_MEDIA.get():
        return ACTION_AUTOMATIC_MEDIA
    return base


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
                daily_period,
                new_daily,
                monthly_period,
                new_monthly,
                policy_floor=(
                    raw_state.get("policy_floor")
                    if isinstance(raw_state, dict) else None
                ),
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


def _precheck_limits(
    category: str,
    byte_count: int,
    daily_period: str,
    monthly_period: str,
) -> None:
    """A read-only hard-limit check before policy evaluation.

    This preserves the existing daily/monthly error contract (and the daily hard
    limit) even when an unknown action would otherwise be denied by policy.
    The authoritative check is repeated inside ``_reserve_locked`` before write.
    """
    with _lock:
        raw_state = _read_row_state()
        daily_used, monthly_used = _normalize_state(
            raw_state, daily_period, monthly_period
        )
        if daily_used + byte_count > DAILY_LIMIT_BYTES:
            raise OutboundBandwidthExceededError(
                "daily", daily_used, byte_count, DAILY_LIMIT_BYTES
            )
        if monthly_used + byte_count > MONTHLY_LIMIT_BYTES:
            raise OutboundBandwidthExceededError(
                "monthly", monthly_used, byte_count, MONTHLY_LIMIT_BYTES
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
    current = _utc_now(now)
    daily_period, monthly_period = _period_keys(current)
    _precheck_limits(category, byte_count, daily_period, monthly_period)
    action = classify_outbound_action(category)
    await ensure_action_allowed(action, byte_count, now=current)
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
    current = _utc_now(now)
    daily_period, monthly_period = _period_keys(current)
    _precheck_limits(category, byte_count, daily_period, monthly_period)
    action = classify_outbound_action(category)
    ensure_action_allowed_sync(action, byte_count, now=current)
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
    policy_floor_bytes = None
    try:
        raw_state = _read_row_state()
        daily_used, monthly_used = _normalize_state(
            raw_state, daily_period, monthly_period
        )
        policy_floor_bytes = _policy_floor_bytes(raw_state, monthly_period)
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
        "policy_floor_bytes": policy_floor_bytes,
        "policy_tier": _policy_tier(max(monthly_used, policy_floor_bytes or 0))
        if available else 0,
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
