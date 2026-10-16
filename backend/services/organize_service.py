"""
Organize service — all organizer business logic lives here.

Both text commands and inline panels call these exact functions.
"""
import asyncio
import logging

from backend.db import client as db_client
from backend.diagnostics import record_event
from backend.helper import symbols as sym
from backend.services import settings_service

logger = logging.getLogger(__name__)


async def do_list(owner_id: int) -> str:
    t0 = asyncio.get_event_loop().time()
    try:
        total = await db_client.count_saves(owner_id)
        fwd = await db_client.count_saves(owner_id, "forward")
        deep = await db_client.count_saves(owner_id, "deep")
        logs = await db_client.count_logs(owner_id)
        bio = await db_client.get_bio_state(owner_id)
        record_event("organize", "list", (asyncio.get_event_loop().time() - t0) * 1000, "SUCCESS")

        bio_status = "OFF"
        bio_template = "—"
        if bio:
            bio_status = "ON" if bio.get("is_active") else "OFF"
            bio_template = bio.get("template", "—")

        lines = [
            "**LifeOS Status**\n",
            f"{sym.MEDIA_UNKNOWN} **Saves**",
            f"  Total: `{total}`",
            f"  Forward: `{fwd}`",
            f"  Deep: `{deep}`\n",
            f"{sym.CONTENT_STATIC} **Logs**",
            f"  Entries: `{logs}`\n",
            f"{sym.CONTENT_AI} **Bio Engine**",
            f"  Status: `{bio_status}`",
            f"  Template: `{bio_template}`",
        ]
        return "\n".join(lines)
    except Exception as exc:
        logger.error("organize list failed: %s", exc)
        record_event("organize", "list", 0, "ERROR", str(exc))
        return f"{sym.STATE_FAILED} Error: {exc}"


async def do_clean(owner_id: int) -> str:
    t0 = asyncio.get_event_loop().time()
    try:
        days = settings_service.log_retention_days()
        deleted = await db_client.clean_logs(owner_id, days=days)
        record_event("organize", "clean", (asyncio.get_event_loop().time() - t0) * 1000, "SUCCESS")
        return f"{sym.OP_REFRESH} Cleaned `{deleted}` log entries older than {days} days."
    except Exception as exc:
        logger.error("organize clean failed: %s", exc)
        record_event("organize", "clean", 0, "ERROR", str(exc))
        return f"{sym.STATE_FAILED} Error: {exc}"
