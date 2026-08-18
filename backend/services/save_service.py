"""
Save service — the single authoritative Save Engine.

Deep Save is the ONLY save method. It downloads the source content and
re-uploads it as a brand-new Saved Messages message:

    SOURCE MESSAGE → download → local temp file → upload → NEW message → DB

There is no native forwarding anywhere in this module. A protected chat
that blocks forwarding does not affect Deep Save, and a Deep Save failure
is never silently converted into a forward.

Text commands, the Glass UI, and the AI SaveTool all call the same
``execute_save`` pipeline — no business logic lives in any handler.
"""
import asyncio
import logging
import os
import re
import shutil
import tempfile
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

_LINK_RE = re.compile(
    r"https?://(?:t|telegram)\.me/"
    r"(?:c/(\d+)/(\d+)"        # private:  /c/<internal_chat>/<msg_id>
    r"|(\w+)/(\d+))"           # username: /<username>/<msg_id>
)

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
    "Text": "📝",
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
    """Compact, information-dense LifeOS caption.

    The model name is dominant, the metadata is one line each, and the
    original source text is appended afterwards by ``_append_original_text``.
    """
    size_str = _format_bytes(file_size) if file_size else "—"
    icon = media_icon(media_type)
    lines = [
        f"{icon} {save_code} · DEEP",
        f"👤 {sender}",
        f"🕒 {dt.strftime('%Y-%m-%d %H:%M')}",
        f"🆔 {chat_id}/{msg_id}",
        f"🗂 {media_type} · {size_str}" + (f" · {mime}" if mime else ""),
    ]
    if file_name:
        lines.append(f"📄 {file_name}")
    if tags:
        lines.append(" ".join(tags))
    return "\n".join(lines)


def _append_original_text(caption: str, message) -> str:
    """Append the original message text below the LifeOS caption block.

    The saved item carries the generated caption without losing the source
    content. Text-only sources are represented here too.
    """
    try:
        text = (getattr(message, "text", None) or "").strip()
    except Exception:
        text = ""
    if not text:
        return caption
    return f"{caption}\n\n{text}"


def _upload_kwargs_for_media(media, mime_type: str | None, file_name: str | None) -> dict:
    """Build ``send_file`` kwargs that preserve the original media type.

    - Photos are sent as photo messages (Telethon auto-detects from the
      ``.jpg`` name when ``force_document=False``).
    - Documents keep their original Telegram attributes (video, audio/voice,
      animated, sticker, filename) so a re-uploaded item renders as the same
      type instead of degrading to a generic document.
    """
    if isinstance(media, MessageMediaPhoto):
        return {"force_document": False}
    if isinstance(media, MessageMediaDocument):
        doc = getattr(media, "document", None)
        attrs: list = []
        has_filename = False
        for attr in getattr(doc, "attributes", []) or []:
            if isinstance(attr, DocumentAttributeFilename):
                has_filename = True
                attrs.append(DocumentAttributeFilename(file_name or attr.file_name or "file"))
            else:
                attrs.append(attr)
        if not has_filename and file_name:
            attrs.append(DocumentAttributeFilename(file_name))
        kwargs: dict = {"attributes": attrs, "force_document": False}
        doc_mime = getattr(doc, "mime_type", None) if doc is not None else None
        resolved_mime = mime_type or doc_mime
        if resolved_mime:
            kwargs["mime_type"] = resolved_mime
        return kwargs
    return {"force_document": False}


def build_confirmation(
    save_code: str,
    media_type: str,
    file_name: str | None,
) -> str:
    icon = media_icon(media_type)
    lines = [
        f"{icon} **Saved Successfully**",
        "",
        f"**Code:** `{save_code}`",
        f"**Type:** {media_type}",
    ]
    if file_name:
        lines.append(f"**Filename:** `{file_name}`")
    return "\n".join(lines)


def _extract_uploaded_metadata(sent) -> tuple[str | None, str | None, int | None]:
    """Extract (file_id, mime_type, file_size) from the newly-uploaded message.

    Deep Save persists metadata that refers to the NEW message Telegram
    created for the re-upload, not the original source message, so lookups
    and retrieval always point at the actual saved item.
    """
    media = getattr(sent, "media", None)
    if isinstance(media, MessageMediaDocument):
        doc = media.document
        return (
            str(getattr(doc, "id", "")) or None,
            getattr(doc, "mime_type", None),
            getattr(doc, "size", None),
        )
    if isinstance(media, MessageMediaPhoto):
        photo = media.photo
        return (
            str(getattr(photo, "id", "")) or None,
            "image/jpeg",
            None,
        )
    return None, None, None


def parse_telegram_link(link: str) -> tuple[str | None, int, int]:
    """Parse a t.me / telegram.me link into (username, chat_id, msg_id).

    For private "/c/" links:
      https://t.me/c/3080318802/42  →  (None, -1003080318802, 42)
    For username links:
      https://t.me/somechannel/42    →  ("somechannel", 0, 42)

    Returns (None, 0, 0) if the link doesn't match any known pattern.
    """
    m = _LINK_RE.search(link.strip())
    if not m:
        return None, 0, 0

    private_chat, private_msg = m.group(1), m.group(2)
    username, username_msg = m.group(3), m.group(4)

    if private_chat is not None:
        chat_id = int(f"-100{private_chat}")
        msg_id = int(private_msg)
        logger.info("[LINK_SAVE] parsed type=private chat_id=%s msg_id=%s", chat_id, msg_id)
        return None, chat_id, msg_id

    chat_username = username
    msg_id = int(username_msg)
    logger.info("[LINK_SAVE] parsed type=username chat=%s msg_id=%s", chat_username, msg_id)
    return chat_username, 0, msg_id


def _format_bytes(n: int | None) -> str:
    if not n:
        return "0 B"
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.2f} KB"
    if n < 1024 * 1024 * 1024:
        return f"{n / (1024 * 1024):.2f} MB"
    return f"{n / (1024 * 1024 * 1024):.2f} GB"


async def _resolve_sender(reply_msg) -> tuple[str, int]:
    """Resolve the source message's sender name + id (best effort)."""
    sender_id = getattr(reply_msg, "sender_id", None) or 0
    sender_name = "Unknown"
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
    return sender_name, sender_id


def _extract_source_media(reply_msg, save_code: str) -> tuple[str | None, int | None, str | None, str | None, str]:
    """Extract (mime_type, file_size, file_name, file_id, media_type) from the source."""
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
    return mime_type, file_size, file_name, file_id, media_type


async def execute_save(client, owner_id: int, reply_msg, tz_str: str) -> str:
    """Deep Save — the single authoritative save pipeline.

    Deep Save downloads the source content and uploads it again as a NEW
    Saved Messages message. It NEVER forwards, under any circumstance:

    - text-only source → a new ``send_message`` text message
    - media source → ``download_media`` → validate → ``send_file``

    A download or upload failure is an honest Deep Save failure. The DB
    record is written only after the Telegram operation succeeded.
    """
    save_code = await db_client.get_next_save_code()
    now = datetime.now(_get_tz(tz_str))
    sender_name, sender_id = await _resolve_sender(reply_msg)
    origin_chat_id = reply_msg.chat_id
    origin_msg_id = reply_msg.id

    media = reply_msg.media
    mime_type, file_size, file_name, file_id, media_type = _extract_source_media(reply_msg, save_code)

    # Text-only source → a NEW text message (no media transfer, no forward).
    if media is None:
        original_text = (reply_msg.text or "").strip()
        if not original_text:
            return "⚠️ Replied message has no text or media to save."
        media_type = "Text"
        mime_type = None
        file_name = None
        file_id = None
        file_size = len(original_text.encode("utf-8"))

    logger.info(
        "[SAVE] owner=%s media=%s save_code=%s file_name=%s mime=%s size=%s file_id=%s",
        owner_id, media is not None, save_code, file_name, mime_type, file_size, file_id,
    )

    max_bytes = settings_service.max_deep_save_mb() * 1024 * 1024
    if file_size and file_size > max_bytes:
        mb = file_size / (1024 * 1024)
        limit_mb = settings_service.max_deep_save_mb()
        return f"⚠️ File is {mb:.1f} MB — exceeds the {limit_mb} MB deep-save limit."

    tags = build_tags(media_type, now)
    caption = _append_original_text(
        build_caption(
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
        ),
        reply_msg,
    )

    sent = None
    actual_size = file_size

    if media is None:
        try:
            t0 = asyncio.get_event_loop().time()
            sent = await client.send_message("me", caption)
            record_event("save", "send_message", (asyncio.get_event_loop().time() - t0) * 1000, "SUCCESS")
        except Exception as exc:
            logger.error("deep save text send failed: %s", exc)
            record_event("save", "send_message", 0, "ERROR", str(exc))
            return f"❌ Deep Save failed: text could not be uploaded to Saved Messages ({exc})"
    else:
        # Isolated temp storage per operation; removed on every exit path.
        tmp_dir = tempfile.mkdtemp(prefix="lifeos_dl_")
        tmp_path = os.path.join(tmp_dir, os.path.basename(file_name or "file.bin"))
        try:
            # STAGE 1 — physically download the source media.
            try:
                t0 = asyncio.get_event_loop().time()
                await client.download_media(reply_msg, file=tmp_path)
                record_event("save", "download_media", (asyncio.get_event_loop().time() - t0) * 1000, "SUCCESS")
            except Exception as exc:
                logger.error("deep save download failed: %s", exc)
                record_event("save", "download_media", 0, "ERROR", str(exc))
                return f"❌ Deep Save failed: unable to download the source message ({exc})"

            # Validate the download before uploading anything.
            if not os.path.exists(tmp_path):
                return "❌ Deep Save failed: downloaded file is missing."
            actual_size = os.path.getsize(tmp_path)
            if actual_size == 0:
                return "❌ Deep Save failed: downloaded file is empty."

            # STAGE 2 — upload the downloaded content as a NEW message.
            try:
                t1 = asyncio.get_event_loop().time()
                sent = await client.send_file(
                    "me",
                    tmp_path,
                    caption=caption,
                    **_upload_kwargs_for_media(media, mime_type, file_name),
                )
                record_event("save", "send_file", (asyncio.get_event_loop().time() - t1) * 1000, "SUCCESS")
            except Exception as exc:
                logger.error("deep save upload failed: %s", exc)
                record_event("save", "send_file", 0, "ERROR", str(exc))
                return f"❌ Deep Save failed: downloaded media could not be uploaded to Saved Messages ({exc})"
        except asyncio.CancelledError:
            raise
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    # STAGE 3 — metadata from the NEWLY uploaded message + DB persistence.
    saved_chat_id = sent.chat_id if sent else None
    saved_msg_id = sent.id if sent else None
    new_file_id, new_mime, new_size = _extract_uploaded_metadata(sent)

    payload = {
        "save_code": save_code,
        "save_type": "deep",
        "origin_chat_id": origin_chat_id,
        "origin_msg_id": origin_msg_id,
        "saved_chat_id": saved_chat_id,
        "saved_msg_id": saved_msg_id,
        "sender_name": sender_name,
        "sender_id": sender_id,
        "mime_type": new_mime or mime_type,
        "file_id": new_file_id or file_id,
        "file_size": actual_size or new_size,
        "media_type": media_type,
        "tags": tags,
        "caption": caption,
        "owner_id": owner_id,
        "created_at": now.isoformat(),
    }

    inserted = None
    try:
        inserted = await db_client.insert_save(payload)
    except Exception as exc:
        logger.error("[SAVE_DB] insert_save raised: %s", exc, exc_info=True)

    if inserted is None:
        logger.error("[SAVE_DB] insert returned None — row NOT in database")
        await db_client.log(owner_id, "ERROR", f"Saved D {save_code} but DB insert failed", {
            "save_code": save_code,
            "origin_chat_id": origin_chat_id,
            "origin_msg_id": origin_msg_id,
        })
        return f"⚠️ Uploaded to Saved Messages, but the database record failed for `{save_code}`."

    await db_client.log(owner_id, "INFO", f"Saved D {save_code}", {
        "save_code": save_code,
        "origin_chat_id": origin_chat_id,
        "origin_msg_id": origin_msg_id,
    })
    logger.info("[SAVE] completed: %s", save_code)
    return build_confirmation(save_code, media_type, file_name)


async def execute_link_save(client, owner_id: int, link: str, tz_str: str) -> str:
    """Resolve a Telegram link and Deep-Save the linked message.

    This is the same Deep Save pipeline as ``execute_save`` — the only
    difference is the source resolution (a t.me link instead of a reply).
    """
    logger.info("[LINK_SAVE] resolving link: %s", link)
    channel, chat_id, msg_id = parse_telegram_link(link)
    if not channel and not chat_id:
        logger.warning("[LINK_SAVE] invalid telegram link: %s", link)
        return "❌ Could not parse link. Use https://t.me/channel/123 or https://t.me/c/123/456"

    try:
        if chat_id:
            target_msg = await client.get_messages(chat_id, ids=msg_id)
        else:
            entity = await client.get_entity(channel)
            target_msg = await client.get_messages(entity, ids=msg_id)
        logger.info(
            "[LINK_SAVE] source message fetched: msg_id=%s chat_id=%s",
            getattr(target_msg, "id", None), getattr(target_msg, "chat_id", None),
        )
    except Exception as exc:
        logger.error("[LINK_SAVE] fetch source message failed: %s", exc, exc_info=True)
        return f"❌ Could not resolve link: {exc}"

    if target_msg is None:
        logger.warning("[LINK_SAVE] source message not found at link")
        return "❌ Message not found at that link."

    return await execute_save(client, owner_id, target_msg, tz_str)
