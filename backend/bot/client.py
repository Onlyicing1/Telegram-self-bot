"""
Telethon client factory — headless StringSession, never interactive.

Connection parameters tuned for Render Free tier:
  auto_reconnect     — transparently recover from network blips
  connection_retries — up to 5 attempts per disconnect event
  retry_delay        — 2 s between retry attempts
  flood_sleep_threshold — auto-sleep up to 60 s on Telegram flood responses
"""
import logging
from telethon import TelegramClient, events
from telethon.sessions import StringSession

logger = logging.getLogger(__name__)


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
    await client.connect()

    if not await client.is_user_authorized():
        raise RuntimeError(
            "Telethon session is not authorized. "
            "Re-generate SESSION_STRING and update the environment variable."
        )

    me = await client.get_me()
    logger.info("Telethon connected as %s (id=%s)", me.first_name, me.id)

    return client
