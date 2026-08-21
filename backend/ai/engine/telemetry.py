"""
AI execution telemetry — the normalized record contract.

Every AI request (provider path AND the local deterministic fast path)
is normalized by the Dispatcher into ONE ``AIExecutionRecord`` and handed
to the process-wide ``telemetry`` store. This module is the single
source of truth for AI execution telemetry — providers never maintain
their own ad-hoc status/token implementations; provider-specific usage
fields are already normalized into ``ProviderResponse.usage`` upstream
and land here through ``EngineResult``.

Token accuracy contract (never fabricate):
  - ``token_source="actual"``      — the provider reported authoritative usage.
  - ``token_source="estimated"``   — only a local character-based estimate exists;
                                     every user-facing rendering marks it (``≈``).
  - ``token_source="unavailable"`` — no usage information exists; UI shows
                                     "Unavailable" instead of inventing numbers.

Storage is deliberately ephemeral (bounded RAM deque, no database): the
project rule forbids new schema surfaces for cosmetic telemetry. The
optional show-telemetry chat preference is likewise RAM-only.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

_TOKEN_SOURCE_ACTUAL = "actual"
_TOKEN_SOURCE_ESTIMATED = "estimated"
_TOKEN_SOURCE_UNAVAILABLE = "unavailable"

# Human-readable failure reasons keyed by the provider layer's
# ``failure_type`` classification. Raw provider errors stay in logs;
# users see only these short reasons (full detail stays behind the
# diagnostics panel).
_FAILURE_REASONS: dict[str, str] = {
    "timeout": "Timeout",
    "rate_limited": "Rate limited",
    "auth": "Sign-in failed",
    "model_not_found": "Model unavailable",
    "server": "Provider error",
    "network": "Connection failed",
    "request": "Invalid request",
    "blocked": "Blocked",
}


@dataclass(frozen=True)
class AIExecutionRecord:
    """Normalized result of ONE AI request. Provider-independent."""

    timestamp: str = ""
    provider: str = ""
    model: str = ""
    status: str = "failed"          # "success" | "failed"
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    token_source: str = _TOKEN_SOURCE_UNAVAILABLE
    context_tokens: int = 0         # input tokens actually consumed by the request
    max_context: int = 0            # 0 = unknown (never invent a limit)
    latency: float = 0.0
    retry_count: int = 0
    fallback_used: bool = False
    tool_call_count: int = 0
    finish_state: str = ""
    error_reason: str = ""          # short, human-readable ("Timeout")
    error_detail: str = ""          # sanitized longer detail (diagnostics level)
    owner_id: int = 0


def humanize_failure(failure_type: str, raw_error: str = "") -> str:
    """Map an internal failure classification to a short user-facing reason."""
    reason = _FAILURE_REASONS.get((failure_type or "").strip().lower(), "")
    if reason:
        return reason
    lowered = (raw_error or "").lower()
    if "timeout" in lowered or "timed out" in lowered:
        return "Timeout"
    if "rate" in lowered or "429" in lowered:
        return "Rate limited"
    if "401" in lowered or "403" in lowered or "auth" in lowered:
        return "Sign-in failed"
    return "Unavailable"


def format_tokens(count: int) -> str:
    """Compact token count: 487 → "487", 2671 → "2.7k", 96912 → "96.9k"."""
    if count < 1000:
        return str(count)
    if count < 100_000:
        value = f"{count / 1000:.1f}".rstrip("0").rstrip(".")
        return f"{value}k"
    return f"{round(count / 1000)}k"


def format_tokens_exact(count: int) -> str:
    """Exact grouped count for the Details panel: 2184 → "2,184"."""
    return f"{count:,}"


def format_latency(seconds: float) -> str:
    """Compact latency: 2.734 → "2.7s", 0.8 → "0.8s", 15.4 → "15s"."""
    if seconds < 10:
        return f"{seconds:.1f}s"
    return f"{seconds:.0f}s"


def format_latency_exact(seconds: float) -> str:
    """Precise latency for Details: 2.734 → "2.734s"."""
    return f"{seconds:.3f}s"


def format_time_of(iso_timestamp: str) -> str:
    """Short clock time from an ISO timestamp: "...T12:41:33" → "12:41"."""
    return (iso_timestamp or "")[11:16]


def compact_telemetry_line(record: "AIExecutionRecord | None") -> str:
    """One restrained telemetry line for the chat view: "2.7s · 2.6k tokens".

    Estimated usage is explicitly marked with "≈"; unavailable usage is
    omitted entirely rather than invented.
    """
    if record is None:
        return ""
    parts = [format_latency(record.latency)]
    if record.token_source == _TOKEN_SOURCE_ESTIMATED:
        parts.append(f"≈{format_tokens(record.total_tokens)} tokens")
    elif record.token_source == _TOKEN_SOURCE_ACTUAL:
        parts.append(f"{format_tokens(record.total_tokens)} tokens")
    return " · ".join(parts)


class ExecutionTelemetry:
    """Bounded in-RAM store of normalized AI execution records."""

    def __init__(self, maxlen: int = 200) -> None:
        self._records: deque[AIExecutionRecord] = deque(maxlen=maxlen)
        self._show_telemetry: dict[int, bool] = {}

    # ── Recording ──

    def record_execution(self, result: Any, owner_id: int = 0) -> AIExecutionRecord:
        """Normalize an ``EngineResult`` into a record and store it.

        Reads ONLY the documented metadata keys the Dispatcher writes
        (``token_source``, ``retry_count``, ``fallback_used``,
        ``failure_type``, ``finish_state``). ``owner_id`` is supplied by the
        caller (``EngineResult`` has no owner field). Never raises.
        """
        metadata: dict[str, Any] = getattr(result, "metadata", None) or {}
        success = bool(getattr(result, "success", False))
        token_source = str(metadata.get("token_source", "")) or _TOKEN_SOURCE_UNAVAILABLE
        failure_reason = ""
        error_detail = ""
        if not success:
            failure_type = str(metadata.get("failure_type", ""))
            raw_error = ""
            errors = getattr(result, "errors", None) or []
            if errors:
                raw_error = str(errors[-1])
            failure_reason = humanize_failure(failure_type, raw_error)
            error_detail = raw_error[:300]
        record = AIExecutionRecord(
            timestamp=datetime.now(timezone.utc).isoformat(),
            provider=str(getattr(result, "provider", "") or ""),
            model=str(getattr(result, "model", "") or ""),
            status="success" if success else "failed",
            input_tokens=int(getattr(result, "prompt_tokens", 0) or 0),
            output_tokens=int(getattr(result, "completion_tokens", 0) or 0),
            total_tokens=int(getattr(result, "total_tokens", 0) or 0),
            token_source=token_source,
            context_tokens=int(metadata.get("context_tokens", getattr(result, "prompt_tokens", 0)) or 0),
            latency=float(getattr(result, "latency", 0.0) or 0.0),
            retry_count=int(metadata.get("retry_count", 0) or 0),
            fallback_used=bool(metadata.get("fallback_used", False)),
            tool_call_count=int(metadata.get("tool_call_count", 0) or 0),
            finish_state=str(metadata.get("finish_state", "")),
            error_reason=failure_reason,
            error_detail=error_detail,
            owner_id=int(owner_id or 0),
        )
        self._records.append(record)
        return record

    # ── Access ──

    def last(self) -> AIExecutionRecord | None:
        return self._records[-1] if self._records else None

    def recent(self, count: int = 20) -> list[AIExecutionRecord]:
        if count <= 0 or not self._records:
            return []
        return list(self._records)[-count:]

    def summary(self, *, hours: float | None = None, since_midnight_utc: bool = False) -> dict[str, int]:
        """Aggregate records over a window.

        ``since_midnight_utc=True`` aggregates the current UTC calendar day
        ("Today"); otherwise ``hours`` defines a rolling window (24h ≈ Today,
        7d, 30d). Aggregation is honest about what the ephemeral store holds:
        it can only ever cover this process's lifetime.
        """
        now = datetime.now(timezone.utc)
        if since_midnight_utc:
            start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        elif hours is not None:
            start = now - timedelta(hours=hours)
        else:
            start = now.replace(hour=0, minute=0, second=0, microsecond=0)

        totals = {
            "requests": 0,
            "success": 0,
            "failed": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "fallbacks": 0,
            "retries": 0,
        }
        for rec in self._records:
            try:
                ts = datetime.fromisoformat(rec.timestamp)
            except ValueError:
                continue
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            if ts < start:
                continue
            totals["requests"] += 1
            if rec.status == "success":
                totals["success"] += 1
            else:
                totals["failed"] += 1
            totals["input_tokens"] += rec.input_tokens
            totals["output_tokens"] += rec.output_tokens
            totals["total_tokens"] += rec.total_tokens
            if rec.fallback_used:
                totals["fallbacks"] += 1
            totals["retries"] += rec.retry_count
        return totals

    # ── Chat telemetry preference (RAM-only by design) ──

    def get_telemetry_pref(self, owner_id: int) -> bool:
        return bool(self._show_telemetry.get(owner_id, False))

    def set_telemetry_pref(self, owner_id: int, enabled: bool) -> None:
        self._show_telemetry[owner_id] = bool(enabled)

    def reset_for_tests(self) -> None:
        self._records.clear()
        self._show_telemetry.clear()


telemetry = ExecutionTelemetry()
