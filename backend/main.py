"""
LifeOS — deterministic entry point.

Startup phases (strict sequential):
  1. Config validation (hard-exit on missing required vars only)
  2. Database warm-up (optional — continues on failure)
  3. Telethon client — connect + authorize
  4. Command handler registration (exactly once)
  5. Bio cron resume (if persisted active in DB)
  6. Uvicorn web server (background task)

Shutdown sequence on SIGTERM / SIGINT:
  A. Bio cron cancelled
  B. Uvicorn signalled to exit
  C. All remaining asyncio tasks cancelled + awaited (zero orphans)
  D. Telethon disconnected cleanly

Reliability:
  - Telethon is supervised: auto-reconnect on disconnect.
  - Watchdog pings Telegram every 60s; force-disconnect if stalled.
  - Helper bot watchdog checks every 30s; reconnects or rebuilds automatically.
  - Bio cron is supervised: restarts if loop exits unexpectedly.
  - No background coroutine may silently die.
"""
import asyncio
import logging
import signal
import sys

import uvicorn

import backend.config as cfg_module
from backend.bio import engine as bio_engine
from backend.bot.client import build_client
from backend.bot.router import register_all
from backend.db import client as db_client
from backend.helper.client import build_helper, disconnect_helper, get_bot_username
from backend.helper.panels import register_callback_handlers
from backend.helper.inline_engine import (
    register_inline_handler,
    set_self_client,
    set_helper_username,
    set_owner_id,
)
from backend.helper.inline_sender import register_input_listener
from backend.helper.panel_settings import load as load_panel_settings
from backend.helper.callback_trace import configure as configure_callback_trace
from backend.helper import watchdog as helper_watchdog
from backend.health import (
    check_stale,
    increment_restart,
    mark_started,
    set_bio_cron_ok,
    set_supervisor_ok,
    set_telethon_connected,
    set_watchdog_ok,
    update_heartbeat,
)
from backend.web.app import app as web_app
from backend.diagnostics import record_event

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
logging.getLogger("backend").setLevel(logging.INFO)
logging.getLogger("telethon").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

_uvicorn_server: uvicorn.Server | None = None

_WATCHDOG_INTERVAL = 60
_WATCHDOG_TIMEOUT = 15
_RECONNECT_DELAY = 10
_HEARTBEAT_INTERVAL = 5.0


async def _heartbeat(shutdown: asyncio.Event) -> None:
    while not shutdown.is_set():
        try:
            update_heartbeat()
            check_stale()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Heartbeat error: %s", exc)
        await asyncio.sleep(_HEARTBEAT_INTERVAL)


async def _run_web(port: int) -> None:
    global _uvicorn_server
    config = uvicorn.Config(
        web_app,
        host="0.0.0.0",
        port=port,
        log_level="warning",
        access_log=False,
    )
    _uvicorn_server = uvicorn.Server(config)
    await _uvicorn_server.serve()


async def _supervise_telethon(client, shutdown: asyncio.Event) -> None:
    set_supervisor_ok(True)
    while not shutdown.is_set():
        set_telethon_connected(client.is_connected())
        try:
            await client.run_until_disconnected()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Telethon run_until_disconnected error: %s", exc)

        set_telethon_connected(False)

        if shutdown.is_set():
            break

        increment_restart()
        logger.warning("Telethon disconnected — reconnecting in %ds...", _RECONNECT_DELAY)
        await asyncio.sleep(_RECONNECT_DELAY)
        try:
            await client.connect()
            if not await client.is_user_authorized():
                logger.error("Reconnect: session not authorized — will retry")
                continue
            set_telethon_connected(True)
            logger.info("Telethon reconnected successfully")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("Reconnect failed: %s — will retry in %ds", exc, _RECONNECT_DELAY)
    set_supervisor_ok(False)


async def _supervise_helper(helper_client, shutdown: asyncio.Event) -> None:
    while not shutdown.is_set():
        try:
            await helper_client.run_until_disconnected()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Helper bot run_until_disconnected error: %s", exc)

        if shutdown.is_set():
            break

        logger.warning("Helper bot disconnected — reconnecting in %ds", _RECONNECT_DELAY)
        await asyncio.sleep(_RECONNECT_DELAY)
        try:
            await helper_client.connect()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("Helper bot reconnect failed: %s — will retry", exc)


async def _watchdog(client, shutdown: asyncio.Event) -> None:
    while not shutdown.is_set():
        try:
            await asyncio.sleep(_WATCHDOG_INTERVAL)
            if shutdown.is_set():
                break
            connected = client.is_connected()
            set_telethon_connected(connected)
            if not connected:
                logger.warning("Watchdog: client not connected — skipping ping")
                continue
            try:
                await asyncio.wait_for(client.get_me(), timeout=_WATCHDOG_TIMEOUT)
                set_watchdog_ok(True)
            except asyncio.TimeoutError:
                logger.warning("Watchdog: health check timed out — forcing disconnect")
                set_watchdog_ok(False)
                try:
                    await client.disconnect()
                except Exception:
                    pass
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("Watchdog: health check failed (%s) — forcing disconnect", exc)
                set_watchdog_ok(False)
                try:
                    await client.disconnect()
                except Exception:
                    pass
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Watchdog error: %s", exc)


async def main() -> None:
    cfg = cfg_module.load()

    mark_started()

    shutdown: asyncio.Event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, shutdown.set)
        except NotImplementedError:
            pass

    logger.info("[1/5] Database warm-up")
    db = db_client.get_db()
    if db:
        try:
            db.table("bot_logs").select("id").limit(1).execute()
            logger.info("[1/5] Database OK")
        except Exception as exc:
            logger.warning("[1/5] Database warm-up failed (%s) — continuing", exc)
    else:
        logger.info("[1/5] Using in-memory fallback — no database required")

    load_panel_settings()

    logger.info("[2/5] Connecting Telethon")
    client = await build_client(cfg["API_ID"], cfg["API_HASH"], cfg["SESSION_STRING"])
    set_telethon_connected(True)

    logger.info("[3/5] Registering command handlers")
    register_all(client, cfg["OWNER_ID"], cfg["TZ"])

    helper_client = None
    if cfg.get("HELPER_BOT_ENABLED"):
        logger.info("[3.5/5] Starting helper bot")
        try:
            helper_client = await build_helper(cfg["BOT_TOKEN"])
            if helper_client is not None:
                register_callback_handlers(helper_client, cfg["OWNER_ID"])
                register_inline_handler(helper_client, cfg["OWNER_ID"])
                set_self_client(client)
                set_helper_username(get_bot_username())
                set_owner_id(cfg["OWNER_ID"])
                configure_callback_trace(client, cfg["OWNER_ID"])
                register_input_listener(client, cfg["OWNER_ID"])
                helper_watchdog.configure(cfg["BOT_TOKEN"], client, cfg["OWNER_ID"])
                helper_watchdog.start()
                logger.info("[3.5/5] Helper bot online — Inline Mode enabled")
        except Exception:
            logger.exception("[3.5/5] Helper bot failed — inline UI disabled")
            helper_client = None
    else:
        logger.info("[3.5/5] Helper bot: no BOT_TOKEN — inline UI disabled")

    logger.info("[4/5] Bio cron resume check")
    try:
        state = db_client.get_bio_state(cfg["OWNER_ID"])
        if state and state.get("is_active"):
            bio_engine.start_cron(client, cfg["OWNER_ID"], cfg["TZ"])
            logger.info("[4/5] Bio cron resumed")
        elif cfg.get("BIO_UPDATE_ENABLED"):
            bio_engine.start_cron(client, cfg["OWNER_ID"], cfg["TZ"])
            logger.info("[4/5] Bio cron started (BIO_UPDATE_ENABLED=true)")
        else:
            logger.info("[4/5] Bio cron not active — skipping")
        set_bio_cron_ok(bio_engine.is_running())
    except Exception as exc:
        logger.warning("[4/5] Bio cron resume check failed: %s", exc)
        set_bio_cron_ok(False)

    logger.info("[5/5] Starting web server on port %s", cfg["PORT"])
    web_task = asyncio.create_task(_run_web(cfg["PORT"]), name="lifeos-web")

    tg_supervisor = asyncio.create_task(
        _supervise_telethon(client, shutdown), name="lifeos-tg-supervisor"
    )
    watchdog_task = asyncio.create_task(
        _watchdog(client, shutdown), name="lifeos-watchdog"
    )
    heartbeat_task = asyncio.create_task(
        _heartbeat(shutdown), name="lifeos-heartbeat"
    )

    helper_supervisor = None
    if helper_client is not None:
        helper_supervisor = asyncio.create_task(
            _supervise_helper(helper_client, shutdown), name="lifeos-helper-supervisor"
        )
        logger.info("[3.5/5] Helper bot supervisor started")

    logger.info("LifeOS online.")

    await shutdown.wait()

    logger.info("Shutdown: stopping bio cron")
    bio_engine.stop_cron()
    set_bio_cron_ok(False)
    set_supervisor_ok(False)
    set_telethon_connected(False)

    logger.info("Shutdown: stopping helper watchdog")
    helper_watchdog.stop()

    logger.info("Shutdown: signalling web server")
    if _uvicorn_server is not None:
        _uvicorn_server.should_exit = True

    logger.info("Shutdown: cancelling all tasks")
    pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    for task in pending:
        task.cancel()
    await asyncio.gather(*pending, return_exceptions=True)

    if helper_client is not None:
        logger.info("Shutdown: disconnecting helper bot")
        await disconnect_helper()

    logger.info("Shutdown: disconnecting Telethon")
    try:
        await client.disconnect()
    except Exception as exc:
        logger.warning("Telethon disconnect: %s", exc)

    logger.info("LifeOS stopped cleanly.")


if __name__ == "__main__":
    asyncio.run(main())
