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

M1.2 scope: the two container formats above are now extractable as well —
**PDF** (text layer only, through ``pypdf``) and **DOCX** (through the standard
library's ``zipfile`` + ``xml.etree.ElementTree``). Nothing else changed: the
resolve/transfer/validate/cleanup/zero-context/fail-closed contracts are
identical, DOCX is treated as an untrusted archive (only ``word/document.xml``
is read, the archive is never unpacked to disk, and macros, embedded objects and
external links are never opened), and every added bound is finite (pages, XML
elements, archive entry bytes, extracted characters). A container whose
signature contradicts its declared MIME, an encrypted PDF, or a payload that
cannot be parsed is a HARD failure (``MediaError``) — never fabricated content —
while a parseable container with no extractable text is reported honestly with
empty content.

M1.3 scope: the OCR **boundary** for still images. Nothing about resolution,
transfer, validation, cleanup, the zero-context rule or the fail-closed
contract changes: an image is only ever the message the runtime already
resolved, its payload's own signature must corroborate the declared MIME before
any decode, its declared dimensions must fit a hard bitmap bound (so a small
compressed payload can never be expanded into an unbounded bitmap), recognition
runs off the event loop inside a finite timeout, the normalized text is capped
by the same character ceiling every other extractor uses, and the temporary
directory is removed on every exit path.

Recognition itself is a seam (``OcrEngine``). This phase ships the BOUNDARY and
provisions **no** engine: an image is therefore reported ``UNSUPPORTED``
*without being transferred* until an engine is provisioned, so the project never
silently acquires a heavy native stack, and no image is ever partially
processed. Persian and English text are preserved unchanged by normalization;
recognition QUALITY is a property of whatever engine is later provisioned and
is deliberately not claimed here.

M1.4 scope: the same pattern for **Voice/Audio** — a bounded speech-to-text
boundary. Resolution, transfer, validation, cleanup, the zero-context rule and
the fail-closed contract are unchanged. The additions are the audio containers
this boundary can both corroborate and bound using only the standard library,
the channel/rate/duration guards enforced BEFORE any decode, the transcription
timeout, and normalization into the same character ceiling. Transcription is a
seam (``SttEngine``) and this phase likewise provisions **no** engine: Voice and
Audio are reported ``UNSUPPORTED`` *without being transferred* until one is
provisioned. The engine decision is deferred on measured evidence — every local
candidate evaluated either could not be delivered by ``requirements.txt`` or
measured far outside the project's documented resource budget. No transcript is
ever fabricated, no hosted service is used, and no audio container whose
duration cannot be determined is ever handed to an engine.

M1.8 scope: the duration bound becomes the per-CHUNK ceiling instead of the
ceiling for the whole request. A recording longer than one chunk is divided into
ordered, bounded chunks by ``backend/services/stt_chunking.py`` — OGG pages and
RIFF/WAVE frames only, never an arbitrary byte offset — and transcribed one chunk
at a time through the SAME ``SttEngine`` seam, under an explicit chunk-count and
total-duration cap and inside ONE aggregate deadline. A container that cannot be
divided deterministically (FLAC, a single indivisible OGG page) is refused exactly
as before rather than approximated. Everything else is unchanged: one download
path, one transcript, one ``MediaAnalysis``, one owner-facing response, and the
same zero-context rule — a chunk carries nothing but audio the boundary already
validated.

Scope: Video and GIF are explicitly OUTSIDE Media Processing. Both are refused
deterministically from the classifier's own label (``UNPROCESSABLE_MEDIA_TYPES``)
before any capability check, so neither can reach an extractor, a transfer or
the model; the owner receives the boundary's existing honest UNSUPPORTED result.
"""
from __future__ import annotations

import asyncio
import io
import logging
import os
import shutil
import tempfile
import time
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass
from enum import Enum
from typing import Any, Protocol

from backend.ai.media import MediaInfo, classify_message
from backend.ai.prompt.budget import DEFAULT_MAX_CONTEXT_TOKENS
from backend.runtime.operation_watchdog import guarded_await
from backend.services import settings_service, stt_chunking, stt_fallback
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

#: Media Processing scope. Video is OUT of scope, and so is the animated image
#: the classifier labels ``GIF`` (an ``image/gif`` document) or ``Animation``
#: (``DocumentAttributeAnimated``) — the two shapes a Telegram GIF arrives in.
#: The label is the single existing classifier's own deterministic read of the
#: Telegram metadata, so the refusal is pure data inspection: no model, no
#: download and no per-request heuristic is involved. These types are refused
#: BEFORE any capability check, so they can never reach OCR, speech-to-text,
#: text extraction, the bounded transfer or the model — a GIF is otherwise an
#: ``image/gif`` asset the OCR capability would have accepted.
UNPROCESSABLE_MEDIA_TYPES = frozenset({"Video", "GIF", "Animation"})

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

#: Container format MIMEs this phase can extract text from (M1.2). PDF needs the
#: one added dependency (``pypdf``); DOCX needs only the standard library.
PDF_MIME_TYPES = frozenset({"application/pdf", "application/x-pdf"})

DOCX_MIME_TYPES = frozenset({
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.ms-word.document.macroenabled.12",
})

#: Ceiling for the text this phase may extract from ONE asset, derived from the
#: project's own prompt budget: ``prompt/budget.py`` documents ≈4 characters per
#: token for English text and caps conversation context at
#: ``DEFAULT_MAX_CONTEXT_TOKENS``, so normalized media text can never be larger
#: than the context budget it would eventually have to fit inside.
MAX_EXTRACTED_CHARS = DEFAULT_MAX_CONTEXT_TOKENS * 4

#: Bounds for the container formats (M1.2). Each is finite and enforced at the
#: point of use, so no document can walk unbounded pages or XML elements, read
#: an unbounded archive entry, or grow past ``MAX_EXTRACTED_CHARS``.
MAX_PDF_PAGES = 50
MAX_DOCX_TEXT_ELEMENTS = 5_000
MAX_ARCHIVE_ENTRY_BYTES = 8 * 1024 * 1024

#: M1.3 — still-image MIME types the OCR boundary may process. Only containers
#: whose signature can be corroborated cheaply and deterministically are listed;
#: every other type stays UNSUPPORTED (fail-closed), exactly as in M1/M1.2.
OCR_IMAGE_MIME_TYPES = frozenset({
    "image/jpeg", "image/jpg", "image/png", "image/webp", "image/bmp", "image/gif",
})

#: M1.3 input bound for OCR. The transfer is already gated by the project's
#: media size authority (``max_download_bytes``); this is the tighter ceiling
#: applied in addition for images, and it is enforced BEFORE the transfer.
MAX_OCR_INPUT_BYTES = 8 * 1024 * 1024

#: M1.3 pre-decode guards. The declared dimensions are read straight from the
#: container header, so a payload that would expand into an unbounded bitmap is
#: refused before any decoder (or engine) sees it — the decompression-bomb bound.
MAX_IMAGE_PIXELS = 12_000_000
MAX_IMAGE_SIDE = 10_000

#: M1.3 character ceiling for OCR text, shared with the other extractors so every
#: path produces text bounded by the same project prompt budget.
MAX_OCR_CHARS = MAX_EXTRACTED_CHARS

#: M1.3 wall-clock bound for ONE recognition. Finite, well inside the media
#: request's own envelope, and never applied to an unrelated AI request.
OCR_TIMEOUT_S = 45.0

#: M1.4 — audio MIME types the STT boundary may process. Only containers whose
#: signature AND duration are cheaply and deterministically derivable with the
#: standard library are listed. Every other audio container (MP3, M4A/MP4, WebM,
#: AAC, AC3, ...) stays UNSUPPORTED and is never transferred, rather than being
#: handed to an engine on a guessed duration.
STT_AUDIO_MIME_TYPES = frozenset({
    "audio/ogg", "audio/opus", "application/ogg",
    "audio/wav", "audio/x-wav", "audio/wave", "audio/vnd.wave",
    "audio/flac", "audio/x-flac",
})

#: M1.4 input bound for speech-to-text: tighter than the media size authority
#: (50 MiB) and enforced BEFORE the transfer, exactly like the OCR input bound.
#: Derivation: it must hold ``MAX_STT_DURATION_S`` of the largest stream this
#: phase accepts uncompressed (16 kHz mono 16-bit PCM ≈ 32 KB/s → ≈ 9.6 MiB), so
#: every compressed format fits with headroom while an oversized payload is
#: refused before it is ever downloaded.
MAX_STT_INPUT_BYTES = 20 * 1024 * 1024

#: M1.4 decoded-stream bounds. Audio expands into a decoded PCM stream whose size
#: the container's own declared duration, channels and rate predict, so all three
#: are bounded BEFORE any decoder or engine sees the bytes.
MAX_STT_DURATION_S = 300.0
MAX_STT_CHANNELS = 2
MAX_STT_SAMPLE_RATE = 48_000

#: Bound for the container walk that derives the declared audio info, so a
#: deliberately fragmented container cannot be walked without limit.
_MAX_AUDIO_PAGES = 200_000

#: M1.4 character ceiling for a transcript, shared with every other extractor so
#: every path produces text bounded by the same project prompt budget.
MAX_STT_CHARS = MAX_EXTRACTED_CHARS

#: M1.4 wall-clock bound for ONE transcription. Finite, well inside the media
#: request's own envelope, and never applied to an unrelated AI request.
STT_TIMEOUT_S = 60.0

#: Bounded multi-chunk speech-to-text. ``MAX_STT_DURATION_S`` above is the longest
#: audio ONE recognition may be asked to handle, so it is the per-CHUNK ceiling —
#: not the ceiling for the whole request. A longer recording is divided into
#: ordered chunks by ``backend/services/stt_chunking.py`` and transcribed one chunk
#: at a time through the SAME ``SttEngine`` seam.
#:
#: Every bound is finite and together they make an unbounded number of provider
#: calls impossible: the chunk count is capped explicitly, the total duration is
#: that cap times the per-chunk ceiling (never an arbitrary large multiplier), the
#: input-byte bound above is unchanged, and one request's whole transcription runs
#: inside ONE aggregate deadline. That aggregate value is derived from the media
#: envelope rather than invented: the handler's own backstop is 240s
#: (``media_ai_service.DEFAULT_ENVELOPE_S``) and the provider call that answers an
#: analytical media request reserves 120s of it
#: (``media_ai_service.PROVIDER_CALL_SAFETY_TIMEOUT_S``), so 120s is what is left
#: for recognition — which is also exactly two per-chunk bounds.
MAX_STT_CHUNKS = 4
MAX_STT_TOTAL_DURATION_S = MAX_STT_CHUNKS * MAX_STT_DURATION_S
STT_TOTAL_TIMEOUT_S = 120.0

#: Container signatures. MIME alone is never trusted for a safety-relevant
#: parse: the actual container must corroborate the declared type.
_PDF_MAGIC = b"%PDF-"
_PDF_HEADER_SCAN_BYTES = 1024
_ZIP_MAGIC = b"PK\x03\x04"
_DOCX_DOCUMENT_ENTRY = "word/document.xml"
_WORD_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"

#: Fallback temp filename when Telegram's (untrusted) name is unusable.
_FALLBACK_FILE_NAME = "media.bin"
_MAX_FILE_NAME_CHARS = 120


class MediaError(Exception):
    """Raised when media cannot be resolved, transferred or validated honestly.

    ``stage`` optionally names the INTERNAL stage this failure happened in (see
    the ``MEDIA_STAGE_*`` tokens). It is what keeps a media failure diagnosable
    on its way to the handler: the dispatcher stamps it on the failure result so
    the owner-facing notice reports the failing LEG instead of collapsing every
    media failure into one generic provider message. It is a closed token or a
    bounded reason's stage — never a payload, a credential or Telegram metadata —
    and an empty stage is valid: the caller that knows the leg attributes it.
    """

    def __init__(self, message: str, *, stage: str = "") -> None:
        super().__init__(message)
        self.stage = stage


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


#: The internal failure identity of the media path. The dispatcher stamps it on
#: a media failure result so the handler can render the media reason (stage +
#: bounded detail) instead of a generic provider message.
MEDIA_FAILURE_TYPE = "media"

#: The closed set of media failure stages — one token per leg of the existing
#: media path, so ONE live request is enough to tell which leg failed: the
#: runtime's Telegram source, target selection, bounded message resolution, the
#: bounded transfer, payload validation, STT availability, the engine call, the
#: boundary's own transcription timeout, the analysis as a whole (the default
#: when no deeper leg claimed the failure) and the post-analysis provider call.
MEDIA_STAGE_SOURCE = "media_source"
MEDIA_STAGE_TARGET = "media_target"
MEDIA_STAGE_RESOLUTION = "media_resolution"
MEDIA_STAGE_DOWNLOAD = "media_download"
MEDIA_STAGE_VALIDATION = "media_validation"
MEDIA_STAGE_STT_UNAVAILABLE = "media_stt_unavailable"
MEDIA_STAGE_STT_ENGINE = "media_stt_engine"
MEDIA_STAGE_STT_TIMEOUT = "media_stt_timeout"
MEDIA_STAGE_ANALYSIS = "media_analysis"
MEDIA_STAGE_PROVIDER = "media_provider"
MEDIA_STAGE_STT_EXHAUSTED = "media_stt_exhausted"


def bounded_reason(error: Any, limit: int = 200) -> str:
    """A bounded, single-line diagnostic reason for ``error`` (never a payload).

    Media failures carry their own sanitized message — the Gemini engine redacts
    the credential and bounds the provider detail — so the message IS the safe
    diagnostic. It is collapsed and capped here so no log line, failure result
    or owner-facing notice can grow without bound.
    """
    text = " ".join(str(error or "").split())[:limit]
    return text or type(error).__name__


def _stage_trace(
    stage: str, *, request_id: str = "", level: int = logging.INFO, **fields: Any,
) -> None:
    """Emit ONE media-stage trace line, in the project's existing trace shape.

    Same ``key=value`` convention as the AI execution traces, so one request can
    be followed stage by stage with the request id the AI layer already owns.
    Fields are expected to be closed tokens, labels, counts or bounded reasons —
    never media bytes, credentials or Telegram identifiers.
    """
    extra = " ".join(f"{key}={value}" for key, value in fields.items())
    logger.log(
        level,
        "MEDIA_STAGE request_id=%s stage=%s%s",
        request_id or "-", stage, f" {extra}" if extra else "",
    )


def is_downloadable(media_type: str) -> bool:
    """True when ``media_type`` is an asset this boundary may transfer."""
    return str(media_type or "") in DOWNLOADABLE_MEDIA_TYPES


def is_unprocessable(media_type: str) -> bool:
    """True when ``media_type`` is explicitly outside Media Processing scope."""
    return str(media_type or "") in UNPROCESSABLE_MEDIA_TYPES


def is_extractable_mime(mime_type: str) -> bool:
    """True when this phase can turn ``mime_type`` into text.

    Text-shaped MIME types need no dependency at all; the two container formats
    (M1.2) need ``pypdf`` for PDF and only the standard library for DOCX. A type
    outside this set is never transferred.
    """
    value = str(mime_type or "").strip().lower()
    if not value:
        return False
    if value in TEXT_MIME_TYPES or value in PDF_MIME_TYPES or value in DOCX_MIME_TYPES:
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


def is_pdf_mime(mime_type: str) -> bool:
    """True for the PDF MIME types this boundary extracts (M1.2)."""
    return str(mime_type or "").strip().lower() in PDF_MIME_TYPES


def is_docx_mime(mime_type: str) -> bool:
    """True for the OOXML wordprocessing MIME types this boundary extracts (M1.2)."""
    return str(mime_type or "").strip().lower() in DOCX_MIME_TYPES


def is_image_mime(mime_type: str) -> bool:
    """True for the still-image MIME types the OCR boundary may process (M1.3)."""
    return str(mime_type or "").strip().lower() in OCR_IMAGE_MIME_TYPES


class OcrEngine(Protocol):
    """The OCR seam: ONE deterministic ``image bytes -> text`` callable.

    The boundary owns everything around recognition — deterministic target
    resolution, bounded transfer, payload validation, bounds, timeout, temporary
    cleanup and normalization. An engine only turns already-validated image bytes
    into text, so provisioning one is a deployment decision that cannot alter the
    media contract. No engine is provisioned by default (see module docstring).
    """

    def recognize(self, image: bytes) -> str:
        """Return the text found in ``image`` (empty when there is none)."""
        ...


_ocr_engine: "OcrEngine | None" = None


def set_ocr_engine(engine: "OcrEngine | None") -> None:
    """Provision (or clear) the process-wide OCR engine.

    Nothing is provisioned by default, so an unprovisioned runtime reports images
    as UNSUPPORTED *without transferring them* and never depends on a heavy
    native stack it did not explicitly opt into.
    """
    global _ocr_engine
    _ocr_engine = engine


def get_ocr_engine() -> "OcrEngine | None":
    """The provisioned OCR engine, or ``None`` when none is available."""
    return _ocr_engine


def ocr_available() -> bool:
    """True when an OCR engine is provisioned on this runtime."""
    return _ocr_engine is not None


def is_stt_mime(mime_type: str) -> bool:
    """True for the audio MIME types the STT boundary may process (M1.4)."""
    return str(mime_type or "").strip().lower() in STT_AUDIO_MIME_TYPES


class SttEngine(Protocol):
    """The STT seam: ONE deterministic ``audio bytes -> transcript`` callable.

    The boundary owns everything around transcription — deterministic target
    resolution, bounded transfer, container validation, the channel/rate/duration
    guards, the timeout, temporary cleanup and normalization. An engine only turns
    already-validated audio bytes into text, so provisioning one is a deployment
    decision that cannot alter the media contract. No engine is provisioned by
    default (see module docstring).
    """

    def transcribe(self, audio: bytes) -> str:
        """Return the transcript of ``audio`` (empty when there is no speech)."""
        ...


_stt_engine: "SttEngine | None" = None


def set_stt_engine(engine: "SttEngine | None") -> None:
    """Provision (or clear) the process-wide speech-to-text engine.

    Nothing is provisioned by default, so an unprovisioned runtime reports Voice
    and Audio as UNSUPPORTED *without transferring them* and never depends on a
    heavy native stack or a model artifact it did not explicitly opt into.
    """
    global _stt_engine
    _stt_engine = engine


def get_stt_engine() -> "SttEngine | None":
    """The provisioned STT engine, or ``None`` when none is available."""
    return _stt_engine


def stt_available() -> bool:
    """True when a speech-to-text engine is provisioned on this runtime."""
    return _stt_engine is not None


class _TextAccumulator:
    """Accumulate extracted chunks without ever exceeding the char ceiling."""

    __slots__ = ("_parts", "_size", "_limit", "truncated")

    def __init__(self, limit: int) -> None:
        self._parts: list[str] = []
        self._size = 0
        self._limit = max(1, int(limit))
        self.truncated = False

    def add(self, chunk: str) -> bool:
        """Append one chunk; returns False once the ceiling is reached."""
        chunk = chunk or ""
        if not chunk:
            return True
        separator = 1 if self._parts else 0
        remaining = self._limit - self._size
        if len(chunk) + separator > remaining:
            tail = remaining - separator - 1
            if tail > 0:
                self._parts.append(chunk[:tail] + "…")
                self._size = self._limit
            self.truncated = True
            return False
        self._parts.append(chunk)
        self._size += len(chunk) + separator
        return True

    def text(self) -> str:
        return "\n".join(self._parts)


def _read_document_bytes(path: str) -> bytes:
    """Read a container file whole — it is already bounded by the size authority.

    ``analyze_media`` refuses anything larger than the media size limit before
    extraction, so this read is bounded by that established limit and never by
    an unbounded stream. (The character ceiling bounds the extracted TEXT, which
    is not the same quantity as the container's bytes.)
    """
    with open(path, "rb") as handle:
        return handle.read()


def _append_line(accumulator: _TextAccumulator, line: str) -> bool:
    line = (line or "").strip()
    if not line:
        return True
    return accumulator.add(line)


def _extract_pdf_document(path: str, limit: int) -> tuple[str, bool, str]:
    """PDF text layer, bounded by page count and extracted characters. No OCR.

    Page order is preserved. An encrypted PDF, a payload without a real
    ``%PDF-`` header, or an unparsable document raises ``MediaError`` — the
    fail-closed contract for payloads that cannot be read honestly.
    """
    raw = _read_document_bytes(path)
    if _PDF_MAGIC not in raw[:_PDF_HEADER_SCAN_BYTES]:
        raise MediaError("The file is not a readable PDF (no %PDF- header).")

    try:
        from pypdf import PdfReader
    except Exception as exc:  # noqa: BLE001 — dependency boundary
        raise MediaError("PDF extraction is not available on this runtime.") from exc

    try:
        reader = PdfReader(io.BytesIO(raw))
        if bool(getattr(reader, "is_encrypted", False)):
            try:
                decrypted = reader.decrypt("")
            except Exception:  # noqa: BLE001 — a refused decrypt is not a crash
                decrypted = 0
            if not decrypted:
                raise MediaError("The PDF is encrypted and cannot be read.")
    except MediaError:
        raise
    except Exception as exc:  # noqa: BLE001 — parser boundary
        raise MediaError(f"The PDF could not be read: {type(exc).__name__}") from exc

    accumulator = _TextAccumulator(limit)
    truncated = False
    pages_read = 0
    try:
        for page in reader.pages:
            if pages_read >= MAX_PDF_PAGES:
                truncated = True
                break
            pages_read += 1
            try:
                page_text = page.extract_text() or ""
            except Exception:  # noqa: BLE001 — one bad page is not a bad document
                page_text = ""
            if not _append_line(accumulator, page_text):
                break
    except Exception as exc:  # noqa: BLE001 — parser boundary
        if pages_read == 0:
            raise MediaError(f"The PDF could not be read: {type(exc).__name__}") from exc
        truncated = True

    content = accumulator.text()
    truncated = truncated or accumulator.truncated
    if not content:
        return "", truncated, "The PDF contains no extractable text layer."
    return content, truncated, ""


def _extract_docx_document(path: str, limit: int) -> tuple[str, bool, str]:
    """DOCX paragraph text (including table cells), bounded and fail-closed.

    The DOCX is an UNTRUSTED ZIP/XML container: only ``word/document.xml`` is
    read, the archive is never unpacked to disk, no other entry (macros,
    embeddings, external links) is ever opened, and a body declaring a DTD or an
    entity is refused.
    """
    raw = _read_document_bytes(path)
    if not raw.startswith(_ZIP_MAGIC):
        raise MediaError("The file is not a readable DOCX container.")

    try:
        archive = zipfile.ZipFile(io.BytesIO(raw))
    except Exception as exc:  # noqa: BLE001 — malformed container
        raise MediaError(f"The DOCX container could not be opened: {type(exc).__name__}") from exc

    with archive:
        try:
            entry = archive.getinfo(_DOCX_DOCUMENT_ENTRY)
        except KeyError as exc:
            raise MediaError("The container is not a DOCX document.") from exc
        if entry.file_size > MAX_ARCHIVE_ENTRY_BYTES:
            raise MediaError("The document body exceeds the extraction bound.")
        try:
            with archive.open(entry) as handle:
                xml_bytes = handle.read(MAX_ARCHIVE_ENTRY_BYTES + 1)
        except Exception as exc:  # noqa: BLE001 — entry boundary
            raise MediaError(
                f"The document body could not be read: {type(exc).__name__}"
            ) from exc

    if len(xml_bytes) > MAX_ARCHIVE_ENTRY_BYTES:
        raise MediaError("The document body exceeds the extraction bound.")

    head = xml_bytes[:4096].upper()
    if b"<!DOCTYPE" in head or b"<!ENTITY" in head:
        raise MediaError("The document body declares an unsafe XML construct.")

    try:
        root = ET.fromstring(xml_bytes)
    except Exception as exc:  # noqa: BLE001 — untrusted XML boundary
        raise MediaError(f"The document body could not be parsed: {type(exc).__name__}") from exc

    accumulator = _TextAccumulator(limit)
    truncated = False
    blocks = 0
    for paragraph in root.iter(f"{_WORD_NS}p"):
        if blocks >= MAX_DOCX_TEXT_ELEMENTS:
            truncated = True
            break
        blocks += 1
        line = "".join(node.text or "" for node in paragraph.iter(f"{_WORD_NS}t"))
        if not _append_line(accumulator, line):
            break

    content = accumulator.text()
    truncated = truncated or accumulator.truncated
    if not content:
        return "", truncated, "The DOCX document contains no extractable text."
    return content, truncated, ""


#: Container signatures for the OCR image formats. MIME alone is never trusted
#: for a decode: the payload's own header must corroborate the declared type.
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_GIF_MAGICS = (b"GIF87a", b"GIF89a")
_JPEG_SOF_MARKERS = frozenset(
    {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
)
#: Upper bound for the JPEG marker walk, so a malformed header cannot be scanned
#: without limit.
_JPEG_SCAN_BYTES = 512 * 1024


def _png_dimensions(data: bytes) -> "tuple[int, int] | None":
    if len(data) < 24 or not data.startswith(_PNG_MAGIC) or data[12:16] != b"IHDR":
        return None
    return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")


def _jpeg_dimensions(data: bytes) -> "tuple[int, int] | None":
    if len(data) < 4 or data[:2] != b"\xff\xd8":
        return None
    index = 2
    end = min(len(data), _JPEG_SCAN_BYTES)
    while index + 3 < end:
        if data[index] != 0xFF:
            index += 1
            continue
        marker = data[index + 1]
        if marker in {0xFF, 0x01} or 0xD0 <= marker <= 0xD8:
            index += 2
            continue
        if index + 4 > end:
            return None
        size = int.from_bytes(data[index + 2:index + 4], "big")
        if size < 2:
            return None
        if marker in _JPEG_SOF_MARKERS:
            if index + 9 > end:
                return None
            height = int.from_bytes(data[index + 5:index + 7], "big")
            width = int.from_bytes(data[index + 7:index + 9], "big")
            return width, height
        index += 2 + size
    return None


def _gif_dimensions(data: bytes) -> "tuple[int, int] | None":
    if len(data) < 10 or data[:6] not in _GIF_MAGICS:
        return None
    return int.from_bytes(data[6:8], "little"), int.from_bytes(data[8:10], "little")


def _webp_dimensions(data: bytes) -> "tuple[int, int] | None":
    if len(data) < 30 or data[:4] != b"RIFF" or data[8:12] != b"WEBP":
        return None
    chunk = data[12:16]
    if chunk == b"VP8X":
        width = int.from_bytes(data[24:27], "little") + 1
        height = int.from_bytes(data[27:30], "little") + 1
        return width, height
    if chunk == b"VP8 ":
        if data[23:26] != b"\x9d\x01\x2a":
            return None
        width = int.from_bytes(data[26:28], "little") & 0x3FFF
        height = int.from_bytes(data[28:30], "little") & 0x3FFF
        return width, height
    if chunk == b"VP8L":
        if data[20] != 0x2F:
            return None
        bits = int.from_bytes(data[21:25], "little")
        return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
    return None


def _bmp_dimensions(data: bytes) -> "tuple[int, int] | None":
    if len(data) < 26 or data[:2] != b"BM":
        return None
    width = int.from_bytes(data[18:22], "little", signed=True)
    height = int.from_bytes(data[22:26], "little", signed=True)
    return abs(width), abs(height)


_IMAGE_SIGNATURE_READERS = {
    "image/png": _png_dimensions,
    "image/jpeg": _jpeg_dimensions,
    "image/jpg": _jpeg_dimensions,
    "image/gif": _gif_dimensions,
    "image/webp": _webp_dimensions,
    "image/bmp": _bmp_dimensions,
}


def _validate_image_payload(data: bytes, mime_type: str) -> tuple[int, int]:
    """Corroborate the declared MIME and bound the bitmap BEFORE any decode.

    The payload's own container signature must match the declared MIME, so a
    mislabelled file is refused instead of being handed to a decoder as if it
    were an image. The declared dimensions are read from the header and checked
    against hard bounds, so a small compressed payload can never be expanded
    into an unbounded bitmap.

    Raises:
        MediaError: unsupported/unmatched container, invalid size, or a bitmap
                     beyond the pixel or side bound.
    """
    value = str(mime_type or "").strip().lower()
    reader = _IMAGE_SIGNATURE_READERS.get(value)
    if reader is None:
        raise MediaError(f"{value or 'the declared type'} is not a supported image format.")
    dimensions = reader(data)
    if dimensions is None:
        raise MediaError("The file is not a readable image of the declared type.")
    width, height = dimensions
    if width <= 0 or height <= 0:
        raise MediaError("The image declares an invalid size.")
    if width > MAX_IMAGE_SIDE or height > MAX_IMAGE_SIDE or width * height > MAX_IMAGE_PIXELS:
        raise MediaError(
            f"The image is {width}x{height} — exceeds the "
            f"{MAX_IMAGE_PIXELS}-pixel OCR bound."
        )
    return width, height


def _normalize_extracted_text(text: str) -> str:
    """Deterministic normalization shared by every extractor (OCR and STT).

    Line structure survives (paragraphs stay separated), horizontal whitespace
    runs collapse to one space, blank-line runs collapse to one blank line, and
    nothing else is rewritten: Persian/Arabic text, its ZWNJ (U+200C) and any
    directional marks pass through untouched. No truncation happens here — the
    caller applies the character ceiling.
    """
    if not text:
        return ""
    lines: list[str] = []
    blank_run = 0
    for raw_line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = " ".join(raw_line.split())
        if line:
            blank_run = 0
            lines.append(line)
        else:
            blank_run += 1
            if blank_run == 1:
                lines.append("")
    while lines and not lines[-1]:
        lines.pop()
    while lines and not lines[0]:
        lines.pop(0)
    return "\n".join(lines)


def _cap_text(text: str, limit: int) -> tuple[str, bool]:
    """Cap ``text`` at ``limit`` characters, reporting truncation honestly."""
    if limit <= 0:
        return "", bool(text)
    if len(text) <= limit:
        return text, False
    return text[: limit - 1] + "…", True


async def _run_ocr(engine: OcrEngine, data: bytes, timeout_s: float) -> str:
    """Run ONE recognition off the event loop, under a finite bound.

    Recognition is CPU-bound, so it runs in a worker thread — the same
    ``asyncio.to_thread`` pattern M1.2 uses for document parsing — and the
    awaited result is wrapped in a finite timeout so a stalled engine fails the
    analysis honestly instead of holding the request. A worker thread cannot be
    interrupted cooperatively, which is why the bound is enforced on the awaited
    result rather than assumed to stop the engine itself.
    """
    try:
        result = await asyncio.wait_for(
            asyncio.to_thread(engine.recognize, data), timeout=timeout_s,
        )
    except asyncio.CancelledError:
        raise
    except asyncio.TimeoutError as exc:
        raise MediaError(f"OCR did not finish within {timeout_s:g}s.") from exc
    except MediaError:
        raise
    except Exception as exc:  # noqa: BLE001 — engine boundary
        raise MediaError(f"OCR failed: {type(exc).__name__}") from exc
    return result if isinstance(result, str) else ""


async def _extract_image_content(
    path: str, mime_type: str, limit: int,
) -> tuple[str, bool, str]:
    """OCR ONE downloaded image, bounded at every step (M1.3).

    Bounds, in order: the payload must be inside the OCR input bound, its
    signature must corroborate the declared MIME, its declared dimensions must
    fit the bitmap bound, recognition must finish inside the OCR timeout, and the
    normalized text is capped at ``limit`` characters.
    """
    engine = _ocr_engine
    if engine is None:
        raise MediaError("No OCR engine is provisioned on this runtime.")

    data = _read_document_bytes(path)
    if not data:
        return "", False, "The image carried no data to read."
    if len(data) > MAX_OCR_INPUT_BYTES:
        raise MediaError(
            f"The image is {_format_bytes(len(data))} — exceeds the "
            f"{_format_bytes(MAX_OCR_INPUT_BYTES)} OCR input limit."
        )

    _validate_image_payload(data, mime_type)
    raw_text = await _run_ocr(engine, data, OCR_TIMEOUT_S)
    text = _normalize_extracted_text(raw_text)
    if not text:
        return "", False, "No readable text was detected in the image."
    capped, truncated = _cap_text(text, limit)
    return capped, truncated, ""


#: Container signatures for the audio formats this boundary may process.
_OGG_MAGIC = b"OggS"
_WAV_RIFF = b"RIFF"
_WAV_WAVE = b"WAVE"
_FLAC_MAGIC = b"fLaC"
_OPUS_HEADER = b"OpusHead"
_VORBIS_HEADER = b"\x01vorbis"
#: Opus granule positions are ALWAYS in 48 kHz units, whatever the input rate
#: the stream declares, so the two must not be conflated when deriving duration.
_OPUS_GRANULE_RATE = 48_000


def _ogg_audio_info(data: bytes) -> "tuple[int, int, float] | None":
    """``(channels, sample_rate, duration_s)`` from an OGG/Opus or OGG/Vorbis stream.

    Walks OGG pages (bounded by page count, over an already size-bounded file),
    reads the codec identification header of the first audio page and the final
    granule position, which is the stream's exact sample count. Returns ``None``
    for anything that is not a readable OGG stream.
    """
    total = len(data)
    if total < 28 or data[:4] != _OGG_MAGIC or data[4] != 0:
        return None
    channels = 0
    sample_rate = 0
    granule_rate = 0
    granule = 0
    pages = 0
    index = 0
    while index + 27 <= total:
        if data[index:index + 4] != _OGG_MAGIC or data[index + 4] != 0:
            return None
        pages += 1
        if pages > _MAX_AUDIO_PAGES:
            return None
        page_granule = int.from_bytes(data[index + 6:index + 14], "little")
        if page_granule > granule:
            granule = page_granule
        segment_count = data[index + 26]
        payload = index + 27 + segment_count
        if payload > total:
            return None
        if channels == 0:
            if data[payload:payload + 8] == _OPUS_HEADER and payload + 19 <= total:
                channels = data[payload + 9]
                sample_rate = int.from_bytes(data[payload + 12:payload + 16], "little")
                granule_rate = _OPUS_GRANULE_RATE
            elif data[payload:payload + 7] == _VORBIS_HEADER and payload + 16 <= total:
                channels = data[payload + 11]
                sample_rate = int.from_bytes(data[payload + 12:payload + 16], "little")
                granule_rate = sample_rate
        index = payload + sum(data[index + 27:payload])
    if channels <= 0 or sample_rate <= 0 or granule_rate <= 0:
        return None
    return channels, sample_rate, granule / granule_rate


def _wav_audio_info(data: bytes) -> "tuple[int, int, float] | None":
    """``(channels, sample_rate, duration_s)`` from a RIFF/WAVE stream.

    Reads only the ``fmt `` and ``data`` chunk headers, so the walk is bounded by
    the declared chunk sizes and never by the payload's contents.
    """
    total = len(data)
    if total < 44 or data[:4] != _WAV_RIFF or data[8:12] != _WAV_WAVE:
        return None
    channels = 0
    sample_rate = 0
    byte_rate = 0
    data_bytes = 0
    index = 12
    while index + 8 <= total:
        chunk_id = data[index:index + 4]
        chunk_size = int.from_bytes(data[index + 4:index + 8], "little")
        body = index + 8
        if chunk_id == b"fmt ":
            if body + 16 > total:
                return None
            channels = int.from_bytes(data[body + 2:body + 4], "little")
            sample_rate = int.from_bytes(data[body + 4:body + 8], "little")
            byte_rate = int.from_bytes(data[body + 8:body + 12], "little")
        elif chunk_id == b"data":
            data_bytes = min(chunk_size, max(0, total - body))
            break
        index = body + chunk_size + (chunk_size % 2)
    if channels <= 0 or sample_rate <= 0 or byte_rate <= 0:
        return None
    return channels, sample_rate, data_bytes / byte_rate


def _flac_audio_info(data: bytes) -> "tuple[int, int, float] | None":
    """``(channels, sample_rate, duration_s)`` from a FLAC stream.

    STREAMINFO is always the first metadata block, so the walk is a fixed
    34-byte read. A stream that does not declare a total sample count cannot be
    bounded and therefore returns ``None`` (fail-closed at the call site).
    """
    if len(data) < 42 or data[:4] != _FLAC_MAGIC:
        return None
    if (data[4] & 0x7F) != 0 or int.from_bytes(data[5:8], "big") != 34:
        return None
    # STREAMINFO body starts at 8; its packed sample-rate/channels/total-samples
    # field is 10 bytes into that body (after min/max block and min/max frame
    # sizes), i.e. file offset 18.
    bits = int.from_bytes(data[18:26], "big")
    sample_rate = bits >> 44
    channels = ((bits >> 41) & 0x07) + 1
    total_samples = bits & ((1 << 36) - 1)
    if sample_rate <= 0 or channels <= 0:
        return None
    # A total-sample count of 0 means "unknown": a VALID stream whose length the
    # container does not declare. It is reported as a zero duration so the caller
    # can refuse it for being unbounded rather than for being malformed.
    return channels, sample_rate, (total_samples / sample_rate) if total_samples else 0.0


_STT_SIGNATURE_READERS = {
    "audio/ogg": _ogg_audio_info,
    "audio/opus": _ogg_audio_info,
    "application/ogg": _ogg_audio_info,
    "audio/wav": _wav_audio_info,
    "audio/x-wav": _wav_audio_info,
    "audio/wave": _wav_audio_info,
    "audio/vnd.wave": _wav_audio_info,
    "audio/flac": _flac_audio_info,
    "audio/x-flac": _flac_audio_info,
}


def _validate_audio_payload(
    data: bytes, mime_type: str, *, max_duration_s: float | None = None,
) -> tuple[int, int, float]:
    """Corroborate the declared MIME and bound the decoded stream BEFORE any decode.

    The payload's own container signature must match the declared MIME, and the
    stream it declares — channel count, sample rate and duration — must fit hard
    bounds, so no container trick can ask an engine to chew on an unbounded
    decoded stream. A stream whose duration cannot be determined is refused
    rather than transcribed unbounded.

    ``max_duration_s`` is the duration ceiling THIS check applies and defaults to
    ``MAX_STT_DURATION_S``, which is what every other caller means. The audio
    extraction passes the larger TOTAL ceiling instead, because a recording longer
    than one chunk is divided by :mod:`backend.services.stt_chunking` rather than
    refused here.

    Raises:
        MediaError: unsupported/unmatched container, invalid stream, a channel,
                     rate or duration beyond its bound, or an unbounded stream.
    """
    value = str(mime_type or "").strip().lower()
    reader = _STT_SIGNATURE_READERS.get(value)
    if reader is None:
        raise MediaError(f"{value or 'the declared type'} is not a supported audio format.")
    info = reader(data)
    if info is None:
        raise MediaError("The file is not a readable audio stream of the declared type.")
    channels, sample_rate, duration_s = info
    if channels <= 0 or sample_rate <= 0:
        raise MediaError("The audio declares an invalid stream.")
    if channels > MAX_STT_CHANNELS:
        raise MediaError(
            f"The audio has {channels} channels — exceeds the "
            f"{MAX_STT_CHANNELS}-channel speech-to-text bound."
        )
    if sample_rate > MAX_STT_SAMPLE_RATE:
        raise MediaError(
            f"The audio is {sample_rate} Hz — exceeds the "
            f"{MAX_STT_SAMPLE_RATE} Hz speech-to-text bound."
        )
    if duration_s <= 0:
        raise MediaError(
            "The audio duration could not be determined, so speech-to-text "
            "cannot be bounded."
        )
    bound = MAX_STT_DURATION_S if max_duration_s is None else float(max_duration_s)
    if duration_s > bound:
        raise MediaError(
            f"The audio is {duration_s:.0f}s — exceeds the "
            f"{bound:.0f}s speech-to-text bound."
        )
    return channels, sample_rate, duration_s


async def _run_stt(
    engine: SttEngine, data: bytes, timeout_s: float, request_id: str = "",
) -> str:
    """Run ONE transcription off the event loop, under a finite bound.

    Transcription is CPU-bound, so it runs in a worker thread — the same
    ``asyncio.to_thread`` pattern M1.2 uses for document parsing — and the
    awaited result is wrapped in a finite timeout so a stalled engine fails the
    analysis honestly instead of holding the request. A worker thread cannot be
    interrupted cooperatively, so the bound is enforced on the awaited result
    rather than assumed to stop the engine itself.

    Every exit path carries an internal stage: the boundary's own timeout and the
    engine's own failure are distinct legs, and the engine's message (which names
    the remote service and its HTTP status) is preserved as the bounded reason —
    it is what makes the next live failure diagnostic instead of generic.
    """
    try:
        result = await asyncio.wait_for(
            asyncio.to_thread(engine.transcribe, data), timeout=timeout_s,
        )
    except asyncio.CancelledError:
        raise
    except asyncio.TimeoutError as exc:
        _stage_trace(
            "stt_timeout", request_id=request_id, level=logging.WARNING,
            error="TimeoutError",
            reason=bounded_reason(f"speech-to-text did not finish within {timeout_s:g}s"),
        )
        raise MediaError(
            f"Speech-to-text did not finish within {timeout_s:g}s.",
            stage=MEDIA_STAGE_STT_TIMEOUT,
        ) from exc
    except MediaError as exc:
        if not getattr(exc, "stage", ""):
            exc.stage = MEDIA_STAGE_STT_ENGINE
        _stage_trace(
            "stt_engine_failed", request_id=request_id, level=logging.WARNING,
            error=type(exc).__name__, reason=bounded_reason(exc),
        )
        raise
    except Exception as exc:  # noqa: BLE001 — engine boundary
        _stage_trace(
            "stt_engine_failed", request_id=request_id, level=logging.WARNING,
            error=type(exc).__name__, reason=bounded_reason(exc),
        )
        raise MediaError(
            f"Speech-to-text failed: {type(exc).__name__}",
            stage=MEDIA_STAGE_STT_ENGINE,
        ) from exc
    return result if isinstance(result, str) else ""


async def _stt_attempt(
    engine: SttEngine, data: bytes, timeout_s: float, request_id: str,
) -> str:
    """The primitive ONE provider attempt is made through — :func:`_run_stt`.

    Named as its own bound method so the fallback layer's ``run_engine`` hook
    receives exactly the boundary's attempt semantics: worker-thread execution,
    the awaited timeout on the REMAINING budget, and the classified stage on
    every failure. No other behavior is added here.
    """
    return await _run_stt(engine, data, timeout_s, request_id=request_id)


async def _run_stt_chunked(
    engine: SttEngine, data: bytes, mime_type: str, duration_s: float,
    request_id: str = "",
) -> str:
    """Transcribe ONE over-long audio payload as ordered, bounded chunks.

    The division itself is :mod:`backend.services.stt_chunking`'s job and runs off
    the event loop (container parsing is CPU-bound over an already size-bounded
    payload). This function owns the OPERATION around it, and every rule it
    enforces is deliberate:

      * chunks are transcribed STRICTLY in source order, one at a time. The
        engine each chunk uses is the request's attempt plan: the SELECTED
        engine first, and — through the fallback layer (M2.2) — the next
        eligible healthy candidate when a provider fails a chunk with a
        fallback-eligible failure. A plan pins the candidate that succeeded,
        so earlier chunks are never retranscribed on a later provider switch;
      * each chunk keeps the engine's own multi-pass behaviour, because it is the
        SAME ``transcribe`` seam: the configured pass count multiplies the calls
        per CHUNK (chunks x passes x the fallback attempt ceiling, all capped by
        the constants above), and can never become an unbounded retry loop;
      * the whole operation runs inside ONE aggregate deadline: each chunk gets the
        smaller of the per-chunk bound and what is left of it — and a fallback
        candidate receives only what is LEFT of the chunk's own bound, never a
        fresh one — while a spent deadline fails the operation instead of
        starting another chunk or another provider;
      * a chunk whose attempt plan is EXHAUSTED fails the WHOLE operation — the
        error propagates — so a partial transcript is never returned as a
        complete transcription, and no failed or skipped chunk is ever silently
        dropped.

    The chunks are in-memory slices of the already-validated payload, so there is
    no temporary file to leak on any exit path, and the plan (with the last chunk
    built) is released before returning.
    """
    plan = await asyncio.to_thread(
        stt_chunking.plan, data, mime_type,
        max_chunk_duration_s=MAX_STT_DURATION_S, max_chunks=MAX_STT_CHUNKS,
    )
    if plan is None:
        raise MediaError(
            f"The audio is {duration_s:.0f}s — exceeds the "
            f"{MAX_STT_DURATION_S:.0f}s speech-to-text bound and this container "
            "cannot be divided into shorter parts of the same format.",
            stage=MEDIA_STAGE_VALIDATION,
        )
    count = plan.count
    _stage_trace(
        "stt_chunks_planned", request_id=request_id, mime=mime_type,
        chunks=count, duration_s=f"{duration_s:.1f}",
    )
    attempts = stt_fallback.attempt_plan(engine)
    if attempts is not None:
        _stage_trace(
            "stt_fallback_armed", request_id=request_id,
            selected=attempts.selected_id, candidates=attempts.candidate_count,
        )
    started = time.monotonic()
    parts: list[str] = []
    try:
        for index in range(count):
            remaining = STT_TOTAL_TIMEOUT_S - (time.monotonic() - started)
            if remaining <= 0:
                raise MediaError(
                    f"Speech-to-text did not finish within "
                    f"{STT_TOTAL_TIMEOUT_S:g}s ({index} of {count} parts "
                    "transcribed).",
                    stage=MEDIA_STAGE_STT_TIMEOUT,
                )
            payload = plan.chunk(index)
            bound = min(STT_TIMEOUT_S, remaining)
            _stage_trace(
                "stt_chunk_invoked", request_id=request_id, index=index + 1,
                chunks=count, bytes=len(payload), timeout_s=f"{bound:.1f}",
            )
            if attempts is not None:
                raw = await attempts.run(
                    payload, bound, _stt_attempt, request_id=request_id,
                )
            else:
                raw = await _run_stt(engine, payload, bound, request_id=request_id)
            part = _normalize_extracted_text(raw)
            _stage_trace(
                "stt_chunk_returned", request_id=request_id, index=index + 1,
                chunks=count, chars=len(part),
            )
            parts.append(part)
    finally:
        plan = None

    merged = stt_chunking.join_transcripts(parts)
    _stage_trace(
        "stt_chunks_merged", request_id=request_id, chunks=count, chars=len(merged),
    )
    return merged


async def _extract_audio_content(
    path: str, mime_type: str, limit: int, request_id: str = "",
) -> tuple[str, bool, str]:
    """Transcribe ONE downloaded audio asset, bounded at every step (M1.4).

    Bounds, in order: the payload must be inside the STT input bound, its
    signature must corroborate the declared MIME, its declared channels, sample
    rate and total duration must fit their bounds, transcription must finish
    inside the STT timeout, and the normalized transcript is capped at ``limit``
    characters.

    Audio longer than ONE chunk is no longer refused: it is divided into ordered,
    bounded chunks by :mod:`backend.services.stt_chunking` and transcribed through
    the same engine seam, and the result is still ONE transcript and ONE
    ``MediaAnalysis``. The character ceiling keeps its existing meaning in both
    routes: a cap on the PRESENTED text, reported through ``truncated`` — never a
    licence to drop audio from the transcription.

    The STT stages are traced individually — engine availability, engine
    invocation, each chunk of a divided recording, the engine's return (with its
    character count, so an EMPTY transcript is distinguishable from a failure) and
    the engine's failure (with its bounded reason) — so a live request identifies
    its leg without guessing.
    """
    engine = _stt_engine
    if engine is None:
        _stage_trace(
            "stt_engine_unavailable", request_id=request_id, level=logging.WARNING,
        )
        raise MediaError(
            "No speech-to-text engine is provisioned on this runtime.",
            stage=MEDIA_STAGE_STT_UNAVAILABLE,
        )

    data = _read_document_bytes(path)
    if not data:
        return "", False, "The audio carried no data to read."
    if len(data) > MAX_STT_INPUT_BYTES:
        raise MediaError(
            f"The audio is {_format_bytes(len(data))} — exceeds the "
            f"{_format_bytes(MAX_STT_INPUT_BYTES)} speech-to-text input limit.",
            stage=MEDIA_STAGE_VALIDATION,
        )

    try:
        _channels, _sample_rate, duration_s = _validate_audio_payload(
            data, mime_type, max_duration_s=MAX_STT_TOTAL_DURATION_S,
        )
    except MediaError as exc:
        # The validation leg refuses the container before any engine is invoked;
        # the refined message is preserved and only the stage is attributed.
        if not getattr(exc, "stage", ""):
            exc.stage = MEDIA_STAGE_VALIDATION
        raise

    if duration_s > MAX_STT_DURATION_S:
        # Longer than ONE recognition may handle: divided into ordered, bounded
        # chunks and transcribed one at a time through the same engine seam.
        raw_text = await _run_stt_chunked(
            engine, data, mime_type, duration_s, request_id=request_id,
        )
    else:
        _stage_trace(
            "stt_engine_invoked", request_id=request_id,
            engine=type(engine).__name__, bytes=len(data),
        )
        attempts = stt_fallback.attempt_plan(engine)
        if attempts is not None:
            _stage_trace(
                "stt_fallback_armed", request_id=request_id,
                selected=attempts.selected_id, candidates=attempts.candidate_count,
            )
            raw_text = await attempts.run(
                data, STT_TIMEOUT_S, _stt_attempt, request_id=request_id,
            )
        else:
            raw_text = await _run_stt(engine, data, STT_TIMEOUT_S, request_id=request_id)
    text = _normalize_extracted_text(raw_text)
    _stage_trace(
        "stt_engine_returned", request_id=request_id,
        engine=type(engine).__name__, chars=len(text),
    )
    if not text:
        return "", False, "No speech was detected in the audio."
    capped, truncated = _cap_text(text, limit)
    return capped, truncated, ""


def _extract_content(path: str, mime_type: str, limit: int) -> tuple[str, bool, str]:
    """Extract text from ONE downloaded asset, bounded by ``limit`` characters.

    Returns ``(content, truncated, empty_reason)``, where ``empty_reason`` is
    only used when ``content`` is empty. The text path is M1's, unchanged; the
    container formats are M1.2 and fail closed through ``MediaError``.
    """
    if is_pdf_mime(mime_type):
        return _extract_pdf_document(path, limit)
    if is_docx_mime(mime_type):
        return _extract_docx_document(path, limit)
    content, truncated = _read_text(path, limit)
    return content, truncated, "" if content else "The downloaded text asset was empty."


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
    request_id: str = "",
) -> Any:
    """Fetch the ONE message the TRUSTED RUNTIME identified, by its own ids.

    Resolution is deterministic and lives outside the model: ``chat_id`` and
    ``message_id`` come from the request scope (the replied-to message, or the
    triggering message itself), never from model output, text, recency, sender
    or history. Nothing here searches for a message, and nothing falls back to
    another one — unreadable ids produce an honest ``MediaError``.

    ``request_id`` is trace correlation only (the AI layer's request id); it
    never participates in target selection and is never sent anywhere.

    The fetched object stays inside this boundary: it is consumed by
    :func:`analyze_media` and is never returned to an AI layer, a prompt or a
    provider.
    """
    _stage_trace("media_resolution_started", request_id=request_id, timeout_s=f"{_bounded(timeout_s, MEDIA_RESOLVE_TIMEOUT_S):g}")
    client = _resolve_client(source)
    target_chat = _coerce_int(chat_id)
    target_message = _coerce_int(message_id)
    if not target_chat:
        raise MediaError(
            "A concrete chat id is required to resolve media.",
            stage=MEDIA_STAGE_RESOLUTION,
        )
    if not target_message:
        raise MediaError(
            "A concrete message id is required to resolve media.",
            stage=MEDIA_STAGE_RESOLUTION,
        )

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
            f"Resolving the media message timed out after {bound:g}s.",
            stage=MEDIA_STAGE_RESOLUTION,
        ) from exc
    except Exception as exc:
        logger.warning("MEDIA_RESOLVE_FAILED chat_id=%s message_id=%s error=%s",
                       target_chat, target_message, type(exc).__name__)
        raise MediaError(
            f"Resolving the media message failed: {exc}",
            stage=MEDIA_STAGE_RESOLUTION,
        ) from exc

    if message is None:
        raise MediaError(
            "The media message could not be found.",
            stage=MEDIA_STAGE_RESOLUTION,
        )
    _stage_trace("media_resolution_completed", request_id=request_id)
    return message


async def analyze_media(
    source: Any,
    owner_id: int,
    message: Any,
    *,
    timeout_s: Any = None,
    size_limit_bytes: int | None = None,
    request_id: str = "",
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
        request_id:        Trace correlation only (the AI layer's request id);
                           it never selects, resolves or transfers anything.

    Returns:
        A ``MediaAnalysis`` that either carries extracted text or states
        honestly why this phase did not process the asset.

    Raises:
        MediaError:  nothing to resolve, no media, oversized, transfer failed or
                     timed out, or the download was missing/empty/malformed.
        asyncio.CancelledError: re-raised unchanged (after cleanup).
    """
    if message is None:
        raise MediaError(
            "No media message was resolved for this request.",
            stage=MEDIA_STAGE_TARGET,
        )

    info = classify_message(message)
    if not info.has_media:
        raise MediaError(
            "The resolved message carries no media.",
            stage=MEDIA_STAGE_TARGET,
        )

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

    if is_unprocessable(info.media_type):
        analysis = _unsupported(
            info, message,
            f"{info.media_type} is outside the Media Processing scope "
            "(video and GIF are not processed).",
        )
        logger.info(
            "MEDIA_ANALYZE owner=%s type=%s status=%s reason=out_of_scope",
            owner_id, info.media_type, analysis.status,
        )
        return analysis

    ocr_candidate = is_image_mime(info.mime_type) and ocr_available()
    stt_candidate = is_stt_mime(info.mime_type) and stt_available()
    if is_stt_mime(info.mime_type):
        # The single line that answers "was STT even available for this asset?" —
        # a missing engine is otherwise only visible as an UNSUPPORTED outcome.
        _stage_trace(
            "stt_availability", request_id=request_id, type=info.media_type,
            mime=info.mime_type, available=stt_available(), candidate=stt_candidate,
        )
    if not is_extractable_mime(info.mime_type) and not ocr_candidate and not stt_candidate:
        if is_image_mime(info.mime_type):
            reason = (
                f"OCR is not available for {info.media_type} "
                f"({info.mime_type}) — no OCR engine is provisioned on this runtime."
            )
        elif is_stt_mime(info.mime_type):
            reason = (
                f"Speech-to-text is not available for {info.media_type} "
                f"({info.mime_type}) — no STT engine is provisioned on this runtime."
            )
        else:
            reason = (
                f"No local extraction capability for {info.media_type} "
                f"({info.mime_type or 'unknown type'}) in this phase."
            )
        analysis = _unsupported(info, message, reason)
        logger.info(
            "MEDIA_ANALYZE owner=%s type=%s mime=%s status=%s", owner_id,
            info.media_type, info.mime_type or "-", analysis.status,
        )
        return analysis

    limit = size_limit_bytes if size_limit_bytes and size_limit_bytes > 0 else max_download_bytes()
    if ocr_candidate:
        # An image is additionally bounded by the OCR input bound, and the tighter
        # of the two is applied BEFORE the transfer: an image OCR could never be
        # allowed to read is never downloaded at all.
        limit = min(limit, MAX_OCR_INPUT_BYTES)
    elif stt_candidate:
        # Same rule for audio: the tighter of the two bounds is applied BEFORE the
        # transfer, so audio STT could never be allowed to read is never fetched.
        limit = min(limit, MAX_STT_INPUT_BYTES)
    if info.file_size and info.file_size > limit:
        raise MediaError(
            f"Media is {_format_bytes(info.file_size)} — exceeds the "
            f"{_format_bytes(limit)} processing limit.",
            stage=MEDIA_STAGE_VALIDATION,
        )

    client = _resolve_client(source)
    bound = download_timeout(timeout_s)
    _stage_trace(
        "media_download_started", request_id=request_id, type=info.media_type,
        declared_bytes=info.file_size or "-", timeout_s=f"{bound:g}",
    )
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
            raise MediaError(
                f"Media download failed: {exc}",
                stage=MEDIA_STAGE_DOWNLOAD,
            ) from exc

        if result is None:
            raise MediaError(
                "Telegram returned no media for the resolved message.",
                stage=MEDIA_STAGE_DOWNLOAD,
            )

        path = result if isinstance(result, str) and os.path.exists(result) else destination
        if not os.path.exists(path):
            raise MediaError(
                "Downloaded media is missing.",
                stage=MEDIA_STAGE_DOWNLOAD,
            )
        size = os.path.getsize(path)
        if size == 0:
            raise MediaError(
                "Downloaded media is empty.",
                stage=MEDIA_STAGE_DOWNLOAD,
            )
        if size > limit:
            raise MediaError(
                f"Downloaded media is {_format_bytes(size)} — exceeds the "
                f"{_format_bytes(limit)} processing limit.",
                stage=MEDIA_STAGE_DOWNLOAD,
            )
        _stage_trace(
            "media_download_completed", request_id=request_id,
            type=info.media_type, bytes=size,
        )

        # Container parsing is CPU-bound over an already size-bounded file, so
        # it runs off the event loop; the temporary directory is still removed
        # on every exit path below.
        if ocr_candidate:
            content, truncated, empty_reason = await _extract_image_content(
                path, info.mime_type, MAX_OCR_CHARS,
            )
        elif stt_candidate:
            content, truncated, empty_reason = await _extract_audio_content(
                path, info.mime_type, MAX_STT_CHARS, request_id=request_id,
            )
        else:
            content, truncated, empty_reason = await asyncio.to_thread(
                _extract_content, path, info.mime_type, MAX_EXTRACTED_CHARS,
            )
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    analysis = MediaAnalysis(
        media_type=info.media_type,
        mime_type=info.mime_type,
        file_size=size,
        file_name=info.file_name,
        status=MediaStatus.EXTRACTED.value,
        content=content,
        reason="" if content else (empty_reason or "The downloaded asset was empty."),
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
