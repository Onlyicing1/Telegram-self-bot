"""
RuntimeSupervisor — self-healing watchdog with atomic recovery.

Architecture:
  - ONE watchdog task runs forever (30s interval)
  - Each tick performs a REAL RPC (get_me) as the heartbeat
  - 3 consecutive heartbeat failures → client declared DEAD
  - Recovery is atomic: lock-protected, single execution
  - Recovery sequence: stop → cancel tasks → dispose → rebuild → re-register → re-wire → resume bio → verify
  - Limited retries with exponential backoff
  - All retries exhausted → sys.exit(1) so Render restarts the process
  - Exactly ONE active self client at all times

Mandatory log tags:
  WATCHDOG_HEARTBEAT_OK
  WATCHDOG_HEARTBEAT_FAILED
  WATCHDOG_RECOVERY_STARTED
  WATCHDOG_RECOVERY_SUCCESS
  WATCHDOG_RECOVERY_FAILED
  WATCHDOG_PROCESS_EXIT
"""
import asyncio
import logging
import random
import sys
import time
from typing import Any

from telethon import TelegramClient

from backend.runtime.states import RuntimeState
from backend.runtime.tracer import trace, trace_exception
from backend.runtime.task_guard import guarded_create_task, set_runtime_state_ref
from backend.runtime.heartbeat import start_heartbeat, stop_heartbeat, update_state as update_heartbeat_state
from backend.bio import engine as bio_engine
from backend.username import engine as username_engine
from backend.profile import scheduler as profile_scheduler
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
    set_heartbeat,
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
_UPDATE_STALE_DEFAULT = 300.0


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
        "_watchdog_task", "_run_task",
        "_recovery_lock", "_recovery_attempts",
        "_client_alive", "_consecutive_failures",
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
        self._recovery_lock = asyncio.Lock()
        self._recovery_attempts: int = 0
        self._client_alive: bool = False
        self._consecutive_failures: int = 0

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

    # ── Startup ──

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

        logger.info("[2/5] Building self-client")
        await self._build_and_register()

        if self.helper_enabled:
            logger.info("[3/5] Starting helper bot")
            await self._start_helper()
        else:
            logger.info("[3/5] Helper bot: no BOT_TOKEN — inline UI disabled")

        logger.info("[4/5] Bio + Username cron resume check")
        await self._resume_engines()

        logger.info("[5/5] Starting web server on port %s", self.port)
        self._start_web_server()

        self._transition(RuntimeState.READY)
        set_supervisor_ok(True)
        logger.info("LifeOS online.")

        self._watchdog_task = guarded_create_task(
            self._watchdog_loop(), name="lifeos-watchdog"
        )
        self._run_task = guarded_create_task(
            self._run_loop(), name="lifeos-run"
        )
        start_heartbeat()
        start_diagnostics()

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

    async def _resume_engines(self) -> None:
        try:
            bio_state = await db_client.get_bio_state(self.owner_id)
            if bio_state and bio_state.get("is_active"):
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

        try:
            uname_state = await db_client.get_username_state(self.owner_id)
            if uname_state and uname_state.get("is_active"):
                self._start_username_cron()
                logger.info("[4/5] Username cron resumed")
            else:
                logger.info("[4/5] Username cron not active — skipping")
        except Exception as exc:
            logger.warning("[4/5] Username cron resume check failed: %s", exc)

    def _start_bio_cron(self) -> None:
        if self.client is None:
            logger.warning("Cannot start bio cron — no client")
            return
        bio_engine.start_cron(self.client, self.owner_id, self.tz_str)
        set_bio_cron_ok(True)

    def _start_username_cron(self) -> None:
        if self.client is None:
            logger.warning("Cannot start username cron — no client")
            return
        username_engine.start_cron(self.client, self.owner_id, self.tz_str)

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
                guarded_create_task(
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

    async def _watchdog_loop(self) -> None:
        while not self.shutdown_event.is_set():
            await asyncio.sleep(_HEARTBEAT_INTERVAL)
            if self.shutdown_event.is_set():
                return

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
                    trace(
                        "WATCHDOG_RECOVERY_STARTED",
                        reason="no_active_client",
                        consecutive_failures=self._consecutive_failures,
                    )
                    logger.warning(
                        "WATCHDOG_RECOVERY_STARTED — client declared DEAD "
                        "(no active client, %d consecutive failures)",
                        self._consecutive_failures,
                    )
                    set_last_rebuild_reason("watchdog: no active client")
                    await self._trigger_recovery()
                continue

            try:
                stale_threshold = settings_svc.update_stale_seconds()
            except Exception:
                stale_threshold = int(_UPDATE_STALE_DEFAULT)

            last_event = get_last_telethon_event()
            now = time.time()
            if last_event > 0:
                event_age = now - last_event
                if event_age > stale_threshold:
                    trace(
                        "WATCHDOG_UPDATE_STALE",
                        last_event_age=f"{event_age:.0f}s",
                        threshold=f"{stale_threshold}s",
                        gen=self.client_generation,
                    )
                    logger.warning(
                        "WATCHDOG_UPDATE_STALE — no updates for %.0fs "
                        "(threshold=%ds, gen=%d) — update loop stalled",
                        event_age, stale_threshold, self.client_generation,
                    )
                    record_event(
                        "runtime", "update_stale", event_age,
                        "STALE", f"threshold={stale_threshold}s",
                    )
                    set_last_rebuild_reason(
                        f"watchdog: update_stale ({event_age:.0f}s > {stale_threshold}s)"
                    )
                    self._consecutive_failures = _HEARTBEAT_FAILURE_THRESHOLD
                    await self._trigger_recovery()
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
                    trace(
                        "WATCHDOG_RECOVERY_STARTED",
                        reason="heartbeat_failures",
                        consecutive_failures=self._consecutive_failures,
                    )
                    logger.warning(
                        "WATCHDOG_RECOVERY_STARTED — client declared DEAD "
                        "(%d consecutive heartbeat failures)",
                        self._consecutive_failures,
                    )
                    await self._trigger_recovery()

    async def _trigger_recovery(self) -> None:
        if self.shutdown_event.is_set():
            return

        try:
            acquired = await asyncio.wait_for(
                self._recovery_lock.acquire(), timeout=30.0
            )
            if not acquired:
                logger.warning("Recovery lock acquisition timed out (30s) — skipping")
                return
        except asyncio.TimeoutError:
            logger.warning("Recovery lock acquisition timed out (30s) — skipping")
            return

        try:
            await self._do_recovery()
        finally:
            self._recovery_lock.release()

    async def _do_recovery(self) -> None:
        self._transition(RuntimeState.RECOVERING)
        set_task_state("lifeos-recovery", "RUNNING")

        self._recovery_attempts += 1
        attempt = self._recovery_attempts

        if attempt > _MAX_RECOVERY_ATTEMPTS:
            trace("WATCHDOG_PROCESS_EXIT", reason="recovery_limit_exceeded", attempts=attempt)
            logger.error(
                "WATCHDOG_PROCESS_EXIT — recovery limit exceeded "
                "(%d/%d attempts). Terminating process (exit code 1).",
                attempt, _MAX_RECOVERY_ATTEMPTS,
            )
            self._transition(RuntimeState.FAILED)
            set_supervisor_ok(False)
            set_task_state("lifeos-recovery", "FAILED")
            self.shutdown_event.set()
            sys.exit(1)

        delay = _backoff(attempt)
        trace("WATCHDOG_RECOVERY_STARTED", attempt=attempt, backoff_delay=f"{delay:.1f}s")
        logger.warning(
            "WATCHDOG_RECOVERY_STARTED — attempt %d/%d, backoff %.1fs",
            attempt, _MAX_RECOVERY_ATTEMPTS, delay,
        )
        record_event("runtime", "recovery_start", 0, "ATTEMPT",
                     f"attempt={attempt}")

        logger.info("Recovery: stopping bio cron")
        try:
            await bio_engine.stop_cron()
        except Exception as exc:
            logger.warning("Recovery: bio stop error: %s", exc)
        set_bio_cron_ok(False)

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

        logger.info("Recovery: cancelling all orphan tasks")
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

            logger.info("Recovery: resuming bio + username engines")
            await self._resume_engines()

            logger.info("Recovery: verifying with fresh heartbeat")
            await self._verify_heartbeat()

            self._run_task = guarded_create_task(
                self._run_loop(), name="lifeos-run"
            )

            start_heartbeat()

            set_last_update()
            set_last_telethon_event()

            self._recovery_attempts = 0
            self._consecutive_failures = 0
            self._transition(RuntimeState.READY)
            set_supervisor_ok(True)
            set_task_state("lifeos-recovery", "DONE")
            increment_restart()
            trace("WATCHDOG_RECOVERY_SUCCESS", gen=self.client_generation)
            logger.info(
                "WATCHDOG_RECOVERY_SUCCESS — system operational (gen=%d)",
                self.client_generation,
            )
            record_event("runtime", "recovery", 0, "SUCCESS",
                         f"gen={self.client_generation}")

        except Exception as exc:
            trace_exception("WATCHDOG_RECOVERY_FAILED", exc, attempt=attempt)
            logger.error(
                "WATCHDOG_RECOVERY_FAILED — attempt %d/%d: %s",
                attempt, _MAX_RECOVERY_ATTEMPTS, exc,
            )
            record_event("runtime", "recovery", 0, "ERROR", str(exc))
            set_last_rebuild_reason(f"recovery_error: {exc}")
            set_task_state("lifeos-recovery", "FAILED")

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
        protected_names = {"lifeos-watchdog", "lifeos-web", "lifeos-heartbeat", "lifeos-profile-scheduler"}
        to_cancel = []
        for task in asyncio.all_tasks():
            if task is current:
                continue
            name = task.get_name()
            if name in protected_names:
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

    async def stop(self) -> None:
        trace("SHUTDOWN_INITIATED")
        logger.info("Shutdown initiated")
        self._transition(RuntimeState.STOPPING)
        self.shutdown_event.set()

        await get_lifecycle().shutdown_all()
        clear_all_targets()

        logger.info("Shutdown: stopping heartbeat")
        await stop_heartbeat()

        logger.info("Shutdown: stopping diagnostics")
        await stop_diagnostics()

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

        logger.info("Shutdown: stopping profile scheduler")
        try:
            await profile_scheduler.stop()
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
