"""Bounded, privacy-safe egress attribution for the LifeOS runtime.

This module answers ONE question for the owner: which bounded subsystem produced
the payload bytes this process actually moved?  It is deliberately not a second
bandwidth governor and not a metrics backend:

* the category vocabulary is CLOSED — an unknown source is refused, so a URL,
  filename, chat id, prompt or token can never become a metric label;
* counters are process-local, bounded and reset at each UTC month boundary;
* no per-request log line is emitted; a single aggregate summary is flushed on
  the existing heartbeat/shutdown lifecycle, never by a new scheduler;
* every public function swallows its own failure, because measurement must
  never break an application operation.

Measurement classes
-------------------
Telegram payloads are measured at the logical transfer boundary (the exact
buffer/file size).  Telegram's pinned client exposes only logical progress
callbacks, not wire bytes, so MTProto framing and automatic retransmissions are
NOT included; those are reported as unmeasured, never invented.

HTTP payloads are measured by wrapping ``httpx.Client.send`` /
``httpx.AsyncClient.send`` (public API, installed once at runtime startup).
Request bodies are read only when httpx has already materialized them; a
streaming request body is measured from ``Content-Length`` when present and is
otherwise recorded as unmeasured.  A non-streaming response is measured from
the body httpx already read.  A streaming response is wrapped with a counting
proxy that observes the caller's own iteration — it never consumes the stream
itself.  Bytes are decoded-body bytes, so content encoding can make the figure
differ from the wire; that is documented, not hidden.
"""
from __future__ import annotations

import contextlib
import contextvars
import logging
import os
import threading
import time
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Iterator
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)

ENV_FLAG = "RENDER_EGRESS_ATTRIBUTION"
FLUSH_ENV = "RENDER_EGRESS_FLUSH_SECONDS"

SOURCE_TELEGRAM_UPLOAD = "telegram_upload"
SOURCE_TELEGRAM_DOWNLOAD = "telegram_download"
SOURCE_TELEGRAM_TRANSPORT = "telegram_transport"
SOURCE_AI_PROVIDER = "ai_provider"
SOURCE_AI_SEARCH = "ai_search"
SOURCE_STT_PROVIDER = "stt_provider"
SOURCE_TTS_PROVIDER = "tts_provider"
SOURCE_SUPABASE_REST = "supabase_rest"
SOURCE_SUPABASE_STORAGE = "supabase_storage"
SOURCE_RENDER_API = "render_api"
SOURCE_OTHER_HTTP = "other_http"

SOURCE_ORDER: tuple[str, ...] = (
    SOURCE_TELEGRAM_UPLOAD,
    SOURCE_TELEGRAM_DOWNLOAD,
    SOURCE_TELEGRAM_TRANSPORT,
    SOURCE_AI_PROVIDER,
    SOURCE_AI_SEARCH,
    SOURCE_STT_PROVIDER,
    SOURCE_TTS_PROVIDER,
    SOURCE_SUPABASE_REST,
    SOURCE_SUPABASE_STORAGE,
    SOURCE_RENDER_API,
    SOURCE_OTHER_HTTP,
)
_SOURCE_SET = frozenset(SOURCE_ORDER)

#: Telegram downloads are INBOUND bytes.  They are measured because they explain
#: media operations, but they are never added to outbound reconciliation.
OUTBOUND_SOURCES: tuple[str, ...] = tuple(
    source for source in SOURCE_ORDER if source != SOURCE_TELEGRAM_DOWNLOAD
)
INBOUND_SOURCES: tuple[str, ...] = (SOURCE_TELEGRAM_DOWNLOAD,)

_DEFAULT_FLUSH_S = 300.0
_MIN_FLUSH_S = 30.0
_MAX_FLUSH_S = 3600.0
_MAX_RECORD_BYTES = 1 << 62

_TRUTHY = frozenset({"1", "true", "yes", "on"})
_lock = threading.Lock()
_month_key = ""
_totals: dict[str, int] = {}
_ops: dict[str, int] = {}
_unmeasured_ops: dict[str, int] = {}
_estimated_bytes = 0
_estimated_ops = 0
_interval_started = time.monotonic()
_last_flush = time.monotonic()
_failures_since_flush = 0
_startup_logged = False

_source_override: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "lifeos_egress_source", default=None
)

# ── HTTP client instrumentation state ──
_original_async_send: Any = None
_original_sync_send: Any = None
_installed = False


def _now(now: datetime | None) -> datetime:
    if now is None:
        return datetime.now(timezone.utc)
    if now.tzinfo is None:
        return now.replace(tzinfo=timezone.utc)
    return now.astimezone(timezone.utc)


def enabled() -> bool:
    """True when ``RENDER_EGRESS_ATTRIBUTION`` is explicitly enabled."""
    try:
        return (os.getenv(ENV_FLAG, "") or "").strip().lower() in _TRUTHY
    except Exception:  # noqa: BLE001 — configuration failure means disabled
        return False


def flush_interval_seconds() -> float:
    """Bounded flush cadence; an invalid value falls back to the default."""
    try:
        raw = float(os.getenv(FLUSH_ENV, "") or _DEFAULT_FLUSH_S)
    except (TypeError, ValueError):
        return _DEFAULT_FLUSH_S
    return min(_MAX_FLUSH_S, max(_MIN_FLUSH_S, raw))


def _reset_month_locked(month: str) -> None:
    global _month_key, _estimated_bytes, _estimated_ops
    _month_key = month
    _totals.clear()
    _ops.clear()
    _unmeasured_ops.clear()
    for source in SOURCE_ORDER:
        _totals[source] = 0
        _ops[source] = 0
        _unmeasured_ops[source] = 0
    _estimated_bytes = 0
    _estimated_ops = 0


def _ensure_month_locked(now: datetime) -> None:
    month = now.strftime("%Y-%m")
    if month != _month_key:
        _reset_month_locked(month)


def _positive_int(value: Any, *, allow_zero: bool) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if value < 0 or (value == 0 and not allow_zero):
        return None
    if value > _MAX_RECORD_BYTES:
        return None
    return int(value)


def record(source: str, byte_count: Any, *, estimated: bool = False) -> bool:
    """Record one measured payload transfer.  Never raises.

    ``source`` must be one of :data:`SOURCE_ORDER`; anything else is refused so
    no caller can create an unbounded label dimension.  ``estimated`` keeps an
    approximation in its own counter, never mixed with exact measurements.
    """
    global _estimated_bytes, _estimated_ops
    if not enabled():
        return False
    try:
        if source not in _SOURCE_SET:
            return False
        value = _positive_int(byte_count, allow_zero=False)
        if value is None:
            return False
        with _lock:
            _ensure_month_locked(_now(None))
            _totals[source] += value
            _ops[source] += 1
            if estimated:
                _estimated_bytes += value
                _estimated_ops += 1
        return True
    except Exception:  # noqa: BLE001 — measurement never breaks the caller
        _note_failure()
        return False


def _record_request_bytes(source: str, byte_count: int) -> bool:
    """Add a request body's bytes to the CURRENT operation, not a new one."""
    if not enabled() or source not in _SOURCE_SET:
        return False
    value = _positive_int(byte_count, allow_zero=False)
    if value is None:
        return False
    try:
        with _lock:
            _ensure_month_locked(_now(None))
            _totals[source] += value
        return True
    except Exception:  # noqa: BLE001
        _note_failure()
        return False


def record_operation(source: str, byte_count: Any = 0) -> bool:
    """Record one completed operation, including a real zero-byte response."""
    if not enabled():
        return False
    try:
        if source not in _SOURCE_SET:
            return False
        value = _positive_int(byte_count, allow_zero=True)
        if value is None:
            return False
        with _lock:
            _ensure_month_locked(_now(None))
            _totals[source] += value
            _ops[source] += 1
        return True
    except Exception:  # noqa: BLE001
        _note_failure()
        return False


def record_unmeasured(source: str) -> bool:
    """Record an operation whose byte count could not be measured.

    It is counted only in the unmeasured-operation bucket: a missing size is
    never turned into a zero-byte measurement.
    """
    if not enabled():
        return False
    try:
        if source not in _SOURCE_SET:
            return False
        with _lock:
            _ensure_month_locked(_now(None))
            _unmeasured_ops[source] += 1
        return True
    except Exception:  # noqa: BLE001
        _note_failure()
        return False


def record_telegram_download(payload: Any) -> bool:
    """Record ONE Telegram download payload (a path or raw bytes).

    A missing/unreadable path is recorded as an unmeasured operation, never as
    zero downloaded bytes.  The download itself is INBOUND traffic and is kept
    out of outbound reconciliation.
    """
    try:
        if isinstance(payload, (bytes, bytearray, memoryview)):
            size = len(payload)
        elif isinstance(payload, str):
            size = os.path.getsize(payload)
        else:
            return record_unmeasured(SOURCE_TELEGRAM_DOWNLOAD)
        return record_operation(SOURCE_TELEGRAM_DOWNLOAD, size)
    except Exception:  # noqa: BLE001
        return record_unmeasured(SOURCE_TELEGRAM_DOWNLOAD)


def _note_failure() -> None:
    global _failures_since_flush
    try:
        with _lock:
            _failures_since_flush += 1
    except Exception:  # noqa: BLE001
        pass


def _coverage_locked() -> str:
    if sum(_unmeasured_ops.values()) > 0:
        return "partial"
    if sum(_ops.values()) > 0:
        return "complete"
    return "unknown"


def _status_locked() -> str:
    if not enabled():
        return "disabled"
    if _failures_since_flush > 0:
        return "measurement_failure"
    if sum(_ops.values()) == 0 and sum(_unmeasured_ops.values()) == 0:
        return "enabled_idle"
    return "enabled_recording"


def snapshot(now: datetime | None = None) -> dict[str, Any]:
    """A bounded, privacy-safe aggregate of the current UTC month."""
    moment = _now(now)
    if not enabled():
        return {
            "enabled": False,
            "status": "disabled",
            "coverage": "unknown",
            "month": moment.strftime("%Y-%m"),
            "measured_bytes": 0,
            "measured_outbound_bytes": 0,
            "measured_inbound_bytes": 0,
            "estimated_bytes": 0,
            "estimated_operations": 0,
            "operations": 0,
            "unmeasured_operations": 0,
            "failures": 0,
            "sources": {},
            "interval_seconds": 0,
        }
    try:
        with _lock:
            _ensure_month_locked(moment)
            measured = sum(_totals.values())
            outbound = sum(_totals[source] for source in OUTBOUND_SOURCES)
            inbound = sum(_totals[source] for source in INBOUND_SOURCES)
            return {
                "enabled": True,
                "status": _status_locked(),
                "coverage": _coverage_locked(),
                "month": _month_key,
                "measured_bytes": measured,
                "measured_outbound_bytes": outbound,
                "measured_inbound_bytes": inbound,
                "estimated_bytes": _estimated_bytes,
                "estimated_operations": _estimated_ops,
                "operations": sum(_ops.values()),
                "unmeasured_operations": sum(_unmeasured_ops.values()),
                "failures": _failures_since_flush,
                "interval_seconds": int(max(0.0, time.monotonic() - _interval_started)),
                "sources": {
                    source: {
                        "bytes": _totals[source],
                        "ops": _ops[source],
                        "unmeasured_ops": _unmeasured_ops[source],
                    }
                    for source in SOURCE_ORDER
                    if _totals[source] or _ops[source] or _unmeasured_ops[source]
                },
            }
    except Exception:  # noqa: BLE001
        _note_failure()
        return {
            "enabled": True,
            "status": "measurement_failure",
            "coverage": "unknown",
            "month": moment.strftime("%Y-%m"),
            "measured_bytes": 0,
            "measured_outbound_bytes": 0,
            "measured_inbound_bytes": 0,
            "estimated_bytes": 0,
            "estimated_operations": 0,
            "operations": 0,
            "unmeasured_operations": 0,
            "failures": 1,
            "sources": {},
            "interval_seconds": 0,
        }


def log_startup() -> None:
    """Emit ONE sanitized confirmation of the attribution state at boot."""
    global _startup_logged
    try:
        with _lock:
            if _startup_logged:
                return
            _startup_logged = True
            state = "enabled" if enabled() else "disabled"
        logger.info(
            "EGRESS_ATTRIBUTION_STARTUP status=%s flag=%s flush_s=%d "
            "measurement=payload_level telegram_upload=true telegram_download=true "
            "http_client=true wire_overhead=false",
            state,
            ENV_FLAG,
            int(flush_interval_seconds()),
        )
    except Exception:  # noqa: BLE001
        pass


def _source_fragment_locked() -> str:
    parts = []
    for source in SOURCE_ORDER:
        if _totals[source] or _ops[source] or _unmeasured_ops[source]:
            parts.append(
                f"{source}:{_totals[source]}/{_ops[source]}/u{_unmeasured_ops[source]}"
            )
    return ",".join(parts) if parts else "none"


def _policy_line(policy: dict[str, Any]) -> str:
    if not isinstance(policy, dict):
        return ""
    keys = (
        "month",
        "tier",
        "effective_bytes",
        "local_month_bytes",
        "render_bytes",
        "floor_bytes",
        "render_trusted",
        "render_coverage",
        "render_latest_data_at",
        "snapshot_age_s",
        "denials",
        "available",
    )
    parts = []
    for key in keys:
        if key in policy:
            value = policy[key]
            if isinstance(value, bool):
                value = "true" if value else "false"
            parts.append(f"{key}={value}")
    return "BANDWIDTH_POLICY_SNAPSHOT " + " ".join(parts)


def _reconciliation_line(policy: dict[str, Any]) -> str:
    """One sanitized Render-vs-measured view; never claims a cause."""
    if not isinstance(policy, dict):
        return ""
    render = policy.get("render_bytes")
    if isinstance(render, bool) or not isinstance(render, int):
        return ""
    try:
        rec = reconciliation(
            render,
            render_trusted=policy.get("render_trusted"),
            render_coverage=policy.get("render_coverage"),
        )
    except Exception:  # noqa: BLE001
        return ""
    return (
        "EGRESS_RECONCILIATION "
        f"render_outbound_bytes={rec['render_outbound_bytes']} "
        f"measured_outbound_bytes={rec['measured_outbound_bytes']} "
        f"estimated_bytes={rec['estimated_bytes']} "
        f"unmeasured_operations={rec['unmeasured_operations']} "
        f"unattributed_difference={rec['unattributed_difference']} "
        f"render_trusted={'true' if rec['render_trusted'] else 'false'} "
        f"render_coverage={rec['render_coverage']} "
        "difference_is_not_attribution=true"
    )


def flush(
    now: datetime | None = None,
    *,
    policy: dict[str, Any] | None = None,
) -> bool:
    """Emit ONE aggregate summary and start a new interval.  Never raises."""
    global _interval_started, _last_flush, _failures_since_flush
    moment = _now(now)
    try:
        with _lock:
            if not _month_key:
                _ensure_month_locked(moment)
            interval_s = int(max(0.0, time.monotonic() - _interval_started))
            measured = sum(_totals.values())
            outbound = sum(_totals[source] for source in OUTBOUND_SOURCES)
            inbound = sum(_totals[source] for source in INBOUND_SOURCES)
            operations = sum(_ops.values())
            unmeasured = sum(_unmeasured_ops.values())
            estimated = _estimated_bytes
            estimated_ops = _estimated_ops
            failures = _failures_since_flush
            status = _status_locked()
            coverage = _coverage_locked()
            sources = _source_fragment_locked()
            month = _month_key
            _interval_started = time.monotonic()
            _last_flush = _interval_started
            _failures_since_flush = 0
        logger.info(
            "EGRESS_ATTRIBUTION_SUMMARY status=%s coverage=%s enabled=%s month=%s "
            "interval_s=%d measured_bytes=%d measured_outbound_bytes=%d "
            "measured_inbound_bytes=%d estimated_bytes=%d estimated_operations=%d "
            "operations=%d unmeasured_operations=%d failures=%d sources=%s",
            status,
            coverage,
            "true" if enabled() else "false",
            month,
            interval_s,
            measured,
            outbound,
            inbound,
            estimated,
            estimated_ops,
            operations,
            unmeasured,
            failures,
            sources,
        )
        line = _policy_line(policy or {})
        if line:
            logger.info(line)
        reconciliation_line = _reconciliation_line(policy or {})
        if reconciliation_line:
            logger.info(reconciliation_line)
        return True
    except Exception:  # noqa: BLE001
        _note_failure()
        return False


def flush_due(now: datetime | None = None) -> bool:
    """True when the aggregate summary is due (disabled attribution is never due)."""
    if not enabled():
        return False
    try:
        return (time.monotonic() - _last_flush) >= flush_interval_seconds()
    except Exception:  # noqa: BLE001
        return False


def maybe_flush(
    now: datetime | None = None,
    *,
    policy: dict[str, Any] | None = None,
) -> bool:
    """Flush only when due, so an existing 30s heartbeat creates no log spam."""
    if not flush_due(now):
        return False
    return flush(now, policy=policy)


def reconciliation(
    render_outbound_bytes: int | None = None,
    *,
    render_trusted: bool | None = None,
    render_coverage: str | None = None,
) -> dict[str, Any]:
    """Compare Render's service metric with measured application payload bytes.

    The difference is returned as a neutral ``unattributed_difference``: it may
    contain MTProto/framing overhead, content encoding, retransmissions,
    response egress, traffic this process did not instrument, or an incomplete
    Render measurement.  It is never attributed to a provider by this function.
    """
    snap = snapshot()
    measured = int(snap.get("measured_outbound_bytes") or 0)
    difference: int | None = None
    if isinstance(render_outbound_bytes, int) and not isinstance(render_outbound_bytes, bool):
        difference = int(render_outbound_bytes) - measured
    return {
        "render_outbound_bytes": render_outbound_bytes,
        "render_trusted": render_trusted,
        "render_coverage": render_coverage,
        "measured_outbound_bytes": measured,
        "measured_inbound_bytes": int(snap.get("measured_inbound_bytes") or 0),
        "estimated_bytes": int(snap.get("estimated_bytes") or 0),
        "unmeasured_operations": int(snap.get("unmeasured_operations") or 0),
        "coverage": snap.get("coverage"),
        "attribution_status": snap.get("status"),
        "unattributed_difference": difference,
        "difference_is_not_attribution": True,
    }


# ── HTTP client instrumentation (public httpx API only) ──

_AI_HOST_SUFFIXES = (
    "openai.com",
    "openrouter.ai",
    "groq.com",
    "cerebras.ai",
    "mistral.ai",
    "together.xyz",
    "fireworks.ai",
    "sambanova.ai",
    "nvidia.com",
    "cohere.ai",
    "cohere.com",
    "siliconflow.cn",
    "z.ai",
    "bynara.id",
    "cloudflare.com",
    "generativelanguage.googleapis.com",
    "googleapis.com",
)
_SEARCH_HOST_SUFFIXES = ("ydc-index.io", "you.com")
_STT_PATH_MARKERS = ("audio/transcriptions", "audio/transcribe", "/asr/", "/stt")
_TTS_PATH_MARKERS = ("audio/speech", "/tts", "/synthesize")

_supabase_host_cache: tuple[str, str] = ("", "")


def _supabase_host() -> str:
    global _supabase_host_cache
    try:
        raw = os.getenv("SUPABASE_URL", "") or ""
        if raw == _supabase_host_cache[0]:
            return _supabase_host_cache[1]
        host = (urlparse(raw).hostname or "").lower()
        _supabase_host_cache = (raw, host)
        return host
    except Exception:  # noqa: BLE001
        return ""


def _host_matches(host: str, suffix: str) -> bool:
    return host == suffix or host.endswith("." + suffix)


def classify_request(request: Any) -> str:
    """Map one request to a bounded source; the URL itself is never recorded."""
    override = _source_override.get()
    if override in _SOURCE_SET:
        return override
    try:
        url = request.url
        host = (url.host or "").lower()
        path = (url.path or "").lower()
    except Exception:  # noqa: BLE001
        return SOURCE_OTHER_HTTP

    if _host_matches(host, "api.render.com"):
        return SOURCE_RENDER_API
    supabase = _supabase_host()
    if supabase and _host_matches(host, supabase):
        if path.startswith("/storage/v1/") or "/storage/v1/object" in path:
            return SOURCE_SUPABASE_STORAGE
        return SOURCE_SUPABASE_REST
    if any(marker in path for marker in _STT_PATH_MARKERS):
        return SOURCE_STT_PROVIDER
    if any(marker in path for marker in _TTS_PATH_MARKERS):
        return SOURCE_TTS_PROVIDER
    if "tts." in host and "speechmatics" in host:
        return SOURCE_TTS_PROVIDER
    if "asr." in host and "speechmatics" in host:
        return SOURCE_STT_PROVIDER
    if any(_host_matches(host, suffix) for suffix in _SEARCH_HOST_SUFFIXES):
        return SOURCE_AI_SEARCH
    if any(_host_matches(host, suffix) for suffix in _AI_HOST_SUFFIXES):
        return SOURCE_AI_PROVIDER
    return SOURCE_OTHER_HTTP


def _request_body_size(request: Any) -> tuple[int | None, bool]:
    """``(bytes, exact)`` for a request body already materialized by httpx."""
    try:
        content = request.content
    except Exception:  # noqa: BLE001 — a streaming request has no materialized body
        content = None
    if content:
        return len(content), True
    if content is not None:
        return 0, True
    raw_length = request.headers.get("content-length")
    if raw_length is not None:
        try:
            value = int(raw_length)
        except (TypeError, ValueError):
            return None, False
        if value >= 0:
            return value, True
    return None, False


class _CountingAsyncStream(httpx.AsyncByteStream):
    """Delegate an async byte stream, counting what the CALLER consumes."""

    def __init__(self, stream: Any, source: str) -> None:
        self._stream = stream
        self._source = source
        self._total = 0
        self._finished = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        async for chunk in self._stream:
            if chunk:
                self._total += len(chunk)
            yield chunk
        self._finish()

    async def aclose(self) -> None:
        try:
            close = getattr(self._stream, "aclose", None)
            if close is not None:
                await close()
        finally:
            self._finish()

    def _finish(self) -> None:
        if self._finished:
            return
        self._finished = True
        if self._total:
            record_operation(self._source, self._total)
        else:
            # A closed-but-unconsumed stream is NOT a measured zero.
            record_unmeasured(self._source)


class _CountingSyncStream(httpx.SyncByteStream):
    """Delegate a sync byte stream, counting what the CALLER consumes."""

    def __init__(self, stream: Any, source: str) -> None:
        self._stream = stream
        self._source = source
        self._total = 0
        self._finished = False

    def __iter__(self) -> Iterator[bytes]:
        for chunk in self._stream:
            if chunk:
                self._total += len(chunk)
            yield chunk
        self._finish()

    def close(self) -> None:
        try:
            close = getattr(self._stream, "close", None)
            if close is not None:
                close()
        finally:
            self._finish()

    def _finish(self) -> None:
        if self._finished:
            return
        self._finished = True
        if self._total:
            record_operation(self._source, self._total)
        else:
            record_unmeasured(self._source)


def _attach_response(response: Any, source: str, *, is_async: bool) -> None:
    try:
        if getattr(response, "is_stream_consumed", False):
            try:
                size = len(response.content)
            except Exception:  # noqa: BLE001
                record_unmeasured(source)
                return
            record_operation(source, size)
            return
        stream = getattr(response, "stream", None)
        if stream is None:
            record_unmeasured(source)
            return
        if is_async and hasattr(stream, "__aiter__"):
            response.stream = _CountingAsyncStream(stream, source)
        elif not is_async and hasattr(stream, "__iter__"):
            response.stream = _CountingSyncStream(stream, source)
        else:
            record_unmeasured(source)
    except Exception:  # noqa: BLE001
        _note_failure()


async def _async_send(self: Any, request: Any, **kwargs: Any) -> Any:
    source = classify_request(request)
    size, exact = _request_body_size(request)
    if exact and size:
        _record_request_bytes(source, size)
    elif not exact:
        record_unmeasured(source)
    try:
        response = await _original_async_send(self, request, **kwargs)
    except Exception:  # noqa: BLE001 — no response was received
        record_unmeasured(source)
        raise
    _attach_response(response, source, is_async=True)
    return response


def _sync_send(self: Any, request: Any, **kwargs: Any) -> Any:
    source = classify_request(request)
    size, exact = _request_body_size(request)
    if exact and size:
        _record_request_bytes(source, size)
    elif not exact:
        record_unmeasured(source)
    try:
        response = _original_sync_send(self, request, **kwargs)
    except Exception:  # noqa: BLE001
        record_unmeasured(source)
        raise
    _attach_response(response, source, is_async=False)
    return response


def install_httpx_instrumentation() -> bool:
    """Install the ONE process-wide httpx hook (idempotent, enabled-only)."""
    global _original_async_send, _original_sync_send, _installed
    if _installed:
        return True
    if not enabled():
        return False
    try:
        import httpx

        _original_async_send = httpx.AsyncClient.send
        _original_sync_send = httpx.Client.send
        httpx.AsyncClient.send = _async_send
        httpx.Client.send = _sync_send
        _installed = True
        return True
    except Exception:  # noqa: BLE001
        _note_failure()
        return False


def uninstall_httpx_instrumentation() -> None:
    """Restore httpx for tests; harmless when instrumentation is not installed."""
    global _installed
    if not _installed:
        return
    try:
        import httpx

        httpx.AsyncClient.send = _original_async_send
        httpx.Client.send = _original_sync_send
    except Exception:  # noqa: BLE001
        pass
    finally:
        _installed = False


def reset_for_tests() -> None:
    """Reset process-local counters (tests only; never called by the runtime)."""
    global _interval_started, _last_flush, _failures_since_flush, _startup_logged
    global _month_key, _estimated_bytes, _estimated_ops
    with _lock:
        _month_key = ""
        _totals.clear()
        _ops.clear()
        _unmeasured_ops.clear()
        _estimated_bytes = 0
        _estimated_ops = 0
        _interval_started = time.monotonic()
        _last_flush = _interval_started
        _failures_since_flush = 0
        _startup_logged = False


@contextlib.contextmanager
def source_scope(source: str):
    """Label requests made inside this block with a known bounded source."""
    if source not in _SOURCE_SET:
        yield
        return
    token = _source_override.set(source)
    try:
        yield
    finally:
        _source_override.reset(token)
