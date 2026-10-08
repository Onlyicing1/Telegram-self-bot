"""
Bot bridge — send a RECONSTRUCTED message as the helper bot.

The Emoji & Reaction reconstruction flow (ROADMAP §15/§17) deletes the
owner's original outgoing message and delivers the emoji-transformed
content as a NEW message sent by a bot account in the SAME destination.
This module is that delivery step and nothing else:

  - it is send-only (no event handlers, no update loop, no polling);
  - the sender is the existing optional helper bot client
    (``backend/helper/client.py``) — no second bot infrastructure;
  - entity dicts are rebuilt into real Telethon ``MessageEntity`` objects
    (``formatting_entities=``), so formatting travels with the message;
  - every call is bounded and normalized exactly like the rest of
    ``backend/telegram_api``.

Capability constraint (ROADMAP §17/§28): bots can only attach
custom-emoji entities when the bot purchased additional usernames on
Fragment. Telegram enforces this server-side; this module sends the
transformed entities as-is and surfaces any server rejection as a
TelegramAPIError — capability fallback (e.g. alt-text degradation) is the
transformer/reconstructor's decision, not a silent behavior here.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from backend.helper import client as helper_client
from backend.runtime.operation_watchdog import guarded_await
from backend.telegram_api._helpers import dict_entities_to_tl
from backend.telegram_api.exceptions import (
    TelegramAPIError,
    TelegramTimeoutError,
)

logger = logging.getLogger(__name__)

_SHORT_CALL_TIMEOUT = 30.0


def bridge_available() -> bool:
    """True when the helper bot client is connected and usable."""
    return helper_client.is_available()


def bridge_bot_id() -> int:
    """Bot account id — the loop-prevention sender check (ROADMAP §24)."""
    return helper_client.get_bot_id()


async def send_reconstructed(
    client: Any,
    chat_id: int | str,
    text: str,
    entities: list[dict[str, Any]] | None = None,
    reply_to_msg_id: int | None = None,
    *,
    resolved_peer: Any = None,
) -> dict[str, Any]:
    """Send ``text`` with pre-built ``entities`` as the helper bot.

    ``client`` is the self-client, used only to resolve the destination
    peer so the bot sends to the SAME chat the original message lived in
    (ROADMAP §18). Entities are plain dicts (the ``serialize_message``
    representation); they are rebuilt into TL objects for the bot's
    ``send_message(formatting_entities=...)``.

    ``resolved_peer`` is an already-resolved input peer for a destination the
    self client cannot express as the BOT's own peer — the private chat the
    helper bot holds with the owner, whose access hash only the bot's session
    has. It is opt-in and resolves nothing itself: every existing caller keeps
    the same-destination resolution above unchanged.

    Raises:
        TelegramAPIError: helper bot unavailable, unknown entity type,
            missing entity payload, or Telegram rejected the send
            (e.g. the Fragment custom-emoji capability constraint).
        TelegramTimeoutError: the send exceeded the bounded timeout.
    """
    if not helper_client.is_available():
        raise TelegramAPIError("bot bridge unavailable: helper bot is not connected")
    if not text and not entities:
        raise TelegramAPIError("bot bridge: refusing to send an empty message")

    bot = helper_client.get_client()
    tl_entities = await dict_entities_to_tl(bot, entities)
    peer = resolved_peer if resolved_peer is not None else await client.get_input_entity(chat_id)
    try:
        msg = await guarded_await(
            bot.send_message(
                peer,
                text,
                formatting_entities=tl_entities or None,
                reply_to=reply_to_msg_id,
            ),
            name="telegram:bridge:send_reconstructed",
            timeout=_SHORT_CALL_TIMEOUT,
        )
    except asyncio.TimeoutError:
        raise TelegramTimeoutError(
            f"bridge send_reconstructed timed out after {_SHORT_CALL_TIMEOUT}s"
        ) from None
    except TelegramAPIError:
        raise
    except Exception as exc:
        raise TelegramAPIError(f"bridge send_reconstructed failed: {exc}") from exc

    # Serialized with the SELF client's user/cache context so ids/names in
    # the dict are consistent with the rest of the facade's output.
    from backend.telegram_api._helpers import serialize_message

    return serialize_message(msg)
