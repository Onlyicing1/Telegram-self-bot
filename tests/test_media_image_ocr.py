"""
Media Processing M1.3 — the bounded OCR boundary for still images.

The M1 boundary (``backend/services/media_service.py``) is unchanged in shape;
this file pins the M1.3 addition to it:

  1. OCR is a SEAM, not a dependency. Recognition is supplied through
     ``OcrEngine`` and provisioned with ``set_ocr_engine``; this phase ships no
     engine, so an image is reported ``UNSUPPORTED`` *without* being transferred
     and the project never silently acquires a heavy native stack.
  2. Every bound is finite and enforced at the point of use: the OCR input
     bound (applied BEFORE the transfer), the MIME/container signature match,
     the pre-decode bitmap bound (decompression-bomb guard), the recognition
     timeout, and the shared character ceiling.
  3. Recognition runs off the event loop (the same ``asyncio.to_thread`` pattern
     M1.2 uses for document parsing) and never blocks it.
  4. Honest outcomes only: no readable text reports empty content with a reason;
     a malformed/mismatched payload, an oversized bitmap, a timeout and an
     engine failure all raise ``MediaError`` instead of fabricated content.
  5. Normalization is deterministic and lossless for meaningful text: line
     structure survives, whitespace runs collapse, and Persian/Arabic text —
     including ZWNJ — passes through unchanged, in reading order.
  6. The model-facing rendering (``as_context_text``) still carries no caption,
     sender, chat id, message id or filename.

No live Telegram and no OCR dependency: the Telegram boundary is a scripted fake
shaped like the Telethon client surface the facade consumes, the media types are
real ``telethon.tl.types`` objects so the existing classifier runs unmodified,
and the image fixtures are real container bytes built in-process.
"""
from __future__ import annotations

import asyncio
import os
import struct
import tempfile
import threading
import time
import zlib
from typing import Any

import pytest
from telethon.tl.types import (
    Document,
    DocumentAttributeFilename,
    DocumentAttributeSticker,
    InputStickerSetEmpty,
    MessageMediaDocument,
    MessageMediaPhoto,
    Photo,
    PhotoSize,
)

from backend.services import media_service
from backend.services.media_service import (
    MAX_EXTRACTED_CHARS,
    MAX_IMAGE_PIXELS,
    MAX_IMAGE_SIDE,
    MAX_OCR_CHARS,
    MAX_OCR_INPUT_BYTES,
    MediaError,
    MediaStatus,
)

OWNER = 7770001
CHAT = -1007778889999
MESSAGE_ID = 574942
CAPTION = "caption-that-must-never-reach-the-model"
FILE_NAME = "scan.png"

_PERSIAN = "سلام، این یک آزمایش است"
#: Built from an explicit escape so the joiner can never be lost in transit.
_ZWNJ = "\u200c"
_PERSIAN_ZWNJ = "می" + _ZWNJ + "کند"
_MIXED = "این متن فارسی است\nand this line is English 12345"


# ── Fake Telegram surface (mirrors tests/test_media_processing.py) ──


class _FakeMessage:
    """The attribute surface ``backend/ai/media.classify_message`` inspects."""

    def __init__(self, media: Any, *, caption: str = CAPTION, mid: int = MESSAGE_ID,
                 chat_id: int = CHAT) -> None:
        self.media = media
        self.message = caption
        self.text = caption
        self.id = mid
        self.chat_id = chat_id


class _FakeClient:
    """A scripted, bounded-transfer-shaped Telethon client."""

    def __init__(self, payload: bytes = b"", *, error: Exception | None = None) -> None:
        self.payload = payload
        self.error = error
        self.calls: list[dict[str, Any]] = []

    async def download_media(self, message: Any, file: Any = None,
                             progress_callback: Any = None, **kwargs: Any) -> Any:
        self.calls.append({"op": "download_media", "file": file})
        if self.error is not None:
            raise self.error
        if file:
            with open(file, "wb") as handle:
                handle.write(self.payload)
            return file
        return None

    async def get_messages(self, *args: Any, **kwargs: Any) -> Any:  # pragma: no cover
        self.calls.append({"op": "get_messages"})
        raise AssertionError("the media boundary must never search for a message")

    async def iter_messages(self, *args: Any, **kwargs: Any) -> Any:  # pragma: no cover
        self.calls.append({"op": "iter_messages"})
        raise AssertionError("the media boundary must never scan for a message")


def _media_temp_dirs() -> set[str]:
    try:
        entries = os.listdir(tempfile.gettempdir())
    except OSError:  # pragma: no cover - defensive
        return set()
    return {name for name in entries if name.startswith("lifeos_media_")}


# ── Real image fixtures (stdlib encoders, no dependency) ──


def _png(width: int, height: int) -> bytes:
    """A complete, genuinely valid PNG built with ``zlib`` + ``struct``."""

    def chunk(tag: bytes, payload: bytes) -> bytes:
        body = tag + payload
        return struct.pack(">I", len(payload)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    raw = b"".join(b"\x00" + b"\x00\x00\x00" * width for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


def _png_header(width: int, height: int) -> bytes:
    """PNG header only — enough to declare dimensions without carrying a bitmap."""
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + ihdr + struct.pack(">I", 0)


def _jpeg(width: int, height: int) -> bytes:
    app0 = b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    sof0 = (
        b"\xff\xc0" + struct.pack(">H", 17) + b"\x08"
        + struct.pack(">HH", height, width)
        + b"\x03\x01\x11\x00\x02\x11\x00\x03\x11\x00"
    )
    return b"\xff\xd8" + app0 + sof0 + b"\xff\xd9"


def _gif(width: int, height: int) -> bytes:
    return b"GIF89a" + struct.pack("<HH", width, height) + b"\x00\x00\x00\x3b"


def _webp_vp8x(width: int, height: int) -> bytes:
    payload = (
        b"\x00" + b"\x00\x00\x00"
        + (width - 1).to_bytes(3, "little") + (height - 1).to_bytes(3, "little")
    )
    chunk = b"VP8X" + struct.pack("<I", len(payload)) + payload
    body = b"WEBP" + chunk
    return b"RIFF" + struct.pack("<I", len(body)) + body


def _bmp(width: int, height: int) -> bytes:
    return (
        b"BM" + struct.pack("<I", 54) + b"\x00\x00\x00\x00" + struct.pack("<I", 54)
        + struct.pack("<I", 40) + struct.pack("<ii", width, height)
        + b"\x01\x00\x18\x00"
    )


def _photo_media() -> MessageMediaPhoto:
    """Classifier labels this ``Photo`` with ``image/jpeg``."""
    return MessageMediaPhoto(photo=Photo(
        id=3, access_hash=3, file_reference=b"", date=None,
        sizes=[PhotoSize(type="y", w=8, h=8, size=len(_jpeg(8, 8)))], dc_id=1,
    ))


def _image_document(mime: str, name: str, size: int, *, sticker: bool = False) -> MessageMediaDocument:
    """A still-image document. ``sticker`` labels it ``Sticker`` instead of the
    type derived from ``mime`` — used for ``image/gif``, whose own media type
    (``GIF``) is outside Media Processing scope while the FORMAT is still a
    declared OCR format (see ``_IMAGE_SIGNATURE_READERS``)."""
    attributes: list[Any] = [DocumentAttributeFilename(file_name=name)]
    if sticker:
        attributes.append(DocumentAttributeSticker(alt="x", stickerset=InputStickerSetEmpty()))
    return MessageMediaDocument(document=Document(
        id=2, access_hash=2, file_reference=b"", date=None, mime_type=mime, size=size,
        dc_id=1, attributes=attributes,
    ))


# ── Deterministic scripted engines (the seam, not the pipeline) ──


class _ScriptedEngine:
    """Returns a fixed string; records how and on which thread it was called."""

    def __init__(self, text: str = "", *, error: Exception | None = None,
                 delay: float = 0.0) -> None:
        self.text = text
        self.error = error
        self.delay = delay
        self.calls: list[bytes] = []
        self.thread_names: list[str] = []

    def recognize(self, image: bytes) -> str:
        self.calls.append(image)
        self.thread_names.append(threading.current_thread().name)
        if self.delay:
            time.sleep(self.delay)
        if self.error is not None:
            raise self.error
        return self.text


@pytest.fixture(autouse=True)
def _reset_engine():
    """No global engine leaks between tests — the default is 'unprovisioned'."""
    previous = media_service.get_ocr_engine()
    media_service.set_ocr_engine(None)
    yield
    media_service.set_ocr_engine(previous)


# ── 1. The seam itself ──


def test_no_engine_is_provisioned_by_default():
    assert media_service.ocr_available() is False
    assert media_service.get_ocr_engine() is None


def test_engine_registry_round_trips():
    media_service.set_ocr_engine(_ScriptedEngine("x"))
    assert media_service.ocr_available() is True
    assert isinstance(media_service.get_ocr_engine(), _ScriptedEngine)
    media_service.set_ocr_engine(None)
    assert media_service.ocr_available() is False


@pytest.mark.parametrize("mime,present", [
    ("image/jpeg", True), ("image/jpg", True), ("image/png", True),
    ("image/webp", True), ("image/bmp", True), ("image/gif", True),
    ("IMAGE/PNG", True),
    ("image/tiff", False), ("video/mp4", False), ("application/pdf", False),
    ("text/plain", False), ("", False),
])
def test_image_mime_set_is_explicit(mime, present):
    assert media_service.is_image_mime(mime) is present


def test_is_extractable_mime_stays_dependency_and_engine_free():
    # The M1/M1.2 predicate keeps describing the dependency-free extractors only;
    # images are gated by is_image_mime + ocr_available, never folded into it.
    assert media_service.is_extractable_mime("image/jpeg") is False
    assert media_service.is_extractable_mime("application/pdf") is True
    assert media_service.is_extractable_mime("text/plain") is True


# ── 2. No engine: fail closed, and NOTHING is transferred ──


@pytest.mark.asyncio
async def test_unprovisioned_runtime_reports_images_unsupported_without_transfer():
    client = _FakeClient(payload=_jpeg(8, 8))
    analysis = await media_service.analyze_media(client, OWNER, _FakeMessage(_photo_media()))

    assert analysis.media_type == "Photo"
    assert analysis.status == MediaStatus.UNSUPPORTED
    assert analysis.content == ""
    assert analysis.has_content is False
    assert "OCR" in analysis.reason
    assert client.calls == [], "an image must not be transferred without an OCR engine"


@pytest.mark.asyncio
async def test_unprovisioned_runtime_reports_image_documents_unsupported_without_transfer():
    client = _FakeClient(payload=_png(4, 4))
    media = _image_document("image/png", FILE_NAME, len(_png(4, 4)))
    analysis = await media_service.analyze_media(client, OWNER, _FakeMessage(media))

    assert analysis.media_type == "Document"
    assert analysis.status == MediaStatus.UNSUPPORTED
    assert "OCR" in analysis.reason
    assert client.calls == []


# ── 3. Provisioned engine: the real end-to-end pipeline ──


@pytest.mark.asyncio
async def test_english_text_is_extracted_through_the_boundary():
    engine = _ScriptedEngine("Hello World 12345")
    media_service.set_ocr_engine(engine)
    client = _FakeClient(payload=_jpeg(8, 8))

    analysis = await media_service.analyze_media(client, OWNER, _FakeMessage(_photo_media()))

    assert analysis.status == MediaStatus.EXTRACTED
    assert analysis.content == "Hello World 12345"
    assert analysis.has_content is True
    assert analysis.truncated is False
    assert analysis.mime_type == "image/jpeg"
    assert len(engine.calls) == 1, "recognition must run exactly once"
    assert len(client.calls) == 1


@pytest.mark.asyncio
async def test_persian_text_survives_the_boundary_unchanged():
    engine = _ScriptedEngine(f"{_PERSIAN_ZWNJ} {_PERSIAN}")
    media_service.set_ocr_engine(engine)
    client = _FakeClient(payload=_png(4, 4))
    media = _image_document("image/png", FILE_NAME, len(_png(4, 4)))

    analysis = await media_service.analyze_media(client, OWNER, _FakeMessage(media))

    assert analysis.content == f"{_PERSIAN_ZWNJ} {_PERSIAN}"
    assert _ZWNJ in analysis.content, "ZWNJ must survive normalization"
    assert analysis.content.count(_ZWNJ) == 1
    assert _PERSIAN in analysis.as_context_text()


@pytest.mark.asyncio
async def test_mixed_direction_lines_keep_their_order():
    engine = _ScriptedEngine(_MIXED)
    media_service.set_ocr_engine(engine)
    client = _FakeClient(payload=_png(4, 4))
    media = _image_document("image/png", FILE_NAME, len(_png(4, 4)))

    analysis = await media_service.analyze_media(client, OWNER, _FakeMessage(media))

    assert analysis.content.split("\n") == _MIXED.split("\n")


@pytest.mark.asyncio
async def test_normalization_collapses_whitespace_but_keeps_line_structure():
    engine = _ScriptedEngine("  Hello   world  \n\n\n\n  second   line\there  \n\n")
    media_service.set_ocr_engine(engine)
    client = _FakeClient(payload=_png(4, 4))
    media = _image_document("image/png", FILE_NAME, len(_png(4, 4)))

    analysis = await media_service.analyze_media(client, OWNER, _FakeMessage(media))

    assert analysis.content == "Hello world\n\nsecond line here"


@pytest.mark.asyncio
async def test_raw_ocr_output_is_normalized_before_it_reaches_the_analysis():
    # The engine may return anything; only the normalized form is exposed.
    engine = _ScriptedEngine("\r\n\r\n  a  \r\n\r\n\r\n b \r\n\r\n")
    media_service.set_ocr_engine(engine)
    client = _FakeClient(payload=_png(4, 4))
    media = _image_document("image/png", FILE_NAME, len(_png(4, 4)))

    analysis = await media_service.analyze_media(client, OWNER, _FakeMessage(media))

    assert analysis.content == "a\n\nb"


# ── 4. Honest empty result (no fabricated text, no wasted provider round) ──


@pytest.mark.asyncio
async def test_no_readable_text_is_reported_honestly_without_content():
    engine = _ScriptedEngine("")
    media_service.set_ocr_engine(engine)
    client = _FakeClient(payload=_png(4, 4))
    media = _image_document("image/png", FILE_NAME, len(_png(4, 4)))

    analysis = await media_service.analyze_media(client, OWNER, _FakeMessage(media))

    assert analysis.status == MediaStatus.EXTRACTED
    assert analysis.content == ""
    assert analysis.has_content is False
    assert "no readable text" in analysis.reason.lower()
    rendered = analysis.as_context_text()
    assert "Content:" not in rendered
    assert analysis.reason in rendered


@pytest.mark.asyncio
async def test_whitespace_only_ocr_output_counts_as_no_text():
    engine = _ScriptedEngine("  \n\n \t \n ")
    media_service.set_ocr_engine(engine)
    client = _FakeClient(payload=_png(4, 4))
    media = _image_document("image/png", FILE_NAME, len(_png(4, 4)))

    analysis = await media_service.analyze_media(client, OWNER, _FakeMessage(media))

    assert analysis.content == ""
    assert analysis.has_content is False


@pytest.mark.asyncio
async def test_a_non_string_engine_result_is_treated_as_no_text():
    class _BadEngine:
        def recognize(self, image: bytes) -> Any:
            return None

    media_service.set_ocr_engine(_BadEngine())
    client = _FakeClient(payload=_png(4, 4))
    media = _image_document("image/png", FILE_NAME, len(_png(4, 4)))

    analysis = await media_service.analyze_media(client, OWNER, _FakeMessage(media))

    assert analysis.status == MediaStatus.EXTRACTED
    assert analysis.content == ""


# ── 5. Fail-closed container validation ──


@pytest.mark.asyncio
async def test_malformed_image_payload_fails_honestly():
    media_service.set_ocr_engine(_ScriptedEngine("never reached"))
    client = _FakeClient(payload=b"this is plainly not a png")
    media = _image_document("image/png", FILE_NAME, 31)
    before = _media_temp_dirs()

    with pytest.raises(MediaError) as error:
        await media_service.analyze_media(client, OWNER, _FakeMessage(media))

    assert "not a readable image" in str(error.value)
    assert _media_temp_dirs() == before


@pytest.mark.asyncio
async def test_mime_container_mismatch_is_refused():
    # Declares PNG, carries a real JPEG: the signature must win over the label.
    media_service.set_ocr_engine(_ScriptedEngine("never reached"))
    payload = _jpeg(8, 8)
    client = _FakeClient(payload=payload)
    media = _image_document("image/png", FILE_NAME, len(payload))

    with pytest.raises(MediaError) as error:
        await media_service.analyze_media(client, OWNER, _FakeMessage(media))

    assert "not a readable image of the declared type" in str(error.value)


# ``image/gif`` is a declared OCR format but the GIF *media type* is outside
# Media Processing scope, so its rows are labeled ``Sticker``: the format still
# reaches the OCR path (signature corroboration and decoding keep their
# coverage) while the scope refusal itself is pinned in
# tests/test_media_scope_and_delivery.py.
@pytest.mark.parametrize("mime,payload,sticker", [
    ("image/png", _jpeg(8, 8), False),
    ("image/jpeg", _png(4, 4), False),
    ("image/gif", _png(4, 4), True),
    ("image/bmp", _webp_vp8x(4, 4), False),
])
@pytest.mark.asyncio
async def test_every_declared_image_type_corroborates_its_signature(mime, payload, sticker):
    media_service.set_ocr_engine(_ScriptedEngine("never reached"))
    client = _FakeClient(payload=payload)
    media = _image_document(mime, FILE_NAME, len(payload), sticker=sticker)

    with pytest.raises(MediaError):
        await media_service.analyze_media(client, OWNER, _FakeMessage(media))


@pytest.mark.parametrize("mime,payload,sticker", [
    ("image/png", _png(4, 4), False),
    ("image/jpeg", _jpeg(8, 8), False),
    ("image/gif", _gif(4, 4), True),
    ("image/webp", _webp_vp8x(4, 4), False),
    ("image/bmp", _bmp(4, 4), False),
])
@pytest.mark.asyncio
async def test_every_declared_image_type_parses_when_the_signature_matches(mime, payload, sticker):
    engine = _ScriptedEngine("matched")
    media_service.set_ocr_engine(engine)
    client = _FakeClient(payload=payload)
    media = _image_document(mime, FILE_NAME, len(payload), sticker=sticker)

    analysis = await media_service.analyze_media(client, OWNER, _FakeMessage(media))

    assert analysis.content == "matched"
    assert len(engine.calls) == 1


# ── 6. Bounds ──


@pytest.mark.asyncio
async def test_image_beyond_the_ocr_input_bound_is_never_transferred():
    engine = _ScriptedEngine("never reached")
    media_service.set_ocr_engine(engine)
    client = _FakeClient(payload=_png(4, 4))
    media = _image_document("image/png", "huge.png", MAX_OCR_INPUT_BYTES + 1)

    with pytest.raises(MediaError) as error:
        await media_service.analyze_media(client, OWNER, _FakeMessage(media))

    assert "exceeds" in str(error.value)
    assert client.calls == [], "an image beyond the OCR bound must not be downloaded"
    assert engine.calls == [], "recognition must never start on a refused asset"


@pytest.mark.asyncio
async def test_oversized_bitmap_is_refused_before_any_decode():
    engine = _ScriptedEngine("never reached")
    media_service.set_ocr_engine(engine)
    payload = _png_header(MAX_IMAGE_SIDE * 2, MAX_IMAGE_SIDE * 2)
    client = _FakeClient(payload=payload)
    media = _image_document("image/png", FILE_NAME, len(payload))

    with pytest.raises(MediaError) as error:
        await media_service.analyze_media(client, OWNER, _FakeMessage(media))

    assert "pixel OCR bound" in str(error.value)
    assert engine.calls == []


@pytest.mark.asyncio
async def test_a_small_payload_cannot_declare_an_unbounded_bitmap():
    # Decompression bomb: a few bytes claiming a gigantic canvas.
    engine = _ScriptedEngine("never reached")
    media_service.set_ocr_engine(engine)
    payload = _png_header(60_000, 60_000)
    assert len(payload) < 100
    assert 60_000 * 60_000 > MAX_IMAGE_PIXELS
    client = _FakeClient(payload=payload)
    media = _image_document("image/png", FILE_NAME, len(payload))

    with pytest.raises(MediaError):
        await media_service.analyze_media(client, OWNER, _FakeMessage(media))

    assert engine.calls == []


@pytest.mark.asyncio
async def test_a_bitmap_inside_the_bound_is_accepted():
    engine = _ScriptedEngine("ok")
    media_service.set_ocr_engine(engine)
    payload = _png_header(1_000, 1_000)
    client = _FakeClient(payload=payload)
    media = _image_document("image/png", FILE_NAME, len(payload))

    analysis = await media_service.analyze_media(client, OWNER, _FakeMessage(media))

    assert analysis.content == "ok"
    assert 1_000 * 1_000 <= MAX_IMAGE_PIXELS


@pytest.mark.asyncio
async def test_ocr_text_is_capped_at_the_shared_character_ceiling():
    engine = _ScriptedEngine("ا" * (MAX_OCR_CHARS + 5_000))
    media_service.set_ocr_engine(engine)
    client = _FakeClient(payload=_png(4, 4))
    media = _image_document("image/png", FILE_NAME, len(_png(4, 4)))

    analysis = await media_service.analyze_media(client, OWNER, _FakeMessage(media))

    assert MAX_OCR_CHARS == MAX_EXTRACTED_CHARS
    assert analysis.truncated is True
    assert len(analysis.content) == MAX_OCR_CHARS
    assert analysis.content.endswith("…")
    # Persian is still Persian after truncation, not bullet-folded to bytes.
    assert analysis.content.startswith("ا")


@pytest.mark.asyncio
async def test_short_ocr_text_is_not_reported_as_truncated():
    engine = _ScriptedEngine("short answer")
    media_service.set_ocr_engine(engine)
    client = _FakeClient(payload=_png(4, 4))
    media = _image_document("image/png", FILE_NAME, len(_png(4, 4)))

    analysis = await media_service.analyze_media(client, OWNER, _FakeMessage(media))

    assert analysis.truncated is False
    assert analysis.content == "short answer"


# ── 7. Failure paths ──


@pytest.mark.asyncio
async def test_ocr_timeout_fails_honestly(monkeypatch):
    monkeypatch.setattr(media_service, "OCR_TIMEOUT_S", 0.02)
    media_service.set_ocr_engine(_ScriptedEngine("late", delay=0.4))
    client = _FakeClient(payload=_png(4, 4))
    media = _image_document("image/png", FILE_NAME, len(_png(4, 4)))
    before = _media_temp_dirs()

    started = time.monotonic()
    with pytest.raises(MediaError) as error:
        await media_service.analyze_media(client, OWNER, _FakeMessage(media))

    assert "did not finish" in str(error.value)
    assert time.monotonic() - started < 5.0
    assert _media_temp_dirs() == before


@pytest.mark.asyncio
async def test_engine_failure_is_wrapped_as_a_media_error():
    media_service.set_ocr_engine(_ScriptedEngine(error=RuntimeError("engine exploded")))
    client = _FakeClient(payload=_png(4, 4))
    media = _image_document("image/png", FILE_NAME, len(_png(4, 4)))
    before = _media_temp_dirs()

    with pytest.raises(MediaError) as error:
        await media_service.analyze_media(client, OWNER, _FakeMessage(media))

    assert "OCR failed" in str(error.value)
    assert _media_temp_dirs() == before


@pytest.mark.asyncio
async def test_missing_download_is_refused_before_recognition():
    engine = _ScriptedEngine("never reached")
    media_service.set_ocr_engine(engine)
    client = _FakeClient(payload=b"")
    media = _image_document("image/png", FILE_NAME, 40)

    with pytest.raises(MediaError):
        await media_service.analyze_media(client, OWNER, _FakeMessage(media))

    assert engine.calls == []


# ── 8. Execution model: off the event loop, bounded cleanup ──


@pytest.mark.asyncio
async def test_recognition_runs_off_the_event_loop():
    engine = _ScriptedEngine("threaded", delay=0.25)
    media_service.set_ocr_engine(engine)
    client = _FakeClient(payload=_png(4, 4))
    media = _image_document("image/png", FILE_NAME, len(_png(4, 4)))

    ticks = 0

    async def _ticker() -> None:
        nonlocal ticks
        while True:
            await asyncio.sleep(0.01)
            ticks += 1

    ticker = asyncio.create_task(_ticker())
    try:
        await media_service.analyze_media(client, OWNER, _FakeMessage(media))
    finally:
        ticker.cancel()
        with pytest.raises(asyncio.CancelledError):
            await ticker

    assert ticks >= 10, "the event loop must keep running during recognition"
    assert engine.thread_names and engine.thread_names[0] != threading.main_thread().name


@pytest.mark.asyncio
async def test_temp_directory_is_removed_on_success_failure_and_timeout(monkeypatch):
    before = _media_temp_dirs()

    media_service.set_ocr_engine(_ScriptedEngine("ok"))
    ok_client = _FakeClient(payload=_png(4, 4))
    ok_media = _image_document("image/png", FILE_NAME, len(_png(4, 4)))
    assert (await media_service.analyze_media(ok_client, OWNER, _FakeMessage(ok_media))).content == "ok"
    assert not os.path.exists(ok_client.calls[0]["file"])

    media_service.set_ocr_engine(_ScriptedEngine(error=RuntimeError("boom")))
    with pytest.raises(MediaError):
        await media_service.analyze_media(_FakeClient(payload=_png(4, 4)), OWNER,
                                          _FakeMessage(_image_document("image/png", FILE_NAME, 40)))

    monkeypatch.setattr(media_service, "OCR_TIMEOUT_S", 0.02)
    media_service.set_ocr_engine(_ScriptedEngine("late", delay=0.4))
    with pytest.raises(MediaError):
        await media_service.analyze_media(_FakeClient(payload=_png(4, 4)), OWNER,
                                          _FakeMessage(_image_document("image/png", FILE_NAME, 40)))

    assert _media_temp_dirs() == before


@pytest.mark.asyncio
async def test_cancellation_cleans_up_and_propagates():
    engine = _ScriptedEngine("slow", delay=30.0)
    media_service.set_ocr_engine(engine)
    client = _FakeClient(payload=_png(4, 4))
    media = _image_document("image/png", FILE_NAME, len(_png(4, 4)))
    before = _media_temp_dirs()

    task = asyncio.create_task(
        media_service.analyze_media(client, OWNER, _FakeMessage(media))
    )
    await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert _media_temp_dirs() == before


# ── 9. Zero Telegram context ──


@pytest.mark.asyncio
async def test_model_facing_rendering_excludes_every_telegram_identifier():
    engine = _ScriptedEngine("recognized text")
    media_service.set_ocr_engine(engine)
    client = _FakeClient(payload=_png(4, 4))
    media = _image_document("image/png", FILE_NAME, len(_png(4, 4)))

    analysis = await media_service.analyze_media(client, OWNER, _FakeMessage(media))
    rendered = analysis.as_context_text()

    assert "recognized text" in rendered
    assert CAPTION not in rendered
    assert str(MESSAGE_ID) not in rendered
    assert str(abs(CHAT)) not in rendered
    assert FILE_NAME not in rendered
    assert "Caption" not in rendered


@pytest.mark.asyncio
async def test_engine_sees_only_image_bytes_never_telegram_metadata():
    engine = _ScriptedEngine("x")
    media_service.set_ocr_engine(engine)
    payload = _png(4, 4)
    client = _FakeClient(payload=payload)
    media = _image_document("image/png", FILE_NAME, len(payload))

    await media_service.analyze_media(client, OWNER, _FakeMessage(media))

    assert engine.calls == [payload], "the engine receives the validated bytes and nothing else"


# ── 10. Existing behaviour is untouched ──


@pytest.mark.asyncio
async def test_ocr_engine_is_never_used_for_non_image_media():
    engine = _ScriptedEngine("must not be used")
    media_service.set_ocr_engine(engine)

    doc = Document(
        id=9, access_hash=9, file_reference=b"", date=None,
        mime_type="text/plain", size=5, dc_id=1,
        attributes=[DocumentAttributeFilename(file_name="notes.txt")],
    )
    client = _FakeClient(payload=b"plain")
    analysis = await media_service.analyze_media(
        client, OWNER, _FakeMessage(MessageMediaDocument(document=doc)),
    )

    assert analysis.content == "plain"
    assert engine.calls == []


@pytest.mark.asyncio
async def test_non_image_media_stays_unsupported_regardless_of_engine():
    engine = _ScriptedEngine("must not be used")
    media_service.set_ocr_engine(engine)

    client = _FakeClient(payload=b"x")
    media = _image_document("application/msword", "report.doc", 4096)
    analysis = await media_service.analyze_media(client, OWNER, _FakeMessage(media))

    assert analysis.status == MediaStatus.UNSUPPORTED
    assert client.calls == []
    assert engine.calls == []


@pytest.mark.asyncio
async def test_text_extraction_path_is_unaffected_by_the_ocr_seam():
    media_service.set_ocr_engine(_ScriptedEngine("ignored"))
    doc = Document(
        id=8, access_hash=8, file_reference=b"", date=None,
        mime_type="text/markdown", size=6, dc_id=1,
        attributes=[DocumentAttributeFilename(file_name="readme.md")],
    )
    client = _FakeClient(payload=b"# hi\n")
    analysis = await media_service.analyze_media(
        client, OWNER, _FakeMessage(MessageMediaDocument(document=doc)),
    )

    assert analysis.status == MediaStatus.EXTRACTED
    assert analysis.content == "# hi\n"
    assert analysis.truncated is False


def test_the_ocr_boundary_adds_no_import_of_an_ocr_library():
    from pathlib import Path

    source = Path(media_service.__file__).read_text(encoding="utf-8")
    for forbidden in ("rapidocr", "paddle", "pytesseract", "easyocr", "cv2", "onnxruntime", "PIL"):
        assert forbidden not in source, forbidden
    assert "import numpy" not in source
