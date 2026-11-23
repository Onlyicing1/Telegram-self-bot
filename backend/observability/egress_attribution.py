"""Bounded, opt-in attribution of outbound application payload bytes.

This module MEASURES only.  It never enforces a limit, retries, delays, or
changes a transfer, and it is fully disabled unless
``RENDER_EGRESS_ATTRIBUTION`` is exactly ``"1"``.

Two seams are counted:

* **Telegram uploads** — Telethon's ``progress_callback(current, total)`` is
  CUMULATIVE bytes for one single-file ``upload_file`` / ``send_file`` (pinned
  Telethon 1.43.0).  Only positive deltas are added, so a logical transfer is
  counted once and a restarted upload is not counted twice.  The counter is
  file payload, NOT MTProto wire bytes: RPC frames, TL/encryption overhead,
  keepalives, panel edits and connection setup stay unmeasured.
* **HTTP request bodies** — an ``httpx`` event hook reading the request's
  ``Content-Length``.  That is the body httpx actually sends (JSON, raw bytes,
  form data and multipart framing included), and it excludes TCP/IP, TLS and
  HTTP headers.  The body is never read, so a streamed body is untouched; a
  body of unknown length is reported as unmeasured rather than guessed.

Reported semantics:

* ``exact_payload_bytes`` and ``estimated_payload_bytes`` are separate
  channels and are never summed together.  Every path instrumented here
  exposes an exact body length, so the estimate channel stays at zero.
* ``unmeasured_operations`` counts requests whose body length is unknown.
* Counters live in THIS PROCESS ONLY and reset on restart: no database, no
  telemetry store, no scheduler, no background task.  The aggregate line is
  emitted lazily by the next recorded event once ``FLUSH_INTERVAL_SECONDS``
  have elapsed, so a quiet process emits nothing and a window whose activity
  never resumes is emitted when activity next arrives (or on ``flush()``).
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

logger = logging.getLogger(__name__)

#: The one flag.  Any value other than exactly ``"1"`` keeps measurement off.
ENV_FLAG = "RENDER_EGRESS_ATTRIBUTION"

#: Minimum spacing between aggregate log summaries.
FLUSH_INTERVAL_SECONDS = 300.0

# ── closed vocabulary ───────────────────────────────────────────────────────

TELEGRAM_SELF_UPLOAD = "telegram_self_upload"
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
    # Provider labels.
    "openai", "gemini", "openrouter", "groq", "cerebras", "mistral", "zai",
    "sambanova", "nvidia", "cohere", "siliconflow", "fireworks", "nararouter",
    "cloudflare", "you", "speechmatics", "grok", "dummy", "openai_compat",
})

_FIELDS = (
    "exact_payload_bytes",
    "estimated_payload_bytes",
    "operation_count",
    "failed_operation_count",
    "unmeasured_operations",
)

_LOCK = threading.Lock()
_counts: Dict[str, Dict[str, Dict[str, int]]] = {}
_window_started: datetime = datetime.now(timezone.utc)
_last_flush: float = time.monotonic()


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


def _add(
    category: Any,
    label: Any,
    *,
    exact: int = 0,
    estimated: int = 0,
    unmeasured: int = 0,
    operations: int = 0,
    failed: int = 0,
) -> None:
    """Add one bounded observation.  Never raises, never touches the network."""
    if not enabled():
        return
    try:
        safe_category = _safe_category(category)
        safe_label = _safe_label(label)
        with _LOCK:
            bucket = _counts.setdefault(safe_category, {}).setdefault(safe_label, _blank())
            bucket["exact_payload_bytes"] += max(0, int(exact))
            bucket["estimated_payload_bytes"] += max(0, int(estimated))
            bucket["operation_count"] += max(0, int(operations))
            bucket["failed_operation_count"] += max(0, int(failed))
            bucket["unmeasured_operations"] += max(0, int(unmeasured))
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


def mark_failed(category: str, *, label: str = "") -> None:
    """Mark the operation already recorded for this category as failed."""
    _add(category, label, failed=1)


# ── flush / snapshot ────────────────────────────────────────────────────────

def _emit_locked(window: str) -> None:
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
                "operation_count=%d failed_operation_count=%d "
                "unmeasured_operations=%d scope=process",
                window,
                category,
                label or "-",
                bucket["exact_payload_bytes"],
                bucket["estimated_payload_bytes"],
                bucket["operation_count"],
                bucket["failed_operation_count"],
                bucket["unmeasured_operations"],
            )
            for field in _FIELDS:
                totals[field] += bucket[field]
    logger.info(
        "[RENDER_EGRESS] window=%s totals exact_payload_bytes=%d "
        "estimated_payload_bytes=%d operation_count=%d "
        "failed_operation_count=%d unmeasured_operations=%d "
        "note=application_payload_only_not_render_bandwidth",
        window,
        totals["exact_payload_bytes"],
        totals["estimated_payload_bytes"],
        totals["operation_count"],
        totals["failed_operation_count"],
        totals["unmeasured_operations"],
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


def flush() -> bool:
    """Emit the current window now (no timer, no task) and reset it."""
    if not enabled():
        return False
    try:
        with _LOCK:
            moment = datetime.now(timezone.utc)
            if _counts:
                _emit_locked(f"{_iso(_window_started)}..{_iso(moment)}")
            _reset_window_locked(moment)
            return True
    except Exception:  # noqa: BLE001
        return False


def snapshot() -> Dict[str, Any]:
    """The current in-memory window (process-scoped, resets on restart)."""
    captured = datetime.now(timezone.utc)
    try:
        with _LOCK:
            categories = {
                category: {
                    label: dict(bucket) for label, bucket in labels.items()
                }
                for category, labels in _counts.items()
            }
            started = _window_started
    except Exception:  # noqa: BLE001
        return {
            "enabled": enabled(),
            "window_started_utc": _iso(captured),
            "captured_at_utc": _iso(captured),
            "flush_interval_seconds": FLUSH_INTERVAL_SECONDS,
            "counters_scope": "process",
            "categories": {},
            "totals": _blank(),
        }
    totals = _blank()
    for labels in categories.values():
        for bucket in labels.values():
            for field in _FIELDS:
                totals[field] += bucket[field]
    return {
        "enabled": enabled(),
        "window_started_utc": _iso(started),
        "captured_at_utc": _iso(captured),
        "flush_interval_seconds": FLUSH_INTERVAL_SECONDS,
        "counters_scope": "process",
        "categories": categories,
        "totals": totals,
    }


def reset() -> None:
    """Drop every counter and restart the window (tests / operator reset)."""
    try:
        with _LOCK:
            _reset_window_locked(datetime.now(timezone.utc))
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


# ── HTTP request bodies ─────────────────────────────────────────────────────

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
    override = request.extensions.get(_REQUEST_EXTENSION)
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
        return


def _on_response(response: Any, category: str, label: str) -> None:
    try:
        if response.status_code >= 400:
            resolved_category, resolved_label = _target(response.request, category, label)
            mark_failed(resolved_category, label=resolved_label)
    except Exception:  # noqa: BLE001
        return


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
