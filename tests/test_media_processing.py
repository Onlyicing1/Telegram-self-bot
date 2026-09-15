"""
Media processing boundary — download safety, normalization, zero AI context.

Contract this file pins (M1 of the media processing phase):

  1. ONE bounded transfer primitive. The media service transfers through
     ``backend/telegram_api`` (``guarded_await``) — never through the raw
     client, never unbounded, and never more than the facade's finite ceiling.
  2. Fail-closed limits. Telegram's declared size is checked BEFORE any
     transfer, the transferred size is checked again afterwards, an empty or
     missing download is refused, and a media type this phase cannot turn into
     text is reported unsupported WITHOUT being transferred at all.
  3. Reliable cleanup. The per-operation temporary directory is removed on
     success, failure, timeout and cancellation, and an untrusted Telegram
     filename can never escape it.
  4. A provider-independent, serializable result that contains no Telethon
     object, no client reference and no filesystem handle — and whose only
     model-facing rendering carries NO Telegram conversational context
     (no caption, no sender, no chat id, no message id).
  5. Deterministic resolution. The service processes exactly the message the
     trusted runtime resolved; it never searches for a message.

No live Telegram and no providers: the Telegram boundary is a scripted fake
shaped like the Telethon client surface the facade consumes, and the media
types are real ``telethon.tl.types`` objects so the existing classifier runs
unmodified.
"""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any

import pytest
from telethon.tl.types import (
    Document,
    DocumentAttributeAnimated,
    DocumentAttributeAudio,
    DocumentAttributeFilename,
    DocumentAttributeSticker,
    DocumentAttributeVideo,
    GeoPoint,
    InputStickerSetEmpty,
    MessageMediaDocument,
    MessageMediaGeo,
    MessageMediaPhoto,
    Photo,
    PhotoSize,
)

from backend.services import media_service, settings_service
from backend.services.media_service import MediaError, MediaStatus
from backend.telegram_api import media as telegram_media
from backend.telegram_api.api import TelegramAPI
from backend.telegram_api.exceptions import TelegramTimeoutError

OWNER = 7770001
CHAT = -1007778889999
MESSAGE_ID = 574942
CAPTION = "caption-that-must-never-reach-the-model"
FILE_NAME = "notes.txt"


# ── Fake Telegram surface ──


class _FakeMessage:
    """The attribute surface ``backend/ai/media.classify_message`` inspects."""

    def __init__(self, media: Any, *, caption: str = "", mid: int = MESSAGE_ID,
                 chat_id: int = CHAT) -> None:
        self.media = media
        self.message = caption
        self.text = caption
        self.id = mid
        self.chat_id = chat_id


class _FakeClient:
    """A scripted Telethon-shaped client.

    ``get_messages``/``iter_messages`` exist so a test can prove the service
    never searches for a message — calling either one fails the test.
    """

    def __init__(self, payload: bytes = b"", *, delay: float = 0.0,
                 error: Exception | None = None, write: bool = True) -> None:
        self.payload = payload
        self.delay = delay
        self.error = error
        self.write = write
        self.calls: list[dict[str, Any]] = []
        self.cancelled = False

    async def download_media(self, message: Any, file: Any = None, progress_callback: Any = None,
                             **kwargs: Any) -> Any:
        self.calls.append({"op": "download_media", "message": message, "file": file})
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
            if self.error is not None:
                raise self.error
            if self.write and file:
                with open(file, "wb") as handle:
                    handle.write(self.payload)
                return file
            return None
        except asyncio.CancelledError:
            self.cancelled = True
            raise

    async def get_messages(self, *args: Any, **kwargs: Any) -> Any:  # pragma: no cover
        self.calls.append({"op": "get_messages"})
        raise AssertionError("the media boundary must never search for a message")

    async def iter_messages(self, *args: Any, **kwargs: Any) -> Any:  # pragma: no cover
        self.calls.append({"op": "iter_messages"})
        raise AssertionError("the media boundary must never scan for a message")


def _text_message(*, mime: str = "text/plain", name: str = FILE_NAME, size: int = 11,
                  mid: int = MESSAGE_ID, chat_id: int = CHAT, caption: str = CAPTION) -> _FakeMessage:
    """A real text document message (classifier runs unmodified)."""
    doc = Document(
        id=1, access_hash=1, file_reference=b"", date=None, mime_type=mime, size=size,
        dc_id=1, attributes=[DocumentAttributeFilename(file_name=name)],
    )
    return _FakeMessage(MessageMediaDocument(document=doc), caption=caption, mid=mid, chat_id=chat_id)


def _document_media(mime: str, attributes: list[Any], *, size: int = 4096) -> MessageMediaDocument:
    return MessageMediaDocument(document=Document(
        id=2, access_hash=2, file_reference=b"", date=None, mime_type=mime, size=size,
        dc_id=1, attributes=attributes,
    ))


def _media_temp_dirs() -> set[str]:
    """Leftover per-operation temp directories (the media boundary's prefix)."""
    try:
        entries = os.listdir(tempfile.gettempdir())
    except OSError:  # pragma: no cover - defensive
        return set()
    return {name for name in entries if name.startswith("lifeos_media_")}


# ── 1. Bounded transfer ──


@pytest.mark.asyncio
async def test_text_document_is_transferred_and_normalized():
    client = _FakeClient(payload=b"hello world")
    analysis = await media_service.analyze_media(client, OWNER, _text_message())

    assert analysis.status == MediaStatus.EXTRACTED
    assert analysis.content == "hello world"
    assert analysis.file_size == 11
    assert analysis.mime_type == "text/plain"
    assert analysis.media_type == "Document"
    assert analysis.has_content is True
    assert len(client.calls) == 1
    assert client.calls[0]["op"] == "download_media"


@pytest.mark.asyncio
async def test_transfer_is_routed_through_the_bounded_facade(monkeypatch):
    seen: dict[str, Any] = {}

    async def spy(client, message, file_path=None, progress_callback=None, timeout=None):
        seen["timeout"] = timeout
        seen["file_path"] = file_path
        return None

    monkeypatch.setattr(media_service.telegram_media, "download_media", spy)
    client = _FakeClient(payload=b"hello world")

    with pytest.raises(MediaError):
        await media_service.analyze_media(client, OWNER, _text_message(), timeout_s=7.0)

    assert seen["timeout"] == 7.0
    assert isinstance(seen["file_path"], str)
    assert client.calls == [], "the service must not download through the raw client"


@pytest.mark.asyncio
async def test_the_service_accepts_the_facade_and_downloads_identically():
    client = _FakeClient(payload=b"via facade")
    analysis = await media_service.analyze_media(TelegramAPI(client), OWNER, _text_message())

    assert analysis.content == "via facade"
    assert len(client.calls) == 1


@pytest.mark.asyncio
async def test_the_transfer_bound_is_finite_and_cannot_be_raised():
    assert 0 < telegram_media.MEDIA_DOWNLOAD_TIMEOUT_S < 600
    assert media_service.MEDIA_DOWNLOAD_TIMEOUT_S == telegram_media.MEDIA_DOWNLOAD_TIMEOUT_S

    assert media_service.download_timeout(None) == media_service.MEDIA_DOWNLOAD_TIMEOUT_S
    assert media_service.download_timeout(99999) == media_service.MEDIA_DOWNLOAD_TIMEOUT_S
    assert media_service.download_timeout(7.0) == 7.0
    assert media_service.download_timeout(0) == media_service.MEDIA_DOWNLOAD_TIMEOUT_S
    assert media_service.download_timeout(-5) == media_service.MEDIA_DOWNLOAD_TIMEOUT_S
    assert media_service.download_timeout("junk") == media_service.MEDIA_DOWNLOAD_TIMEOUT_S
    assert telegram_media._effective_timeout(99999) == telegram_media.MEDIA_DOWNLOAD_TIMEOUT_S


@pytest.mark.asyncio
async def test_transfer_timeout_is_bounded_and_surfaces_honestly():
    client = _FakeClient(payload=b"x", delay=30.0)
    before = _media_temp_dirs()
    started = time.monotonic()

    with pytest.raises(MediaError) as error:
        await media_service.analyze_media(client, OWNER, _text_message(), timeout_s=0.05)

    assert "timed out" in str(error.value).lower()
    assert time.monotonic() - started < 5.0
    assert _media_temp_dirs() == before


@pytest.mark.asyncio
async def test_facade_download_raises_a_typed_timeout():
    client = _FakeClient(payload=b"x", delay=30.0)
    with pytest.raises(TelegramTimeoutError):
        await telegram_media.download_media(client, _text_message(), timeout=0.05)


@pytest.mark.asyncio
async def test_facade_download_writes_the_file_and_returns_the_path():
    client = _FakeClient(payload=b"abc")
    with tempfile.TemporaryDirectory() as tmp:
        target = os.path.join(tmp, "media.bin")
        result = await telegram_media.download_media(client, _text_message(), file_path=target)

    assert result == target
    assert client.calls[0]["file"] == target


# ── 2. Fail-closed limits ──


@pytest.mark.asyncio
async def test_declared_size_is_refused_before_any_transfer():
    client = _FakeClient(payload=b"x" * 4096)
    with pytest.raises(MediaError) as error:
        await media_service.analyze_media(client, OWNER, _text_message(size=4096),
                                          size_limit_bytes=1024)

    assert "exceeds" in str(error.value)
    assert client.calls == [], "an oversized asset must never start a transfer"


@pytest.mark.asyncio
async def test_transferred_size_is_refused_after_the_transfer():
    client = _FakeClient(payload=b"x" * 4096)
    before = _media_temp_dirs()

    with pytest.raises(MediaError):
        await media_service.analyze_media(client, OWNER, _text_message(size=10),
                                          size_limit_bytes=1024)

    assert len(client.calls) == 1
    assert not os.path.exists(client.calls[0]["file"])
    assert _media_temp_dirs() == before


@pytest.mark.asyncio
async def test_the_size_limit_reuses_the_project_media_bound(monkeypatch):
    assert media_service.max_download_bytes() == settings_service.max_deep_save_mb() * 1024 * 1024

    monkeypatch.setattr(media_service.settings_service, "max_deep_save_mb", lambda: 7)
    assert media_service.max_download_bytes() == 7 * 1024 * 1024


@pytest.mark.asyncio
async def test_missing_and_empty_downloads_are_refused():
    before = _media_temp_dirs()

    with pytest.raises(MediaError, match="no media"):
        await media_service.analyze_media(_FakeClient(write=False), OWNER, _text_message())

    with pytest.raises(MediaError, match="empty"):
        await media_service.analyze_media(_FakeClient(payload=b""), OWNER, _text_message())

    assert _media_temp_dirs() == before


@pytest.mark.asyncio
async def test_transfer_failures_are_wrapped_as_media_errors():
    client = _FakeClient(error=RuntimeError("flood"))
    before = _media_temp_dirs()

    with pytest.raises(MediaError) as error:
        await media_service.analyze_media(client, OWNER, _text_message())

    assert "flood" in str(error.value)
    assert _media_temp_dirs() == before


# ── 3. Cleanup + path safety ──


@pytest.mark.asyncio
async def test_temp_directory_is_removed_on_success_and_failure():
    before = _media_temp_dirs()

    client = _FakeClient(payload=b"hello world")
    analysis = await media_service.analyze_media(client, OWNER, _text_message())
    assert analysis.content == "hello world"
    transferred = client.calls[0]["file"]
    assert not os.path.exists(transferred)
    assert _media_temp_dirs() == before

    with pytest.raises(MediaError):
        await media_service.analyze_media(_FakeClient(error=RuntimeError("boom")), OWNER, _text_message())
    assert _media_temp_dirs() == before


@pytest.mark.asyncio
async def test_cancellation_cleans_up_and_propagates():
    client = _FakeClient(payload=b"x", delay=30.0)
    before = _media_temp_dirs()

    task = asyncio.create_task(
        media_service.analyze_media(client, OWNER, _text_message(), timeout_s=20.0)
    )
    await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert client.cancelled is True
    assert _media_temp_dirs() == before


@pytest.mark.parametrize("name", [
    "../../../../etc/cron.d/evil",
    "/etc/passwd",
    "..",
    "",
    "a/b/../../../../x",
    "..\\..\\windows\\evil",
    "\x00null.txt",
])
@pytest.mark.asyncio
async def test_untrusted_filenames_cannot_escape_the_temp_directory(name):
    client = _FakeClient(payload=b"hello world")
    analysis = await media_service.analyze_media(client, OWNER, _text_message(name=name))

    assert analysis.status == MediaStatus.EXTRACTED
    destination = client.calls[0]["file"]
    root = os.path.realpath(tempfile.gettempdir())
    assert os.path.realpath(destination).startswith(root + os.sep)
    assert os.path.basename(destination) in {"evil", "passwd", "x", "null.txt", "media.bin"}
    assert not os.path.exists(destination)


# ── 4. Classification, unsupported assets, malformed media ──


@pytest.mark.parametrize("media_type,present", [
    ("Photo", True), ("Voice", True), ("Audio", True), ("Document", True),
    ("Video", True), ("Sticker", True), ("Animation", True), ("GIF", True),
    ("WebPage", False), ("Contact", False), ("Poll", False), ("Location", False),
    ("Unknown", False), ("", False),
])
@pytest.mark.asyncio
async def test_downloadable_taxonomy_matches_the_existing_classifier(media_type, present):
    assert media_service.is_downloadable(media_type) is present


@pytest.mark.parametrize("mime,extractable", [
    ("text/plain", True), ("text/markdown", True), ("text/csv", True),
    ("application/json", True), ("application/x-sh", True),
    ("application/pdf", True), ("application/x-pdf", True),
    ("application/vnd.openxmlformats-officedocument.wordprocessingml.document", True),
    ("image/jpeg", False), ("audio/ogg", False), ("video/mp4", False),
    ("application/msword", False), ("application/vnd.ms-excel", False),
    ("application/zip", False), ("", False),
])
@pytest.mark.asyncio
async def test_extractable_mime_covers_text_and_the_container_formats(mime, extractable):
    assert media_service.is_extractable_mime(mime) is extractable


@pytest.mark.parametrize("mime", ["text/plain", "text/markdown", "application/json"])
@pytest.mark.asyncio
async def test_common_text_assets_are_extracted(mime):
    client = _FakeClient(payload=b'{"a": 1}')
    analysis = await media_service.analyze_media(client, OWNER, _text_message(mime=mime))
    assert analysis.status == MediaStatus.EXTRACTED
    assert analysis.content == '{"a": 1}'


@pytest.mark.asyncio
async def test_assets_without_an_extractor_are_never_transferred():
    cases = {
        "Photo": MessageMediaPhoto(
            photo=Photo(id=3, access_hash=3, file_reference=b"", date=None,
                        sizes=[PhotoSize(type="y", w=1, h=1, size=2048)], dc_id=1)
        ),
        "Video": _document_media("video/mp4", [DocumentAttributeVideo(duration=1, w=1, h=1)]),
        "Voice": _document_media("audio/ogg", [DocumentAttributeAudio(duration=1, voice=True)]),
        "Audio": _document_media("audio/mpeg", [DocumentAttributeAudio(duration=1, voice=False)]),
        "Sticker": _document_media("image/webp", [DocumentAttributeSticker(
            alt="x", stickerset=InputStickerSetEmpty())]),
        "Animation": _document_media("image/gif", [DocumentAttributeAnimated()]),
        "GIF": _document_media("image/gif", []),
        "Document": _document_media("application/msword", [
            DocumentAttributeFilename(file_name="report.doc")]),
    }

    for expected_type, media in cases.items():
        client = _FakeClient(payload=b"x")
        analysis = await media_service.analyze_media(client, OWNER, _FakeMessage(media, caption=CAPTION))

        assert analysis.media_type == expected_type, expected_type
        assert analysis.status == MediaStatus.UNSUPPORTED
        assert analysis.content == ""
        assert analysis.reason, expected_type
        assert analysis.has_content is False
        assert client.calls == [], f"{expected_type} must not be transferred"


@pytest.mark.asyncio
async def test_non_downloadable_media_is_reported_unsupported_not_fetched():
    client = _FakeClient(payload=b"x")
    message = _FakeMessage(MessageMediaGeo(geo=GeoPoint(long=0.0, lat=0.0, access_hash=1)))

    analysis = await media_service.analyze_media(client, OWNER, message)

    assert analysis.media_type == "Location"
    assert analysis.status == MediaStatus.UNSUPPORTED
    assert "not a downloadable media asset" in analysis.reason
    assert client.calls == []


@pytest.mark.asyncio
async def test_message_without_media_is_refused_deterministically():
    client = _FakeClient(payload=b"x")
    with pytest.raises(MediaError, match="carries no media"):
        await media_service.analyze_media(client, OWNER, _FakeMessage(None))
    assert client.calls == []


@pytest.mark.asyncio
async def test_unresolved_target_is_refused_deterministically():
    client = _FakeClient(payload=b"x")
    with pytest.raises(MediaError, match="No media message was resolved"):
        await media_service.analyze_media(client, OWNER, None)
    assert client.calls == []


@pytest.mark.asyncio
async def test_missing_telegram_source_is_refused():
    with pytest.raises(MediaError, match="No Telegram client"):
        await media_service.analyze_media(None, OWNER, _text_message())


@pytest.mark.asyncio
async def test_no_message_is_ever_searched_for():
    client = _FakeClient(payload=b"hello world")
    await media_service.analyze_media(client, OWNER, _text_message())
    assert [call["op"] for call in client.calls] == ["download_media"]


@pytest.mark.asyncio
async def test_a_malformed_document_is_reported_unsupported_rather_than_guessed():
    client = _FakeClient(payload=b"x")
    message = _FakeMessage(MessageMediaDocument(document=None))

    analysis = await media_service.analyze_media(client, OWNER, message)

    assert analysis.status == MediaStatus.UNSUPPORTED
    assert analysis.mime_type == ""
    assert client.calls == []


# ── extracted content is bounded ──


@pytest.mark.asyncio
async def test_extracted_text_is_bounded_and_reported_as_truncated():
    payload = b"a" * (media_service.MAX_EXTRACTED_CHARS + 500)
    client = _FakeClient(payload=payload)

    analysis = await media_service.analyze_media(client, OWNER, _text_message(size=len(payload)))

    assert analysis.status == MediaStatus.EXTRACTED
    assert analysis.truncated is True
    assert len(analysis.content) == media_service.MAX_EXTRACTED_CHARS
    assert analysis.file_size == len(payload)


@pytest.mark.asyncio
async def test_undecodable_text_asset_does_not_raise():
    client = _FakeClient(payload=b"\xff\xfe\x00binary-ish")
    analysis = await media_service.analyze_media(client, OWNER, _text_message())
    assert analysis.status == MediaStatus.EXTRACTED
    assert analysis.content


# ── 5. Normalized, serializable, zero-context representation ──


@pytest.mark.asyncio
async def test_analysis_is_plain_serializable_data():
    client = _FakeClient(payload=b"hello world")
    analysis = await media_service.analyze_media(client, OWNER, _text_message())

    payload = analysis.as_dict()
    assert set(payload) == {
        "media_type", "mime_type", "file_size", "file_name", "status", "content",
        "reason", "caption", "source_chat_id", "source_message_id", "truncated",
    }
    assert all(isinstance(value, (str, int, bool)) for value in payload.values())
    assert all(not hasattr(value, "media") for value in payload.values())
    assert "telethon" not in json.dumps(payload).lower()


@pytest.mark.asyncio
async def test_model_facing_rendering_excludes_all_telegram_context():
    client = _FakeClient(payload=b"hello world")
    analysis = await media_service.analyze_media(client, OWNER, _text_message())

    rendered = analysis.as_context_text()

    assert "hello world" in rendered
    assert CAPTION not in rendered
    assert str(MESSAGE_ID) not in rendered
    assert str(abs(CHAT)) not in rendered
    assert FILE_NAME not in rendered
    assert "Caption" not in rendered


@pytest.mark.asyncio
async def test_unsupported_rendering_states_the_reason_and_claims_no_content():
    client = _FakeClient(payload=b"x")
    analysis = await media_service.analyze_media(
        client, OWNER, _FakeMessage(_document_media("application/msword", [
            DocumentAttributeFilename(file_name="report.doc")]), caption=CAPTION)
    )

    rendered = analysis.as_context_text()

    assert "Status: unsupported" in rendered
    assert analysis.reason in rendered
    assert "Content:" not in rendered
    assert CAPTION not in rendered


# ── 6. Architectural boundaries ──


@pytest.mark.asyncio
async def test_the_boundary_imports_no_telethon_and_no_provider():
    source = Path(media_service.__file__).read_text(encoding="utf-8")

    assert "import telethon" not in source
    assert "from telethon" not in source
    assert "backend.ai.providers" not in source
    assert "provider_manager" not in source
    assert "chat(" not in source
