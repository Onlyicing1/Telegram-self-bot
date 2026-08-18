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
from backend.helper.client import build_helper, disconnect_helper, register_helper_hooks, get_client as get_helper_client
from backend.helper.inline_engine import set_self_client, set_helper_username, set_helper_id, set_owner_id
from backend.helper.lifecycle import get_lifecycle, configure_lifecycle
from backend.helper.target_context import clear_all as clear_all_targets
from backend.helper.callback_trace import configure as configure_callback_trace
from backend.helper import panels as panels_module
from backend.helper.inline_sender import register_input_listener
from backend.profile import scheduler as profile_scheduler
from backend.diagnostics import record_event
from backend.health import (
    set_runtime_state, set_telethon_connected, set_supervisor_ok,
    set_bio_cron_ok, set_username_cron_ok, set_helper_connected, set_last_update,
    set_last_telethon_event, set_last_event_dispatch,
    set_last_rpc, set_rpc_latency, set_restart_count, increment_restart,
    set_client_generation, set_last_rebuild_reason, set_task_state,
    tick_loop, get_stale_loops,
)
from backend.services import settings_service

logger = logging.getLogger(__name__)

_HEARTBEAT_INTERVAL = 30
_HEARTBEAT_FAILURE_THRESHOLD = 3
_RPC_TIMEOUT = 15
_REBUILD_TIMEOUT = 60
_REGISTER_TIMEOUT = 30
_LOOP_STALE_THRESHOLD = 90
_LOOP_STARVATION_MS = 5000
_RECOVERY_COOLDOWN = 180
_MAX_RECOVERY_ATTEMPTS = 5


def _backoff(attempt: int) -> float:
    base = min(60, 2 ** attempt)
    jitter = random.uniform(-0.3, 0.3) * base
    return max(1.0, base + jitter)


class RuntimeSupervisor:
    __slots__ = (
        "cfg", "api_id", "api_hash", "session_string", "owner_id", "tz_str",
        "bot_token", "helper_enabled", "state", "client", "helper_client",
        "client_generation", "shutdown_event", "_run_task", "_web_task",
        "_helper_task", "_consecutive_failures", "_reconnect_failures",
        "_recovery_attempts", "_recovery_lock", "_recovery_cooldown_until",
        "_last_watchdog_tick", "_client_alive",
    )

    def __init__(self, cfg: dict) -> None:
        self.cfg = cfg
        self.api_id = cfg["API_ID"]
        self.api_hash = cfg["API_HASH"]
        self.session_string = cfg["SESSION_STRING"]
        self.owner_id = cfg["OWNER_ID"]
        self.tz_str = cfg["TZ"]
        self.bot_token = cfg.get("BOT_TOKEN", "")
        self.helper_enabled = cfg.get("HELPER_BOT_ENABLED", False)
        self.state = RuntimeState.STARTING
        self.client: TelegramClient | None = None
        self.helper_client: TelegramClient | None = None
        self.client_generation = 0
        self.shutdown_event = asyncio.Event()
        self._run_task: asyncio.Task | None = None
        self._web_task: asyncio.Task | None = None
        self._helper_task: asyncio.Task | None = None
        self._consecutive_failures = 0
        self._reconnect_failures = 0
        self._recovery_attempts = 0
        self._recovery_lock = asyncio.Lock()
        self._recovery_cooldown_until = 0.0
        self._last_watchdog_tick = 0.0
        self._client_alive = False

    @property
    def client_alive(self) -> bool:
        return self._client_alive

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
        try:
            from backend.runtime.crash_diagnostics import record_runtime_event
            record_runtime_event("STATE_TRANSITION", f"{old} -> {new_state}")
        except Exception:
            pass

    def _trace_telethon_disconnect(self, reason: str) -> None:
        trace("SELF_DISCONNECTED", gen=self.client_generation, reason=reason)
        logger.warning("Self-client disconnected — watchdog will detect and recover")

        try:
            from backend.runtime.crash_diagnostics import record_runtime_event, record_exception
            from backend.health import (
                get_last_telethon_event,
                get_last_rpc,
                get_last_event_dispatch,
            )

            details = []
            client = self.client
            if client is not None:
                try:
                    details.append(f"connected={client.is_connected()}")
                    details.append(f"authorized={client.is_user_authorized() if client.is_connected() else False}")
                    details.append(f"dc_id={getattr(client.session, 'dc_id', 'unknown')}")
                    details.append(f"server={getattr(client.session, 'server_address', 'unknown')}")
                    details.append(f"reconnect_attempts={getattr(client, '_reconnect_attempts', 0)}")
                except Exception:
                    details.append("client_state_unavailable")
            else:
                details.append("client=None")

            details.append(f"gen={self.client_generation}")
            details.append(f"last_rpc={get_last_rpc()}")
            details.append(f"last_telethon_event={get_last_telethon_event()}")
            details.append(f"last_dispatch={get_last_event_dispatch()}")
            details.append(f"reason={reason}")

            record_runtime_event("TELETHON_DISCONNECT", ", ".join(details))
        except Exception:
            pass

    async def start(self) -> None:
        trace("SUPERVISOR_START")
        logger.info("RuntimeSupervisor starting")

        settings_service.load_all()

        self._transition(RuntimeState.CONNECTING)
        await self._build_and_register()

        if self.helper_enabled:
            self._transition(RuntimeState.REGISTERING)
            await self._start_helper()

        await self._resume_bio_cron()
        await self._resume_username_cron()

        self._transition(RuntimeState.READY)
        set_supervisor_ok(True)
        set_restart_count(0)

        self._start_web_server()

        start_heartbeat()
        start_keepalive()
        start_failsafe()
        from backend.runtime.diagnostics import start_diagnostics
        start_diagnostics()

        configure_heartbeat(self)
        configure_keepalive(self)
        configure_failsafe(self)

        self._run_task = immortal_create_task(self._run_loop, name="lifeos-run")

        from backend.runtime.memory_cleanup import start_memory_cleanup
        start_memory_cleanup()

        trace("SUPERVISOR_READY", gen=self.client_generation)
        logger.info("RuntimeSupervisor READY (gen=%d)", self.client_generation)

    async def _build_and_register(self) -> None:
        trace("SELF_CONNECTING")
        self.client = await build_client(self.api_id, self.api_hash, self.session_string)
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

        trace("SELF_CONNECTED", gen=self.client_generation)
        set_last_update()
        set_last_telethon_event()
        set_last_event_dispatch()

        set_self_client(self.client)
        set_owner_id(self.owner_id)

        register_all(self.client, self.owner_id, self.tz_str)

        if self.helper_enabled:
            configure_lifecycle(self.client, self.owner_id)
            configure_callback_trace(self.client, self.owner_id)
            register_input_listener(self.client, self.owner_id)

        self._wire_ai_tools()

    def _wire_ai_tools(self) -> None:
        try:
            from backend.ai.engine.engine import get_engine
            from backend.ai.tools.registry import create_default_registry
            from backend.ai.tools.context import ToolContext
            from backend.telegram_api import TelegramAPI

            engine = get_engine()
            tool_ctx = ToolContext(
                telegram=TelegramAPI(self.client),
                owner_id=self.owner_id,
                tz_str=self.tz_str,
                client=self.client,
            )
            registry = create_default_registry(tool_ctx)
            engine.attach_tools(registry, tool_ctx, owner_id=self.owner_id, tz_str=self.tz_str)
            trace("AI_TOOLS_WIRED", gen=self.client_generation)
            logger.info("AI tool runtime wired (gen=%d)", self.client_generation)
        except Exception as exc:
            trace_exception("AI_TOOLS_WIRE_FAILED", exc)
            logger.warning("AI tool runtime wiring failed: %s", exc)

    async def _start_helper(self) -> None:
        if not self.helper_enabled:
            return
        try:
            helper = await build_helper(self.bot_token)
            if helper is not None:
                self.helper_client = helper
                set_helper_connected(True)
                update_heartbeat_state(helper_connected=True)
                register_helper_hooks(helper)

                from backend.helper.client import get_bot_username, get_bot_id
                bot_username = get_bot_username()
                bot_id = get_bot_id()
                set_helper_username(bot_username)
                set_helper_id(bot_id)
                if not bot_username:
                    logger.warning(
                        "Helper bot has no public @username (id=%s) — "
                        "inline panels will fail until a username is set "
                        "via BotFather or Telegram settings",
                        bot_id,
                    )

                from backend.helper.inline_engine import register_inline_handler
                register_inline_handler(helper, self.owner_id)
                from backend.helper.panels import register_callback_handlers
                register_callback_handlers(helper, self.owner_id)

                self._start_helper_loop(helper)

                trace("HELPER_STARTED", username=bot_username, bot_id=bot_id)
        except Exception as exc:
            trace_exception("HELPER_START_FAILED", exc)
            logger.error("Helper bot start failed: %s", exc)
            set_helper_connected(False)
            update_heartbeat_state(helper_connected=False)

    def _start_helper_loop(self, helper) -> None:
        if self._helper_task is not None and not self._helper_task.done():
            trace("HELPER_LOOP_ALREADY_RUNNING")
            return
        self._helper_task = immortal_create_task(
            helper.run_until_disconnected,
            name="lifeos-helper",
        )
        trace("HELPER_LOOP_STARTED", gen=self.client_generation)

    async def _stop_helper_loop(self) -> None:
        task = self._helper_task
        self._helper_task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await asyncio.wait_for(task, timeout=5.0)
            except (asyncio.CancelledError, asyncio.TimeoutError, Exception):
                pass

    async def _resume_bio_cron(self) -> None:
        from backend.db import client as db_client
        try:
            state = await db_client.get_bio_state(self.owner_id)
            if state and state.get("is_active"):
                bio_engine.start_cron(self.client, self.owner_id, self.tz_str)
                set_bio_cron_ok(True)
                trace("BIO_CRON_RESUMED")
            elif self.cfg.get("BIO_UPDATE_ENABLED"):
                bio_engine.start_cron(self.client, self.owner_id, self.tz_str)
                set_bio_cron_ok(True)
                trace("BIO_CRON_AUTO_STARTED")
        except Exception as exc:
            trace_exception("BIO_CRON_RESUME_FAILED", exc)

    async def _resume_username_cron(self) -> None:
        from backend.db import client as db_client
        try:
            state = await db_client.get_username_state(self.owner_id)
            if state and state.get("is_active"):
                username_engine.start_cron(self.client, self.owner_id, self.tz_str)
                set_username_cron_ok(True)
                trace("USERNAME_CRON_RESUMED")
            elif self.cfg.get("USERNAME_UPDATE_ENABLED"):
                username_engine.start_cron(self.client, self.owner_id, self.tz_str)
                set_username_cron_ok(True)
                trace("USERNAME_CRON_AUTO_STARTED")
        except Exception as exc:
            trace_exception("USERNAME_CRON_RESUME_FAILED", exc)

    async def _run_loop(self) -> None:
        while not self.shutdown_event.is_set():
            try:
                await self.client.run_until_disconnected()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                trace_exception("SELF_RUN_LOOP_ERROR", exc, gen=self.client_generation)
                logger.exception("Run loop error: %s", exc)

            if self.shutdown_event.is_set():
                break

            self._client_alive = False
            set_telethon_connected(False)
            update_heartbeat_state(self_connected=False)
            self._trace_telethon_disconnect("run_until_disconnected_returned")
            await asyncio.sleep(2)
            continue

        trace("SELF_RUN_LOOP_EXITED", gen=self.client_generation)

    async def _trigger_reconnect(self) -> None:
        if self._recovery_lock.locked():
            return
        if self._in_recovery_cooldown():
            trace("RECOVERY_COOLDOWN", remaining=f"{self._recovery_cooldown_until - time.time():.0f}s")
            return

        escalate = False
        async with self._recovery_lock:
            self._reconnect_failures += 1
            trace("WATCHDOG_RECOVERY", reason="reconnect", attempt=self._reconnect_failures)
            logger.warning("WATCHDOG_RECOVERY — reconnect attempt %d", self._reconnect_failures)

            try:
                if self.client:
                    await asyncio.wait_for(self.client.disconnect(), timeout=10.0)
                await asyncio.sleep(1)
                await asyncio.wait_for(self.client.connect(), timeout=30.0)

                authorized = await asyncio.wait_for(
                    self.client.is_user_authorized(), timeout=15.0
                )
                if not authorized:
                    raise RuntimeError("Session not authorized after reconnect")

                self._client_alive = True
                self._consecutive_failures = 0
                self._reconnect_failures = 0
                set_telethon_connected(True)
                update_heartbeat_state(self_connected=True)
                set_last_update()
                set_last_telethon_event()
                trace("SELF_RECONNECTED", gen=self.client_generation)
                logger.info("SELF_RECONNECTED (gen=%d)", self.client_generation)
                return

            except Exception as exc:
                trace_exception("RECONNECT_FAILED", exc, gen=self.client_generation)
                logger.warning("RECONNECT_FAILED: %s — escalating to rebuild", exc)
                set_last_rebuild_reason(f"reconnect_failed: {exc}")
                escalate = True

        if escalate:
            await self._trigger_full_recovery()

    async def _trigger_full_recovery(self) -> None:
        if self._in_recovery_cooldown():
            return

        async with self._recovery_lock:
            self._recovery_attempts += 1
            attempt = self._recovery_attempts
            trace("WATCHDOG_RECOVERY", reason="full", attempt=attempt)
            logger.warning("WATCHDOG_RECOVERY — full recovery attempt %d/%d",
                          attempt, _MAX_RECOVERY_ATTEMPTS)

            try:
                await self._do_recovery(attempt)
            except Exception as exc:
                trace_exception("RECOVERY_FAILED", exc, attempt=attempt)
                logger.error(
                    "RECOVERY_FAILED — attempt %d: %s",
                    attempt, exc,
                )
                record_event("runtime", "recovery", 0, "ERROR", str(exc))
                set_last_rebuild_reason(f"recovery_error: {exc}")
                set_task_state("lifeos-recovery", "FAILED")
                try:
                    from backend.runtime.crash_diagnostics import record_exception, record_runtime_event
                    record_exception(exc, source=f"recovery_attempt_{attempt}")
                    record_runtime_event("RECOVERY_FAILED", f"attempt={attempt}: {exc}")
                except Exception:
                    pass
                if attempt >= _MAX_RECOVERY_ATTEMPTS:
                    from backend.runtime.crash_diagnostics import record_exit_reason, dump_crash_snapshot
                    record_exit_reason("TELETHON_FATAL", f"recovery exhausted after {attempt} attempts: {exc}")
                    dump_crash_snapshot(reason=f"recovery_exhausted:{type(exc).__name__}")
                    logger.error("Recovery exhausted after %d attempts — exiting so Render restarts", attempt)
                    self._transition(RuntimeState.FAILED)
                    import sys as _sys
                    _sys.exit(1)
                immortal_create_task(self._retry_full_recovery, name="lifeos-recovery-retry")
                return

    async def _do_recovery(self, attempt: int) -> None:
        delay = _backoff(attempt)
        trace("RECOVERY_STARTED", attempt=attempt, backoff=f"{delay:.1f}s")
        logger.info("RECOVERY_STARTED — attempt %d, backoff %.1fs", attempt, delay)

        set_task_state("lifeos-recovery", "RUNNING")

        logger.info("Recovery: stopping helper bot")
        await self._stop_helper_loop()
        await self._stop_helper()
        set_helper_connected(False)
        update_heartbeat_state(helper_connected=False)

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

            logger.info("Recovery: resuming cron engines")
            await self._resume_bio_cron()
            await self._resume_username_cron()

            self._recovery_attempts = 0
            self._consecutive_failures = 0
            self._reconnect_failures = 0
            self._recovery_cooldown_until = time.time() + _RECOVERY_COOLDOWN
            self._transition(RuntimeState.READY)
            set_supervisor_ok(True)
            set_task_state("lifeos-recovery", "DONE")
            increment_restart()
            trace("RECOVERY_SUCCESS", gen=self.client_generation)
            logger.info("RUNTIME_RECOVERED (gen=%d)", self.client_generation)
            record_event("runtime", "recovery", 0, "SUCCESS",
                         f"gen={self.client_generation}")

        except Exception as exc:
            trace_exception("RECOVERY_BUILD_FAILED", exc, gen=self.client_generation)
            logger.error("RECOVERY_BUILD_FAILED: %s", exc)
            raise

    async def _retry_full_recovery(self) -> None:
        await asyncio.sleep(30.0)
        if self.shutdown_event.is_set():
            return
        await self._trigger_full_recovery()

    async def _hard_reset_runtime(self) -> None:
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

            await self._stop_helper_loop()
            await self._stop_helper()
            set_helper_connected(False)
            update_heartbeat_state(helper_connected=False)

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
                immortal_create_task(self._retry_full_recovery, name="lifeos-failsafe-retry")
                return
            except Exception as exc:
                trace_exception("CLIENT_REBUILD_FAILED", exc, gen=self.client_generation, reason="failsafe")
                logger.error("CLIENT_REBUILD_FAILED — failsafe: %s", exc)
                immortal_create_task(self._retry_full_recovery, name="lifeos-failsafe-retry")
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
                immortal_create_task(self._retry_full_recovery, name="lifeos-failsafe-retry")
                return
            except Exception as exc:
                trace_exception("CLIENT_REBUILD_FAILED", exc, gen=self.client_generation, reason="failsafe_register")
                logger.error("CLIENT_REBUILD_FAILED — failsafe registration error: %s", exc)
                immortal_create_task(self._retry_full_recovery, name="lifeos-failsafe-retry")
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
                self._run_loop, name="lifeos-run"
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
            immortal_create_task(self._retry_full_recovery, name="lifeos-failsafe-retry")
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
            "lifeos-watchdog", "lifeos-web", "lifeos-web-server", "lifeos-heartbeat",
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

    def _start_web_server(self) -> None:
        if self._web_task is not None and not self._web_task.done():
            return
        import os
        import uvicorn
        from backend.web.app import app, set_owner_id

        set_owner_id(self.owner_id)
        port = int(os.environ.get("PORT", self.cfg.get("PORT", 8000)))
        config = uvicorn.Config(
            app,
            host="0.0.0.0",
            port=port,
            log_level="warning",
            access_log=False,
        )
        server = uvicorn.Server(config)
        self._web_task = immortal_create_task(
            lambda: server.serve(),
            name="lifeos-web-server",
        )
        trace("WEB_SERVER_STARTING", host="0.0.0.0", port=port)
        logger.info("Web server starting on 0.0.0.0:%d", port)

    async def _stop_web_server(self) -> None:
        if self._web_task is None:
            return
        if not self._web_task.done():
            self._web_task.cancel()
            try:
                await asyncio.wait_for(self._web_task, timeout=5.0)
            except (asyncio.CancelledError, asyncio.TimeoutError, Exception):
                pass
        self._web_task = None

    async def _stop_helper(self) -> None:
        helper = self.helper_client
        self.helper_client = None
        set_helper_username("")
        set_helper_id(0)
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
                from backend.health import update_heartbeat, check_stale, set_heartbeat
                update_heartbeat()
                check_stale()
                set_heartbeat()
                set_task_state("lifeos-watchdog", "RUNNING")
            except Exception:
                pass

            try:
                import os
                mem_rss = int(open(f"/proc/{os.getpid()}/status").read().split("VmRSS:")[1].split()[0]) * 1024
                if mem_rss > 400 * 1024 * 1024:
                    trace("MEMORY_PRESSURE", rss_bytes=mem_rss, threshold="400MB")
                    logger.warning("MEMORY_PRESSURE — RSS=%dMB, triggering cleanup", mem_rss // (1024 * 1024))
                    import gc
                    collected = gc.collect()
                    trace("MEMORY_CLEANUP", collected=collected, rss_after="unknown")
                    logger.info("MEMORY_CLEANUP — gc.collect() freed %d objects", collected)
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

            # ── Helper health check ──
            if self.helper_enabled and not self._recovery_lock.locked():
                helper = self.helper_client
                helper_alive = (
                    helper is not None
                    and helper.is_connected()
                )
                if not helper_alive:
                    trace(
                        "HELPER_DISCONNECTED",
                        gen=self.client_generation,
                        helper_task_alive=(
                            self._helper_task is not None
                            and not self._helper_task.done()
                        ),
                    )
                    logger.warning(
                        "HELPER_DISCONNECTED — restarting helper bot"
                    )
                    set_last_rebuild_reason("watchdog: helper disconnected")
                    set_helper_connected(False)
                    update_heartbeat_state(helper_connected=False)
                    await self._stop_helper_loop()
                    await self._stop_helper()
                    try:
                        await self._start_helper()
                    except Exception as exc:
                        trace_exception("HELPER_RESTART_FAILED", exc)
                        logger.error("HELPER_RESTART_FAILED: %s", exc)

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
                await asyncio.wait_for(client.get_me(), timeout=_RPC_TIMEOUT)
                self._consecutive_failures = 0
                set_last_rpc()
                set_rpc_latency(0)
                set_task_state("lifeos-watchdog", "RUNNING")
            except asyncio.TimeoutError:
                self._consecutive_failures += 1
                trace(
                    "WATCHDOG_HEARTBEAT_FAILED",
                    reason="rpc_timeout",
                    consecutive_failures=self._consecutive_failures,
                    threshold=_HEARTBEAT_FAILURE_THRESHOLD,
                )
                logger.warning(
                    "WATCHDOG_HEARTBEAT_FAILED — RPC timeout "
                    "(consecutive_failures=%d/%d)",
                    self._consecutive_failures, _HEARTBEAT_FAILURE_THRESHOLD,
                )
                if self._consecutive_failures >= _HEARTBEAT_FAILURE_THRESHOLD:
                    trace("WATCHDOG_RECOVERY", reason="rpc_timeout")
                    logger.warning("WATCHDOG_RECOVERY — RPC timeout")
                    set_last_rebuild_reason("watchdog: rpc timeout")
                    await self._trigger_full_recovery()
            except Exception as exc:
                self._consecutive_failures += 1
                trace_exception("WATCHDOG_HEARTBEAT_ERROR", exc)
                logger.warning(
                    "WATCHDOG_HEARTBEAT_ERROR — %s (consecutive_failures=%d/%d)",
                    exc, self._consecutive_failures, _HEARTBEAT_FAILURE_THRESHOLD,
                )
                if self._consecutive_failures >= _HEARTBEAT_FAILURE_THRESHOLD:
                    trace("WATCHDOG_RECOVERY", reason="rpc_error")
                    set_last_rebuild_reason(f"watchdog: rpc error: {exc}")
                    await self._trigger_full_recovery()

    def _in_recovery_cooldown(self) -> bool:
        return time.time() < self._recovery_cooldown_until

    async def stop(self) -> None:
        trace("SHUTDOWN_INITIATED")
        logger.info("Shutdown initiated")
        self._transition(RuntimeState.STOPPING)
        self.shutdown_event.set()
        try:
            from backend.runtime.crash_diagnostics import record_runtime_event
            record_runtime_event("SHUTDOWN_INITIATED", f"gen={self.client_generation}")
        except Exception:
            pass

        from backend.bio import engine as bio_engine
        from backend.username import engine as username_engine
        await bio_engine.stop_cron()
        await username_engine.stop_cron()

        from backend.runtime.heartbeat import stop_heartbeat
        from backend.runtime.keepalive import stop_keepalive
        from backend.runtime.failsafe import stop_failsafe
        from backend.runtime.diagnostics import stop_diagnostics
        from backend.runtime.memory_cleanup import stop_memory_cleanup
        await stop_heartbeat()
        await stop_keepalive()
        await stop_failsafe()
        await stop_diagnostics()
        await stop_memory_cleanup()

        if self._run_task and not self._run_task.done():
            self._run_task.cancel()
            try:
                await asyncio.wait_for(self._run_task, timeout=10.0)
            except (asyncio.CancelledError, asyncio.TimeoutError, Exception):
                pass

        await self._stop_web_server()

        await self._stop_helper_loop()
        await self._stop_helper()

        if self.client is not None:
            try:
                trace("SELF_DISCONNECTED", reason="shutdown")
                await asyncio.wait_for(self.client.disconnect(), timeout=10.0)
            except (asyncio.TimeoutError, Exception):
                logger.warning("Self-client disconnect timed out during shutdown")

        from backend.helper.lifecycle import get_lifecycle
        await get_lifecycle().shutdown_all()

        await self._cancel_orphan_tasks()

        set_supervisor_ok(False)
        set_telethon_connected(False)
        set_helper_connected(False)
        update_heartbeat_state(helper_connected=False)
        trace("SHUTDOWN_COMPLETE")
        logger.info("LifeOS stopped cleanly.")
        try:
            from backend.runtime.crash_diagnostics import record_runtime_event
            record_runtime_event("SHUTDOWN_COMPLETE", "stop() finished")
        except Exception:
            pass
