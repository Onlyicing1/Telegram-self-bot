"""
Media Processing M1.5 — the Gemini-backed OCR / STT engines behind the EXISTING
media seams.

The M1.3/M1.4 boundary (``backend/services/media_service.py``) is unchanged in
shape; this file pins the provisioning of its two seams:

  1. ``media_service.set_ocr_engine`` / ``set_stt_engine`` are now satisfied by ONE
     Gemini engine (``backend/services/gemini_media_engine.py``) built from the
     SAME ENV configuration the provider layer already uses. No parallel
     configuration system, no new secret, no second provider-selection system and
     no ``ProviderManager.vision()`` resurrection.
  2. Every existing bound stays authoritative and is enforced by the boundary
     BEFORE Gemini is contacted: input size, container/MIME corroboration, image
     pixel bound, audio duration/channel/rate bounds, the OCR/STT timeout and the
     shared character ceiling.
  3. The engine is handed ONLY the validated payload bytes and a fixed
     instruction. No caption, sender, chat id, message id, filename or
     conversation history is ever sent, and nothing about Telegram is reachable
     from the engine.
  4. Failures are controlled and honest: authentication, malformed requests,
     timeouts, malformed responses, blocked responses, rate limits and server
     errors each become a ``MediaError`` with a sanitized reason — never a
     fabricated transcript or caption, and never an echoed API key.
  5. A missing credential leaves both seams unprovisioned, so the boundary keeps
     failing closed exactly as M1.3/M1.4 record, and startup is unaffected.

The HTTP boundary is scripted: a recording ``httpx`` transport is installed under
the engine, so no test needs a real ``GEMINI_API_KEY`` and no byte leaves the
process. The Telegram boundary is a scripted fake shaped like the Telethon client
surface the facade consumes, and the media fixtures are real container bytes built
in-process.
"""
from __future__ import annotations

import base64
import inspect
import io
import json
import os
import re
import struct
import tempfile
import threading
import time
import tokenize
import wave
import zlib
from pathlib import Path
from typing import Any

import httpx
import pytest
from telethon.tl.types import (
    Document,
    DocumentAttributeAudio,
    DocumentAttributeFilename,
    MessageMediaDocument,
    MessageMediaPhoto,
    Photo,
    PhotoSize,
)

from backend.services import gemini_media_engine as engine_module
from backend.services import media_service
from backend.services.gemini_media_engine import (
    DEFAULT_MEDIA_MODEL,
    GEMINI_API_BASE,
    GEMINI_UPLOAD_BASE,
    MAX_OUTPUT_TOKENS,
    OCR_INSTRUCTION,
    STT_INSTRUCTION,
    GeminiMediaEngine,
    build_gemini_media_engine,
    gemini_mime_type,
    provision_gemini_media_engines,
    resolve_api_key,
    resolve_media_model,
)
from backend.services.media_service import (
    MAX_OCR_CHARS,
    MAX_OCR_INPUT_BYTES,
    MAX_STT_CHARS,
    MediaError,
    MediaStatus,
)

OWNER = 7770001
CHAT = -1007778889999
MESSAGE_ID = 574942
#: Distinctive tokens: if any of them appears in a recorded request body, the
#: privacy contract is broken and the assertion says exactly which one.
CAPTION = "caption-must-never-reach-gemini-5f3a"
FILE_NAME = "secret-filename-must-never-reach-gemini.png"
API_KEY = "test-api-key-not-a-real-credential"
_UPLOAD_URL = "https://upload.example.invalid/files?upload_id=xyz"
_FILE_URI = "https://generativelanguage.googleapis.com/v1beta/files/abc123"
_FILE_NAME = "files/abc123"

_PERSIAN = "سلام، این یک آزمایش است"
_ZWNJ = "\u200c"
_PERSIAN_ZWNJ = "می" + _ZWNJ + "کند"


# ── Scripted Gemini transport (the HTTP boundary, fully controlled) ──


class _GeminiStub(httpx.BaseTransport):
    """A recording Gemini transport: scripted responses, zero network I/O.

    Records every request (method, URL, headers, body), the thread it ran on and
    how many times each endpoint was hit, so the tests can prove the request
    shape, the absence of Telegram metadata and the absence of any retry loop.
    """

    def __init__(
        self,
        *,
        ocr_text: str = "stub ocr text",
        stt_text: str = "stub transcript",
        generate_status: int = 200,
        generate_payload: Any = None,
        generate_raw: bytes | None = None,
        upload_status: int = 200,
        upload_payload: Any = None,
        delete_status: int = 200,
        file_state: str = "ACTIVE",
        detail: str = "",
        raise_error: Exception | None = None,
        delay: float = 0.0,
    ) -> None:
        self.ocr_text = ocr_text
        self.stt_text = stt_text
        self.generate_status = generate_status
        self.generate_payload = generate_payload
        self.generate_raw = generate_raw
        self.upload_status = upload_status
        self.upload_payload = upload_payload
        self.delete_status = delete_status
        self.file_state = file_state
        self.detail = detail
        self.raise_error = raise_error
        self.delay = delay
        self.requests: list[httpx.Request] = []
        self.threads: list[str] = []
        self.deleted: list[str] = []
        self.create_calls = 0

    # ── transport protocol ──

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        self.threads.append(threading.current_thread().name)
        if self.delay:
            time.sleep(self.delay)
        if self.raise_error is not None:
            raise self.raise_error
        url = str(request.url)
        if request.method == "POST" and url.endswith("/upload/v1beta/files"):
            if self.upload_status >= 400:
                return self._error(request, self.upload_status)
            return httpx.Response(
                self.upload_status,
                headers={"x-goog-upload-url": _UPLOAD_URL},
                request=request,
            )
        if request.method == "POST" and url == _UPLOAD_URL:
            if self.upload_status >= 400:
                return self._error(request, self.upload_status)
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
        if url.endswith(":generateContent"):
            self.create_calls += 1
            if self.generate_status >= 400:
                return self._error(request, self.generate_status)
            if self.generate_raw is not None:
                return httpx.Response(200, content=self.generate_raw, request=request)
            if self.generate_payload is not None:
                return httpx.Response(200, json=self.generate_payload, request=request)
            return httpx.Response(
                200,
                json={
                    "candidates": [
                        {
                            "content": {"parts": [{"text": self._text_for(request)}]},
                            "finishReason": "STOP",
                        }
                    ],
                    "usageMetadata": {"promptTokenCount": 1, "totalTokenCount": 2},
                },
                request=request,
            )
        return httpx.Response(
            404, json={"error": {"message": f"unknown endpoint {url}"}}, request=request,
        )

    # ── helpers ──

    def _error(self, request: httpx.Request, status: int) -> httpx.Response:
        return httpx.Response(
            status,
            json={"error": {"message": self.detail or "scripted failure", "code": status,
                            "status": "SCRIPTED"}},
            request=request,
        )

    def _text_for(self, request: httpx.Request) -> str:
        """OCR vs STT is decided by the MIME type the engine actually sent."""
        for part in _body(request)["contents"][0]["parts"]:
            if "inlineData" in part:
                return self.ocr_text if part["inlineData"]["mimeType"].startswith("image/") \
                    else self.stt_text
            if "fileData" in part:
                return self.stt_text
        return ""

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
    """Install a scripted transport under the engine (no key, no network)."""
    created: list[_GeminiStub] = []

    def _install(**kwargs: Any) -> _GeminiStub:
        transport = _GeminiStub(**kwargs)
        created.append(transport)
        real_client = httpx.Client

        def factory(*args: Any, **inner: Any) -> httpx.Client:
            inner["transport"] = transport
            return real_client(*args, **inner)

        monkeypatch.setattr(httpx, "Client", factory)
        return transport

    return _install


def _body(request: httpx.Request) -> dict[str, Any]:
    return json.loads(request.content.decode("utf-8"))


def _instruction(request: httpx.Request) -> str:
    return _body(request)["contents"][0]["parts"][0]["text"]


def _media_part(request: httpx.Request) -> dict[str, Any]:
    return _body(request)["contents"][0]["parts"][-1]


def _sent_mime(request: httpx.Request) -> str:
    part = _media_part(request)
    if "inlineData" in part:
        return part["inlineData"]["mimeType"]
    return part["fileData"]["mimeType"]


def _engine(model: str = DEFAULT_MEDIA_MODEL) -> GeminiMediaEngine:
    return GeminiMediaEngine(API_KEY, model, key_env_var="AI_GEMINI_API_KEY")


def _provision(engine: GeminiMediaEngine | None) -> None:
    media_service.set_ocr_engine(engine)
    media_service.set_stt_engine(engine)


# ── Fake Telegram surface (mirrors tests/test_media_stt.py) ──


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


def _media_temp_dirs() -> set[str]:
    try:
        entries = os.listdir(tempfile.gettempdir())
    except OSError:  # pragma: no cover - defensive
        return set()
    return {name for name in entries if name.startswith("lifeos_media_")}


# ── Real container fixtures (stdlib encoders, no dependency) ──


def _png(width: int, height: int) -> bytes:
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


def _bmp(width: int, height: int) -> bytes:
    return (
        b"BM" + struct.pack("<I", 54) + b"\x00\x00\x00\x00" + struct.pack("<I", 54)
        + struct.pack("<I", 40) + struct.pack("<ii", width, height)
        + b"\x01\x00\x18\x00"
    )


def _webp_vp8x(width: int, height: int) -> bytes:
    payload = (
        b"\x00" + b"\x00\x00\x00"
        + (width - 1).to_bytes(3, "little") + (height - 1).to_bytes(3, "little")
    )
    chunk = b"VP8X" + struct.pack("<I", len(payload)) + payload
    body = b"WEBP" + chunk
    return b"RIFF" + struct.pack("<I", len(body)) + body


def _photo_media() -> MessageMediaPhoto:
    """Classifier labels this ``Photo`` with ``image/jpeg``."""
    return MessageMediaPhoto(photo=Photo(
        id=3, access_hash=3, file_reference=b"", date=None,
        sizes=[PhotoSize(type="y", w=8, h=8, size=len(_jpeg(8, 8)))], dc_id=1,
    ))


def _image_document(mime: str, name: str, size: int) -> MessageMediaDocument:
    return MessageMediaDocument(document=Document(
        id=2, access_hash=2, file_reference=b"", date=None, mime_type=mime, size=size,
        dc_id=1, attributes=[DocumentAttributeFilename(file_name=name)],
    ))


def _opus_head(channels: int = 1, sample_rate: int = 48_000) -> bytes:
    return (
        b"OpusHead" + bytes([1, channels])
        + (312).to_bytes(2, "little") + sample_rate.to_bytes(4, "little")
        + (0).to_bytes(2, "little") + bytes([0])
    )


def _ogg_page(header_type: int, granule: int, sequence: int, packet: bytes) -> bytes:
    lacing: list[int] = []
    remaining = len(packet)
    while remaining >= 255:
        lacing.append(255)
        remaining -= 255
    lacing.append(remaining)
    return (
        b"OggS" + bytes([0, header_type])
        + granule.to_bytes(8, "little")
        + (1).to_bytes(4, "little") + sequence.to_bytes(4, "little")
        + (0).to_bytes(4, "little")
        + bytes([len(lacing)]) + bytes(lacing) + packet
    )


def _ogg_opus(duration_s: float, *, channels: int = 1, sample_rate: int = 48_000) -> bytes:
    granule = int(duration_s * 48_000)
    return (
        _ogg_page(0x02, 0, 0, _opus_head(channels, sample_rate))
        + _ogg_page(0x04, granule, 1, b"\x00" * 40)
    )


def _wav(duration_s: float, *, channels: int = 1, sample_rate: int = 16_000) -> bytes:
    buffer = io.BytesIO()
    handle = wave.open(buffer, "wb")
    handle.setnchannels(channels)
    handle.setsampwidth(2)
    handle.setframerate(sample_rate)
    handle.writeframes(b"\x00\x00" * int(duration_s * sample_rate) * channels)
    handle.close()
    return buffer.getvalue()


def _flac(duration_s: float, *, channels: int = 1, sample_rate: int = 16_000) -> bytes:
    total_samples = int(duration_s * sample_rate)
    packed = (sample_rate << 44) | ((channels - 1) << 41) | total_samples
    streaminfo = b"\x00" * 10 + packed.to_bytes(8, "big") + b"\x00" * 16
    return b"fLaC" + b"\x00" + (34).to_bytes(3, "big") + streaminfo


def _voice_message(payload: bytes, *, mime: str = "audio/ogg",
                   duration: int = 2) -> _FakeMessage:
    doc = Document(
        id=1, access_hash=1, file_reference=b"", date=None, mime_type=mime,
        size=len(payload), dc_id=1,
        attributes=[DocumentAttributeAudio(duration=duration, voice=True)],
    )
    return _FakeMessage(MessageMediaDocument(document=doc))


def _audio_message(payload: bytes, *, mime: str = "audio/wav",
                   name: str = "track.wav", size: int | None = None) -> _FakeMessage:
    doc = Document(
        id=2, access_hash=2, file_reference=b"", date=None, mime_type=mime,
        size=len(payload) if size is None else size, dc_id=1,
        attributes=[
            DocumentAttributeAudio(duration=2, voice=False),
            DocumentAttributeFilename(file_name=name),
        ],
    )
    return _FakeMessage(MessageMediaDocument(document=doc))


# ── Global state hygiene ──


@pytest.fixture(autouse=True)
def _reset_engines_and_env(monkeypatch):
    """No engine and no Gemini ENV leaks between tests."""
    ocr, stt = media_service.get_ocr_engine(), media_service.get_stt_engine()
    media_service.set_ocr_engine(None)
    media_service.set_stt_engine(None)
    for name in ("AI_GEMINI_API_KEY", "GEMINI_API_KEY", "AI_GEMINI_MEDIA_MODEL",
                 "AI_GEMINI_MODEL", "AI_GEMINI_STT_MODEL", "AI_GEMINI_STT_LANGUAGE"):
        monkeypatch.delenv(name, raising=False)
    yield
    media_service.set_ocr_engine(ocr)
    media_service.set_stt_engine(stt)


# ── 1. Configuration: the EXISTING conventions, nothing parallel ──


def test_no_gemini_engine_is_provisioned_by_default():
    assert media_service.ocr_available() is False
    assert media_service.stt_available() is False
    assert media_service.get_ocr_engine() is None
    assert media_service.get_stt_engine() is None


def test_missing_credential_leaves_both_seams_unavailable(monkeypatch):
    status = provision_gemini_media_engines()

    assert status["configured"] is False
    assert "AI_GEMINI_API_KEY" in status["reason"]
    assert media_service.ocr_available() is False
    assert media_service.stt_available() is False


def test_the_existing_gemini_key_provisions_both_seams(monkeypatch):
    monkeypatch.setenv("AI_GEMINI_API_KEY", API_KEY)

    status = provision_gemini_media_engines()

    assert status == {
        "configured": True,
        "model": DEFAULT_MEDIA_MODEL,
        "key_env_var": "AI_GEMINI_API_KEY",
        "reason": "",
    }
    assert media_service.ocr_available() is True
    assert media_service.stt_available() is True
    # ONE engine satisfies both seams: no second provider-selection system.
    assert media_service.get_ocr_engine() is media_service.get_stt_engine()
    assert isinstance(media_service.get_ocr_engine(), GeminiMediaEngine)


def test_the_plain_gemini_key_variable_is_the_documented_fallback(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", API_KEY)

    status = provision_gemini_media_engines()

    assert status["configured"] is True
    assert status["key_env_var"] == "GEMINI_API_KEY"


def test_the_ai_gemini_key_wins_over_the_plain_one(monkeypatch):
    monkeypatch.setenv("AI_GEMINI_API_KEY", "ai-key")
    monkeypatch.setenv("GEMINI_API_KEY", "plain-key")

    key, env_var = resolve_api_key()

    assert (key, env_var) == ("ai-key", "AI_GEMINI_API_KEY")


def test_a_blank_credential_is_not_a_credential(monkeypatch):
    monkeypatch.setenv("AI_GEMINI_API_KEY", "   ")

    status = provision_gemini_media_engines()

    assert status["configured"] is False
    assert media_service.ocr_available() is False


def test_media_model_defaults_to_the_smallest_supported_model():
    assert DEFAULT_MEDIA_MODEL == "gemini-3.5-flash-lite"
    engine, model, reason = build_gemini_media_engine()
    # No credential ⇒ no engine, and the failure is a value, not an exception.

    assert engine is None and model == "" and reason


def test_media_model_resolution_order(monkeypatch):
    monkeypatch.setenv("AI_GEMINI_API_KEY", API_KEY)

    _, env_var = resolve_media_model()
    assert env_var == ""
    assert build_gemini_media_engine()[1] == DEFAULT_MEDIA_MODEL

    monkeypatch.setenv("AI_GEMINI_MODEL", "gemini-3.6-flash")
    assert resolve_media_model() == ("gemini-3.6-flash", "AI_GEMINI_MODEL")

    # The dedicated media override takes precedence over the chat model.
    monkeypatch.setenv("AI_GEMINI_MEDIA_MODEL", "gemini-2.5-flash-lite")
    assert resolve_media_model() == ("gemini-2.5-flash-lite", "AI_GEMINI_MEDIA_MODEL")
    engine = build_gemini_media_engine()[0]
    assert engine is not None and engine.model == "gemini-2.5-flash-lite"


def test_a_deprecated_configured_model_is_resolved_before_use(monkeypatch):
    monkeypatch.setenv("AI_GEMINI_API_KEY", API_KEY)
    monkeypatch.setenv("AI_GEMINI_MEDIA_MODEL", "gemini-2.0-flash")

    # The project's existing deprecation map is reused — no second one.
    assert resolve_media_model() == ("gemini-2.5-flash", "AI_GEMINI_MEDIA_MODEL")


def test_provisioning_never_raises_and_never_makes_a_request():
    class _Exploding(httpx.BaseTransport):
        def handle_request(self, request: httpx.Request) -> httpx.Response:  # pragma: no cover
            raise AssertionError("provisioning must not contact the API")

    real_client = httpx.Client
    httpx.Client = lambda *a, **kw: real_client(*a, **{**kw, "transport": _Exploding()})
    try:
        status = provision_gemini_media_engines()  # unconfigured
        assert status["configured"] is False
    finally:
        httpx.Client = real_client


def test_the_status_never_carries_the_credential(monkeypatch):
    monkeypatch.setenv("AI_GEMINI_API_KEY", API_KEY)

    status = provision_gemini_media_engines()

    assert status["key_env_var"] == "AI_GEMINI_API_KEY"
    assert API_KEY not in json.dumps(status)
    assert API_KEY not in repr(media_service.get_ocr_engine())


def test_provisioning_is_wired_only_from_the_runtime_supervisor():
    root = Path(__file__).resolve().parent.parent / "backend"
    callers = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*.py")
        if "provision_gemini_media_engines(" in path.read_text(encoding="utf-8")
    }
    # The engine's own definition plus the ONE startup wiring point.
    assert callers == {"services/gemini_media_engine.py", "runtime/supervisor.py"}


def test_the_supervisor_startup_path_provisions_the_media_engines():
    from backend.runtime.supervisor import RuntimeSupervisor

    source = inspect.getsource(RuntimeSupervisor.start)
    assert "_provision_media_engines()" in source


def test_the_supervisor_hook_provisions_and_clears_the_seams(monkeypatch):
    from backend.runtime.supervisor import RuntimeSupervisor

    supervisor = RuntimeSupervisor({
        "API_ID": 1, "API_HASH": "hash", "SESSION_STRING": "session",
        "OWNER_ID": OWNER, "TZ": "UTC",
    })

    supervisor._provision_media_engines()
    assert media_service.ocr_available() is False

    monkeypatch.setenv("AI_GEMINI_API_KEY", API_KEY)
    supervisor._provision_media_engines()
    assert media_service.ocr_available() is True
    assert media_service.stt_available() is True


# ── 2. Unconfigured runtime keeps failing closed (M1.3/M1.4 behaviour) ──


@pytest.mark.asyncio
async def test_unconfigured_runtime_reports_images_unsupported_without_transfer():
    client = _FakeClient(payload=_png(4, 4))

    analysis = await media_service.analyze_media(client, OWNER, _FakeMessage(_photo_media()))

    assert analysis.status == MediaStatus.UNSUPPORTED
    assert "OCR" in analysis.reason
    assert client.calls == []


@pytest.mark.asyncio
async def test_unconfigured_runtime_reports_voice_unsupported_without_transfer():
    payload = _ogg_opus(2.0)
    client = _FakeClient(payload=payload)

    analysis = await media_service.analyze_media(client, OWNER, _voice_message(payload))

    assert analysis.status == MediaStatus.UNSUPPORTED
    assert "Speech-to-text" in analysis.reason
    assert client.calls == []


# ── 3. OCR through the real boundary and the real Gemini engine ──


@pytest.mark.asyncio
async def test_ocr_sends_only_the_image_and_the_deterministic_instruction(stub):
    transport = stub(ocr_text="Invoice 42\nTotal: 10 EUR")
    _provision(_engine())
    payload = _jpeg(8, 8)
    client = _FakeClient(payload=payload)

    analysis = await media_service.analyze_media(client, OWNER, _FakeMessage(_photo_media()))

    assert analysis.status == MediaStatus.EXTRACTED
    assert analysis.content == "Invoice 42\nTotal: 10 EUR"
    assert len(client.calls) == 1 and len(transport.requests) == 1
    request = transport.generate_requests[0]
    # The documented Generate Content dialect the provider adapter already speaks.
    assert str(request.url) == f"{GEMINI_API_BASE}/models/{DEFAULT_MEDIA_MODEL}:generateContent"
    # The credential travels in the header and NEVER in the URL.
    assert request.headers["x-goog-api-key"] == API_KEY
    assert API_KEY not in str(request.url)
    # Exactly two parts: the fixed instruction and the image itself.
    body = _body(request)
    assert len(body["contents"]) == 1
    assert [sorted(p) for p in body["contents"][0]["parts"]] == [
        ["text"], ["inlineData"],
    ]
    assert _instruction(request) == OCR_INSTRUCTION
    part = _media_part(request)
    assert part["inlineData"]["mimeType"] == "image/jpeg"
    assert base64.b64decode(part["inlineData"]["data"]) == payload
    assert body["generationConfig"]["temperature"] == 0.0
    assert body["generationConfig"]["maxOutputTokens"] == MAX_OUTPUT_TOKENS
    assert 0 < body["generationConfig"]["maxOutputTokens"] <= MAX_OUTPUT_TOKENS
    assert _media_temp_dirs() == set(), "the boundary must still clean up"


@pytest.mark.asyncio
async def test_ocr_runs_off_the_event_loop(stub):
    transport = stub(ocr_text="text")
    _provision(_engine())
    payload = _png(4, 4)
    client = _FakeClient(payload=payload)
    media = _image_document("image/png", FILE_NAME, len(payload))

    await media_service.analyze_media(client, OWNER, _FakeMessage(media))

    assert transport.threads and transport.threads[0] != threading.main_thread().name


@pytest.mark.asyncio
async def test_ocr_result_flows_through_the_boundary_unchanged(stub):
    stub(ocr_text=f"{_PERSIAN_ZWNJ} {_PERSIAN}")
    _provision(_engine())
    client = _FakeClient(payload=_png(4, 4))
    media = _image_document("image/png", FILE_NAME, len(_png(4, 4)))

    analysis = await media_service.analyze_media(client, OWNER, _FakeMessage(media))

    assert analysis.content == f"{_PERSIAN_ZWNJ} {_PERSIAN}"
    assert analysis.content.count(_ZWNJ) == 1
    assert _PERSIAN in analysis.as_context_text()


@pytest.mark.asyncio
async def test_ocr_multi_part_text_is_preserved_in_reading_order(stub):
    stub(generate_payload={
        "candidates": [{
            "content": {"parts": [{"text": "first line"}, {"text": "second line"}]},
            "finishReason": "STOP",
        }],
    })
    _provision(_engine())
    client = _FakeClient(payload=_png(4, 4))
    media = _image_document("image/png", FILE_NAME, len(_png(4, 4)))

    analysis = await media_service.analyze_media(client, OWNER, _FakeMessage(media))

    assert analysis.content == "first line\nsecond line"


@pytest.mark.asyncio
async def test_ocr_no_text_is_reported_honestly_without_content(stub):
    stub(ocr_text="")
    _provision(_engine())
    client = _FakeClient(payload=_png(4, 4))
    media = _image_document("image/png", FILE_NAME, len(_png(4, 4)))

    analysis = await media_service.analyze_media(client, OWNER, _FakeMessage(media))

    assert analysis.status == MediaStatus.EXTRACTED
    assert analysis.content == ""
    assert analysis.has_content is False
    assert analysis.reason == "No readable text was detected in the image."


@pytest.mark.asyncio
async def test_ocr_whitespace_only_output_counts_as_no_text(stub):
    stub(ocr_text="   \n\n  \t ")
    _provision(_engine())
    client = _FakeClient(payload=_png(4, 4))
    media = _image_document("image/png", FILE_NAME, len(_png(4, 4)))

    analysis = await media_service.analyze_media(client, OWNER, _FakeMessage(media))

    assert analysis.has_content is False
    assert analysis.reason == "No readable text was detected in the image."


@pytest.mark.asyncio
async def test_ocr_oversized_text_is_capped_by_the_existing_ceiling(stub):
    stub(ocr_text="ا" * (MAX_OCR_CHARS + 5_000))
    _provision(_engine())
    client = _FakeClient(payload=_png(4, 4))
    media = _image_document("image/png", FILE_NAME, len(_png(4, 4)))

    analysis = await media_service.analyze_media(client, OWNER, _FakeMessage(media))

    assert len(analysis.content) == MAX_OCR_CHARS
    assert analysis.truncated is True


# ── 4. OCR failure paths: controlled, sanitized, never fabricated ──


async def _ocr_analysis(client: _FakeClient):
    return await media_service.analyze_media(client, OWNER, _FakeMessage(_photo_media()))


@pytest.mark.asyncio
async def test_ocr_malformed_response_fails_controlled(stub):
    stub(generate_payload={"foo": "bar"})
    _provision(_engine())

    with pytest.raises(MediaError) as exc:
        await _ocr_analysis(_FakeClient(payload=_jpeg(8, 8)))

    assert "no OCR result" in str(exc.value)


@pytest.mark.asyncio
async def test_ocr_non_json_response_fails_controlled(stub):
    stub(generate_raw=b"<html>not json</html>")
    _provision(_engine())

    with pytest.raises(MediaError) as exc:
        await _ocr_analysis(_FakeClient(payload=_jpeg(8, 8)))

    assert "unreadable OCR response" in str(exc.value)


@pytest.mark.asyncio
async def test_ocr_blocked_response_fails_controlled(stub):
    stub(generate_payload={"promptFeedback": {"blockReason": "SAFETY"}})
    _provision(_engine())

    with pytest.raises(MediaError) as exc:
        await _ocr_analysis(_FakeClient(payload=_jpeg(8, 8)))

    assert "SAFETY" in str(exc.value)


@pytest.mark.asyncio
async def test_ocr_safety_finish_reason_fails_controlled(stub):
    stub(generate_payload={"candidates": [{"content": {"parts": []}, "finishReason": "SAFETY"}]})
    _provision(_engine())

    with pytest.raises(MediaError) as exc:
        await _ocr_analysis(_FakeClient(payload=_jpeg(8, 8)))

    assert "SAFETY" in str(exc.value)


@pytest.mark.parametrize("status,expected", [
    (401, "rejected the configured API key"),
    (403, "rejected the configured API key"),
    (429, "rate limited"),
    (404, "could not find the configured model"),
    (500, "unavailable"),
    (400, "refused the OCR request"),
])
@pytest.mark.asyncio
async def test_ocr_api_failures_are_controlled(stub, status, expected):
    transport = stub(generate_status=status)
    _provision(_engine())

    with pytest.raises(MediaError) as exc:
        await _ocr_analysis(_FakeClient(payload=_jpeg(8, 8)))

    assert expected in str(exc.value)
    assert len(transport.generate_requests) == 1, "no retry loop inside the engine"


@pytest.mark.asyncio
async def test_an_api_failure_never_echoes_the_credential(stub):
    stub(generate_status=401, detail=f"API key not valid: {API_KEY}")
    _provision(_engine())

    with pytest.raises(MediaError) as exc:
        await _ocr_analysis(_FakeClient(payload=_jpeg(8, 8)))

    message = str(exc.value)
    assert API_KEY not in message
    assert "***" in message


@pytest.mark.asyncio
async def test_ocr_timeout_is_controlled(stub):
    stub(raise_error=httpx.ReadTimeout("scripted timeout"))
    _provision(_engine())

    with pytest.raises(MediaError) as exc:
        await _ocr_analysis(_FakeClient(payload=_jpeg(8, 8)))

    assert "timed out" in str(exc.value)


@pytest.mark.asyncio
async def test_ocr_transport_failure_is_controlled(stub):
    stub(raise_error=httpx.ConnectError("scripted transport failure"))
    _provision(_engine())

    with pytest.raises(MediaError) as exc:
        await _ocr_analysis(_FakeClient(payload=_jpeg(8, 8)))

    assert "ConnectError" in str(exc.value)


@pytest.mark.asyncio
async def test_ocr_failure_reaches_the_owner_as_a_media_error_not_as_content(stub):
    stub(generate_status=500)
    _provision(_engine())
    client = _FakeClient(payload=_jpeg(8, 8))

    with pytest.raises(MediaError):
        await _ocr_analysis(client)

    # The boundary still cleaned up and never returned a partial analysis.
    assert _media_temp_dirs() == set()


# ── 5. OCR MIME propagation and the pre-Gemini guards ──


@pytest.mark.parametrize("mime,payload,expected", [
    ("image/png", _png(4, 4), "image/png"),
    ("image/jpeg", _jpeg(8, 8), "image/jpeg"),
    ("image/jpg", _jpeg(8, 8), "image/jpeg"),
    ("image/webp", _webp_vp8x(4, 4), "image/webp"),
])
@pytest.mark.asyncio
async def test_ocr_propagates_the_validated_container_as_a_documented_mime(
    stub, mime, payload, expected,
):
    transport = stub(ocr_text="ok")
    _provision(_engine())
    client = _FakeClient(payload=payload)

    analysis = await media_service.analyze_media(
        client, OWNER, _FakeMessage(_image_document(mime, FILE_NAME, len(payload))),
    )

    assert analysis.content == "ok"
    assert _sent_mime(transport.generate_requests[0]) == expected


@pytest.mark.parametrize("mime,payload", [
    ("image/gif", _gif(4, 4)),
    ("image/bmp", _bmp(4, 4)),
])
@pytest.mark.asyncio
async def test_ocr_refuses_a_container_gemini_does_not_document(stub, mime, payload):
    transport = stub()
    _provision(_engine())
    client = _FakeClient(payload=payload)

    with pytest.raises(MediaError) as exc:
        await media_service.analyze_media(
            client, OWNER, _FakeMessage(_image_document(mime, FILE_NAME, len(payload))),
        )

    assert "Gemini does not accept" in str(exc.value)
    assert transport.requests == [], "an undocumented format must never be sent"


@pytest.mark.asyncio
async def test_ocr_is_never_contacted_when_the_payload_contradicts_the_mime(stub):
    transport = stub()
    _provision(_engine())
    client = _FakeClient(payload=_wav(1.0))
    media = _image_document("image/png", FILE_NAME, len(_wav(1.0)))

    with pytest.raises(MediaError) as exc:
        await media_service.analyze_media(client, OWNER, _FakeMessage(media))

    assert "not a readable image" in str(exc.value)
    assert transport.requests == []


@pytest.mark.asyncio
async def test_ocr_is_never_contacted_for_an_oversized_bitmap(stub):
    transport = stub()
    _provision(_engine())
    payload = _png(4, 4)
    client = _FakeClient(payload=payload)
    media = _image_document("image/png", "bomb.png", len(payload))

    with pytest.raises(MediaError):
        await media_service.analyze_media(client, OWNER, media)

    assert transport.requests == []


@pytest.mark.asyncio
async def test_an_image_beyond_the_ocr_input_bound_is_never_transferred(stub):
    transport = stub()
    _provision(_engine())
    payload = _png(4, 4)
    client = _FakeClient(payload=payload)
    media = _image_document("image/png", FILE_NAME, MAX_OCR_INPUT_BYTES + 1)

    with pytest.raises(MediaError) as exc:
        await media_service.analyze_media(client, OWNER, _FakeMessage(media))

    assert "processing limit" in str(exc.value)
    assert client.calls == [], "an image past the bound is refused before the transfer"
    assert transport.requests == []


@pytest.mark.asyncio
async def test_gemini_mime_refuses_an_unrecognised_container():
    with pytest.raises(MediaError):
        gemini_mime_type(b"not a container at all")


# ── 6. STT through the real boundary and the real Gemini engine ──


@pytest.mark.asyncio
async def test_stt_sends_only_the_audio_and_the_deterministic_instruction(stub):
    transport = stub(stt_text="سلام دنیا")
    _provision(_engine())
    payload = _ogg_opus(2.0)
    client = _FakeClient(payload=payload)

    analysis = await media_service.analyze_media(client, OWNER, _voice_message(payload))

    assert analysis.status == MediaStatus.EXTRACTED
    assert analysis.content == "سلام دنیا"
    request = transport.generate_requests[0]
    assert str(request.url) == f"{GEMINI_API_BASE}/models/{DEFAULT_MEDIA_MODEL}:generateContent"
    assert request.headers["x-goog-api-key"] == API_KEY
    assert _instruction(request) == STT_INSTRUCTION
    part = _media_part(request)
    assert part["inlineData"]["mimeType"] == "audio/ogg"
    assert base64.b64decode(part["inlineData"]["data"]) == payload
    assert transport.upload_requests == []
    assert _media_temp_dirs() == set()


@pytest.mark.asyncio
async def test_stt_result_flows_through_the_boundary_unchanged(stub):
    stub(stt_text=f"{_PERSIAN_ZWNJ} {_PERSIAN}")
    _provision(_engine())
    payload = _ogg_opus(2.0)
    client = _FakeClient(payload=payload)

    analysis = await media_service.analyze_media(client, OWNER, _voice_message(payload))

    assert analysis.content == f"{_PERSIAN_ZWNJ} {_PERSIAN}"
    assert analysis.content.count(_ZWNJ) == 1


@pytest.mark.parametrize("mime,payload,expected", [
    ("audio/ogg", _ogg_opus(2.0), "audio/ogg"),
    ("audio/opus", _ogg_opus(2.0), "audio/ogg"),
    ("application/ogg", _ogg_opus(2.0), "audio/ogg"),
    ("audio/wav", _wav(1.0), "audio/wav"),
    ("audio/x-wav", _wav(1.0), "audio/wav"),
    ("audio/wave", _wav(1.0), "audio/wav"),
    ("audio/vnd.wave", _wav(1.0), "audio/wav"),
    ("audio/flac", _flac(1.0), "audio/flac"),
    ("audio/x-flac", _flac(1.0), "audio/flac"),
])
@pytest.mark.asyncio
async def test_stt_propagates_the_validated_container_as_a_documented_mime(
    stub, mime, payload, expected,
):
    transport = stub(stt_text="ok")
    _provision(_engine())
    client = _FakeClient(payload=payload)
    name = "track" + (".ogg" if mime.endswith("ogg") else ".bin")
    message = (
        _voice_message(payload, mime=mime) if mime == "audio/ogg"
        else _audio_message(payload, mime=mime, name=name)
    )

    analysis = await media_service.analyze_media(client, OWNER, message)

    assert analysis.content == "ok", mime
    assert _sent_mime(transport.generate_requests[0]) == expected


@pytest.mark.asyncio
async def test_stt_no_speech_is_reported_honestly(stub):
    stub(stt_text="")
    _provision(_engine())
    payload = _ogg_opus(2.0)
    client = _FakeClient(payload=payload)

    analysis = await media_service.analyze_media(client, OWNER, _voice_message(payload))

    assert analysis.status == MediaStatus.EXTRACTED
    assert analysis.content == ""
    assert analysis.has_content is False
    assert analysis.reason == "No speech was detected in the audio."


@pytest.mark.asyncio
async def test_stt_oversized_transcript_is_capped_by_the_existing_ceiling(stub):
    stub(stt_text="ب" * (MAX_STT_CHARS + 5_000))
    _provision(_engine())
    payload = _wav(1.0)
    client = _FakeClient(payload=payload)

    analysis = await media_service.analyze_media(client, OWNER, _audio_message(payload))

    assert len(analysis.content) == MAX_STT_CHARS
    assert analysis.truncated is True


# ── 7. STT failure paths ──


async def _stt_analysis(client: _FakeClient):
    payload = _wav(1.0)
    return await media_service.analyze_media(client, OWNER, _audio_message(payload))


@pytest.mark.asyncio
async def test_stt_malformed_response_fails_controlled(stub):
    stub(generate_payload={"candidates": []})
    _provision(_engine())

    with pytest.raises(MediaError) as exc:
        await _stt_analysis(_FakeClient(payload=_wav(1.0)))

    assert "no speech-to-text result" in str(exc.value)


@pytest.mark.asyncio
async def test_stt_non_json_response_fails_controlled(stub):
    stub(generate_raw=b"{}")
    _provision(_engine())

    with pytest.raises(MediaError) as exc:
        await _stt_analysis(_FakeClient(payload=_wav(1.0)))

    assert "no speech-to-text result" in str(exc.value)


@pytest.mark.parametrize("status,expected", [
    (401, "rejected the configured API key"),
    (403, "rejected the configured API key"),
    (429, "rate limited"),
    (500, "unavailable"),
    (400, "refused the speech-to-text request"),
])
@pytest.mark.asyncio
async def test_stt_api_failures_are_controlled(stub, status, expected):
    transport = stub(generate_status=status)
    _provision(_engine())

    with pytest.raises(MediaError) as exc:
        await _stt_analysis(_FakeClient(payload=_wav(1.0)))

    assert expected in str(exc.value)
    assert len(transport.generate_requests) == 1, "no retry loop inside the engine"


@pytest.mark.asyncio
async def test_stt_timeout_is_controlled(stub):
    stub(raise_error=httpx.ReadTimeout("scripted timeout"))
    _provision(_engine())

    with pytest.raises(MediaError) as exc:
        await _stt_analysis(_FakeClient(payload=_wav(1.0)))

    assert "timed out" in str(exc.value)


@pytest.mark.asyncio
async def test_stt_engine_bounds_are_finite_and_inside_the_boundary_bounds():
    assert 0 < engine_module.OCR_TIMEOUT_S < media_service.OCR_TIMEOUT_S
    assert 0 < engine_module.STT_TIMEOUT_S < media_service.STT_TIMEOUT_S
    assert 0 < engine_module.INLINE_PAYLOAD_MAX_BYTES < media_service.MAX_STT_INPUT_BYTES


# ── 8. The Files API path for audio past the inline budget ──


def _large_wav() -> bytes:
    """2 channels × 16 kHz × 16-bit × 290 s ≈ 18.5 MiB: inside the STT bound,
    past the inline budget, so the documented Files API flow is required."""
    payload = _wav(290.0, channels=2, sample_rate=16_000)
    assert engine_module.INLINE_PAYLOAD_MAX_BYTES < len(payload) <= media_service.MAX_STT_INPUT_BYTES
    return payload


@pytest.mark.asyncio
async def test_large_audio_uses_the_documented_files_api_and_is_deleted(stub):
    transport = stub(stt_text="transcribed")
    _provision(_engine())
    payload = _large_wav()
    client = _FakeClient(payload=payload)

    analysis = await media_service.analyze_media(client, OWNER, _audio_message(payload))

    assert analysis.content == "transcribed"
    # 1) the resumable upload starts, 2) one finalize request carries the bytes,
    # 3) generateContent references the uploaded file, 4) the file is deleted.
    start = transport.upload_requests[0]
    assert str(start.url).startswith(GEMINI_UPLOAD_BASE)
    assert start.headers["X-Goog-Upload-Command"] == "start"
    assert start.headers["X-Goog-Upload-Protocol"] == "resumable"
    assert json.loads(start.content.decode()) == {"file": {"display_name": "lifeos-media"}}
    finalize = transport.requests[1]
    assert str(finalize.url) == _UPLOAD_URL
    assert finalize.headers["X-Goog-Upload-Command"] == "upload, finalize"
    assert finalize.content == payload
    part = _media_part(transport.generate_requests[0])
    assert part["fileData"] == {"mimeType": "audio/wav", "fileUri": _FILE_URI}
    assert transport.deleted == [f"{GEMINI_API_BASE}/{_FILE_NAME}"]
    assert transport.generate_requests and len(transport.generate_requests) == 1
    # No identifier beyond the fixed display name ever reaches the API.
    assert FILE_NAME.encode() not in transport.sent_bytes()
    assert CAPTION.encode() not in transport.sent_bytes()


@pytest.mark.asyncio
async def test_uploaded_audio_is_deleted_even_when_generation_fails(stub):
    transport = stub(generate_status=500)
    _provision(_engine())
    payload = _large_wav()

    with pytest.raises(MediaError):
        await media_service.analyze_media(
            _FakeClient(payload=payload), OWNER, _audio_message(payload),
        )

    assert transport.deleted == [f"{GEMINI_API_BASE}/{_FILE_NAME}"]


@pytest.mark.asyncio
async def test_upload_failure_is_controlled_and_attempted_once(stub):
    transport = stub(upload_status=401)
    _provision(_engine())
    payload = _large_wav()

    with pytest.raises(MediaError) as exc:
        await media_service.analyze_media(
            _FakeClient(payload=payload), OWNER, _audio_message(payload),
        )

    assert "rejected the configured API key" in str(exc.value)
    assert len(transport.upload_requests) == 1
    assert transport.generate_requests == []
    assert transport.deleted == []


@pytest.mark.asyncio
async def test_a_failed_remote_cleanup_never_masks_the_result(stub):
    transport = stub(stt_text="ok", delete_status=500)
    _provision(_engine())
    payload = _large_wav()

    analysis = await media_service.analyze_media(
        _FakeClient(payload=payload), OWNER, _audio_message(payload),
    )

    # The API auto-expires uploads; a failed delete must not fail the operation.
    assert analysis.content == "ok"
    assert transport.deleted == []


@pytest.mark.asyncio
async def test_a_truncated_generation_is_still_returned_honestly(stub, caplog):
    stub(generate_payload={
        "candidates": [{"content": {"parts": [{"text": "partial"}]}, "finishReason": "MAX_TOKENS"}],
    })
    _provision(_engine())
    payload = _ogg_opus(2.0)

    with caplog.at_level("WARNING", logger="backend.services.gemini_media_engine"):
        analysis = await media_service.analyze_media(
            _FakeClient(payload=payload), OWNER, _voice_message(payload),
        )

    # Usable text is returned; the token-ceiling truncation is traced, not hidden.
    assert analysis.content == "partial"
    assert any("OUTPUT_TRUNCATED" in record.getMessage() for record in caplog.records)


# ── 9. Pre-Gemini bounds and routing for audio ──


@pytest.mark.asyncio
async def test_a_container_longer_than_the_duration_bound_never_reaches_gemini(stub):
    transport = stub()
    _provision(_engine())
    payload = _ogg_opus(330.0)
    client = _FakeClient(payload=payload)

    with pytest.raises(MediaError) as exc:
        await media_service.analyze_media(client, OWNER, _voice_message(payload))

    assert "speech-to-text bound" in str(exc.value)
    assert transport.requests == []


@pytest.mark.asyncio
async def test_a_container_beyond_the_channel_bound_never_reaches_gemini(stub):
    transport = stub()
    _provision(_engine())
    payload = _wav(1.0, channels=3)
    client = _FakeClient(payload=payload)

    with pytest.raises(MediaError) as exc:
        await media_service.analyze_media(client, OWNER, _audio_message(payload))

    assert "channel" in str(exc.value)
    assert transport.requests == []


@pytest.mark.asyncio
async def test_an_unsupported_audio_container_is_never_transferred(stub):
    transport = stub()
    _provision(_engine())
    payload = b"ID3\x03\x00\x00"
    client = _FakeClient(payload=payload)
    media = _audio_message(payload, mime="audio/mpeg")

    analysis = await media_service.analyze_media(client, OWNER, _FakeMessage(media))

    assert analysis.status == MediaStatus.UNSUPPORTED
    assert client.calls == [] and transport.requests == []


# ── 10. Privacy: the engine sees bytes and nothing else ──


@pytest.mark.asyncio
async def test_gemini_receives_no_telegram_metadata_caption_or_history(stub):
    transport = stub(ocr_text="ok", stt_text="ok")
    _provision(_engine())

    image = _png(4, 4)
    await media_service.analyze_media(
        _FakeClient(payload=image), OWNER,
        _FakeMessage(_image_document("image/png", FILE_NAME, len(image))),
    )
    audio = _wav(1.0)
    await media_service.analyze_media(
        _FakeClient(payload=audio), OWNER, _audio_message(audio, name=FILE_NAME),
    )

    sent = transport.sent_bytes()
    texts = " ".join(_instruction(r) for r in transport.generate_requests)
    for forbidden in (CAPTION, FILE_NAME, str(CHAT), str(MESSAGE_ID)):
        assert forbidden.encode() not in sent, forbidden
        assert forbidden not in texts, forbidden
    for word in ("caption", "filename", "sender"):
        assert word not in texts.lower(), word
    # Only the payload plus the two fixed instructions were ever transmitted.
    assert texts.count(OCR_INSTRUCTION) == 1
    assert texts.count(STT_INSTRUCTION) == 1
    assert len(transport.generate_requests) == 2


@pytest.mark.asyncio
async def test_the_instructions_are_static_and_name_nothing(stub):
    transport = stub(ocr_text="ok")
    _provision(_engine())
    payload = _png(4, 4)
    media = _image_document("image/png", FILE_NAME, len(payload))

    await media_service.analyze_media(
        _FakeClient(payload=payload), OWNER, _FakeMessage(media),
    )

    instruction = _instruction(transport.generate_requests[0])
    assert instruction == OCR_INSTRUCTION
    for word in ("Telegram", "chat", "sender", "history", "caption", "user", "owner"):
        assert word not in instruction.lower()
    assert "summar" in instruction.lower() and "invent" in instruction.lower()


def _code_only(module: Any) -> str:
    """The module's source with every string/comment token removed.

    Prose must not be able to satisfy — or break — a dependency assertion, so the
    checks below look at executable code, not at the documentation that names what
    the module deliberately does NOT do.
    """
    tokens = tokenize.generate_tokens(io.StringIO(inspect.getsource(module)).readline)
    kept = [
        token.string
        for token in tokens
        if token.type not in (tokenize.STRING, tokenize.COMMENT, tokenize.NL)
    ]
    return " ".join(kept)


def test_the_engine_module_has_no_provider_or_prompt_dependency():
    code = _code_only(engine_module)
    for banned in (
        "ProviderManager", "PromptBuilder", "prompt_builder",
        "provider_manager", "MEDIA_ANALYSIS_SYSTEM_PROMPT", "context_builder",
    ):
        assert banned not in code, banned
    # The unused/dead provider vision path is never resurrected or referenced.
    assert re.search(r"\bvision\b", code) is None


def test_the_gemini_integration_adds_no_heavy_dependency():
    code = _code_only(engine_module)
    for banned in (
        "torch", "whisper", "onnxruntime", "pytesseract", "cv2", "paddle",
        "tensorflow", "ffmpeg", "google.genai", "google.generativeai", "google.ai",
    ):
        assert banned not in code, banned

    requirements = (Path(__file__).resolve().parent.parent / "backend/requirements.txt").read_text()
    assert "httpx==0.27.0" in requirements
    assert "google" not in requirements.lower()


def test_the_engine_holds_no_telegram_or_event_loop_state():
    # Credential, the general media model and the OPTIONAL dedicated-STT selection
    # only: the engine still holds no Telegram object, no chat/message id and no
    # event-loop state.
    slots = set(GeminiMediaEngine.__slots__)
    assert slots == {
        "_api_key", "_model", "_key_env_var", "_stt_model", "_stt_language",
    }
    # A synchronous engine: the boundary owns the thread offload and the timeout.
    assert not inspect.iscoroutinefunction(GeminiMediaEngine.recognize)
    assert not inspect.iscoroutinefunction(GeminiMediaEngine.transcribe)
