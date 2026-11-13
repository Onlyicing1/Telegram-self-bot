"""
Intelligent Reaction outgoing handler.

Wires the reaction engine onto the EXISTING self-client update path: one more
``events.NewMessage(outgoing=True)`` handler registered through
``backend.bot.router.register_all`` — no second client, no second update loop,
no scheduler, no executor, no polling. It only builds the bounded context
(message text, coarse chat type, explicit reply state, at most one replied-to
text) and hands the message to ``backend.services.intelligent_reaction_service``,
which owns every decision (ownership, eligibility, dedup, settings, probability,
catalog, AI call, validation and the single reaction attempt).

Nothing is ever reported back into the chat: a disabled mode, a probability
miss, a declined evaluation or a rejected reaction produces no visible side
effect — no fake success, no marker. ``via_bot_id`` is passed through so the
Glass UI panel machinery (inline-bot-origin messages) is structurally excluded.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from telethon import events

from backend.bot.handlers.guard import is_owner
from backend.services.intelligent_reaction_service import (
    classify_chat_type,
    process_outgoing_message,
)
from backend.telegram_api._helpers import serialize_message

logger = logging.getLogger(__name__)


def _chat_type(event: Any) -> str:
    """Coarse classification only — never an id, title or member list."""
    chat = getattr(event, "chat", None)
    return classify_chat_type(
        is_private=bool(getattr(event, "is_private", False) or getattr(chat, "is_user", False)),
        is_group=bool(getattr(event, "is_group", False) or getattr(chat, "is_group", False)),
        is_channel=bool(
            getattr(event, "is_channel", False) or getattr(chat, "is_channel", False)
        ),
        is_supergroup=bool(getattr(chat, "is_supergroup", False)),
    )


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

        reply_to = message.get("reply_to_msg_id")
        is_reply = isinstance(reply_to, int) and not isinstance(reply_to, bool) and reply_to > 0
        replied_text = ""
        if is_reply:
            try:
                reply = await event.get_reply_message()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.info(
                    "[INTELLIGENT_REACTION] reply context unavailable: %s", type(exc).__name__,
                )
                reply = None
            text = getattr(reply, "message", None) or getattr(reply, "text", None) or ""
            if isinstance(text, str):
                replied_text = text

        try:
            await process_outgoing_message(
                owner_id=owner_id,
                client=client,
                message=message,
                chat_type=_chat_type(event),
                is_reply=is_reply,
                replied_text=replied_text,
                via_bot_id=getattr(raw_message, "via_bot_id", None),
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("[INTELLIGENT_REACTION] handler failed: %s", type(exc).__name__)
