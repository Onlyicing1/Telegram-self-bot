"""
.db clean   — Remove orphan DB rows (Saved Messages message no longer exists).
.db stats   — Display database statistics (counts, types, sizes, dates, orphans).
.db vacuum  — Run orphan cleanup + index optimization. Summary only.
.db        — Inline panel: choose database action.

Inline Mode:
  - .db (no args) → inline panel with Clean/Stats/Vacuum buttons.
  - .db clean/stats/vacuum still work as edit-in-place (backward compat).
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
    register_action,
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
        return dt.strftime("%d %b %Y")
    except Exception:
        return str(iso_str)[:10]


async def _find_orphans(client, owner_id: int) -> tuple[list[int], int]:
    items = db_client.list_all_saves(owner_id)
    orphan_ids: list[int] = []
    for item in items:
        saved_chat_id = item.get("saved_chat_id")
        saved_msg_id = item.get("saved_msg_id")
        if not saved_chat_id or not saved_msg_id:
            orphan_ids.append(item.get("id"))
            continue
        try:
            msg = await client.get_messages(saved_chat_id, ids=saved_msg_id)
            if msg is None or (isinstance(msg, list) and not any(m is not None for m in msg)):
                orphan_ids.append(item.get("id"))
        except Exception:
            pass
    return orphan_ids, len(items)


async def _do_clean(client, owner_id: int) -> str:
    t0 = asyncio.get_event_loop().time()
    try:
        orphan_ids, total = await _find_orphans(client, owner_id)
        removed = db_client.cleanup_orphans(owner_id, orphan_ids)
        remaining = total - removed
        record_event("database", "clean orphans", (asyncio.get_event_loop().time() - t0) * 1000, "SUCCESS", f"{removed}/{total}")
        await db_client.log(owner_id, "INFO", f"DB clean: removed {removed} orphans", {
            "removed": removed, "remaining": remaining,
        })
        return (
            f"🧹 **Database cleanup complete**\n\n"
            f"Removed: `{removed}` orphan rows\n"
            f"Remaining: `{remaining}` items"
        )
    except Exception as exc:
        logger.error("db clean failed: %s", exc)
        record_event("database", "clean orphans", 0, "ERROR", str(exc))
        await db_client.log(owner_id, "ERROR", f"DB clean failed: {exc}", {})
        return f"❌ Cleanup error: {exc}"


async def _do_stats(owner_id: int, tz_str: str) -> str:
    t0 = asyncio.get_event_loop().time()
    try:
        stats = db_client.get_stats(owner_id)
        total = stats["total"]
        by_type = stats["by_type"]
        size_bytes = stats["size_estimate"]

        if size_bytes >= 1024 * 1024:
            size_str = f"{size_bytes / (1024 * 1024):.1f} MB"
        elif size_bytes >= 1024:
            size_str = f"{size_bytes / 1024:.1f} KB"
        else:
            size_str = f"{size_bytes} B"

        lines = [
            f"📊 **Database Statistics**\n",
            f"Total saved items: `{total}`\n",
            f"**Breakdown by type:**",
        ]
        for mt in ["Photo", "Video", "Animation", "Audio", "Voice", "Document", "Unknown"]:
            count = by_type.get(mt, 0)
            if count:
                lines.append(f"  {_icon(mt)} {mt}: `{count}`")

        lines.append(f"\n**Database size estimate:** `{size_str}`")
        lines.append(f"**Oldest save:** {_format_date(stats['oldest'], tz_str)}")
        lines.append(f"**Newest save:** {_format_date(stats['newest'], tz_str)}")

        await db_client.log(owner_id, "INFO", f"DB stats: {total} items", stats)
        record_event("database", "stats", (asyncio.get_event_loop().time() - t0) * 1000, "SUCCESS")
        return "\n".join(lines)
    except Exception as exc:
        logger.error("db stats failed: %s", exc)
        record_event("database", "stats", 0, "ERROR", str(exc))
        return f"❌ Stats error: {exc}"


async def _do_vacuum(client, owner_id: int) -> str:
    t0 = asyncio.get_event_loop().time()
    try:
        orphan_ids, total = await _find_orphans(client, owner_id)
        removed = db_client.cleanup_orphans(owner_id, orphan_ids)
        remaining = total - removed
        record_event("database", "vacuum", (asyncio.get_event_loop().time() - t0) * 1000, "SUCCESS", f"{removed}/{total}")
        await db_client.log(owner_id, "INFO", f"DB vacuum: removed {removed} orphans", {
            "removed": removed, "remaining": remaining,
        })
        return (
            f"⚙️ **Vacuum complete**\n\n"
            f"Orphans removed: `{removed}`\n"
            f"Items remaining: `{remaining}`\n"
            f"Index optimization: skipped (PostgREST)"
        )
    except Exception as exc:
        logger.error("db vacuum failed: %s", exc)
        record_event("database", "vacuum", 0, "ERROR", str(exc))
        await db_client.log(owner_id, "ERROR", f"DB vacuum failed: {exc}", {})
        return f"❌ Vacuum error: {exc}"


async def _db_clean_action(event, extra: str) -> tuple:
    from backend.helper.inline_engine import _self_client, _owner_id
    result = await _do_clean(_self_client, _owner_id)
    builder = InlinePanelBuilder()
    builder.add_row("Back", "panel:db")
    builder.add_row("Close", "panel:help:close")
    return result, builder.build()


async def _db_stats_action(event, extra: str) -> tuple:
    from backend.helper.inline_engine import _owner_id
    from backend.bot.handlers.misc import _resolve_tz
    result = await _do_stats(_owner_id, _resolve_tz())
    builder = InlinePanelBuilder()
    builder.add_row("Back", "panel:db")
    builder.add_row("Close", "panel:help:close")
    return result, builder.build()


async def _db_vacuum_action(event, extra: str) -> tuple:
    from backend.helper.inline_engine import _self_client, _owner_id
    result = await _do_vacuum(_self_client, _owner_id)
    builder = InlinePanelBuilder()
    builder.add_row("Back", "panel:db")
    builder.add_row("Close", "panel:help:close")
    return result, builder.build()


async def _db_panel_handler(event, extra: str) -> None:
    text = "**Database**\n\nChoose an action:"
    builder = InlinePanelBuilder()
    builder.add_row("📊 Statistics", "action:db_stats")
    builder.add_row("🧹 Clean Orphans", "action:db_clean")
    builder.add_row("⚙️ Vacuum", "action:db_vacuum")
    builder.add_row("Close", "panel:help:close")
    await event.edit(text, buttons=builder.build())


async def _db_inline_builder(event, extra: str) -> list:
    from telethon.tl import types
    text = "**Database**\n\nChoose an action:"
    builder = InlinePanelBuilder()
    builder.add_row("📊 Statistics", "action:db_stats")
    builder.add_row("🧹 Clean Orphans", "action:db_clean")
    builder.add_row("⚙️ Vacuum", "action:db_vacuum")
    builder.add_row("Close", "panel:help:close")
    buttons = builder.build()
    msg = types.InputBotInlineMessageText(
        message=text,
        reply_markup=types.ReplyInlineMarkup(rows=buttons) if buttons else None,
    )
    result = types.InputBotInlineResult(
        id="0",
        type="article",
        title="Database",
        send_message=msg,
    )
    return [result]


def register(client, owner_id: int, tz_str: str):

    register_panel("db", _db_panel_handler)
    register_inline_builder("db", _db_inline_builder)
    register_action("db_clean", _db_clean_action)
    register_action("db_stats", _db_stats_action)
    register_action("db_vacuum", _db_vacuum_action)

    @client.on(events.NewMessage(outgoing=True, pattern=r"^\.db\s+(clean|stats|vacuum)$"))
    async def db_cmd(event):
        if not is_owner(event, owner_id):
            return
        action = event.pattern_match.group(1)
        if action == "clean":
            result = await _do_clean(client, owner_id)
        elif action == "stats":
            result = await _do_stats(owner_id, tz_str)
        elif action == "vacuum":
            result = await _do_vacuum(client, owner_id)
        await event.edit(result)

    @client.on(events.NewMessage(outgoing=True, pattern=r"^\.db$"))
    async def db_panel(event):
        if not is_owner(event, owner_id):
            return
        helper = get_client()
        if helper is None:
            await event.edit("⚠️ Inline mode requires the helper bot (BOT_TOKEN).")
            return
        try:
            await event.delete()
            await send_inline_panel(client, event.chat_id, "db")
        except Exception as exc:
            logger.warning("db inline send failed: %s", exc)
