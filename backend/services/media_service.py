"""
Media service — the controlled, provider-independent media processing boundary.

This is the ONE place Telegram media is downloaded for processing. It exists so
that no AI layer ever handles raw media: the trusted runtime resolves WHICH
message carries the media, this service transfers it under explicit limits,
validates the result, and returns a normalized, serializable representation
that holds no Telegram/Telethon object, no filesystem handle and no
conversational state.

Layering (mirrors ``backend/services/history_service.py``):

  * Telegram access goes exclusively through ``backend/telegram_api``. The
    transfer is the facade's bounded ``download_media`` (``guarded_await``);
    this module never imports Telethon and never calls the client's download
    directly.
  * The media TARGET is resolved by :func:`resolve_media_message` from the
    runtime's own chat/message ids — one bounded fetch, no search, no fallback
    to another message.
  * Media TYPE detection is NOT re-implemented: ``backend/ai/media.py::
    classify_message`` stays the single classifier, and its taxonomy decides
    which assets this boundary may touch.
  * Limits are finite and fail closed: Telegram's declared size is checked
    before the transfer, the transfer is bounded by one finite timeout, the
    downloaded file must exist and be non-empty, and the temporary directory is
    removed on every exit path (success, failure, cancellation).
  * Resolution is deterministic and lives OUTSIDE the model. The caller hands
    this service the message the runtime already resolved — exactly the way
    ``save_service.execute_save`` receives the replied-to message. There is no
    recency, sender, caption or "last media" heuristic here, and no path in
    which a model chooses a Telegram message or a destination.
  * Normalization is provider-independent. Nothing in this module knows about a
    provider, a prompt, a model or a chat: it produces data. If the content is
    ever handed to the LLM (a later phase's decision), the only model-facing
    surface is ``MediaAnalysis.as_context_text()``, which renders the normalized
    result and deliberately omits the caption and every Telegram identifier.

Failure contract: hard failures — nothing to resolve, no media on the resolved
message, not a downloadable asset, oversized, transfer failure, malformed or
empty download — are raised as ``MediaError``. A media type this phase has no
extraction capability for returns an honest ``MediaStatus.UNSUPPORTED`` result
instead of fabricated content.

M1 scope: text-bearing documents (``text/*`` and the common text-shaped
``application/*`` MIME types) are transferred and read with the standard
library. Every other type is reported ``UNSUPPORTED`` WITHOUT being transferred
— fail-closed, so a type this phase cannot turn into content is never partially
processed. No OCR, speech-to-text, vision, PDF or video stack is claimed or
required.
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
import tempfile
from dataclasses import dataclass
from enum import Enum
from typing import Any

from backend.ai.media import MediaInfo, classify_message
from backend.ai.prompt.budget import DEFAULT_MAX_CONTEXT_TOKENS
from backend.runtime.operation_watchdog import guarded_await
from backend.services import settings_service
from backend.telegram_api import media as telegram_media
from backend.telegram_api.exceptions import TelegramAPIError

logger = logging.getLogger(__name__)

#: The one authority for the transfer's time bound (the facade's ceiling).
MEDIA_DOWNLOAD_TIMEOUT_S = telegram_media.MEDIA_DOWNLOAD_TIMEOUT_S

#: Bounded timeout for resolving ONE message by id. A single-message fetch is a
#: short RPC (the same order as the facade's 30s short-call bound), not a
#: transfer, so it gets its own finite ceiling.
MEDIA_RESOLVE_TIMEOUT_S = 30.0

#: Media types this boundary may TRANSFER. The taxonomy is the existing
#: classifier's (``backend/ai/media.py``); ``WebPage``/``Contact``/``Poll``/
#: ``Location``/``Unknown`` are labelled there but are not downloadable
#: assets, so they are never fetched — they are reported unsupported.
DOWNLOADABLE_MEDIA_TYPES = frozenset({
    "Photo", "Voice", "Audio", "Document", "Video", "Sticker", "Animation", "GIF",
})

#: MIME prefixes that can be turned into text with the standard library only.
TEXT_MIME_PREFIXES = ("text/",)

#: Text-shaped MIME types that carry no ``text/`` prefix but are still text.
TEXT_MIME_TYPES = frozenset({
    "application/json",
    "application/x-ndjson",
    "application/xml",
    "application/yaml",
    "application/x-yaml",
    "application/javascript",
    "application/x-sh",
    "application/sql",
    "application/csv",
})

#: Ceiling for the text this phase may extract from ONE asset, derived from the
#: project's own prompt budget: ``prompt/budget.py`` documents ≈4 characters per
#: token for English text and caps conversation context at
#: ``DEFAULT_MAX_CONTEXT_TOKENS``, so normalized media text can never be larger
#: than the context budget it would eventually have to fit inside.
MAX_EXTRACTED_CHARS = DEFAULT_MAX_CONTEXT_TOKENS * 4

#: Fallback temp filename when Telegram's (untrusted) name is unusable.
_FALLBACK_FILE_NAME = "media.bin"
_MAX_FILE_NAME_CHARS = 120


class MediaError(Exception):
    """Raised when media cannot be resolved, transferred or validated honestly."""


class MediaStatus(str, Enum):
    """Honest outcome of one media normalization.

    ``EXTRACTED``   — ``content`` carries text this phase genuinely produced.
    ``UNSUPPORTED`` — the asset exists but this phase has no capability for it;
                      ``reason`` says why and ``content`` stays empty.
    """

    EXTRACTED = "extracted"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True)
class MediaAnalysis:
    """Normalized, serializable result of one media normalization.

    Contains NO Telethon object, no client reference, no filesystem handle, no
    reply context and no sender/chat conversation state.

    ``content`` is the ONLY field intended for a future model step. ``caption``
    and the source identifiers are application metadata: ``as_context_text()``
    deliberately does not render them, so a media answer can never carry
    Telegram conversational context into the model.

    Attributes:
        media_type:        Classifier label (``"Photo"``, ``"Document"``, ...).
        mime_type:         MIME type as Telegram declared it, or empty.
        file_size:         Bytes actually transferred (0 when nothing was).
        file_name:         Telegram-supplied filename, or empty.
        status:            ``MediaStatus`` value.
        content:           Extracted text, empty unless ``EXTRACTED``.
        reason:            Why the asset was not processed (``UNSUPPORTED``).
        caption:           The media message's own caption, application data.
        source_chat_id:    Chat the media lives in (0 when unknown).
        source_message_id: Telegram message id of the media.
        truncated:         True when the extracted text hit the size ceiling.
    """

    media_type: str
    mime_type: str = ""
    file_size: int = 0
    file_name: str = ""
    status: str = MediaStatus.UNSUPPORTED
    content: str = ""
    reason: str = ""
    caption: str = ""
    source_chat_id: int = 0
    source_message_id: int = 0
    truncated: bool = False

    @property
    def has_content(self) -> bool:
        """True when this analysis carries genuinely extracted text."""
        return self.status == MediaStatus.EXTRACTED and bool(self.content)

    def as_dict(self) -> dict[str, Any]:
        """Deterministic plain-data view of this analysis."""
        return {
            "media_type": self.media_type,
            "mime_type": self.mime_type,
            "file_size": self.file_size,
            "file_name": self.file_name,
            "status": self.status,
            "content": self.content,
            "reason": self.reason,
            "caption": self.caption,
            "source_chat_id": self.source_chat_id,
            "source_message_id": self.source_message_id,
            "truncated": self.truncated,
        }

    def as_context_text(self) -> str:
        """Render the normalized result as plain text for a future model step.

        Renders ONLY the processed media result: no caption, no sender, no chat
        id, no message id, no surrounding conversation. This is the single
        model-facing surface of the media boundary, so the zero-context rule is
        enforced in one place.
        """
        parts = ["[Media Content]", f"Type: {self.media_type or 'Unknown'}"]
        if self.mime_type:
            parts.append(f"MIME: {self.mime_type}")
        if self.file_size:
            parts.append(f"Size: {_format_bytes(self.file_size)}")
        parts.append(f"Status: {self.status}")
        if self.reason:
            parts.append(f"Reason: {self.reason}")
        if self.content:
            parts.append(f"Content:\n{self.content}")
            if self.truncated:
                parts.append("(content truncated at the processing limit)")
        return "\n".join(parts)


def is_downloadable(media_type: str) -> bool:
    """True when ``media_type`` is an asset this boundary may transfer."""
    return str(media_type or "") in DOWNLOADABLE_MEDIA_TYPES


def is_extractable_mime(mime_type: str) -> bool:
    """True when this phase can turn ``mime_type`` into text without new deps."""
    value = str(mime_type or "").strip().lower()
    if not value:
        return False
    if value in TEXT_MIME_TYPES:
        return True
    return value.startswith(TEXT_MIME_PREFIXES)


def max_download_bytes() -> int:
    """The project's single established media size bound, in bytes.

    Reuses the Deep Save limit (``settings_service.max_deep_save_mb()``) rather
    than introducing a second, contradictable media size constant.
    """
    try:
        megabytes = int(settings_service.max_deep_save_mb())
    except Exception:  # noqa: BLE001 — a settings failure must not remove the bound
        megabytes = 50
    return max(1, megabytes) * 1024 * 1024


def download_timeout(timeout_s: Any = None) -> float:
    """Finite transfer bound, never above the facade's ceiling.

    Mirrors the history AI service's use of the caller's own envelope: a
    tighter caller stays tighter, and no caller can remove the bound.
    """
    return _bounded(timeout_s, MEDIA_DOWNLOAD_TIMEOUT_S)


def _bounded(timeout_s: Any, ceiling: float) -> float:
    if timeout_s is None:
        return ceiling
    try:
        value = float(timeout_s)
    except (TypeError, ValueError):
        return ceiling
    if value <= 0:
        return ceiling
    return min(value, ceiling)


def _resolve_client(source: Any) -> Any:
    """Normalize a ``TelegramAPI`` facade or the client it wraps to the client.

    The facade's ``client`` property is documented as service-layer use only,
    which is exactly what this module is.
    """
    if source is None:
        raise MediaError("No Telegram client available for media processing.")
    wrapped = getattr(source, "client", None)
    if wrapped is not None and hasattr(wrapped, "download_media"):
        return wrapped
    if hasattr(source, "download_media"):
        return source
    raise MediaError("Unsupported Telegram source for media processing.")


def _safe_temp_path(tmp_dir: str, file_name: Any) -> str:
    """A destination inside ``tmp_dir`` built from an UNTRUSTED Telegram name.

    Telegram filenames are sender-controlled: they may be absolute, contain
    separators, contain ``..`` or NUL, or be empty. Only the basename survives,
    and the result is verified to stay inside ``tmp_dir`` before it is returned.
    """
    root = os.path.realpath(tmp_dir)
    name = os.path.basename(str(file_name or "").replace("\\", "/").replace("\x00", "")).strip()
    if not name or name in {".", ".."}:
        name = _FALLBACK_FILE_NAME
    name = name[:_MAX_FILE_NAME_CHARS]
    candidate = os.path.join(root, name)
    if os.path.dirname(os.path.realpath(candidate)) != root:
        candidate = os.path.join(root, _FALLBACK_FILE_NAME)
    return candidate


def _read_text(path: str, limit: int) -> tuple[str, bool]:
    """Read at most ``limit`` bytes of a text asset; never more.

    Bounded and decode-safe: an unavailable or mis-declared encoding cannot
    raise, and a file larger than the ceiling is truncated honestly rather than
    partially processed silently.
    """
    with open(path, "rb") as handle:
        raw = handle.read(limit + 1)
    truncated = len(raw) > limit
    if truncated:
        raw = raw[:limit]
    return raw.decode("utf-8", errors="replace"), truncated


def _format_bytes(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / (1024 * 1024):.1f} MB"


def _unsupported(info: MediaInfo, message: Any, reason: str) -> MediaAnalysis:
    return MediaAnalysis(
        media_type=info.media_type,
        mime_type=info.mime_type,
        file_size=0,
        file_name=info.file_name,
        status=MediaStatus.UNSUPPORTED.value,
        reason=reason,
        caption=info.caption or "",
        source_chat_id=_coerce_int(getattr(message, "chat_id", 0)),
        source_message_id=_coerce_int(getattr(message, "id", 0)),
    )


def _coerce_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


async def resolve_media_message(
    source: Any,
    *,
    chat_id: Any,
    message_id: Any,
    timeout_s: Any = None,
) -> Any:
    """Fetch the ONE message the TRUSTED RUNTIME identified, by its own ids.

    Resolution is deterministic and lives outside the model: ``chat_id`` and
    ``message_id`` come from the request scope (the replied-to message, or the
    triggering message itself), never from model output, text, recency, sender
    or history. Nothing here searches for a message, and nothing falls back to
    another one — unreadable ids produce an honest ``MediaError``.

    The fetched object stays inside this boundary: it is consumed by
    :func:`analyze_media` and is never returned to an AI layer, a prompt or a
    provider.
    """
    client = _resolve_client(source)
    target_chat = _coerce_int(chat_id)
    target_message = _coerce_int(message_id)
    if not target_chat:
        raise MediaError("A concrete chat id is required to resolve media.")
    if not target_message:
        raise MediaError("A concrete message id is required to resolve media.")

    bound = _bounded(timeout_s, MEDIA_RESOLVE_TIMEOUT_S)
    try:
        message = await guarded_await(
            client.get_messages(target_chat, ids=target_message),
            name="media:get_messages",
            timeout=bound,
        )
    except asyncio.CancelledError:
        raise
    except asyncio.TimeoutError as exc:
        logger.warning("MEDIA_RESOLVE_FAILED chat_id=%s message_id=%s error=timeout",
                       target_chat, target_message)
        raise MediaError(
            f"Resolving the media message timed out after {bound:g}s."
        ) from exc
    except Exception as exc:
        logger.warning("MEDIA_RESOLVE_FAILED chat_id=%s message_id=%s error=%s",
                       target_chat, target_message, type(exc).__name__)
        raise MediaError(f"Resolving the media message failed: {exc}") from exc

    if message is None:
        raise MediaError("The media message could not be found.")
    return message


async def analyze_media(
    source: Any,
    owner_id: int,
    message: Any,
    *,
    timeout_s: Any = None,
    size_limit_bytes: int | None = None,
) -> MediaAnalysis:
    """Resolve, transfer, validate and normalize ONE media message.

    Args:
        source:            A ``TelegramAPI`` facade or the Telethon client it
                           wraps (service-layer use).
        owner_id:          Owner the request belongs to — used for tracing only;
                           media processing is read-only and persists nothing.
        message:           The media message the trusted runtime ALREADY
                           resolved for this request. Never model output.
        timeout_s:         Optional tighter bound for the transfer.
        size_limit_bytes:  Optional tighter size limit (defaults to the
                           project's media limit).

    Returns:
        A ``MediaAnalysis`` that either carries extracted text or states
        honestly why this phase did not process the asset.

    Raises:
        MediaError:  nothing to resolve, no media, oversized, transfer failed or
                     timed out, or the download was missing/empty/malformed.
        asyncio.CancelledError: re-raised unchanged (after cleanup).
    """
    if message is None:
        raise MediaError("No media message was resolved for this request.")

    info = classify_message(message)
    if not info.has_media:
        raise MediaError("The resolved message carries no media.")

    if not is_downloadable(info.media_type):
        analysis = _unsupported(
            info, message,
            f"{info.media_type or 'Unknown'} is not a downloadable media asset.",
        )
        logger.info(
            "MEDIA_ANALYZE owner=%s type=%s status=%s", owner_id,
            info.media_type or "Unknown", analysis.status,
        )
        return analysis

    if not is_extractable_mime(info.mime_type):
        analysis = _unsupported(
            info, message,
            f"No local extraction capability for {info.media_type} "
            f"({info.mime_type or 'unknown type'}) in this phase.",
        )
        logger.info(
            "MEDIA_ANALYZE owner=%s type=%s mime=%s status=%s", owner_id,
            info.media_type, info.mime_type or "-", analysis.status,
        )
        return analysis

    limit = size_limit_bytes if size_limit_bytes and size_limit_bytes > 0 else max_download_bytes()
    if info.file_size and info.file_size > limit:
        raise MediaError(
            f"Media is {_format_bytes(info.file_size)} — exceeds the "
            f"{_format_bytes(limit)} processing limit."
        )

    client = _resolve_client(source)
    bound = download_timeout(timeout_s)
    tmp_dir = tempfile.mkdtemp(prefix="lifeos_media_")
    try:
        destination = _safe_temp_path(tmp_dir, info.file_name)
        try:
            result = await telegram_media.download_media(
                client, message, file_path=destination, timeout=bound,
            )
        except asyncio.CancelledError:
            raise
        except TelegramAPIError as exc:
            logger.warning(
                "MEDIA_DOWNLOAD_FAILED owner=%s type=%s error=%s",
                owner_id, info.media_type, type(exc).__name__,
            )
            raise MediaError(f"Media download failed: {exc}") from exc

        if result is None:
            raise MediaError("Telegram returned no media for the resolved message.")

        path = result if isinstance(result, str) and os.path.exists(result) else destination
        if not os.path.exists(path):
            raise MediaError("Downloaded media is missing.")
        size = os.path.getsize(path)
        if size == 0:
            raise MediaError("Downloaded media is empty.")
        if size > limit:
            raise MediaError(
                f"Downloaded media is {_format_bytes(size)} — exceeds the "
                f"{_format_bytes(limit)} processing limit."
            )

        content, truncated = _read_text(path, MAX_EXTRACTED_CHARS)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    analysis = MediaAnalysis(
        media_type=info.media_type,
        mime_type=info.mime_type,
        file_size=size,
        file_name=info.file_name,
        status=MediaStatus.EXTRACTED.value,
        content=content,
        reason="" if content else "The downloaded text asset was empty.",
        caption=info.caption or "",
        source_chat_id=_coerce_int(getattr(message, "chat_id", 0)),
        source_message_id=_coerce_int(getattr(message, "id", 0)),
        truncated=truncated,
    )
    logger.info(
        "MEDIA_ANALYZE owner=%s type=%s mime=%s size=%s status=%s truncated=%s",
        owner_id, info.media_type, info.mime_type or "-", size, analysis.status, truncated,
    )
    return analysis
