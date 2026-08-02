"""
Task guard — wraps asyncio tasks so no failure is ever silent.

Two task factories:

guarded_create_task:
  - Single-shot: logs crash, re-raises (task dies).
  - Use for coroutines that are expected to complete.

immortal_create_task:
  - Forever-loop: wraps the coroutine in an infinite try/except so the
    task NEVER dies from an exception. CancelledError is re-raised; all
    other exceptions are logged and the loop sleeps with backoff.
  - Use for every supervisor/watchdog/heartbeat/keepalive loop.

Usage:
    from backend.runtime.task_guard import guarded_create_task, immortal_create_task
    task = immortal_create_task(my_forever_loop(), name="lifeos-something")
"""
import asyncio
import logging
import random

from backend.runtime.tracer import trace_task_crash, trace_task_cancelled, trace

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


def immortal_create_task(
    coro: "asyncio.coroutines",
    name: str,
    *,
    backoff_base: float = 2.0,
    backoff_max: float = 60.0,
) -> asyncio.Task:
    """Wrap a coroutine so it NEVER dies from an exception.

    The coroutine itself should be a ``while True`` loop. If an exception
    escapes the coroutine body, this wrapper logs it, sleeps with
    exponential backoff, and re-enters the coroutine from the top.
    CancelledError is always re-raised (cooperative cancellation).
    """
    async def _immortal_wrapper():
        attempt = 0
        while True:
            try:
                await coro
                trace("IMMORTAL_EXIT", task=name, reason="coro_returned")
                logger.warning("IMMORTAL_EXIT — task '%s' returned normally (will restart)", name)
                await asyncio.sleep(1.0)
                continue
            except asyncio.CancelledError:
                trace_task_cancelled(name, _get_runtime_state())
                raise
            except Exception as exc:
                attempt += 1
                base = min(backoff_max, backoff_base * (2 ** attempt))
                jitter = random.uniform(-0.3, 0.3) * base
                delay = max(1.0, base + jitter)
                trace_task_crash(name, exc, _get_runtime_state())
                trace("IMMORTAL_RESTART", task=name, attempt=attempt, backoff=f"{delay:.1f}s")
                logger.exception("IMMORTAL — task '%s' crashed (attempt %d), restarting in %.1fs:", name, attempt, delay)
                await asyncio.sleep(delay)

    return asyncio.create_task(_immortal_wrapper(), name=name)
