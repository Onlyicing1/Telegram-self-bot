"""
Crash report generator — produces structured crash reports for fatal exceptions.

Reuses backend.runtime.tracer for log-based trace output and
backend.diagnostics for the in-memory event ring.
"""
from __future__ import annotations

import traceback
from datetime import datetime, timezone
from typing import Any

from backend.runtime.tracer import trace_exception, monotonic_seconds


def generate_crash_report(
    component: str,
    exc: BaseException,
    *,
    trace_id: str = "",
    active_provider: str = "",
    active_session: str = "",
) -> dict[str, Any]:
    """Generate a structured crash report for a fatal exception.

    Logs the exception via tracer.trace_exception and returns a dict
    with all relevant context for diagnostics.
    """
    if not trace_id:
        trace_id = f"CRASH-{monotonic_seconds():.3f}"

    trace_exception(
        "FATAL_EXCEPTION",
        exc,
        component=component,
        trace_id=trace_id,
        active_provider=active_provider,
        active_session=active_session,
    )

    from backend.health import snapshot as health_snap
    from backend.observability.runtime_status import runtime_status

    snap = health_snap()
    runtime = runtime_status()

    return {
        "trace_id": trace_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "component": component,
        "exception_type": type(exc).__name__,
        "exception_message": str(exc),
        "stack_trace": "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
        "runtime_state": snap.get("runtime_state", "unknown"),
        "active_provider": active_provider,
        "active_session": active_session,
        "memory_mb": runtime.get("memory_mb", 0),
        "pending_tasks": runtime.get("pending_tasks", 0),
        "uptime_s": snap.get("uptime_s"),
        "restart_count": snap.get("restart_count", 0),
        "client_generation": snap.get("client_generation", 0),
    }


def format_crash_report(report: dict[str, Any]) -> str:
    """Format a crash report dict as a readable string."""
    lines = [
        f"=== CRASH REPORT ===",
        f"Trace ID:     {report.get('trace_id', '')}",
        f"Timestamp:    {report.get('timestamp', '')}",
        f"Component:    {report.get('component', '')}",
        f"Exception:    {report.get('exception_type', '')}: {report.get('exception_message', '')}",
        f"Runtime State: {report.get('runtime_state', '')}",
        f"Active Provider: {report.get('active_provider', '')}",
        f"Active Session:  {report.get('active_session', '')}",
        f"Memory (MB):     {report.get('memory_mb', 0)}",
        f"Pending Tasks:   {report.get('pending_tasks', 0)}",
        f"Uptime (s):      {report.get('uptime_s')}",
        f"Restart Count:   {report.get('restart_count', 0)}",
        f"Client Gen:      {report.get('client_generation', 0)}",
        f"",
        f"--- Stack Trace ---",
        report.get("stack_trace", ""),
    ]
    return "\n".join(lines)
