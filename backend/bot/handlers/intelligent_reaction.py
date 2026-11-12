"""
Intelligent Reaction outgoing handler.

Wires the AI-driven reaction engine onto the EXISTING self-client update path:
one more ``events.NewMessage(outgoing=True)`` handler registered through
``backend.bot.router.register_all`` — no second client, no second update loop,
no scheduler, no executor, no polling. It only hands the owner's outgoing
message to ``backend.services.intelligent_reaction_service``, which owns every
decision (ownership, eligibility, dedup, settings, catalog, AI evaluation, the
independent probability draw and the single reaction attempt).

Nothing is ever reported back into the chat: a disabled mode, a message the AI
declined, a failed evaluation or a rejected reaction produces no visible side
effect — no fake success, no marker. ``via_bot_id`` is passed through so the
Glass UI panel machinery (inline-bot-origin messages) is structurally excluded.
"""
from __future__ import annotations

import asyncio
import logging

from telethon import events

from backend.bot.handlers.guard import is_owner
from backend.services.intelligent_reaction_service import process_outgoing_message
from backend.telegram_api._helpers import serialize_message

logger = logging.getLogger(__name__)


def register(client, owner_id: int) -> None:
    """Register the single outgoing intelligent-reaction handler."""

    @client.on(events.NewMessage(outgoing=True))
    async def intelligent_reaction_handler(event):
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
            logger.warning("[INTELLIGENT_REACTION] handler failed: %s", exc)
