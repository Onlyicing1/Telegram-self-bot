"""
Bio command handler.

Business logic lives in backend.services.bio_service.
This handler is only the Telethon wiring + panel rendering.
"""
import logging

from telethon import events

from backend.bot.handlers.guard import is_owner
from backend.db import client as db_client
from backend.services import bio_service
from backend.helper import (
    InlinePanelBuilder,
    register_panel,
    register_inline_builder,
    register_action,
    register_input,
    send_inline_panel,
    render,
    to_edit_buttons,
)
from backend.helper.client import get_client

logger = logging.getLogger(__name__)


async def _bio_on_action(event, extra: str, chat_id: int) -> tuple[str, str, list] | None:
    from backend.helper.inline_engine import _self_client, _owner_id
    from backend.bot.handlers.misc import _resolve_tz
    result = await bio_service.do_on(_self_client, _owner_id, _resolve_tz())
    return "Bio Engine", result, []


async def _bio_off_action(event, extra: str, chat_id: int) -> tuple[str, str, list] | None:
    from backend.helper.inline_engine import _owner_id
    result = await bio_service.do_off(_owner_id)
    return "Bio Engine", result, []


async def _bio_show_action(event, extra: str, chat_id: int) -> tuple[str, str, list] | None:
    from backend.helper.inline_engine import _owner_id
    from backend.bot.handlers.misc import _resolve_tz
    result = await bio_service.do_show(_owner_id, _resolve_tz())
    return "Bio Engine", result, []


async def _bio_help_action(event, extra: str, chat_id: int) -> tuple[str, str, list] | None:
    return "Bio Engine", bio_service._HELP, []


async def _bio_template_input_handler(text, chat_id, msg_id, inline_chat_id, inline_msg_id):
    from backend.helper.inline_engine import _owner_id, _self_client
    result = await bio_service.do_template(_owner_id, text)
    helper = get_client()
    if helper and inline_chat_id and inline_msg_id:
        try:
            await helper.edit_message(inline_chat_id, inline_msg_id, result)
        except Exception as exc:
            logger.warning("bio template inline edit failed: %s", exc)
    if _self_client:
        try:
            await _self_client.delete_messages(chat_id, [msg_id])
        except Exception:
            pass


async def _bio_text_input_handler(text, chat_id, msg_id, inline_chat_id, inline_msg_id):
    from backend.helper.inline_engine import _owner_id, _self_client
    result = await bio_service.do_text(_owner_id, text)
    helper = get_client()
    if helper and inline_chat_id and inline_msg_id:
        try:
            await helper.edit_message(inline_chat_id, inline_msg_id, result)
        except Exception as exc:
            logger.warning("bio text inline edit failed: %s", exc)
    if _self_client:
        try:
            await _self_client.delete_messages(chat_id, [msg_id])
        except Exception:
            pass


async def _bio_mood_input_handler(text, chat_id, msg_id, inline_chat_id, inline_msg_id):
    from backend.helper.inline_engine import _owner_id, _self_client
    result = await bio_service.do_mood(_owner_id, text)
    helper = get_client()
    if helper and inline_chat_id and inline_msg_id:
        try:
            await helper.edit_message(inline_chat_id, inline_msg_id, result)
        except Exception as exc:
            logger.warning("bio mood inline edit failed: %s", exc)
    if _self_client:
        try:
            await _self_client.delete_messages(chat_id, [msg_id])
        except Exception:
            pass


async def _bio_panel_handler(event, extra: str) -> tuple[str, str, list] | None:
    builder = InlinePanelBuilder()
    builder.add_row("✅ Bio ON", "action:bio_on")
    builder.add_row("⏹ Bio OFF", "action:bio_off")
    builder.add_row("👁 Show State", "action:bio_show")
    builder.add_row("📝 Set Template", "input:bio:template")
    builder.add_row("💬 Set Text", "input:bio:text")
    builder.add_row("💭 Set Mood", "input:bio:mood")
    builder.add_row("❓ Help", "action:bio_help")
    return "Bio Engine", "Choose an action:", builder.build()


async def _bio_inline_builder(event, extra: str) -> list:
    builder = InlinePanelBuilder()
    builder.add_row("✅ Bio ON", "action:bio_on")
    builder.add_row("⏹ Bio OFF", "action:bio_off")
    builder.add_row("👁 Show State", "action:bio_show")
    builder.add_row("📝 Set Template", "input:bio:template")
    builder.add_row("💬 Set Text", "input:bio:text")
    builder.add_row("💭 Set Mood", "input:bio:mood")
    builder.add_row("❓ Help", "action:bio_help")
    return [render("Bio Engine", "Choose an action:", builder.build())]


def register(client, owner_id: int, tz_str: str):

    register_panel("bio", _bio_panel_handler)
    register_inline_builder("bio", _bio_inline_builder)
    register_action("bio_on", _bio_on_action)
    register_action("bio_off", _bio_off_action)
    register_action("bio_show", _bio_show_action)
    register_action("bio_help", _bio_help_action)
    register_input("bio", "template", {
        "handler": _bio_template_input_handler,
        "prompt": "**Bio Template**\n\nEnter the new template (use {time}, {mood}, {text}):\n\n_Reply with the template below._",
    })
    register_input("bio", "text", {
        "handler": _bio_text_input_handler,
        "prompt": "**Bio Text**\n\nEnter the new {text} value:\n\n_Reply with the text below._",
    })
    register_input("bio", "mood", {
        "handler": _bio_mood_input_handler,
        "prompt": "**Bio Mood**\n\nEnter the new {mood} value:\n\n_Reply with the mood below._",
    })

    @client.on(events.NewMessage(outgoing=True, pattern=r"^\.bio(?:\s+(.+))?$"))
    async def bio_cmd(event):
        if not is_owner(event, owner_id):
            return
        arg = (event.pattern_match.group(1) or "").strip()

        if not arg:
            helper = get_client()
            if helper is None:
                await event.edit("⚠️ Inline mode requires the helper bot (BOT_TOKEN).")
                return
            try:
                await event.delete()
                await send_inline_panel(client, event.chat_id, "bio")
            except Exception as exc:
                logger.warning("bio inline send failed: %s", exc)
            return

        parts = arg.split(None, 1)
        sub = parts[0].lower()
        rest = parts[1] if len(parts) > 1 else ""

        if sub == "help":
            await event.edit(bio_service._HELP)
        elif sub == "template":
            if not rest:
                await event.edit("⚠️ Usage: `.bio template <template>`")
                return
            result = await bio_service.do_template(owner_id, rest)
            await event.edit(result)
        elif sub == "text":
            if not rest:
                await event.edit("⚠️ Usage: `.bio text <text>`")
                return
            result = await bio_service.do_text(owner_id, rest)
            await event.edit(result)
        elif sub == "mood":
            if not rest:
                await event.edit("⚠️ Usage: `.bio mood <mood>`")
                return
            result = await bio_service.do_mood(owner_id, rest)
            await event.edit(result)
        elif sub == "on":
            result = await bio_service.do_on(client, owner_id, tz_str)
            await event.edit(result)
        elif sub == "off":
            result = await bio_service.do_off(owner_id)
            await event.edit(result)
        elif sub == "show":
            result = await bio_service.do_show(owner_id, tz_str)
            await event.edit(result)
        else:
            await event.edit("⚠️ Unknown bio command. Try `.bio help`")
