"""
Asyncio task diagnostics — dumps all running tasks with stack traces
every 60 seconds to detect event-loop stalls and blocked coroutines.

Detects:
  - Tasks that make no progress (same stack trace across dumps)
  - Event-loop stalls (sleep drift > threshold)
  - Blocked coroutine stacks (tasks waiting on locks, queues, I/O)
  - Deadlocks (tasks blocked on Future/Event/Lock with no progress)
  - Starvation (tasks unchanged across 3+ consecutive dumps)
  - Slow callbacks / slow event handlers
  - Queue sizes and queue processing latency
  - RPC timings and update/handler/event timestamps

When TASK_NO_PROGRESS is reported, prints:
  - full coroutine name
  - current await point (file:line)
  - full stack trace
  - awaited object type
  - elapsed waiting time

This module is read-only: it logs diagnostics but never interferes
with task execution or triggers recovery.
"""
import asyncio
import logging
import time
import traceback
from collections import deque

from backend.runtime.tracer import trace
from backend.runtime.task_guard import immortal_create_task

logger = logging.getLogger("backend.diagnostics_loop")

_INTERVAL = 60.0
_STALL_THRESHOLD_MS = 500.0
_STARVATION_THRESHOLD = 3
_SLOW_HANDLER_PATTERNS = (
    "_callback_router", "_handle_panel", "_handle_action",
    "_handle_input", "_handle_navigation", "_inline_router",
    "_input_listener", "panel_cmd", "save_cmd", "del_cmd",
    "health_cmd", "kill_cmd", "logs_cmd", "help_cmd",
)
_PERMANENT_TASK_NAMES = frozenset({
    "lifeos-heartbeat", "lifeos-keepalive", "lifeos-failsafe",
    "lifeos-diagnostics", "lifeos-memory-cleanup", "lifeos-run",
    "lifeos-web",
})
_PERMANENT_CORO_PATTERNS = (
    "run_until_disconnected", "run_until_connected",
    "_heartbeat_loop", "_keepalive_loop", "_failsafe_loop",
    "_diagnostics_loop", "_cleanup_loop", "_run_loop",
    "uvicorn", "serve",
)

_task: asyncio.Task | None = None
_prev_stacks: dict[str, str] = {}
_stack_unchanged_count: dict[str, int] = {}
_task_first_seen: dict[str, float] = {}


def _get_awaited_object(coro) -> str:
    """Try to identify what the coroutine is currently awaiting."""
    try:
        frame = coro.cr_frame if hasattr(coro, "cr_frame") else None
        if frame is None:
            return "unknown"
        code = frame.f_code
        if code.co_name == "wait_for":
            arg_info = frame.f_locals
            inner = arg_info.get("fut") or arg_info.get("awaitable") or arg_info.get("f")
            if inner is not None:
                return f"wait_for({type(inner).__name__})"
            return "wait_for(?)"
        if code.co_name == "wait":
            arg_info = frame.f_locals
            inner = arg_info.get("fut")
            if inner is not None:
                return f"wait({type(inner).__name__})"
            return "wait(?)"
        return code.co_name
    except Exception:
        return "unknown"


def _get_full_stack(coro, limit: int = 12) -> str:
    """Extract a full stack trace from a coroutine."""
    try:
        frame = coro.cr_frame if hasattr(coro, "cr_frame") else None
        if frame is None:
            return "(no frame)"
        stack_lines = traceback.extract_stack(frame, limit=limit)
        return "".join(traceback.format_list(stack_lines))
    except Exception:
        return "(stack extraction failed)"


def _get_await_location(coro) -> str:
    """Get the current file:line where the coroutine is suspended."""
    try:
        frame = coro.cr_frame if hasattr(coro, "cr_frame") else None
        if frame is None:
            return ""
        code = frame.f_code
        return f"{code.co_filename}:{frame.f_lineno}"
    except Exception:
        return ""


def _get_coro_name(coro) -> str:
    if coro is None:
        return "unknown"
    return getattr(coro, "__name__", getattr(coro, "__qualname__", "unknown"))


def _is_blocked_on_sync_primitive(awaited: str) -> bool:
    return any(
        marker in awaited
        for marker in ("Future", "Event", "Lock", "Semaphore", "Queue", "Condition")
    )


def _is_permanent_task(name: str, coro_name: str = "") -> bool:
    """Classify a task as permanent (long-lived) or bounded (should complete).

    Permanent tasks are expected to wait indefinitely — an unchanged
    stack trace across dumps is NORMAL for them.  Bounded tasks should
    complete within a reasonable period; an unchanged stack is a stall.
    """
    if name in _PERMANENT_TASK_NAMES:
        return True
    if any(pat in coro_name for pat in _PERMANENT_CORO_PATTERNS):
        return True
    return False


def _detect_deadlocks(pending_tasks: list, task_info: dict, elapsed: dict) -> list[str]:
    """Detect tasks blocked on sync primitives with no progress."""
    deadlocks = []
    for t in pending_tasks:
        name = t.get_name()
        awaited = task_info.get(name, {}).get("awaited", "")
        if _is_blocked_on_sync_primitive(awaited):
            wait_time = elapsed.get(name, 0.0)
            if wait_time > _INTERVAL * 2:
                deadlocks.append(
                    f"  DEADLOCK: {name} blocked on {awaited} for {wait_time:.0f}s"
                )
    return deadlocks


def _detect_starvation(permanent_names: frozenset[str] = frozenset()) -> list[str]:
    """Detect bounded tasks whose stack hasn't changed across multiple dumps."""
    starved = []
    for name, count in _stack_unchanged_count.items():
        if name in permanent_names:
            continue
        if count >= _STARVATION_THRESHOLD:
            elapsed = time.time() - _task_first_seen.get(name, time.time())
            starved.append(
                f"  STARVATION: {name} unchanged for {count} dumps ({elapsed:.0f}s)"
            )
    return starved


def _detect_slow_handlers(pending_tasks: list, task_info: dict, elapsed: dict) -> list[str]:
    """Detect slow event handlers — coroutines matching handler patterns that are stalled."""
    slow = []
    for t in pending_tasks:
        name = t.get_name()
        info = task_info.get(name, {})
        coro_name = info.get("coro_name", "")
        if any(pattern in coro_name for pattern in _SLOW_HANDLER_PATTERNS):
            wait_time = elapsed.get(name, 0.0)
            if wait_time > _INTERVAL:
                slow.append(
                    f"  SLOW_HANDLER: {name} ({coro_name}) stuck for {wait_time:.0f}s"
                )
    return slow


def _collect_telethon_queue_info() -> list[str]:
    """Try to collect Telethon update queue sizes and processing latency."""
    lines = []
    try:
        from backend.helper.inline_engine import _self_client
        client = _self_client
        if client is not None and hasattr(client, "_updates"):
            upd = client._updates
            if hasattr(upd, "_pending"):
                pending_size = len(upd._pending)
                lines.append(f"  Update queue size: {pending_size}")
            if hasattr(upd, "_handlers"):
                lines.append(f"  Registered handlers: {len(upd._handlers)}")
            if hasattr(upd, "_last_update_ts"):
                last_ts = upd._last_update_ts
                if last_ts:
                    age = time.time() - last_ts
                    lines.append(f"  Last update processed: {age:.1f}s ago")
    except Exception:
        pass
    return lines


def _collect_health_timestamps() -> list[str]:
    """Collect timing info from the health module."""
    lines = []
    try:
        from backend import health
        snap = health.snapshot()
        if snap.get("last_rpc_s") is not None:
            lines.append(f"  Last RPC: {snap['last_rpc_s']}s ago")
        if snap.get("rpc_latency_ms") is not None:
            lines.append(f"  RPC latency: {snap['rpc_latency_ms']}ms")
        if snap.get("last_command_s") is not None:
            lines.append(f"  Last command: {snap['last_command_s']}s ago")
        if snap.get("last_update_s") is not None:
            lines.append(f"  Last update: {snap['last_update_s']}s ago")
        if snap.get("last_handler_dispatched_s") is not None:
            lines.append(f"  Last handler dispatched: {snap['last_handler_dispatched_s']}s ago")
        if snap.get("last_telethon_event_s") is not None:
            lines.append(f"  Last Telethon event: {snap['last_telethon_event_s']}s ago")
        if snap.get("last_callback_s") is not None:
            lines.append(f"  Last callback: {snap['last_callback_s']}s ago")
        if snap.get("last_event_dispatch_s") is not None:
            lines.append(f"  Last event dispatch: {snap['last_event_dispatch_s']}s ago")
    except Exception:
        pass
    return lines


async def _dump_tasks() -> None:
    """Dump all asyncio tasks with their full diagnostic info."""
    global _prev_stacks, _stack_unchanged_count, _task_first_seen

    t0 = time.monotonic()
    await asyncio.sleep(0)
    loop_latency_ms = (time.monotonic() - t0) * 1000

    tasks = asyncio.all_tasks()
    current = asyncio.current_task()
    pending = []
    for t in tasks:
        if t is current:
            continue
        if t.done():
            continue
        pending.append(t)

    now = time.time()
    stacks: dict[str, str] = {}
    stalled: list[str] = []
    task_info: dict[str, dict] = {}
    elapsed: dict[str, float] = {}

    for t in pending:
        name = t.get_name()
        try:
            coro = t.get_coro()
            if coro is None:
                continue
            frame = coro.cr_frame if hasattr(coro, "cr_frame") else None
            if frame is None:
                continue

            stack_lines = traceback.extract_stack(frame, limit=12)
            stack_str = "".join(traceback.format_list(stack_lines))
            stacks[name] = stack_str

            coro_name = _get_coro_name(coro)
            await_loc = _get_await_location(coro)
            awaited_obj = _get_awaited_object(coro)
            is_permanent = _is_permanent_task(name, coro_name)

            if name not in _task_first_seen:
                _task_first_seen[name] = now
            elapsed[name] = now - _task_first_seen[name]

            task_info[name] = {
                "coro_name": coro_name,
                "await_loc": await_loc,
                "awaited": awaited_obj,
                "stack": stack_str,
                "permanent": is_permanent,
            }

            prev = _prev_stacks.get(name)
            if prev is not None and prev == stack_str:
                _stack_unchanged_count[name] = _stack_unchanged_count.get(name, 0) + 1
                if not is_permanent:
                    stalled.append(name)
            else:
                _stack_unchanged_count[name] = 0

        except Exception:
            pass

    _prev_stacks = stacks

    permanent_names = frozenset(
        name for name, info in task_info.items() if info.get("permanent", False)
    )
    permanent_count = len(permanent_names)
    bounded_count = len(pending) - permanent_count
    bounded_tasks = [t for t in pending if t.get_name() not in permanent_names]

    trace(
        "ASYNC_TASK_DUMP",
        pending_count=len(pending),
        permanent_count=permanent_count,
        bounded_count=bounded_count,
        loop_latency_ms=f"{loop_latency_ms:.1f}",
        stalled_count=len(stalled),
        stalled_tasks=",".join(stalled) if stalled else "",
    )

    if loop_latency_ms > _STALL_THRESHOLD_MS:
        logger.warning(
            "EVENT_LOOP_STALL — %.1fms latency (threshold=%.0fms), "
            "%d pending tasks (%d permanent, %d bounded), %d stalled",
            loop_latency_ms, _STALL_THRESHOLD_MS,
            len(pending), permanent_count, bounded_count, len(stalled),
        )

    deadlocks = _detect_deadlocks(bounded_tasks, task_info, elapsed)
    starved = _detect_starvation(permanent_names)
    slow_handlers = _detect_slow_handlers(bounded_tasks, task_info, elapsed)

    if stalled:
        lines = [
            f"TASK_NO_PROGRESS — {len(stalled)} bounded tasks unchanged since last dump "
            f"(permanent tasks excluded):",
        ]
        for name in stalled:
            info = task_info.get(name, {})
            wait_time = elapsed.get(name, 0.0)
            lines.append(f"")
            lines.append(f"  Task: {name}")
            lines.append(f"  Coroutine: {info.get('coro_name', 'unknown')}")
            lines.append(f"  Await point: {info.get('await_loc', 'unknown')}")
            lines.append(f"  Awaited object: {info.get('awaited', 'unknown')}")
            lines.append(f"  Elapsed waiting: {wait_time:.0f}s")
            lines.append(f"  Unchanged dumps: {_stack_unchanged_count.get(name, 0)}")
            lines.append(f"  Stack trace:")
            stack = info.get("stack", "")
            for stack_line in stack.rstrip().splitlines():
                lines.append(f"    {stack_line}")
        logger.warning("\n".join(lines))

    if deadlocks:
        logger.warning("DEADLOCK_DETECTED — %d tasks blocked on sync primitives:\n%s", len(deadlocks), "\n".join(deadlocks))

    if starved:
        logger.warning("TASK_STARVATION — %d tasks unchanged across %d+ dumps:\n%s", len(starved), _STARVATION_THRESHOLD, "\n".join(starved))

    if slow_handlers:
        logger.warning("SLOW_EVENT_HANDLER — %d handlers stuck beyond threshold:\n%s", len(slow_handlers), "\n".join(slow_handlers))

    if pending:
        task_summary = []
        for t in pending:
            name = t.get_name()
            info = task_info.get(name, {})
            coro_name = info.get("coro_name", "unknown")
            await_loc = info.get("await_loc", "")
            awaited = info.get("awaited", "")
            wait_time = elapsed.get(name, 0.0)
            classification = "PERMANENT" if info.get("permanent", False) else "BOUNDED"
            task_summary.append(
                f"  [{classification}] {name}: {coro_name} awaiting={awaited} "
                f"elapsed={wait_time:.0f}s"
            )
            if await_loc:
                task_summary.append(f"    at {await_loc}")

        queue_info = _collect_telethon_queue_info()
        health_ts = _collect_health_timestamps()

        extra_sections = []
        if queue_info:
            extra_sections.append("TELETHON_QUEUE:")
            extra_sections.extend(queue_info)
        if health_ts:
            extra_sections.append("HEALTH_TIMESTAMPS:")
            extra_sections.extend(health_ts)

        logger.info(
            "ASYNC_TASK_DUMP — %d pending tasks (loop_latency=%.1fms):\n%s%s",
            len(pending), loop_latency_ms,
            "\n".join(task_summary),
            ("\n" + "\n".join(extra_sections)) if extra_sections else "",
        )


async def _diagnostics_loop() -> None:
    logger.info("Asyncio diagnostics started (interval=%ds)", int(_INTERVAL))
    while True:
        await asyncio.sleep(_INTERVAL)
        try:
            await _dump_tasks()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Diagnostics dump error: %s", exc)


def start_diagnostics() -> None:
    global _task
    if _task and not _task.done():
        return
    _task = immortal_create_task(
        _diagnostics_loop, name="lifeos-diagnostics"
    )


async def stop_diagnostics() -> None:
    global _task
    if _task and not _task.done():
        _task.cancel()
        try:
            await asyncio.wait_for(_task, timeout=5.0)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass
    _task = None
