"""
Crash diagnostics — ensures no process exit is ever silent.

This module is the single owner of:
  1. Last-exception ring buffer (exceptions, warnings, runtime events)
  2. Signal capture (SIGTERM, SIGINT, SIGABRT, SIGQUIT)
  3. Crash snapshot generation (full system state before exit)
  4. Exit reason tracking (PROCESS_EXIT_REASON trace before every exit)

Every path that causes the process to terminate MUST call
``record_exit_reason()`` and ``dump_crash_snapshot()`` before exiting.

Design guarantees:
  - All state is in-memory (no DB, no disk).
  - Every function is safe to call from a signal handler or exception hook.
  - The ring buffers are bounded (100 entries each).
  - The crash snapshot is dumped to stdout (Render captures it).
  - No exceptions raised by this module — it degrades silently.
"""
from __future__ import annotations

import asyncio
import logging
import os
import resource
import signal
import sys
import threading
import time
import traceback
from collections import deque
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger("backend.crash_diagnostics")

_RING_SIZE = 100

_exceptions: deque = deque(maxlen=_RING_SIZE)
_warnings: deque = deque(maxlen=_RING_SIZE)
_runtime_events: deque = deque(maxlen=_RING_SIZE)

_exit_reason: str = "UNKNOWN"
_exit_reason_set: bool = False
_process_start: float = time.monotonic()
_signal_received: dict[str, Any] = {}


def uptime_s() -> float:
    return round(time.monotonic() - _process_start, 1)


# ── Ring buffer recording ──

def record_exception(exc: BaseException, source: str = "", context: dict[str, Any] | None = None) -> None:
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "type": type(exc).__name__,
        "message": str(exc),
        "source": source,
        "context": context or {},
        "traceback": "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
    }
    _exceptions.append(entry)


def record_warning(msg: str, source: str = "", context: dict[str, Any] | None = None) -> None:
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "message": msg,
        "source": source,
        "context": context or {},
    }
    _warnings.append(entry)


def record_runtime_event(event: str, details: str = "") -> None:
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "event": event,
        "details": details,
    }
    _runtime_events.append(entry)


# ── Exit reason ──

def record_exit_reason(reason: str, details: str = "") -> None:
    global _exit_reason, _exit_reason_set
    _exit_reason = reason
    _exit_reason_set = True
    from backend.runtime.tracer import trace
    trace("PROCESS_EXIT_REASON", reason=reason, details=details, uptime=f"{uptime_s()}s")
    record_runtime_event("PROCESS_EXIT_REASON", f"{reason}: {details}")
    logger.error("PROCESS_EXIT_REASON = %s (uptime=%ss, details=%s)", reason, uptime_s(), details)


def get_exit_reason() -> str:
    return _exit_reason


def exit_reason_is_set() -> bool:
    return _exit_reason_set


# ── Signal capture ──

def capture_signal(signum: int, frame: Any) -> None:
    sig_name = _signal_name(signum)
    global _signal_received
    _signal_received = {
        "signal": sig_name,
        "signum": signum,
        "time": datetime.now(timezone.utc).isoformat(),
        "uptime_s": uptime_s(),
        "active_tasks": _safe_task_count(),
        "runtime_state": _safe_runtime_state(),
    }

    from backend.runtime.tracer import trace
    trace(
        "SIGNAL_RECEIVED",
        signal=sig_name,
        signum=signum,
        uptime=f"{uptime_s()}s",
        active_tasks=_signal_received["active_tasks"],
        runtime_state=_signal_received["runtime_state"],
    )
    record_runtime_event("SIGNAL_RECEIVED", f"{sig_name} (signum={signum})")

    reason = sig_name
    record_exit_reason(reason, f"signal {signum} received at uptime {uptime_s()}s")

    dump_crash_snapshot(reason=f"signal:{sig_name}")

    if signum in (signal.SIGINT, signal.SIGTERM):
        sys.exit(128 + signum)
    elif signum == signal.SIGABRT:
        sys.exit(134)
    elif signum == signal.SIGQUIT:
        sys.exit(131)
    else:
        sys.exit(128 + signum)


def install_signal_handlers() -> None:
    signals = [signal.SIGTERM, signal.SIGINT, signal.SIGABRT]
    if hasattr(signal, "SIGQUIT"):
        signals.append(signal.SIGQUIT)
    for sig in signals:
        try:
            signal.signal(sig, capture_signal)
        except (OSError, ValueError):
            pass


def _signal_name(signum: int) -> str:
    names = {
        signal.SIGTERM: "SIGTERM",
        signal.SIGINT: "SIGINT",
        signal.SIGABRT: "SIGABRT",
    }
    if hasattr(signal, "SIGQUIT"):
        names[signal.SIGQUIT] = "SIGQUIT"
    return names.get(signum, f"SIGNAL_{signum}")


# ── Global exception hooks ──

def install_global_exception_hooks() -> None:
    sys.excepthook = _sys_excepthook

    try:
        threading.excepthook = _threading_excepthook
    except AttributeError:
        pass

    loop = _get_running_loop()
    if loop is not None:
        loop.set_exception_handler(_async_loop_exception_handler)


def _sys_excepthook(exc_type, exc_value, exc_tb) -> None:
    if issubclass(exc_type, KeyboardInterrupt):
        record_exit_reason("KEYBOARD_INTERRUPT", "KeyboardInterrupt received")
        dump_crash_snapshot(reason="KeyboardInterrupt")
        sys.__excepthook__(exc_type, exc_value, exc_tb)
        return

    if issubclass(exc_type, SystemExit):
        code = exc_value.code if isinstance(exc_value.code, int) else str(exc_value.code)
        record_exit_reason("SYSTEM_EXIT", f"code={code}")
        dump_crash_snapshot(reason=f"SystemExit(code={code})")
        sys.__excepthook__(exc_type, exc_value, exc_tb)
        return

    if issubclass(exc_type, MemoryError):
        record_exit_reason("MEMORY_ERROR", str(exc_value))
        dump_crash_snapshot(reason="MemoryError")
        sys.__excepthook__(exc_type, exc_value, exc_tb)
        return

    if issubclass(exc_type, GeneratorExit):
        record_exit_reason("GENERATOR_EXIT", str(exc_value))
        dump_crash_snapshot(reason="GeneratorExit")
        return

    record_exception(exc_value, source="sys.excepthook")
    from backend.runtime.tracer import trace_exception
    trace_exception("UNHANDLED_EXCEPTION", exc_value, source="sys.excepthook")
    record_exit_reason("UNHANDLED_EXCEPTION", f"{exc_type.__name__}: {exc_value}")
    dump_crash_snapshot(reason=f"unhandled_exception:{exc_type.__name__}")
    sys.__excepthook__(exc_type, exc_value, exc_tb)


def _threading_excepthook(args) -> None:
    exc = args.exc_value
    if exc is None:
        return
    record_exception(exc, source=f"threading:{args.thread.name if args.thread else 'unknown'}")
    from backend.runtime.tracer import trace_exception
    trace_exception("GLOBAL_EXCEPTION", exc, source=f"thread:{args.thread.name if args.thread else 'unknown'}")
    record_runtime_event("GLOBAL_EXCEPTION", f"thread={args.thread.name if args.thread else 'unknown'}: {type(exc).__name__}: {exc}")


def _async_loop_exception_handler(loop, context) -> None:
    exc = context.get("exception")
    if exc is not None:
        record_exception(exc, source=f"asyncio:{context.get('message', 'unknown')}")
        from backend.runtime.tracer import trace_exception
        trace_exception("ASYNC_EXCEPTION", exc, source=context.get("message", "asyncio_loop"))
        record_runtime_event("ASYNC_EXCEPTION", f"{type(exc).__name__}: {exc}")
    else:
        msg = context.get("message", "unknown async error")
        record_warning(msg, source="asyncio_loop")
    loop.default_exception_handler(context)


# ── Task / Future exception capture ──

def capture_task_exception(task_name: str, exc: BaseException) -> None:
    record_exception(exc, source=f"task:{task_name}")
    from backend.runtime.tracer import trace
    trace(
        "TASK_FATAL",
        task=task_name,
        exc_type=type(exc).__name__,
        exc_repr=str(exc)[:200],
    )
    record_runtime_event("TASK_FATAL", f"task={task_name}: {type(exc).__name__}: {exc}")


# ── Crash snapshot ──

def dump_crash_snapshot(reason: str = "") -> None:
    from backend.runtime.tracer import trace
    trace("CRASH_SNAPSHOT_CREATED", reason=reason, uptime=f"{uptime_s()}s")
    record_runtime_event("CRASH_SNAPSHOT_CREATED", reason)

    lines = []
    lines.append("=" * 60)
    lines.append("CRASH SNAPSHOT")
    lines.append("=" * 60)
    lines.append(f"Exit Reason: {_exit_reason}")
    lines.append(f"Snapshot Reason: {reason}")
    lines.append(f"Timestamp: {datetime.now(timezone.utc).isoformat()}")
    lines.append(f"Uptime: {uptime_s()}s")
    lines.append(f"PID: {os.getpid()}")
    lines.append("")

    lines.append("--- PROCESS ---")
    try:
        usage = resource.getrusage(resource.RUSAGE_SELF)
        mem_mb = usage.ru_maxrss / 1024
        cpu_s = usage.ru_utime + usage.ru_stime
        lines.append(f"Memory: {mem_mb:.1f} MB (peak RSS)")
        lines.append(f"CPU: {cpu_s:.2f}s")
    except Exception:
        lines.append("Memory/CPU: unavailable")
    lines.append(f"Python: {sys.version.split()[0]}")
    lines.append(f"Threads: {threading.active_count()}")
    lines.append("")

    lines.append("--- SIGNAL ---")
    if _signal_received:
        lines.append(f"Last signal: {_signal_received.get('signal', 'none')}")
        lines.append(f"  time: {_signal_received.get('time', '')}")
        lines.append(f"  uptime: {_signal_received.get('uptime_s', '')}s")
        lines.append(f"  active_tasks: {_signal_received.get('active_tasks', 0)}")
        lines.append(f"  runtime_state: {_signal_received.get('runtime_state', 'unknown')}")
    else:
        lines.append("No signal received")
    lines.append("")

    lines.append("--- RUNTIME STATE ---")
    lines.append(f"State: {_safe_runtime_state()}")
    lines.append(f"Client generation: {_safe_client_generation()}")
    lines.append("")

    lines.append("--- ASYNCIO TASKS ---")
    lines.append(f"Pending tasks: {_safe_task_count()}")
    task_info = _collect_task_info()
    for name, state, coro_name, await_loc in task_info:
        lines.append(f"  {name}: {state} — {coro_name}")
        if await_loc:
            lines.append(f"    at {await_loc}")
    lines.append("")

    lines.append("--- IMMORTAL TASKS ---")
    loops = _safe_get_all_loop_progress()
    for name, info in loops.items():
        state = info.get("state", "unknown")
        last_tick = info.get("last_tick", 0)
        if last_tick > 0:
            age = time.time() - last_tick
            tick_str = f"{age:.0f}s ago"
        else:
            tick_str = "never"
        lines.append(f"  {name}: {state}, last_tick={tick_str}")
    lines.append("")

    lines.append("--- HEALTH TIMESTAMPS ---")
    snap = _safe_health_snapshot()
    if snap:
        for key in (
            "runtime_state", "telethon_connected", "supervisor_ok",
            "helper_connected", "bio_cron_ok", "watchdog_ok",
            "heartbeat_age_s", "uptime_s", "restart_count",
            "client_generation", "last_rebuild_reason",
            "rpc_latency_ms", "last_rpc_s", "last_command_s",
            "last_update_s", "last_telethon_event_s",
            "last_callback_s", "last_event_dispatch_s",
        ):
            val = snap.get(key)
            if val is not None:
                lines.append(f"  {key}: {val}")
    lines.append("")

    lines.append("--- LAST 10 EXCEPTIONS ---")
    for i, entry in enumerate(list(_exceptions)[-10:]):
        lines.append(f"  [{i+1}] {entry['ts']}: {entry['type']}: {entry['message']}")
        if entry.get("source"):
            lines.append(f"       source: {entry['source']}")
        tb_lines = entry.get("traceback", "").strip().splitlines()
        if len(tb_lines) > 2:
            for tb_line in tb_lines[-3:]:
                lines.append(f"       {tb_line}")
    lines.append("")

    lines.append("--- LAST 10 WARNINGS ---")
    for i, entry in enumerate(list(_warnings)[-10:]):
        lines.append(f"  [{i+1}] {entry['ts']}: {entry['message']}")
        if entry.get("source"):
            lines.append(f"       source: {entry['source']}")
    lines.append("")

    lines.append("--- LAST 10 RUNTIME EVENTS ---")
    for i, entry in enumerate(list(_runtime_events)[-10:]):
        lines.append(f"  [{i+1}] {entry['ts']}: {entry['event']} — {entry.get('details', '')}")
    lines.append("")

    lines.append("--- TELETHON STATE ---")
    telethon_state = _collect_telethon_state()
    for key, val in telethon_state.items():
        lines.append(f"  {key}: {val}")
    lines.append("")

    lines.append("--- AI STATE ---")
    ai_state = _safe_ai_state()
    for key, val in ai_state.items():
        lines.append(f"  {key}: {val}")
    lines.append("")

    lines.append("--- EXIT REASON ---")
    lines.append(f"  PROCESS_EXIT_REASON = {_exit_reason}")
    lines.append("")

    output = "\n".join(lines)
    print(output, flush=True)

    from backend.runtime.tracer import trace
    trace("PROCESS_TERMINATING", exit_reason=_exit_reason, uptime=f"{uptime_s()}s")
    record_runtime_event("PROCESS_TERMINATING", f"exit_reason={_exit_reason}")


# ── Helpers (all safe — never raise) ──

def _get_running_loop():
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        return None


def _safe_task_count() -> int:
    try:
        return len(asyncio.all_tasks())
    except Exception:
        return -1


def _safe_runtime_state() -> str:
    try:
        from backend.health import snapshot
        return snapshot().get("runtime_state", "unknown")
    except Exception:
        return "unknown"


def _safe_client_generation() -> int:
    try:
        from backend.health import snapshot
        return snapshot().get("client_generation", 0)
    except Exception:
        return 0


def _safe_health_snapshot() -> dict:
    try:
        from backend.health import snapshot
        return snapshot()
    except Exception:
        return {}


def _safe_get_all_loop_progress() -> dict:
    try:
        from backend.health import get_all_loop_progress
        return get_all_loop_progress()
    except Exception:
        return {}


def _collect_task_info() -> list[tuple[str, str, str, str]]:
    result: list[tuple[str, str, str, str]] = []
    try:
        current = asyncio.current_task()
        for task in asyncio.all_tasks():
            if task is current:
                continue
            name = task.get_name()
            if task.done():
                if task.cancelled():
                    state = "CANCELLED"
                elif task.exception():
                    state = f"FAILED({type(task.exception()).__name__})"
                else:
                    state = "DONE"
            else:
                state = "RUNNING"
            coro_name = "unknown"
            await_loc = ""
            try:
                coro = task.get_coro()
                if coro is not None:
                    coro_name = getattr(coro, "__name__", getattr(coro, "__qualname__", "unknown"))
                    frame = getattr(coro, "cr_frame", None)
                    if frame is not None:
                        code = frame.f_code
                        await_loc = f"{code.co_filename}:{frame.f_lineno}"
            except Exception:
                pass
            result.append((name, state, coro_name, await_loc))
    except Exception:
        pass
    return result


def _collect_telethon_state() -> dict[str, Any]:
    state: dict[str, Any] = {}
    try:
        from backend.helper.inline_engine import _self_client
        client = _self_client
        if client is not None:
            state["connected"] = client.is_connected()
            state["authorized"] = client.is_user_authorized() if client.is_connected() else False
            state["dc_id"] = getattr(client.session, "dc_id", "unknown")
            state["server_address"] = getattr(client.session, "server_address", "unknown")
            state["reconnect_attempts"] = getattr(client, "_reconnect_attempts", 0)
        else:
            state["connected"] = "no_client"
    except Exception as exc:
        state["error"] = str(exc)

    try:
        from backend.health import (
            get_last_telethon_event,
            get_last_rpc,
            get_last_event_dispatch,
        )
        state["last_telethon_event_s"] = _age(get_last_telethon_event())
        state["last_rpc_s"] = _age(get_last_rpc())
        state["last_dispatch_s"] = _age(get_last_event_dispatch())
    except Exception:
        pass

    return state


def _safe_ai_state() -> dict[str, Any]:
    try:
        from backend.observability.runtime_status import _ai_status
        return _ai_status()
    except Exception:
        return {"available": False}


def _age(ts: float) -> str:
    if not ts:
        return "never"
    age = time.time() - ts
    if age < 60:
        return f"{int(age)}s ago"
    if age < 3600:
        return f"{int(age / 60)}m ago"
    return f"{int(age / 3600)}h ago"


# ── Full initialization ──

def install_all() -> None:
    install_signal_handlers()
    install_global_exception_hooks()
    record_runtime_event("CRASH_DIAGNOSTICS_INSTALLED", "")


def dump_buffers() -> None:
    lines = ["=== RING BUFFER DUMP ==="]

    lines.append(f"\n--- Exceptions ({len(_exceptions)}) ---")
    for entry in _exceptions:
        lines.append(f"  {entry['ts']}: {entry['type']}: {entry['message']} (source={entry.get('source', '')})")

    lines.append(f"\n--- Warnings ({len(_warnings)}) ---")
    for entry in _warnings:
        lines.append(f"  {entry['ts']}: {entry['message']} (source={entry.get('source', '')})")

    lines.append(f"\n--- Runtime Events ({len(_runtime_events)}) ---")
    for entry in _runtime_events:
        lines.append(f"  {entry['ts']}: {entry['event']} — {entry.get('details', '')}")

    print("\n".join(lines), flush=True)
