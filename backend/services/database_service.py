"""
Database service — all database maintenance business logic lives here.

Both text commands and inline panels call these exact functions.
"""
import asyncio
import logging
from datetime import datetime

from backend.db import client as db_client
from backend.bio.engine import _get_tz
from backend.diagnostics import record_event

logger = logging.getLogger(__name__)

_AI_STATS_TIMEOUT = 3.0

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


def icon(media_type: str | None) -> str:
    return _MEDIA_ICON.get(media_type or "Unknown", "📦")


def format_date(iso_str: str | None, tz_str: str) -> str:
    if not iso_str:
        return "—"
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        return dt.strftime("%d %b %Y")
    except Exception:
        return str(iso_str)[:10]


async def find_orphans(client, owner_id: int) -> tuple[list[int], int]:
    items = await db_client.list_all_saves(owner_id)
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


async def do_clean(client, owner_id: int) -> str:
    t0 = asyncio.get_event_loop().time()
    try:
        orphan_ids, total = await find_orphans(client, owner_id)
        removed = await db_client.cleanup_orphans(owner_id, orphan_ids)
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


async def _ai_database_counts(owner_id: int) -> tuple[int | None, int | None]:
    """Read optional AI table counts through the repository abstraction."""
    try:
        from backend.ai.database.manager import get_repository_manager

        manager = get_repository_manager()
        results = await asyncio.wait_for(
            asyncio.gather(
                asyncio.to_thread(manager.usage.count, owner_id),
                asyncio.to_thread(manager.provider_stats.count, owner_id),
                return_exceptions=True,
            ),
            timeout=_AI_STATS_TIMEOUT,
        )
        counts: list[int | None] = []
        for result in results:
            if isinstance(result, bool) or not isinstance(result, int):
                counts.append(None)
            else:
                counts.append(result)
        return counts[0], counts[1]
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning("AI database row counts unavailable: %s", exc)
        return None, None


async def do_stats(owner_id: int, tz_str: str) -> str:
    t0 = asyncio.get_event_loop().time()
    try:
        stats = await db_client.get_stats(owner_id)
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
                lines.append(f"  {icon(mt)} {mt}: `{count}`")

        lines.append(f"\n**Database size estimate:** `{size_str}`")
        lines.append(f"**Oldest save:** {format_date(stats['oldest'], tz_str)}")
        lines.append(f"**Newest save:** {format_date(stats['newest'], tz_str)}")

        usage_count, provider_count = await _ai_database_counts(owner_id)
        ghost_count = await db_client.count_ghost_chats()
        lines.append(
            f"\n**AI usage rows:** `{usage_count}`"
            if usage_count is not None else
            "\n**AI usage rows:** `Unavailable`"
        )
        lines.append(
            f"**AI provider rows:** `{provider_count}`"
            if provider_count is not None else
            "**AI provider rows:** `Unavailable`"
        )
        lines.append(
            f"**Ghost Room chats:** `{ghost_count}`"
            if ghost_count is not None else
            "**Ghost Room chats:** `Unavailable`"
        )

        await db_client.log(owner_id, "INFO", f"DB stats: {total} items", {
            **stats,
            "ai_usage_rows": usage_count,
            "ai_provider_rows": provider_count,
            "ghost_room_chats": ghost_count,
        })
        record_event("database", "stats", (asyncio.get_event_loop().time() - t0) * 1000, "SUCCESS")
        return "\n".join(lines)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.error("db stats failed: %s", exc)
        record_event("database", "stats", 0, "ERROR", str(exc))
        return f"❌ Stats error: {exc}"


async def do_vacuum(client, owner_id: int) -> str:
    t0 = asyncio.get_event_loop().time()
    try:
        orphan_ids, total = await find_orphans(client, owner_id)
        removed = await db_client.cleanup_orphans(owner_id, orphan_ids)
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
