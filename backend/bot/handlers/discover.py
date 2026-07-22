"""
.list [n]       — Show recent saved items (newest first, default 10).
.find <text>    — Search saved items by short code, filename, caption, or mime type.
.list           — Inline panel: show recent saves or enter search.
.find           — Inline panel: input prompt for search text.

Both use indexed DB queries. Never scan the entire table.

Inline Mode:
  - .list (no args) → inline panel showing recent saves.
  - .list <n> still works as edit-in-place (backward compat).
  - .find (no args) → inline panel with input prompt for search text.
  - .find <text> still works as edit-in-place (backward compat).
"""
import asyncio
import logging
from datetime import datetime
from telethon import events
from backend.bot.handlers.guard import is_owner
from backend.db import client as db_client
from backend.bio.engine import _get_tz
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

_MEDIA_ICON = {
    "Photo": "📷",
    "Video": "🎬",
    "Animation": "🎞",
    "Audio": "🎵",
    "Voice": "🎤",
    "Sticker": "🏷",
    "Document": "📄",
    "Unknown": "📦",
}


def _icon(media_type: str | None) -> str:
    return _MEDIA_ICON.get(media_type or "Unknown", "📦")


def _format_date(iso_str: str | None, tz_str: str) -> str:
    if not iso_str:
        return "—"
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        tz = _get_tz(tz_str)
        local_dt = dt.astimezone(tz) if dt.tzinfo else dt
        return local_dt.strftime("%d %b")
    except Exception:
        return str(iso_str)[:10]


def _format_list_entry(row: dict, tz_str: str) -> str:
    code = row.get("short_code") or row.get("save_code") or "—"
    icon = _icon(row.get("media_type"))
    name = row.get("file_name") or "—"
    mtype = row.get("media_type") or "Unknown"
    date_str = _format_date(row.get("created_at"), tz_str)
    return f"{icon} `{code}`\n   {name}\n   {mtype} · {date_str}"


def _format_find_entry(row: dict, tz_str: str) -> str:
    code = row.get("short_code") or row.get("save_code") or "—"
    icon = _icon(row.get("media_type"))
    name = row.get("file_name") or "—"
    mtype = row.get("media_type") or "Unknown"
    date_str = _format_date(row.get("created_at"), tz_str)
    return f"{icon} `{code}` — {name}\n   {mtype} · {date_str}"


async def _do_list(owner_id: int, limit: int, tz_str: str) -> str:
    t0 = asyncio.get_event_loop().time()
    try:
        items = db_client.list_recent_saves(owner_id, limit=limit)
        record_event("database", "list_recent_saves", (asyncio.get_event_loop().time() - t0) * 1000, "SUCCESS")
    except Exception as exc:
        logger.error("list db error: %s", exc)
        record_event("database", "list_recent_saves", 0, "ERROR", str(exc))
        return f"❌ DB error: {exc}"
    if not items:
        return "📭 No saved items yet."
    lines = [f"📋 **Recent Saves** ({len(items)})", ""]
    lines.extend(_format_list_entry(r, tz_str) for r in items)
    return "\n".join(lines)


async def _do_find(owner_id: int, query: str, tz_str: str) -> str:
    t0 = asyncio.get_event_loop().time()
    try:
        items = db_client.search_saves(owner_id, query, limit=20)
        record_event("database", "search_saves", (asyncio.get_event_loop().time() - t0) * 1000, "SUCCESS")
    except Exception as exc:
        logger.error("find db error: %s", exc)
        record_event("database", "search_saves", 0, "ERROR", str(exc))
        return f"❌ DB error: {exc}"
    if not items:
        return f"🔍 No matches for `{query}`"
    lines = [f"🔍 **Results** for `{query}` ({len(items)})", ""]
    lines.extend(_format_find_entry(r, tz_str) for r in items)
    return "\n".join(lines)


async def _list_inline_builder(event, extra: str) -> list:
    from telethon.tl import types
    from backend.helper.inline_engine import _owner_id
    from backend.bot.handlers.misc import _resolve_tz
    tz_str = _resolve_tz()
    limit = 10
    if extra and extra.isdigit():
        limit = min(int(extra), 50)
    text = await _do_list(_owner_id, limit, tz_str)
    builder = InlinePanelBuilder()
    builder.add_row("Close", "panel:help:close")
    buttons = builder.build()
    msg = types.InputBotInlineMessageTextAuto(
        message=text,
        reply_markup=types.ReplyInlineMarkup(rows=buttons) if buttons else None,
    )
    result = types.InputBotInlineResult(
        id="0",
        type="article",
        title="Recent Saves",
        send_message=msg,
    )
    return [result]


async def _find_input_handler(text, chat_id, msg_id, inline_chat_id, inline_msg_id):
    from backend.helper.inline_engine import _owner_id
    from backend.bot.handlers.misc import _resolve_tz
    result = await _do_find(_owner_id, text, _resolve_tz())
    builder = InlinePanelBuilder()
    builder.add_row("Close", "panel:help:close")
    helper = get_client()
    if helper and inline_chat_id and inline_msg_id:
        try:
            await helper.edit_message(inline_chat_id, inline_msg_id, result, buttons=builder.build())
            await helper.delete_messages(chat_id, [msg_id])
        except Exception as exc:
            logger.warning("find inline edit failed: %s", exc)


async def _find_inline_builder(event, extra: str) -> list:
    from telethon.tl import types
    text = "**Search**\n\nEnter search text:"
    builder = InlinePanelBuilder()
    builder.add_row("Enter Search Text", "input:find:query")
    builder.add_row("Close", "panel:help:close")
    buttons = builder.build()
    msg = types.InputBotInlineMessageTextAuto(
        message=text,
        reply_markup=types.ReplyInlineMarkup(rows=buttons) if buttons else None,
    )
    result = types.InputBotInlineResult(
        id="0",
        type="article",
        title="Search Saves",
        send_message=msg,
    )
    return [result]


def register(client, owner_id: int, tz_str: str):

    register_panel("list", _list_inline_builder)
    register_panel("find", _find_inline_builder)
    register_inline_builder("list", _list_inline_builder)
    register_inline_builder("find", _find_inline_builder)
    register_input("find", "query", {
        "handler": _find_input_handler,
        "prompt": "**Search**\n\nEnter search text (filename, caption, code, or MIME):\n\n_Reply below._",
    })

    @client.on(events.NewMessage(outgoing=True, pattern=r"^\.list(?:\s+(\d+))?$"))
    async def list_cmd(event):
        if not is_owner(event, owner_id):
            return
        n_str = event.pattern_match.group(1)
        limit = int(n_str) if n_str else 10
        if limit < 1 or limit > 50:
            await event.edit("⚠️ Use a number between 1 and 50.")
            return
        result = await _do_list(owner_id, limit, tz_str)
        await event.edit(result)

    @client.on(events.NewMessage(outgoing=True, pattern=r"^\.find\s+(.+)$"))
    async def find_cmd(event):
        if not is_owner(event, owner_id):
            return
        query = event.pattern_match.group(1).strip()
        result = await _do_find(owner_id, query, tz_str)
        await event.edit(result)

    @client.on(events.NewMessage(outgoing=True, pattern=r"^\.find$"))
    async def find_panel(event):
        if not is_owner(event, owner_id):
            return
        helper = get_client()
        if helper is None:
            await event.edit("⚠️ Inline mode requires the helper bot (BOT_TOKEN).")
            return
        try:
            await event.delete()
            await send_inline_panel(client, event.chat_id, "find")
        except Exception as exc:
            logger.warning("find inline send failed: %s", exc)
