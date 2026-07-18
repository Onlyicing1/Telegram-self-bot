"""
Telethon client factory — headless StringSession, never interactive.

Every external await (connect, is_user_authorized, get_me) is bounded by a
hard timeout and instrumented with START/END logging so a stalled socket is
always visible. Connection parameters tuned for Render Free tier:
  auto_reconnect     — transparently recover from network blips
  connection_retries — up to 5 attempts per disconnect event
  retry_delay        — 2 s between retry attempts
  flood_sleep_threshold — auto-sleep up to 60 s on Telegram flood responses
"""
import asyncio
import logging
import time

from telethon import TelegramClient
from telethon.sessions import StringSession

logger = logging.getLogger(__name__)

_CONNECT_TIMEOUT = 30.0
_AUTH_TIMEOUT = 15.0
_GET_ME_TIMEOUT = 15.0


async def _timed(coro, op: str, timeout: float):
    """Run a coroutine with a hard timeout + START/END instrumentation."""
    logger.info("[TG] START %s", op)
    start = time.monotonic()
    try:
        result = await asyncio.wait_for(coro, timeout=timeout)
        logger.info("[TG] END %s (%.2f sec)", op, time.monotonic() - start)
        return result
    except asyncio.TimeoutError:
        logger.error("[TG] TIMEOUT %s after %.0fs", op, timeout)
        raise
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("[TG] ERROR %s", op)
        raise


async def build_client(
    api_id: int,
    api_hash: str,
    session_string: str,
) -> TelegramClient:
    client = TelegramClient(
        StringSession(session_string),
        api_id,
        api_hash,
        system_version="4.16.30-vxCUSTOM",
        device_model="LifeOS",
        auto_reconnect=True,
        connection_retries=5,
        retry_delay=2,
        flood_sleep_threshold=60,
    )

    await _timed(client.connect(), "connect", _CONNECT_TIMEOUT)

    authorized = await _timed(client.is_user_authorized(), "is_user_authorized", _AUTH_TIMEOUT)
    if not authorized:
        raise RuntimeError(
            "Telethon session is not authorized. "
            "Re-generate SESSION_STRING and update the environment variable."
        )

    me = await _timed(client.get_me(), "get_me", _GET_ME_TIMEOUT)
    logger.info("Telethon connected as %s (id=%s)", me.first_name, me.id)
    return client
