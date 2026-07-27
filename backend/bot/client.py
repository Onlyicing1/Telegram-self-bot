"""
Telethon client factory — headless StringSession, never interactive.

All network operations have bounded timeouts:
  - connect(): 30s
  - is_user_authorized(): 15s
  - get_me(): 15s

Connection parameters tuned for Render Free tier:
  auto_reconnect     — transparently recover from network blips
  connection_retries — up to 5 attempts per disconnect event
  retry_delay        — 2 s between retry attempts
  flood_sleep_threshold — auto-sleep up to 60 s on Telegram flood responses
"""
import asyncio
import logging

from telethon import TelegramClient
from telethon.sessions import StringSession

logger = logging.getLogger(__name__)

_CONNECT_TIMEOUT = 30
_AUTHORIZE_TIMEOUT = 15
_GET_ME_TIMEOUT = 15


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

    await asyncio.wait_for(client.connect(), timeout=_CONNECT_TIMEOUT)

    authorized = await asyncio.wait_for(
        client.is_user_authorized(), timeout=_AUTHORIZE_TIMEOUT
    )
    if not authorized:
        raise RuntimeError(
            "Telethon session is not authorized. "
            "Re-generate SESSION_STRING and update the environment variable."
        )

    me = await asyncio.wait_for(client.get_me(), timeout=_GET_ME_TIMEOUT)
    logger.info("Telethon connected as %s (id=%s)", me.first_name, me.id)

    return client
