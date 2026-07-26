"""
.preview <code> / .retrieve <code> / .r <code> — Show stored metadata.
.send <code>                                 — Forward the saved asset.

Business logic lives in backend.services.retrieve_service.
"""
import logging
from telethon import events
from backend.bot.handlers.guard import is_owner
from backend.services import retrieve_service
from backend.helper import (
    InlinePanelBuilder,
    register_panel,
    register_inline_builder,
    register_input,
    send_inline_panel,
    render,
    render_edit,
    to_edit_buttons,
)
from backend.helper.client import get_client

logger = logging.getLogger(__name__)


async def _preview_input_handler(text, chat_id, msg_id, inline_chat_id, inline_msg_id):
    from backend.helper.inline_engine import _self_client, _owner_id
    result = await retrieve_service.do_preview(_self_client, _owner_id, text)
    builder = InlinePanelBuilder()
    builder.add_row("Back", "panel:help:back")
    builder.add_row("Close", "panel:help:close")
    helper = get_client()
    if helper and inline_chat_id and inline_msg_id:
        try:
            await helper.edit_message(inline_chat_id, inline_msg_id, result, buttons=to_edit_buttons(builder.build()))
            await helper.delete_messages(chat_id, [msg_id])
        except Exception as exc:
            logger.warning("preview inline edit failed: %s", exc)


async def _send_input_handler(text, chat_id, msg_id, inline_chat_id, inline_msg_id):
    from backend.helper.inline_engine import _self_client, _owner_id
    result = await retrieve_service.do_send(_self_client, _owner_id, text, chat_id)
    builder = InlinePanelBuilder()
    builder.add_row("Back", "panel:help:back")
    builder.add_row("Close", "panel:help:close")
    helper = get_client()
    if helper and inline_chat_id and inline_msg_id:
        try:
            await helper.edit_message(inline_chat_id, inline_msg_id, result, buttons=to_edit_buttons(builder.build()))
            await helper.delete_messages(chat_id, [msg_id])
        except Exception as exc:
            logger.warning("send inline edit failed: %s", exc)


async def _preview_inline_builder(event, extra: str) -> list:
    builder = InlinePanelBuilder()
    builder.add_row("Enter Code", "input:preview:code")
    builder.add_row("Disable Auto Close", "timer:toggle")
    builder.add_row("Close", "panel:help:close")
    return [render("Preview", "Auto Close\n120s\n\nEnter a save code to preview:", builder.build())]


async def _send_inline_builder(event, extra: str) -> list:
    builder = InlinePanelBuilder()
    builder.add_row("Enter Code", "input:send:code")
    builder.add_row("Disable Auto Close", "timer:toggle")
    builder.add_row("Close", "panel:help:close")
    return [render("Send", "Auto Close\n120s\n\nEnter a save code to forward to this chat:", builder.build())]


def register(client, owner_id: int):
    register_panel("preview", _preview_inline_builder)
    register_panel("send", _send_inline_builder)
    register_inline_builder("preview", _preview_inline_builder)
    register_inline_builder("send", _send_inline_builder)
    register_input("preview", "code", {
        "handler": _preview_input_handler,
        "prompt": "**Preview**\n\nEnter save code (e.g. S0001):\n\n_Reply with the code below._",
    })
    register_input("send", "code", {
        "handler": _send_input_handler,
        "prompt": "**Send**\n\nEnter save code (e.g. S0001):\n\n_Reply with the code below._",
    })

    @client.on(events.NewMessage(outgoing=True, pattern=r"^\.(?:preview|retrieve|r)\s+(\S+)$"))
    async def preview(event):
        if not is_owner(event, owner_id):
            return
        save_code = event.pattern_match.group(1).upper()
        result = await retrieve_service.do_preview(client, owner_id, save_code)
        await event.edit(result)

    @client.on(events.NewMessage(outgoing=True, pattern=r"^\.(?:preview|retrieve|r)$"))
    async def preview_panel(event):
        if not is_owner(event, owner_id):
            return
        helper = get_client()
        if helper is None:
            await event.edit("⚠️ Inline mode requires the helper bot (BOT_TOKEN).")
            return
        try:
            await event.delete()
            await send_inline_panel(client, event.chat_id, "preview")
        except Exception as exc:
            logger.warning("preview inline send failed: %s", exc)

    @client.on(events.NewMessage(outgoing=True, pattern=r"^\.send\s+(\S+)$"))
    async def send_cmd(event):
        if not is_owner(event, owner_id):
            return
        save_code = event.pattern_match.group(1).upper()
        result = await retrieve_service.do_send(client, owner_id, save_code, event.chat_id)
        if result.startswith("✅"):
            await event.delete()
        else:
            await event.edit(result)

    @client.on(events.NewMessage(outgoing=True, pattern=r"^\.send$"))
    async def send_panel(event):
        if not is_owner(event, owner_id):
            return
        helper = get_client()
        if helper is None:
            await event.edit("⚠️ Inline mode requires the helper bot (BOT_TOKEN).")
            return
        try:
            await event.delete()
            await send_inline_panel(client, event.chat_id, "send")
        except Exception as exc:
            logger.warning("send inline send failed: %s", exc)
