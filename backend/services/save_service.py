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
import inspect
import logging
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from datetime import datetime
from typing import Awaitable, Callable

from telethon.tl.types import (
    MessageMediaDocument,
    MessageMediaPhoto,
    DocumentAttributeFilename,
)

from backend.bio.engine import _get_tz
from backend.db import client as db_client
from backend.diagnostics import record_event
from backend.helper import symbols as sym
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

# Media-type glyphs come from the ONE central registry so the caption, the
# DB statistics and the retrieve list never drift apart.
_MEDIA_ICON = {
    "Photo": sym.MEDIA_PHOTO,
    "Video": sym.MEDIA_VIDEO,
    "Animation": sym.MEDIA_ANIMATION,
    "Audio": sym.MEDIA_AUDIO,
    "Voice": sym.MEDIA_VOICE,
    "Sticker": sym.MEDIA_STICKER,
    "Document": sym.MEDIA_DOCUMENT,
    "Text": sym.MEDIA_TEXT,
    "Unknown": sym.MEDIA_UNKNOWN,
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

# The caption's own section labels. ``sym.FILE_NAME`` is written by
# ``build_caption`` for the Telegram file name; the system hashtag line
# (#saved…) marks the END of the LifeOS metadata block, so everything after it
# is the original source text and is never touched by a metadata rewrite.
FILE_NAME_PREFIX = sym.FILE_NAME
ADDITIONAL_TAGS_PREFIX = f"{sym.META_TAGS} Additional tags"
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
    return _MEDIA_ICON.get(media_type or "Unknown", sym.MEDIA_UNKNOWN)


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
        f"{sym.META_SENDER} {sender}",
        f"{sym.META_TIME} {dt.strftime('%Y-%m-%d %H:%M')}",
        f"{sym.META_ID} {chat_id}/{msg_id}",
        f"{sym.META_MEDIA} {media_type} · {size_str}" + (f" · {mime}" if mime else ""),
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


class SaveOutcome(str):
    """The Deep Save result text, carrying the new item's structured identity.

    Subclasses ``str`` so every existing caller (handlers, panels, tests)
    keeps receiving exactly the text it received before, while the AI tool
    boundary can expose ``save_code`` through ``ToolResult.data`` instead of
    re-parsing human-readable confirmation text. ``saved`` is True only when
    the whole pipeline really persisted the item.

    For an album, ``saved`` describes the WHOLE album (every item persisted),
    ``save_code`` is empty unless exactly one item was saved, and ``media_type``
    is empty — the per-item codes and types are listed in the result text and
    each has its own ``saved_items`` row.
    """

    save_code: str
    saved: bool
    media_type: str

    def __new__(
        cls, text: str, *, save_code: str = "", saved: bool = False, media_type: str = ""
    ) -> "SaveOutcome":
        outcome = super().__new__(cls, text)
        outcome.save_code = str(save_code or "")
        outcome.saved = bool(saved)
        outcome.media_type = str(media_type or "")
        return outcome


# ── transfer progress (service → UI, never the reverse) ──


@dataclass(frozen=True)
class SaveProgressEvent:
    """One real transfer observation for the caller's own status surface.

    ``completed_bytes`` / ``total_bytes`` come straight from Telethon's
    ``progress_callback`` (or from the phase boundary), never from elapsed
    time. ``total_bytes`` is ``None`` when the transfer cannot report a size,
    and ``finished`` marks the phase boundary so the caller can render a
    truthful 100% even when no callback ever fired.
    """

    phase: str                 # "download" | "upload"
    completed_bytes: int = 0
    total_bytes: int | None = None
    item_index: int = 1
    item_count: int = 1
    finished: bool = False


ProgressCallback = Callable[[SaveProgressEvent], Awaitable[None]]


async def _emit_progress(progress: ProgressCallback | None, event: SaveProgressEvent) -> None:
    """Report one progress event; a reporting failure never aborts a transfer."""
    if progress is None:
        return
    try:
        result = progress(event)
        if inspect.isawaitable(result):
            await result
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 — a UI failure must not fail the save
        logger.warning("[SAVE] progress reporting failed: %s", exc)


def _transfer_progress_callback(
    progress: ProgressCallback | None,
    phase: str,
    item_index: int,
    item_count: int,
    total_bytes: int | None,
) -> Callable | None:
    """Telethon's ``progress_callback`` adapter (``(current, total)``).

    Pinned Telethon 1.43.0 awaits an awaitable callback result for both
    ``download_media`` and ``send_file``, so this coroutine is invoked from the
    transfer itself with the REAL byte counts.
    """
    if progress is None:
        return None

    async def _callback(current, total=None) -> None:
        await _emit_progress(
            progress,
            SaveProgressEvent(
                phase=phase,
                completed_bytes=int(current or 0),
                total_bytes=int(total) if total else total_bytes,
                item_index=item_index,
                item_count=item_count,
            ),
        )

    return _callback


# ── view-once / expiring media ──

#: Telegram's documented view-once marker (``2**31 - 1``). It is a SENTINEL,
#: not a duration: any other positive TTL is a secret-chat self-destruct timer.
VIEW_ONCE_TTL_SECONDS = 2147483647
EPHEMERAL_VIEW_ONCE = "view_once"
EPHEMERAL_EXPIRING = "expiring"


def media_ttl_seconds(media) -> int | None:
    """The media's own self-destruct TTL, or ``None`` for ordinary media."""
    ttl = getattr(media, "ttl_seconds", None)
    if isinstance(ttl, bool) or not isinstance(ttl, int):
        return None
    return ttl if ttl > 0 else None


def ephemeral_media_kind(media) -> str | None:
    """``view_once`` / ``expiring`` / ``None``, decided from Telegram's TTL only.

    The view-once marker is exactly ``VIEW_ONCE_TTL_SECONDS``; every OTHER
    positive TTL is an expiry timer. Both mean the media is ephemeral by the
    sender's/Telegram's design, so neither is ever downloaded, re-uploaded or
    recorded — no attribute guessing and no workaround.
    """
    ttl = media_ttl_seconds(media)
    if ttl is None:
        return None
    return EPHEMERAL_VIEW_ONCE if ttl == VIEW_ONCE_TTL_SECONDS else EPHEMERAL_EXPIRING


def ephemeral_refusal(media) -> str:
    """The deterministic, pre-transfer refusal text for ephemeral media."""
    kind = ephemeral_media_kind(media)
    ttl = media_ttl_seconds(media)
    if kind == EPHEMERAL_VIEW_ONCE:
        return (
            f"{sym.STATE_FAILED} Nothing was saved: this is view-once media. "
            "Telegram marks it as one-time viewing, so LifeOS does not download "
            "or re-upload it."
        )
    return (
        f"{sym.STATE_FAILED} Nothing was saved: this media expires after {ttl}s. "
        "LifeOS does not download or re-upload ephemeral media."
    )


# ── albums (grouped media) ──

#: Telegram's own hard cap on the number of items in one album. A window of
#: this many ids on each side of the selected message therefore always contains
#: every member of the album it belongs to — deterministic and bounded.
ALBUM_MAX_ITEMS = 10


def album_group_id(message) -> int | None:
    """The message's album id, or ``None`` when it is not part of an album."""
    grouped = getattr(message, "grouped_id", None)
    if isinstance(grouped, bool) or not isinstance(grouped, int) or grouped <= 0:
        return None
    return grouped


async def resolve_album_messages(client, target) -> tuple[list, str]:
    """Resolve the FULL album the selected message belongs to.

    Returns ``(members, error)``. A non-album target resolves to ``[target]``
    with no fetch at all. An album member is resolved by reading the bounded
    id window around it and keeping ONLY messages whose ``grouped_id`` and chat
    match the target — so an unrelated neighbouring message is never included
    and no member is ever listed twice. Members are ordered by message id.

    A resolution failure returns an honest error instead of silently saving
    the single selected item: quietly saving one of an album is exactly the
    behavior this path must not have.
    """
    grouped = album_group_id(target)
    if grouped is None:
        return [target], ""

    base_id = getattr(target, "id", 0) or 0
    chat_id = getattr(target, "chat_id", None)
    if not base_id or chat_id is None:
        return [target], ""

    window = [
        message_id
        for message_id in range(
            base_id - (ALBUM_MAX_ITEMS - 1), base_id + ALBUM_MAX_ITEMS
        )
        if message_id > 0
    ]
    try:
        fetched = await client.get_messages(chat_id, ids=window)
    except Exception as exc:  # noqa: BLE001 — honest resolution failure
        logger.warning(
            "[SAVE] album resolution failed chat=%s msg=%s: %s", chat_id, base_id, exc
        )
        return [], f"the album could not be resolved ({type(exc).__name__})"

    if fetched is None:
        fetched = []
    elif not isinstance(fetched, (list, tuple)):
        fetched = [fetched]

    members: list = []
    seen: set[int] = set()
    for message in fetched:
        if message is None:
            continue
        message_id = getattr(message, "id", None)
        if not isinstance(message_id, int) or message_id in seen:
            continue
        if album_group_id(message) != grouped:
            continue
        if getattr(message, "chat_id", None) != chat_id:
            continue
        seen.add(message_id)
        members.append(message)

    if base_id not in seen:
        members.append(target)
    members.sort(key=lambda message: getattr(message, "id", 0) or 0)
    logger.info(
        "[SAVE] album resolved chat=%s grouped_id=%s selected=%s members=%d",
        chat_id, grouped, base_id, len(members),
    )
    return members, ""


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


def _one_line(value, limit: int = 180) -> str:
    """Collapse a result string into one bounded line (album summaries)."""
    line = " ".join(str(value or "").split())
    return line[:limit] + ("…" if len(line) > limit else "")


def _album_outcome(results: list) -> SaveOutcome:
    """Aggregate per-item outcomes — honest about partial success.

    The album is reported as saved only when EVERY resolved item persisted; a
    partial run states how many items were saved and names the failing ones.
    Each successful item keeps its own code and metadata row.
    """
    total = len(results)
    saved = [result for result in results if getattr(result, "saved", False)]
    codes = [getattr(result, "save_code", "") for result in saved]
    codes = [code for code in codes if code]

    lines: list[str] = []
    if saved and len(saved) == total:
        lines.append(f"{sym.STATE_COMPLETED} **Album saved — {total} of {total} items**")
    elif saved:
        lines.append(f"{sym.STATE_WARNING} **Album saved — {len(saved)} of {total} items**")
    else:
        lines.append(f"{sym.STATE_FAILED} **Album save failed — 0 of {total} items**")
    lines.append("")

    for index, result in enumerate(results, 1):
        if getattr(result, "saved", False):
            code = getattr(result, "save_code", "") or "—"
            media_type = getattr(result, "media_type", "") or "Item"
            lines.append(f"{sym.OP_SAVE} `{code}` · {media_type}")
        else:
            lines.append(f"{sym.STATE_FAILED} Item {index} — {_one_line(result)}")

    fully_saved = bool(total) and len(saved) == total
    return SaveOutcome(
        "\n".join(lines),
        save_code=codes[0] if len(codes) == 1 else "",
        saved=fully_saved,
    )


async def _deep_save_one(
    client,
    owner_id: int,
    reply_msg,
    tz_str: str,
    *,
    metadata: SaveMetadata,
    progress: ProgressCallback | None = None,
    item_index: int = 1,
    item_count: int = 1,
) -> SaveOutcome:
    """Deep-Save ONE source message — the per-item half of ``execute_save``.

    Its Telegram behavior is exactly the previously single-item pipeline
    (ephemeral refusal → download → validate → upload as a NEW message → DB);
    the only additions are the item's ordinal (for progress) and the optional
    progress reporter. The temporary directory is removed on every exit path.
    """
    media = getattr(reply_msg, "media", None)

    # Ephemeral (view-once / expiring) media is refused BEFORE any transfer,
    # any save code, any caption and any database write.
    if ephemeral_media_kind(media) is not None:
        logger.info(
            "[SAVE] refused ephemeral media kind=%s chat=%s msg=%s",
            ephemeral_media_kind(media),
            getattr(reply_msg, "chat_id", None),
            getattr(reply_msg, "id", None),
        )
        return SaveOutcome(ephemeral_refusal(media), saved=False)

    save_code = await db_client.get_next_save_code()
    now = datetime.now(_get_tz(tz_str))
    sender_name, sender_id = await _resolve_sender(reply_msg)
    origin_chat_id = reply_msg.chat_id
    origin_msg_id = reply_msg.id

    mime_type, file_size, file_name, file_id, media_type = _extract_source_media(reply_msg, save_code)

    # Text-only source → a NEW text message (no media transfer, no forward).
    if media is None:
        original_text = (reply_msg.text or "").strip()
        if not original_text:
            return SaveOutcome(
                f"{sym.STATE_WARNING} Replied message has no text or media to save.",
                saved=False,
            )
        media_type = "Text"
        mime_type = None
        file_name = None
        file_id = None
        file_size = len(original_text.encode("utf-8"))

    logger.info(
        "[SAVE] owner=%s media=%s save_code=%s item=%s/%s file_name=%s mime=%s size=%s "
        "file_id=%s display_name=%s tags=%s",
        owner_id, media is not None, save_code, item_index, item_count, file_name,
        mime_type, file_size, file_id, metadata.display_name, len(metadata.tags),
    )

    max_bytes = settings_service.max_deep_save_mb() * 1024 * 1024
    if file_size and file_size > max_bytes:
        mb = file_size / (1024 * 1024)
        limit_mb = settings_service.max_deep_save_mb()
        return SaveOutcome(
            f"{sym.STATE_WARNING} File is {mb:.1f} MB — exceeds the {limit_mb} MB deep-save limit.",
            saved=False,
        )

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
            return SaveOutcome(
                f"{sym.STATE_FAILED} Deep Save failed: text could not be uploaded to Saved Messages ({exc})",
                saved=False,
            )
    else:
        # Isolated temp storage per item; removed on every exit path.
        tmp_dir = tempfile.mkdtemp(prefix="lifeos_dl_")
        tmp_path = os.path.join(tmp_dir, os.path.basename(file_name or "file.bin"))
        try:
            # STAGE 1 — physically download the source media.
            await _emit_progress(
                progress,
                SaveProgressEvent(
                    phase="download", completed_bytes=0, total_bytes=file_size,
                    item_index=item_index, item_count=item_count,
                ),
            )
            download_callback = _transfer_progress_callback(
                progress, "download", item_index, item_count, file_size
            )
            try:
                t0 = asyncio.get_event_loop().time()
                download_kwargs = {}
                if download_callback is not None:
                    download_kwargs["progress_callback"] = download_callback
                await client.download_media(reply_msg, file=tmp_path, **download_kwargs)
                record_event("save", "download_media", (asyncio.get_event_loop().time() - t0) * 1000, "SUCCESS")
            except Exception as exc:
                logger.error("deep save download failed: %s", exc)
                record_event("save", "download_media", 0, "ERROR", str(exc))
                return SaveOutcome(
                    f"{sym.STATE_FAILED} Deep Save failed: unable to download the source message ({exc})",
                    saved=False,
                )

            # Validate the download before uploading anything.
            if not os.path.exists(tmp_path):
                return SaveOutcome(
                    f"{sym.STATE_FAILED} Deep Save failed: downloaded file is missing.",
                    saved=False,
                )
            actual_size = os.path.getsize(tmp_path)
            if actual_size == 0:
                return SaveOutcome(
                    f"{sym.STATE_FAILED} Deep Save failed: downloaded file is empty.",
                    saved=False,
                )

            await _emit_progress(
                progress,
                SaveProgressEvent(
                    phase="download", completed_bytes=actual_size,
                    total_bytes=file_size or actual_size, item_index=item_index,
                    item_count=item_count, finished=True,
                ),
            )

            # STAGE 2 — upload the downloaded content as a NEW message.
            await _emit_progress(
                progress,
                SaveProgressEvent(
                    phase="upload", completed_bytes=0, total_bytes=actual_size,
                    item_index=item_index, item_count=item_count,
                ),
            )
            upload_callback = _transfer_progress_callback(
                progress, "upload", item_index, item_count, actual_size
            )
            try:
                t1 = asyncio.get_event_loop().time()
                upload_kwargs = _upload_kwargs_for_media(media, mime_type, file_name)
                if upload_callback is not None:
                    upload_kwargs["progress_callback"] = upload_callback
                sent = await client.send_file(
                    "me",
                    tmp_path,
                    caption=caption,
                    **upload_kwargs,
                )
                record_event("save", "send_file", (asyncio.get_event_loop().time() - t1) * 1000, "SUCCESS")
            except Exception as exc:
                logger.error("deep save upload failed: %s", exc)
                record_event("save", "send_file", 0, "ERROR", str(exc))
                return SaveOutcome(
                    f"{sym.STATE_FAILED} Deep Save failed: downloaded media could not be uploaded to Saved Messages ({exc})",
                    saved=False,
                )

            await _emit_progress(
                progress,
                SaveProgressEvent(
                    phase="upload", completed_bytes=actual_size,
                    total_bytes=actual_size, item_index=item_index,
                    item_count=item_count, finished=True,
                ),
            )
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
        return SaveOutcome(
            f"{sym.STATE_WARNING} Uploaded to Saved Messages, but the database record failed for `{save_code}`.",
            saved=False,
        )

    await db_client.log(owner_id, "INFO", f"Saved D {save_code}", {
        "save_code": save_code,
        "origin_chat_id": origin_chat_id,
        "origin_msg_id": origin_msg_id,
    })
    logger.info("[SAVE] completed: %s", save_code)
    return SaveOutcome(
        build_confirmation(save_code, media_type, file_name),
        save_code=save_code,
        saved=True,
        media_type=media_type,
    )


async def execute_save(
    client,
    owner_id: int,
    reply_msg,
    tz_str: str,
    *,
    metadata: SaveMetadata | None = None,
    progress: ProgressCallback | None = None,
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

    An album source is resolved to its full, bounded member set (see
    ``resolve_album_messages``) and saved item by item through the SAME
    per-item pipeline; partial success is reported as partial. Ephemeral media
    (view-once and expiring) is refused before any transfer.

    ``progress`` is an optional observer of the REAL transfer byte counts. It
    knows nothing about the Glass UI: the caller decides what to render.
    """
    if metadata is None:
        metadata = SaveMetadata()
    else:
        try:
            metadata = SaveMetadata.from_raw(metadata.display_name, list(metadata.tags))
        except ValueError as exc:
            return SaveOutcome(f"{sym.STATE_WARNING} Nothing was saved: {exc}", saved=False)

    members, album_error = await resolve_album_messages(client, reply_msg)
    if album_error:
        return SaveOutcome(
            f"{sym.STATE_FAILED} Deep Save failed: {album_error}", saved=False
        )

    if len(members) == 1:
        return await _deep_save_one(
            client, owner_id, reply_msg, tz_str,
            metadata=metadata, progress=progress, item_index=1, item_count=1,
        )

    results: list[SaveOutcome] = []
    for index, member in enumerate(members, 1):
        results.append(
            await _deep_save_one(
                client, owner_id, member, tz_str,
                metadata=metadata, progress=progress,
                item_index=index, item_count=len(members),
            )
        )

    outcome = _album_outcome(results)
    logger.info(
        "[SAVE] album finished: %d/%d items saved",
        sum(1 for result in results if result.saved), len(results),
    )
    return outcome


async def execute_link_save(
    client,
    owner_id: int,
    link: str,
    tz_str: str,
    *,
    metadata: SaveMetadata | None = None,
    progress: ProgressCallback | None = None,
) -> str:
    """Resolve a Telegram link and Deep-Save the linked message.

    This is the same Deep Save pipeline as ``execute_save`` — the only
    difference is the source resolution (a t.me link instead of a reply), so it
    takes and forwards the same optional ``SaveMetadata`` and progress contracts.
    """
    logger.info("[LINK_SAVE] resolving link: %s", link)
    channel, chat_id, msg_id = parse_telegram_link(link)
    if not channel and not chat_id:
        logger.warning("[LINK_SAVE] invalid telegram link: %s", link)
        return f"{sym.STATE_FAILED} Could not parse link. Use https://t.me/channel/123 or https://t.me/c/123/456"

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
        return f"{sym.STATE_FAILED} Could not resolve link: {exc}"

    if target_msg is None:
        logger.warning("[LINK_SAVE] source message not found at link")
        return f"{sym.STATE_FAILED} Message not found at that link."

    return await execute_save(
        client, owner_id, target_msg, tz_str, metadata=metadata, progress=progress
    )
