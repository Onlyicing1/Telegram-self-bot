"""
ManagedTask — a supervised asyncio task with automatic restart.

Each ManagedTask wraps a coroutine in a lifecycle:
  - start() creates the asyncio task
  - _watch() monitors the task; if it exits unexpectedly, restarts it
  - stop() cancels and awaits the task deterministically
  - the watchdog loop checks every _watchdog_interval seconds

The task coroutine receives no arguments — it must close over its own
dependencies. The factory is called on every (re)start so a fresh
coroutine is created each time.

Guarantees:
  - At most one live task at a time
  - Cancellation is awaited (no orphaned tasks)
  - Unexpected exits are logged and the task is recreated
  - The watchdog itself is a managed task
"""
import asyncio
import logging

logger = logging.getLogger(__name__)


class ManagedTask:
    __slots__ = ("name", "factory", "task", "_watchdog_task",
                 "_watchdog_interval", "_restart_delay", "_running",
                 "_stop_requested")

    def __init__(
        self,
        name: str,
        factory: callable,
        watchdog_interval: float = 30.0,
        restart_delay: float = 2.0,
    ):
        self.name = name
        self.factory = factory
        self.task: asyncio.Task | None = None
        self._watchdog_task: asyncio.Task | None = None
        self._watchdog_interval = watchdog_interval
        self._restart_delay = restart_delay
        self._running = False
        self._stop_requested = False

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._stop_requested = False
        self._spawn()
        self._watchdog_task = asyncio.create_task(
            self._watch(), name=f"{self.name}-watchdog"
        )

    def _spawn(self) -> None:
        if self._stop_requested:
            return
        coro = self.factory()
        self.task = asyncio.create_task(coro, name=self.name)
        logger.info("ManagedTask '%s' started", self.name)

    async def _watch(self) -> None:
        while self._running and not self._stop_requested:
            await asyncio.sleep(self._watchdog_interval)
            if self._stop_requested:
                return
            if self.task is None or self.task.done():
                if self._stop_requested:
                    return
                exc = None
                if self.task and not self.task.cancelled():
                    exc = self.task.exception()
                if exc:
                    logger.warning(
                        "ManagedTask '%s' died with: %s — restarting in %.1fs",
                        self.name, exc, self._restart_delay,
                    )
                else:
                    logger.warning(
                        "ManagedTask '%s' exited unexpectedly — restarting in %.1fs",
                        self.name, self._restart_delay,
                    )
                await asyncio.sleep(self._restart_delay)
                if self._stop_requested:
                    return
                self._spawn()

    async def stop(self, timeout: float = 10.0) -> None:
        self._stop_requested = True
        self._running = False

        if self._watchdog_task and not self._watchdog_task.done():
            self._watchdog_task.cancel()
            try:
                await asyncio.wait_for(self._watchdog_task, timeout=timeout)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
            self._watchdog_task = None

        if self.task and not self.task.done():
            self.task.cancel()
            try:
                await asyncio.wait_for(self.task, timeout=timeout)
            except (asyncio.CancelledError, asyncio.TimeoutError, Exception):
                pass
        self.task = None

    async def stop_watchdog(self, timeout: float = 5.0) -> None:
        """Stop the watchdog without cancelling the main task."""
        self._stop_requested = True
        self._running = False
        if self._watchdog_task and not self._watchdog_task.done():
            self._watchdog_task.cancel()
            try:
                await asyncio.wait_for(self._watchdog_task, timeout=timeout)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
            self._watchdog_task = None

    @property
    def is_alive(self) -> bool:
        return self.task is not None and not self.task.done()

    def state(self) -> str:
        if self.task is None:
            return "STOPPED"
        if self.task.done():
            if self.task.cancelled():
                return "CANCELLED"
            exc = self.task.exception()
            if exc:
                return f"FAILED ({type(exc).__name__})"
            return "DONE"
        return "RUNNING"
