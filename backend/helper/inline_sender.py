"""
Inline sender — self-bot side of the Inline Mode architecture.

Provides send_inline_panel which delegates ALL lifecycle management
to the PanelLifecycleManager. This module never touches sessions,
timers, or cleanup directly.
"""
import asyncio
import logging

from telethon import events

from backend.bot.handlers.guard import is_owner
from backend.helper import inline_engine
from backend.helper.input_state import get_pending, clear_pending
from backend.helper.lifecycle import get_lifecycle
from backend.helper.rpc_timeout import rpc_await

logger = logging.getLogger(__name__)


async def send_inline_panel(self_client, chat_id: int, query: str) -> bool:
    helper_username = inline_engine.get_helper_username()
    if not helper_username:
        from backend.helper import client as helper_client_mod
        if not helper_client_mod.is_available():
            logger.error(
                "[PANEL] send_inline_panel: helper bot is not connected — "
                "cannot use inline mode"
            )
        elif not inline_engine.get_helper_id():
            logger.error(
                "[PANEL] send_inline_panel: helper username is empty and "
                "helper id is 0 — GetMe likely failed during helper startup"
            )
        else:
            logger.error(
                "[PANEL] send_inline_panel: helper account has no public "
                "username (id=%s) — inline mode requires a @username, "
                "set one via BotFather or Telegram settings",
                inline_engine.get_helper_id(),
            )
        return False

    logger.info(
        "[PANEL] send_inline_panel: using helper @%s for query='%s'",
        helper_username, query,
    )

    from backend.services import settings_service

    parts = query.split(":", 1)
    panel_id = parts[0]
    extra = parts[1] if len(parts) > 1 else ""

    lifecycle = get_lifecycle()

    if settings_service.is_reuse_existing_panel():
        reused = await lifecycle.try_reuse_panel(chat_id, panel_id, extra)
        if reused:
            return True

    success, msg_chat_id, msg_id = await lifecycle.create_panel(
        chat_id, panel_id, extra
    )

    logger.info(
        "[PANEL] send_inline_panel query='%s' chat=%s msg=%s success=%s",
        query, msg_chat_id, msg_id, success,
    )
    return success


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
            await asyncio.wait_for(
                handler(text, event.chat_id, event.message.id, inline_chat_id, inline_msg_id),
                timeout=60.0,
            )
        except asyncio.TimeoutError:
            logger.error("INPUT_HANDLER_TIMEOUT owner=%s panel=%s — input handler exceeded 60s", owner_id, pending_entry.get("panel_id", ""))
        except Exception:
            logger.exception("Input listener handler failed")
