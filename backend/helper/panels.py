"""
Inline panel system for the helper bot.

ROOT MENU rules:
  - Root menu (nav_stack length 1) has ONLY Close — never Back or Home.
  - Submenus (nav_stack length > 1) have Back, Home, and Close.

SESSION LIFECYCLE:
  All lifecycle management goes through the PanelLifecycleManager.
  This module handles callback routing and rendering only.
"""
import asyncio
import logging
from typing import Awaitable, Callable, Any

from telethon import events
from telethon.tl.custom import Button

from backend.bot.handlers.guard import is_owner
from backend.helper.context import truncate_callback_data
from backend.helper.input_state import set_pending, clear_pending, get_pending
from backend.helper.lifecycle import get_lifecycle
from backend.helper.session_manager import Session

logger = logging.getLogger(__name__)


def _buttons_repr(buttons: list) -> tuple[tuple[str, ...], ...]:
    result = []
    for row in buttons:
        row_data = []
        if isinstance(row, list):
            for btn in row:
                data = getattr(btn, "data", None)
                if isinstance(data, bytes):
                    row_data.append(data.decode("utf-8", errors="replace"))
                elif isinstance(data, str):
                    row_data.append(data)
                else:
                    row_data.append(str(data))
        result.append(tuple(row_data))
    return tuple(result)


async def _safe_edit(event, text: str, buttons: list, chat_id: int | None, msg_id: int | None) -> bool:
    lifecycle = get_lifecycle()
    key = (chat_id, msg_id)
    new_repr = (text, _buttons_repr(buttons))
    last = lifecycle.get_render(chat_id or 0, msg_id or 0)
    if last == new_repr:
        logger.info("[CALLBACK] edit skipped — content unchanged (key=%s:%s)", chat_id, msg_id)
        return False
    try:
        await event.edit(text, buttons=buttons if buttons else [])
        lifecycle.set_render(chat_id or 0, msg_id or 0, text, new_repr)
        return True
    except Exception as exc:
        logger.warning("[CALLBACK] edit failed: %s (key=%s:%s)", exc, chat_id, msg_id)
        return False


def resolve_callback_message(event):
    chat_id = None
    msg_id = None
    inline_msg_id = None
    try:
        if hasattr(event, "message_id") and event.message_id:
            msg_id = event.message_id
        if hasattr(event, "chat_id") and event.chat_id:
            chat_id = event.chat_id
        if hasattr(event, "inline_message_id") and event.inline_message_id:
            inline_msg_id = event.inline_message_id
    except Exception:
        pass
    return chat_id, msg_id, inline_msg_id


def register_callback_handlers(client, owner_id: int) -> None:
    if client is None:
        return

    @client.on(events.CallbackQuery())
    async def _callback_router(event):
        from backend.services import settings_service
        from backend.health import set_last_callback

        try:
            set_last_callback()
        except Exception:
            pass

        chat_id, msg_id, inline_msg_id = resolve_callback_message(event)

        try:
            if not is_owner(event, owner_id):
                return

            data = event.data.decode("utf-8", errors="replace") if event.data else ""
            if not data:
                return

            data = truncate_callback_data(data)

            lifecycle = get_lifecycle()
            session = lifecycle.get_or_create(chat_id or 0, msg_id or 0, owner_id)
            if session is None:
                return

            session.nav_stack.append(data)
            await route_callback(event, session, data, chat_id, msg_id, inline_msg_id)

        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("[CALLBACK] router error: %s", exc)
            try:
                await event.answer("Error processing callback.")
            except Exception:
                pass


async def route_callback(event, session: Session, data: str, chat_id, msg_id, inline_msg_id) -> None:
    from backend.helper.panel_render import render_panel
    from backend.helper.panel_settings import handle_settings_callback

    try:
        if data == "close":
            await _safe_edit(event, "Panel closed.", [], chat_id, msg_id)
            get_lifecycle().close(chat_id or 0, msg_id or 0)
            return

        if data == "back":
            if len(session.nav_stack) > 1:
                session.nav_stack.pop()
                session.nav_stack.pop()
            data = session.nav_stack[-1] if session.nav_stack else "root"

        if data == "home":
            session.nav_stack.clear()
            data = "root"

        if data.startswith("settings:"):
            await handle_settings_callback(event, session, data, chat_id, msg_id, inline_msg_id)
            return

        text, buttons = render_panel(session, data)
        is_root = len(session.nav_stack) == 1

        if not is_root:
            buttons.append([Button.inline("◀️ Back", b"back"), Button.inline("🏠 Home", b"home")])
        buttons.append([Button.inline("✖️ Close", b"close")])

        await _safe_edit(event, text, buttons, chat_id, msg_id)

    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.exception("[CALLBACK] route error: %s", exc)
        try:
            await event.answer("Error rendering panel.")
        except Exception:
            pass
