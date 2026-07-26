"""
Inline sender — self-bot side of the Inline Mode architecture.

Provides send_inline_panel which:
  1. Triggers inline mode on the helper bot.
  2. Auto-sends the first inline result.
  3. Starts the panel auto-close timer (only if globally enabled).
  4. Deletes the triggering command message (zero-spam).
"""
import logging

from telethon import events

from backend.bot.handlers.guard import is_owner
from backend.helper import inline_engine
from backend.helper.input_state import get_pending, clear_pending
from backend.helper.panel_timer import init_panel
from backend.helper.panels import _create_session

logger = logging.getLogger(__name__)


async def send_inline_panel(self_client, chat_id: int, query: str) -> bool:
    helper_username = inline_engine.get_helper_username()
    if not helper_username:
        return False

    try:
        success, msg_chat_id, msg_id = await inline_engine.trigger(self_client, chat_id, query)
        logger.info(
            "[PANEL] PANEL CREATED query='%s' trigger_chat_id=%s trigger_msg_id=%s "
            "success=%s init_panel_key='%s:%s'",
            query, msg_chat_id, msg_id, success,
            msg_chat_id, msg_id,
        )
        if success and msg_id:
            _create_session(msg_chat_id, msg_id, panel_type=query)
            init_panel(self_client, msg_chat_id, msg_id)
        return success
    except Exception:
        return False


def register_input_listener(self_client, owner_id: int) -> None:

    @self_client.on(events.NewMessage(outgoing=True))
    async def _input_listener(event):
        if not is_owner(event, owner_id):
            return

        pending = get_pending(owner_id)
        if not pending:
            return

        if event.chat_id != pending["chat_id"]:
            return

        text = event.raw_text or ""
        if text.startswith("."):
            return

        pending_entry = clear_pending(owner_id)
        if not pending_entry:
            return

        handler = pending_entry["handler"]
        inline_chat_id = pending_entry.get("inline_chat_id", 0)
        inline_msg_id = pending_entry.get("inline_msg_id", 0)
        try:
            await handler(text, event.chat_id, event.message.id, inline_chat_id, inline_msg_id)
        except Exception:
            logger.exception("Input listener handler failed")
