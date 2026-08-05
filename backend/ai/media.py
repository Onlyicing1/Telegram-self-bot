"""
Media classifier — detects and labels media types from Telethon messages.

Used by the AI reply-mode handler to classify replied messages so the
engine receives structured media metadata without analyzing the content.

Classification is based on Telethon's ``Message.media`` attribute and
``Message.document``/``Message.photo`` fields. No download is performed.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class MediaInfo:
    """Structured metadata about a message's media.

    Attributes:
        has_media:     Whether the message contains any media at all.
        media_type:    Human-readable label: Photo, Video, Voice, Audio,
                       Document, Sticker, Animation, GIF, Unknown, or empty.
        mime_type:     MIME type string (e.g. ``"image/jpeg"``) or empty.
        file_size:     File size in bytes, or 0.
        file_name:     Original filename if available, or empty.
        caption:       Message caption (for media messages) or empty.
        text:          Message text (for text messages) or empty.
        is_text:       True if the message has no media (pure text).
    """

    has_media: bool = False
    media_type: str = ""
    mime_type: str = ""
    file_size: int = 0
    file_name: str = ""
    caption: str = ""
    text: str = ""
    is_text: bool = True

    def as_context_text(self) -> str:
        """Render a compact text summary for the prompt builder."""
        if self.is_text:
            return f"Text: {self.text[:500]}" if self.text else "Text: (empty)"
        parts = [f"Media Type: {self.media_type}"]
        if self.mime_type:
            parts.append(f"MIME: {self.mime_type}")
        if self.file_size:
            parts.append(f"Size: {self._fmt_size(self.file_size)}")
        if self.file_name:
            parts.append(f"Filename: {self.file_name}")
        if self.caption:
            parts.append(f"Caption: {self.caption[:500]}")
        if self.text:
            parts.append(f"Text: {self.text[:500]}")
        return "\n".join(parts)

    @staticmethod
    def _fmt_size(n: int) -> str:
        if n < 1024:
            return f"{n} B"
        if n < 1024 * 1024:
            return f"{n / 1024:.1f} KB"
        return f"{n / (1024 * 1024):.1f} MB"


def classify_message(msg: Any) -> MediaInfo:
    """Classify a Telethon message into structured ``MediaInfo``.

    No download, no network calls. Pure attribute inspection.
    """
    text = ""
    try:
        text = msg.message or ""
    except Exception:
        pass

    caption = ""
    try:
        caption = msg.message or ""
    except Exception:
        pass

    media = getattr(msg, "media", None)
    if media is None:
        return MediaInfo(
            has_media=False,
            media_type="",
            text=text,
            caption="",
            is_text=True,
        )

    media_type = "Unknown"
    mime_type = ""
    file_size = 0
    file_name = ""

    # ── Photo ──
    try:
        from telethon.tl.types import MessageMediaPhoto
        if isinstance(media, MessageMediaPhoto):
            media_type = "Photo"
            mime_type = "image/jpeg"
            photo = getattr(media, "photo", None)
            if photo:
                sizes = getattr(photo, "sizes", None)
                if sizes:
                    biggest = sizes[-1]
                    file_size = getattr(biggest, "size", 0) or 0
    except Exception:
        pass

    # ── Document-based media (video, voice, audio, sticker, animation, GIF, document) ──
    try:
        from telethon.tl.types import MessageMediaDocument
        if isinstance(media, MessageMediaDocument):
            doc = getattr(media, "document", None)
            if doc:
                mime_type = getattr(doc, "mime_type", "") or ""
                file_size = getattr(doc, "size", 0) or 0

                attrs = getattr(doc, "attributes", []) or []
                is_sticker = False
                is_animation = False
                is_voice = False
                is_audio = False
                is_video = False
                file_name = ""

                for attr in attrs:
                    attr_cls = type(attr).__name__
                    if attr_cls == "DocumentAttributeSticker":
                        is_sticker = True
                    elif attr_cls == "DocumentAttributeAnimated":
                        is_animation = True
                    elif attr_cls == "DocumentAttributeAudio":
                        if getattr(attr, "voice", False):
                            is_voice = True
                        else:
                            is_audio = True
                    elif attr_cls == "DocumentAttributeVideo":
                        is_video = True
                    elif attr_cls == "DocumentAttributeFilename":
                        file_name = getattr(attr, "file_name", "") or ""

                if is_sticker:
                    media_type = "Sticker"
                elif is_animation:
                    media_type = "Animation"
                elif is_voice:
                    media_type = "Voice"
                elif is_audio:
                    media_type = "Audio"
                elif is_video:
                    media_type = "Video"
                elif mime_type == "image/gif":
                    media_type = "GIF"
                else:
                    media_type = "Document"

                if not file_name:
                    file_name = getattr(doc, "filename", "") or ""
    except Exception:
        pass

    # ── Web page / URL preview ──
    try:
        from telethon.tl.types import MessageMediaWebPage
        if isinstance(media, MessageMediaWebPage):
            media_type = "WebPage"
            mime_type = "text/html"
    except Exception:
        pass

    # ── Contact ──
    try:
        from telethon.tl.types import MessageMediaContact
        if isinstance(media, MessageMediaContact):
            media_type = "Contact"
            mime_type = "text/x-vcard"
    except Exception:
        pass

    # ── Poll ──
    try:
        from telethon.tl.types import MessageMediaPoll
        if isinstance(media, MessageMediaPoll):
            media_type = "Poll"
    except Exception:
        pass

    # ── Geo ──
    try:
        from telethon.tl.types import MessageMediaGeo
        if isinstance(media, MessageMediaGeo):
            media_type = "Location"
    except Exception:
        pass

    return MediaInfo(
        has_media=True,
        media_type=media_type,
        mime_type=mime_type,
        file_size=file_size,
        file_name=file_name,
        caption=caption,
        text=text,
        is_text=False,
    )
