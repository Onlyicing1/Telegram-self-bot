"""
Delete service — all deletion business logic lives here.

Both text commands and inline panels call these exact functions.
"""
import asyncio
import logging

from backend.db import client as db_client
from backend.diagnostics import record_event
from backend.services import settings_service

logger = logging.getLogger(__name__)


async def do_del_n(client, chat_id, n: int) -> str:
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


async def do_del_id(client, chat_id, start_id: int) -> str:
    t0 = asyncio.get_event_loop().time()
    try:
        msg_ids = []
        async for msg in client.iter_messages(chat_id, min_id=start_id - 1):
            msg_ids.append(msg.id)
            if len(msg_ids) >= settings_service.delete_batch_size():
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


async def do_del_code(client, owner_id: int, code: str) -> str:
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
