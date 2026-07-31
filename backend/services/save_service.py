"""
Save service — all save business logic lives here.

Both text commands and inline panels call these exact functions.
No business logic exists in any handler module.
"""
import asyncio
import io
import logging
from datetime import datetime

from telethon.tl.types import (
    MessageMediaDocument,
    MessageMediaPhoto,
    DocumentAttributeFilename,
)

from backend.bio.engine import _get_tz
from backend.db import client as db_client
from backend.diagnostics import record_event
from backend.services import settings_service

logger = logging.getLogger(__name__)

_MEDIA_TYPE_MAP = {
    "image/jpeg": "Photo",
    "image/png": "Photo",
    "image/gif": "Animation",
    "image/webp": "Sticker",
    "video/mp4": "Video",
    "video/quicktime": "Video",
    "audio/mpeg": "Audio",
    "audio/ogg": "Voice",
    "audio/mp4": "Audio",
    "application/pdf": "Document",
}

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

_MIME_EXT = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "video/mp4": ".mp4",
    "video/quicktime": ".mov",
    "audio/mpeg": ".mp3",
    "audio/ogg": ".ogg",
    "audio/mp4": ".m4a",
    "application/pdf": ".pdf",
    "application/zip": ".zip",
    "application/vnd.android.package-archive": ".apk",
}


def detect_media_type(mime: str | None) -> str:
    if not mime:
        return "Unknown"
    return _MEDIA_TYPE_MAP.get(mime, "Document")


def media_icon(media_type: str | None) -> str:
    return _MEDIA_ICON.get(media_type or "Unknown", "📦")


def extract_file_name(media) -> str | None:
    if isinstance(media, MessageMediaDocument):
        doc = media.document
        for attr in getattr(doc, "attributes", []):
            if isinstance(attr, DocumentAttributeFilename) and attr.file_name:
                return attr.file_name
            fn = getattr(attr, "file_name", None)
            if fn:
                return fn
    return None


def generate_filename(media, mime_type: str | None, save_code: str) -> str:
    if isinstance(media, MessageMediaPhoto):
        return f"photo_{save_code}.jpg"
    ext = _MIME_EXT.get(mime_type or "", ".bin")
    return f"{save_code}{ext}"


def build_tags(media_type: str, dt: datetime) -> list[str]:
    mt = media_type.lower().replace(" ", "_")
    return [
        "#saved",
        f"#saved_{mt}",
        f"#saved_{dt.year}",
        f"#saved_{dt.year}_{dt.month:02d}",
        f"#saved_{dt.year}_{dt.month:02d}_{dt.day}",
    ]


def build_caption(
    save_code: str,
    sender: str,
    chat_id: int,
    msg_id: int,
    dt: datetime,
    media_type: str,
    mime: str | None,
    file_size: int | None,
    file_name: str | None,
    tags: list[str],
) -> str:
    size_str = f"{file_size / 1024:.1f} KB" if file_size else "—"
    return (
        f"━━━━━━━━━━━━━━\n"
        f"📦 LifeOS\n\n"
        f"Code:\n{save_code}\n\n"
        f"Folder:\nUnfiled\n\n"
        f"Saved:\n{dt.strftime('%Y-%m-%d %H:%M')}\n"
        f"━━━━━━━━━━━━━━\n\n"
        f"🎙 Sender: {sender}\n"
        f"🖼 Type: {media_type}\n"
        f"🧾 MIME: {mime or '—'}\n"
        f"📦 Size: {size_str}\n"
        f"📁 File: {file_name or '—'}\n"
        f"🏷 Tags: {' '.join(tags)}"
    )


def build_confirmation(
    save_code: str,
    mode: str,
    media_type: str,
    file_name: str | None,
) -> str:
    icon = media_icon(media_type)
    mode_label = "Forward Save" if mode == "f" else "Deep Save"
    lines = [
        f"{icon} **Saved Successfully**",
        "",
        f"**Code:** `{save_code}`",
        f"**Type:** {media_type}",
    ]
    if file_name:
        lines.append(f"**Filename:** `{file_name}`")
    lines.append(f"**Mode:** {mode_label}")
    return "\n".join(lines)


def _unwrap_forward(result) -> object | None:
    if result is None:
        return None
    return result[0] if isinstance(result, list) else result


async def execute_save(client, owner_id: int, reply_msg, mode: str, tz_str: str) -> str:
    """Execute a save operation and return a result string."""
    save_code = await db_client.get_next_save_code()
    tz = _get_tz(tz_str)
    now = datetime.now(tz)

    sender_name = "Unknown"
    sender_id = reply_msg.sender_id or 0
    try:
        sender = await reply_msg.get_sender()
        if sender:
            parts = [
                getattr(sender, "first_name", "") or "",
                getattr(sender, "last_name", "") or "",
            ]
            sender_name = " ".join(p for p in parts if p).strip() or str(sender_id)
    except Exception:
        pass

    origin_chat_id = reply_msg.chat_id
    origin_msg_id = reply_msg.id

    mime_type = None
    file_size = None
    file_name = None
    file_id = None

    media = reply_msg.media
    if isinstance(media, MessageMediaDocument):
        doc = media.document
        mime_type = getattr(doc, "mime_type", None)
        file_size = getattr(doc, "size", None)
        file_name = extract_file_name(media)
        file_id = str(getattr(doc, "id", ""))
    elif isinstance(media, MessageMediaPhoto):
        mime_type = "image/jpeg"
        photo = media.photo
        if hasattr(photo, "sizes") and photo.sizes:
            file_size = getattr(photo.sizes[-1], "size", None)
        file_id = str(getattr(photo, "id", ""))

    media_type = detect_media_type(mime_type)
    if not file_name:
        file_name = generate_filename(media, mime_type, save_code)
    tags = build_tags(media_type, now)

    has_media = media is not None
    logger.info(
        "[SAVE] owner=%s mode=%s media=%s save_code=%s file_name=%s mime=%s size=%s file_id=%s",
        owner_id, mode, has_media, save_code, file_name, mime_type, file_size, file_id,
    )

    if mode == "f":
        t0 = asyncio.get_event_loop().time()
        try:
            raw = await client.forward_messages("me", reply_msg)
            fwd = _unwrap_forward(raw)
            saved_chat_id = fwd.chat_id if fwd else None
            saved_msg_id = fwd.id if fwd else None
            record_event("save", "forward_messages", (asyncio.get_event_loop().time() - t0) * 1000, "SUCCESS")
        except Exception as exc:
            logger.error("forward save failed: %s", exc)
            record_event("save", "forward_messages", 0, "ERROR", str(exc))
            return f"❌ Forward failed: {exc}"

        payload = {
            "save_code": save_code,
            "save_type": "forward",
            "origin_chat_id": origin_chat_id,
            "origin_msg_id": origin_msg_id,
            "saved_chat_id": saved_chat_id,
            "saved_msg_id": saved_msg_id,
            "sender_name": sender_name,
            "sender_id": sender_id,
            "mime_type": mime_type,
            "file_id": file_id,
            "file_size": file_size,
            "media_type": media_type,
            "tags": tags,
            "caption": None,
            "owner_id": owner_id,
            "created_at": now.isoformat(),
        }
        inserted = None
        try:
            inserted = db_client.insert_save(payload)
        except Exception as exc:
            logger.error("[SAVE_DB] forward insert_save raised: %s", exc, exc_info=True)
        if inserted is None:
            logger.error("[SAVE_DB] forward insert returned None — row NOT in database")
        else:
            logger.info("[SAVE_DB] forward insert_ok=True id=%s", inserted.get("id"))

        await db_client.log(owner_id, "INFO", f"Saved F {save_code}", {
            "save_code": save_code,
            "origin_chat_id": origin_chat_id,
            "origin_msg_id": origin_msg_id,
        })
        return build_confirmation(save_code, mode, media_type, file_name)

    else:
        if not media:
            return "⚠️ Replied message has no downloadable media."

        max_bytes = settings_service.max_deep_save_mb() * 1024 * 1024
        if file_size and file_size > max_bytes:
            mb = file_size / (1024 * 1024)
            limit_mb = settings_service.max_deep_save_mb()
            return (
                f"⚠️ File is {mb:.1f} MB — exceeds the "
                f"{limit_mb} MB deep-save limit.\n"
                "Use `.save f` for a forward save instead."
            )

        caption = build_caption(
            save_code=save_code,
            sender=sender_name,
            chat_id=origin_chat_id,
            msg_id=origin_msg_id,
            dt=now,
            media_type=media_type,
            mime=mime_type,
            file_size=file_size,
            file_name=file_name,
            tags=tags,
        )

        buf = io.BytesIO()
        sent = None
        try:
            t0 = asyncio.get_event_loop().time()
            await client.download_media(reply_msg, file=buf)
            record_event("save", "download_media", (asyncio.get_event_loop().time() - t0) * 1000, "SUCCESS")

            buf_size = buf.tell()
            if buf_size == 0:
                return "❌ Download produced an empty buffer."

            buf.seek(0)
            buf.name = file_name

            try:
                t1 = asyncio.get_event_loop().time()
                sent = await client.send_file(
                    "me",
                    buf,
                    caption=caption,
                    force_document=False,
                )
                record_event("save", "send_file", (asyncio.get_event_loop().time() - t1) * 1000, "SUCCESS")
            except Exception as exc:
                logger.error("deep save upload failed: %s", exc)
                record_event("save", "send_file", 0, "ERROR", str(exc))
                return f"❌ Upload failed: {exc}"

        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("deep save download failed: %s", exc)
            record_event("save", "download_media", 0, "ERROR", str(exc))
            return f"❌ Download failed: {exc}"
        finally:
            buf.close()

        saved_chat_id = sent.chat_id if sent else None
        saved_msg_id = sent.id if sent else None

        payload = {
            "save_code": save_code,
            "save_type": "deep",
            "origin_chat_id": origin_chat_id,
            "origin_msg_id": origin_msg_id,
            "saved_chat_id": saved_chat_id,
            "saved_msg_id": saved_msg_id,
            "sender_name": sender_name,
            "sender_id": sender_id,
            "mime_type": mime_type,
            "file_id": file_id,
            "file_size": file_size,
            "media_type": media_type,
            "tags": tags,
            "caption": caption,
            "owner_id": owner_id,
            "created_at": now.isoformat(),
        }
        inserted = None
        try:
            inserted = db_client.insert_save(payload)
        except Exception as exc:
            logger.error("[SAVE_DB] deep insert_save raised: %s", exc, exc_info=True)
        if inserted is None:
            logger.error("[SAVE_DB] deep insert returned None — row NOT in database")
        else:
            logger.info("[SAVE_DB] deep insert_ok=True id=%s", inserted.get("id"))

        await db_client.log(owner_id, "INFO", f"Saved D {save_code}", {
            "save_code": save_code,
            "origin_chat_id": origin_chat_id,
            "origin_msg_id": origin_msg_id,
        })
        return build_confirmation(save_code, mode, media_type, file_name)
