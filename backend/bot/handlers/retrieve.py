"""
.preview <code> / .retrieve <code> / .r <code> — Show stored metadata for a saved item.
.send <code>                                 — Forward the saved asset into the current chat.

Inline Mode:
  - .preview (no code) → inline panel with input prompt for save code.
  - .send (no code) → inline panel with input prompt for save code.
  - .preview <code> / .send <code> still work as edit-in-place (backward compat).

All three preview aliases share one execution path. No duplicated logic.
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


def _format_preview(row: dict) -> str:
    size_str = f"{row['file_size'] / 1024:.1f} KB" if row.get("file_size") else "—"
    tags = " ".join(row.get("tags") or [])
    code = row.get("short_code") or row.get("save_code") or "—"
    return (
        f"**Save Code:** `{code}`\n"
        f"**Type:** {row.get('save_type', '—').title()}\n"
        f"**Media:** {row.get('media_type', '—')}\n"
        f"**MIME:** `{row.get('mime_type') or '—'}`\n"
        f"**Size:** {size_str}\n"
        f"**Sender:** {row.get('sender_name') or '—'}\n"
        f"**Origin Chat:** `{row.get('origin_chat_id')}`\n"
        f"**Origin Msg:** `{row.get('origin_msg_id')}`\n"
        f"**Saved At:** {str(row.get('created_at', '—'))[:19]}\n"
        f"**Tags:** {tags or '—'}"
    )


async def _do_preview(self_client, owner_id: int, save_code: str) -> str:
    save_code = save_code.upper().strip()
    t0 = asyncio.get_event_loop().time()
    try:
        row = db_client.query_save(save_code)
        record_event("database", "query_save", (asyncio.get_event_loop().time() - t0) * 1000, "SUCCESS")
    except Exception as exc:
        logger.error("preview db error: %s", exc)
        record_event("database", "query_save", 0, "ERROR", str(exc))
        return f"❌ DB error: {exc}"
    if not row:
        return f"❌ No item found for `{save_code}`"
    await db_client.log(owner_id, "INFO", f"Preview {save_code}", {"save_code": save_code})
    return _format_preview(row)


async def _do_send(self_client, owner_id: int, save_code: str, target_chat: int) -> str:
    save_code = save_code.upper().strip()
    t0 = asyncio.get_event_loop().time()
    try:
        row = db_client.query_save(save_code)
        record_event("database", "query_save", (asyncio.get_event_loop().time() - t0) * 1000, "SUCCESS")
    except Exception as exc:
        logger.error("send db error: %s", exc)
        record_event("database", "query_save", 0, "ERROR", str(exc))
        return f"❌ DB error: {exc}"
    if not row:
        return f"❌ No item found for `{save_code}`"

    saved_chat_id = row.get("saved_chat_id")
    saved_msg_id = row.get("saved_msg_id")
    if not saved_chat_id or not saved_msg_id:
        return "❌ Saved location data is missing for this entry."

    t1 = asyncio.get_event_loop().time()
    try:
        await self_client.forward_messages(target_chat, saved_msg_id, saved_chat_id)
        record_event("retrieve", "forward_messages", (asyncio.get_event_loop().time() - t1) * 1000, "SUCCESS")
    except Exception as exc:
        logger.error("send forward failed: %s", exc)
        record_event("retrieve", "forward_messages", 0, "ERROR", str(exc))
        return f"❌ Forward failed: {exc}"

    await db_client.log(owner_id, "INFO", f"Sent {save_code} to {target_chat}", {
        "save_code": save_code,
        "target_chat": target_chat,
    })
    return f"✅ Sent `{save_code}` to this chat."


async def _preview_input_handler(text, chat_id, msg_id, inline_chat_id, inline_msg_id):
    from backend.helper.inline_engine import _self_client, _owner_id
    result = await _do_preview(_self_client, _owner_id, text)
    builder = InlinePanelBuilder()
    builder.add_row("Back", "panel:help:back")
    builder.add_row("Close", "panel:help:close")
    helper = get_client()
    if helper and inline_chat_id and inline_msg_id:
        try:
            await helper.edit_message(inline_chat_id, inline_msg_id, result, buttons=builder.build())
            await helper.delete_messages(chat_id, [msg_id])
        except Exception as exc:
            logger.warning("preview inline edit failed: %s", exc)


async def _send_input_handler(text, chat_id, msg_id, inline_chat_id, inline_msg_id):
    from backend.helper.inline_engine import _self_client, _owner_id
    result = await _do_send(_self_client, _owner_id, text, chat_id)
    builder = InlinePanelBuilder()
    builder.add_row("Back", "panel:help:back")
    builder.add_row("Close", "panel:help:close")
    helper = get_client()
    if helper and inline_chat_id and inline_msg_id:
        try:
            await helper.edit_message(inline_chat_id, inline_msg_id, result, buttons=builder.build())
            await helper.delete_messages(chat_id, [msg_id])
        except Exception as exc:
            logger.warning("send inline edit failed: %s", exc)


async def _preview_inline_builder(event, extra: str) -> list:
    from telethon.tl import types
    text = "**Preview**\n\nEnter a save code to preview:"
    builder = InlinePanelBuilder()
    builder.add_row("Enter Code", "input:preview:code")
    builder.add_row("Close", "panel:help:close")
    buttons = builder.build()
    msg = types.InputBotInlineMessageText(
        message=text,
        reply_markup=types.ReplyInlineMarkup(rows=buttons) if buttons else None,
    )
    result = types.InputBotInlineResult(
        id="0",
        type="article",
        title="Preview Saved Item",
        send_message=msg,
    )
    return [result]


async def _send_inline_builder(event, extra: str) -> list:
    from telethon.tl import types
    text = "**Send**\n\nEnter a save code to forward to this chat:"
    builder = InlinePanelBuilder()
    builder.add_row("Enter Code", "input:send:code")
    builder.add_row("Close", "panel:help:close")
    buttons = builder.build()
    msg = types.InputBotInlineMessageText(
        message=text,
        reply_markup=types.ReplyInlineMarkup(rows=buttons) if buttons else None,
    )
    result = types.InputBotInlineResult(
        id="0",
        type="article",
        title="Send Saved Item",
        send_message=msg,
    )
    return [result]


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
        result = await _do_preview(client, owner_id, save_code)
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
        result = await _do_send(client, owner_id, save_code, event.chat_id)
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
