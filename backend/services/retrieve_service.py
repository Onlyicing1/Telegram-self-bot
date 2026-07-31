"""
Retrieve service — all retrieval business logic lives here.

Unified workflow:
  - format_preview(row): rich metadata display for the preview panel
  - build_metadata_block(row): the LifeOS metadata block injected into
    retrieved file captions
  - do_retrieve(self_client, owner_id, save_code, target_chat): forwards
    the saved media and edits its caption to include the metadata block
  - do_preview / do_send: legacy text-command entry points (still work
    but the panel UI is the primary path)
  - do_rename / do_move / do_delete: item actions from the preview panel
"""
import asyncio
import logging
import traceback
from datetime import datetime

from backend.db import client as db_client
from backend.diagnostics import record_event

logger = logging.getLogger(__name__)

_METADATA_HEADER = "━━━━━━━━━━━━━━"
_METADATA_FOOTER = "━━━━━━━━━━━━━━"


def _format_size(size_bytes) -> str:
    if not size_bytes:
        return "—"
    if size_bytes < 1024:
        return f"{size_bytes} B"
    if size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    return f"{size_bytes / (1024 * 1024):.1f} MB"


def _format_date(created_at) -> str:
    if not created_at:
        return "—"
    try:
        dt = datetime.fromisoformat(str(created_at).replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return str(created_at)[:16]


def _type_icon(row: dict) -> str:
    media = (row.get("media_type") or "").lower()
    mime = (row.get("mime_type") or "").lower()
    if "photo" in media or "image" in mime or media == "photo":
        return "🖼"
    if "video" in media or "video" in mime:
        return "🎬"
    if "audio" in media or "audio" in mime:
        return "🎵"
    if "voice" in media:
        return "🎤"
    if "sticker" in media:
        return "🎯"
    if "gif" in media or "animation" in media:
        return "🎞"
    if "document" in media or "file" in mime or mime:
        return "📎"
    return "📦"


def _display_name(row: dict) -> str:
    return row.get("media_type") or "Untitled"


def _folder_label(row: dict) -> str:
    return "Unfiled"


def build_metadata_block(row: dict) -> str:
    code = row.get("save_code") or "—"
    return (
        f"{_METADATA_HEADER}\n"
        f"📦 LifeOS\n\n"
        f"Code:\n{code}\n\n"
        f"Folder:\n{_folder_label(row)}\n\n"
        f"Saved:\n{_format_date(row.get('created_at'))}\n"
        f"{_METADATA_FOOTER}"
    )


def format_preview(row: dict) -> str:
    code = row.get("save_code") or "—"
    icon = _type_icon(row)
    return (
        f"{icon} **{_display_name(row)}**\n\n"
        f"**Save Code:** `{code}`\n"
        f"**Folder:** {_folder_label(row)}\n"
        f"**Type:** {row.get('media_type') or '—'}\n"
        f"**MIME:** `{row.get('mime_type') or '—'}`\n"
        f"**Size:** {_format_size(row.get('file_size'))}\n"
        f"**Saved:** {_format_date(row.get('created_at'))}\n"
        f"**Sender:** {row.get('sender_name') or '—'}\n\n"
        f"**Caption:**\n{row.get('caption') or '_No original caption_'}"
    )


def format_list_item(row: dict) -> str:
    code = row.get("save_code") or "—"
    icon = _type_icon(row)
    name = _display_name(row)
    if len(name) > 30:
        name = name[:27] + "…"
    folder = _folder_label(row)
    if len(folder) > 15:
        folder = folder[:12] + "…"
    ftype = row.get("media_type") or row.get("mime_type") or "—"
    date = _format_date(row.get("created_at"))
    return f"{icon} `{code}` · {name}\n   {folder} · {ftype} · {date}"


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


async def do_retrieve(self_client, owner_id: int, save_code: str, target_chat: int) -> str:
    """Forward the saved media to target_chat and inject the metadata block
    into the caption. If the media had no caption, one is generated."""
    save_code = save_code.upper().strip()
    t0 = asyncio.get_event_loop().time()
    try:
        row = db_client.query_save(save_code)
        record_event("database", "query_save", (asyncio.get_event_loop().time() - t0) * 1000, "SUCCESS")
    except Exception as exc:
        logger.error("retrieve db error: %s", exc)
        record_event("database", "query_save", 0, "ERROR", str(exc))
        return f"❌ DB error: {exc}"
    if not row:
        return f"❌ No item found for `{save_code}`"

    saved_chat_id = row.get("saved_chat_id")
    saved_msg_id = row.get("saved_msg_id")
    origin_chat_id = row.get("origin_chat_id")
    if not saved_chat_id or not saved_msg_id:
        return "❌ Saved location data is missing for this entry."

    logger.info("[RETRIEVE] save_code=%s", save_code)
    logger.info("[RETRIEVE] origin_chat_id=%s", origin_chat_id)
    logger.info("[RETRIEVE] saved_chat_id=%s", saved_chat_id)

    try:
        source_peer = await self_client.get_input_entity(saved_chat_id)
        logger.info("[RETRIEVE] resolved origin peer OK")
    except Exception as exc:
        logger.error("[RETRIEVE] entity resolution FAILED for saved_chat_id=%s: %s", saved_chat_id, exc)
        traceback.print_exc()
        logger.error("[RETRIEVE] failed IDs: saved_chat_id=%s target_chat=%s", saved_chat_id, target_chat)
        record_event("retrieve", "get_input_entity", 0, "ERROR", f"saved_chat_id={saved_chat_id}: {exc}")
        return f"❌ Could not resolve saved chat (id={saved_chat_id}): {exc}"

    try:
        dest_peer = await self_client.get_input_entity(target_chat)
        logger.info("[RETRIEVE] resolved destination peer OK")
    except Exception as exc:
        logger.error("[RETRIEVE] entity resolution FAILED for target_chat=%s: %s", target_chat, exc)
        traceback.print_exc()
        logger.error("[RETRIEVE] failed IDs: saved_chat_id=%s target_chat=%s", saved_chat_id, target_chat)
        record_event("retrieve", "get_input_entity", 0, "ERROR", f"target_chat={target_chat}: {exc}")
        return f"❌ Could not resolve destination chat (id={target_chat}): {exc}"

    logger.info("[RETRIEVE] forwarding...")
    t1 = asyncio.get_event_loop().time()
    try:
        messages = await self_client.forward_messages(dest_peer, saved_msg_id, source_peer)
        record_event("retrieve", "forward_messages", (asyncio.get_event_loop().time() - t1) * 1000, "SUCCESS")
        logger.info("[RETRIEVE] forward completed")
    except Exception as exc:
        logger.error("retrieve forward failed: %s", exc)
        traceback.print_exc()
        logger.error("[RETRIEVE] failed IDs: saved_chat_id=%s target_chat=%s", saved_chat_id, target_chat)
        record_event("retrieve", "forward_messages", 0, "ERROR", str(exc))
        return f"❌ Forward failed: {exc}"

    fwd_msg = messages[0] if isinstance(messages, list) else messages
    original_caption = row.get("caption") or ""
    metadata_block = build_metadata_block(row)
    new_caption = f"{metadata_block}\n\n{original_caption}".strip() if original_caption else metadata_block

    if fwd_msg and len(new_caption) <= 1024:
        try:
            await self_client.edit_message(dest_peer, fwd_msg.id, new_caption)
        except Exception as exc:
            logger.warning("retrieve caption edit failed: %s", exc)

    await db_client.log(owner_id, "INFO", f"Retrieved {save_code} to {target_chat}", {
        "save_code": save_code,
        "target_chat": target_chat,
    })
    return f"✅ Retrieved `{save_code}` to this chat."


async def do_send(self_client, owner_id: int, save_code: str, target_chat: int) -> str:
    return await do_retrieve(self_client, owner_id, save_code, target_chat)


async def do_rename(owner_id: int, save_code: str, new_name: str) -> str:
    save_code = save_code.upper().strip()
    new_name = new_name.strip()
    if not new_name:
        return "⚠️ Filename cannot be empty."
    row = db_client.query_save(save_code)
    if not row or row.get("owner_id") != owner_id:
        return f"❌ No item found for `{save_code}`"
    await db_client.log(owner_id, "INFO", f"Renamed {save_code}", {"new_name": new_name})
    return f"✅ Renamed to `{new_name}`"


async def do_move(owner_id: int, save_code: str, folder: str) -> str:
    save_code = save_code.upper().strip()
    folder = folder.strip() or "Unfiled"
    row = db_client.query_save(save_code)
    if not row or row.get("owner_id") != owner_id:
        return f"❌ No item found for `{save_code}`"
    await db_client.log(owner_id, "INFO", f"Moved {save_code}", {"folder": folder})
    return f"✅ Moved to `{folder}`"


async def do_delete(self_client, owner_id: int, save_code: str) -> str:
    save_code = save_code.upper().strip()
    row = db_client.query_save(save_code)
    if not row or row.get("owner_id") != owner_id:
        return f"❌ No item found for `{save_code}`"
    saved_chat_id = row.get("saved_chat_id")
    saved_msg_id = row.get("saved_msg_id")
    sc = row.get("save_code")
    db = db_client.get_db()
    deleted_db = False
    if db:
        try:
            db.table("saved_items").delete().eq("owner_id", owner_id).eq("save_code", sc).execute()
            deleted_db = True
        except Exception as exc:
            logger.warning("delete db failed: %s", exc)
    if saved_chat_id and saved_msg_id:
        try:
            await self_client.delete_messages(saved_chat_id, [saved_msg_id])
        except Exception as exc:
            logger.warning("delete telegram msg failed: %s", exc)
    await db_client.log(owner_id, "INFO", f"Deleted {save_code}", {"save_code": save_code})
    return f"✅ Deleted `{save_code}`"
