successfully downloaded text file (SHA: a3fb714c667249f2932c5e289bb03ed47d9f0896)
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
  - Telethon is supervised: if run_until_disconnected() returns (connection
    lost), the supervisor reconnects automatically.
  - A watchdog pings Telegram every 60s; if the ping times out, the client
    is force-disconnected so the supervisor can reconnect.
  - Bio cron is supervised: if the cron loop exits unexpectedly, it restarts.
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
    """Update the health heartbeat every few seconds while running."""
    while not shutdown.is_set():
        try:
            update_heartbeat()
            check_stale()
            record_event("watchdog", "heartbeat", 0, "SUCCESS")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Heartbeat error: %s", exc)
            record_event("watchdog", "heartbeat", 0, "ERROR", str(exc))
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
    """Run run_until_disconnected() in a loop, reconnecting on exit."""
    set_supervisor_ok(True)
    while not shutdown.is_set():
        set_telethon_connected(client.is_connected())
        try:
            await client.run_until_disconnected()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Telethon run_until_disconnected error: %s", exc)
            record_event("telethon", "run_until_disconnected", 0, "ERROR", str(exc))

        set_telethon_connected(False)
        record_event("telethon", "disconnected", 0, "WARNING")

        if shutdown.is_set():
            break

        increment_restart()
        logger.warning("Telethon disconnected — reconnecting in %ds...", _RECONNECT_DELAY)
        await asyncio.sleep(_RECONNECT_DELAY)
        try:
            await client.connect()
            if not await client.is_user_authorized():
                logger.error("Reconnect: session not authorized — will retry")
                record_event("telethon", "reconnect", 0, "ERROR", "not authorized")
                continue
            set_telethon_connected(True)
            record_event("telethon", "reconnect", 0, "SUCCESS")
            logger.info("Telethon reconnected successfully")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("Reconnect failed: %s — will retry in %ds", exc, _RECONNECT_DELAY)
            record_event("telethon", "reconnect", 0, "ERROR", str(exc))
    set_supervisor_ok(False)


async def _watchdog(client, shutdown: asyncio.Event) -> None:
    """Periodically check Telethon health; force-disconnect if stalled."""
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
                record_event("watchdog", "health check", 0, "SUCCESS")
                set_watchdog_ok(True)
            except asyncio.TimeoutError:
                logger.warning("Watchdog: health check timed out — forcing disconnect")
                record_event("watchdog", "health check", _WATCHDOG_TIMEOUT * 1000, "TIMEOUT")
                set_watchdog_ok(False)
                try:
                    await client.disconnect()
                except Exception:
                    pass
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("Watchdog: health check failed (%s) — forcing disconnect", exc)
                record_event("watchdog", "health check", 0, "ERROR", str(exc))
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

    # ── Phase 1: Database warm-up (optional) ──────────────────────────────
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

    # ── Phase 2: Telethon client ──────────────────────────────────────────
    logger.info("[2/5] Connecting Telethon")
    client = await build_client(cfg["API_ID"], cfg["API_HASH"], cfg["SESSION_STRING"])
    set_telethon_connected(True)

    # ── Phase 3: Register command handlers (exactly once) ─────────────────
    logger.info("[3/5] Registering command handlers")
    register_all(client, cfg["OWNER_ID"], cfg["TZ"])

    # ── Phase 3.5: Helper bot (optional — Inline Mode + callbacks) ────────
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
                register_input_listener(client, cfg["OWNER_ID"])
                logger.info("[3.5/5] Helper bot online — Inline Mode enabled")
        except Exception as exc:
            logger.warning("[3.5/5] Helper bot failed: %s — inline UI disabled", exc)
            helper_client = None
    else:
        logger.info("[3.5/5] Helper bot: no BOT_TOKEN — inline UI disabled")

    # ── Phase 4: Resume bio cron if it was active before last restart ─────
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

    # ── Phase 5: Web server (background, non-blocking) ────────────────────
    logger.info("[5/5] Starting web server on port %s", cfg["PORT"])
    web_task = asyncio.create_task(_run_web(cfg["PORT"]), name="lifeos-web")

    # ── Supervisors: Telethon + watchdog + heartbeat ────────────────────────
    tg_supervisor = asyncio.create_task(
        _supervise_telethon(client, shutdown), name="lifeos-tg-supervisor"
    )
    watchdog_task = asyncio.create_task(
        _watchdog(client, shutdown), name="lifeos-watchdog"
    )
    heartbeat_task = asyncio.create_task(
        _heartbeat(shutdown), name="lifeos-heartbeat"
    )

    logger.info("LifeOS online.")

    # Wait for shutdown signal — supervisors keep the bot alive indefinitely
    await shutdown.wait()

    # ── Shutdown A: bio cron ──────────────────────────────────────────────
    logger.info("Shutdown: stopping bio cron")
    bio_engine.stop_cron()
    set_bio_cron_ok(False)
    set_supervisor_ok(False)
    set_telethon_connected(False)

    # ── Shutdown B: web server ────────────────────────────────────────────
    logger.info("Shutdown: signalling web server")
    if _uvicorn_server is not None:
        _uvicorn_server.should_exit = True

    # ── Shutdown C: all remaining tasks ───────────────────────────────────
    logger.info("Shutdown: cancelling all tasks")
    pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    for task in pending:
        task.cancel()
    await asyncio.gather(*pending, return_exceptions=True)

    # ── Shutdown D: Helper bot ──────────────────────────────────────────
    if helper_client is not None:
        logger.info("Shutdown: disconnecting helper bot")
        await disconnect_helper()

    # ── Shutdown E: Telethon ─────────────────────────────────────────────
    logger.info("Shutdown: disconnecting Telethon")
    try:
        await client.disconnect()
    except Exception as exc:
        logger.warning("Telethon disconnect: %s", exc)

    logger.info("LifeOS stopped cleanly.")


if __name__ == "__main__":
    asyncio.run(main())
