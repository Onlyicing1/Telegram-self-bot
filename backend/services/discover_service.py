"""
Discover service — all discovery/list/search business logic lives here.

Both text commands and inline panels call these exact functions.
"""
import asyncio
import logging
from datetime import datetime

from backend.db import client as db_client
from backend.bio.engine import _get_tz
from backend.diagnostics import record_event

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


def icon(media_type: str | None) -> str:
    return _MEDIA_ICON.get(media_type or "Unknown", "📦")


def format_date(iso_str: str | None, tz_str: str) -> str:
    if not iso_str:
        return "—"
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        tz = _get_tz(tz_str)
        local_dt = dt.astimezone(tz) if dt.tzinfo else dt
        return local_dt.strftime("%d %b")
    except Exception:
        return str(iso_str)[:10]


def format_list_entry(row: dict, tz_str: str) -> str:
    code = row.get("short_code") or row.get("save_code") or "—"
    ic = icon(row.get("media_type"))
    name = row.get("file_name") or "—"
    mtype = row.get("media_type") or "Unknown"
    date_str = format_date(row.get("created_at"), tz_str)
    return f"{ic} `{code}`\n   {name}\n   {mtype} · {date_str}"


def format_find_entry(row: dict, tz_str: str) -> str:
    code = row.get("short_code") or row.get("save_code") or "—"
    ic = icon(row.get("media_type"))
    name = row.get("file_name") or "—"
    mtype = row.get("media_type") or "Unknown"
    date_str = format_date(row.get("created_at"), tz_str)
    return f"{ic} `{code}` — {name}\n   {mtype} · {date_str}"


async def do_list(owner_id: int, limit: int, tz_str: str) -> str:
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
    lines.extend(format_list_entry(r, tz_str) for r in items)
    return "\n".join(lines)


async def do_find(owner_id: int, query: str, tz_str: str) -> str:
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
    lines.extend(format_find_entry(r, tz_str) for r in items)
    return "\n".join(lines)
