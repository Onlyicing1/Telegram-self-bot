"""
.del <n>         — Delete the last n outgoing messages in this chat.
.del id <msgid>  — Delete all messages from <msgid> forward in this chat.
.del <code>      — Delete a saved item: Telegram message + DB row.
.del             — Inline panel: choose deletion mode.

Business logic lives in backend.services.delete_service.
"""
import logging
from telethon import events
from backend.bot.handlers.guard import is_owner
from backend.services import delete_service
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


async def _del_n_input_handler(text, chat_id, msg_id, inline_chat_id, inline_msg_id):
    from backend.helper.inline_engine import _self_client
    text = text.strip()
    if not text.isdigit():
        result = "⚠️ Please enter a number between 1 and 500."
    else:
        result = await delete_service.do_del_n(_self_client, chat_id, int(text))
    builder = InlinePanelBuilder()
    builder.add_row("Close", "panel:help:close")
    helper = get_client()
    if helper and inline_chat_id and inline_msg_id:
        try:
            await helper.edit_message(inline_chat_id, inline_msg_id, result, buttons=to_edit_buttons(builder.build()))
            await helper.delete_messages(chat_id, [msg_id])
        except Exception as exc:
            logger.warning("del n inline edit failed: %s", exc)


async def _del_id_input_handler(text, chat_id, msg_id, inline_chat_id, inline_msg_id):
    from backend.helper.inline_engine import _self_client
    text = text.strip()
    if not text.isdigit():
        result = "⚠️ Please enter a valid message ID (number)."
    else:
        result = await delete_service.do_del_id(_self_client, chat_id, int(text))
    builder = InlinePanelBuilder()
    builder.add_row("Close", "panel:help:close")
    helper = get_client()
    if helper and inline_chat_id and inline_msg_id:
        try:
            await helper.edit_message(inline_chat_id, inline_msg_id, result, buttons=to_edit_buttons(builder.build()))
            await helper.delete_messages(chat_id, [msg_id])
        except Exception as exc:
            logger.warning("del id inline edit failed: %s", exc)


async def _del_code_input_handler(text, chat_id, msg_id, inline_chat_id, inline_msg_id):
    from backend.helper.inline_engine import _self_client, _owner_id
    result = await delete_service.do_del_code(_self_client, _owner_id, text)
    builder = InlinePanelBuilder()
    builder.add_row("Close", "panel:help:close")
    helper = get_client()
    if helper and inline_chat_id and inline_msg_id:
        try:
            await helper.edit_message(inline_chat_id, inline_msg_id, result, buttons=to_edit_buttons(builder.build()))
            await helper.delete_messages(chat_id, [msg_id])
        except Exception as exc:
            logger.warning("del code inline edit failed: %s", exc)


async def _del_inline_builder(event, extra: str) -> list:
    builder = InlinePanelBuilder()
    builder.add_row("Delete last N messages", "input:del:n")
    builder.add_row("Delete from Msg ID", "input:del:id")
    builder.add_row("Delete saved item by code", "input:del:code")
    builder.add_row("Close", "panel:help:close")
    return [render("Delete", "Choose a deletion mode:", builder.build())]


def register(client, owner_id: int):
    register_panel("del", _del_inline_builder)
    register_inline_builder("del", _del_inline_builder)
    register_input("del", "n", {
        "handler": _del_n_input_handler,
        "prompt": "**Delete Messages**\n\nEnter the number of messages to delete (1-500):\n\n_Reply with the number below._",
    })
    register_input("del", "id", {
        "handler": _del_id_input_handler,
        "prompt": "**Delete from Message ID**\n\nEnter the starting message ID:\n\n_Reply with the ID below._",
    })
    register_input("del", "code", {
        "handler": _del_code_input_handler,
        "prompt": "**Delete Saved Item**\n\nEnter the save code (e.g. S0001):\n\n_Reply with the code below._",
    })

    @client.on(events.NewMessage(outgoing=True, pattern=r"^\.del(?:\s+(.+))?$"))
    async def del_cmd(event):
        if not is_owner(event, owner_id):
            return

        arg = (event.pattern_match.group(1) or "").strip()

        if not arg:
            helper = get_client()
            if helper is None:
                await event.edit("⚠️ Usage: `.del <n>` or `.del id <msgid>` or `.del <code>`")
                return
            try:
                await event.delete()
                await send_inline_panel(client, event.chat_id, "del")
            except Exception as exc:
                logger.warning("del inline send failed: %s", exc)
            return

        if arg.lower().startswith("id "):
            rest = arg[3:].strip()
            if not rest.isdigit():
                await event.edit("⚠️ Usage: `.del id <msgid>`")
                return
            await event.delete()
            result = await delete_service.do_del_id(client, event.chat_id, int(rest))

        elif arg.isdigit():
            n = int(arg)
            if n < 1 or n > 500:
                await event.edit("⚠️ n must be between 1 and 500.")
                return
            await event.delete()
            result = await delete_service.do_del_n(client, event.chat_id, n)

        else:
            result = await delete_service.do_del_code(client, owner_id, arg)
            await event.edit(result)