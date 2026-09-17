"""
Media Processing — the DEDICATED Gemini transcription model as a controlled STT
engine option (the Persian recognition experiment).

The live Persian failure is a recognition-quality question: the direct-STT path
already delivers the engine's transcript verbatim (M1.6), so the remaining lever
is WHICH model transcribes the audio and HOW its request is configured. This file
pins the implementation of that lever, not its quality:

  1. ``GeminiMediaEngine`` stays the ONE implementation of both seams. OCR always
     uses the existing general media model; only ``transcribe`` can move.
  2. STT model selection is explicit and deterministic: the dedicated model
     (:data:`DEDICATED_TRANSCRIPTION_MODEL`) is used only when it is named in
     ``AI_GEMINI_STT_MODEL``. Nothing is silently replaced and the media boundary
     (``media_service``), the download path and the direct-STT answer contract are
     untouched.
  3. The dedicated model speaks a DIFFERENT documented API surface
     (``POST /v1beta/interactions`` with ``generation_config.transcription_config``),
     so the engine sends its own documented request there — never the Generate
     Content shape, and never a field the transcription contract does not define
     (no temperature/topK/topP/candidateCount/maxOutputTokens/system_instruction,
     no timestamps, no diarization, no custom vocabulary).
  4. Verbatim mode is sent explicitly (Smart transcription is never requested);
     the language is pinned with ``language_codes`` ONLY when
     ``AI_GEMINI_STT_LANGUAGE`` is configured, so automatic detection stays the
     default.
  5. The same bounded transport applies: inline base64 under the existing budget,
     the existing Files API upload flow (deleted in ``finally``) above it, one
     request per operation, the same timeout, the same error normalization and NO
     fallback to another model — so the A/B/C/D comparison stays interpretable.

The HTTP boundary is a scripted ``httpx`` transport: no test needs a real Gemini
credential, no byte leaves the process, and no test proves anything about actual
recognition quality — that requires the owner's live Telegram comparison.
"""
from __future__ import annotations

import base64
import inspect
import io
import json
import os
import struct
import tempfile
import time
import wave
import zlib
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from telethon.tl.types import (
    Document,
    DocumentAttributeAudio,
    DocumentAttributeFilename,
    MessageMediaDocument,
)

from backend.ai.conversation.context_builder import ReplyContext
from backend.ai.engine.dispatcher import Dispatcher
from backend.ai.engine.hooks import NOOP_HOOKS
from backend.ai.engine.metrics import EngineMetrics
from backend.ai.providers.base.capabilities import ProviderCapabilities
from backend.ai.providers.base.config import ProviderConfig
from backend.ai.providers.base.contract import BaseProvider, ProviderResponse
from backend.ai.providers.manager.manager import ProviderManager
from backend.ai.providers.registry.registry import ProviderRegistry
from backend.ai.session.request import AIRequest
from backend.ai.tools.context import ToolContext
from backend.ai.tools.executor import ToolExecutor
from backend.ai.tools.registry import create_default_registry
from backend.services import gemini_media_engine as engine_module
from backend.services import media_ai_service, media_service
from backend.services.gemini_media_engine import (
    DEFAULT_MEDIA_MODEL,
    DEDICATED_TRANSCRIPTION_MODEL,
    GEMINI_API_BASE,
    GEMINI_UPLOAD_BASE,
    INTERACTIONS_ENDPOINT,
    OCR_INSTRUCTION,
    STT_INSTRUCTION,
    STT_LANGUAGE_ENV_VAR,
    STT_MODEL_ENV_VAR,
    GeminiMediaEngine,
    build_gemini_media_engine,
    provision_gemini_media_engines,
    resolve_stt_language,
    resolve_stt_model,
    stt_instruction,
)
from backend.services.media_service import MediaError, MediaStatus
from backend.telegram_api.api import TelegramAPI

OWNER = 7770003
CHAT = -1007778889997
REPLY_ID = 58200
REQUEST_ID = 58201
API_KEY = "test-dedicated-stt-key-not-a-credential"
CAPTION = "caption-must-never-reach-the-transcription-model-9d1f"
FILE_NAME = "voice-filename-must-never-reach-the-transcription-model.ogg"
_UPLOAD_URL = "https://upload.example.invalid/files?upload_id=dedicated"
_FILE_NAME = "files/dedicated123"
_FILE_URI = f"{GEMINI_API_BASE}/{_FILE_NAME}"
#: The engine's own Persian sample and a Latin one: a distinctive Latin transcript
#: proves the answer is the engine's text rather than a scripted provider's.
TRANSCRIPT = "Dia de ventos e de cap. Xi, zabolié. Olha, sei chat."
_PERSIAN = "سلام، این یک آزمایش است"
_ZWNJ = "\u200c"
_PERSIAN_ZWNJ = "می" + _ZWNJ + "کند"


# ── Scripted API transport (the whole HTTP boundary, controlled) ──


def _interaction(text: str = "stub transcript", status: str = "completed") -> dict[str, Any]:
    """The documented Interactions response shape: ``model_output`` steps."""
    content: list[dict[str, Any]] = []
    if text:
        content.append({"type": "text", "text": text})
    return {
        "id": "interactions/scripted",
        "status": status,
        "steps": [{"id": "step_001", "type": "model_output", "content": content}],
    }


class _ApiStub(httpx.BaseTransport):
    """A recording transport for BOTH API surfaces, with zero network I/O."""

    def __init__(
        self,
        *,
        transcript: str = "stub transcript",
        interaction_status: str = "completed",
        interaction_payload: Any = None,
        interaction_raw: bytes | None = None,
        upload_payload: Any = None,
        interaction_statuses: list[int] | None = None,
        http_status: int = 200,
        generate_text: str = "stub ocr text",
        upload_status: int = 200,
        delete_status: int = 200,
        file_state: str = "ACTIVE",
        detail: str = "",
        raise_error: Exception | None = None,
        delay: float = 0.0,
    ) -> None:
        self.transcript = transcript
        self.interaction_status = interaction_status
        self.interaction_payload = interaction_payload
        self.interaction_raw = interaction_raw
        self.upload_payload = upload_payload
        #: Per-call statuses for the interaction endpoint, so a test can script
        #: "the first attempt fails, the bounded second one succeeds". The last
        #: value repeats; ``http_status`` is used when this is not given.
        self.interaction_statuses = list(interaction_statuses or [])
        self._interaction_calls = 0
        self.http_status = http_status
        self.generate_text = generate_text
        self.upload_status = upload_status
        self.delete_status = delete_status
        self.file_state = file_state
        self.detail = detail
        self.raise_error = raise_error
        self.delay = delay
        self.requests: list[httpx.Request] = []
        self.deleted: list[str] = []

    # ── transport protocol ──

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.delay:
            time.sleep(self.delay)
        if self.raise_error is not None:
            raise self.raise_error
        url = str(request.url)
        if request.method == "POST" and url.endswith("/upload/v1beta/files"):
            if self.upload_status >= 400:
                return self._error(request, self.upload_status)
            return httpx.Response(
                self.upload_status, headers={"x-goog-upload-url": _UPLOAD_URL}, request=request,
            )
        if request.method == "POST" and url == _UPLOAD_URL:
            if self.upload_status >= 400:
                return self._error(request, self.upload_status)
            # The finalize response carries the FILE, never the interaction body:
            # the two are separate endpoints with separate response shapes.
            payload = self.upload_payload or {
                "file": {"name": _FILE_NAME, "uri": _FILE_URI, "state": self.file_state}
            }
            return httpx.Response(200, json=payload, request=request)
        if request.method == "DELETE":
            if self.delete_status >= 400:
                return self._error(request, self.delete_status)
            self.deleted.append(url)
            return httpx.Response(200, json={}, request=request)
        if request.method == "GET":
            return httpx.Response(
                200, json={"name": _FILE_NAME, "uri": _FILE_URI, "state": self.file_state},
                request=request,
            )
        if url == INTERACTIONS_ENDPOINT:
            status = self.http_status
            if self.interaction_statuses:
                index = min(self._interaction_calls, len(self.interaction_statuses) - 1)
                status = self.interaction_statuses[index]
            self._interaction_calls += 1
            if status >= 400:
                return self._error(request, status)
            if self.interaction_raw is not None:
                return httpx.Response(200, content=self.interaction_raw, request=request)
            if self.interaction_payload is not None:
                return httpx.Response(200, json=self.interaction_payload, request=request)
            return httpx.Response(
                200, json=_interaction(self.transcript, self.interaction_status), request=request,
            )
        if url.endswith(":generateContent"):
            if self.http_status >= 400:
                return self._error(request, self.http_status)
            return httpx.Response(
                200,
                json={
                    "candidates": [
                        {
                            "content": {"parts": [{"text": self.generate_text}]},
                            "finishReason": "STOP",
                        }
                    ]
                },
                request=request,
            )
        return httpx.Response(
            404, json={"error": {"message": f"unexpected endpoint {url}"}}, request=request,
        )

    # ── helpers ──

    def _error(self, request: httpx.Request, status: int) -> httpx.Response:
        return httpx.Response(
            status,
            json={"error": {"message": self.detail or "scripted failure", "code": status}},
            request=request,
        )

    @property
    def interaction_requests(self) -> list[httpx.Request]:
        return [r for r in self.requests if str(r.url) == INTERACTIONS_ENDPOINT]

    @property
    def generate_requests(self) -> list[httpx.Request]:
        return [r for r in self.requests if str(r.url).endswith(":generateContent")]

    @property
    def upload_requests(self) -> list[httpx.Request]:
        return [r for r in self.requests if str(r.url).endswith("/upload/v1beta/files")]

    def sent_bytes(self) -> bytes:
        return b"\n".join(r.content for r in self.requests)


@pytest.fixture
def stub(monkeypatch):
    """Install a scripted transport under the engine (no real key, no network)."""

    def _install(**kwargs: Any) -> _ApiStub:
        transport = _ApiStub(**kwargs)
        real_client = httpx.Client

        def factory(*args: Any, **inner: Any) -> httpx.Client:
            inner["transport"] = transport
            return real_client(*args, **inner)

        monkeypatch.setattr(httpx, "Client", factory)
        return transport

    return _install


def _body(request: httpx.Request) -> dict[str, Any]:
    return json.loads(request.content.decode("utf-8"))


def _config(request: httpx.Request) -> dict[str, Any]:
    return _body(request)["generation_config"]["transcription_config"]


def _audio_item(request: httpx.Request) -> dict[str, Any]:
    body = _body(request)
    assert len(body["input"]) == 1, "one audio item only — no text instruction is sent"
    return body["input"][0]


def _engine(
    model: str = DEFAULT_MEDIA_MODEL, *, stt_model: str = "", stt_language: str = "",
) -> GeminiMediaEngine:
    return GeminiMediaEngine(
        API_KEY, model, key_env_var="AI_GEMINI_API_KEY",
        stt_model=stt_model, stt_language=stt_language,
    )


def _dedicated(language: str = "") -> GeminiMediaEngine:
    return _engine(stt_model=DEDICATED_TRANSCRIPTION_MODEL, stt_language=language)


# ── Real container fixtures (stdlib encoders, no dependency) ──


def _png(width: int, height: int) -> bytes:
    def chunk(tag: bytes, payload: bytes) -> bytes:
        body = tag + payload
        return struct.pack(">I", len(payload)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    raw = b"".join(b"\x00" + b"\x00\x00\x00" * width for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")
    )


def _opus_head() -> bytes:
    return (
        b"OpusHead" + bytes([1, 1]) + (312).to_bytes(2, "little")
        + (48_000).to_bytes(4, "little") + (0).to_bytes(2, "little") + bytes([0])
    )


def _ogg_page(header_type: int, granule: int, sequence: int, packet: bytes) -> bytes:
    lacing: list[int] = []
    remaining = len(packet)
    while remaining >= 255:
        lacing.append(255)
        remaining -= 255
    lacing.append(remaining)
    return (
        b"OggS" + bytes([0, header_type]) + granule.to_bytes(8, "little")
        + (1).to_bytes(4, "little") + sequence.to_bytes(4, "little")
        + (0).to_bytes(4, "little") + bytes([len(lacing)]) + bytes(lacing) + packet
    )


def _ogg_opus(duration_s: float = 2.0) -> bytes:
    granule = int(duration_s * 48_000)
    return (
        _ogg_page(0x02, 0, 0, _opus_head())
        + _ogg_page(0x04, granule, 1, b"\x00" * 40)
    )


def _wav(duration_s: float = 1.0, *, channels: int = 1, sample_rate: int = 16_000) -> bytes:
    buffer = io.BytesIO()
    handle = wave.open(buffer, "wb")
    handle.setnchannels(channels)
    handle.setsampwidth(2)
    handle.setframerate(sample_rate)
    handle.writeframes(b"\x00\x00" * int(duration_s * sample_rate) * channels)
    handle.close()
    return buffer.getvalue()


def _flac(duration_s: float = 1.0, *, channels: int = 1, sample_rate: int = 16_000) -> bytes:
    total_samples = int(duration_s * sample_rate)
    packed = (sample_rate << 44) | ((channels - 1) << 41) | total_samples
    streaminfo = b"\x00" * 10 + packed.to_bytes(8, "big") + b"\x00" * 16
    return b"fLaC" + b"\x00" + (34).to_bytes(3, "big") + streaminfo


def _large_wav() -> bytes:
    """Inside the STT bound, past the inline budget ⇒ the Files API is required."""
    payload = _wav(290.0, channels=2, sample_rate=16_000)
    assert engine_module.INLINE_PAYLOAD_MAX_BYTES < len(payload) <= media_service.MAX_STT_INPUT_BYTES
    return payload


# ── Fake Telegram surface ──


class _FakeMessage:
    """The attribute surface ``backend/ai/media.classify_message`` inspects."""

    def __init__(self, media: Any, *, caption: str = CAPTION, mid: int = REPLY_ID,
                 chat_id: int = CHAT) -> None:
        self.media = media
        self.message = caption
        self.text = caption
        self.id = mid
        self.chat_id = chat_id


class _BoundaryClient:
    """Boundary-only client: transfer scripted, and message SEARCH forbidden."""

    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.calls: list[dict[str, Any]] = []

    async def download_media(self, message: Any, file: Any = None, **kwargs: Any) -> Any:
        self.calls.append({"op": "download_media", "file": file})
        if file:
            with open(file, "wb") as handle:
                handle.write(self.payload)
            return file
        return None

    async def get_messages(self, *args: Any, **kwargs: Any) -> Any:  # pragma: no cover
        raise AssertionError("the media boundary must never search for a message")


class _TelegramClient:
    """The dispatcher's client: one resolved message plus one bounded transfer."""

    def __init__(self, message: Any, payload: bytes) -> None:
        self.message = message
        self.payload = payload
        self.ops: list[str] = []

    async def get_messages(self, chat_id: Any, ids: Any = None, **kwargs: Any) -> Any:
        self.ops.append("get_messages")
        return self.message

    async def download_media(self, message: Any, file: Any = None, **kwargs: Any) -> Any:
        self.ops.append("download_media")
        if file:
            with open(file, "wb") as handle:
                handle.write(self.payload)
            return file
        return io.BytesIO(self.payload)


def _voice_message(payload: bytes, *, mime: str = "audio/wav",
                   name: str = FILE_NAME) -> _FakeMessage:
    doc = Document(
        id=1, access_hash=1, file_reference=b"", date=None, mime_type=mime,
        size=len(payload), dc_id=1,
        attributes=[
            DocumentAttributeAudio(duration=2, voice=True),
            DocumentAttributeFilename(file_name=name),
        ],
    )
    return _FakeMessage(MessageMediaDocument(document=doc))


def _audio_message(payload: bytes, *, mime: str, name: str) -> _FakeMessage:
    """A non-voice audio document (Telegram Voice is OGG/Opus only)."""
    doc = Document(
        id=4, access_hash=4, file_reference=b"", date=None, mime_type=mime,
        size=len(payload), dc_id=1,
        attributes=[
            DocumentAttributeAudio(duration=2, voice=False),
            DocumentAttributeFilename(file_name=name),
        ],
    )
    return _FakeMessage(MessageMediaDocument(document=doc))


def _image_message(payload: bytes) -> _FakeMessage:
    doc = Document(
        id=2, access_hash=2, file_reference=b"", date=None, mime_type="image/png",
        size=len(payload), dc_id=1,
        attributes=[DocumentAttributeFilename(file_name=FILE_NAME)],
    )
    return _FakeMessage(MessageMediaDocument(document=doc))


def _media_temp_dirs() -> set[str]:
    try:
        entries = os.listdir(tempfile.gettempdir())
    except OSError:  # pragma: no cover - defensive
        return set()
    return {name for name in entries if name.startswith("lifeos_media_")}


# ── Global state hygiene ──


@pytest.fixture(autouse=True)
def _reset_engines_and_env(monkeypatch):
    ocr, stt = media_service.get_ocr_engine(), media_service.get_stt_engine()
    media_service.set_ocr_engine(None)
    media_service.set_stt_engine(None)
    for name in (
        "AI_GEMINI_API_KEY", "GEMINI_API_KEY", "AI_GEMINI_MEDIA_MODEL", "AI_GEMINI_MODEL",
        STT_MODEL_ENV_VAR, STT_LANGUAGE_ENV_VAR,
    ):
        monkeypatch.delenv(name, raising=False)
    yield
    media_service.set_ocr_engine(ocr)
    media_service.set_stt_engine(stt)


# ── 1. Model selection: explicit, deterministic, OCR untouched ──


def test_the_dedicated_transcription_model_is_the_documented_one():
    assert DEDICATED_TRANSCRIPTION_MODEL == "gemini-3.5-transcribe"
    assert INTERACTIONS_ENDPOINT == f"{GEMINI_API_BASE}/interactions"


def test_no_dedicated_model_is_selected_by_default():
    # Unset ENV ⇒ nothing is selected, and nothing is silently substituted.
    assert resolve_stt_model() == ("", "")
    assert resolve_stt_language() == ("", "")
    assert _engine().stt_transport == "generate_content"


def test_stt_model_selection_requires_the_explicit_override(monkeypatch):
    monkeypatch.setenv("AI_GEMINI_API_KEY", API_KEY)

    engine, model, _reason = build_gemini_media_engine()

    # Unset ⇒ the existing general media route, unchanged.
    assert resolve_stt_model() == ("", "")
    assert engine.stt_model == "" and engine.stt_language == ""
    assert engine.stt_transport == "generate_content"

    monkeypatch.setenv(STT_MODEL_ENV_VAR, DEDICATED_TRANSCRIPTION_MODEL)
    engine, model, _reason = build_gemini_media_engine()

    assert resolve_stt_model() == (DEDICATED_TRANSCRIPTION_MODEL, STT_MODEL_ENV_VAR)
    assert engine.stt_model == DEDICATED_TRANSCRIPTION_MODEL
    assert engine.stt_transport == "interactions"
    # The general media model is NOT silently replaced.
    assert engine.model == DEFAULT_MEDIA_MODEL == model


def test_a_deprecated_stt_model_is_resolved_before_use(monkeypatch):
    monkeypatch.setenv(STT_MODEL_ENV_VAR, "gemini-2.0-flash")

    # The project's existing deprecation map is reused — no second one.
    assert resolve_stt_model() == ("gemini-2.5-flash", STT_MODEL_ENV_VAR)


def test_language_is_automatic_unless_explicitly_pinned(monkeypatch):
    assert resolve_stt_language() == ("", "")
    monkeypatch.setenv(STT_LANGUAGE_ENV_VAR, "fa-IR")
    assert resolve_stt_language() == ("fa-IR", STT_LANGUAGE_ENV_VAR)
    # A blank value is not a language.
    monkeypatch.setenv(STT_LANGUAGE_ENV_VAR, "   ")
    assert resolve_stt_language() == ("", "")


def test_provisioning_keeps_the_media_model_and_adds_the_stt_selection(monkeypatch):
    monkeypatch.setenv("AI_GEMINI_API_KEY", API_KEY)
    monkeypatch.setenv(STT_MODEL_ENV_VAR, DEDICATED_TRANSCRIPTION_MODEL)
    monkeypatch.setenv(STT_LANGUAGE_ENV_VAR, "fa-IR")

    status = provision_gemini_media_engines()

    assert status == {
        "configured": True,
        "model": DEFAULT_MEDIA_MODEL,
        "key_env_var": "AI_GEMINI_API_KEY",
        "reason": "",
    }
    assert media_service.ocr_available() is True
    assert media_service.stt_available() is True
    # ONE engine still satisfies both seams.
    assert media_service.get_ocr_engine() is media_service.get_stt_engine()
    engine = media_service.get_stt_engine()
    assert engine.stt_model == DEDICATED_TRANSCRIPTION_MODEL
    assert engine.stt_language == "fa-IR"


def test_the_media_model_override_still_governs_ocr(monkeypatch):
    monkeypatch.setenv("AI_GEMINI_API_KEY", API_KEY)
    monkeypatch.setenv("AI_GEMINI_MEDIA_MODEL", "gemini-2.5-flash-lite")
    monkeypatch.setenv(STT_MODEL_ENV_VAR, DEDICATED_TRANSCRIPTION_MODEL)

    engine, model, _reason = build_gemini_media_engine()

    assert model == "gemini-2.5-flash-lite"
    assert engine.model == "gemini-2.5-flash-lite"
    assert engine.stt_model == DEDICATED_TRANSCRIPTION_MODEL


@pytest.mark.asyncio
async def test_ocr_still_uses_the_general_model_instruction_and_image_type(stub):
    transport = stub(generate_text="Invoice 42")
    media_service.set_ocr_engine(_dedicated("fa-IR"))  # dedicated STT configured
    payload = _png(4, 4)

    analysis = await media_service.analyze_media(
        _BoundaryClient(payload), OWNER, _image_message(payload),
    )

    assert analysis.status == MediaStatus.EXTRACTED
    assert analysis.content == "Invoice 42"
    request = transport.generate_requests[0]
    assert str(request.url) == f"{GEMINI_API_BASE}/models/{DEFAULT_MEDIA_MODEL}:generateContent"
    assert _body(request)["contents"][0]["parts"][0]["text"] == OCR_INSTRUCTION
    assert _body(request)["contents"][0]["parts"][-1]["inlineData"]["mimeType"] == "image/png"
    # The speech model's endpoint is never touched by OCR.
    assert transport.interaction_requests == []
    assert _media_temp_dirs() == set()


@pytest.mark.asyncio
async def test_an_unconfigured_dedicated_model_leaves_stt_on_the_general_route(stub):
    transport = stub(transcript="ignored", generate_text="general route text")
    media_service.set_stt_engine(_engine())  # no stt_model ⇒ unchanged behaviour
    payload = _wav()

    analysis = await media_service.analyze_media(
        _BoundaryClient(payload), OWNER, _voice_message(payload),
    )

    assert analysis.content == "general route text"
    request = transport.generate_requests[0]
    assert str(request.url) == f"{GEMINI_API_BASE}/models/{DEFAULT_MEDIA_MODEL}:generateContent"
    assert _body(request)["contents"][0]["parts"][0]["text"] == STT_INSTRUCTION
    assert transport.interaction_requests == []
    # The legacy generation controls belong to that route only.
    assert _body(request)["generationConfig"] == {
        "temperature": engine_module.SAMPLING_TEMPERATURE,
        "maxOutputTokens": engine_module.MAX_OUTPUT_TOKENS,
    }


# ── 2. The dedicated request: its own documented contract ──


@pytest.mark.asyncio
async def test_the_dedicated_request_is_the_documented_transcription_request(stub):
    transport = stub(transcript="سلام دنیا")
    payload = _ogg_opus()

    text = _dedicated().transcribe(payload)

    assert text == "سلام دنیا"
    assert len(transport.interaction_requests) == 1, "one request per operation"
    assert transport.generate_requests == [], "never the Generate Content shape"
    request = transport.interaction_requests[0]
    assert str(request.url) == INTERACTIONS_ENDPOINT
    assert request.headers["x-goog-api-key"] == API_KEY
    assert API_KEY not in str(request.url)
    body = _body(request)
    assert body["model"] == DEDICATED_TRANSCRIPTION_MODEL
    item = _audio_item(request)
    assert item["type"] == "audio"
    assert item["mime_type"] == "audio/ogg"
    # The representation the documentation shows for THIS model: the Files API
    # URI form, never an inline payload the model's own guide does not demonstrate.
    assert item["uri"] == _FILE_URI
    assert "data" not in item
    # The bytes that travelled are the validated bytes, byte-identical.
    assert transport.requests[1].content == payload
    # Verbatim, explicitly — never Smart transcription.
    assert _config(request)["mode"] == {"type": "verbatim"}


@pytest.mark.asyncio
async def test_the_dedicated_request_carries_no_unverified_controls(stub):
    transport = stub()
    _dedicated().transcribe(_wav())

    body = _body(transport.interaction_requests[0])

    assert set(body) == {"model", "input", "generation_config", "store"}
    assert set(body["generation_config"]) == {"transcription_config"}
    assert set(body["generation_config"]["transcription_config"]) == {"mode"}
    serialized = json.dumps(body)
    for banned in (
        "temperature", "topK", "topP", "candidateCount", "maxOutputTokens",
        "system_instruction", "response_format", "timestamp_granularities", "word",
        "diarization", "custom_vocabulary", "smart", "thinking",
    ):
        assert banned not in serialized, banned


@pytest.mark.asyncio
async def test_store_is_disabled_so_the_voice_note_is_not_retained_remotely(stub):
    transport = stub()
    _dedicated().transcribe(_wav())

    # The documented opt-out from the API's default server-side retention.
    assert _body(transport.interaction_requests[0])["store"] is False


@pytest.mark.asyncio
async def test_no_text_instruction_is_sent_to_the_dedicated_model(stub):
    transport = stub()
    _dedicated().transcribe(_wav())

    request = transport.interaction_requests[0]
    assert len(_body(request)["input"]) == 1
    assert STT_INSTRUCTION not in json.dumps(_body(request))


@pytest.mark.asyncio
async def test_explicit_language_pins_language_codes(stub):
    transport = stub()
    _dedicated("fa-IR").transcribe(_wav())

    assert _config(transport.interaction_requests[0])["language_codes"] == ["fa-IR"]


@pytest.mark.asyncio
async def test_automatic_language_sends_no_language_field(stub):
    transport = stub()
    _dedicated().transcribe(_wav())

    config = _config(transport.interaction_requests[0])
    assert "language_codes" not in config


def test_the_general_route_instruction_is_byte_identical_by_default():
    assert stt_instruction() == STT_INSTRUCTION
    assert stt_instruction("") == STT_INSTRUCTION


def test_the_general_route_can_append_one_language_line_for_comparison_d():
    instruction = stt_instruction("fa-IR")

    # Comparison D (general model + explicit language hint) is an APPENDED line
    # over the untouched M1.5c instruction — never a replacement.
    assert instruction.startswith(STT_INSTRUCTION)
    assert instruction.count("fa-IR") == 1
    assert instruction != STT_INSTRUCTION
    assert stt_instruction("fa-IR") == instruction, "deterministic, not per-request"


@pytest.mark.asyncio
async def test_the_general_route_with_a_language_hint_stays_on_generate_content(stub):
    transport = stub(generate_text="hint route text")
    payload = _wav()

    text = _engine(stt_language="fa-IR").transcribe(payload)

    assert text == "hint route text"
    assert transport.interaction_requests == []
    instruction = _body(transport.generate_requests[0])["contents"][0]["parts"][0]["text"]
    assert instruction == stt_instruction("fa-IR")
    assert "fa-IR" in instruction


# ── 3. Audio formats and the inline/Files transport boundary ──


@pytest.mark.asyncio
@pytest.mark.parametrize("mime,payload,expected", [
    ("audio/ogg", _ogg_opus(), "audio/ogg"),
    ("audio/opus", _ogg_opus(), "audio/ogg"),
    ("audio/wav", _wav(), "audio/wav"),
    ("audio/x-wav", _wav(), "audio/wav"),
    ("audio/flac", _flac(), "audio/flac"),
    ("audio/x-flac", _flac(), "audio/flac"),
])
async def test_every_validated_audio_container_maps_to_a_documented_mime(
    stub, mime, payload, expected,
):
    transport = stub()
    media_service.set_stt_engine(_dedicated())
    message = (
        _voice_message(payload, mime=mime) if mime == "audio/ogg"
        else _audio_message(payload, mime=mime, name="note.bin")
    )

    analysis = await media_service.analyze_media(
        _BoundaryClient(payload), OWNER, message,
    )

    assert analysis.content == "stub transcript"
    item = _audio_item(transport.interaction_requests[0])
    assert item["mime_type"] == expected
    assert item["uri"] == _FILE_URI
    # The validated container is what was uploaded, unmodified and untranscoded.
    assert transport.requests[1].content == payload


@pytest.mark.asyncio
async def test_a_small_voice_note_uses_the_documented_uri_representation(stub):
    """Primary transport = the Files API URI form, for small notes too.

    The dedicated model's own documentation shows only ``uri``; the inline
    ``data`` form is the bounded FALLBACK, not the primary path — so even a tiny
    voice note is uploaded, referenced by URI, and the remote file is removed.
    """
    transport = stub()
    payload = _ogg_opus()

    _dedicated().transcribe(payload)

    item = _audio_item(transport.interaction_requests[0])
    assert "uri" in item and "data" not in item
    assert item["uri"] == _FILE_URI
    assert len(transport.upload_requests) == 1, "one bounded upload sequence"
    assert transport.requests[1].content == payload
    assert transport.deleted == [f"{GEMINI_API_BASE}/{_FILE_NAME}"]
    assert _media_temp_dirs() == set()


@pytest.mark.asyncio
async def test_a_transient_upload_failure_falls_back_to_the_inline_form_once(stub):
    """A transient first-attempt failure uses ONE bounded fallback, never a loop.

    The fallback is the documented inline audio representation (it needs no
    upload at all), the first attempt's remote file is still deleted, and a
    successful fallback delivers its transcript.
    """
    transport = stub(transcript="fallback transcript", upload_status=503)
    payload = _ogg_opus()   # small enough for the inline fallback

    text = _dedicated().transcribe(payload)

    assert text == "fallback transcript"
    assert len(transport.upload_requests) == 1, "the retry never re-uploads"
    assert len(transport.interaction_requests) == 1
    item = _audio_item(transport.interaction_requests[0])
    assert "data" in item and "uri" not in item
    assert base64.b64decode(item["data"]) == payload


@pytest.mark.asyncio
async def test_the_first_attempts_remote_file_is_deleted_before_the_fallback(stub):
    """A failed attempt's uploaded file never outlives that attempt.

    The first (URI) attempt's interaction is rejected with a TRANSIENT status, so
    the bounded second attempt runs — and by then the first attempt's remote file
    has already been deleted, i.e. cleanup is per attempt, not per operation.
    """
    transport = stub(transcript="fallback transcript", interaction_statuses=[500, 200])

    assert _dedicated().transcribe(_ogg_opus()) == "fallback transcript"

    assert len(transport.interaction_requests) == 2
    assert transport.deleted == [f"{GEMINI_API_BASE}/{_FILE_NAME}"]
    assert "uri" in _audio_item(transport.interaction_requests[0])
    assert "data" in _audio_item(transport.interaction_requests[1])


@pytest.mark.asyncio
async def test_large_audio_uses_the_same_files_api_flow_and_is_deleted(stub):
    transport = stub(transcript="transcribed")
    payload = _large_wav()

    text = _dedicated().transcribe(payload)

    assert text == "transcribed"
    start = transport.upload_requests[0]
    assert str(start.url).startswith(GEMINI_UPLOAD_BASE)
    assert start.headers["X-Goog-Upload-Command"] == "start"
    assert json.loads(start.content.decode()) == {"file": {"display_name": "lifeos-media"}}
    finalize = transport.requests[1]
    assert str(finalize.url) == _UPLOAD_URL
    assert finalize.headers["X-Goog-Upload-Command"] == "upload, finalize"
    assert finalize.content == payload
    item = _audio_item(transport.interaction_requests[0])
    assert item == {"type": "audio", "uri": _FILE_URI, "mime_type": "audio/wav"}
    assert transport.deleted == [f"{GEMINI_API_BASE}/{_FILE_NAME}"]
    assert len(transport.interaction_requests) == 1


@pytest.mark.asyncio
async def test_the_uploaded_file_is_deleted_when_the_interaction_fails(stub):
    # A DETERMINISTIC failure (400): no bounded retry, so exactly one attempt and
    # exactly one cleanup — the property this test pins.
    transport = stub(http_status=400)
    payload = _large_wav()

    with pytest.raises(MediaError):
        _dedicated().transcribe(payload)

    assert len(transport.interaction_requests) == 1
    assert transport.deleted == [f"{GEMINI_API_BASE}/{_FILE_NAME}"]


@pytest.mark.asyncio
async def test_a_failed_cleanup_never_masks_the_transcript(stub):
    transport = stub(transcript="still fine", delete_status=500)

    assert _dedicated().transcribe(_large_wav()) == "still fine"


@pytest.mark.asyncio
async def test_a_non_audio_payload_is_refused_before_any_request(stub):
    transport = stub()

    with pytest.raises(MediaError) as exc:
        _dedicated().transcribe(_png(4, 4))

    assert "audio input only" in str(exc.value)
    assert transport.requests == []


@pytest.mark.asyncio
async def test_an_empty_payload_is_never_sent(stub):
    transport = stub()

    assert _dedicated().transcribe(b"") == ""
    assert transport.requests == []


# ── 4. Response parsing ──


@pytest.mark.asyncio
async def test_the_transcript_is_read_from_the_documented_steps_shape(stub):
    stub(interaction_payload={
        "id": "interactions/abc",
        "status": "completed",
        "steps": [
            {"id": "s1", "type": "model_output", "content": [
                {"type": "text", "text": "first part"},
            ]},
            {"id": "s2", "type": "model_output", "content": [
                {"type": "text", "text": "second part"},
                {"type": "word_info", "text": "ignored", "speaker": "spk_1"},
            ]},
        ],
    })

    assert _dedicated().transcribe(_wav()) == "first part\nsecond part"


@pytest.mark.asyncio
async def test_output_text_is_accepted_only_as_a_fallback(stub):
    stub(interaction_payload={"id": "interactions/x", "status": "completed", "output_text": "plain"})

    assert _dedicated().transcribe(_wav()) == "plain"


@pytest.mark.asyncio
async def test_a_persian_transcript_survives_the_engine_unchanged(stub):
    stub(transcript=f"{_PERSIAN_ZWNJ} {_PERSIAN}")

    text = _dedicated().transcribe(_wav())

    assert text == f"{_PERSIAN_ZWNJ} {_PERSIAN}"
    assert text.count(_ZWNJ) == 1


@pytest.mark.asyncio
async def test_no_speech_is_reported_honestly_without_content(stub):
    stub(interaction_payload=_interaction(""))
    media_service.set_stt_engine(_dedicated())
    payload = _wav()

    analysis = await media_service.analyze_media(
        _BoundaryClient(payload), OWNER, _voice_message(payload),
    )

    assert analysis.status == MediaStatus.EXTRACTED
    assert analysis.content == ""
    assert analysis.has_content is False
    assert analysis.reason == "No speech was detected in the audio."


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["failed", "in_progress", "cancelled"])
async def test_a_non_completed_interaction_fails_controlled(stub, status):
    transport = stub(interaction_status=status)

    with pytest.raises(MediaError) as exc:
        _dedicated().transcribe(_wav())

    assert status in str(exc.value)
    assert len(transport.interaction_requests) == 1


@pytest.mark.asyncio
async def test_an_unreadable_response_fails_controlled(stub):
    stub(interaction_raw=b"<html>not json</html>")

    with pytest.raises(MediaError) as exc:
        _dedicated().transcribe(_wav())

    assert "unreadable" in str(exc.value)


@pytest.mark.asyncio
async def test_a_malformed_response_shape_fails_controlled(stub):
    stub(interaction_payload=["not", "a", "dict"])

    with pytest.raises(MediaError) as exc:
        _dedicated().transcribe(_wav())

    assert "unreadable" in str(exc.value)


# ── 5. Failures are bounded, honest and never fall back ──


@pytest.mark.asyncio
@pytest.mark.parametrize("status,expected,attempts", [
    (401, "rejected the configured API key", 1),
    (403, "rejected the configured API key", 1),
    (429, "rate limited", 2),
    (404, "could not find the configured model", 1),
    (500, "unavailable", 2),
    (400, "refused the speech-to-text request", 1),
])
async def test_api_failures_are_normalized_and_never_fall_back(stub, status, expected, attempts):
    transport = stub(http_status=status)

    with pytest.raises(MediaError) as exc:
        _dedicated().transcribe(_wav())

    assert expected in str(exc.value)
    # DETERMINISTIC statuses (4xx other than 429) are never re-sent; the two
    # transient ones (429, >= 500) get exactly ONE bounded second attempt.
    assert len(transport.interaction_requests) == attempts, "bounded attempts, never a loop"
    # The critical property for the experiment: a failing dedicated model is NOT
    # silently re-asked on the general model.
    assert transport.generate_requests == []


@pytest.mark.asyncio
async def test_a_failure_never_echoes_the_credential(stub):
    stub(http_status=401, detail=f"API key not valid: {API_KEY}")

    with pytest.raises(MediaError) as exc:
        _dedicated().transcribe(_wav())

    message = str(exc.value)
    assert API_KEY not in message
    assert "***" in message


@pytest.mark.asyncio
async def test_a_timeout_is_controlled(stub):
    stub(raise_error=httpx.ReadTimeout("scripted timeout"))

    with pytest.raises(MediaError) as exc:
        _dedicated().transcribe(_wav())

    assert "timed out" in str(exc.value)


@pytest.mark.asyncio
async def test_a_transport_failure_is_controlled(stub):
    stub(raise_error=httpx.ConnectError("scripted transport failure"))

    with pytest.raises(MediaError) as exc:
        _dedicated().transcribe(_wav())

    assert "ConnectError" in str(exc.value)


@pytest.mark.asyncio
async def test_an_unavailable_model_keeps_the_media_failure_contract(stub):
    transport = stub(http_status=404, detail="models/gemini-3.5-transcribe is not found")
    media_service.set_stt_engine(_dedicated())
    payload = _wav()

    with pytest.raises(MediaError) as exc:
        await media_service.analyze_media(
            _BoundaryClient(payload), OWNER, _voice_message(payload),
        )

    assert "could not find the configured model" in str(exc.value)
    assert len(transport.interaction_requests) == 1
    assert _media_temp_dirs() == set()


def test_the_engine_bounds_are_finite_and_inside_the_boundary_bounds():
    assert 0 < engine_module.OCR_TIMEOUT_S < media_service.OCR_TIMEOUT_S
    assert 0 < engine_module.STT_OPERATION_DEADLINE_S < media_service.STT_TIMEOUT_S
    # The whole worst case — ONE operation deadline plus ONE bounded cleanup —
    # still fits inside the boundary's own bound, so the engine always fails (or
    # succeeds) with its own precise outcome before the boundary's generic one.
    assert (
        engine_module.STT_OPERATION_DEADLINE_S + engine_module.STT_CLEANUP_TIMEOUT_S
        < media_service.STT_TIMEOUT_S
    )
    assert engine_module.STT_CONNECT_TIMEOUT_S < engine_module.STT_OPERATION_DEADLINE_S
    assert engine_module.STT_WRITE_TIMEOUT_S < engine_module.STT_OPERATION_DEADLINE_S
    assert engine_module.STT_MIN_ATTEMPT_S < engine_module.STT_OPERATION_DEADLINE_S
    assert engine_module.STT_MAX_ATTEMPTS == 2, "one attempt plus one bounded retry"
    assert engine_module.MAX_OUTPUT_TOKENS == 8192


# ── 6. Observability: bounded, non-sensitive ──


@pytest.mark.asyncio
async def test_the_dedicated_run_is_traced_without_the_transcript(stub, caplog):
    transport = stub(transcript=TRANSCRIPT)

    with caplog.at_level("INFO", logger="backend.services.gemini_media_engine"):
        _dedicated("fa-IR").transcribe(_ogg_opus())

    assert len(transport.interaction_requests) == 1
    records = [r.getMessage() for r in caplog.records]
    line = next(r for r in records if r.startswith("GEMINI_MEDIA_ENGINE kind="))
    for field in ("model=" + DEDICATED_TRANSCRIPTION_MODEL, "transport=interactions",
                  "mime=audio/ogg", "bytes=", "language=fa-IR", "mode=verbatim",
                  "chars=", "elapsed_ms=", "status=ok"):
        assert field in line, field
    # No transcript, no audio, no credential, no Telegram metadata.
    for record in records:
        assert TRANSCRIPT not in record
        assert API_KEY not in record
        assert CAPTION not in record
        assert FILE_NAME not in record


@pytest.mark.asyncio
async def test_a_failed_dedicated_run_is_traced_as_failed(stub, caplog):
    stub(http_status=429)

    with caplog.at_level("INFO", logger="backend.services.gemini_media_engine"):
        with pytest.raises(MediaError):
            _dedicated().transcribe(_wav())

    assert any(
        r.getMessage().startswith("GEMINI_MEDIA_ENGINE kind=") and "status=failed" in r.getMessage()
        for r in caplog.records
    )


@pytest.mark.asyncio
async def test_the_automatic_language_mode_is_traced_as_auto(stub, caplog):
    stub()

    with caplog.at_level("INFO", logger="backend.services.gemini_media_engine"):
        _dedicated().transcribe(_wav())

    assert any("language=auto" in r.getMessage() for r in caplog.records)


# ── 7. Telegram metadata and module hygiene ──


@pytest.mark.asyncio
async def test_no_telegram_metadata_reaches_the_transcription_model(stub):
    transport = stub(transcript="ok")
    media_service.set_stt_engine(_dedicated("fa-IR"))
    payload = _ogg_opus()

    await media_service.analyze_media(
        _BoundaryClient(payload), OWNER, _voice_message(payload, mime="audio/ogg"),
    )

    sent = transport.sent_bytes()
    for forbidden in (CAPTION, FILE_NAME, str(CHAT), str(REPLY_ID)):
        assert forbidden.encode() not in sent, forbidden
    texts = json.dumps(_body(transport.interaction_requests[0]))
    for word in ("caption", "filename", "sender", "chat_id", "message_id"):
        assert word not in texts.lower(), word


def test_the_engine_module_gains_no_new_import():
    names = {name for name, _ in inspect.getmembers(engine_module, inspect.ismodule)}
    assert names == {"base64", "logging", "os", "time", "httpx", "media_service"}
    source = inspect.getsource(engine_module)
    assert "\nimport re" not in source and "\nfrom re" not in source


def test_the_engine_still_exposes_the_two_existing_seams():
    engine = _dedicated()
    assert not inspect.iscoroutinefunction(engine.transcribe)
    assert not inspect.iscoroutinefunction(engine.recognize)
    assert set(engine.__slots__) == {
        "_api_key", "_model", "_key_env_var", "_stt_model", "_stt_language",
    }


# ── 8. The unchanged direct-STT path, now over the dedicated engine ──


class _ScriptedProvider(BaseProvider):
    """Records every prompt it receives; its text must NEVER become a direct answer."""

    def __init__(self, text: str = "SECOND-MODEL-ANSWER") -> None:
        super().__init__(ProviderConfig(provider_name="scripted", enabled=True, default_model="m1"))
        self._text = text
        self.prompts: list[list[dict[str, Any]]] = []

    @property
    def name(self) -> str:
        return "scripted"

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(supports_tools=True)

    async def chat(self, messages: list[dict[str, Any]], **kwargs: Any) -> ProviderResponse:
        self.prompts.append([dict(m) for m in messages])
        return ProviderResponse(
            text=self._text, provider_name="scripted", success=True, metadata={"model": "m1"},
        )

    async def vision(self, *args: Any, **kwargs: Any) -> ProviderResponse:  # pragma: no cover
        raise AssertionError("the dead vision() path must never be used")

    def initialize(self) -> None:
        return None

    def shutdown(self) -> None:
        return None

    def count_tokens(self, text: str) -> int:
        return max(1, len(text) // 4)

    def health(self) -> dict[str, Any]:
        return {"healthy": True, "ready": True}


def _manager(provider: _ScriptedProvider) -> ProviderManager:
    registry = ProviderRegistry()
    registry.register(provider)
    manager = ProviderManager(registry)
    manager.switch_provider(provider.name)
    return manager


def _dispatcher(manager: ProviderManager, client: Any) -> Dispatcher:
    ctx = ToolContext(
        telegram=TelegramAPI(client), owner_id=OWNER, tz_str="UTC", client=client,
        extra={"chat_id": CHAT, "request_id": "dedicated-stt"},
    )
    executor = ToolExecutor(create_default_registry(ctx), ctx)

    conversation = MagicMock()
    session = MagicMock()
    session.session_id = "s"
    session.owner_id = OWNER
    session.active_provider = manager.get_active_name()
    conversation.get_session.return_value = session
    conversation.restore_history = AsyncMock()
    conversation.get_history.return_value = []

    prompt_builder = MagicMock()
    package = MagicMock()
    package.system_prompt = "sys"
    package.runtime_context = ""
    package.conversation_context = ""
    package.tool_context = ""
    package.user_input = "hi"
    package.metadata = {}
    package.estimated_tokens.estimated_input_tokens = 1
    package.estimated_tokens.prompt_size_chars = 1
    prompt_builder.build.return_value = package

    return Dispatcher(
        conversation, prompt_builder, manager, NOOP_HOOKS, EngineMetrics(), tool_executor=executor,
    )


def _reply_context(**overrides: Any) -> ReplyContext:
    values: dict[str, Any] = {
        "exists": True,
        "message_id": REPLY_ID,
        "sender_id": 4242,
        "sender_name": "sender-name-LEAK",
        "chat_id": CHAT,
        "chat_title": "chat-title-LEAK",
        "media_type": "Voice",
        "text_preview": "reply-text-preview-LEAK",
        "timestamp": "2026-09-16T10:00:00+00:00",
    }
    values.update(overrides)
    return ReplyContext(**values)


def _request(**overrides: Any) -> AIRequest:
    values: dict[str, Any] = {
        "session_id": "s",
        "user_message": "این رو stt کن",
        "owner_id": OWNER,
        "chat_id": CHAT,
        "message_id": REQUEST_ID,
        "request_id": "dedicated-stt",
        "timeout_s": 240.0,
    }
    values.update(overrides)
    return AIRequest(**values)


@pytest.mark.asyncio
async def test_direct_stt_over_the_dedicated_engine_bypasses_the_provider(stub):
    transport = stub(transcript=TRANSCRIPT)
    media_service.set_stt_engine(_dedicated("fa-IR"))
    provider = _ScriptedProvider()
    payload = _wav(2.0)
    client = _TelegramClient(_voice_message(payload), payload)
    dispatcher = _dispatcher(_manager(provider), client)

    result = await dispatcher.dispatch(_request(reply_context=_reply_context()))

    # The transcript itself is the answer, produced by the dedicated model.
    assert result.success is True
    assert result.response == TRANSCRIPT
    assert provider.prompts == []
    assert client.ops == ["get_messages", "download_media"]
    assert len(transport.interaction_requests) == 1
    assert _body(transport.interaction_requests[0])["model"] == DEDICATED_TRANSCRIPTION_MODEL
    assert _config(transport.interaction_requests[0])["language_codes"] == ["fa-IR"]


@pytest.mark.asyncio
async def test_the_sst_alias_takes_the_same_dedicated_direct_path(stub):
    transport = stub(transcript=TRANSCRIPT)
    media_service.set_stt_engine(_dedicated())
    provider = _ScriptedProvider()
    payload = _wav(2.0)
    client = _TelegramClient(_voice_message(payload), payload)
    dispatcher = _dispatcher(_manager(provider), client)

    result = await dispatcher.dispatch(_request(
        user_message="این رو SST کن", reply_context=_reply_context(),
    ))

    assert result.response == TRANSCRIPT
    assert provider.prompts == []
    assert len(transport.interaction_requests) == 1


@pytest.mark.asyncio
async def test_analytical_media_still_uses_the_provider_manager(stub):
    transport = stub(transcript="analytical transcript")
    media_service.set_stt_engine(_dedicated("fa-IR"))
    provider = _ScriptedProvider()
    payload = _wav(2.0)
    client = _TelegramClient(_voice_message(payload), payload)
    dispatcher = _dispatcher(_manager(provider), client)

    result = await dispatcher.dispatch(_request(
        user_message="این ویس درباره چیه؟", reply_context=_reply_context(),
    ))

    assert result.provider == "scripted"
    assert len(provider.prompts) == 1
    assert provider.prompts[0][1]["content"].endswith("analytical transcript")
    # The analysis still came from the dedicated engine; only the ANSWER differs.
    assert len(transport.interaction_requests) == 1


def test_the_direct_stt_classifier_is_unchanged_and_regex_free():
    assert media_ai_service.is_direct_stt_request("این رو stt کن") is True
    assert media_ai_service.is_direct_stt_request("این رو SST کن") is True
    assert media_ai_service.is_direct_stt_request("transcribe this") is True
    assert media_ai_service.is_direct_stt_request("این ویس درباره چیه؟") is False
    assert "re" not in {
        name for name, _ in inspect.getmembers(media_ai_service, inspect.ismodule)
    }
