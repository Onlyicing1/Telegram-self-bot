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
    t_enter = _now_ms()
    trace_collector.trace("SEND_INLINE_PANEL ENTER (sender)")
    logger.info("[TRACE] send_inline_panel ENTER: t=%.1fms, chat_id=%s, query='%s', loop=%d, task='%s', tasks=%d",
                t_enter, chat_id, query, _loop_id(), _task_name(), _task_count())

    # ── Pre-flight: verify self_client ──
    sc_connected = self_client.is_connected() if self_client else False
    logger.info("[TRACE] send_inline_panel pre-flight: self_client connected=%s, is_none=%s",
                sc_connected, self_client is None)

    # ── Pre-flight: verify helper username ──
    helper_username = inline_engine.get_helper_username()
    logger.info("[TRACE] send_inline_panel pre-flight: helper_username='%s'", helper_username)
    if not helper_username:
        logger.error("[TRACE] send_inline_panel ABORT: helper_username is empty — inline UI will fail")
        trace_collector.trace("SEND_INLINE_PANEL ABORT: helper_username empty")
        return False

    # ── Pre-flight: verify helper client state ──
    from backend.helper.client import get_client
    helper = get_client()
    if helper is not None:
        hc_connected = helper.is_connected()
        logger.info("[TRACE] send_inline_panel pre-flight: helper_client connected=%s, loop=%d, is_none=False",
                    hc_connected, _loop_id())
        try:
            handlers = helper.list_event_handlers()
            logger.info("[TRACE] send_inline_panel pre-flight: helper_client event_handlers=%d", len(handlers))
        except Exception as e:
            logger.warning("[TRACE] send_inline_panel pre-flight: failed to list helper handlers: %s", e)
    else:
        logger.warning("[TRACE] send_inline_panel pre-flight: helper_client is None — get_client() returned None")

    # ── List all asyncio tasks for forensic snapshot ──
    try:
        all_tasks = asyncio.all_tasks()
        for i, t in enumerate(all_tasks):
            logger.info("[TRACE] send_inline_panel task[%d]: name='%s', done=%s, cancelled=%s",
                        i, t.get_name(), t.done(), t.cancelled())
    except RuntimeError:
        logger.warning("[TRACE] send_inline_panel: could not enumerate tasks")

    t_before_trigger = _now_ms()
    logger.info("[TRACE] send_inline_panel BEFORE trigger: elapsed=%.1fms", t_before_trigger - t_enter)
    try:
        trace_collector.trace("TRIGGER ENTER (sender calls inline_engine.trigger)")
        result = await inline_engine.trigger(self_client, chat_id, query)
        t_after_trigger = _now_ms()
        trace_collector.trace(f"TRIGGER DONE: ok={result}")
        logger.info("[TRACE] send_inline_panel AFTER trigger: elapsed=%.1fms, ok=%s",
                    t_after_trigger - t_enter, result)
    except Exception as exc:
        t_after_trigger = _now_ms()
        logger.error("[TRACE] send_inline_panel trigger EXCEPTION: elapsed=%.1fms, exc_type=%s, exc=%s",
                     t_after_trigger - t_enter, type(exc).__name__, exc)
        logger.exception("[TRACE] send_inline_panel trigger traceback:")
        result = False

    if not result:
        logger.warning("[TRACE] send_inline_panel returning False — trigger() returned False")
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
