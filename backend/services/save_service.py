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

Owner-supplied metadata (a display name and semantic tags) is represented
exactly once, by ``SaveMetadata``: both adapters build that same object and
hand it to ``execute_save``, so the normalization rules below cannot drift
between the manual and the AI path.
"""
import asyncio
import logging
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
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


# ── owner-supplied saved-item metadata ──

MAX_DISPLAY_NAME_CHARS = 120
MAX_SAVE_TAGS = 10
MAX_TAG_CHARS = 40
MAX_FILE_NAME_CHARS = 120

# A file name becomes a REAL Telegram document filename and a temporary upload
# path, so a separator or a control character can never be part of it.
_FORBIDDEN_FILE_NAME_CHARS = frozenset("/\\")

# The caption's own section labels. ``📄`` is written by ``build_caption`` for
# the Telegram file name; the system hashtag line (#saved…) marks the END of
# the LifeOS metadata block, so everything after it is the original source
# text and is never touched by a metadata rewrite.
FILE_NAME_PREFIX = "📄"
ADDITIONAL_TAGS_PREFIX = "🏷 Additional tags"
_SYSTEM_TAG_MARKER = "#saved"
# Only the caption's own header line ("{icon} {code} · DEEP") contains this,
# and a saved DOCUMENT's header icon IS ``FILE_NAME_PREFIX`` — so the header
# must be excluded when looking for the file-name section, or a rewrite would
# replace the header instead of the filename.
_HEADER_MARKER = "· DEEP"


def normalize_display_name(value) -> str | None:
    """The ONE display-name rule, shared by every Save surface.

    ``None`` and an empty/whitespace-only name both mean "the owner gave no
    name" and store NULL — the column is never filled with ``''`` and never
    with an invented value. Internal whitespace is collapsed so a pasted name
    cannot smuggle newlines or runs of spaces into a panel, and a name over the
    bound is REFUSED rather than silently truncated.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("the name must be text")
    name = " ".join(value.split())
    if not name:
        return None
    if len(name) > MAX_DISPLAY_NAME_CHARS:
        raise ValueError(
            f"the name is {len(name)} characters — the limit is {MAX_DISPLAY_NAME_CHARS}"
        )
    return name


def normalize_tags(values) -> tuple[str, ...]:
    """The ONE tag rule, shared by every Save surface.

    Deterministic and non-semantic: trim, collapse internal whitespace, drop
    empty entries, dedupe case-insensitively (the first spelling wins, so the
    stored casing is the owner's), bound each tag's length and the count.
    Nothing is translated, folded to another script, or invented — folding
    Arabic/Persian letter variants for MATCHING belongs to the retrieval phase,
    not to storage. A bare string is ONE tag; it is never split per character.
    """
    if values is None:
        return ()
    if isinstance(values, str):
        values = [values]
    tags: list[str] = []
    seen: set[str] = set()
    for raw in values:
        if not isinstance(raw, str):
            raise ValueError("every tag must be text")
        tag = " ".join(raw.split())
        if not tag:
            continue
        if len(tag) > MAX_TAG_CHARS:
            raise ValueError(
                f"tag {tag!r} is {len(tag)} characters — the limit is {MAX_TAG_CHARS}"
            )
        key = tag.casefold()
        if key in seen:
            continue
        seen.add(key)
        tags.append(tag)
    if len(tags) > MAX_SAVE_TAGS:
        raise ValueError(f"{len(tags)} tags — the limit is {MAX_SAVE_TAGS}")
    return tuple(tags)


def normalize_file_name(value) -> str:
    """The ONE rule for the actual Telegram file name (Save V2 sync).

    Distinct from ``normalize_display_name``: that value is the item's LOGICAL
    label, this one becomes the document's real filename. Trim + collapse
    whitespace, refuse an empty name and any path separator, and REFUSE rather
    than truncate a name over the bound. The owner's exact spelling —
    extension included — is preserved: nothing is appended or rewritten.
    """
    if not isinstance(value, str):
        raise ValueError("the file name must be text")
    name = " ".join(value.split())
    if not name:
        raise ValueError("send a file name")
    if any(ch in _FORBIDDEN_FILE_NAME_CHARS for ch in name):
        raise ValueError("the file name cannot contain / or \\")
    if len(name) > MAX_FILE_NAME_CHARS:
        raise ValueError(
            f"the file name is {len(name)} characters — the limit is {MAX_FILE_NAME_CHARS}"
        )
    return name


def render_additional_tags(tags) -> str | None:
    """The caption's Additional-tags section — the OWNER's tags, or ``None``.

    A labelled section of its own, so an owner tag can never be confused with
    the generated system hashtags on the next line. Empty/blank entries render
    nothing at all (the section is then omitted instead of emitted empty).
    """
    values = [str(tag).strip() for tag in (tags or []) if str(tag or "").strip()]
    if not values:
        return None
    return f"{ADDITIONAL_TAGS_PREFIX}: " + " ".join(f"#{tag}" for tag in values)


def _split_metadata_block(caption: str) -> tuple[list[str], list[str]]:
    """Split a caption into (LifeOS metadata block, everything after it).

    The system hashtag line is the boundary: it is the LAST line
    ``build_caption`` emits, and ``_append_original_text`` appends the source
    text after it. A metadata rewrite therefore can never reach the original
    text. A caption without that line (an item saved before this format) is
    treated as metadata only.
    """
    lines = str(caption or "").split("\n")
    for index, line in enumerate(lines):
        if line.strip().startswith(_SYSTEM_TAG_MARKER):
            return lines[:index], lines[index:]
    return lines, []


def _set_metadata_line(caption: str, prefix: str, rendered: str | None, *, skip=None) -> str:
    """Set, replace or remove the ONE metadata line starting with ``prefix``.

    Deterministic and bounded: every other line — the other metadata fields,
    the system hashtags and the original source text — is preserved byte for
    byte. ``rendered=None`` removes the line when it exists. ``skip`` excludes
    lines that can never be the section being set (a caption header shares the
    saved document's icon, so it starts with the same prefix).
    """
    head, tail = _split_metadata_block(caption)
    index = next(
        (
            i for i, line in enumerate(head)
            if line.strip().startswith(prefix) and not (skip and skip(line))
        ),
        None,
    )
    if index is not None:
        if rendered is None:
            head.pop(index)
        else:
            head[index] = rendered
    elif rendered is not None:
        head.append(rendered)
    return "\n".join(head + tail)


def with_additional_tags(caption: str, tags) -> str:
    """Return ``caption`` with ONLY its Additional-tags section set to ``tags``.

    The saved Telegram caption's synchronization primitive: the owner's tags
    are replaced/added/removed as one section, while the system hashtags and
    the original source text stay exactly as they were.
    """
    return _set_metadata_line(caption, ADDITIONAL_TAGS_PREFIX, render_additional_tags(tags))


def with_file_name(caption: str, file_name: str | None) -> str:
    """Return ``caption`` with ONLY its file-name line set to ``file_name``."""
    name = str(file_name or "").strip()
    rendered = f"{FILE_NAME_PREFIX} {name}" if name else None
    return _set_metadata_line(
        caption, FILE_NAME_PREFIX, rendered, skip=lambda line: _HEADER_MARKER in line
    )


@dataclass(frozen=True)
class SaveMetadata:
    """Owner-supplied metadata for one saved item — the shared Save contract.

    The Glass Save panel and the AI ``SaveTool`` are two thin adapters over
    ``execute_save``; both hand it THIS object, so the rules above live once,
    there is no second metadata type and no second writer of
    ``saved_items.display_name`` / ``saved_items.tags``.

    A save with no metadata is the default and stays valid: the name is NULL
    and the tags are empty — never generated, never guessed.
    """

    display_name: str | None = None
    tags: tuple[str, ...] = ()

    @classmethod
    def from_raw(cls, display_name=None, tags=None) -> "SaveMetadata":
        """Build from adapter input (an AI argument or a panel's text).

        Raises ``ValueError`` with an owner-readable reason when the input
        cannot be honoured; the caller decides what to tell the owner.
        """
        return cls(normalize_display_name(display_name), normalize_tags(tags))

    def insert_fields(self) -> dict:
        """The ``saved_items`` fields this metadata contributes to the payload."""
        fields: dict = {"tags": list(self.tags)}
        if self.display_name is not None:
            # display_name arrives in its own additive migration. Omitting the
            # key when there is no name keeps every metadata-less save working
            # against a database that has not applied it yet — PostgREST
            # rejects an INSERT that names a column it does not know.
            fields["display_name"] = self.display_name
        return fields


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


def caption_hashtags(media_type: str, dt: datetime) -> list[str]:
    """The hashtag line rendered INTO the caption — never into ``tags``.

    These values used to be persisted as the row's ``tags``. Since Save V2 the
    column belongs to the owner alone, so this line is presentation only: a
    saved message keeps exactly the look it had, while an owner who supplied no
    tags now gets ``tags = '{}'`` instead of five invented ones.
    """
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
    owner_tags=(),
) -> str:
    """Compact, information-dense LifeOS caption.

    The model name is dominant, the metadata is one line each, and the
    original source text is appended afterwards by ``_append_original_text``.

    ``tags`` are the generated system hashtags (presentation only);
    ``owner_tags`` are the owner's own semantic tags and render as their own
    labelled section, never merged with the hashtag line.
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
        lines.append(f"{FILE_NAME_PREFIX} {file_name}")
    additional = render_additional_tags(owner_tags)
    if additional:
        lines.append(additional)
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


def _sender_display_name(entity) -> str:
    """Display name of the entity Telegram reported as the SOURCE sender.

    Same precedence the AI conversation layer uses for sender names
    (``telegram_context._entity_name``): ``first_name last_name`` →
    ``username`` → ``title``. The ``title`` branch is not a chat fallback —
    a channel post carries no ``from_id``, so Telethon reports the CHANNEL
    itself as the sender (``Message._finish_init``), and that channel's
    ``title`` IS the sender's name. The origin chat's own title is never
    substituted for a sender that could not be resolved.
    """
    first = getattr(entity, "first_name", "") or ""
    last = getattr(entity, "last_name", "") or ""
    name = f"{first} {last}".strip()
    if name:
        return name
    return getattr(entity, "username", "") or getattr(entity, "title", "") or ""


async def _resolve_sender(reply_msg) -> tuple[str, int]:
    """Resolve the SOURCE message's sender name + id (best effort).

    The name describes the sender of the saved message, never the chat it
    came from: the previous implementation only handled user-shaped entities
    and degraded to ``str(sender_id)``, so a channel-sourced save persisted
    the channel's raw numeric identity as the "Sender" shown in previews.
    """
    sender_id = getattr(reply_msg, "sender_id", None) or 0
    try:
        sender = await reply_msg.get_sender()
    except Exception:
        sender = None
    sender_name = ""
    if sender is not None and getattr(sender, "title", None):
        # Channel-shaped sender: a signed post names the person who actually
        # sent it, which is more specific than the channel itself.
        sender_name = (getattr(reply_msg, "post_author", None) or "").strip()
    if not sender_name:
        sender_name = _sender_display_name(sender) if sender is not None else ""
    if not sender_name:
        # Last resort is the sender's OWN numeric id — never the chat title.
        sender_name = f"User {sender_id}" if sender_id else "Unknown"
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


async def execute_save(
    client,
    owner_id: int,
    reply_msg,
    tz_str: str,
    *,
    metadata: SaveMetadata | None = None,
) -> str:
    """Deep Save — the single authoritative save pipeline.

    Deep Save downloads the source content and uploads it again as a NEW
    Saved Messages message. It NEVER forwards, under any circumstance:

    - text-only source → a new ``send_message`` text message
    - media source → ``download_media`` → validate → ``send_file``

    A download or upload failure is an honest Deep Save failure. The DB
    record is written only after the Telegram operation succeeded.

    ``metadata`` is the owner's optional display name and tags (see
    ``SaveMetadata``). Omitted/empty metadata reproduces the previous behavior
    for the row's identity and Telegram side exactly; the only difference is
    that the ``tags`` column now stays empty instead of receiving invented
    hashtags. Metadata that cannot be honoured is refused BEFORE any transfer —
    it is never silently truncated or discarded.
    """
    if metadata is None:
        metadata = SaveMetadata()
    else:
        try:
            metadata = SaveMetadata.from_raw(metadata.display_name, list(metadata.tags))
        except ValueError as exc:
            return f"⚠️ Nothing was saved: {exc}"

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
        "[SAVE] owner=%s media=%s save_code=%s file_name=%s mime=%s size=%s file_id=%s "
        "display_name=%s tags=%s",
        owner_id, media is not None, save_code, file_name, mime_type, file_size, file_id,
        metadata.display_name, len(metadata.tags),
    )

    max_bytes = settings_service.max_deep_save_mb() * 1024 * 1024
    if file_size and file_size > max_bytes:
        mb = file_size / (1024 * 1024)
        limit_mb = settings_service.max_deep_save_mb()
        return f"⚠️ File is {mb:.1f} MB — exceeds the {limit_mb} MB deep-save limit."

    caption_tags = caption_hashtags(media_type, now)
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
            tags=caption_tags,
            owner_tags=metadata.tags,
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
        "caption": caption,
        "owner_id": owner_id,
        "created_at": now.isoformat(),
    }
    payload.update(metadata.insert_fields())

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


async def execute_link_save(
    client,
    owner_id: int,
    link: str,
    tz_str: str,
    *,
    metadata: SaveMetadata | None = None,
) -> str:
    """Resolve a Telegram link and Deep-Save the linked message.

    This is the same Deep Save pipeline as ``execute_save`` — the only
    difference is the source resolution (a t.me link instead of a reply), so it
    takes and forwards the same optional ``SaveMetadata`` contract.
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

    return await execute_save(client, owner_id, target_msg, tz_str, metadata=metadata)
