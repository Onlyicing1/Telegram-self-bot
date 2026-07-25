"""
Helper bot client factory.

The helper bot is a separate Telegram client that owns the InlineQuery and
CallbackQuery handlers. It runs as a bot (using ``BOT_TOKEN``) alongside the
self-bot. Both share the same asyncio event loop.

Public functions:
  - ``build_helper(token)`` — connect the helper bot and return the client.
  - ``get_client()`` — return the connected helper client (or None).
  - ``disconnect_helper()`` — disconnect the helper bot cleanly.
  - ``get_bot_username()`` — return the helper bot's @username (lowercased).
"""
import logging

from telethon import TelegramClient

from backend.config import load

logger = logging.getLogger(__name__)

_client: TelegramClient | None = None
_bot_username: str = ""


async def build_helper(token: str) -> TelegramClient | None:
    """Connect the helper bot and return the client.

    Returns None if the token is missing or connection fails.
    """
    global _client, _bot_username

    if not token:
        logger.warning("[HELPER] No BOT_TOKEN provided — helper bot disabled")
        return None

    cfg = load()
    client = TelegramClient(
        "helper_bot",
        cfg["API_ID"],
        cfg["API_HASH"],
        connection_retries=5,
        retry_delay=2,
        flood_sleep_threshold=60,
    )

    try:
        await client.connect()
        if not client.is_connected():
            logger.warning("[HELPER] Helper bot failed to connect")
            return None
    except Exception:
        logger.exception("[HELPER] Helper bot connect failed")
        return None

    try:
        await client.start(bot_token=token)
        me = await client.get_me()
        if me and me.username:
            _bot_username = me.username.lower()
            logger.info("[HELPER] Helper bot started: @%s (id=%s)", _bot_username, me.id)
        else:
            logger.warning("[HELPER] Helper bot has no username — inline mode unavailable")
    except Exception:
        logger.exception("[HELPER] Helper bot start failed")
        await client.disconnect()
        return None

    _client = client
    logger.info("=== HELPER CLIENT IDENTITY DIAGNOSTIC ===")
    logger.info("HELPER_CLIENT id=%s type=%s", id(client), type(client).__name__)
    try:
        handlers = client.list_event_handlers()
        logger.info("HELPER_CLIENT handlers at handoff: count=%d", len(handlers))
        for i, (builder, handler) in enumerate(handlers):
            logger.info("HELPER_CLIENT handler[%d]: builder_type=%s, handler=%s",
                        i, type(builder).__name__, getattr(handler, '__name__', str(handler)))
    except Exception as exc:
        logger.warning("HELPER_CLIENT handler dump failed: %s", exc)
    logger.info("HELPER_CLIENT get_client() id=%s (should match above)", id(_client))
    logger.info("=== END HELPER CLIENT IDENTITY DIAGNOSTIC ===")
    return client


def get_client() -> TelegramClient | None:
    return _client


def get_bot_username() -> str:
    return _bot_username


async def disconnect_helper() -> None:
    global _client
    if _client is not None:
        try:
            await _client.disconnect()
        except Exception:
            pass
    _client = None
