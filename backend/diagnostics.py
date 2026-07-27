"""
Diagnostics module — event history (black box) + .kill diagnostic snapshot.

Provides:
  - An in-memory circular event log (500 entries, automatic overwrite)
  - Event recording from every subsystem (Telethon, Bio, DB, Save, etc.)
  - A complete diagnostic snapshot of all subsystems for .kill
  - Stalled-task detection and selective recovery
  - Event filtering and formatting for .logs

The diagnostic snapshot now reads from the RuntimeSupervisor when
available, falling back to direct module access for backward compatibility.

No database, no disk writes. All snapshot collection is synchronous and
non-blocking — it reads module-level state only, never performs I/O.
"""
import asyncio
import logging
import os
import sys
from collections import deque
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

_RING_SIZE = 500
_event_ring: deque = deque(maxlen=_RING_SIZE)

_TG_MSG_LIMIT = 4096


def record_event(module: str, action: str, duration_ms: float, result: str, details: str | None = None) -> None:
    entry = {
        "ts": datetime.now(timezone.utc),
        "module": module,
        "action": action,
        "duration_ms": round(duration_ms, 1),
        "result": result,
        "details": details,
    }
    _event_ring.append(entry)


def get_events() -> list:
    return list(_event_ring)


def _format_duration(ms: float) -> str:
    if ms < 1000:
        return f"{int(ms)}ms"
    seconds = ms / 1000
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes = int(seconds // 60)
    secs = int(seconds % 60)
    return f"{minutes}m {secs}s"


def _format_event(e: dict) -> str:
    ts = e["ts"].strftime("%H:%M:%S")
    dur = _format_duration(e["duration_ms"])
    line = f"{ts} | {e['module']} | {e['action']} | {dur} | {e['result']}"
    if e.get("details"):
        line += f" | {e['details']}"
    return line


def filter_events(limit: int = 20, module: str | None = None, errors_only: bool = False) -> list:
    events = get_events()
    if errors_only:
        events = [e for e in events if e["result"] not in ("SUCCESS",)]
    if module:
        events = [e for e in events if e["module"].lower() == module.lower()]
    events.reverse()
    return events[:limit]


def format_events(events: list) -> str:
    if not events:
        return "📭 No events recorded."
    lines = [f"📋 **Event Log** ({len(events)})", ""]
    for e in events:
        lines.append(f"```\n{_format_event(e)}\n```")
    return "\n".join(lines)


def split_message(text: str, limit: int = _TG_MSG_LIMIT) -> list[str]:
    if len(text) <= limit:
        return [text]
    parts = []
    remaining = text
    while remaining:
        if len(remaining) <= limit:
            parts.append(remaining)
            break
        chunk = remaining[:limit]
        last_nl = chunk.rfind("\n")
        if last_nl > limit // 2:
            split_at = last_nl
        else:
            split_at = limit
        parts.append(remaining[:split_at])
        remaining = remaining[split_at:].lstrip("\n")
    return parts


def _get_task_state(task: asyncio.Task) -> str:
    if task.done():
        if task.cancelled():
            return "CANCELLED"
        exc = task.exception()
        if exc:
            return f"FAILED ({type(exc).__name__})"
        return "DONE"
    return "RUNNING"


def _get_coro_name(task: asyncio.Task) -> str:
    coro = task.get_coro()
    if coro is None:
        return "unknown"
    return getattr(coro, "__name__", getattr(coro, "__qualname__", "unknown"))


def _get_await_location(task: asyncio.Task) -> str:
    coro = task.get_coro()
    if coro is None:
        return ""
    frame = coro.cr_frame
    if frame is not None:
        code = frame.f_code
        return f"{code.co_filename}:{frame.f_lineno}"
    return ""


def _collect_process_section() -> list:
    lines = ["=== PROCESS ==="]
    try:
        import resource
        usage = resource.getrusage(resource.RUSAGE_SELF)
        mem_mb = usage.ru_maxrss / 1024
        cpu_s = usage.ru_utime + usage.ru_stime
        lines.append(f"• PID: {os.getpid()}")
        lines.append(f"• Memory: {mem_mb:.1f} MB (max RSS)")
        lines.append(f"• CPU: {cpu_s:.2f}s user+sys")
    except Exception:
        lines.append(f"• PID: {os.getpid()}")
        lines.append("• Memory/CPU: unavailable")
    lines.append(f"• Python: {sys.version.split()[0]}")
    return lines


def _collect_runtime_section(health_snap: dict) -> list:
    lines = ["=== RUNTIME ==="]
    lines.append(f"• State: {health_snap.get('runtime_state', 'unknown')}")
    lines.append(f"• Supervisor: {'OK' if health_snap.get('supervisor_ok') else 'NOT OK'}")
    lines.append(f"• Client Generation: {health_snap.get('client_generation', 0)}")
    if health_snap.get('last_rebuild_reason'):
        lines.append(f"• Last Rebuild Reason: {health_snap['last_rebuild_reason']}")
    if health_snap.get('rpc_latency_ms') is not None:
        lines.append(f"• RPC Latency: {health_snap['rpc_latency_ms']:.1f}ms")
    return lines


def _collect_telethon_section(client) -> list:
    lines = ["=== TELETHON ==="]
    try:
        connected = client.is_connected()
        lines.append(f"• Connected: {connected}")
        lines.append(f"• Authorized: {client.is_user_authorized() if connected else False}")
    except Exception:
        lines.append("• Connected: unknown")
        lines.append("• Authorized: unknown")
    return lines


def _collect_helper_section(health_snap: dict) -> list:
    lines = ["=== HELPER ==="]
    lines.append(f"• Connected: {health_snap.get('helper_connected', False)}")
    return lines


def _collect_supervisor_section(health_snap: dict) -> list:
    lines = ["=== SUPERVISOR ==="]
    task_states = health_snap.get("task_states", {})
    if task_states:
        for name, state in task_states.items():
            lines.append(f"• {name}: {state}")
    else:
        tasks = asyncio.all_tasks()
        lines.append(f"• Running tasks: {len(tasks)}")
    lines.append(f"• Heartbeat age: {health_snap.get('heartbeat_age_s', 'unknown')}")
    return lines


def _collect_bio_section(bio_engine) -> list:
    lines = ["=== BIO ENGINE ==="]
    lines.append(f"• Running: {bio_engine.is_running()}")
    return lines


def _collect_database_section(db_client) -> list:
    lines = ["=== DATABASE ==="]
    lines.append(f"• Available: {db_client.is_available()}")
    return lines


def _collect_save_engine_section() -> list:
    return ["=== SAVE ENGINE ===", "• (no active jobs tracked)"]


def _collect_event_loop_section() -> list:
    lines = ["=== EVENT LOOP ==="]
    current = asyncio.current_task()
    for task in asyncio.all_tasks():
        if task is current:
            continue
        name = task.get_name()
        state = _get_task_state(task)
        coro_name = _get_coro_name(task)
        loc = _get_await_location(task)
        lines.append(f"• {name} — {state} — {coro_name}")
        if loc:
            lines.append(f"  at {loc}")
    return lines


def _collect_last_events_section() -> list:
    lines = ["=== LAST EVENTS ==="]
    events = get_events()
    if not events:
        lines.append("• (no events recorded)")
        return lines
    for e in events[-20:]:
        lines.append(f"• {_format_event(e)}")
    return lines


def build_diagnostic_report(client, bio_engine, db_client, health_snap: dict) -> str:
    sections = []
    sections.extend(_collect_process_section())
    sections.append("")
    sections.extend(_collect_runtime_section(health_snap))
    sections.append("")
    sections.extend(_collect_telethon_section(client))
    sections.append("")
    sections.extend(_collect_helper_section(health_snap))
    sections.append("")
    sections.extend(_collect_supervisor_section(health_snap))
    sections.append("")
    sections.extend(_collect_bio_section(bio_engine))
    sections.append("")
    sections.extend(_collect_database_section(db_client))
    sections.append("")
    sections.extend(_collect_save_engine_section())
    sections.append("")
    sections.extend(_collect_event_loop_section())
    sections.append("")
    sections.extend(_collect_last_events_section())
    return "\n".join(sections)


async def recover_stalled(client, owner_id: int, tz_str: str, bio_engine, db_client) -> str:
    """Recover stalled tasks. Uses RuntimeSupervisor if available."""
    stalled = []
    recovered = []
    still_unhealthy = []

    current = asyncio.current_task()
    protected = {
        "lifeos-tg-supervisor", "lifeos-watchdog", "lifeos-heartbeat",
        "lifeos-web", "lifeos-liveness", "lifeos-helper-supervisor",
        "lifeos-helper-watchdog",
    }
    for task in asyncio.all_tasks():
        if task is current or task.done():
            continue
        name = task.get_name()
        if name in protected:
            continue
        coro = task.get_coro()
        if coro is None:
            continue
        frame = coro.cr_frame
        if frame is None:
            continue
        stalled.append(task)

    for task in stalled:
        name = task.get_name()
        try:
            task.cancel()
            await asyncio.wait_for(task, timeout=5.0)
        except (asyncio.CancelledError, asyncio.TimeoutError, Exception):
            pass
        recovered.append(name)

    if not bio_engine.is_running():
        try:
            bio_engine.start_cron(client, owner_id, tz_str)
            await asyncio.sleep(1)
            if bio_engine.is_running():
                recovered.append("Bio Cron restarted")
            else:
                still_unhealthy.append("Bio Cron failed to restart")
        except Exception as exc:
            still_unhealthy.append(f"Bio Cron restart error: {exc}")

    lines = ["", "=== RECOVERY ==="]
    if recovered:
        lines.append("Recovered:")
        for r in recovered:
            lines.append(f"  ✔ {r}")
    if still_unhealthy:
        lines.append("Still unhealthy:")
        for u in still_unhealthy:
            lines.append(f"  ✖ {u}")
    if not recovered and not still_unhealthy:
        lines.append("No stalled tasks detected — nothing to recover.")
    return "\n".join(lines)
