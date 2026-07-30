"""
Task guard — wraps asyncio tasks so no failure is ever silent.

Every background task created via guarded_create_task gets:
  - A try/except boundary that captures the full traceback
  - Structured trace events (TASK_CRASHED / TASK_CANCELLED)
  - The current runtime state at the moment of failure
  - Re-raise of CancelledError (cooperative cancellation)

Usage:
    from backend.runtime.task_guard import guarded_create_task
    task = guarded_create_task(my_coro(), name="lifeos-something")
"""
import asyncio
import logging

from backend.runtime.tracer import trace_task_crash, trace_task_cancelled

logger = logging.getLogger("backend.task_guard")

_runtime_state_ref: dict = {"state": "STARTING"}


def set_runtime_state_ref(state: str) -> None:
    _runtime_state_ref["state"] = state


def _get_runtime_state() -> str:
    return _runtime_state_ref["state"]


def guarded_create_task(
    coro: "asyncio.coroutines",
    name: str,
    *,
    reraise_cancelled: bool = True,
) -> asyncio.Task:
    async def _wrapper():
        try:
            return await coro
        except asyncio.CancelledError:
            trace_task_cancelled(name, _get_runtime_state())
            if reraise_cancelled:
                raise
            return None
        except Exception as exc:
            trace_task_crash(name, exc, _get_runtime_state())
            logger.exception("Task '%s' crashed:", name)
            raise
        finally:
            logger.debug("Task '%s' finished.", name)

    return asyncio.create_task(_wrapper(), name=name)
