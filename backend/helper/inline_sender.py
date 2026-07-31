"""
Inline sender — self-bot side of the Inline Mode architecture.

Provides send_inline_panel which:
  1. Checks if a panel already exists for this chat.
  2. If yes: edits the existing inline message (never duplicates).
  3. If no: triggers inline mode, auto-sends first result, starts timer.
  4. Deletes the triggering command message (zero-spam).
"""
import logging

from telethon import events

from backend.bot.handlers.guard import is_owner
from backend.helper import inline_engine
from backend.helper.input_state import get_pending, clear_pending
from backend.helper.panel_timer import init_panel, set_content
from backend.helper.session_manager import (
    create_session as _create_session,
    find_session_by_chat as _find_session_by_chat,
    push_nav as _push_nav,
    clear_session,
)

logger = logging.getLogger(__name__)


async def send_inline_panel(self_client, chat_id: int, query: str) -> bool:
    helper_username = inline_engine.get_helper_username()
    if not helper_username:
        logger.warning("[PANEL] send_inline_panel: no helper username — aborting")
        return False

    from backend.services import settings_service
    from backend.helper.panels import cleanup_panel_resources
    from backend.helper.session_manager import (
        destroy_all_sessions_for_chat,
        find_all_sessions_by_chat,
    )

    owner_id = inline_engine.get_owner_id()

    existing_sessions = find_all_sessions_by_chat(chat_id)

    if existing_sessions:
        reuse = settings_service.is_reuse_existing_panel()
        allow_multiple = settings_service.is_allow_multiple_panels()

        if reuse and len(existing_sessions) == 1:
            existing = existing_sessions[0]
            old_msg_id = existing.get("msg_id", 0)
            old_chat_id = existing.get("chat_id", chat_id)
            if old_msg_id and old_chat_id:
                from backend.helper.panel_render import render_edit
                from backend.helper.panels import _finalize_panel, _extract_render_result, get_panel

                parts = query.split(":", 1)
                panel_id = parts[0]
                extra = parts[1] if len(parts) > 1 else ""

                handler = get_panel(panel_id)

                if handler is not None:
                    try:
                        result = await handler(None, extra)
                        title, body, buttons = _extract_render_result(result)
                        if title or body or buttons:
                            title, body, buttons = _finalize_panel(
                                title, body, buttons, panel_id, old_chat_id, old_msg_id
                            )
                            text, built_buttons = render_edit(title, body, buttons)
                            set_content(old_chat_id, old_msg_id, title, body, buttons)
                            try:
                                await self_client.edit_message(
                                    old_chat_id, old_msg_id,
                                    message=text,
                                    buttons=built_buttons if built_buttons else [],
                                )
                            except Exception as exc:
                                if "not modified" not in str(exc).lower():
                                    logger.warning("[PANEL] edit existing failed: %s — falling back to new", exc)
                                else:
                                    return True
                            _push_nav(old_chat_id, old_msg_id, panel_id, extra)
                            return True
                    except Exception:
                        logger.warning("[PANEL] re-render existing panel failed — creating new")

        for session in existing_sessions:
            old_chat_id = session.get("chat_id", chat_id)
            old_msg_id = session.get("msg_id", 0)
            if old_chat_id and old_msg_id:
                cleanup_panel_resources(old_chat_id, old_msg_id, owner_id)

        destroy_all_sessions_for_chat(chat_id)

    try:
        success, msg_chat_id, msg_id, inline_msg_id = await inline_engine.trigger(self_client, chat_id, query)
        logger.info(
            "[PANEL] PANEL CREATED query='%s' chat_id=%s msg_id=%s "
            "inline_msg_id='%s' success=%s",
            query, msg_chat_id, msg_id, inline_msg_id, success,
        )
        if success and msg_id:
            parts = query.split(":", 1)
            panel_id = parts[0]
            extra = parts[1] if len(parts) > 1 else ""
            _create_session(
                msg_chat_id, msg_id,
                panel_type=panel_id, extra=extra,
                inline_message_id=inline_msg_id,
            )
            init_panel(self_client, msg_chat_id, msg_id, owner_id=owner_id)
        return success
    except Exception:
        logger.exception("[PANEL] send_inline_panel: exception")
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
