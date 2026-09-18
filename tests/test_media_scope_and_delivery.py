"""
Media Processing scope + single-message media delivery.

Contract this file pins (the phase that takes Video/GIF out of Media Processing
and stops one media request from becoming a burst of Telegram messages):

  1. Video and GIF are explicitly OUTSIDE Media Processing. The refusal is
     deterministic — it comes from the single existing classifier's own label
     (``UNPROCESSABLE_MEDIA_TYPES``) and is decided BEFORE any capability check,
     so neither type can reach OCR, speech-to-text, text extraction, the bounded
     transfer or the model. GIF is the load-bearing case: ``image/gif`` IS a
     declared OCR format, so before this phase a GIF document was downloaded and
     handed to the OCR engine.
  2. The refusal the owner sees is the boundary's existing honest result
     (``⚠️ I can't process this <type> yet.`` + the scope reason) and the media
     route never consults the provider.
  3. Everything genuinely in scope is untouched: photos still reach OCR, Voice
     and Audio still reach STT, text/PDF/DOCX still extract, and the
     downloadable taxonomy is unchanged so a video reply still routes to the
     media path (a deterministic refusal) instead of silently becoming an LLM
     question about media it cannot see.
  4. A media answer is ONE logical result: it is delivered as ONE controlled
     Telegram response — one message when it fits, ONE attached document when it
     cannot — never one message per internal piece or per page. Nothing is
     truncated, and the normal (non-media) paginating delivery is unchanged.

No live Telegram and no network: the same scripted fakes the media suites use,
imported from them so there is exactly one harness per surface.
"""
from __future__ import annotations

import io
import zipfile
from types import SimpleNamespace

import pytest
from telethon.tl.types import (
    DocumentAttributeAnimated,
    DocumentAttributeFilename,
    DocumentAttributeVideo,
)

from backend.ai.engine.result import EngineResult
from backend.ai.tools.delivery import (
    SAFE_LIMIT,
    _format_chunks,
    _utf16_units,
    deliver_response,
    deliver_single_message,
)
from backend.bot.handlers.ai_unified import _is_media_result
from backend.services import media_ai_service, media_service
from backend.services.media_service import (
    UNPROCESSABLE_MEDIA_TYPES,
    MediaStatus,
)
from tests.test_media_ai_integration import (
    _FakeClient as _IntegrationClient,
    _ScriptedProvider,
    _dispatcher,
    _manager,
    _reply_context,
    _request,
    _video,
)
from tests.test_media_image_ocr import (
    _FakeClient as _OcrClient,
    _FakeMessage as _OcrMessage,
    _ScriptedEngine as _OcrEngine,
    _jpeg,
    _photo_media,
)
from tests.test_media_processing import (
    _FakeClient as _MediaClient,
    _FakeMessage as _MediaMessage,
    _document_media,
)
from tests.test_media_stt import (
    _FakeClient as _SttClient,
    _ScriptedEngine as _SttEngine,
    _ogg_opus,
    _voice_message,
)

OWNER = 7770001
CHAT = -1007778889999
MESSAGE_ID = 574942
FILE_NAME = "clip.bin"
TAIL_SENTINEL = "TAIL-SENTINEL-9"
ATTACHMENT_NAME = "media-extract.txt"


@pytest.fixture(autouse=True)
def _reset_engines():
    """No global engine leaks between tests — the default is 'unprovisioned'."""
    previous_ocr = media_service.get_ocr_engine()
    previous_stt = media_service.get_stt_engine()
    media_service.set_ocr_engine(None)
    media_service.set_stt_engine(None)
    yield
    media_service.set_ocr_engine(previous_ocr)
    media_service.set_stt_engine(previous_stt)


# ── Delivery harness ──


class _FakeEvent:
    """The Telethon event surface the delivery layer uses."""

    def __init__(self, *, chat_id: int = CHAT, client: object | None = None) -> None:
        self.chat_id = chat_id
        if client is not None:
            self.client = client
        self.edits: list[str] = []
        self.replies: list[str] = []

    async def edit(self, text=None, buttons=None, **kwargs) -> None:
        self.edits.append(text)

    async def reply(self, text=None, **kwargs) -> None:
        self.replies.append(text)


class _FakeSender:
    """The Telethon client surface the one-attachment path needs."""

    def __init__(self) -> None:
        self.attachments: list[tuple[object, bytes, dict]] = []

    async def send_file(self, entity, file, **kwargs) -> None:
        payload = file.getvalue() if hasattr(file, "getvalue") else b""
        self.attachments.append((entity, payload, kwargs))


class _SingleMessageClient:
    """A client that resolves ONE message and refuses any transfer."""

    def __init__(self, message) -> None:
        self.message = message
        self.ops: list[str] = []

    async def get_messages(self, chat_id, ids=None) -> object:
        self.ops.append("get_messages")
        return self.message

    async def download_media(self, message, file=None, progress_callback=None):  # pragma: no cover
        self.ops.append("download_media")
        raise AssertionError("an out-of-scope media type must never be transferred")


class _TrapManager:
    """A provider manager that fails the test if the media route ever consults it."""

    def __init__(self) -> None:
        self.calls = 0

    async def chat(self, *args, **kwargs):  # pragma: no cover
        self.calls += 1
        raise AssertionError("the provider must never see an out-of-scope media request")


def _long_text(lines: int = 40, width: int = 400) -> str:
    body = [f"line-{index:03d} " + ("x" * width) for index in range(lines)]
    body.append(TAIL_SENTINEL)
    return "\n".join(body)


def _docx_bytes(text: str) -> bytes:
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:body></w:document>"
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("word/document.xml", document)
    return buffer.getvalue()


# ── 1. The scope set is explicit ──


def test_the_out_of_scope_set_is_exactly_video_and_the_two_gif_labels():
    assert UNPROCESSABLE_MEDIA_TYPES == {"Video", "GIF", "Animation"}
    for media_type in ("Video", "GIF", "Animation"):
        assert media_service.is_unprocessable(media_type) is True
    for media_type in ("Photo", "Voice", "Audio", "Document", "Sticker", "Unknown", ""):
        assert media_service.is_unprocessable(media_type) is False


def test_a_gif_is_an_image_format_that_the_scope_gate_refuses():
    # The reason the scope gate must come first: image/gif IS a declared OCR
    # format, so capability alone would have accepted a GIF.
    assert media_service.is_image_mime("image/gif") is True


# ── 2. Video/GIF never enter an extraction path ──


@pytest.mark.asyncio
async def test_a_video_is_refused_before_any_capability_or_transfer():
    ocr, stt = _OcrEngine("never reached"), _SttEngine("never reached")
    media_service.set_ocr_engine(ocr)
    media_service.set_stt_engine(stt)
    client = _MediaClient(payload=b"x")
    media = _document_media("video/mp4", [DocumentAttributeVideo(duration=1, w=1, h=1)])

    analysis = await media_service.analyze_media(client, OWNER, _MediaMessage(media))

    assert analysis.media_type == "Video"
    assert analysis.status == MediaStatus.UNSUPPORTED
    assert analysis.content == ""
    assert "outside the Media Processing scope" in analysis.reason
    assert analysis.has_content is False
    assert client.calls == []
    assert ocr.calls == [] and stt.calls == []


@pytest.mark.parametrize("attributes,expected_type", [
    ([], "GIF"),
    ([DocumentAttributeAnimated()], "Animation"),
])
@pytest.mark.asyncio
async def test_a_gif_is_refused_in_both_of_its_telegram_shapes(attributes, expected_type):
    ocr = _OcrEngine("never reached")
    media_service.set_ocr_engine(ocr)
    client = _MediaClient(payload=b"GIF89a")
    media = _document_media("image/gif", attributes)

    analysis = await media_service.analyze_media(client, OWNER, _MediaMessage(media))

    assert analysis.media_type == expected_type
    assert analysis.status == MediaStatus.UNSUPPORTED
    assert "outside the Media Processing scope" in analysis.reason
    assert client.calls == [], "a GIF must never be transferred"
    assert ocr.calls == [], "a GIF must never reach the OCR engine"


@pytest.mark.asyncio
async def test_a_video_declaring_a_still_image_mime_is_still_out_of_scope():
    # Before this phase a DocumentAttributeVideo whose MIME is image/gif was
    # labeled Video and then matched the OCR capability by MIME alone.
    ocr = _OcrEngine("never reached")
    media_service.set_ocr_engine(ocr)
    client = _MediaClient(payload=b"GIF89a")
    media = _document_media("image/gif", [DocumentAttributeVideo(duration=1, w=1, h=1)])

    analysis = await media_service.analyze_media(client, OWNER, _MediaMessage(media))

    assert analysis.media_type == "Video"
    assert analysis.status == MediaStatus.UNSUPPORTED
    assert client.calls == []
    assert ocr.calls == []


@pytest.mark.asyncio
async def test_the_owner_gets_a_deterministic_refusal_and_no_provider_round():
    client = _SingleMessageClient(_MediaMessage(
        _document_media("video/mp4", [DocumentAttributeVideo(duration=1, w=1, h=1)]),
    ))
    manager = _TrapManager()

    answer = await media_ai_service.answer_media_request(
        client, OWNER,
        chat_id=CHAT, message_id=MESSAGE_ID,
        request_text="این ویدیو چیه؟",
        provider_manager=manager,
        request_id="scope",
    )

    assert answer.text.startswith("⚠️ I can't process this Video")
    assert "outside the Media Processing scope" in answer.text
    assert answer.provider == "local"
    assert manager.calls == 0
    assert client.ops == ["get_messages"]


@pytest.mark.asyncio
async def test_the_scope_refusal_reaches_the_owner_through_the_dispatcher():
    provider = _ScriptedProvider("SHOULD-NOT-RUN")
    client = _IntegrationClient(message=_video())
    dispatcher, _ = _dispatcher(_manager(provider), client, fail_prompt_build=True)

    result = await dispatcher.dispatch(
        _request(reply_context=_reply_context(media_type="Video"))
    )

    assert result.success is True
    assert "outside the Media Processing scope" in result.response
    assert provider.prompts == []
    assert "download_media" not in client.ops()


# ── 3. Everything genuinely in scope is untouched ──


@pytest.mark.asyncio
async def test_photos_still_reach_the_ocr_path():
    engine = _OcrEngine("recognized text")
    media_service.set_ocr_engine(engine)
    client = _OcrClient(payload=_jpeg(8, 8))

    analysis = await media_service.analyze_media(client, OWNER, _OcrMessage(_photo_media()))

    assert analysis.status == MediaStatus.EXTRACTED
    assert analysis.content == "recognized text"
    assert len(engine.calls) == 1


@pytest.mark.asyncio
async def test_voice_still_reaches_the_stt_path():
    engine = _SttEngine("transcript")
    media_service.set_stt_engine(engine)
    payload = _ogg_opus(2.0)
    client = _SttClient(payload=payload)

    analysis = await media_service.analyze_media(client, OWNER, _voice_message(payload))

    assert analysis.status == MediaStatus.EXTRACTED
    assert analysis.content == "transcript"
    assert len(engine.calls) == 1


@pytest.mark.asyncio
async def test_text_and_docx_documents_still_extract():
    text_payload = b"plain text stays extractable"
    text_client = _MediaClient(payload=text_payload)
    text_analysis = await media_service.analyze_media(
        text_client, OWNER,
        _MediaMessage(_document_media(
            "text/plain", [DocumentAttributeFilename(file_name="notes.txt")],
            size=len(text_payload),
        )),
    )
    assert text_analysis.status == MediaStatus.EXTRACTED
    assert text_analysis.content == "plain text stays extractable"

    docx_payload = _docx_bytes("DOCX body survives the scope change")
    docx_client = _MediaClient(payload=docx_payload)
    docx_analysis = await media_service.analyze_media(
        docx_client, OWNER,
        _MediaMessage(_document_media(
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            [DocumentAttributeFilename(file_name="report.docx")],
            size=len(docx_payload),
        )),
    )
    assert docx_analysis.status == MediaStatus.EXTRACTED
    assert docx_analysis.content == "DOCX body survives the scope change"


def test_pdf_and_the_downloadable_taxonomy_are_unchanged():
    assert media_service.is_extractable_mime("application/pdf") is True
    assert media_service.is_docx_mime(
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    ) is True
    # Video/GIF stay DOWNLOADABLE on purpose: the deterministic refusal belongs
    # to the media boundary, so a video reply still routes to the media path
    # (and never silently becomes an LLM question about media it cannot see).
    for media_type in ("Video", "GIF", "Animation", "Photo", "Voice", "Audio", "Document"):
        assert media_service.is_downloadable(media_type) is True
    for media_type in ("WebPage", "Contact", "Poll", "Location", "Unknown", ""):
        assert media_service.is_downloadable(media_type) is False


# ── 4. ONE media request → ONE controlled Telegram response ──


@pytest.mark.asyncio
async def test_a_normal_short_media_result_is_exactly_one_message():
    event = _FakeEvent()

    result = await deliver_single_message(event, "Nova read this", "متن کوتاه استخراج‌شده")

    assert result.success is True
    assert result.chunks_delivered == 1 and result.total_chunks == 1
    assert len(event.edits) == 1
    assert event.replies == []
    assert "متن کوتاه استخراج‌شده" in event.edits[0]
    assert _utf16_units(event.edits[0]) <= SAFE_LIMIT


@pytest.mark.asyncio
async def test_many_internal_pieces_are_one_logical_result_and_one_message():
    payload = ("\n".join(f"piece-{index:03d} value" for index in range(200))).encode()
    client = _MediaClient(payload=payload)

    analysis = await media_service.analyze_media(
        client, OWNER,
        _MediaMessage(_document_media(
            "text/plain", [DocumentAttributeFilename(file_name="long.txt")],
            size=len(payload),
        )),
    )

    assert analysis.status == MediaStatus.EXTRACTED
    assert analysis.content.count("\n") == 199, "internal pieces combine into ONE result"

    event = _FakeEvent()
    result = await deliver_single_message(event, "Nova read this", analysis.content)

    assert result.chunks_delivered == 1
    assert len(event.edits) == 1
    assert event.replies == [], "internal pieces must not become separate messages"


@pytest.mark.asyncio
async def test_a_large_media_result_is_one_attachment_and_never_a_burst():
    event = _FakeEvent()
    sender = _FakeSender()

    result = await deliver_single_message(
        event, "Nova transcribe this", _long_text(), client=sender,
    )

    assert result.success is True
    assert result.chunks_delivered == 1 and result.total_chunks == 1
    assert event.replies == [], "a large result must never become a burst of messages"
    assert len(sender.attachments) == 1, "exactly ONE controlled response is sent"

    entity, payload, kwargs = sender.attachments[0]
    assert entity == CHAT
    assert kwargs["force_document"] is True
    assert kwargs["attributes"][0].file_name == ATTACHMENT_NAME


@pytest.mark.asyncio
async def test_the_attachment_carries_the_complete_result_and_the_notice_is_one_line():
    event = _FakeEvent()
    sender = _FakeSender()

    await deliver_single_message(event, "Nova transcribe this", _long_text(), client=sender)

    decoded = sender.attachments[0][1].decode("utf-8")
    assert TAIL_SENTINEL in decoded, "the complete result must be preserved"
    assert _utf16_units(decoded) > SAFE_LIMIT, "the oversized result really did not fit"

    assert len(event.edits) == 1
    assert ATTACHMENT_NAME in event.edits[0]
    assert "truncated" in event.edits[0]
    assert TAIL_SENTINEL not in event.edits[0], "the notice never duplicates the content"


@pytest.mark.asyncio
async def test_an_undeliverable_attachment_falls_back_without_losing_content():
    # No client and no send_file: the attachment cannot be sent, so the existing
    # paginating delivery is used rather than dropping the result.
    event = _FakeEvent()

    result = await deliver_single_message(event, "Nova transcribe this", _long_text())

    assert result.success is True
    assert result.total_chunks > 1
    assert len(event.replies) == result.total_chunks - 1
    assert TAIL_SENTINEL in event.edits[0] + "".join(event.replies)


@pytest.mark.asyncio
async def test_secondary_notes_ride_with_a_fitting_media_answer():
    event = _FakeEvent()

    result = await deliver_single_message(
        event, "Nova read this", "متن کوتاه", notes=("_↻ Backup model used_",),
    )

    assert result.total_chunks == 1
    assert "Backup model used" in event.edits[0]


@pytest.mark.asyncio
async def test_secondary_notes_ride_on_the_oversized_notice_only():
    event = _FakeEvent()
    sender = _FakeSender()

    result = await deliver_single_message(
        event, "Nova transcribe this", _long_text(), client=sender,
        notes=("_↻ Backup model used_",),
    )

    assert result.total_chunks == 1
    assert len(sender.attachments) == 1
    assert "Backup model used" in event.edits[0]
    assert "Backup model used" not in sender.attachments[0][1].decode("utf-8")


@pytest.mark.asyncio
async def test_an_empty_media_result_stays_a_deterministic_failure_state():
    event = _FakeEvent()

    result = await deliver_single_message(event, "Nova read this", "   ")

    assert result.success is True
    assert result.chunks_delivered == 1
    assert event.replies == []
    assert "no response" in event.edits[0].lower()


# ── 5. Non-media delivery is untouched ──


@pytest.mark.asyncio
async def test_normal_short_ai_responses_are_unchanged():
    event = _FakeEvent()

    result = await deliver_response(event, "Nova hi", "پاسخ کوتاه")

    assert result.chunks_delivered == 1 and result.total_chunks == 1
    assert event.replies == []


@pytest.mark.asyncio
async def test_normal_long_ai_responses_still_paginate():
    event = _FakeEvent()

    result = await deliver_response(event, "Nova write a lot", _long_text())

    assert result.total_chunks == len(_format_chunks("Nova write a lot", _long_text()))
    assert result.total_chunks > 1
    assert len(event.replies) == result.total_chunks - 1
    assert TAIL_SENTINEL in event.edits[0] + "".join(event.replies)


# ── 6. The handler routes media answers to the single-message path ──


def test_media_results_are_recognized_by_the_stamped_media_action():
    media = SimpleNamespace(metadata={"ai_action": {"action": "media_analysis"}})
    other = SimpleNamespace(metadata={"ai_action": {"action": "delete_messages"}})

    assert _is_media_result(media) is True
    assert _is_media_result(other) is False
    assert _is_media_result(SimpleNamespace(metadata={})) is False
    assert _is_media_result(SimpleNamespace(metadata=None)) is False
    assert _is_media_result(SimpleNamespace()) is False


@pytest.mark.asyncio
async def test_the_dispatcher_stamps_a_media_answer_for_single_message_delivery():
    provider = _ScriptedProvider("SHOULD-NOT-RUN")
    client = _IntegrationClient(message=_video())
    dispatcher, _ = _dispatcher(_manager(provider), client, fail_prompt_build=True)

    result = await dispatcher.dispatch(
        _request(reply_context=_reply_context(media_type="Video"))
    )

    assert result.metadata["ai_action"]["action"] == "media_analysis"
    assert _is_media_result(result) is True


def test_a_non_media_result_is_not_routed_to_the_media_delivery():
    result = EngineResult(
        success=True,
        metadata={"ai_action": {"action": "delete_messages", "kind": "executable"}},
    )

    assert _is_media_result(result) is False
