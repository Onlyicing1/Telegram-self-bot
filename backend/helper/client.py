"""
Helper bot client factory.

Creates a Telethon ``TelegramClient`` using a bot token (not a user session).
The helper bot is optional — if ``BOT_TOKEN`` is not set, ``build_helper``
returns ``None`` and all inline UI features are silently disabled.

The helper bot uses the same Telethon connection parameters as the self-bot
for consistency: auto-reconnect, 5 retries, 2s delay, 60s flood-sleep.
"""
import logging
import os

from telethon import TelegramClient
from telethon.sessions import StringSession

logger = logging.getLogger(__name__)

_client: TelegramClient | None = None
_bot_username: str = ""


def is_available() -> bool:
    """Return True if the helper bot was successfully started."""
    return _client is not None and _client.is_connected()


def get_bot_username() -> str:
    """Return the helper bot's username (without @), or empty string."""
    return _bot_username


def _mask_token(token: str) -> str:
    """Return a masked version of the token for logging (first 6 + last 4)."""
    if len(token) <= 10:
        return "***"
    return f"{token[:6]}...{token[-4:]}"


async def build_helper(bot_token: str) -> TelegramClient | None:
    """
    Create and connect the helper bot client.

    Returns the connected ``TelegramClient`` or ``None`` if no token is set.
    Raises ``RuntimeError`` if the token is set but invalid.
    """
    global _client

    # ── Diagnostic report ────────────────────────────────────────────────
    env_ok = bool(bot_token)
    token_masked = _mask_token(bot_token) if bot_token else "(empty)"
    token_stripped = bot_token.strip() if bot_token else ""
    has_whitespace = bool(bot_token) and bot_token != token_stripped
    token_len = len(bot_token) if bot_token else 0

    api_id_raw = os.getenv("API_ID", "")
    api_hash_raw = os.getenv("API_HASH", "")

    logger.info("=== HELPER BOT STARTUP REPORT ===")
    logger.info("HELPER ENV ........ %s", "OK" if env_ok else "FAIL")
    logger.info("HELPER TOKEN ...... %s (len=%d, stripped_len=%d, whitespace=%s)",
                token_masked, token_len, len(token_stripped), has_whitespace)
    logger.info("HELPER VAR NAME ... BOT_TOKEN (matches README + render.yaml)")
    logger.info("HELPER FALLBACK ... default='' in config.py, no override")
    logger.info("API_ID ............ present=%s (len=%d)", bool(api_id_raw), len(api_id_raw))
    logger.info("API_HASH .......... present=%s (len=%d)", bool(api_hash_raw), len(api_hash_raw))

    if not bot_token:
        logger.info("Helper bot: no BOT_TOKEN set — inline UI disabled")
        logger.info("=== END HELPER BOT REPORT ===")
        return None

    if has_whitespace:
        logger.warning("HELPER TOKEN has leading/trailing whitespace — stripping for validation")

    clean_token = token_stripped

    # ── Lightweight validation: call get_me via bot_token before full client ──
    validation_pass = False
    validation_error = ""
    validation_exc_type = ""
    try:
        validation_client = TelegramClient(
            StringSession(),
            int(api_id_raw) if api_id_raw else 0,
            api_hash_raw,
            system_version="4.16.30-vxCUSTOM",
            device_model="LifeOS-Helper-Validate",
            auto_reconnect=False,
            connection_retries=1,
        )
        await validation_client.connect()
        await validation_client.start(bot_token=clean_token)
        me = await validation_client.get_me()
        validation_pass = True
        logger.info("HELPER VALIDATION . PASS — bot @%s (id=%s)", me.username, me.id)
        await validation_client.disconnect()
    except Exception as exc:
        validation_error = str(exc)
        validation_exc_type = type(exc).__name__
        logger.info("HELPER VALIDATION . FAIL — %s: %s", validation_exc_type, validation_error)
        try:
            await validation_client.disconnect()
        except Exception:
            pass

    # ── Full client startup ───────────────────────────────────────────────
    login_pass = False
    login_error = ""
    login_exc_type = ""
    client = None
    try:
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
        await client.connect()
        await client.start(bot_token=clean_token)
        me = await client.get_me()
        login_pass = True
        global _bot_username
        _bot_username = (me.username or "").lstrip("@")
        logger.info("HELPER LOGIN ...... PASS — bot @%s (id=%s)", me.username, me.id)
    except Exception as exc:
        login_error = str(exc)
        login_exc_type = type(exc).__name__
        logger.info("HELPER LOGIN ...... FAIL — %s: %s", login_exc_type, login_error)
        if client is not None:
            try:
                await client.disconnect()
            except Exception:
                pass

    # ── Final report ──────────────────────────────────────────────────────
    reason = ""
    if not validation_pass:
        reason = f"{validation_exc_type}: {validation_error}"
    elif not login_pass:
        reason = f"{login_exc_type}: {login_error}"

    logger.info("HELPER ENV ........ %s", "OK" if env_ok else "FAIL")
    logger.info("HELPER TOKEN ...... %s", token_masked)
    logger.info("HELPER VALIDATION . %s", "PASS" if validation_pass else "FAIL")
    logger.info("HELPER LOGIN ...... %s", "PASS" if login_pass else "FAIL")
    logger.info("REASON ............ %s", reason if reason else "(none)")
    logger.info("=== END HELPER BOT REPORT ===")

    if not login_pass:
        raise RuntimeError(
            f"Helper bot login failed: {reason}. "
            "Check BOT_TOKEN — it must be a valid bot token from BotFather."
        )

    _client = client
    return client


async def disconnect_helper() -> None:
    """Disconnect the helper bot cleanly."""
    global _client
    if _client is not None:
        try:
            await _client.disconnect()
        except Exception as exc:
            logger.warning("Helper bot disconnect error: %s", exc)
        _client = None


def get_client() -> TelegramClient | None:
    """Return the current helper bot client (or None)."""
    return _client
