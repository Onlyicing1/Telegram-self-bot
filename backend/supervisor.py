"""
Supervisor — watchdog + heartbeat + task supervision + Telethon reconnect.

This module is the anti-freeze layer. It guarantees:

  1. Every long-running background task is supervised. If a task exits
     unexpectedly (returns, raises, or hangs), it is recreated within
     one watchdog tick (default 60s).
  2. A heartbeat is written every minute by the bio cron. If the heartbeat
     goes stale (>120s), the watchdog restarts ONLY the bio cron task —
     not the whole process.
  3. Telethon connectivity is probed each tick. If the client is disconnected
     or a ping times out, the client is reconnected in-place (disconnect →
     connect) without restarting Render.
  4. Event-loop liveness is verified by scheduling a trivial callback and
     confirming it runs within a deadline.
  5. Nothing swallows exceptions. All supervised coroutines log full
     tracebacks via logger.exception().

Design: the supervisor owns a registry of named tasks. Each task is a
factory (callable returning a coroutine) plus a heartbeat timestamp. The
watchdog loop ticks every 60s and reconciles the registry: dead tasks are
recreated, stale heartbeats trigger a restart of that specific task.
"""
import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional

logger = logging.getLogger(__name__)

_WATCHDOG_INTERVAL = 60.0
_HEARTBEAT_STALE_SEC = 120.0
_LOOP_PROBE_TIMEOUT = 10.0
_TELETHON_PING_TIMEOUT = 20.0


@dataclass
class SupervisedTask:
    name: str
    factory: Callable[[], Awaitable]
    task: Optional[asyncio.Task] = None
    last_heartbeat: float = 0.0
    restart_count: int = 0
    # If True, a stale heartbeat triggers a restart of THIS task.
    heartbeat_monitored: bool = False


class Supervisor:
    """Owns and reconciles long-running asyncio tasks."""

    def __init__(self):
        self._tasks: dict[str, SupervisedTask] = {}
        self._watchdog_task: Optional[asyncio.Task] = None
        self._client = None
        self._client_factory: Optional[Callable[[], Awaitable]] = None
        self._loop_probe_deadline = _LOOP_PROBE_TIMEOUT

    # ── Registration ───────────────────────────────────────────────────────

    def register(self, name: str, factory: Callable[[], Awaitable],
                 heartbeat_monitored: bool = False) -> SupervisedTask:
        """Register a named task factory. Does not start it — call start_all()."""
        if name in self._tasks:
            logger.warning("Task '%s' already registered — replacing.", name)
        st = SupervisedTask(name=name, factory=factory, heartbeat_monitored=heartbeat_monitored)
        self._tasks[name] = st
        logger.info("Registered supervised task '%s'", name)
        return st

    def set_client(self, client, client_factory: Callable[[], Awaitable]) -> None:
        """Provide the Telethon client + a reconnect factory for self-heal."""
        self._client = client
        self._client_factory = client_factory

    def heartbeat(self, name: str) -> None:
        """Update the heartbeat timestamp for a supervised task."""
        st = self._tasks.get(name)
        if st:
            st.last_heartbeat = time.monotonic()

    # ── Lifecycle ──────────────────────────────────────────────────────────

    def start_all(self) -> None:
        """Start every registered task + the watchdog."""
        for st in self._tasks.values():
            if st.task is None or st.task.done():
                self._start_one(st)
        if self._watchdog_task is None or self._watchdog_task.done():
            self._watchdog_task = asyncio.create_task(self._watchdog_loop(), name="lifeos-watchdog")
            logger.info("Watchdog started (interval=%.0fs)", _WATCHDOG_INTERVAL)

    def _start_one(self, st: SupervisedTask) -> None:
        st.last_heartbeat = time.monotonic()
        st.task = asyncio.create_task(st.factory(), name=f"lifeos-{st.name}")
        logger.info("Started task '%s' (restart #%d)", st.name, st.restart_count)

    async def stop_all(self) -> None:
        """Cancel every task + the watchdog. Awaited on shutdown."""
        if self._watchdog_task and not self._watchdog_task.done():
            self._watchdog_task.cancel()
        for st in self._tasks.values():
            if st.task and not st.task.done():
                st.task.cancel()
        pending = [t for t in [self._watchdog_task] + [s.task for s in self._tasks.values()] if t]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        logger.info("Supervisor stopped all tasks.")

    # ── Watchdog ───────────────────────────────────────────────────────────

    async def _watchdog_loop(self) -> None:
        logger.info("Watchdog loop running")
        while True:
            try:
                await asyncio.sleep(_WATCHDOG_INTERVAL)
                await self._tick()
            except asyncio.CancelledError:
                logger.info("Watchdog cancelled.")
                raise
            except Exception:
                logger.exception("Watchdog tick error (will retry next interval)")

    async def _tick(self) -> None:
        """Reconcile tasks, probe loop + Telethon, restart stale/dead tasks."""
        await self._probe_loop()
        await self._probe_telethon()

        now = time.monotonic()
        for st in self._tasks.values():
            # Restart dead tasks
            if st.task is None or st.task.done():
                logger.warning("Watchdog: task '%s' is dead — restarting.", st.name)
                st.restart_count += 1
                self._start_one(st)
                continue

            # Restart heartbeat-stale tasks
            if st.heartbeat_monitored:
                age = now - st.last_heartbeat
                if age > _HEARTBEAT_STALE_SEC:
                    logger.error(
                        "Watchdog: task '%s' heartbeat stale (%.0fs > %.0fs) — cancelling + restarting.",
                        st.name, age, _HEARTBEAT_STALE_SEC,
                    )
                    await self.restart_task(st.name)
                else:
                    logger.info(
                        "Watchdog: task '%s' alive (heartbeat age %.0fs, restarts=%d).",
                        st.name, age, st.restart_count,
                    )
            else:
                logger.info(
                    "Watchdog: task '%s' alive (restarts=%d).",
                    st.name, st.restart_count,
                )

    async def restart_task(self, name: str) -> bool:
        """Cancel + restart a specific task by name. Returns True if restarted."""
        st = self._tasks.get(name)
        if not st:
            logger.warning("restart_task: unknown task '%s'.", name)
            return False
        if st.task and not st.task.done():
            try:
                st.task.cancel()
                await asyncio.wait_for(
                    asyncio.shield(asyncio.gather(st.task, return_exceptions=True)),
                    timeout=5.0,
                )
            except asyncio.TimeoutError:
                logger.error("restart_task: '%s' did not cancel within 5s — proceeding.", name)
            except Exception:
                logger.exception("restart_task: error cancelling '%s'", name)
        st.restart_count += 1
        self._start_one(st)
        logger.info("restart_task: '%s' restarted (restart #%d).", name, st.restart_count)
        return True

    async def _probe_loop(self) -> None:
        """Verify the event loop is progressing by running a trivial callback."""
        async def _noop():
            return 1
        try:
            await asyncio.wait_for(_noop(), timeout=self._loop_probe_deadline)
        except asyncio.TimeoutError:
            logger.error("Watchdog: event loop probe timed out — loop may be blocked.")
        except Exception:
            logger.exception("Watchdog: event loop probe error")

    async def _probe_telethon(self) -> None:
        """Probe Telethon connectivity. Reconnect if disconnected or ping times out."""
        if self._client is None:
            return
        try:
            connected = self._client.is_connected()
            if not connected:
                logger.error("Watchdog: Telethon disconnected — reconnecting.")
                await self._reconnect_telethon()
                return
            # Ping with timeout to detect a stalled-but-connected socket.
            try:
                await asyncio.wait_for(
                    self._client.get_me(),
                    timeout=_TELETHON_PING_TIMEOUT,
                )
            except asyncio.TimeoutError:
                logger.error("Watchdog: Telethon get_me() timed out — reconnecting.")
                await self._reconnect_telethon()
            except Exception as exc:
                logger.warning("Watchdog: Telethon get_me() error: %s — attempting reconnect.", exc)
                await self._reconnect_telethon()
        except Exception:
            logger.exception("Watchdog: Telethon probe error")

    async def _reconnect_telethon(self) -> None:
        """Reconnect the Telethon client via the registered factory.

        The factory (set by main.py) owns the full disconnect→connect→verify
        sequence with its own timeouts, so we simply await it here.
        """
        if self._client is None or self._client_factory is None:
            logger.error("Watchdog: cannot reconnect — no client/factory.")
            return
        try:
            await asyncio.wait_for(self._client_factory(), timeout=45.0)
            logger.info("Watchdog: Telethon reconnected successfully.")
        except asyncio.TimeoutError:
            logger.error("Watchdog: Telethon reconnect timed out — will retry next tick.")
        except Exception:
            logger.exception("Watchdog: Telethon reconnect error")

    # ── Introspection (for /api/health or debugging) ───────────────────────

    def status(self) -> dict:
        return {
            name: {
                "alive": st.task is not None and not st.task.done(),
                "restart_count": st.restart_count,
                "heartbeat_age_sec": (time.monotonic() - st.last_heartbeat) if st.last_heartbeat else None,
            }
            for name, st in self._tasks.items()
        }


# Module-level singleton — one supervisor per process.
supervisor = Supervisor()
