"""
.del <n>         — Delete the last n outgoing messages in this chat.
.del id <msgid>  — Delete all messages from <msgid> forward in this chat.
.del <code>      — Delete a saved item: Telegram message + DB row.
.del             — Inline panel: choose deletion mode.

Edit-first policy: error feedback edits the trigger message.
Successful deletion silently removes all targeted messages (including the command).
"""
import asyncio
import logging
from telethon import events
from backend.bot.handlers.guard import is_owner
from backend.db import client as db_client
from backend.diagnostics import record_event
from backend.helper import (
    InlinePanelBuilder,
    register_panel,
    register_inline_builder,
    register_input,
    send_inline_panel,
)
from backend.helper.client import get_client

logger = logging.getLogger(__name__)

_BATCH = 100


async def _do_del_n(client, chat_id, n: int) -> str:
    if n < 1 or n > 500:
        return "⚠️ n must be between 1 and 500."
    t0 = asyncio.get_event_loop().time()
    try:
        msg_ids = []
        async for msg in client.iter_messages(chat_id, limit=n + 5, from_user="me"):
            msg_ids.append(msg.id)
            if len(msg_ids) >= n:
                break
        if msg_ids:
            await client.delete_messages(chat_id, msg_ids[:n])
        record_event("delete", "del n", (asyncio.get_event_loop().time() - t0) * 1000, "SUCCESS")
        return f"🗑 Deleted `{len(msg_ids[:n])}` messages."
    except Exception as exc:
        logger.error("del n failed: %s", exc)
        record_event("delete", "del n", 0, "ERROR", str(exc))
        return f"❌ Delete failed: {exc}"


async def _do_del_id(client, chat_id, start_id: int) -> str:
    t0 = asyncio.get_event_loop().time()
    try:
        msg_ids = []
        async for msg in client.iter_messages(chat_id, min_id=start_id - 1):
            msg_ids.append(msg.id)
            if len(msg_ids) >= _BATCH:
                await client.delete_messages(chat_id, msg_ids)
                msg_ids = []
        if msg_ids:
            await client.delete_messages(chat_id, msg_ids)
        record_event("delete", "del id", (asyncio.get_event_loop().time() - t0) * 1000, "SUCCESS")
        return f"🗑 Deleted messages from ID `{start_id}` forward."
    except Exception as exc:
        logger.error("del id failed: %s", exc)
        record_event("delete", "del id", 0, "ERROR", str(exc))
        return f"❌ Delete failed: {exc}"


async def _do_del_code(client, owner_id: int, code: str) -> str:
    code = code.upper().strip()
    t0 = asyncio.get_event_loop().time()
    try:
        row = db_client.query_save(code)
        record_event("database", "query_save", (asyncio.get_event_loop().time() - t0) * 1000, "SUCCESS")
    except Exception as exc:
        logger.error("del save_code DB query failed: %s", exc)
        record_event("database", "query_save", 0, "ERROR", str(exc))
        return f"❌ DB error: {exc}"
    if not row:
        return f"❌ No saved item found for `{code}`"

    saved_chat_id = row.get("saved_chat_id")
    saved_msg_id = row.get("saved_msg_id")
    display = row.get("short_code") or row.get("save_code") or code

    tg_deleted = False
    tg_error = None
    if saved_chat_id and saved_msg_id:
        try:
            await client.delete_messages(saved_chat_id, [saved_msg_id])
            tg_deleted = True
        except Exception as exc:
            tg_error = exc
            logger.warning("del %s: Telegram deletion failed: %s", code, exc)
    else:
        tg_deleted = True

    db_deleted = False
    db_error = None
    try:
        removed = db_client.delete_save_row(owner_id, code)
        db_deleted = removed is not None
    except Exception as exc:
        db_error = exc
        logger.error("del %s: DB deletion failed: %s", code, exc)

    await db_client.log(
        owner_id,
        "INFO" if (tg_deleted and db_deleted) else "ERROR",
        f"Delete {code}: tg={'ok' if tg_deleted else 'fail'}, db={'ok' if db_deleted else 'fail'}",
        {"save_code": code, "tg_error": str(tg_error) if tg_error else None},
    )

    if tg_deleted and db_deleted:
        return f"🗑 Deleted `{display}`"
    elif tg_deleted and not db_deleted:
        return f"⚠️ `{display}`: Telegram message deleted, but DB row removal failed: {db_error}"
    elif not tg_deleted and db_deleted:
        if tg_error:
            return f"⚠️ `{display}`: DB row deleted, but Telegram message deletion failed: {tg_error}"
        return f"🗑 Deleted `{display}` (Telegram message was already missing)"
    return f"❌ `{display}`: Both Telegram and DB deletion failed. TG: {tg_error}, DB: {db_error}"


async def _del_n_input_handler(text, chat_id, msg_id, inline_chat_id, inline_msg_id):
    from backend.helper.inline_engine import _self_client
    text = text.strip()
    if not text.isdigit():
        result = "⚠️ Please enter a number between 1 and 500."
    else:
        result = await _do_del_n(_self_client, chat_id, int(text))
    builder = InlinePanelBuilder()
    builder.add_row("Close", "panel:help:close")
    helper = get_client()
    if helper and inline_chat_id and inline_msg_id:
        try:
            await helper.edit_message(inline_chat_id, inline_msg_id, result, buttons=builder.build())
            await helper.delete_messages(chat_id, [msg_id])
        except Exception as exc:
            logger.warning("del n inline edit failed: %s", exc)


async def _del_id_input_handler(text, chat_id, msg_id, inline_chat_id, inline_msg_id):
    from backend.helper.inline_engine import _self_client
    text = text.strip()
    if not text.isdigit():
        result = "⚠️ Please enter a valid message ID (number)."
    else:
        result = await _do_del_id(_self_client, chat_id, int(text))
    builder = InlinePanelBuilder()
    builder.add_row("Close", "panel:help:close")
    helper = get_client()
    if helper and inline_chat_id and inline_msg_id:
        try:
            await helper.edit_message(inline_chat_id, inline_msg_id, result, buttons=builder.build())
            await helper.delete_messages(chat_id, [msg_id])
        except Exception as exc:
            logger.warning("del id inline edit failed: %s", exc)


async def _del_code_input_handler(text, chat_id, msg_id, inline_chat_id, inline_msg_id):
    from backend.helper.inline_engine import _self_client, _owner_id
    result = await _do_del_code(_self_client, _owner_id, text)
    builder = InlinePanelBuilder()
    builder.add_row("Close", "panel:help:close")
    helper = get_client()
    if helper and inline_chat_id and inline_msg_id:
        try:
            await helper.edit_message(inline_chat_id, inline_msg_id, result, buttons=builder.build())
            await helper.delete_messages(chat_id, [msg_id])
        except Exception as exc:
            logger.warning("del code inline edit failed: %s", exc)


async def _del_inline_builder(event, extra: str) -> list:
    from telethon.tl import types
    text = "**Delete**\n\nChoose a deletion mode:"
    builder = InlinePanelBuilder()
    builder.add_row("Delete last N messages", "input:del:n")
    builder.add_row("Delete from Msg ID", "input:del:id")
    builder.add_row("Delete saved item by code", "input:del:code")
    builder.add_row("Close", "panel:help:close")
    buttons = builder.build()
    msg = types.InputBotInlineMessageText(
        message=text,
        reply_markup=types.ReplyInlineMarkup(rows=buttons) if buttons else None,
    )
    result = types.InputBotInlineResult(
        id="0",
        type="article",
        title="Delete Messages",
        send_message=msg,
    )
    return [result]


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
            result = await _do_del_id(client, event.chat_id, int(rest))

        elif arg.isdigit():
            n = int(arg)
            if n < 1 or n > 500:
                await event.edit("⚠️ n must be between 1 and 500.")
                return
            await event.delete()
            result = await _do_del_n(client, event.chat_id, n)

        else:
            result = await _do_del_code(client, owner_id, arg)
            await event.edit(result)
