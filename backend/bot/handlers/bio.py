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
    render_edit,
    to_edit_buttons,
)
from backend.helper.client import get_client

logger = logging.getLogger(__name__)


async def _bio_on_action(event, extra: str) -> tuple:
    from backend.helper.inline_engine import _self_client, _owner_id
    from backend.bot.handlers.misc import _resolve_tz
    result = await bio_service.do_on(_self_client, _owner_id, _resolve_tz())
    builder = InlinePanelBuilder()
    builder.add_row("Back", "panel:bio")
    builder.add_row("Close", "panel:help:close")
    return result, to_edit_buttons(builder.build())


async def _bio_off_action(event, extra: str) -> tuple:
    from backend.helper.inline_engine import _owner_id
    result = await bio_service.do_off(_owner_id)
    builder = InlinePanelBuilder()
    builder.add_row("Back", "panel:bio")
    builder.add_row("Close", "panel:help:close")
    return result, to_edit_buttons(builder.build())


async def _bio_show_action(event, extra: str) -> tuple:
    from backend.helper.inline_engine import _owner_id
    from backend.bot.handlers.misc import _resolve_tz
    result = await bio_service.do_show(_owner_id, _resolve_tz())
    builder = InlinePanelBuilder()
    builder.add_row("Back", "panel:bio")
    builder.add_row("Close", "panel:help:close")
    return result, to_edit_buttons(builder.build())


async def _bio_help_action(event, extra: str) -> tuple:
    builder = InlinePanelBuilder()
    builder.add_row("Back", "panel:bio")
    builder.add_row("Close", "panel:help:close")
    return bio_service._HELP, to_edit_buttons(builder.build())


async def _bio_template_input_handler(text, chat_id, msg_id, inline_chat_id, inline_msg_id):
    from backend.helper.inline_engine import _owner_id
    result = await bio_service.do_template(_owner_id, text)
    builder = InlinePanelBuilder()
    builder.add_row("Back", "panel:bio")
    builder.add_row("Close", "panel:help:close")
    helper = get_client()
    if helper and inline_chat_id and inline_msg_id:
        try:
            await helper.edit_message(inline_chat_id, inline_msg_id, result, buttons=to_edit_buttons(builder.build()))
            await helper.delete_messages(chat_id, [msg_id])
        except Exception as exc:
            logger.warning("bio template inline edit failed: %s", exc)


async def _bio_text_input_handler(text, chat_id, msg_id, inline_chat_id, inline_msg_id):
    from backend.helper.inline_engine import _owner_id
    result = await bio_service.do_text(_owner_id, text)
    builder = InlinePanelBuilder()
    builder.add_row("Back", "panel:bio")
    builder.add_row("Close", "panel:help:close")
    helper = get_client()
    if helper and inline_chat_id and inline_msg_id:
        try:
            await helper.edit_message(inline_chat_id, inline_msg_id, result, buttons=to_edit_buttons(builder.build()))
            await helper.delete_messages(chat_id, [msg_id])
        except Exception as exc:
            logger.warning("bio text inline edit failed: %s", exc)


async def _bio_mood_input_handler(text, chat_id, msg_id, inline_chat_id, inline_msg_id):
    from backend.helper.inline_engine import _owner_id
    result = await bio_service.do_mood(_owner_id, text)
    builder = InlinePanelBuilder()
    builder.add_row("Back", "panel:bio")
    builder.add_row("Close", "panel:help:close")
    helper = get_client()
    if helper and inline_chat_id and inline_msg_id:
        try:
            await helper.edit_message(inline_chat_id, inline_msg_id, result, buttons=to_edit_buttons(builder.build()))
            await helper.delete_messages(chat_id, [msg_id])
        except Exception as exc:
            logger.warning("bio mood inline edit failed: %s", exc)


async def _bio_panel_handler(event, extra: str) -> None:
    builder = InlinePanelBuilder()
    builder.add_row("✅ Bio ON", "action:bio_on")
    builder.add_row("⏹ Bio OFF", "action:bio_off")
    builder.add_row("👁 Show State", "action:bio_show")
    builder.add_row("📝 Set Template", "input:bio:template")
    builder.add_row("💬 Set Text", "input:bio:text")
    builder.add_row("💭 Set Mood", "input:bio:mood")
    builder.add_row("❓ Help", "action:bio_help")
    builder.add_row("Close", "panel:help:close")
    text, buttons = render_edit("Bio Engine", "Choose an action:", builder.build())
    await event.edit(text, buttons=buttons)


async def _bio_inline_builder(event, extra: str) -> list:
    builder = InlinePanelBuilder()
    builder.add_row("✅ Bio ON", "action:bio_on")
    builder.add_row("⏹ Bio OFF", "action:bio_off")
    builder.add_row("👁 Show State", "action:bio_show")
    builder.add_row("📝 Set Template", "input:bio:template")
    builder.add_row("💬 Set Text", "input:bio:text")
    builder.add_row("💭 Set Mood", "input:bio:mood")
    builder.add_row("❓ Help", "action:bio_help")
    builder.add_row("Close", "panel:help:close")
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
        "prompt": "**Set Template**\n\nEnter the new bio template:\n\n_Tokens: {time} {mood} {text}_\n\n_Reply below._",
    })
    register_input("bio", "text", {
        "handler": _bio_text_input_handler,
        "prompt": "**Set Text**\n\nEnter the {text} token value:\n\n_Reply below._",
    })
    register_input("bio", "mood", {
        "handler": _bio_mood_input_handler,
        "prompt": "**Set Mood**\n\nEnter the {mood} token value:\n\n_Reply below._",
    })

    @client.on(events.NewMessage(outgoing=True, pattern=r"^\.bio(?:\s+(.+))?$"))
    async def bio_cmd(event):
        if not is_owner(event, owner_id):
            return

        arg = (event.pattern_match.group(1) or "").strip()

        if not arg:
            helper = get_client()
            if helper is None:
                await event.edit(bio_service._HELP)
                return
            try:
                await event.delete()
                await send_inline_panel(client, event.chat_id, "bio")
            except Exception as exc:
                logger.warning("bio inline send failed: %s", exc)
            return

        try:
            state = db_client.get_or_create_bio_state(owner_id)
        except Exception as exc:
            logger.error("bio db init failed: %s", exc)
            await event.edit(f"❌ DB error: {exc}")
            return

        if arg in ("help", "template") and " " not in arg:
            if arg == "template":
                await event.edit(
                    f"**Current template:**\n`{state.get('template') or '🕒 {time} | 💭 {mood}'}`\n\n"
                    "To change: `.bio template <new template>`"
                )
            else:
                await event.edit(bio_service._HELP)
            return

        if arg.startswith("template "):
            result = await bio_service.do_template(owner_id, arg[9:].strip())
            await event.edit(result)
        elif arg.startswith("text "):
            result = await bio_service.do_text(owner_id, arg[5:].strip())
            await event.edit(result)
        elif arg.startswith("mood "):
            result = await bio_service.do_mood(owner_id, arg[5:].strip())
            await event.edit(result)
        elif arg == "on":
            result = await bio_service.do_on(client, owner_id, tz_str)
            await event.edit(result)
        elif arg == "off":
            result = await bio_service.do_off(owner_id)
            await event.edit(result)
        elif arg == "show":
            result = await bio_service.do_show(owner_id, tz_str)
            await event.edit(result)
        else:
            await event.edit("⚠️ Unknown bio command. Try `.bio help`")