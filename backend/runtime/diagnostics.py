"""
Asyncio task diagnostics — dumps all running tasks with stack traces
every 60 seconds to detect event-loop stalls and blocked coroutines.

Detects:
  - Tasks that make no progress (same stack trace across dumps)
  - Event-loop stalls (sleep drift > threshold)
  - Blocked coroutine stacks (tasks waiting on locks, queues, I/O)

This module is read-only: it logs diagnostics but never interferes
with task execution or triggers recovery.
"""
import asyncio
import logging
import time
import traceback

from backend.runtime.tracer import trace
from backend.runtime.task_guard import guarded_create_task

logger = logging.getLogger("backend.diagnostics_loop")

_INTERVAL = 60.0
_STALL_THRESHOLD_MS = 500.0
_task: asyncio.Task | None = None
_prev_stacks: dict[str, str] = {}


async def _dump_tasks() -> None:
    """Dump all asyncio tasks with their current stack trace."""
    global _prev_stacks

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

    for t in pending:
        name = t.get_name()
        try:
            frame = t.get_coro().cr_frame if hasattr(t.get_coro(), "cr_frame") else None
            if frame is None:
                continue
            stack_lines = traceback.extract_stack(frame, limit=8)
            stack_str = "".join(traceback.format_list(stack_lines))
            stacks[name] = stack_str

            prev = _prev_stacks.get(name)
            if prev is not None and prev == stack_str:
                stalled.append(name)
        except Exception:
            pass

    _prev_stacks = stacks

    trace(
        "ASYNC_TASK_DUMP",
        pending_count=len(pending),
        loop_latency_ms=f"{loop_latency_ms:.1f}",
        stalled_count=len(stalled),
        stalled_tasks=",".join(stalled) if stalled else "",
    )

    if loop_latency_ms > _STALL_THRESHOLD_MS:
        logger.warning(
            "EVENT_LOOP_STALL — %.1fms latency (threshold=%.0fms), "
            "%d pending tasks, %d stalled",
            loop_latency_ms, _STALL_THRESHOLD_MS,
            len(pending), len(stalled),
        )

    if stalled:
        logger.warning(
            "TASK_NO_PROGRESS — %d tasks unchanged since last dump: %s",
            len(stalled), ", ".join(stalled),
        )

    if pending:
        task_summary = []
        for t in pending:
            name = t.get_name()
            done = t.done()
            cancelled = t.cancelled()
            task_summary.append(
                f"  {name}: done={done} cancelled={cancelled}"
            )
        logger.info(
            "ASYNC_TASK_DUMP — %d pending tasks (loop_latency=%.1fms):\n%s",
            len(pending), loop_latency_ms,
            "\n".join(task_summary),
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
    _task = guarded_create_task(
        _diagnostics_loop(), name="lifeos-diagnostics"
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
