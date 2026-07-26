"""
Inline sender — self-bot side of the Inline Mode architecture.

Provides send_inline_panel which:
  1. Triggers inline mode on the helper bot.
  2. Auto-sends the first inline result.
  3. Starts the panel auto-delete timer (120 seconds).
  4. Deletes the triggering command message (zero-spam).

Also provides register_input_listener which wires a NewMessage
handler on the self-bot to listen for the owner's next message when
a panel is in "input" state (Type B commands).
"""
import logging

from telethon import events

from backend.bot.handlers.guard import is_owner
from backend.helper import inline_engine
from backend.helper.input_state import get_pending, clear_pending
from backend.helper.panel_timer import init_panel

logger = logging.getLogger(__name__)


async def send_inline_panel(self_client, chat_id: int, query: str) -> bool:
    """Trigger inline mode, auto-send the first result, init auto-close timer."""
    helper_username = inline_engine.get_helper_username()
    if not helper_username:
        return False

    try:
        success, msg_chat_id, msg_id = await inline_engine.trigger(self_client, chat_id, query)
        if success and msg_id:
            init_panel(self_client, msg_chat_id, msg_id)
        return success
    except Exception:
        return False


def register_input_listener(self_client, owner_id: int) -> None:
    """Wire a handler that listens for the owner's next message when
    a panel is in input state.
    """

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
