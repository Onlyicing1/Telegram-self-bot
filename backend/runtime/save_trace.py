"""Opt-in, bounded line-level tracing for ONE Save attempt.

The Save path is diagnosed from Render logs, so it needs execution evidence,
not guesses. This module provides it in two layers:

* **stage events** — explicit ``[SAVE_TRACE]`` records emitted by the Save code
  itself (handler → link parsing → peer resolution → Deep Save → transfer →
  persistence). They are cheap, carry a stage/branch/outcome classification and
  are the primary correlation points.
* **line events** — real ``sys.settrace`` events for the executed statements of
  the Save path only, so a refusal can be pinned to the exact line and condition
  that produced it.

Hard constraints this module keeps:

* **OFF by default.** Nothing is installed and nothing is logged unless
  ``SAVE_TRACE`` is truthy, and then only for the duration of one Save attempt.
* **Scoped.** ``LINE_SCOPE`` lists the exact modules/functions that may emit
  line events, and every event must come from the task that started the trace.
  Unrelated asyncio tasks, other chats and other modules are never recorded.
  ``sys.settrace`` is thread-wide — that is why the task check is mandatory and
  not merely cosmetic. One line-traced attempt runs at a time.
* **Bounded.** A maximum number of events and a maximum duration; reaching
  either restores the previous tracing state immediately and records ONE
  ``TRUNCATED`` event.
* **Restored.** The previous tracer is reinstated on success, exception,
  cancellation and timeout alike (``with trace.capturing():``), so tracing can
  never be left installed.
* **Never authoritative.** Every failure here is swallowed: a trace can never
  change a Save result, and logging never raises into the caller.
* **Safe.** No link text, chat/user ids, message text, file bytes, object reprs
  or absolute paths are logged. Cross-event identity uses a keyed, truncated
  fingerprint (``fp:<hex>``) that is only comparable inside one process.

This is instrumentation only: it deliberately does not decide, classify or
alter anything about the Save pipeline.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import sys
import time
import uuid
from typing import Any

logger = logging.getLogger("backend.save_trace")

#: Stable grep prefix for every record this module writes.
TRACE_PREFIX = "[SAVE_TRACE]"

#: The ONE switch. Values ``1/true/yes/on`` (case-insensitive) enable tracing.
ENV_FLAG = "SAVE_TRACE"

#: Default bounds. A save attempt can never emit more than this many records
#: (the final ``TRUNCATED`` record is the only exception) nor keep tracing past
#: the duration bound.
DEFAULT_MAX_EVENTS = 3000
DEFAULT_MAX_DURATION_S = 120.0

# ── event kinds ─────────────────────────────────────────────────────────────
ENTER = "ENTER"
LINE = "LINE"
BRANCH = "BRANCH"
CALL_START = "CALL_START"
CALL_END = "CALL_END"
RETURN = "RETURN"
EXCEPTION = "EXCEPTION"
CLEANUP = "CLEANUP"
NOT_REACHED = "NOT_REACHED"
TRUNCATED = "TRUNCATED"

# ── outcome classification ──────────────────────────────────────────────────
OUTCOME_OK = "ok"
OUTCOME_FAILED = "failed"
OUTCOME_REFUSED = "refused"
OUTCOME_NOT_REACHED = "not_reached"
OUTCOME_INCONCLUSIVE = "inconclusive"

#: The (module, function) pairs whose executed lines may be recorded. Paths are
#: repository-relative and matched by suffix, so a checkout path never leaks
#: into a record. Only these functions are traced — not their whole modules.
LINE_SCOPE: dict[str, frozenset[str]] = {
    "backend/services/save_service.py": frozenset({
        "parse_link_target",
        "_parse_tg_openmessage",
        "_parse_http_link",
        "_parse_positive_id",
        "_resolve_link_peer",
        "_dialog_entity",
        "_fetch_linked_message",
        "execute_link_save",
        "execute_save",
        "_deep_save_one",
        "_deep_save_album",
        "media_ttl_seconds",
        "ephemeral_media_kind",
        "ephemeral_refusal",
        "_extract_source_media",
    }),
    "backend/bot/handlers/save.py": frozenset({
        "_save_link_input_handler",
        "_save_reply_wait_handler",
        "_start_status",
        "_delete_trigger_message",
        "success_action_rows",
        "_link_save_result_outcome",
    }),
}

_TRUTHY = frozenset({"1", "true", "yes", "on"})

_RESERVED_FIELDS = frozenset({
    "id", "seq", "elapsed_ms", "stage", "kind", "mod", "func", "line",
    "outcome", "error",
})

#: Per-process key for the identity fingerprints in the records. A fingerprint
#: is comparable inside one process only — which is exactly what correlating
#: events of one Render instance needs.
_FINGERPRINT_KEY = os.urandom(8)

#: How many line-traced attempts may be installed at once. ``sys.settrace`` is
#: thread-wide, so a second concurrent install would silently replace the first
#: and the first would restore the wrong state on exit.
_active_line_traces = 0


def save_trace_enabled() -> bool:
    """Whether ``SAVE_TRACE`` enables instrumentation for the next attempt."""
    try:
        return (os.getenv(ENV_FLAG, "") or "").strip().casefold() in _TRUTHY
    except Exception:  # noqa: BLE001 — configuration can never break a save
        return False


def fingerprint(value: Any) -> str:
    """A short keyed identity for cross-event correlation, never the raw id."""
    try:
        raw = str(value).encode("utf-8", "replace")
    except Exception:  # noqa: BLE001 — a bad value must not break a trace
        return "fp:unknown"
    digest = hashlib.blake2s(raw, key=_FINGERPRINT_KEY, digest_size=4).hexdigest()
    return f"fp:{digest}"


def _safe_scalar(value: Any) -> str | None:
    """Render one field value safely: no reprs, no newlines, bounded length."""
    if value is None:
        return None
    try:
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, int):
            return str(value)
        if isinstance(value, float):
            return f"{value:.1f}"
        if isinstance(value, str):
            # One whitespace-free token, so ``key=value`` stays machine-parsable
            # and a multi-line message can never break a log line.
            text = "_".join(value.split())
            return text[:120]
        return f"<{type(value).__name__}>"
    except Exception:  # noqa: BLE001 — a weird value is simply omitted
        return None


def _module_hint(filename: str) -> str:
    """The repository-relative module a frame belongs to (never its full path)."""
    try:
        normalized = os.path.normpath(str(filename)).replace("\\", "/")
    except Exception:  # noqa: BLE001
        return "unknown"
    for scoped in LINE_SCOPE:
        if normalized.endswith(scoped):
            return scoped
    return os.path.basename(normalized)


def _in_scope(code) -> bool:
    functions = LINE_SCOPE.get(_module_hint(code.co_filename))
    return bool(functions) and code.co_name in functions


class _Capture:
    """``with trace.capturing():`` — installs the tracer, always restores it.

    Nested blocks on the SAME trace (``execute_link_save`` → ``execute_save``)
    share one installation: the tracer is installed when the outermost block
    opens and restored when it closes, so an inner block can never leave the
    outer stages untraced.
    """

    def __init__(self, trace: "SaveTrace") -> None:
        self._trace = trace

    def __enter__(self) -> "SaveTrace":
        self._trace._depth += 1
        if self._trace._depth == 1:
            self._trace._install()
        return self._trace

    def __exit__(self, exc_type, exc, tb) -> bool:
        try:
            if exc_type is not None:
                self._trace.event(
                    "trace", EXCEPTION, outcome=OUTCOME_INCONCLUSIVE,
                    error=getattr(exc_type, "__name__", "Exception"),
                )
            if self._trace._depth <= 1:
                self._trace.event(
                    "trace", CLEANUP, outcome=OUTCOME_OK,
                    events=self._trace.recorded, truncated=self._trace.truncated,
                )
        finally:
            self._trace._depth = max(0, self._trace._depth - 1)
            if self._trace._depth == 0:
                self._trace._restore()
        return False  # never swallows the caller's exception


class SaveTrace:
    """One Save attempt's trace. A disabled trace is a cheap no-op."""

    def __init__(
        self,
        operation: str = "save",
        *,
        enabled: bool | None = None,
        max_events: int | None = None,
        max_duration_s: float | None = None,
    ) -> None:
        self.operation = str(operation or "save")
        self.enabled = save_trace_enabled() if enabled is None else bool(enabled)
        self._max_events = int(
            DEFAULT_MAX_EVENTS if max_events is None else max_events
        )
        self._max_duration_s = float(
            DEFAULT_MAX_DURATION_S if max_duration_s is None else max_duration_s
        )
        self.trace_id = uuid.uuid4().hex[:8]
        self._started = time.monotonic()
        self._seq = 0
        self._truncated: str | None = None
        self._installed = False
        self._depth = 0
        self._previous_trace: Any = None
        self._task: Any = None
        self._scope_cache: dict[Any, bool] = {}
        # Stored once: a bound method is re-created on every attribute access,
        # so identity checks against ``sys.gettrace()`` need this exact object.
        self._global_ref = self._trace_global
        self._local_ref = self._trace_local

    # ── construction ────────────────────────────────────────────────────────

    @classmethod
    def begin(cls, operation: str = "save", **kwargs) -> "SaveTrace":
        """A trace for one attempt — disabled unless ``SAVE_TRACE`` is set."""
        return cls(operation, **kwargs)

    # ── observable state (tests + the CLEANUP record) ────────────────────────

    @property
    def recorded(self) -> int:
        return self._seq

    @property
    def truncated(self) -> str | None:
        return self._truncated

    @property
    def elapsed_ms(self) -> float:
        return (time.monotonic() - self._started) * 1000.0

    # ── structured events ───────────────────────────────────────────────────

    def event(
        self,
        stage: str,
        kind: str,
        *,
        outcome: str | None = None,
        module: str | None = None,
        func: str | None = None,
        line: int | None = None,
        error: str | None = None,
        **fields: Any,
    ) -> None:
        """Record one stage event; bounded, sanitized and failure-contained."""
        if not self.enabled or self._truncated is not None:
            return
        if self._seq >= self._max_events:
            self._truncate("events")
            return
        if self.elapsed_ms > self._max_duration_s * 1000.0:
            self._truncate("duration")
            return
        self._write(
            stage, kind, outcome=outcome, module=module, func=func, line=line,
            error=error, fields=fields,
        )

    def enter(self, stage: str, func: str, **fields: Any) -> None:
        self.event(stage, ENTER, outcome=OUTCOME_OK, func=func, **fields)

    def branch(self, stage: str, name: str, matched: Any, **fields: Any) -> None:
        """Log a branch decision AND the non-sensitive evidence that made it."""
        self.event(
            stage, BRANCH, outcome=OUTCOME_OK, name=name, matched=matched, **fields
        )

    def returned(
        self, stage: str, func: str, outcome: str = OUTCOME_OK, **fields: Any
    ) -> None:
        self.event(stage, RETURN, outcome=outcome, func=func, **fields)

    def failed(self, stage: str, func: str, error: BaseException | str, **fields: Any) -> None:
        name = error if isinstance(error, str) else type(error).__name__
        self.event(stage, EXCEPTION, outcome=OUTCOME_FAILED, func=func, error=name, **fields)

    def not_reached(self, stage: str, reason: str, **fields: Any) -> None:
        self.event(
            stage, NOT_REACHED, outcome=OUTCOME_NOT_REACHED, reason=reason, **fields
        )

    # ── line tracing ────────────────────────────────────────────────────────

    def capturing(self) -> _Capture:
        """Install the scoped line tracer for the life of the ``with`` block."""
        return _Capture(self)

    def _install(self) -> None:
        global _active_line_traces
        if not self.enabled or self._installed or self._truncated is not None:
            return
        if _active_line_traces:
            # Another attempt already owns the thread-wide tracer. Stage events
            # still work; line events would belong to the wrong attempt.
            self.event(
                "trace", EXCEPTION, outcome=OUTCOME_INCONCLUSIVE,
                error="TraceAlreadyActive",
            )
            return
        try:
            self._task = asyncio.current_task()
        except RuntimeError:
            self._task = None
        if self._task is None:
            self.event(
                "trace", EXCEPTION, outcome=OUTCOME_INCONCLUSIVE,
                error="NoRunningTask",
            )
            return
        try:
            self._previous_trace = sys.gettrace()
            self._installed = True
            _active_line_traces += 1
            sys.settrace(self._global_ref)
        except Exception as exc:  # noqa: BLE001 — tracing is never required
            self._installed = False
            _active_line_traces = max(0, _active_line_traces - 1)
            self.event(
                "trace", EXCEPTION, outcome=OUTCOME_INCONCLUSIVE,
                error=type(exc).__name__,
            )

    def _restore(self) -> None:
        global _active_line_traces
        if not self._installed:
            return
        self._installed = False
        _active_line_traces = max(0, _active_line_traces - 1)
        try:
            sys.settrace(self._previous_trace)
        except Exception:  # noqa: BLE001 — restoration must not raise
            sys.settrace(None)

    def _trace_global(self, frame, event, arg):
        """Only 'call' events reach a global tracer: cheap scope + task filter."""
        if not self._installed or self._truncated is not None:
            return None
        code = frame.f_code
        cached = self._scope_cache.get(code)
        if cached is None:
            cached = _in_scope(code)
            self._scope_cache[code] = cached
        if not cached:
            return None
        if not self._owns_task():
            return None
        return self._local_ref

    def _owns_task(self) -> bool:
        try:
            return asyncio.current_task() is self._task
        except RuntimeError:
            return False

    def _trace_local(self, frame, event, arg):
        try:
            if event == "line":
                self.event(
                    "line", LINE,
                    module=_module_hint(frame.f_code.co_filename),
                    func=frame.f_code.co_name,
                    line=frame.f_lineno,
                )
            elif event == "exception":
                exc_type = arg[0] if arg else None
                self.event(
                    "line", EXCEPTION, outcome=OUTCOME_FAILED,
                    module=_module_hint(frame.f_code.co_filename),
                    func=frame.f_code.co_name,
                    line=frame.f_lineno,
                    error=getattr(exc_type, "__name__", "Exception"),
                )
        except Exception:  # noqa: BLE001 — a tracer must never break the frame
            pass
        return self._local_ref

    # ── writing ─────────────────────────────────────────────────────────────

    def _truncate(self, reason: str) -> None:
        """Stop recording, restore tracing, and say why in one final record."""
        if self._truncated is not None:
            return
        self._truncated = reason
        self._restore()
        self._seq += 1
        parts = [
            TRACE_PREFIX,
            f"id={self.trace_id}",
            f"seq={self._seq}",
            f"elapsed_ms={self.elapsed_ms:.1f}",
            "stage=trace",
            f"kind={TRUNCATED}",
            f"outcome={OUTCOME_INCONCLUSIVE}",
            f"reason={reason}",
            f"max_events={self._max_events}",
        ]
        self._log(" ".join(parts))

    def _write(
        self,
        stage: str,
        kind: str,
        *,
        outcome: str | None,
        module: str | None,
        func: str | None,
        line: int | None,
        error: str | None,
        fields: dict,
    ) -> None:
        self._seq += 1
        parts = [
            TRACE_PREFIX,
            f"id={self.trace_id}",
            f"seq={self._seq}",
            f"elapsed_ms={self.elapsed_ms:.1f}",
            f"stage={_safe_scalar(stage) or 'unknown'}",
            f"kind={kind}",
        ]
        if module:
            parts.append(f"mod={_safe_scalar(module)}")
        if func:
            parts.append(f"func={_safe_scalar(func)}")
        if line is not None:
            parts.append(f"line={int(line)}")
        if outcome:
            parts.append(f"outcome={_safe_scalar(outcome)}")
        if error:
            parts.append(f"error={_safe_scalar(error)}")
        for key in sorted(fields):
            if key in _RESERVED_FIELDS:
                continue
            rendered = _safe_scalar(fields[key])
            if rendered is None:
                continue
            parts.append(f"{key}={rendered}")
        self._log(" ".join(parts))

    @staticmethod
    def _log(message: str) -> None:
        """Log one record; logging failure must never reach the Save path."""
        try:
            logger.info("%s", message)
        except Exception:  # noqa: BLE001
            pass


def reset_state() -> None:
    """Drop the thread tracer (test isolation after an aborted attempt)."""
    global _active_line_traces
    _active_line_traces = 0
    try:
        sys.settrace(None)
    except Exception:  # noqa: BLE001
        pass
