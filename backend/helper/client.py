"""
Helper bot client factory.

Creates a Telethon TelegramClient using a bot token (not a user session).
The helper bot is optional — if BOT_TOKEN is not set, build_helper returns None
and all inline UI features are silently disabled.

All network operations have bounded timeouts:
  - connect(): 30s
  - start(bot_token): 30s (wraps connect + auth)
  - get_me(): 15s
"""
import asyncio
import logging
import os

from telethon import TelegramClient, events
from telethon.sessions import StringSession

from backend.runtime.tracer import trace, trace_exception

logger = logging.getLogger(__name__)

_CONNECT_TIMEOUT = 30
_START_TIMEOUT = 30
_GET_ME_TIMEOUT = 15

_client: TelegramClient | None = None
_bot_username: str = ""
_bot_id: int = 0


def is_available() -> bool:
    return _client is not None and _client.is_connected()


def get_bot_username() -> str:
    return _bot_username


def get_bot_id() -> int:
    return _bot_id


async def build_helper(bot_token: str) -> TelegramClient | None:
    """Create and connect the helper bot client.

    Returns the connected TelegramClient or None if no token is set.
    Raises RuntimeError if the token is set but invalid.
    """
    global _client, _bot_username, _bot_id

    if not bot_token:
        logger.info("Helper bot: no BOT_TOKEN set — inline UI disabled")
        return None

    clean_token = bot_token.strip()
    api_id_raw = os.getenv("API_ID", "")
    api_hash_raw = os.getenv("API_HASH", "")

    client = TelegramClient(
        StringSession(),
        int(api_id_raw) if api_id_raw else 0,
        api_hash_raw,
        system_version="4.16.30-vxCUSTOM",
        device_model="LifeOS-Helper",
        auto_reconnect=True,
        connection_retries=5,
        retry_delay=2,
        flood_sleep_threshold=60,
    )

    try:
        await asyncio.wait_for(client.connect(), timeout=_CONNECT_TIMEOUT)
        await asyncio.wait_for(
            client.start(bot_token=clean_token),
            timeout=_START_TIMEOUT,
        )
        me = await asyncio.wait_for(client.get_me(), timeout=_GET_ME_TIMEOUT)
        _bot_username = (me.username or "").lstrip("@")
        _bot_id = me.id
        trace("HELPER_CONNECTED", username=_bot_username, id=me.id)
        logger.info("Helper bot connected: @%s (id=%s)", me.username, me.id)
    except Exception as exc:
        trace_exception("HELPER_CONNECT_FAILED", exc)
        try:
            await client.disconnect()
        except Exception:
            pass
        raise RuntimeError(
            f"Helper bot login failed: {exc}. "
            "Check BOT_TOKEN — it must be a valid bot token from BotFather."
        )

    _client = client
    return client


async def disconnect_helper() -> None:
    global _client, _bot_username, _bot_id
    _bot_username = ""
    _bot_id = 0
    if _client is not None:
        trace("HELPER_DISCONNECTED", reason="disconnect_helper_called")
        try:
            await asyncio.wait_for(_client.disconnect(), timeout=10.0)
        except (asyncio.TimeoutError, Exception) as exc:
            logger.warning("Helper bot disconnect error: %s", exc)
        _client = None


def get_client() -> TelegramClient | None:
    return _client


def register_helper_hooks(client) -> None:
    """Register runtime event hooks on the helper bot client.

    Tracks CallbackQuery and Raw events for health telemetry.
    Must be called after the helper bot is connected.
    """
    @client.on(events.CallbackQuery())
    async def _helper_callback_hook(event):
        from backend.health import set_last_callback, set_last_event_dispatch
        try:
            set_last_callback()
            set_last_event_dispatch()
        except Exception:
            pass

    @client.on(events.Raw)
    async def _helper_raw_hook(event):
        from backend.health import set_last_telethon_event
        try:
            set_last_telethon_event()
        except Exception:
            pass
