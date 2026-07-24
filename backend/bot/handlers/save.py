"""
Save Engine
  .save f / .s f  — Forward save: metadata log + forward to Saved-Messages, no download.
  .save d / .s d  — Deep save: download → validate → upload → DB record → cleanup.
  .save            — Inline panel: choose Forward or Deep save.

Both the long (.save) and short (.s) aliases share one execution path.

Safety guarantees:
  - Deep save enforces a hard size limit before any download is attempted.
  - BytesIO buffer is always closed in a finally block — zero memory leaks.
  - Empty-download guard rejects corrupted or zero-byte transfers.
  - Edit-first policy: every response edits the triggering message in-place.
  - Media type is preserved: forward mode keeps the original Telegram media;
    deep mode re-uploads with force_document=False for photos/videos so
    Telegram reconstructs the native media type instead of a generic file.

Inline Mode:
  - .save (no args) triggers an inline panel with Forward/Deep buttons.
  - The reply message reference is stored in tmp_context before triggering.
  - Callback handler retrieves the context and executes the save.
  - .save f / .save d with args still work as edit-in-place (backward compat).
"""
import asyncio
import io
import logging
from datetime import datetime

from telethon import events
from telethon.tl.types import (
    MessageMediaDocument,
    MessageMediaPhoto,
    DocumentAttributeFilename,
)

from backend.bot.handlers.guard import is_owner
from backend.db import client as db_client
from backend.bio.engine import _get_tz
from backend.diagnostics import record_event
from backend.helper import (
    InlinePanelBuilder,
    register_panel,
    register_inline_builder,
    send_inline_panel,
    set_context,
    get_context,
)
from backend.helper.client import get_client

logger = logging.getLogger(__name__)

_MAX_DEEP_BYTES = 50 * 1024 * 1024

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


def _detect_media_type(mime: str | None) -> str:
    if not mime:
        return "Unknown"
    return _MEDIA_TYPE_MAP.get(mime, "Document")


def _media_icon(media_type: str | None) -> str:
    return _MEDIA_ICON.get(media_type or "Unknown", "📦")


def _extract_file_name(media) -> str | None:
    if isinstance(media, MessageMediaDocument):
        doc = media.document
        for attr in getattr(doc, "attributes", []):
            if isinstance(attr, DocumentAttributeFilename) and attr.file_name:
                return attr.file_name
            fn = getattr(attr, "file_name", None)
            if fn:
                return fn
    return None


def _generate_filename(media, mime_type: str | None, save_code: str) -> str:
    if isinstance(media, MessageMediaPhoto):
        return f"photo_{save_code}.jpg"
    ext = _MIME_EXT.get(mime_type or "", ".bin")
    return f"{save_code}{ext}"


def _build_tags(media_type: str, dt: datetime) -> list[str]:
    mt = media_type.lower().replace(" ", "_")
    return [
        "#saved",
        f"#saved_{mt}",
        f"#saved_{dt.year}",
        f"#saved_{dt.year}_{dt.month:02d}",
        f"#saved_{dt.year}_{dt.month:02d}_{dt.day}",
    ]


def _build_caption(
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
        f"📦 DeepSaved\n"
        f"🎙 Sender: {sender}\n"
        f"💬 Chat ID: `{chat_id}`\n"
        f"🆔 Msg ID: `{msg_id}`\n"
        f"🕒 Time: {dt.strftime('%Y-%m-%d %H:%M:%S %Z')}\n"
        f"🖼 Type: {media_type}\n"
        f"🧾 MIME: {mime or '—'}\n"
        f"📦 Size: {size_str}\n"
        f"📁 File: {file_name or '—'}\n"
        f"🏷 Tags: {' '.join(tags)}"
    )


def _unwrap_forward(result) -> object | None:
    if result is None:
        return None
    return result[0] if isinstance(result, list) else result


def _build_confirmation(
    save_code: str,
    mode: str,
    media_type: str,
    file_name: str | None,
) -> str:
    icon = _media_icon(media_type)
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


async def _execute_save(client, owner_id: int, reply_msg, mode: str, tz_str: str) -> str:
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
        file_name = _extract_file_name(media)
        file_id = str(getattr(doc, "id", ""))
    elif isinstance(media, MessageMediaPhoto):
        mime_type = "image/jpeg"
        photo = media.photo
        if hasattr(photo, "sizes") and photo.sizes:
            file_size = getattr(photo.sizes[-1], "size", None)
        file_id = str(getattr(photo, "id", ""))

    media_type = _detect_media_type(mime_type)
    if not file_name:
        file_name = _generate_filename(media, mime_type, save_code)
    tags = _build_tags(media_type, now)

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

        try:
            db_client.insert_save({
                "save_code": save_code,
                "short_code": save_code,
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
                "file_name": file_name,
                "tags": tags,
                "caption": None,
                "owner_id": owner_id,
                "created_at": now.isoformat(),
            })
        except Exception as exc:
            logger.warning("save DB insert failed: %s", exc)

        await db_client.log(owner_id, "INFO", f"Saved F {save_code}", {
            "save_code": save_code,
            "origin_chat_id": origin_chat_id,
            "origin_msg_id": origin_msg_id,
        })
        return _build_confirmation(save_code, mode, media_type, file_name)

    else:
        if not media:
            return "⚠️ Replied message has no downloadable media."

        if file_size and file_size > _MAX_DEEP_BYTES:
            mb = file_size / (1024 * 1024)
            return (
                f"⚠️ File is {mb:.1f} MB — exceeds the "
                f"{_MAX_DEEP_BYTES // (1024 * 1024)} MB deep-save limit.\n"
                "Use `.save f` for a forward save instead."
            )

        caption = _build_caption(
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

        try:
            db_client.insert_save({
                "save_code": save_code,
                "short_code": save_code,
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
                "file_name": file_name,
                "tags": tags,
                "caption": caption,
                "owner_id": owner_id,
                "created_at": now.isoformat(),
            })
        except Exception as exc:
            logger.warning("save DB insert failed: %s", exc)

        await db_client.log(owner_id, "INFO", f"Saved D {save_code}", {
            "save_code": save_code,
            "origin_chat_id": origin_chat_id,
            "origin_msg_id": origin_msg_id,
        })
        return _build_confirmation(save_code, mode, media_type, file_name)


async def _save_panel_handler(event, extra: str) -> None:
    from backend.helper.inline_engine import _self_client, _owner_id
    from backend.helper.tmp_context import get_context

    client = _self_client
    owner_id = _owner_id

    if extra.startswith("exec:"):
        mode = extra[5:]
        ctx = get_context(owner_id)
        if not ctx or "reply_msg_id" not in ctx:
            await event.edit("⚠️ Reply context expired. Use `.save` while replying to a message.")
            return

        reply_chat_id = ctx.get("reply_chat_id")
        reply_msg_id = ctx.get("reply_msg_id")
        tz_str = ctx.get("tz_str", "UTC")

        try:
            reply_msg = await client.get_messages(reply_chat_id, ids=reply_msg_id)
        except Exception as exc:
            await event.edit(f"❌ Could not fetch reply message: {exc}")
            return

        if reply_msg is None:
            await event.edit("⚠️ Reply message no longer exists.")
            return

        result = await _execute_save(client, owner_id, reply_msg, mode, tz_str)
        builder = InlinePanelBuilder()
        builder.add_row("Close", "panel:help:close")
        await event.edit(result, buttons=builder.build())
        return

    await event.edit("⚠️ Unknown save action.")


async def _save_inline_builder(event, extra: str) -> list:
    from telethon.tl import types
    text = "**Save Engine**\n\nChoose a save mode:"
    builder = InlinePanelBuilder()
    builder.add_row("📦 Forward Save", "panel:save:exec:f")
    builder.add_row("⬇️ Deep Save", "panel:save:exec:d")
    builder.add_row("Close", "panel:help:close")
    buttons = builder.build()
    msg = types.InputBotInlineMessageText(
        message=text,
        reply_markup=types.ReplyInlineMarkup(rows=buttons) if buttons else None,
    )
    result = types.InputBotInlineResult(
        id="0",
        type="article",
        title="Save Engine",
        send_message=msg,
    )
    return [result]


def register(client, owner_id: int, tz_str: str) -> None:
    register_panel("save", _save_panel_handler)
    register_inline_builder("save", _save_inline_builder)

    @client.on(events.NewMessage(outgoing=True, pattern=r"^\.(?:save|s) (f|d)$"))
    async def save_cmd(event) -> None:
        if not is_owner(event, owner_id):
            return

        mode = event.pattern_match.group(1)
        reply = await event.message.get_reply_message()
        if not reply:
            await event.edit("⚠️ Reply to a message to save it.")
            return

        result = await _execute_save(client, owner_id, reply, mode, tz_str)
        await event.edit(result)

    @client.on(events.NewMessage(outgoing=True, pattern=r"^\.(?:save|s)$"))
    async def save_panel_cmd(event) -> None:
        if not is_owner(event, owner_id):
            return

        reply = await event.message.get_reply_message()
        if not reply:
            await event.edit("⚠️ Reply to a message to save it.")
            return

        helper = get_client()
        if helper is None:
            await event.edit("⚠️ Inline mode requires the helper bot (BOT_TOKEN).")
            return

        set_context(owner_id, {
            "reply_chat_id": reply.chat_id,
            "reply_msg_id": reply.id,
            "tz_str": tz_str,
        })

        try:
            await event.delete()
            await send_inline_panel(client, event.chat_id, "save")
        except Exception as exc:
            logger.warning("save inline send failed: %s", exc)
            try:
                await event.edit(f"⚠️ Inline panel failed: {exc}")
            except Exception:
                pass
