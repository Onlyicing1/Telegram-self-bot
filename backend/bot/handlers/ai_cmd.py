"""
.ai <text> — AI conversation command handler.

This handler is a thin entry point over the canonical AI execution path in
``backend.bot.handlers.ai_unified``. Trigger-word and reply-to-AI activation
are handled by ai_unified; this module only wires the legacy `.ai <text>` dot
command to the same shared pipeline so there is exactly ONE AI entry-point
implementation (no duplicated config restore, formatting, or timeout logic).
"""
import logging

from telethon import events

from backend.bot.handlers.guard import is_owner
from backend.bot.handlers.ai_unified import _execute_ai

logger = logging.getLogger(__name__)


def register(client, owner_id: int, tz_str: str):
    @client.on(events.NewMessage(outgoing=True, pattern=r"^\.ai(?:\s+(.+))?$"))
    async def ai_cmd(event):
        if not is_owner(event, owner_id):
            return

        raw_text = event.raw_text or ""
        match = raw_text.split(None, 1)
        user_message = match[1].strip() if len(match) > 1 else ""

        if not user_message:
            try:
                await event.edit(
                    "🧠 **AI Assistant**\n\n"
                    "Usage: `.ai <message>`\n\n"
                    "Example: `.ai Hello, how are you?`\n\n"
                    "I can help you save messages, manage your bio/username, "
                    "search saved items, delete messages, and more."
                )
            except Exception as exc:
                logger.warning("ai help edit failed: %s", exc)
            return

        await _execute_ai(event, owner_id, user_message, "AI", tz_str)
