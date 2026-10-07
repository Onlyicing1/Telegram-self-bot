"""
Emoji Replacement outgoing handler — Phase 4 (Emoji & Reaction, ROADMAP
§15/§19/§24).

Wires the deterministic replacement pipeline onto the EXISTING self-client
update path: one more ``events.NewMessage(outgoing=True)`` handler registered
through ``backend.bot.router.register_all`` — no second client, no second
update loop, no scheduler, no executor, no polling. It only hands the owner's
outgoing message to
``backend.services.emoji_replacement_service.process_outgoing_message`` and
logs honest outcomes; every decision (category resolution, transformation,
delivery, deletion, loop prevention) lives in the service and the Phase 3
state boundary.

Nothing is ever reported back into the chat: a message without a mapped
emoji, a disabled toggle, no effective category, an unsafe entity
combination, or a failed delivery produces no visible side effect — no fake
success, no spam, no marker. ``via_bot_id`` is passed through so the Glass UI
panel machinery (inline-bot-origin messages) is structurally excluded.
"""
from __future__ import annotations

import asyncio
import logging

from telethon import events

from backend.bot.handlers.guard import is_owner
from backend.services.emoji_replacement_service import process_outgoing_message
from backend.telegram_api._helpers import serialize_message

logger = logging.getLogger(__name__)


def register(client, owner_id: int) -> None:
    """Register the single outgoing replacement handler on the self client."""

    @client.on(events.NewMessage(outgoing=True))
    async def emoji_replacement_handler(event):
        if not is_owner(event, owner_id):
            return

        raw_message = getattr(event, "message", None)
        message = serialize_message(raw_message)
        if not message:
            return

        try:
            await process_outgoing_message(
                owner_id=owner_id,
                client=client,
                message=message,
                via_bot_id=getattr(raw_message, "via_bot_id", None),
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("[EMOJI_REPL] handler failed: %s", exc)
