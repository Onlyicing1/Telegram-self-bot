"""
Inline sender — self-bot side of the Inline Mode architecture.

Provides ``send_inline_panel`` which:
  1. Triggers inline mode on the helper bot.
  2. Auto-sends the first inline result.
  3. Deletes the triggering command message (zero-spam).

Also provides ``register_input_listener`` which wires a NewMessage
handler on the self-bot to listen for the owner's next message when
a panel is in "input" state (Type B commands).
"""
import asyncio
import logging
import time
import traceback

from telethon import events

from backend.bot.handlers.guard import is_owner
from backend.helper import inline_engine
from backend.helper import trace_collector
from backend.helper.input_state import (
    get_pending,
    clear_pending,
    has_pending,
)

logger = logging.getLogger(__name__)


def _now_ms() -> float:
    return time.monotonic() * 1000.0


def _loop_id() -> int:
    try:
        return id(asyncio.get_running_loop())
    except RuntimeError:
        return 0


def _task_name() -> str:
    try:
        t = asyncio.current_task()
        return t.get_name() if t else "(none)"
    except RuntimeError:
        return "(no-loop)"


def _task_count() -> int:
    try:
        return len(asyncio.all_tasks())
    except RuntimeError:
        return -1


async def send_inline_panel(self_client, chat_id: int, query: str) -> bool:
    """Trigger inline mode and auto-send the first result.

    Returns True on success, False on failure.
    """
    trace_collector.trace("SEND_INLINE_PANEL ENTER (sender)")

    helper_username = inline_engine.get_helper_username()
    if not helper_username:
        trace_collector.trace("SEND_INLINE_PANEL ABORT: helper_username empty")
        return False

    from backend.helper.client import get_client
    helper = get_client()
    if helper is not None:
        trace_collector.trace(f"HELPER connected={helper.is_connected()}")
    else:
        trace_collector.trace("HELPER is None")

    trace_collector.trace("TRIGGER ENTER (sender)")
    try:
        result = await inline_engine.trigger(self_client, chat_id, query)
        trace_collector.trace(f"TRIGGER DONE: ok={result}")
    except Exception as exc:
        trace_collector.trace(f"TRIGGER EXCEPTION: {type(exc).__name__}: {exc}")
        result = False

    return result


def register_input_listener(self_client, owner_id: int) -> None:
    """Wire a handler that listens for the owner's next message when
    a panel is in input state.

    This is the self-bot side of Type B (input-required) panels.
    When the helper bot sets a pending input via ``input_state.set_pending``,
    the self-bot listens for the owner's next outgoing message in the same
    chat and feeds it to the pending handler.
    """
    logger.info("[INPUT_LISTENER] register_input_listener() entered: owner_id=%s", owner_id)

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
        logger.info("[INPUT_LISTENER] dispatching: text='%s', chat_id=%s, msg_id=%s, inline_chat_id=%s, inline_msg_id=%s",
                    text, event.chat_id, event.message.id, inline_chat_id, inline_msg_id)
        try:
            await handler(text, event.chat_id, event.message.id, inline_chat_id, inline_msg_id)
            logger.info("[INPUT_LISTENER] handler completed")
        except Exception:
            logger.exception("[INPUT_LISTENER] handler FAILED")
