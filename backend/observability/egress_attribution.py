"""Bounded, opt-in attribution of outbound application payload bytes.

This module MEASURES only.  It never enforces a limit, retries, delays, or
changes a transfer, and it is fully disabled unless
``RENDER_EGRESS_ATTRIBUTION`` is exactly ``"1"``.

Measured seams:

* **Telegram uploads** — Telethon's ``progress_callback(current, total)`` is
  CUMULATIVE bytes for one single-file ``upload_file`` / ``send_file`` (pinned
  Telethon 1.43.0).  Only positive deltas are added, so a logical transfer is
  counted once and a restarted upload is not counted twice.  The counter is
  file payload, NOT MTProto wire bytes: RPC frames, TL/encryption overhead,
  keepalives, panel edits and connection setup stay unmeasured.  The callback
  advances only after a part has been accepted, so retransmissions are not
  represented; this is a payload-level approximation, not a wire meter.
* **Telegram downloads** — the payload returned by the supported download
  boundary (a path or bytes), measured once per completed download.  MTProto
  framing and failed downloads without a payload stay unmeasured.
* **HTTP request bodies** — an ``httpx`` event hook reading the request's
  ``Content-Length``.  That is the body httpx actually sends (JSON, raw bytes,
  form data and multipart framing included), and it excludes TCP/IP, TLS and
  HTTP headers.  The body is never read, so a streamed body is untouched; a
  body of unknown length is reported as unmeasured rather than guessed.
* **HTTP response bodies** — the same response hook measures bytes consumed
  by the application as INBOUND.  A streamed response is wrapped and counted
  only as it is consumed; closing it early records the consumed bytes plus an
  incomplete-stream marker.  Inbound bytes are never added to outbound bytes
  and never compared with Render's outbound metric as if they were egress.

Reported semantics:

* ``exact_payload_bytes`` and ``estimated_payload_bytes`` are separate
  channels and are never summed together.  Every path instrumented here
  exposes an exact body length, so the estimate channel stays at zero.
* ``exact_inbound_bytes`` is a separate inbound channel.  It is never added
  to any outbound total.
* ``unmeasured_operations`` / ``unmeasured_inbound_operations`` count
  operations whose byte count is unknown.  ``incomplete_inbound_streams``
  counts streams closed before exhaustion.
* Window counters are emitted by an existing lifecycle caller (the heartbeat)
  at most every ``FLUSH_INTERVAL_SECONDS``; an enabled process with no
  activity emits a bounded status line at most every
  ``STATUS_INTERVAL_SECONDS``.  Month-to-date counters live beside the window
  counters so a quiet process can still be reconciled with Render's calendar
  month.  They are process-scoped and reset on restart: no database, no
  telemetry store, no scheduler, no background task.
* Labels come from a closed vocabulary.  A URL, id, filename, prompt, token or
  any other free text can never become a label or a log field.

This is NOT Render bandwidth.  Render's NAT metric stays authoritative, and
the sum of these counters is not expected to equal it.
"""
from __future__ import annotations

import inspect
import logging
import os
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional

import httpx

logger = logging.getLogger(__name__)

#: The one flag.  Any value other than exactly ``"1"`` keeps measurement off.
ENV_FLAG = "RENDER_EGRESS_ATTRIBUTION"

#: Minimum spacing between aggregate log summaries.
FLUSH_INTERVAL_SECONDS = 300.0

# ── closed vocabulary ───────────────────────────────────────────────────────

TELEGRAM_SELF_UPLOAD = "telegram_self_upload"
TELEGRAM_DOWNLOAD = "telegram_download"
TELEGRAM_HELPER = "telegram_helper"
SUPABASE_REST = "supabase_rest"
SUPABASE_STORAGE = "supabase_storage"
AI_PROVIDER = "ai_provider"
AI_DISCOVERY = "ai_discovery"
STT_PROVIDER = "stt_provider"
TTS_PROVIDER = "tts_provider"
RENDER_API = "render_api"
OTHER_KNOWN_HTTP = "other_known_http"

CATEGORIES = (
    TELEGRAM_SELF_UPLOAD,
    TELEGRAM_DOWNLOAD,
    TELEGRAM_HELPER,
    SUPABASE_REST,
    SUPABASE_STORAGE,
    AI_PROVIDER,
    AI_DISCOVERY,
    STT_PROVIDER,
    TTS_PROVIDER,
    RENDER_API,
    OTHER_KNOWN_HTTP,
)
_CATEGORIES = frozenset(CATEGORIES)
_FALLBACK_CATEGORY = OTHER_KNOWN_HTTP

#: Every label this code base may emit.  Anything else becomes ``other``.
_LABELS = frozenset({
    # Call-site labels (fixed, never derived from content).
    "deep_save", "album_member", "voice", "rename_reupload", "ai_attachment",
    "postgrest", "storage_upload", "gemini_media", "render", "helper", "other",
    # Download call-site labels (fixed, never derived from content).
    "download", "save_download", "album_download", "ocr_download",
    "rename_download", "appearance_download", "voice_download",
    "storage_download",
    # Provider labels.
    "openai", "gemini", "openrouter", "groq", "cerebras", "mistral", "zai",
    "sambanova", "nvidia", "cohere", "siliconflow", "fireworks", "nararouter",
    "cloudflare", "you", "speechmatics", "grok", "dummy", "openai_compat",
})

_FIELDS = (
    "exact_payload_bytes",
    "estimated_payload_bytes",
    "exact_inbound_bytes",
    "operation_count",
    "inbound_operation_count",
    "failed_operation_count",
    "unmeasured_operations",
    "unmeasured_inbound_operations",
    "incomplete_inbound_streams",
)

#: Bounded no-activity status cadence.  Activity is still flushed at most every
#: ``FLUSH_INTERVAL_SECONDS``; this only prevents an idle process from being
#: indistinguishable from a disabled one.
STATUS_INTERVAL_SECONDS = 3600.0

_LOCK = threading.Lock()
_counts: Dict[str, Dict[str, Dict[str, int]]] = {}
_window_started: datetime = datetime.now(timezone.utc)
_last_flush: float = time.monotonic()
_month_period: str = datetime.now(timezone.utc).strftime("%Y-%m")
_month_counts: Dict[str, Dict[str, Dict[str, int]]] = {}
_last_status: float = time.monotonic()
_failures: int = 0


def enabled() -> bool:
    """True only when the flag is exactly ``"1"``."""
    return os.environ.get(ENV_FLAG, "") == "1"


def _blank() -> Dict[str, int]:
    return {field: 0 for field in _FIELDS}


def _safe_category(value: Any) -> str:
    return value if isinstance(value, str) and value in _CATEGORIES else _FALLBACK_CATEGORY


def _safe_label(value: Any) -> str:
    if not isinstance(value, str):
        return "other"
    label = value.strip().lower()
    return label if label in _LABELS else "other"


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def record_measurement_failure() -> None:
    """Count a bounded instrumentation failure without touching the transfer."""
    global _failures
    if not enabled():
        return
    try:
        with _LOCK:
            _failures += 1
    except Exception:  # noqa: BLE001
        return


def _roll_month_locked(moment: datetime) -> None:
    global _month_period, _month_counts
    period = moment.strftime("%Y-%m")
    if period != _month_period:
        _month_period = period
        _month_counts = {}


def _add(
    category: Any,
    label: Any,
    *,
    exact: int = 0,
    estimated: int = 0,
    inbound: int = 0,
    inbound_unmeasured: int = 0,
    incomplete: int = 0,
    unmeasured: int = 0,
    operations: int = 0,
    inbound_operations: int = 0,
    failed: int = 0,
) -> None:
    """Add one bounded observation.  Never raises, never touches the network."""
    if not enabled():
        return
    try:
        safe_category = _safe_category(category)
        safe_label = _safe_label(label)
        with _LOCK:
            moment = datetime.now(timezone.utc)
            _roll_month_locked(moment)
            bucket = _counts.setdefault(safe_category, {}).setdefault(safe_label, _blank())
            month = _month_counts.setdefault(safe_category, {}).setdefault(
                safe_label, _blank()
            )
            for target in (bucket, month):
                target["exact_payload_bytes"] += max(0, int(exact))
                target["estimated_payload_bytes"] += max(0, int(estimated))
                target["exact_inbound_bytes"] += max(0, int(inbound))
                target["operation_count"] += max(0, int(operations))
                target["inbound_operation_count"] += max(0, int(inbound_operations))
                target["failed_operation_count"] += max(0, int(failed))
                target["unmeasured_operations"] += max(0, int(unmeasured))
                target["unmeasured_inbound_operations"] += max(
                    0, int(inbound_unmeasured)
                )
                target["incomplete_inbound_streams"] += max(0, int(incomplete))
            _maybe_flush_locked()
    except Exception:  # noqa: BLE001 — measurement can never break a transfer
        return


def record_exact(
    category: str, byte_count: int, *, label: str = "", failed: int = 0
) -> None:
    """Record an exactly measured payload and one operation."""
    _add(category, label, exact=byte_count, operations=1, failed=failed)


def record_estimated(
    category: str, byte_count: int, *, label: str = "", failed: int = 0
) -> None:
    """Record an explicitly estimated payload and one operation."""
    _add(category, label, estimated=byte_count, operations=1, failed=failed)


def record_unmeasured(category: str, *, label: str = "", failed: int = 0) -> None:
    """Record one operation whose byte count is unknown."""
    _add(category, label, unmeasured=1, operations=1, failed=failed)


def record_inbound(
    category: str,
    byte_count: int,
    *,
    label: str = "",
    complete: bool = True,
    failed: int = 0,
) -> None:
    """Record bytes received by the application; never outbound traffic."""
    _add(
        category,
        label,
        inbound=byte_count,
        inbound_operations=1,
        incomplete=0 if complete else 1,
        failed=failed,
    )


def record_inbound_unmeasured(
    category: str,
    *,
    label: str = "",
    complete: bool = False,
    failed: int = 0,
) -> None:
    """Record an inbound operation whose size is unknown."""
    _add(
        category,
        label,
        inbound_unmeasured=1,
        inbound_operations=1,
        incomplete=0 if complete else 1,
        failed=failed,
    )


def mark_failed(category: str, *, label: str = "") -> None:
    """Mark the operation already recorded for this category as failed."""
    _add(category, label, failed=1)


# ── flush / snapshot ────────────────────────────────────────────────────────

def _sum_categories(categories: Dict[str, Dict[str, Dict[str, int]]]) -> Dict[str, int]:
    totals = _blank()
    for labels in categories.values():
        for bucket in labels.values():
            for field in _FIELDS:
                totals[field] += int(bucket.get(field, 0))
    return totals


def _policy_number(policy: Any, key: str) -> Any:
    if not isinstance(policy, dict):
        return "-"
    value = policy.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        return "-"
    return value


def _policy_text(policy: Any, key: str) -> str:
    if not isinstance(policy, dict):
        return "-"
    value = policy.get(key)
    if isinstance(value, bool):
        return str(value).lower()
    if value is None:
        return "-"
    if isinstance(value, (int, str)):
        return str(value)
    return "-"


def _emit_locked(window: str, policy: Any = None) -> None:
    """Emit at most one line per (category, label) plus one totals line."""
    totals = _blank()
    for category in sorted(_counts):
        for label in sorted(_counts[category]):
            bucket = _counts[category][label]
            if not any(bucket.values()):
                continue
            logger.info(
                "[RENDER_EGRESS] window=%s category=%s label=%s "
                "exact_payload_bytes=%d estimated_payload_bytes=%d "
                "exact_inbound_bytes=%d operation_count=%d "
                "inbound_operation_count=%d failed_operation_count=%d "
                "unmeasured_operations=%d unmeasured_inbound_operations=%d "
                "incomplete_inbound_streams=%d scope=process",
                window,
                category,
                label or "-",
                bucket["exact_payload_bytes"],
                bucket["estimated_payload_bytes"],
                bucket["exact_inbound_bytes"],
                bucket["operation_count"],
                bucket["inbound_operation_count"],
                bucket["failed_operation_count"],
                bucket["unmeasured_operations"],
                bucket["unmeasured_inbound_operations"],
                bucket["incomplete_inbound_streams"],
            )
            for field in _FIELDS:
                totals[field] += bucket[field]
    month = _sum_categories(_month_counts)
    render = _policy_number(policy, "render_monthly_bytes")
    measured_month = int(month["exact_payload_bytes"])
    difference: Any = "-"
    if isinstance(render, int):
        difference = int(render) - measured_month
    logger.info(
        "[RENDER_EGRESS] window=%s totals exact_payload_bytes=%d "
        "estimated_payload_bytes=%d exact_inbound_bytes=%d operation_count=%d "
        "inbound_operation_count=%d failed_operation_count=%d "
        "unmeasured_operations=%d unmeasured_inbound_operations=%d "
        "incomplete_inbound_streams=%d month=%s "
        "month_exact_payload_bytes=%d month_exact_inbound_bytes=%d "
        "render_monthly_bytes=%s governor_monthly_bytes=%s policy_floor_bytes=%s "
        "policy_tier=%s policy_trusted=%s render_partial=%s "
        "unattributed_difference_bytes=%s "
        "note=application_payload_only_not_render_bandwidth",
        window,
        totals["exact_payload_bytes"],
        totals["estimated_payload_bytes"],
        totals["exact_inbound_bytes"],
        totals["operation_count"],
        totals["inbound_operation_count"],
        totals["failed_operation_count"],
        totals["unmeasured_operations"],
        totals["unmeasured_inbound_operations"],
        totals["incomplete_inbound_streams"],
        _month_period,
        measured_month,
        month["exact_inbound_bytes"],
        render,
        _policy_number(policy, "governor_monthly_bytes"),
        _policy_number(policy, "policy_floor_bytes"),
        _policy_number(policy, "tier"),
        _policy_text(policy, "trusted"),
        _policy_text(policy, "render_partial"),
        difference,
    )


def _emit_status_locked(moment: datetime, policy: Any = None) -> None:
    """One bounded line that proves an idle enabled process is still alive."""
    month = _sum_categories(_month_counts)
    logger.info(
        "[RENDER_EGRESS] status=enabled_no_activity window=%s..%s "
        "month=%s month_exact_payload_bytes=%d month_exact_inbound_bytes=%d "
        "measurement_failures=%d policy_tier=%s policy_trusted=%s "
        "render_partial=%s scope=process",
        _iso(_window_started),
        _iso(moment),
        _month_period,
        month["exact_payload_bytes"],
        month["exact_inbound_bytes"],
        _failures,
        _policy_number(policy, "tier"),
        _policy_text(policy, "trusted"),
        _policy_text(policy, "render_partial"),
    )


def _reset_window_locked(moment: datetime, mono: Optional[float] = None) -> None:
    global _counts, _window_started, _last_flush
    _counts = {}
    _window_started = moment
    _last_flush = time.monotonic() if mono is None else mono


def _maybe_flush_locked() -> bool:
    """Flush only when the interval elapsed AND something was recorded."""
    mono = time.monotonic()
    if mono - _last_flush < FLUSH_INTERVAL_SECONDS:
        return False
    moment = datetime.now(timezone.utc)
    if _counts:
        _emit_locked(f"{_iso(_window_started)}..{_iso(moment)}")
    _reset_window_locked(moment, mono)
    return True


def flush(policy: Any = None) -> bool:
    """Emit the current window now (no timer, no task) and reset it.

    With no activity this intentionally emits nothing and does not reset the
    month-to-date counters; ``heartbeat_tick`` owns the bounded idle status.
    """
    if not enabled():
        return False
    try:
        with _LOCK:
            moment = datetime.now(timezone.utc)
            _roll_month_locked(moment)
            if _counts:
                _emit_locked(f"{_iso(_window_started)}..{_iso(moment)}", policy)
            _reset_window_locked(moment)
            return True
    except Exception:  # noqa: BLE001
        record_measurement_failure()
        return False


def flush_due() -> bool:
    """True when the existing lifecycle boundary should call ``heartbeat_tick``."""
    if not enabled():
        return False
    try:
        mono = time.monotonic()
        if _counts and mono - _last_flush >= FLUSH_INTERVAL_SECONDS:
            return True
        return not _counts and mono - _last_status >= STATUS_INTERVAL_SECONDS
    except Exception:  # noqa: BLE001
        return False


def heartbeat_tick(policy: Any = None) -> bool:
    """Flush activity or emit one bounded idle status from the heartbeat.

    This is a lifecycle hook, not a scheduler: the heartbeat already exists and
    this call performs no network request and no blocking work.
    """
    global _last_status
    if not enabled():
        return False
    try:
        with _LOCK:
            mono = time.monotonic()
            moment = datetime.now(timezone.utc)
            _roll_month_locked(moment)
            if _counts and mono - _last_flush >= FLUSH_INTERVAL_SECONDS:
                _emit_locked(f"{_iso(_window_started)}..{_iso(moment)}", policy)
                _reset_window_locked(moment, mono)
                return True
            if not _counts and mono - _last_status >= STATUS_INTERVAL_SECONDS:
                _emit_status_locked(moment, policy)
                _last_status = mono
                _reset_window_locked(moment, mono)
                return True
    except Exception:  # noqa: BLE001
        record_measurement_failure()
    return False


def log_startup() -> None:
    """Sanitized confirmation that attribution is enabled or disabled."""
    active = enabled()
    logger.info(
        "[RENDER_EGRESS] startup enabled=%s status=%s interval_seconds=%.0f "
        "status_interval_seconds=%.0f categories=%d counters_scope=process "
        "flag=%s",
        str(active).lower(),
        "recording" if active else "disabled",
        FLUSH_INTERVAL_SECONDS,
        STATUS_INTERVAL_SECONDS,
        len(CATEGORIES),
        ENV_FLAG,
    )


def shutdown(policy: Any = None) -> bool:
    """Flush the final window from the existing shutdown lifecycle."""
    flushed = flush(policy=policy)
    logger.info(
        "[RENDER_EGRESS] shutdown flushed=%s status=%s counters_scope=process",
        str(flushed).lower(),
        "disabled" if not enabled() else "flushed",
    )
    return flushed


def _status_locked() -> str:
    if not enabled():
        return "disabled"
    totals = _sum_categories(_counts)
    month = _sum_categories(_month_counts)
    combined = {field: totals[field] + month[field] for field in _FIELDS}
    if (
        combined["unmeasured_operations"]
        or combined["unmeasured_inbound_operations"]
        or combined["incomplete_inbound_streams"]
    ):
        return "measurement_incomplete"
    if _failures and not any(combined.values()):
        return "measurement_failure"
    if any(combined.values()):
        return "recording"
    return "enabled_no_activity"


def snapshot() -> Dict[str, Any]:
    """The current in-memory window and UTC month (process-scoped)."""
    captured = datetime.now(timezone.utc)
    try:
        with _LOCK:
            categories = {
                category: {
                    label: dict(bucket) for label, bucket in labels.items()
                }
                for category, labels in _counts.items()
            }
            month_categories = {
                category: {
                    label: dict(bucket) for label, bucket in labels.items()
                }
                for category, labels in _month_counts.items()
            }
            started = _window_started
            month = _month_period
            status = _status_locked()
            failures = _failures
    except Exception:  # noqa: BLE001
        record_measurement_failure()
        return {
            "enabled": enabled(),
            "status": "disabled" if not enabled() else "measurement_failure",
            "window_started_utc": _iso(captured),
            "captured_at_utc": _iso(captured),
            "flush_interval_seconds": FLUSH_INTERVAL_SECONDS,
            "status_interval_seconds": STATUS_INTERVAL_SECONDS,
            "counters_scope": "process",
            "month": _month_period,
            "categories": {},
            "totals": _blank(),
            "month_categories": {},
            "month_totals": _blank(),
            "measurement_failures": 0,
        }
    return {
        "enabled": enabled(),
        "status": status,
        "window_started_utc": _iso(started),
        "captured_at_utc": _iso(captured),
        "flush_interval_seconds": FLUSH_INTERVAL_SECONDS,
        "status_interval_seconds": STATUS_INTERVAL_SECONDS,
        "counters_scope": "process",
        "month": month,
        "categories": categories,
        "totals": _sum_categories(categories),
        "month_categories": month_categories,
        "month_totals": _sum_categories(month_categories),
        "measurement_failures": failures,
    }


def reset() -> None:
    """Drop every counter and restart the window (tests / operator reset)."""
    global _month_period, _month_counts, _failures, _last_status
    try:
        with _LOCK:
            moment = datetime.now(timezone.utc)
            _reset_window_locked(moment)
            _month_period = moment.strftime("%Y-%m")
            _month_counts = {}
            _failures = 0
            _last_status = time.monotonic()
    except Exception:  # noqa: BLE001
        return


# ── Telegram uploads ────────────────────────────────────────────────────────

class TelegramUploadTracker:
    """Attributes ONE logical Telegram file upload from cumulative progress.

    Telethon calls ``progress_callback(current, total)`` with the running byte
    count of the file being uploaded.  Only positive deltas are added, so the
    payload is counted once even if a retry restarts the transfer from zero.
    """

    __slots__ = ("_category", "_label", "_active", "_high", "_finished")

    def __init__(self, category: str = TELEGRAM_SELF_UPLOAD, label: str = "") -> None:
        self._category = category
        self._label = label
        self._active = enabled()
        self._high = 0
        self._finished = False

    # The context manager owns the terminal outcome: no exception means the
    # transfer succeeded; any exception (including cancellation) means it did
    # not, and the operation is counted as failed.
    def __enter__(self) -> "TelegramUploadTracker":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self._finish(exc_type is None)
        return False

    def callback(self, current: Any, total: Any = None) -> None:
        """Telethon-compatible progress callback (cumulative bytes)."""
        if not self._active:
            return
        try:
            value = int(current or 0)
        except (TypeError, ValueError):
            return
        if value <= self._high:
            # A restarted upload re-sends from zero.  The logical payload is
            # already accounted, so the restart adds nothing.
            return
        delta = value - self._high
        self._high = value
        # Bytes only: the operation is counted ONCE, when the transfer ends.
        _add(self._category, self._label, exact=delta)

    def wrap(self, inner: Optional[Callable[..., Any]]) -> Optional[Callable[..., Any]]:
        """Compose with the caller's own callback without changing its behavior.

        Returns ``inner`` unchanged when measurement is disabled, so a
        disabled process adds no callback at all.
        """
        if not self._active:
            return inner
        if inner is None:
            return self.callback

        async def _both(current: Any, total: Any = None) -> None:
            self.callback(current, total)
            result = inner(current, total)
            if inspect.isawaitable(result):
                await result

        return _both

    def _finish(self, ok: bool) -> None:
        if not self._active or self._finished:
            return
        self._finished = True
        if not ok and self._high <= 0:
            record_unmeasured(self._category, label=self._label, failed=1)
            return
        _add(self._category, self._label, operations=1, failed=0 if ok else 1)


def telegram_upload(
    category: str = TELEGRAM_SELF_UPLOAD, label: str = ""
) -> TelegramUploadTracker:
    """One tracker per logical upload."""
    return TelegramUploadTracker(category, label)


# ── Telegram downloads ─────────────────────────────────────────────────────

def record_telegram_download(
    payload: Any,
    *,
    label: str = "download",
    failed: int = 0,
    complete: bool = True,
) -> None:
    """Record one Telegram download payload as INBOUND, from a path or bytes.

    Telegram sends the media to this service, so these bytes are not Render
    egress and are never added to an outbound total.  The MTProto request and
    acknowledgement egress remains unmeasured.  ``None``/an unavailable path
    is unmeasured, never zero; a partially consumed download is marked
    incomplete.  The caller invokes this once per logical download, so a
    repeated download is a new inbound operation.
    """
    size: Optional[int] = None
    try:
        if isinstance(payload, (bytes, bytearray, memoryview)):
            size = len(payload)
        elif isinstance(payload, (str, os.PathLike)):
            path = os.fspath(payload)
            if path and os.path.isfile(path):
                size = os.path.getsize(path)
    except (OSError, TypeError, ValueError):
        size = None
    if size is None:
        record_inbound_unmeasured(
            TELEGRAM_DOWNLOAD,
            label=label,
            complete=False,
            failed=failed,
        )
    else:
        record_inbound(
            TELEGRAM_DOWNLOAD,
            size,
            label=label,
            complete=complete,
            failed=failed,
        )


# ── HTTP request and response bodies ────────────────────────────────────────

#: Per-request override key for a client shared by more than one category.
_REQUEST_EXTENSION = "egress_attribution"


def _body_size(request: Any) -> Optional[int]:
    """The request body length httpx will send, or ``None`` when unknown.

    ``Content-Length`` is exact and already computed by httpx for JSON, raw
    bytes, form and multipart bodies; reading it never touches the body, so a
    streamed upload is left intact.  A chunked/unknown-length body is
    ``None`` (unmeasured), never an estimate.
    """
    raw = request.headers.get("content-length")
    if raw is not None:
        try:
            size = int(raw)
        except (TypeError, ValueError):
            return None
        return size if size >= 0 else None
    if request.headers.get("transfer-encoding"):
        return None
    return 0


def _target(request: Any, default_category: str, default_label: str) -> tuple[str, str]:
    extensions = getattr(request, "extensions", None) or {}
    override = extensions.get(_REQUEST_EXTENSION)
    if isinstance(override, dict):
        category = override.get("category") or default_category
        label = override.get("label") or default_label
        return category, label
    return default_category, default_label


def _on_request(request: Any, category: str, label: str) -> None:
    try:
        resolved_category, resolved_label = _target(request, category, label)
        size = _body_size(request)
        if size is None:
            record_unmeasured(resolved_category, label=resolved_label)
        else:
            record_exact(resolved_category, size, label=resolved_label)
    except Exception:  # noqa: BLE001
        record_measurement_failure()


class _CountingSyncStream(httpx.SyncByteStream):
    """Count inbound bytes as a synchronous response stream is consumed."""

    __slots__ = ("_inner", "_category", "_label", "_bytes", "_done")

    def __init__(self, inner: Any, category: str, label: str) -> None:
        self._inner = inner
        self._category = category
        self._label = label
        self._bytes = 0
        self._done = False

    def __iter__(self):
        try:
            for chunk in self._inner:
                self._bytes += len(chunk)
                yield chunk
        except BaseException:
            self._finish(False)
            raise
        else:
            self._finish(True)

    def close(self) -> None:
        self._finish(False)
        close = getattr(self._inner, "close", None)
        if callable(close):
            close()

    def _finish(self, complete: bool) -> None:
        if self._done:
            return
        self._done = True
        record_inbound(
            self._category, self._bytes, label=self._label, complete=complete
        )


class _CountingAsyncStream(httpx.AsyncByteStream):
    """Count inbound bytes as an asynchronous response stream is consumed."""

    __slots__ = ("_inner", "_category", "_label", "_bytes", "_done")

    def __init__(self, inner: Any, category: str, label: str) -> None:
        self._inner = inner
        self._category = category
        self._label = label
        self._bytes = 0
        self._done = False

    def __aiter__(self):
        return self._iterate()

    async def _iterate(self):
        try:
            async for chunk in self._inner:
                self._bytes += len(chunk)
                yield chunk
        except BaseException:
            self._finish(False)
            raise
        else:
            self._finish(True)

    async def aclose(self) -> None:
        self._finish(False)
        aclose = getattr(self._inner, "aclose", None)
        if callable(aclose):
            await aclose()

    def _finish(self, complete: bool) -> None:
        if self._done:
            return
        self._done = True
        record_inbound(
            self._category, self._bytes, label=self._label, complete=complete
        )


def _measure_response(response: Any, category: str, label: str) -> None:
    """Measure response bytes as INBOUND; never consume a stream for it."""
    try:
        if getattr(response, "_egress_response_measured", False):
            return
        setattr(response, "_egress_response_measured", True)
        resolved_category, resolved_label = _target(
            response.request, category, label
        )
        content = getattr(response, "_content", None)
        if getattr(response, "is_stream_consumed", False) or content is not None:
            if isinstance(content, (bytes, bytearray, memoryview)):
                record_inbound(
                    resolved_category,
                    len(content),
                    label=resolved_label,
                    complete=True,
                )
            else:
                record_inbound_unmeasured(
                    resolved_category, label=resolved_label, complete=False
                )
            return
        stream = getattr(response, "stream", None)
        if stream is None:
            record_inbound_unmeasured(
                resolved_category, label=resolved_label, complete=False
            )
            return
        if hasattr(stream, "__aiter__"):
            response.stream = _CountingAsyncStream(
                stream, resolved_category, resolved_label
            )
        else:
            response.stream = _CountingSyncStream(
                stream, resolved_category, resolved_label
            )
    except Exception:  # noqa: BLE001 — never break the response
        record_measurement_failure()


def _on_response(response: Any, category: str, label: str) -> None:
    try:
        if response.status_code >= 400:
            resolved_category, resolved_label = _target(
                response.request, category, label
            )
            mark_failed(resolved_category, label=resolved_label)
        _measure_response(response, category, label)
    except Exception:  # noqa: BLE001
        record_measurement_failure()


def async_event_hooks(category: str, label: str = "") -> Dict[str, list]:
    """Request/response hooks for ``httpx.AsyncClient`` (async hooks)."""
    if not enabled():
        return {}

    async def _request_hook(request: Any) -> None:
        _on_request(request, category, label)

    async def _response_hook(response: Any) -> None:
        _on_response(response, category, label)

    return {"request": [_request_hook], "response": [_response_hook]}


def sync_event_hooks(category: str, label: str = "") -> Dict[str, list]:
    """Request/response hooks for ``httpx.Client`` (synchronous hooks)."""
    if not enabled():
        return {}

    def _request_hook(request: Any) -> None:
        _on_request(request, category, label)

    def _response_hook(response: Any) -> None:
        _on_response(response, category, label)

    return {"request": [_request_hook], "response": [_response_hook]}


def async_client_kwargs(category: str, label: str = "") -> Dict[str, Any]:
    """``httpx.AsyncClient(**kwargs)`` addition; empty when disabled."""
    hooks = async_event_hooks(category, label)
    return {"event_hooks": hooks} if hooks else {}


def sync_client_kwargs(category: str, label: str = "") -> Dict[str, Any]:
    """``httpx.Client(**kwargs)`` addition; empty when disabled."""
    hooks = sync_event_hooks(category, label)
    return {"event_hooks": hooks} if hooks else {}


def request_category(category: str, label: str = "") -> Dict[str, Any]:
    """Per-request category override for a client shared by two categories."""
    if not enabled():
        return {}
    return {"extensions": {_REQUEST_EXTENSION: {"category": category, "label": label}}}
