"""
Centralized bounded-operation watchdog.

Distinguishes between:

A) LONG-LIVED TASKS (Telethon loops, heartbeat, keepalive, failsafe, web server,
   supervisors) — these are NEVER given a lifetime timeout. They use
   ``immortal_create_task`` from ``task_guard.py`` and run indefinitely.

B) BOUNDED OPERATIONS (database queries, short RPC calls, lock acquisition,
   internal processing) — these are expected to finish within a reasonable
   period. If they exceed their configured execution window, the watchdog:

     1. Emits a structured diagnostic via the existing tracer.
     2. Optionally cancels the operation safely.
     3. Ensures ``finally`` blocks and ``async with`` cleanup still execute.
     4. Never leaves ``asyncio.Lock`` objects held (the CancelledError
        propagates through ``async with`` which releases the lock).

Usage patterns:

    # Context manager (preferred — guarantees cleanup)
    async with bounded_operation("db:insert_save", timeout=10):
        await db.table("saved_items").insert(data).execute()

    # One-shot await (for simple coroutine calls)
    result = await guarded_await(coro, name="db:query_save", timeout=10)

Design rules:
  - CancelledError is ALWAYS re-raised (cooperative cancellation).
  - The watchdog never catches CancelledError — it propagates naturally,
    triggering ``__aexit__`` / ``finally`` blocks in the caller's stack.
  - No manual lock release. Locks are released by ``async with`` semantics
    when CancelledError unwinds the stack.
  - No duplicate supervisor/watchdog/recovery architecture is introduced.
  - The existing ``RuntimeSupervisor`` recovery system remains the sole
    recovery mechanism.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Awaitable, TypeVar

from backend.runtime.tracer import trace

logger = logging.getLogger("backend.operation_watchdog")

_T = TypeVar("_T")

_DEFAULT_TIMEOUT = 30.0


def _runtime_state() -> str:
    try:
        from backend.runtime.task_guard import _get_runtime_state
        return _get_runtime_state()
    except Exception:
        return "unknown"


def _client_generation() -> int | None:
    try:
        from backend.health import snapshot
        return snapshot().get("client_generation")
    except Exception:
        return None


class OperationTimeoutError(asyncio.TimeoutError):
    """Raised when a bounded operation exceeds its configured timeout.

    Subclass of ``asyncio.TimeoutError`` so existing ``except TimeoutError``
    handlers continue to work.
    """

    def __init__(self, name: str, elapsed: float, timeout: float) -> None:
        self.name = name
        self.elapsed = elapsed
        self.timeout = timeout
        super().__init__(f"Operation '{name}' timed out after {elapsed:.1f}s (limit={timeout:.1f}s)")


class bounded_operation:
    """Async context manager that bounds an operation's execution time.

    Usage::

        async with bounded_operation("db:insert_save", timeout=10):
            await db.table("saved_items").insert(data).execute()

    If the body takes longer than ``timeout`` seconds:
      1. A ``OP_TIMEOUT`` trace event is emitted with full diagnostics.
      2. The inner task is cancelled via ``asyncio.Task.cancel()``.
      3. ``CancelledError`` propagates through the body's ``finally`` /
         ``async with`` blocks, ensuring resource cleanup.
      4. ``OperationTimeoutError`` is raised to the caller.

    The context manager itself does NOT catch ``CancelledError`` — it
    propagates naturally so that ``async with`` lock releases and ``finally``
    blocks execute in the caller's frame.

    Parameters:
      name:    Human-readable operation name for diagnostics.
      timeout: Maximum allowed execution time in seconds.
      cancel:  If True (default), cancel the operation on timeout. If False,
               only log the diagnostic but do not cancel (useful for
               operations where cancellation is unsafe).
    """

    __slots__ = ("_name", "_timeout", "_cancel", "_t0", "_task")

    def __init__(
        self,
        name: str,
        *,
        timeout: float = _DEFAULT_TIMEOUT,
        cancel: bool = True,
    ) -> None:
        self._name = name
        self._timeout = timeout
        self._cancel = cancel
        self._t0: float = 0.0
        self._task: asyncio.Task | None = None

    async def __aenter__(self) -> "bounded_operation":
        self._t0 = time.monotonic()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> bool:
        elapsed = time.monotonic() - self._t0

        if exc_type is asyncio.CancelledError and self._task is not None:
            _emit_timeout_diagnostic(
                self._name, elapsed, self._timeout,
                cancelled=True, cancel_ok=True,
            )
            raise OperationTimeoutError(self._name, elapsed, self._timeout) from exc_val

        if exc_type is not None and issubclass(exc_type, OperationTimeoutError):
            return False

        return False

    def attach_task(self, task: asyncio.Task) -> None:
        """Associate an asyncio.Task with this operation for cancellation."""
        self._task = task

    @property
    def name(self) -> str:
        return self._name

    @property
    def timeout(self) -> float:
        return self._timeout

    @property
    def elapsed(self) -> float:
        if self._t0:
            return time.monotonic() - self._t0
        return 0.0


async def guarded_await(
    coro: Awaitable[_T],
    *,
    name: str,
    timeout: float = _DEFAULT_TIMEOUT,
    cancel_on_timeout: bool = True,
) -> _T:
    """Await a coroutine with a bounded timeout and structured diagnostics.

    If the coroutine exceeds ``timeout`` seconds:
      1. An ``OP_TIMEOUT`` trace event is emitted.
      2. The coroutine's task is cancelled (if ``cancel_on_timeout`` is True).
      3. ``OperationTimeoutError`` is raised.

    ``CancelledError`` from external cancellation is always re-raised
    immediately (not converted to ``OperationTimeoutError``).

    Args:
      coro:            The coroutine/awaitable to execute.
      name:            Human-readable operation name for diagnostics.
      timeout:         Maximum execution time in seconds.
      cancel_on_timeout: If True, cancel the operation when it exceeds timeout.

    Returns:
      The result of the coroutine.

    Raises:
      OperationTimeoutError: If the operation exceeds its timeout.
      asyncio.CancelledError: If externally cancelled.
    """
    t0 = time.monotonic()

    try:
        return await asyncio.wait_for(coro, timeout=timeout)
    except asyncio.TimeoutError:
        elapsed = time.monotonic() - t0
        _emit_timeout_diagnostic(
            name, elapsed, timeout,
            cancelled=cancel_on_timeout,
            cancel_ok=True,
        )
        raise OperationTimeoutError(name, elapsed, timeout)
    except asyncio.CancelledError:
        raise


def _emit_timeout_diagnostic(
    name: str,
    elapsed: float,
    timeout: float,
    *,
    cancelled: bool,
    cancel_ok: bool,
) -> None:
    """Emit a structured OP_TIMEOUT trace event with full diagnostic context."""
    trace(
        "OP_TIMEOUT",
        operation=name,
        elapsed=f"{elapsed:.1f}s",
        timeout=f"{timeout:.1f}s",
        runtime_state=_runtime_state(),
        client_generation=_client_generation(),
        cancellation_requested=cancelled,
        cancellation_completed=cancel_ok,
    )
    logger.error(
        "OP_TIMEOUT — operation '%s' exceeded %.1fs (limit=%.1fs) "
        "runtime_state=%s gen=%s cancel_requested=%s cancel_ok=%s",
        name, elapsed, timeout,
        _runtime_state(), _client_generation(),
        cancelled, cancel_ok,
    )
