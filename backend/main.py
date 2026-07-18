"""
LifeOS — deterministic entry point.

Startup phases (strict sequential):
  1. Config validation (hard-exit on missing required vars only)
  2. Database warm-up (optional — continues on failure)
  3. Telethon client — connect + authorize (bounded timeouts)
  4. Command handler registration (exactly once)
  5. Bio cron resume (if persisted active in DB)
  6. Uvicorn web server (background task)
  7. Telethon run loop (supervised under the supervisor)

Shutdown sequence on SIGTERM / SIGINT:
  A. Supervisor stops all supervised tasks (tg loop, bio cron, watchdog)
     + cancels remaining asyncio tasks (zero orphans).
  B. Uvicorn signalled to exit.
  C. Telethon disconnected cleanly.

Freeze protection: the main loop waits ONLY on the shutdown event — never on
client.run_until_disconnected(). The Telethon run loop is a supervised task
registered with the supervisor; if it exits (disconnect) the watchdog
restarts it within one tick after a successful reconnect. A half-open socket
that stalls run_until_disconnected() cannot hang the process because the
watchdog probes get_me() every 60 s and reconnects in-place.
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
from backend.supervisor import supervisor
from backend.web.app import app as web_app

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
logging.getLogger("backend").setLevel(logging.INFO)
logging.getLogger("telethon").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

_uvicorn_server: uvicorn.Server | None = None
_client = None

_TG_TASK_NAME = "tg-loop"


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


async def _reconnect_client() -> None:
    """Reconnect the existing Telethon client in-place (used by supervisor).

    Handlers are bound to this client object, so we reconnect the SAME instance
    rather than build a new one. Disconnect first to clear any stalled socket,
    then connect + re-verify authorization.
    """
    global _client
    if _client is None:
        logger.error("Reconnect requested but no client exists.")
        return
    try:
        await asyncio.wait_for(_client.disconnect(), timeout=10.0)
    except asyncio.TimeoutError:
        logger.error("Reconnect: disconnect timed out — forcing connect.")
    except Exception:
        logger.exception("Reconnect: disconnect error")
    try:
        await asyncio.wait_for(_client.connect(), timeout=30.0)
        if not await asyncio.wait_for(_client.is_user_authorized(), timeout=15.0):
            logger.error("Reconnect: session not authorized after reconnect.")
            return
        me = await asyncio.wait_for(_client.get_me(), timeout=15.0)
        logger.info("Telethon reconnected as %s (id=%s)", me.first_name, me.id)
    except asyncio.TimeoutError:
        logger.error("Reconnect: connect timed out — will retry next watchdog tick.")
    except Exception:
        logger.exception("Reconnect: connect error")


async def _tg_loop_factory() -> None:
    """Supervised Telethon run loop. Restarts automatically on disconnect."""
    global _client
    if _client is None:
        logger.error("tg-loop: no client — skipping run.")
        return
    try:
        logger.info("tg-loop: starting run_until_disconnected().")
        await _client.run_until_disconnected()
        logger.info("tg-loop: run_until_disconnected() returned (clean disconnect).")
    except asyncio.CancelledError:
        logger.info("tg-loop: cancelled.")
        raise
    except ConnectionError as exc:
        logger.warning("tg-loop: connection error: %s — watchdog will reconnect.", exc)
    except Exception:
        logger.exception("tg-loop: unexpected error — watchdog will reconnect.")


async def main() -> None:
    global _client
    cfg = cfg_module.load()

    shutdown: asyncio.Event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, shutdown.set)
        except NotImplementedError:
            pass

    # ── Phase 1: Database warm-up (optional) ──────────────────────────────
    logger.info("[1/7] Database warm-up")
    db = db_client.get_db()
    if db:
        try:
            await asyncio.wait_for(
                asyncio.to_thread(lambda: db.table("bot_logs").select("id").limit(1).execute()),
                timeout=15.0,
            )
            logger.info("[1/7] Database OK")
        except Exception as exc:
            logger.warning("[1/7] Database warm-up failed (%s) — continuing", exc)
    else:
        logger.info("[1/7] Using in-memory fallback — no database required")

    # ── Phase 2: Telethon client ──────────────────────────────────────────
    logger.info("[2/7] Connecting Telethon")
    _client = await build_client(cfg["API_ID"], cfg["API_HASH"], cfg["SESSION_STRING"])

    async def _reconnect_factory() -> None:
        await _reconnect_client()
    supervisor.set_client(_client, _reconnect_factory)

    # ── Phase 3: Register command handlers (exactly once) ─────────────────
    logger.info("[3/7] Registering command handlers")
    register_all(_client, cfg["OWNER_ID"], cfg["TZ"])

    # ── Phase 4: Resume bio cron if it was active before last restart ─────
    logger.info("[4/7] Bio cron resume check")
    try:
        state = await db_client.get_bio_state(cfg["OWNER_ID"])
        if state and state.get("is_active"):
            bio_engine.start_cron(_client, cfg["OWNER_ID"], cfg["TZ"])
            logger.info("[4/7] Bio cron resumed")
        elif cfg.get("BIO_UPDATE_ENABLED"):
            bio_engine.start_cron(_client, cfg["OWNER_ID"], cfg["TZ"])
            logger.info("[4/7] Bio cron started (BIO_UPDATE_ENABLED=true)")
        else:
            logger.info("[4/7] Bio cron not active — skipping")
    except Exception as exc:
        logger.warning("[4/7] Bio cron resume check failed: %s", exc)

    # ── Phase 5: Supervisor (watchdog + bio cron) ─────────────────────────
    supervisor.register(_TG_TASK_NAME, _tg_loop_factory, heartbeat_monitored=False)
    logger.info("[5/7] Starting supervisor (watchdog + supervised tasks)")
    supervisor.start_all()

    # ── Phase 6: Web server (background, non-blocking) ────────────────────
    logger.info("[6/7] Starting web server on port %s", cfg["PORT"])
    web_task = asyncio.create_task(_run_web(cfg["PORT"]), name="lifeos-web")

    # ── Phase 7: TG loop launched by supervisor in Phase 5 ────────────────
    logger.info("[7/7] Telethon run loop supervised under watchdog.")

    # ── Main: wait ONLY on shutdown signal ────────────────────────────────
    # The Telethon run loop is a supervised task. If it exits (disconnect),
    # the watchdog reconnects the client and restarts it — no process exit.
    logger.info("LifeOS online.")
    shutdown_task = asyncio.create_task(shutdown.wait(), name="lifeos-shutdown")
    await shutdown_task

    # ── Shutdown A: supervisor (tg loop + bio cron + watchdog) ────────────
    logger.info("Shutdown: stopping supervised tasks")
    await supervisor.stop_all()

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

    # ── Shutdown D: Telethon ──────────────────────────────────────────────
    logger.info("Shutdown: disconnecting Telethon")
    try:
        await _client.disconnect()
    except Exception as exc:
        logger.warning("Telethon disconnect: %s", exc)

    logger.info("LifeOS stopped cleanly.")


if __name__ == "__main__":
    asyncio.run(main())
