"""
Save service — all save business logic lives here.

Both text commands and inline panels call these exact functions.
No business logic exists in any handler module.
"""
import asyncio
import io
import logging
import os
import re
import shutil
import tempfile
import time
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

_PROGRESS_INTERVAL = 120
_PROGRESS_BAR_LEN = 10

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


def _format_elapsed(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def _progress_bar(perc: float) -> str:
    filled = int(perc / 100 * _PROGRESS_BAR_LEN)
    return "█" * filled + "░" * (_PROGRESS_BAR_LEN - filled)


def _render_progress(phase: str, received: int, total: int | None, elapsed: float) -> str:
    if total:
        perc = min(100.0, received / total * 100)
    else:
        perc = 0.0
    bar = _progress_bar(perc)
    total_str = _format_bytes(total) if total else "—"
    label = "Downloading..." if phase == "download" else "Uploading..."
    speed = int(received / elapsed) if elapsed > 0 else 0
    return (
        f"{label}\n"
        f"{bar} {perc:.0f}%\n"
        f"{'Downloaded' if phase == 'download' else 'Uploaded'}:\n"
        f"{_format_bytes(received)} / {total_str}\n"
        f"Total Size:\n{total_str}\n"
        f"Speed:\n{_format_bytes(speed)}/s\n"
        f"Elapsed:\n{_format_elapsed(elapsed)}"
    )


class _ProgressTracker:
    """Tracks real progress from Telethon callbacks, throttles edits to 2 min."""

    def __init__(self):
        self._received = 0
        self._total: int | None = None
        self._start = time.monotonic()
        self._last_edit = 0.0
        self._first_render = True

    def update(self, received: int, total: int | None) -> str | None:
        self._received = received
        if total is not None:
            self._total = total
        now = time.monotonic()
        if self._first_render:
            self._first_render = False
            self._last_edit = now
            return None
        if now - self._last_edit >= _PROGRESS_INTERVAL:
            self._last_edit = now
            elapsed = now - self._start
            return _render_progress("download", self._received, self._total, elapsed)
        return None

    def final(self) -> str:
        elapsed = time.monotonic() - self._start
        return _render_progress("download", self._received, self._total, elapsed)


class _UploadProgressTracker:
    """Tracks upload progress, throttles edits to 2 min."""

    def __init__(self, total: int | None):
        self._total = total
        self._received = 0
        self._start = time.monotonic()
        self._last_edit = 0.0
        self._first_render = True

    def update(self, sent: int, total: int | None) -> str | None:
        self._received = sent
        if total is not None:
            self._total = total
        now = time.monotonic()
        if self._first_render:
            self._first_render = False
            self._last_edit = now
            return None
        if now - self._last_edit >= _PROGRESS_INTERVAL:
            self._last_edit = now
            elapsed = now - self._start
            return _render_progress("upload", self._received, self._total, elapsed)
        return None

    def final(self) -> str:
        elapsed = time.monotonic() - self._start
        return _render_progress("upload", self._received, self._total, elapsed)


async def execute_link_save(client, owner_id: int, link: str, tz_str: str, progress_msg=None) -> str:
    """Resolve a Telegram link, download the media, deep-save it.

    Reuses execute_save's payload/insert logic.  If progress_msg is provided
    (a Telethon message object), it is edited at most every 2 minutes with
    real progress from Telethon's download/upload callbacks.
    """
    logger.info("[LINK_SAVE] resolving link: %s", link)
    channel, chat_id, msg_id = parse_telegram_link(link)
    if not channel and not chat_id:
        logger.warning("[LINK_SAVE] invalid telegram link: %s", link)
        return "❌ Could not parse link. Use https://t.me/channel/123 or https://t.me/c/123/456"

    target_msg = None
    try:
        if chat_id:
            logger.info("[LINK_SAVE] fetching source message...")
            target_msg = await client.get_messages(chat_id, ids=msg_id)
        else:
            logger.info("[LINK_SAVE] resolving username '%s'...", channel)
            entity = await client.get_entity(channel)
            logger.info("[LINK_SAVE] fetching source message...")
            target_msg = await client.get_messages(entity, ids=msg_id)
        logger.info("[LINK_SAVE] source message fetched: msg_id=%s chat_id=%s",
                    getattr(target_msg, "id", None), getattr(target_msg, "chat_id", None))
    except Exception as exc:
        logger.error("[LINK_SAVE] fetch source message failed: %s", exc, exc_info=True)
        return f"❌ Could not resolve link: {exc}"

    if target_msg is None:
        logger.warning("[LINK_SAVE] source message not found at link")
        return "❌ Message not found at that link."

    try:
        has_media = target_msg.media is not None
        logger.info("[LINK_SAVE] media detected: has_media=%s media_type=%s",
                    has_media, type(target_msg.media).__name__)
    except Exception as exc:
        logger.error("[LINK_SAVE] media detection failed: %s", exc, exc_info=True)
        return f"❌ Media detection failed: {exc}"

    if not target_msg.media:
        return "❌ The linked message has no downloadable media."

    try:
        save_code = await db_client.get_next_save_code()
        logger.info("[LINK_SAVE] save code generated: %s", save_code)
    except Exception as exc:
        logger.error("[LINK_SAVE] save code generation failed: %s", exc, exc_info=True)
        return f"❌ Save code generation failed: {exc}"

    tz = _get_tz(tz_str)
    now = datetime.now(tz)

    sender_name = "Unknown"
    sender_id = target_msg.sender_id or 0
    try:
        sender = await target_msg.get_sender()
        if sender:
            parts = [
                getattr(sender, "first_name", "") or "",
                getattr(sender, "last_name", "") or "",
            ]
            sender_name = " ".join(p for p in parts if p).strip() or str(sender_id)
        logger.info("[LINK_SAVE] sender resolved: name=%s id=%s", sender_name, sender_id)
    except Exception as exc:
        logger.error("[LINK_SAVE] sender resolution failed: %s", exc, exc_info=True)

    origin_chat_id = target_msg.chat_id
    origin_msg_id = target_msg.id

    mime_type = None
    file_size = None
    file_name = None
    file_id = None

    try:
        media = target_msg.media
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
        logger.info("[LINK_SAVE] media metadata extracted: mime=%s size=%s file=%s file_id=%s",
                    mime_type, file_size, file_name, file_id)
    except Exception as exc:
        logger.error("[LINK_SAVE] media metadata extraction failed: %s", exc, exc_info=True)
        return f"❌ Media metadata extraction failed: {exc}"

    media_type = detect_media_type(mime_type)
    if not file_name:
        file_name = generate_filename(media, mime_type, save_code)
    tags = build_tags(media_type, now)

    try:
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
        logger.info("[LINK_SAVE] caption built")
    except Exception as exc:
        logger.error("[LINK_SAVE] caption build failed: %s", exc, exc_info=True)
        return f"❌ Caption build failed: {exc}"

    from backend.helper.client import get_client as _get_helper

    tmp_dir = tempfile.mkdtemp(prefix="lifeos_dl_")
    tmp_path = os.path.join(tmp_dir, file_name)
    sent = None
    try:
        download_client = client
        download_msg = target_msg

        helper = _get_helper()
        if helper and helper.is_connected():
            try:
                logger.info("[LINK_SAVE] helper bot available, fetching message with helper...")
                if chat_id:
                    h_msg = await helper.get_messages(chat_id, ids=msg_id)
                else:
                    h_entity = await helper.get_entity(channel)
                    h_msg = await helper.get_messages(h_entity, ids=msg_id)
                if h_msg is not None:
                    download_client = helper
                    download_msg = h_msg
                    logger.info("[LINK_SAVE] helper bot fetched message, will use helper for download")
                else:
                    logger.warning("[LINK_SAVE] helper bot returned None message, falling back to self")
            except Exception as exc:
                logger.warning("[LINK_SAVE] helper bot fetch failed, falling back to self: %s", exc, exc_info=True)
        else:
            logger.info("[LINK_SAVE] helper bot not available, using self account for download")

        logger.info("[LINK_SAVE] download started: %s -> %s", save_code, tmp_path)

        dl_tracker = _ProgressTracker()

        def _on_download(received, total):
            text = dl_tracker.update(received, total)
            if text and progress_msg:
                try:
                    asyncio.get_event_loop().create_task(progress_msg.edit(text))
                except Exception:
                    pass

        t0 = asyncio.get_event_loop().time()
        result_path = await download_client.download_media(
            download_msg, file=tmp_path, progress_callback=_on_download,
        )
        record_event("save", "download_media", (asyncio.get_event_loop().time() - t0) * 1000, "SUCCESS")
        logger.info("[LINK_SAVE] download complete: path=%s", result_path)

        if result_path is None or not os.path.exists(tmp_path) or os.path.getsize(tmp_path) == 0:
            logger.error("[LINK_SAVE] download produced empty or missing file")
            return "❌ Download produced an empty file."

        actual_size = os.path.getsize(tmp_path)
        logger.info("[LINK_SAVE] downloaded file size: %s bytes", actual_size)

        logger.info("[LINK_SAVE] upload started: %s", save_code)
        ul_tracker = _UploadProgressTracker(file_size or actual_size)

        def _on_upload(sent_bytes, total):
            text = ul_tracker.update(sent_bytes, total)
            if text and progress_msg:
                try:
                    asyncio.get_event_loop().create_task(progress_msg.edit(text))
                except Exception:
                    pass

        try:
            t1 = asyncio.get_event_loop().time()
            sent = await client.send_file(
                "me",
                tmp_path,
                caption=caption,
                force_document=False,
                progress_callback=_on_upload,
            )
            record_event("save", "send_file", (asyncio.get_event_loop().time() - t1) * 1000, "SUCCESS")
            logger.info("[LINK_SAVE] upload complete: saved_chat_id=%s saved_msg_id=%s",
                        getattr(sent, "chat_id", None), getattr(sent, "id", None))
        except Exception as exc:
            logger.error("[LINK_SAVE] upload failed: %s", exc, exc_info=True)
            record_event("save", "send_file", 0, "ERROR", str(exc))
            return f"❌ Upload failed: {exc}"

    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.error("[LINK_SAVE] download failed: %s", exc, exc_info=True)
        record_event("save", "download_media", 0, "ERROR", str(exc))
        return f"❌ Download failed: {exc}"
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        logger.info("[LINK_SAVE] temp file cleaned up: %s", tmp_path)

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
    try:
        logger.info("[LINK_SAVE] database insert started: %s", save_code)
        inserted = db_client.insert_save(payload)
        logger.info("[LINK_SAVE] database insert complete: %s inserted=%s", save_code, inserted is not None)
    except Exception as exc:
        logger.error("[LINK_SAVE] database insert failed: %s", exc, exc_info=True)
        inserted = None

    try:
        await db_client.log(owner_id, "INFO", f"Saved D {save_code} (link)", {
            "save_code": save_code,
            "origin_chat_id": origin_chat_id,
            "origin_msg_id": origin_msg_id,
        })
    except Exception as exc:
        logger.error("[LINK_SAVE] db log write failed: %s", exc, exc_info=True)

    logger.info("[LINK_SAVE] completed: %s", save_code)
    return build_confirmation(save_code, "d", media_type, file_name)


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
