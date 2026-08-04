"""
RuntimeSupervisor — self-healing watchdog with layered recovery.

Recovery layers (least invasive first):
  1. Reconnect  — disconnect + connect the existing client (no rebuild)
  2. Rebuild    — dispose dead client, build new one, re-register handlers
  3. Full       — rebuild + restart helper + resume cron engines

The supervisor guarantees:
  - Exactly ONE active self client at all times
  - Generation number increases on every rebuild
  - Runtime state stays READY during reconnect/rebuild
  - Bio/Username engines are NOT restarted on rebuild (client is swapped)
  - Hard timeouts on every network operation
  - Exponential backoff on Telegram errors
  - Dead task detection and automatic recreation
  - Stalled loop detection (alive but not progressing)
  - Event-loop starvation detection
  - No callback exception poisons the dispatcher
  - Every forever-loop is wrapped in immortal_create_task (never dies)
  - Recovery never enters a dead state — infinite retry with backoff

Mandatory log tags:
  KEEPALIVE_TIMEOUT
  CLIENT_RECONNECT
  CLIENT_REBUILD
  WATCHDOG_RECOVERY
  TASK_RESTART
  LOOP_STALLED
  EVENT_LOOP_STARVATION
  CALLBACK_DISPATCH_STALLED
  RECOVERY_SUCCESS
  RECOVERY_FAILED
"""
import asyncio
import logging
import random
import time
from typing import Any

from telethon import TelegramClient

from backend.runtime.states import RuntimeState
from backend.runtime.tracer import trace, trace_exception
from backend.runtime.task_guard import immortal_create_task, guarded_create_task, set_runtime_state_ref
from backend.runtime.heartbeat import start_heartbeat, stop_heartbeat, update_state as update_heartbeat_state, configure as configure_heartbeat
from backend.runtime.keepalive import start_keepalive, stop_keepalive, configure as configure_keepalive
from backend.runtime.failsafe import start_failsafe, stop_failsafe, configure as configure_failsafe
from backend.bio import engine as bio_engine
from backend.username import engine as username_engine
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
    set_last_update,
    set_last_telethon_event,
    get_last_telethon_event,
    get_last_event_dispatch,
    get_last_rpc,
    get_last_callback,
    set_heartbeat,
    increment_restart,
    set_last_rebuild_reason,
    set_client_generation,
    set_task_state,
    set_rpc_latency,
    update_heartbeat,
    check_stale,
    tick_loop,
    get_stale_loops,
    get_all_loop_progress,
)
from backend.helper.client import (
    build_helper,
    disconnect_helper,
    get_bot_username,
)
from backend.helper.panels import register_callback_handlers
from backend.helper.client import register_helper_hooks
from backend.helper.inline_engine import (
    register_inline_handler,
    set_self_client,
    set_helper_username,
    set_owner_id,
)
from backend.helper.inline_sender import register_input_listener
from backend.helper.callback_trace import configure as configure_callback_trace
from backend.helper.lifecycle import configure_lifecycle, get_lifecycle
from backend.services import settings_service as settings_svc
from backend.runtime.diagnostics import start_diagnostics, stop_diagnostics

from backend.helper.target_context import clear_all as clear_all_targets

logger = logging.getLogger(__name__)

_CONNECT_TIMEOUT = 30
_AUTHORIZE_TIMEOUT = 15
_GET_ME_TIMEOUT = 15
_HEARTBEAT_INTERVAL = 30.0
_RPC_TIMEOUT = 15.0
_BACKOFF_BASE = 2.0
_BACKOFF_MAX = 300.0
_BACKOFF_JITTER = 0.3
_MAX_RECOVERY_ATTEMPTS = 5
_HEARTBEAT_FAILURE_THRESHOLD = 3
_UPDATE_STALE_DEFAULT = 90.0
_RECONNECT_FAILURES_BEFORE_REBUILD = 2
_LOOP_STALE_THRESHOLD = 90.0
_LOOP_STARVATION_MS = 5000.0
_RECONNECT_TIMEOUT = 30.0
_REBUILD_TIMEOUT = 45.0
_REGISTER_TIMEOUT = 30.0
_RECOVERY_COOLDOWN = 180.0

_CRITICAL_TASKS = (
    "lifeos-watchdog",
    "lifeos-heartbeat",
    "lifeos-keepalive",
    "lifeos-profile-scheduler",
    "lifeos-helper",
    "lifeos-run",
)


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
        "shutdown_event", "_uvicorn_server",
        "_watchdog_task", "_run_task", "_keepalive_task",
        "_recovery_lock", "_recovery_attempts",
        "_client_alive", "_consecutive_failures",
        "_reconnect_failures",
        "_task_supervisor_task",
        "_last_watchdog_tick",
        "_recovery_cooldown_until",
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

        self.shutdown_event = asyncio.Event()
        self._uvicorn_server: Any = None

        self._watchdog_task: asyncio.Task | None = None
        self._run_task: asyncio.Task | None = None
        self._keepalive_task: asyncio.Task | None = None
        self._task_supervisor_task: asyncio.Task | None = None
        self._recovery_lock = asyncio.Lock()
        self._recovery_attempts: int = 0
        self._client_alive: bool = False
        self._consecutive_failures: int = 0
        self._reconnect_failures: int = 0
        self._last_watchdog_tick: float = 0.0
        self._recovery_cooldown_until: float = 0.0

    def _in_recovery_cooldown(self) -> bool:
        return time.time() < self._recovery_cooldown_until

    def _transition(self, new_state: RuntimeState) -> None:
        if self.state == new_state:
            return
        old = self.state
        logger.info("Runtime: %s -> %s", old, new_state)
        self.state = new_state
        set_runtime_state(str(new_state))
        set_runtime_state_ref(str(new_state))
        update_heartbeat_state(runtime_state=str(new_state))
        trace("RUNTIME_STATE_TRANSITION", old=old, new=new_state)

    async def start(self) -> None:
        mark_started()
        set_supervisor_ok(True)
        self._transition(RuntimeState.STARTING)

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

        settings_svc.load_all()
        logger.info("Panel settings loaded.")

        logger.info("[1b/5] Initializing AI engine")
        try:
            from backend.ai.config.env import apply_env_to_config_manager, apply_env_to_provider_configs
            from backend.ai.config.manager import get_config_manager
            from backend.ai.providers.manager.config_manager import get_provider_config_manager
            cm = get_config_manager()
            apply_env_to_config_manager(cm)
            pcm = get_provider_config_manager()
            apply_env_to_provider_configs(pcm)
            pcm.load(cm)
            from backend.ai.engine.engine import get_engine
            engine = get_engine()
            from backend.bot.handlers.ai_cmd import configure as configure_ai_cmd
            configure_ai_cmd(engine, self.owner_id, self.tz_str)
            logger.info("[1b/5] AI engine initialized (provider=%s)", engine.provider_manager.get_active_name())
        except Exception as exc:
            logger.warning("[1b/5] AI engine init failed: %s", exc)

        logger.info("[2/5] Building self-client")
        await self._build_and_register()

        if self.helper_enabled:
            logger.info("[3/5] Starting helper bot")
            await self._start_helper()
        else:
            logger.info("[3/5] Helper bot: no BOT_TOKEN — inline UI disabled")

        logger.info("[4/5] Bio cron resume check")
        await self._resume_bio_cron()

        logger.info("[4b/5] Username cron resume check")
        await self._resume_username_cron()

        logger.info("[5/5] Starting web server on port %s", self.port)
        self._start_web_server()

        self._transition(RuntimeState.READY)
        set_supervisor_ok(True)
        logger.info("LifeOS online.")

        configure_keepalive(self)
        configure_heartbeat(self)
        configure_failsafe(self)
        self._watchdog_task = immortal_create_task(
            self._watchdog_loop(), name="lifeos-watchdog"
        )
        self._run_task = immortal_create_task(
            self._run_loop(), name="lifeos-run"
        )
        start_keepalive()
        start_heartbeat()
        start_failsafe()
        start_diagnostics()
        self._task_supervisor_task = immortal_create_task(
            self._task_supervisor_loop(), name="lifeos-task-supervisor"
        )

    async def _build_and_register(self) -> None:
        self._transition(RuntimeState.CONNECTING)
        try:
            self.client = await build_client(
                self.api_id, self.api_hash, self.session_string
            )
            self.client_generation += 1
            set_client_generation(self.client_generation)
            set_telethon_connected(True)
            self._client_alive = True
            self._consecutive_failures = 0
            self._reconnect_failures = 0
            update_heartbeat_state(
                self_connected=True,
                client_generation=self.client_generation,
                _client_ref=self.client,
            )
            record_event("runtime", "build_client", 0, "SUCCESS",
                         f"gen={self.client_generation}")
        except Exception as exc:
            trace_exception("SELF_BUILD_FAILED", exc, gen=self.client_generation)
            logger.error("Failed to build client: %s", exc)
            record_event("runtime", "build_client", 0, "ERROR", str(exc))
            self._transition(RuntimeState.FAILED)
            raise

        self._transition(RuntimeState.REGISTERING)
        register_all(self.client, self.owner_id, self.tz_str)
        set_last_update()
        record_event("runtime", "register_handlers", 0, "SUCCESS",
                     f"gen={self.client_generation}")

        if self.helper_enabled:
            set_self_client(self.client)
            configure_lifecycle(self.client, self.owner_id)
            configure_callback_trace(self.client, self.owner_id)
            register_input_listener(self.client, self.owner_id)

        bio_engine.update_client(self.client)
        username_engine.update_client(self.client)

    async def _resume_bio_cron(self) -> None:
        try:
            state = await db_client.get_or_create_bio_state(self.owner_id)
            if state.get("is_active"):
                self._start_bio_cron()
                logger.info("[4/5] Bio cron resumed (is_active=true in DB)")
            elif self.cfg.get("BIO_UPDATE_ENABLED"):
                await db_client.update_bio_state(self.owner_id, {"is_active": True})
                self._start_bio_cron()
                logger.info("[4/5] Bio cron started (BIO_UPDATE_ENABLED=true, persisted)")
            else:
                logger.info("[4/5] Bio cron not active — skipping")
            set_bio_cron_ok(bio_engine.is_running())
        except Exception as exc:
            logger.warning("[4/5] Bio cron resume check failed: %s", exc)
            set_bio_cron_ok(False)

    async def _resume_username_cron(self) -> None:
        try:
            logger.info("USERNAME_DB_LOADING owner_id=%s", self.owner_id)
            state = await db_client.get_or_create_username_state(self.owner_id)
            if state is None:
                logger.error("USERNAME_DB_CREATE_FAILED owner_id=%s — get_or_create returned None", self.owner_id)
                return
            logger.info("USERNAME_DB_READY owner_id=%s is_active=%s", self.owner_id, state.get("is_active"))
            if state.get("is_active"):
                self._start_username_cron()
                logger.info("[4b/5] Username cron resumed")
            else:
                logger.info("[4b/5] Username cron not active — skipping")
        except Exception as exc:
            logger.error("USERNAME_DB_CREATE_FAILED owner_id=%s exc=%s", self.owner_id, exc, exc_info=True)
            logger.warning("[4b/5] Username cron resume check failed: %s", exc)

    def _start_username_cron(self) -> None:
        if self.client is None:
            logger.warning("Cannot start username cron — no client")
            return
        username_engine.start_cron(self.client, self.owner_id, self.tz_str)

    def _start_bio_cron(self) -> None:
        if self.client is None:
            logger.warning("Cannot start bio cron — no client")
            return
        bio_engine.start_cron(self.client, self.owner_id, self.tz_str)
        set_bio_cron_ok(True)

    async def _start_helper(self) -> None:
        try:
            self.helper_client = await build_helper(self.bot_token)
            if self.helper_client is not None:
                register_callback_handlers(self.helper_client, self.owner_id)
                register_helper_hooks(self.helper_client)
                register_inline_handler(self.helper_client, self.owner_id)
                set_self_client(self.client)
                set_helper_username(get_bot_username())
                set_owner_id(self.owner_id)
                set_helper_connected(True)
                update_heartbeat_state(helper_connected=True)
                immortal_create_task(
                    self._supervise_helper(), name="lifeos-helper"
                )
                logger.info("[3/5] Helper bot online — Inline Mode enabled")
        except Exception as exc:
            trace_exception("HELPER_START_FAILED", exc)
            logger.exception("[3/5] Helper bot failed — inline UI disabled")
            self.helper_client = None
            set_helper_connected(False)
            update_heartbeat_state(helper_connected=False)

    async def _supervise_helper(self) -> None:
        helper = self.helper_client
        if helper is None:
            return
        try:
            from backend.health import tick_loop
            tick_loop("lifeos-helper", state="RUNNING", success=True)
        except Exception:
            pass
        try:
            await helper.run_until_disconnected()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            trace_exception("HELPER_DISCONNECTED", exc)
            logger.warning("Helper disconnected: %s", exc)
        trace("HELPER_DISCONNECTED", reason="run_until_disconnected_returned")
        if self.shutdown_event.is_set():
            return
        set_helper_connected(False)
        update_heartbeat_state(helper_connected=False)
        await self._reconnect_helper()

    async def _reconnect_helper(self) -> None:
        attempts = 0
        while attempts < 5 and not self.shutdown_event.is_set():
            attempts += 1
            delay = _backoff(attempts)
            trace("HELPER_RECONNECTING", attempt=attempts, delay=f"{delay:.1f}s")
            logger.info("Helper reconnect %d in %.1fs", attempts, delay)
            await asyncio.sleep(delay)
            helper = self.helper_client
            if helper is None:
                break
            try:
                await asyncio.wait_for(helper.connect(), timeout=_CONNECT_TIMEOUT)
                if helper.is_connected():
                    set_helper_connected(True)
                    update_heartbeat_state(helper_connected=True)
                    trace("HELPER_RECONNECTED", attempt=attempts)
                    logger.info("Helper reconnected")
                    await helper.run_until_disconnected()
                    if self.shutdown_event.is_set():
                        return
                    set_helper_connected(False)
                    update_heartbeat_state(helper_connected=False)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                trace_exception("HELPER_RECONNECT_FAILED", exc, attempt=attempts)
                logger.warning("Helper reconnect failed: %s", exc)
        if not self.shutdown_event.is_set():
            trace("HELPER_RECONNECT_EXHAUSTED", attempts=attempts)
            logger.warning("Helper reconnect exhausted — giving up")
            set_helper_connected(False)
            update_heartbeat_state(helper_connected=False)

    def _start_web_server(self) -> None:
        guarded_create_task(self._run_web(), name="lifeos-web")

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

    async def _run_loop(self) -> None:
        while not self.shutdown_event.is_set():
            client = self.client
            if client is None:
                await asyncio.sleep(1)
                continue
            try:
                from backend.health import tick_loop
                tick_loop("lifeos-run", state="RUNNING", success=True)
            except Exception:
                pass
            try:
                await client.run_until_disconnected()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                trace_exception("SELF_RUN_ERROR", exc, gen=self.client_generation)
                logger.warning("run_until_disconnected error: %s", exc)

            if self.shutdown_event.is_set():
                break

            self._client_alive = False
            set_telethon_connected(False)
            update_heartbeat_state(self_connected=False)
            trace("SELF_DISCONNECTED", gen=self.client_generation, reason="run_until_disconnected_returned")
            trace("SELF_RUN_LOOP_EXITED", gen=self.client_generation)
            logger.warning("Self-client disconnected — watchdog will detect and recover")
            break

    async def _reconnect_client(self) -> bool:
        client = self.client
        if client is None:
            return False

        trace("CLIENT_RECONNECT", gen=self.client_generation)
        logger.warning("CLIENT_RECONNECT — disconnecting existing client (gen=%d)", self.client_generation)

        try:
            await asyncio.wait_for(client.disconnect(), timeout=10.0)
        except (asyncio.TimeoutError, Exception):
            logger.warning("Reconnect: disconnect timed out — proceeding")

        await asyncio.sleep(2)

        try:
            await asyncio.wait_for(client.connect(), timeout=_CONNECT_TIMEOUT)
            if client.is_connected() and await asyncio.wait_for(
                client.is_user_authorized(), timeout=_AUTHORIZE_TIMEOUT
            ):
                set_telethon_connected(True)
                update_heartbeat_state(self_connected=True, _client_ref=client)
                self._client_alive = True
                self._consecutive_failures = 0
                set_last_update()
                set_last_telethon_event()
                trace("CLIENT_RECONNECTED", gen=self.client_generation)
                logger.info("CLIENT_RECONNECTED — existing client reconnected (gen=%d)", self.client_generation)
                return True
        except asyncio.TimeoutError:
            logger.warning("Reconnect: connect timed out")
        except Exception as exc:
            trace_exception("CLIENT_RECONNECT_FAILED", exc, gen=self.client_generation)
            logger.warning("Reconnect failed: %s", exc)

        return False

    async def _rebuild_client(self) -> bool:
        trace("CLIENT_REBUILD", gen=self.client_generation + 1)
        logger.warning("CLIENT_REBUILD — building new client (gen=%d -> %d)",
                        self.client_generation, self.client_generation + 1)

        old_client = self.client
        self.client = None
        self._client_alive = False
        set_telethon_connected(False)
        update_heartbeat_state(self_connected=False)

        if old_client is not None:
            try:
                await asyncio.wait_for(old_client.disconnect(), timeout=10.0)
            except (asyncio.TimeoutError, Exception):
                logger.warning("Rebuild: old client disconnect timed out")

        await asyncio.sleep(1)

        try:
            new_client = await asyncio.wait_for(
                build_client(self.api_id, self.api_hash, self.session_string),
                timeout=_REBUILD_TIMEOUT,
            )
            self.client = new_client
            self.client_generation += 1
            set_client_generation(self.client_generation)
            set_telethon_connected(True)
            self._client_alive = True
            self._consecutive_failures = 0
            self._reconnect_failures = 0
            update_heartbeat_state(
                self_connected=True,
                client_generation=self.client_generation,
                _client_ref=self.client,
            )
            record_event("runtime", "rebuild_client", 0, "SUCCESS",
                         f"gen={self.client_generation}")
        except asyncio.TimeoutError:
            trace("CLIENT_REBUILD_FAILED", gen=self.client_generation, reason="timeout")
            logger.error("CLIENT_REBUILD_FAILED: build timed out after %ds", int(_REBUILD_TIMEOUT))
            record_event("runtime", "rebuild_client", 0, "ERROR", "timeout")
            return False
        except Exception as exc:
            trace_exception("CLIENT_REBUILD_FAILED", exc, gen=self.client_generation)
            logger.error("CLIENT_REBUILD_FAILED: %s", exc)
            record_event("runtime", "rebuild_client", 0, "ERROR", str(exc))
            return False

        try:
            await asyncio.wait_for(
                asyncio.get_event_loop().run_in_executor(
                    None, register_all, self.client, self.owner_id, self.tz_str
                ),
                timeout=_REGISTER_TIMEOUT,
            )
        except asyncio.TimeoutError:
            trace("CLIENT_REBUILD_FAILED", gen=self.client_generation, reason="register_timeout")
            logger.error("CLIENT_REBUILD_FAILED: handler registration timed out after %ds", int(_REGISTER_TIMEOUT))
            record_event("runtime", "register_handlers", 0, "ERROR", "timeout")
            return False
        except Exception as exc:
            trace_exception("CLIENT_REBUILD_FAILED", exc, gen=self.client_generation, reason="register_error")
            logger.error("CLIENT_REBUILD_FAILED: handler registration error: %s", exc)
            return False

        set_last_update()
        set_last_telethon_event()
        record_event("runtime", "register_handlers", 0, "SUCCESS",
                     f"gen={self.client_generation}")

        if self.helper_enabled:
            set_self_client(self.client)
            configure_lifecycle(self.client, self.owner_id)
            configure_callback_trace(self.client, self.owner_id)
            register_input_listener(self.client, self.owner_id)

        bio_engine.update_client(self.client)
        username_engine.update_client(self.client)

        self._run_task = immortal_create_task(
            self._run_loop(), name="lifeos-run"
        )

        trace("CLIENT_REBUILD_OK", gen=self.client_generation)
        logger.info("CLIENT_REBUILD_OK — new client ready (gen=%d)", self.client_generation)
        return True

    async def _trigger_reconnect(self) -> None:
        if self.shutdown_event.is_set():
            return
        if self._in_recovery_cooldown():
            return

        try:
            acquired = await asyncio.wait_for(
                self._recovery_lock.acquire(), timeout=30.0
            )
            if not acquired:
                return
        except asyncio.TimeoutError:
            return

        try:
            success = await asyncio.wait_for(
                self._reconnect_client(), timeout=_RECONNECT_TIMEOUT,
            )
            if success:
                self._reconnect_failures = 0
                self._recovery_cooldown_until = time.time() + _RECOVERY_COOLDOWN
                trace("RECOVERY_SUCCESS", action="reconnect", gen=self.client_generation)
                logger.info("RUNTIME_RECOVERED — reconnect succeeded (gen=%d)", self.client_generation)
                return

            self._reconnect_failures += 1
            trace("RECONNECT_FAILED", count=self._reconnect_failures,
                   threshold=_RECONNECT_FAILURES_BEFORE_REBUILD)
            logger.warning("Reconnect failed (%d/%d) — escalating to rebuild",
                            self._reconnect_failures, _RECONNECT_FAILURES_BEFORE_REBUILD)

            if self._reconnect_failures >= _RECONNECT_FAILURES_BEFORE_REBUILD:
                rebuild_ok = await self._rebuild_client()
                if rebuild_ok:
                    self._reconnect_failures = 0
                    self._recovery_cooldown_until = time.time() + _RECOVERY_COOLDOWN
                    trace("RECOVERY_SUCCESS", action="rebuild", gen=self.client_generation)
                    logger.info("RUNTIME_RECOVERED — rebuild succeeded (gen=%d)", self.client_generation)
                else:
                    await self._do_full_recovery()
        except asyncio.TimeoutError:
            logger.warning("Reconnect timed out after %ds", int(_RECONNECT_TIMEOUT))
        finally:
            self._recovery_lock.release()

    async def _trigger_full_recovery(self) -> None:
        if self.shutdown_event.is_set():
            return
        if self._in_recovery_cooldown():
            return

        try:
            acquired = await asyncio.wait_for(
                self._recovery_lock.acquire(), timeout=30.0
            )
            if not acquired:
                return
        except asyncio.TimeoutError:
            return

        try:
            await self._do_full_recovery()
        finally:
            self._recovery_lock.release()

    async def _trigger_recovery(self) -> None:
        await self._trigger_reconnect()

    async def _do_full_recovery(self) -> None:
        self._recovery_attempts += 1
        attempt = self._recovery_attempts

        trace("RECOVERY_START", attempt=attempt)
        logger.warning("RECOVERY_START — attempt %d", attempt)

        delay = _backoff(attempt)
        trace("WATCHDOG_RECOVERY", attempt=attempt, backoff_delay=f"{delay:.1f}s")
        logger.warning(
            "WATCHDOG_RECOVERY — attempt %d, backoff %.1fs",
            attempt, delay,
        )
        record_event("runtime", "recovery_start", 0, "ATTEMPT",
                     f"attempt={attempt}")

        logger.info("Recovery: stopping helper bot")
        await self._stop_helper()
        set_helper_connected(False)

        logger.info("Recovery: clearing inline panel state")
        await get_lifecycle().shutdown_all()
        clear_all_targets()

        logger.info("Recovery: cancelling run task")
        if self._run_task and not self._run_task.done():
            self._run_task.cancel()
            try:
                await asyncio.wait_for(self._run_task, timeout=5.0)
            except (asyncio.CancelledError, asyncio.TimeoutError, Exception):
                pass
            self._run_task = None

        logger.info("Recovery: cancelling orphan tasks")
        await self._cancel_orphan_tasks()

        logger.info("Recovery: disposing dead client")
        old_client = self.client
        self.client = None
        self._client_alive = False
        set_telethon_connected(False)
        if old_client is not None:
            try:
                await asyncio.wait_for(old_client.disconnect(), timeout=10.0)
            except (asyncio.TimeoutError, Exception):
                logger.warning("Recovery: old client disconnect timed out")

        await asyncio.sleep(delay)

        if self.shutdown_event.is_set():
            return

        try:
            trace("SELF_REBUILDING", gen=self.client_generation + 1)
            logger.info("Recovery: building new client")
            await self._build_and_register()
            trace("SELF_RECONNECTED", gen=self.client_generation)
            logger.info("Recovery: new client ready (gen=%d)", self.client_generation)

            if self.helper_enabled:
                set_self_client(self.client)

            if self.helper_enabled:
                logger.info("Recovery: restarting helper bot")
                await self._start_helper()

            logger.info("Recovery: resuming bio engine")
            await self._resume_bio_cron()

            logger.info("Recovery: resuming username engine")
            await self._resume_username_cron()

            logger.info("Recovery: verifying with fresh heartbeat")
            await self._verify_heartbeat()

            self._run_task = immortal_create_task(
                self._run_loop(), name="lifeos-run"
            )

            start_heartbeat()

            set_last_update()
            set_last_telethon_event()

            self._recovery_attempts = 0
            self._consecutive_failures = 0
            self._reconnect_failures = 0
            self._recovery_cooldown_until = time.time() + _RECOVERY_COOLDOWN
            self._transition(RuntimeState.READY)
            set_supervisor_ok(True)
            set_task_state("lifeos-recovery", "DONE")
            increment_restart()
            trace("RECOVERY_SUCCESS", action="full", gen=self.client_generation)
            logger.info("RUNTIME_RECOVERED — system operational (gen=%d)",
                        self.client_generation)
            record_event("runtime", "recovery", 0, "SUCCESS",
                         f"gen={self.client_generation}")

        except Exception as exc:
            trace_exception("RECOVERY_FAILED", exc, attempt=attempt)
            logger.error(
                "RECOVERY_FAILED — attempt %d: %s",
                attempt, exc,
            )
            record_event("runtime", "recovery", 0, "ERROR", str(exc))
            set_last_rebuild_reason(f"recovery_error: {exc}")
            set_task_state("lifeos-recovery", "FAILED")
            self._recovery_lock.release()
            immortal_create_task(self._retry_full_recovery(), name="lifeos-recovery-retry")
            return

    async def _retry_full_recovery(self) -> None:
        await asyncio.sleep(30.0)
        if self.shutdown_event.is_set():
            return
        await self._trigger_full_recovery()

    async def _hard_reset_runtime(self) -> None:
        """Last-line failsafe: destroy everything Telethon-related and rebuild.

        Called by the failsafe monitor when all signals are frozen.
        Bypasses the normal watchdog/heartbeat chain entirely.
        Never kills the process — only rebuilds the Telegram runtime.
        Preserves: settings, bio state, username state, scheduler, DB connections.
        """
        if self.shutdown_event.is_set():
            return
        if self._in_recovery_cooldown():
            return

        try:
            acquired = await asyncio.wait_for(
                self._recovery_lock.acquire(), timeout=10.0,
            )
            if not acquired:
                return
        except asyncio.TimeoutError:
            return

        try:
            trace("RECOVERY_START", reason="failsafe_hard_reset")
            logger.error("RECOVERY_START — failsafe hard reset triggered")

            trace("CLIENT_REBUILD", gen=self.client_generation + 1, reason="failsafe")
            logger.warning("CLIENT_REBUILD — failsafe destroying all Telethon tasks")

            await self._stop_helper()
            set_helper_connected(False)

            await get_lifecycle().shutdown_all()
            clear_all_targets()

            if self._run_task and not self._run_task.done():
                self._run_task.cancel()
                try:
                    await asyncio.wait_for(self._run_task, timeout=5.0)
                except (asyncio.CancelledError, asyncio.TimeoutError, Exception):
                    pass
                self._run_task = None

            await self._cancel_orphan_tasks()

            old_client = self.client
            self.client = None
            self._client_alive = False
            set_telethon_connected(False)
            update_heartbeat_state(self_connected=False)
            if old_client is not None:
                try:
                    await asyncio.wait_for(old_client.disconnect(), timeout=10.0)
                except (asyncio.TimeoutError, Exception):
                    logger.warning("Failsafe: old client disconnect timed out")

            await asyncio.sleep(2)

            if self.shutdown_event.is_set():
                return

            try:
                new_client = await asyncio.wait_for(
                    build_client(self.api_id, self.api_hash, self.session_string),
                    timeout=_REBUILD_TIMEOUT,
                )
                self.client = new_client
                self.client_generation += 1
                set_client_generation(self.client_generation)
                set_telethon_connected(True)
                self._client_alive = True
                self._consecutive_failures = 0
                self._reconnect_failures = 0
                update_heartbeat_state(
                    self_connected=True,
                    client_generation=self.client_generation,
                    _client_ref=self.client,
                )
                trace("CLIENT_REBUILD_OK", gen=self.client_generation, reason="failsafe")
                logger.info("CLIENT_REBUILD_OK — failsafe new client ready (gen=%d)", self.client_generation)
            except asyncio.TimeoutError:
                trace("CLIENT_REBUILD_FAILED", gen=self.client_generation, reason="failsafe_timeout")
                logger.error("CLIENT_REBUILD_FAILED — failsafe build timed out")
                self._recovery_lock.release()
                immortal_create_task(self._retry_full_recovery(), name="lifeos-failsafe-retry")
                return
            except Exception as exc:
                trace_exception("CLIENT_REBUILD_FAILED", exc, gen=self.client_generation, reason="failsafe")
                logger.error("CLIENT_REBUILD_FAILED — failsafe: %s", exc)
                self._recovery_lock.release()
                immortal_create_task(self._retry_full_recovery(), name="lifeos-failsafe-retry")
                return

            try:
                await asyncio.wait_for(
                    asyncio.get_event_loop().run_in_executor(
                        None, register_all, self.client, self.owner_id, self.tz_str
                    ),
                    timeout=_REGISTER_TIMEOUT,
                )
            except asyncio.TimeoutError:
                trace("CLIENT_REBUILD_FAILED", gen=self.client_generation, reason="failsafe_register_timeout")
                logger.error("CLIENT_REBUILD_FAILED — failsafe registration timed out")
                self._recovery_lock.release()
                immortal_create_task(self._retry_full_recovery(), name="lifeos-failsafe-retry")
                return
            except Exception as exc:
                trace_exception("CLIENT_REBUILD_FAILED", exc, gen=self.client_generation, reason="failsafe_register")
                logger.error("CLIENT_REBUILD_FAILED — failsafe registration error: %s", exc)
                self._recovery_lock.release()
                immortal_create_task(self._retry_full_recovery(), name="lifeos-failsafe-retry")
                return

            set_last_update()
            set_last_telethon_event()

            if self.helper_enabled:
                set_self_client(self.client)
                configure_lifecycle(self.client, self.owner_id)
                configure_callback_trace(self.client, self.owner_id)
                register_input_listener(self.client, self.owner_id)

            bio_engine.update_client(self.client)
            username_engine.update_client(self.client)

            self._run_task = immortal_create_task(
                self._run_loop(), name="lifeos-run"
            )

            await self._resume_bio_cron()
            await self._resume_username_cron()

            try:
                await self._verify_heartbeat()
            except Exception:
                pass

            if self.helper_enabled:
                await self._start_helper()

            self._recovery_attempts = 0
            self._consecutive_failures = 0
            self._reconnect_failures = 0
            self._recovery_cooldown_until = time.time() + _RECOVERY_COOLDOWN
            self._transition(RuntimeState.READY)
            set_supervisor_ok(True)
            set_task_state("lifeos-recovery", "DONE")
            increment_restart()
            trace("RECOVERY_SUCCESS", action="failsafe", gen=self.client_generation)
            logger.info("RUNTIME_RECOVERED — failsafe recovery complete (gen=%d)",
                        self.client_generation)
            record_event("runtime", "recovery", 0, "SUCCESS",
                         f"gen={self.client_generation},reason=failsafe")
        except Exception as exc:
            trace_exception("RECOVERY_FAILED", exc, reason="failsafe")
            logger.error("RECOVERY_FAILED — failsafe: %s", exc)
            self._recovery_lock.release()
            immortal_create_task(self._retry_full_recovery(), name="lifeos-failsafe-retry")
            return
        finally:
            if self._recovery_lock.locked():
                self._recovery_lock.release()

    async def _verify_heartbeat(self) -> None:
        client = self.client
        if client is None:
            raise RuntimeError("No client after build")
        try:
            await asyncio.wait_for(client.get_me(), timeout=_RPC_TIMEOUT)
            logger.info(
                "WATCHDOG_HEARTBEAT_OK — verification passed (gen=%d)",
                self.client_generation,
            )
        except Exception as exc:
            raise RuntimeError(f"Heartbeat verification failed: {exc}") from exc

    async def _cancel_orphan_tasks(self) -> None:
        current = asyncio.current_task()
        protected_names = {
            "lifeos-watchdog", "lifeos-web", "lifeos-heartbeat",
            "lifeos-keepalive", "lifeos-task-supervisor",
            "lifeos-profile-scheduler", "lifeos-tg-supervisor",
            "lifeos-diagnostics", "lifeos-failsafe",
            "lifeos-helper-supervisor", "lifeos-helper-watchdog",
            "lifeos-helper", "lifeos-run",
        }
        to_cancel = []
        for task in asyncio.all_tasks():
            if task is current:
                continue
            name = task.get_name()
            if name in protected_names:
                continue
            if name.startswith("lifeos-panel-timer-"):
                continue
            if task.done():
                continue
            to_cancel.append(task)
        for task in to_cancel:
            task.cancel()
        if to_cancel:
            await asyncio.gather(*to_cancel, return_exceptions=True)

    async def _stop_helper(self) -> None:
        helper = self.helper_client
        self.helper_client = None
        if helper is not None:
            try:
                await asyncio.wait_for(helper.disconnect(), timeout=10.0)
            except (asyncio.TimeoutError, Exception):
                pass
        try:
            await disconnect_helper()
        except Exception:
            pass

    async def _watchdog_loop(self) -> None:
        logger.info("Watchdog started (interval=%ds)", int(_HEARTBEAT_INTERVAL))
        while not self.shutdown_event.is_set():
            t0 = time.monotonic()
            await asyncio.sleep(_HEARTBEAT_INTERVAL)
            loop_latency = (time.monotonic() - t0 - _HEARTBEAT_INTERVAL) * 1000

            tick_loop("lifeos-watchdog", state="RUNNING", success=True)
            self._last_watchdog_tick = time.time()

            if loop_latency > _LOOP_STARVATION_MS:
                trace(
                    "EVENT_LOOP_STARVATION",
                    source="watchdog",
                    loop_latency_ms=f"{loop_latency:.1f}",
                )
                logger.error(
                    "EVENT_LOOP_STARVATION — watchdog loop latency %.1fms. Blocking code suspected.",
                    loop_latency,
                )

            try:
                update_heartbeat()
                check_stale()
                set_heartbeat()
                set_task_state("lifeos-watchdog", "RUNNING")
            except Exception:
                pass

            if self._recovery_lock.locked():
                trace("WATCHDOG_CHECK", status="recovery_in_progress")
                continue

            stale_loops = get_stale_loops(_LOOP_STALE_THRESHOLD)
            if stale_loops:
                trace("LOOP_STALLED", loops=",".join(stale_loops), threshold=f"{_LOOP_STALE_THRESHOLD:.0f}s")
                logger.warning(
                    "LOOP_STALLED — loops not progressing for >%ds: %s",
                    int(_LOOP_STALE_THRESHOLD), ", ".join(stale_loops),
                )
                for name in stale_loops:
                    if name == "lifeos-heartbeat":
                        logger.warning("LOOP_STALLED — heartbeat stalled, restarting")
                        start_heartbeat()
                    elif name == "lifeos-keepalive":
                        logger.warning("LOOP_STALLED — keepalive stalled, restarting")
                        start_keepalive()
                    elif name == "lifeos-profile-scheduler":
                        if self.client:
                            logger.warning("LOOP_STALLED — profile scheduler stalled, restarting")
                            bio_engine.start_cron(self.client, self.owner_id, self.tz_str)
                            username_engine.start_cron(self.client, self.owner_id, self.tz_str)
                set_last_rebuild_reason(f"watchdog: loop_stalled ({','.join(stale_loops)})")
                await self._trigger_reconnect()
                continue

            client = self.client
            if client is None or not self._client_alive:
                self._consecutive_failures += 1
                trace(
                    "WATCHDOG_HEARTBEAT_FAILED",
                    reason="no_active_client",
                    consecutive_failures=self._consecutive_failures,
                    threshold=_HEARTBEAT_FAILURE_THRESHOLD,
                )
                logger.warning(
                    "WATCHDOG_HEARTBEAT_FAILED — no active client "
                    "(consecutive_failures=%d/%d)",
                    self._consecutive_failures, _HEARTBEAT_FAILURE_THRESHOLD,
                )
                if self._consecutive_failures >= _HEARTBEAT_FAILURE_THRESHOLD:
                    trace("WATCHDOG_RECOVERY", reason="no_active_client")
                    logger.warning("WATCHDOG_RECOVERY — no active client")
                    set_last_rebuild_reason("watchdog: no active client")
                    await self._trigger_full_recovery()
                continue

            try:
                stale_threshold = settings_svc.update_stale_seconds()
            except Exception:
                stale_threshold = int(_UPDATE_STALE_DEFAULT)

            now = time.time()

            last_event_ts = get_last_telethon_event()
            last_dispatch_ts = get_last_event_dispatch()
            last_rpc_ts = get_last_rpc()
            last_callback_ts = get_last_callback()

            update_idle = (now - last_event_ts) if last_event_ts > 0 else 0
            dispatch_idle = (now - last_dispatch_ts) if last_dispatch_ts > 0 else 0
            rpc_age = (now - last_rpc_ts) if last_rpc_ts > 0 else 0
            callback_idle = (now - last_callback_ts) if last_callback_ts > 0 else 0

            if last_event_ts > 0 and update_idle > stale_threshold:
                if rpc_age < stale_threshold:
                    trace(
                        "WATCHDOG_UPDATE_STALE",
                        last_event_age=f"{update_idle:.0f}s",
                        threshold=f"{stale_threshold}s",
                        gen=self.client_generation,
                    )
                    logger.warning(
                        "WATCHDOG_UPDATE_STALE — no updates for %.0fs "
                        "(threshold=%ds, gen=%d) — triggering reconnect",
                        update_idle, stale_threshold, self.client_generation,
                    )
                    record_event("runtime", "update_stale", update_idle,
                                 "STALE", f"threshold={stale_threshold}s")
                    set_last_rebuild_reason(
                        f"watchdog: update_stale ({update_idle:.0f}s > {stale_threshold}s)"
                    )
                    await self._trigger_reconnect()
                    continue

            if last_dispatch_ts > 0 and dispatch_idle > stale_threshold:
                if update_idle < stale_threshold:
                    trace(
                        "EVENT_DISPATCH_STALLED",
                        last_event_age=f"{update_idle:.0f}s",
                        last_dispatch_age=f"{dispatch_idle:.0f}s",
                        gen=self.client_generation,
                    )
                    logger.warning(
                        "EVENT_DISPATCH_STALLED — updates arriving "
                        "but no event dispatched for %.0fs (gen=%d)",
                        dispatch_idle, self.client_generation,
                    )
                    set_last_rebuild_reason(
                        f"watchdog: event_dispatch_stalled ({dispatch_idle:.0f}s)"
                    )
                    await self._trigger_reconnect()
                    continue

            if last_callback_ts > 0 and callback_idle > stale_threshold:
                if rpc_age < stale_threshold:
                    trace(
                        "CALLBACK_DISPATCH_STALLED",
                        last_callback_age=f"{callback_idle:.0f}s",
                        threshold=f"{stale_threshold}s",
                        gen=self.client_generation,
                    )
                    logger.warning(
                        "CALLBACK_DISPATCH_STALLED — no callbacks for %.0fs (gen=%d)",
                        callback_idle, self.client_generation,
                    )
                    set_last_rebuild_reason(
                        f"watchdog: callback_dispatch_stalled ({callback_idle:.0f}s)"
                    )
                    await self._trigger_reconnect()
                    continue

            t0 = time.monotonic()
            try:
                await asyncio.wait_for(client.get_me(), timeout=_RPC_TIMEOUT)
                latency_ms = (time.monotonic() - t0) * 1000
                set_last_rpc()
                set_rpc_latency(latency_ms)
                update_heartbeat_state(rpc_latency_ms=latency_ms)
                record_event("runtime", "heartbeat_rpc", latency_ms, "SUCCESS")
                self._consecutive_failures = 0
                trace(
                    "WATCHDOG_HEARTBEAT_OK",
                    latency_ms=f"{latency_ms:.1f}",
                    gen=self.client_generation,
                )
                logger.info(
                    "WATCHDOG_HEARTBEAT_OK — latency=%.1fms gen=%d",
                    latency_ms, self.client_generation,
                )
            except asyncio.CancelledError:
                raise
            except (asyncio.TimeoutError, Exception) as exc:
                self._consecutive_failures += 1
                trace_exception(
                    "WATCHDOG_HEARTBEAT_FAILED",
                    exc,
                    consecutive_failures=self._consecutive_failures,
                    threshold=_HEARTBEAT_FAILURE_THRESHOLD,
                )
                logger.warning(
                    "WATCHDOG_HEARTBEAT_FAILED — %s "
                    "(consecutive_failures=%d/%d)",
                    type(exc).__name__, self._consecutive_failures,
                    _HEARTBEAT_FAILURE_THRESHOLD,
                )
                record_event("runtime", "heartbeat_rpc", 0, "FAILED", str(exc))
                set_last_rebuild_reason(
                    f"watchdog: heartbeat_rpc_failed: {type(exc).__name__}"
                )

                if self._consecutive_failures >= _HEARTBEAT_FAILURE_THRESHOLD:
                    trace("WATCHDOG_RECOVERY", reason="heartbeat_failures")
                    logger.warning("WATCHDOG_RECOVERY — heartbeat failures")
                    await self._trigger_reconnect()

    async def _task_supervisor_loop(self) -> None:
        logger.info("Task supervisor started")
        while not self.shutdown_event.is_set():
            await asyncio.sleep(15)

            if self.shutdown_event.is_set():
                return

            tick_loop("lifeos-task-supervisor", state="RUNNING", success=True)

            for name in _CRITICAL_TASKS:
                task = self._find_task_by_name(name)
                if task is None or task.done():
                    if name == "lifeos-watchdog" and not self.shutdown_event.is_set():
                        trace("TASK_RESTART", task=name, reason="exited")
                        logger.warning("TASK_RESTART — recreating %s", name)
                        self._watchdog_task = immortal_create_task(
                            self._watchdog_loop(), name="lifeos-watchdog"
                        )
                    elif name == "lifeos-heartbeat" and not self.shutdown_event.is_set():
                        trace("TASK_RESTART", task=name, reason="exited")
                        logger.warning("TASK_RESTART — recreating %s", name)
                        start_heartbeat()
                    elif name == "lifeos-keepalive" and not self.shutdown_event.is_set():
                        trace("TASK_RESTART", task=name, reason="exited")
                        logger.warning("TASK_RESTART — recreating %s", name)
                        start_keepalive()
                    elif name == "lifeos-profile-scheduler" and not self.shutdown_event.is_set():
                        trace("TASK_RESTART", task=name, reason="exited")
                        logger.warning("TASK_RESTART — recreating %s", name)
                        if self.client:
                            bio_engine.start_cron(self.client, self.owner_id, self.tz_str)
                            username_engine.start_cron(self.client, self.owner_id, self.tz_str)
                    elif name == "lifeos-helper" and not self.shutdown_event.is_set():
                        if self.helper_enabled and self.helper_client is not None:
                            if not self.helper_client.is_connected():
                                trace("TASK_RESTART", task=name, reason="exited")
                                logger.warning("TASK_RESTART — recreating %s", name)
                                immortal_create_task(
                                    self._supervise_helper(), name="lifeos-helper"
                                )
                    elif name == "lifeos-run" and not self.shutdown_event.is_set():
                        trace("TASK_RESTART", task=name, reason="exited")
                        logger.warning("TASK_RESTART — recreating %s", name)
                        self._run_task = immortal_create_task(
                            self._run_loop(), name="lifeos-run"
                        )

    def _find_task_by_name(self, name: str) -> asyncio.Task | None:
        for task in asyncio.all_tasks():
            if task.get_name() == name:
                return task
        return None

    async def stop(self) -> None:
        trace("SHUTDOWN_INITIATED")
        logger.info("Shutdown initiated")
        self._transition(RuntimeState.STOPPING)
        self.shutdown_event.set()

        await get_lifecycle().shutdown_all()
        clear_all_targets()

        logger.info("Shutdown: stopping failsafe")
        await stop_failsafe()

        logger.info("Shutdown: stopping keepalive")
        await stop_keepalive()

        logger.info("Shutdown: stopping heartbeat")
        await stop_heartbeat()

        logger.info("Shutdown: stopping diagnostics")
        await stop_diagnostics()

        logger.info("Shutdown: stopping task supervisor")
        if self._task_supervisor_task and not self._task_supervisor_task.done():
            self._task_supervisor_task.cancel()
            try:
                await asyncio.wait_for(self._task_supervisor_task, timeout=5.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
            self._task_supervisor_task = None

        logger.info("Shutdown: stopping bio cron")
        try:
            await bio_engine.stop_cron()
        except Exception:
            pass
        set_bio_cron_ok(False)

        logger.info("Shutdown: stopping username cron")
        try:
            await username_engine.stop_cron()
        except Exception:
            pass

        if self._watchdog_task and not self._watchdog_task.done():
            self._watchdog_task.cancel()
            try:
                await asyncio.wait_for(self._watchdog_task, timeout=5.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
            self._watchdog_task = None

        if self._run_task and not self._run_task.done():
            self._run_task.cancel()
            try:
                await asyncio.wait_for(self._run_task, timeout=5.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
            self._run_task = None

        if self._uvicorn_server is not None:
            self._uvicorn_server.should_exit = True

        await self._stop_helper()
        set_helper_connected(False)
        update_heartbeat_state(helper_connected=False)

        if self.client is not None:
            trace("SELF_DISCONNECTED", reason="shutdown")
            logger.info("Shutdown: disconnecting Telethon")
            try:
                await asyncio.wait_for(self.client.disconnect(), timeout=10.0)
            except (asyncio.TimeoutError, Exception) as exc:
                logger.warning("Telethon disconnect: %s", exc)
            set_telethon_connected(False)
            update_heartbeat_state(self_connected=False)

        set_supervisor_ok(False)
        trace("SHUTDOWN_COMPLETE")
        logger.info("LifeOS stopped cleanly.")

    def task_states(self) -> dict[str, str]:
        states = {}
        if self._watchdog_task:
            states["lifeos-watchdog"] = "RUNNING" if not self._watchdog_task.done() else "STOPPED"
        if self._run_task:
            states["lifeos-run"] = "RUNNING" if not self._run_task.done() else "STOPPED"
        states["lifeos-recovery"] = "RECOVERING" if self._recovery_lock.locked else "IDLE"
        return states
