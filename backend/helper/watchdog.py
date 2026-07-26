"""
Helper Watchdog — monitors the helper bot and recovers it automatically.

Runs forever. Checks helper every 30 seconds.
  - If disconnected: reconnect automatically.
  - If reconnect repeatedly fails: rebuild helper automatically.
  - If helper freezes: recover automatically.
  - Never requires manual intervention.
  - Never crashes backend.main.
"""
import asyncio
import logging
import os

from backend.helper.client import build_helper, disconnect_helper, get_client
from backend.helper.panels import register_callback_handlers
from backend.helper.inline_engine import (
    register_inline_handler,
    set_self_client,
    set_helper_username,
    set_owner_id,
)
from backend.helper.client import get_bot_username

logger = logging.getLogger(__name__)

_CHECK_INTERVAL = 30
_MAX_RECONNECT_ATTEMPTS = 3
_RECONNECT_DELAY = 10
_REBUILD_DELAY = 15

_bot_token: str = ""
_self_client = None
_owner_id: int = 0
_task: asyncio.Task | None = None
_consecutive_failures: int = 0


def configure(bot_token: str, self_client, owner_id: int) -> None:
    global _bot_token, _self_client, _owner_id
    _bot_token = bot_token
    _self_client = self_client
    _owner_id = owner_id


def start() -> None:
    global _task
    if _task and not _task.done():
        return
    _task = asyncio.create_task(_watchdog_loop(), name="lifeos-helper-watchdog")
    logger.info("Helper watchdog started")


def stop() -> None:
    global _task
    if _task and not _task.done():
        _task.cancel()
    _task = None


async def _rebuild_helper() -> bool:
    """Tear down and rebuild the helper bot client from scratch."""
    logger.warning("Helper watchdog: rebuilding helper bot")
    try:
        await disconnect_helper()
    except Exception:
        pass

    if not _bot_token:
        logger.warning("Helper watchdog: no BOT_TOKEN — cannot rebuild")
        return False

    try:
        new_client = await build_helper(_bot_token)
        if new_client is None:
            return False

        register_callback_handlers(new_client, _owner_id)
        register_inline_handler(new_client, _owner_id)
        set_self_client(_self_client)
        set_helper_username(get_bot_username())
        set_owner_id(_owner_id)

        asyncio.create_task(
            new_client.run_until_disconnected(),
            name="lifeos-helper-supervisor",
        )
        logger.info("Helper watchdog: helper bot rebuilt successfully")
        return True
    except Exception as exc:
        logger.error("Helper watchdog: rebuild failed: %s", exc)
        return False


async def _watchdog_loop() -> None:
    global _consecutive_failures

    while True:
        try:
            await asyncio.sleep(_CHECK_INTERVAL)

            helper = get_client()
            if helper is None:
                if not _bot_token:
                    continue

                logger.warning("Helper watchdog: helper client is None — rebuilding")
                if await _rebuild_helper():
                    _consecutive_failures = 0
                else:
                    _consecutive_failures += 1
                continue

            if not helper.is_connected():
                logger.warning("Helper watchdog: helper disconnected — reconnecting")
                try:
                    await helper.connect()
                    if helper.is_connected():
                        logger.info("Helper watchdog: reconnected successfully")
                        _consecutive_failures = 0
                        continue
                except Exception as exc:
                    logger.warning("Helper watchdog: reconnect failed: %s", exc)

                _consecutive_failures += 1

                if _consecutive_failures >= _MAX_RECONNECT_ATTEMPTS:
                    logger.warning(
                        "Helper watchdog: %d consecutive failures — rebuilding",
                        _consecutive_failures,
                    )
                    if await _rebuild_helper():
                        _consecutive_failures = 0
                    else:
                        await asyncio.sleep(_REBUILD_DELAY)
            else:
                _consecutive_failures = 0

        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Helper watchdog loop error: %s", exc)
