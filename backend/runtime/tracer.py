"""
Structured lifecycle event tracer.

Every important runtime event is logged with a consistent, grep-friendly tag
so Render logs can be searched by event type:

    [TRACE] SELF_CONNECTED gen=1 user=Parham id=123456 t=12345678.123
    [TRACE] SELF_DISCONNECTED gen=1 reason=run_until_disconnected_returned t=12345690.456
    [TRACE] SELF_RUN_LOOP_EXITED gen=1 t=12345691.789
    [TRACE] WATCHDOG_RECOVERY reason=full attempt=1 backoff_delay=4.0s t=12345695.000
    [TRACE] SELF_RECONNECTED gen=2 t=12345710.000

Every event carries a monotonic timestamp (t=<seconds>) so you can compute
exact millisecond deltas between any two events.

All trace events go through a single function so the format is uniform.
Every exception trace uses traceback.format_exception() (never str(exc) alone).
"""
import logging
import time
import traceback
from datetime import datetime, timezone

logger = logging.getLogger("backend.tracer")

_TRACE_TAG = "[TRACE]"

_monotonic_base = time.monotonic()


def _mono() -> float:
    return time.monotonic() - _monotonic_base


def trace(event: str, **fields) -> None:
    parts = [event]
    for key, value in fields.items():
        if value is None or value == "":
            continue
        parts.append(f"{key}={value}")
    parts.append(f"t={_mono():.3f}")
    logger.warning("%s %s", _TRACE_TAG, " ".join(parts))


def trace_exception(event: str, exc: BaseException, **fields) -> None:
    parts = [event]
    for key, value in fields.items():
        if value is None or value == "":
            continue
        parts.append(f"{key}={value}")
    parts.append(f"exc_type={type(exc).__name__}")
    parts.append(f"exc_repr={exc!r}")
    parts.append(f"t={_mono():.3f}")
    logger.warning("%s %s", _TRACE_TAG, " ".join(parts))
    tb_text = traceback.format_exception(type(exc), exc, exc.__traceback__)
    for line in "".join(tb_text).rstrip().splitlines():
        logger.warning("%s   %s", _TRACE_TAG, line)


def trace_task_crash(task_name: str, exc: BaseException, runtime_state: str = "") -> None:
    trace_exception(
        "TASK_CRASHED",
        exc,
        task=task_name,
        runtime_state=runtime_state or "unknown",
    )
    try:
        from backend.runtime.crash_diagnostics import capture_task_exception
        capture_task_exception(task_name, exc)
    except Exception:
        pass


def trace_task_cancelled(task_name: str, runtime_state: str = "") -> None:
    trace("TASK_CANCELLED", task=task_name, runtime_state=runtime_state or "unknown")


def trace_handler_exception(handler_name: str, exc: BaseException, runtime_state: str = "") -> None:
    trace_exception(
        "HANDLER_EXCEPTION",
        exc,
        handler=handler_name,
        runtime_state=runtime_state or "unknown",
    )


def trace_uncaught(exc: BaseException) -> None:
    trace_exception("UNCAUGHT_EXCEPTION", exc)
    try:
        from backend.runtime.crash_diagnostics import record_exception
        record_exception(exc, source="trace_uncaught")
    except Exception:
        pass


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def monotonic_seconds() -> float:
    return _mono()
