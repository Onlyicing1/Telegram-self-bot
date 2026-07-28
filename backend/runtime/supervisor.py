"""
RuntimeSupervisor — central FSM that owns every runtime coroutine.

Managed tasks:
  - self-client (TelegramClient run_until_disconnected + supervision)
  - helper-client (optional, supervised reconnect)
  - bio-cron (cron loop)
  - heartbeat (updates health heartbeat)
  - liveness-probe (RPC-based health check via get_me)
  - panel-timer-manager (inline panel auto-close timers)

The supervisor implements a proper FSM:
  STARTING → CONNECTING → AUTHORIZING → REGISTERING → READY
  READY → DEGRADED (helper down, bio stopped)
  READY/DEGRADED → RECOVERING (transient failure) → READY
  READY/DEGRADED → REBUILDING (repeated failures) → READY/FAILED
  * → STOPPING → (process exit)

Design guarantees:
  - Only one TelegramClient exists at a time (old one fully disconnected
    and removed before a new one is created).
  - Handler registration happens exactly once per client generation.
  - All network operations have bounded timeouts.
  - Exponential backoff with jitter for reconnect/rebuild delays.
  - Deterministic shutdown: every managed task is cancelled and awaited.
"""
import asyncio
import logging
import random
import time
from typing import Any

from telethon import TelegramClient

from backend.runtime.states import RuntimeState
from backend.runtime.managed_task import ManagedTask
from backend.bio import engine as bio_engine
from backend.bot.client import build_client
from backend.bot.router import register_all
from backend.db import client as db_client
from backend.diagnostics import record_event
from backend.health import (
    mark_started,
    set_runtime_state,
    set_telethon_connected,
    set_supervisor_ok,
    set_bio_cron_ok,
    set_helper_connected,
    set_last_rpc,
    set_last_command,
    set_last_update,
    set_heartbeat,
    set_restart_count,
    increment_restart,
    set_last_rebuild_reason,
    set_client_generation,
    set_task_state,
    set_rpc_latency,
    update_heartbeat,
    check_stale,
)
from backend.helper.client import (
    build_helper,
    disconnect_helper,
    get_bot_username,
    get_client as get_helper_client,
)
from backend.helper.panels import register_callback_handlers
from backend.helper.inline_engine import (
    register_inline_handler,
    set_self_client,
    set_helper_username,
    set_owner_id,
)
from backend.helper.inline_sender import register_input_listener
from backend.helper.callback_trace import configure as configure_callback_trace
from backend.helper.panel_settings import load as load_panel_settings

logger = logging.getLogger(__name__)

_CONNECT_TIMEOUT = 30
_AUTHORIZE_TIMEOUT = 15
_GET_ME_TIMEOUT = 15
_LIVENESS_INTERVAL = 60
_LIVENESS_TIMEOUT = 15
_HEARTBEAT_INTERVAL = 5.0
_BACKOFF_BASE = 2.0
_BACKOFF_MAX = 120.0
_BACKOFF_JITTER = 0.3
_MAX_RECONNECT_ATTEMPTS = 3
_MAX_REBUILD_ATTEMPTS = 5
_HELPER_MAX_RECONNECT = 3


def _backoff(attempt: int) -> float:
    base = min(_BACKOFF_MAX, _BACKOFF_BASE * (2 ** attempt))
    jitter = random.uniform(-_BACKOFF_JITTER, _BACKOFF_JITTER) * base
    return max(1.0, base + jitter)


class RuntimeSupervisor:
    __slots__ = (
        "cfg", "owner_id", "tz_str", "api_id", "api_hash",
        "session_string", "bot_token", "port",
        "state", "client", "client_generation",
        "helper_client", "helper_enabled",
        "_reconnect_attempts", "_rebuild_attempts",
        "_helper_reconnect_attempts", "_helper_permanent_failure",
        "shutdown_event", "_managed_tasks",
        "_uvicorn_server",
    )

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.owner_id = cfg["OWNER_ID"]
        self.tz_str = cfg["TZ"]
        self.api_id = cfg["API_ID"]
        self.api_hash = cfg["API_HASH"]
        self.session_string = cfg["SESSION_STRING"]
        self.bot_token = cfg.get("BOT_TOKEN", "")
        self.port = cfg["PORT"]
        self.helper_enabled = bool(cfg.get("HELPER_BOT_ENABLED"))

        self.state: RuntimeState = RuntimeState.STARTING
        self.client: TelegramClient | None = None
        self.client_generation: int = 0
        self.helper_client: TelegramClient | None = None

        self._reconnect_attempts = 0
        self._rebuild_attempts = 0
        self._helper_reconnect_attempts = 0
        self._helper_permanent_failure = False
        self._uvicorn_server: Any = None

        self.shutdown_event = asyncio.Event()
        self._managed_tasks: dict[str, ManagedTask] = {}

    # ── State transitions ──

    def _transition(self, new_state: RuntimeState) -> None:
        if self.state == new_state:
            return
        logger.info("Runtime: %s → %s", self.state, new_state)
        self.state = new_state
        set_runtime_state(str(new_state))

    # ── Startup ──

    async def start(self) -> None:
        mark_started()
        set_supervisor_ok(True)
        self._transition(RuntimeState.STARTING)

        # Phase 1: DB warm-up
        logger.info("[1/5] Database warm-up")
        db = db_client.get_db()
        if db:
            try:
                db.table("bot_logs").select("id").limit(1).execute()
                logger.info("[1/5] Database OK")
            except Exception as exc:
                logger.warning("[1/5] Database warm-up failed (%s) — continuing", exc)
        else:
            logger.info("[1/5] Using in-memory fallback")

        load_panel_settings()

        # Phase 2-3: Connect + authorize + register
        await self._connect_and_register()

        # Phase 3.5: Helper bot
        if self.helper_enabled:
            await self._start_helper()
        else:
            logger.info("[3.5/5] Helper bot: no BOT_TOKEN — inline UI disabled")

        # Phase 4: Bio cron
        logger.info("[4/5] Bio cron resume check")
        try:
            state = db_client.get_bio_state(self.owner_id)
            if state and state.get("is_active"):
                self._start_bio_cron()
                logger.info("[4/5] Bio cron resumed")
            elif self.cfg.get("BIO_UPDATE_ENABLED"):
                self._start_bio_cron()
                logger.info("[4/5] Bio cron started (BIO_UPDATE_ENABLED=true)")
            else:
                logger.info("[4/5] Bio cron not active — skipping")
            set_bio_cron_ok(bio_engine.is_running())
        except Exception as exc:
            logger.warning("[4/5] Bio cron resume check failed: %s", exc)
            set_bio_cron_ok(False)

        # Phase 5: Web server + managed tasks
        logger.info("[5/5] Starting web server on port %s", self.port)
        self._create_managed_task("lifeos-web", self._run_web, watchdog_interval=60)
        self._create_managed_task("lifeos-heartbeat", self._heartbeat, watchdog_interval=15)
        self._create_managed_task("lifeos-liveness", self._liveness_probe, watchdog_interval=120)

        self._transition(RuntimeState.READY)
        set_supervisor_ok(True)
        logger.info("LifeOS online.")

    async def _connect_and_register(self) -> None:
        self._transition(RuntimeState.CONNECTING)
        try:
            self.client = await build_client(
                self.api_id, self.api_hash, self.session_string
            )
            self.client_generation += 1
            set_client_generation(self.client_generation)
            set_telethon_connected(True)
            record_event("runtime", "build_client", 0, "SUCCESS",
                         f"gen={self.client_generation}")
        except Exception as exc:
            logger.error("Failed to build client: %s", exc)
            record_event("runtime", "build_client", 0, "ERROR", str(exc))
            self._transition(RuntimeState.FAILED)
            raise

        self._transition(RuntimeState.AUTHORIZING)
        try:
            if not await asyncio.wait_for(
                self.client.is_user_authorized(), timeout=_AUTHORIZE_TIMEOUT
            ):
                raise RuntimeError("Session not authorized")
            set_last_rpc()
        except asyncio.TimeoutError:
            logger.error("Authorization check timed out")
            record_event("runtime", "authorize", 0, "TIMEOUT")
            self._transition(RuntimeState.FAILED)
            raise
        except Exception as exc:
            logger.error("Authorization failed: %s", exc)
            record_event("runtime", "authorize", 0, "ERROR", str(exc))
            self._transition(RuntimeState.FAILED)
            raise

        self._transition(RuntimeState.REGISTERING)
        register_all(self.client, self.owner_id, self.tz_str)
        set_last_update()
        record_event("runtime", "register_handlers", 0, "SUCCESS",
                     f"gen={self.client_generation}")

        # Start the self-client supervisor task
        self._create_managed_task(
            "lifeos-tg-supervisor",
            lambda: self._supervise_self_client(),
            watchdog_interval=60,
        )

    def _start_bio_cron(self) -> None:
        if self.client is None:
            logger.warning("Cannot start bio cron — no client")
            return
        bio_engine.start_cron(self.client, self.owner_id, self.tz_str)
        set_bio_cron_ok(True)

    # ── Self-client supervision ──

    async def _supervise_self_client(self) -> None:
        """Run the self-client until disconnected, then trigger recovery."""
        if self.client is None:
            return
        client = self.client
        set_telethon_connected(client.is_connected())
        try:
            await client.run_until_disconnected()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Self-client run_until_disconnected error: %s", exc)

        if self.shutdown_event.is_set():
            return

        set_telethon_connected(False)
        increment_restart()
        logger.warning("Self-client disconnected — entering recovery")
        self._transition(RuntimeState.RECOVERING)
        await self._recover_self_client()

    async def _recover_self_client(self) -> None:
        """Attempt reconnect with exponential backoff, then rebuild."""
        client = self.client
        if client is None:
            return

        while self._reconnect_attempts < _MAX_RECONNECT_ATTEMPTS:
            self._reconnect_attempts += 1
            delay = _backoff(self._reconnect_attempts)
            logger.info(
                "Reconnect attempt %d/%d in %.1fs",
                self._reconnect_attempts, _MAX_RECONNECT_ATTEMPTS, delay,
            )
            await asyncio.sleep(delay)

            if self.shutdown_event.is_set():
                return

            try:
                await asyncio.wait_for(client.connect(), timeout=_CONNECT_TIMEOUT)
                if not await asyncio.wait_for(
                    client.is_user_authorized(), timeout=_AUTHORIZE_TIMEOUT
                ):
                    logger.error("Reconnect: session not authorized")
                    continue
                set_telethon_connected(True)
                set_last_rpc()
                self._reconnect_attempts = 0
                self._transition(RuntimeState.READY)
                record_event("runtime", "reconnect", 0, "SUCCESS")
                logger.info("Self-client reconnected successfully")
                # Re-enter run_until_disconnected
                await client.run_until_disconnected()
                if self.shutdown_event.is_set():
                    return
                set_telethon_connected(False)
                self._transition(RuntimeState.RECOVERING)
            except asyncio.TimeoutError:
                logger.warning("Reconnect timed out")
                record_event("runtime", "reconnect", 0, "TIMEOUT")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("Reconnect failed: %s", exc)
                record_event("runtime", "reconnect", 0, "ERROR", str(exc))

        # Reconnect failed — rebuild
        logger.warning("Reconnect exhausted — rebuilding client")
        set_last_rebuild_reason("reconnect_exhausted")
        await self._rebuild_self_client()

    async def _rebuild_self_client(self) -> None:
        """Destroy the old client and create a new one from scratch."""
        self._transition(RuntimeState.REBUILDING)
        self._rebuild_attempts += 1

        if self._rebuild_attempts > _MAX_REBUILD_ATTEMPTS:
            logger.error("Rebuild limit exceeded — entering FAILED state")
            self._transition(RuntimeState.FAILED)
            set_supervisor_ok(False)
            self.shutdown_event.set()
            return

        old_client = self.client
        self.client = None
        set_telethon_connected(False)

        # Fully disconnect and abandon the old client
        if old_client is not None:
            try:
                await asyncio.wait_for(old_client.disconnect(), timeout=10.0)
            except (asyncio.TimeoutError, Exception):
                logger.warning("Old client disconnect during rebuild timed out")
            old_client = None

        # Stop the old supervisor task's watchdog without cancelling the
        # currently-executing task (which IS the old supervisor task).
        old_task = self._managed_tasks.pop("lifeos-tg-supervisor", None)
        if old_task:
            await old_task.stop_watchdog(timeout=5.0)

        delay = _backoff(self._rebuild_attempts)
        logger.info("Rebuild attempt %d in %.1fs", self._rebuild_attempts, delay)
        await asyncio.sleep(delay)

        if self.shutdown_event.is_set():
            return

        try:
            new_client = await build_client(
                self.api_id, self.api_hash, self.session_string
            )
            self.client = new_client
            self.client_generation += 1
            set_client_generation(self.client_generation)
            set_telethon_connected(True)
            set_last_rpc()

            # Re-register all handlers on the new client
            register_all(new_client, self.owner_id, self.tz_str)

            # Re-wire inline panel dependencies onto the new client
            if self.helper_enabled:
                set_self_client(new_client)
                configure_callback_trace(new_client, self.owner_id)
                register_input_listener(new_client, self.owner_id)

            record_event("runtime", "rebuild", 0, "SUCCESS",
                         f"gen={self.client_generation}")
            logger.info("Self-client rebuilt (gen=%d)", self.client_generation)

            self._reconnect_attempts = 0
            self._rebuild_attempts = 0
            self._transition(RuntimeState.READY)

            # Start a new supervisor task for the new client
            self._create_managed_task(
                "lifeos-tg-supervisor",
                lambda: self._supervise_self_client(),
                watchdog_interval=60,
            )

            # Restart bio cron with the new client
            try:
                state = db_client.get_bio_state(self.owner_id)
                if state and state.get("is_active"):
                    await bio_engine.stop_cron()
                    await asyncio.sleep(0.5)
                    bio_engine.start_cron(new_client, self.owner_id, self.tz_str)
                    set_bio_cron_ok(True)
            except Exception:
                pass

        except Exception as exc:
            logger.error("Rebuild failed: %s", exc)
            record_event("runtime", "rebuild", 0, "ERROR", str(exc))
            set_last_rebuild_reason(f"rebuild_error: {exc}")
            await self._rebuild_self_client()

    # ── Helper client ──

    async def _start_helper(self) -> None:
        logger.info("[3.5/5] Starting helper bot")
        try:
            self.helper_client = await build_helper(self.bot_token)
            if self.helper_client is not None:
                register_callback_handlers(self.helper_client, self.owner_id)
                register_inline_handler(self.helper_client, self.owner_id)
                set_self_client(self.client)
                set_helper_username(get_bot_username())
                set_owner_id(self.owner_id)
                configure_callback_trace(self.client, self.owner_id)
                register_input_listener(self.client, self.owner_id)
                set_helper_connected(True)
                self._create_managed_task(
                    "lifeos-helper-supervisor",
                    lambda: self._supervise_helper(),
                    watchdog_interval=30,
                )
                logger.info("[3.5/5] Helper bot online — Inline Mode enabled")
        except Exception:
            logger.exception("[3.5/5] Helper bot failed — inline UI disabled")
            self.helper_client = None
            set_helper_connected(False)

    async def _supervise_helper(self) -> None:
        helper = self.helper_client
        if helper is None:
            return
        try:
            await helper.run_until_disconnected()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Helper run_until_disconnected error: %s", exc)

        if self.shutdown_event.is_set():
            return

        set_helper_connected(False)
        await self._recover_helper()

    async def _recover_helper(self) -> None:
        """Reconnect helper with backoff, rebuild after repeated failures."""
        if self._helper_permanent_failure:
            logger.warning("Helper permanently failed — not retrying")
            return

        helper = self.helper_client
        if helper is None:
            return

        while self._helper_reconnect_attempts < _HELPER_MAX_RECONNECT:
            self._helper_reconnect_attempts += 1
            delay = _backoff(self._helper_reconnect_attempts)
            logger.info(
                "Helper reconnect attempt %d/%d in %.1fs",
                self._helper_reconnect_attempts, _HELPER_MAX_RECONNECT, delay,
            )
            await asyncio.sleep(delay)

            if self.shutdown_event.is_set():
                return

            try:
                await asyncio.wait_for(helper.connect(), timeout=_CONNECT_TIMEOUT)
                if helper.is_connected():
                    set_helper_connected(True)
                    self._helper_reconnect_attempts = 0
                    logger.info("Helper reconnected successfully")
                    await helper.run_until_disconnected()
                    if self.shutdown_event.is_set():
                        return
                    set_helper_connected(False)
            except asyncio.TimeoutError:
                logger.warning("Helper reconnect timed out")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("Helper reconnect failed: %s", exc)

        # Rebuild helper
        logger.warning("Helper reconnect exhausted — rebuilding")
        set_last_rebuild_reason("helper_reconnect_exhausted")
        await self._rebuild_helper()

    async def _rebuild_helper(self) -> None:
        old = self.helper_client
        self.helper_client = None

        if old is not None:
            try:
                await asyncio.wait_for(old.disconnect(), timeout=10.0)
            except (asyncio.TimeoutError, Exception):
                pass

        old_task = self._managed_tasks.pop("lifeos-helper-supervisor", None)
        if old_task:
            await old_task.stop(timeout=5.0)

        delay = _backoff(1)
        await asyncio.sleep(delay)

        if self.shutdown_event.is_set():
            return

        try:
            new_client = await build_helper(self.bot_token)
            if new_client is None:
                self._helper_permanent_failure = True
                return

            self.helper_client = new_client
            register_callback_handlers(new_client, self.owner_id)
            register_inline_handler(new_client, self.owner_id)
            set_self_client(self.client)
            set_helper_username(get_bot_username())
            set_owner_id(self.owner_id)
            set_helper_connected(True)
            self._helper_reconnect_attempts = 0
            record_event("runtime", "helper_rebuild", 0, "SUCCESS")

            self._create_managed_task(
                "lifeos-helper-supervisor",
                lambda: self._supervise_helper(),
                watchdog_interval=30,
            )
        except Exception as exc:
            logger.error("Helper rebuild failed: %s", exc)
            record_event("runtime", "helper_rebuild", 0, "ERROR", str(exc))
            self._helper_permanent_failure = True
            set_helper_connected(False)

    # ── Heartbeat ──

    async def _heartbeat(self) -> None:
        while not self.shutdown_event.is_set():
            try:
                update_heartbeat()
                check_stale()
                set_heartbeat()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("Heartbeat error: %s", exc)
            await asyncio.sleep(_HEARTBEAT_INTERVAL)

    # ── Liveness probe ──

    async def _liveness_probe(self) -> None:
        """RPC-based liveness check. Uses get_me(), never 'last update'."""
        while not self.shutdown_event.is_set():
            await asyncio.sleep(_LIVENESS_INTERVAL)
            if self.shutdown_event.is_set():
                return

            client = self.client
            if client is None or not client.is_connected():
                logger.warning("Liveness: client not connected — skipping")
                continue

            t0 = time.monotonic()
            try:
                await asyncio.wait_for(client.get_me(), timeout=_LIVENESS_TIMEOUT)
                latency_ms = (time.monotonic() - t0) * 1000
                set_last_rpc()
                set_rpc_latency(latency_ms)
                record_event("runtime", "liveness_probe", latency_ms, "SUCCESS")
            except asyncio.TimeoutError:
                logger.warning("Liveness: get_me timed out — forcing disconnect")
                set_rpc_latency(_LIVENESS_TIMEOUT * 1000)
                record_event("runtime", "liveness_probe", 0, "TIMEOUT")
                set_last_rebuild_reason("liveness_timeout")
                try:
                    await asyncio.wait_for(client.disconnect(), timeout=10.0)
                except (asyncio.TimeoutError, Exception):
                    pass
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("Liveness: get_me failed (%s) — forcing disconnect", exc)
                record_event("runtime", "liveness_probe", 0, "ERROR", str(exc))
                set_last_rebuild_reason(f"liveness_error: {exc}")
                try:
                    await asyncio.wait_for(client.disconnect(), timeout=10.0)
                except (asyncio.TimeoutError, Exception):
                    pass

    # ── Web server ──

    async def _run_web(self) -> None:
        import uvicorn
        from backend.web.app import app as web_app, set_owner_id as web_set_owner_id

        web_set_owner_id(self.owner_id)
        config = uvicorn.Config(
            web_app,
            host="0.0.0.0",
            port=self.port,
            log_level="warning",
            access_log=False,
        )
        self._uvicorn_server = uvicorn.Server(config)
        await self._uvicorn_server.serve()

    # ── Managed task helpers ──

    def _create_managed_task(
        self, name: str, factory: callable, watchdog_interval: float = 30.0
    ) -> None:
        old = self._managed_tasks.pop(name, None)
        if old:
            asyncio.create_task(old.stop(timeout=5.0))
        task = ManagedTask(name, factory, watchdog_interval=watchdog_interval)
        self._managed_tasks[name] = task
        task.start()
        set_task_state(name, task.state())

    # ── Shutdown ──

    async def stop(self) -> None:
        logger.info("Shutdown initiated")
        self._transition(RuntimeState.STOPPING)
        self.shutdown_event.set()

        # Stop bio cron deterministically
        logger.info("Shutdown: stopping bio cron")
        await bio_engine.stop_cron()
        set_bio_cron_ok(False)

        # Signal web server
        logger.info("Shutdown: signalling web server")
        if self._uvicorn_server is not None:
            self._uvicorn_server.should_exit = True

        # Stop all managed tasks
        logger.info("Shutdown: stopping managed tasks")
        for name, mt in list(self._managed_tasks.items()):
            try:
                await mt.stop(timeout=10.0)
                set_task_state(name, "STOPPED")
            except Exception as exc:
                logger.warning("Shutdown: task '%s' stop error: %s", name, exc)
        self._managed_tasks.clear()

        # Disconnect helper
        if self.helper_client is not None:
            logger.info("Shutdown: disconnecting helper bot")
            try:
                await disconnect_helper()
            except Exception as exc:
                logger.warning("Helper disconnect: %s", exc)
            set_helper_connected(False)

        # Disconnect self-client
        if self.client is not None:
            logger.info("Shutdown: disconnecting Telethon")
            try:
                await asyncio.wait_for(self.client.disconnect(), timeout=10.0)
            except (asyncio.TimeoutError, Exception) as exc:
                logger.warning("Telethon disconnect: %s", exc)
            set_telethon_connected(False)

        set_supervisor_ok(False)
        logger.info("LifeOS stopped cleanly.")

    # ── Health snapshot ──

    def task_states(self) -> dict[str, str]:
        states = {}
        for name, mt in self._managed_tasks.items():
            states[name] = mt.state()
        return states
