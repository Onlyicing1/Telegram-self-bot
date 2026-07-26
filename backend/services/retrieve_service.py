"""
Retrieve service — all retrieval business logic lives here.

Both text commands and inline panels call these exact functions.
"""
import asyncio
import logging

from backend.db import client as db_client
from backend.diagnostics import record_event

logger = logging.getLogger(__name__)


def format_preview(row: dict) -> str:
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


async def do_preview(self_client, owner_id: int, save_code: str) -> str:
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
    return format_preview(row)


async def do_send(self_client, owner_id: int, save_code: str, target_chat: int) -> str:
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
