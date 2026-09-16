"""
Media Processing — the STT language contract (the live Persian failure).

A live Persian Voice note, requested with "این ویس رو stt کن", reached the owner
as Latin-script gibberish. The trace pinned the layer:

    MEDIA_STAGE ... stage=media_download_completed type=Voice bytes=35941
    MEDIA_STAGE ... stage=stt_engine_invoked engine=GeminiMediaEngine
    GEMINI_MEDIA_ENGINE kind=speech-to-text mime=audio/ogg bytes=35941 chars=51
    ...
    AI_EXEC_TRACE ... stage=provider_call_completed chars=80

The 51-character engine result IS the user-visible wrong text (the later
80-character provider answer is a different, LATER stage that answers over the
already-wrong transcript), so the transcript was not rewritten on its way out —
the engine produced it. The Generate Content request carries no language
parameter for audio input, so ``STT_INSTRUCTION`` is the engine's ONLY
language-shaping input; that is where this contract is pinned:

  1. the outbound request must require identifying the spoken language and
     transcribing verbatim in that language and its OWN script;
  2. Persian speech must not be translated, transliterated or romanized into
     Latin letters, and no unspoken language may be substituted;
  3. the request stays deterministic — one call per transcription, temperature 0,
     no retry, and no per-request variation of the instruction;
  4. nothing between the engine and the model may rewrite a returned transcript:
     a correct Persian transcript must reach the boundary and the provider-facing
     message letter for letter;
  5. the hardening is STT-only — the OCR contract is untouched.

No network and no model are involved: the HTTP boundary is a scripted ``httpx``
transport and the audio is a real OGG/Opus container built in process. These
tests therefore pin the CONTRACT, not recognition quality — no fixture in this
repository can prove what a remote model hears, and none is faked here. Whether
this instruction actually makes the live Persian transcript correct is NOT proven
by these tests and needs a live Telegram request.
"""
from __future__ import annotations

import base64
import inspect
import io
import json
import tokenize
from typing import Any

import httpx
import pytest
from telethon.tl.types import (
    Document,
    DocumentAttributeAudio,
    MessageMediaDocument,
)

from backend.services import gemini_media_engine, media_ai_service, media_service
from backend.services.gemini_media_engine import (
    GEMINI_API_BASE,
    OCR_INSTRUCTION,
    STT_INSTRUCTION,
    GeminiMediaEngine,
)
from backend.services.media_service import MediaStatus

OWNER = 7770001
CHAT = -1007778889999
MESSAGE_ID = 574942
CAPTION = "caption-that-must-never-reach-the-model"
FILE_NAME = "note.ogg"
API_KEY = "test-gemini-key"
MODEL = "gemini-3.5-flash-lite"

_PERSIAN = "سلام، این یک آزمایش است"
_ZWNJ = "\u200c"
_PERSIAN_ZWNJ = "می" + _ZWNJ + "کند"


# ── Scripted Gemini transport (the HTTP boundary, fully controlled) ──


class _Stub(httpx.BaseTransport):
    """A recording transport: scripted text back, zero network I/O."""

    def __init__(self, stt_text: str = "") -> None:
        self.stt_text = stt_text
        self.requests: list[httpx.Request] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if not str(request.url).endswith(":generateContent"):
            return httpx.Response(404, json={"error": {"message": "unexpected"}}, request=request)
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {"content": {"parts": [{"text": self.stt_text}]}, "finishReason": "STOP"}
                ]
            },
            request=request,
        )

    @property
    def generate_requests(self) -> list[httpx.Request]:
        return [r for r in self.requests if str(r.url).endswith(":generateContent")]


@pytest.fixture
def stub(monkeypatch):
    """Install a scripted transport under the engine (no key, no network)."""

    def _install(**kwargs: Any) -> _Stub:
        transport = _Stub(**kwargs)
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


def _engine() -> GeminiMediaEngine:
    return GeminiMediaEngine(API_KEY, MODEL, key_env_var="AI_GEMINI_API_KEY")


# ── Fake Telegram surface (mirrors tests/test_media_stt.py) ──


class _FakeMessage:
    def __init__(self, media: Any, *, caption: str = CAPTION, mid: int = MESSAGE_ID,
                 chat_id: int = CHAT) -> None:
        self.media = media
        self.message = caption
        self.text = caption
        self.id = mid
        self.chat_id = chat_id


class _FakeClient:
    def __init__(self, payload: bytes = b"") -> None:
        self.payload = payload
        self.calls: list[dict[str, Any]] = []

    async def download_media(self, message: Any, file: Any = None,
                             progress_callback: Any = None, **kwargs: Any) -> Any:
        self.calls.append({"op": "download_media", "file": file})
        if file:
            with open(file, "wb") as handle:
                handle.write(self.payload)
            return file
        return None

    async def get_messages(self, *args: Any, **kwargs: Any) -> Any:  # pragma: no cover
        raise AssertionError("the media boundary must never search for a message")

    async def iter_messages(self, *args: Any, **kwargs: Any) -> Any:  # pragma: no cover
        raise AssertionError("the media boundary must never scan for a message")


# ── Real OGG/Opus fixture (stdlib only, no dependency) ──


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


def _ogg_opus(duration_s: float = 2.0) -> bytes:
    granule = int(duration_s * 48_000)
    return (
        _ogg_page(0x02, 0, 0, _opus_head())
        + _ogg_page(0x04, granule, 1, b"\x00" * 40)
    )


def _voice_message(payload: bytes) -> _FakeMessage:
    doc = Document(
        id=1, access_hash=1, file_reference=b"", date=None, mime_type="audio/ogg",
        size=len(payload), dc_id=1,
        attributes=[DocumentAttributeAudio(duration=2, voice=True)],
    )
    return _FakeMessage(MessageMediaDocument(document=doc))


@pytest.fixture(autouse=True)
def _reset_engine():
    previous = media_service.get_stt_engine()
    media_service.set_stt_engine(None)
    yield
    media_service.set_stt_engine(previous)


# ── 1. The outbound request's language contract ──


@pytest.mark.asyncio
async def test_the_outbound_stt_request_requires_language_identification_and_original_script(stub):
    transport = stub(stt_text=_PERSIAN)
    _engine().transcribe(_ogg_opus())

    assert len(transport.generate_requests) == 1
    instruction = _instruction(transport.generate_requests[0]).lower()
    # The instruction the engine ACTUALLY transmitted — not merely the module
    # constant — must require the model to commit to a language and its script.
    assert "identify the spoken language" in instruction
    assert "that same language" in instruction
    assert "own writing system" in instruction
    assert "verbatim" in instruction
    # Persian is named, because it is the language the live failure was in.
    assert "persian" in instruction


@pytest.mark.asyncio
async def test_the_outbound_stt_request_forbids_translation_transliteration_and_substitution(stub):
    transport = stub(stt_text=_PERSIAN)
    _engine().transcribe(_ogg_opus())

    instruction = _instruction(transport.generate_requests[0]).lower()
    assert "do not translate" in instruction
    assert "transliterate" in instruction and "romanize" in instruction
    assert "latin letters" in instruction
    assert "never in latin letters" in instruction
    assert "not spoken" in instruction, "another language must not be substituted"
    assert "guess words" in instruction and "different language" in instruction


@pytest.mark.asyncio
async def test_the_transmitted_instruction_is_the_shipped_static_constant(stub):
    transport = stub(stt_text=_PERSIAN)
    _engine().transcribe(_ogg_opus())

    # Static and unfilled: no per-request text, no request data interpolated.
    assert _instruction(transport.generate_requests[0]) == STT_INSTRUCTION
    assert "{" not in STT_INSTRUCTION and "}" not in STT_INSTRUCTION


@pytest.mark.asyncio
async def test_the_stt_instruction_carries_no_telegram_identifier_or_conversation(stub):
    transport = stub(stt_text=_PERSIAN)
    _engine().transcribe(_ogg_opus())

    instruction = _instruction(transport.generate_requests[0])
    for forbidden in (CAPTION, FILE_NAME, str(CHAT), str(MESSAGE_ID)):
        assert forbidden not in instruction, forbidden
    for word in ("telegram", "caption", "filename", "sender", "history", "conversation"):
        assert word not in instruction.lower(), word


# ── 2. Determinism: one transcription, one request ──


@pytest.mark.asyncio
async def test_one_transcription_is_exactly_one_deterministic_request(stub):
    transport = stub(stt_text=_PERSIAN)
    engine = _engine()
    payload = _ogg_opus()

    engine.transcribe(payload)
    engine.transcribe(payload)

    assert len(transport.generate_requests) == 2, "no retry loop: one request per call"
    first, second = transport.generate_requests
    assert _instruction(first) == _instruction(second)
    for request in (first, second):
        config = _body(request)["generationConfig"]
        assert config["temperature"] == 0.0, "recognition is extraction, not generation"
        part = _body(request)["contents"][0]["parts"][-1]
        assert part["inlineData"]["mimeType"] == "audio/ogg"
        assert base64.b64decode(part["inlineData"]["data"]) == payload


def _code_only(module: Any) -> str:
    """The module's executable code, with every string and comment removed.

    Prose must not be able to satisfy — or break — this check, and the
    instruction text that FORBIDS transliteration is prose.
    """
    tokens = tokenize.generate_tokens(io.StringIO(inspect.getsource(module)).readline)
    return " ".join(
        token.string
        for token in tokens
        if token.type not in (tokenize.STRING, tokenize.COMMENT, tokenize.NL)
    ).lower()


def test_the_media_boundary_holds_no_translation_or_transliteration_step():
    """Nothing in the media path may rewrite a transcript's language or script."""
    for module in (gemini_media_engine, media_service):
        code = _code_only(module)
        for banned in ("transliterate", "romanize", "unidecode", "deaccent", "translate"):
            assert banned not in code, f"{module.__name__}: {banned}"


# ── 3. Nothing between the engine and the model rewrites the transcript ──


@pytest.mark.asyncio
async def test_a_returned_persian_transcript_reaches_the_boundary_letter_for_letter(stub):
    transcript = f"{_PERSIAN_ZWNJ}\n{_PERSIAN}"
    stub(stt_text=transcript)
    media_service.set_stt_engine(_engine())
    payload = _ogg_opus()

    analysis = await media_service.analyze_media(
        _FakeClient(payload=payload), OWNER, _voice_message(payload),
    )

    assert analysis.status == MediaStatus.EXTRACTED
    assert analysis.content == transcript, "the boundary must not rewrite the transcript"
    assert analysis.content.count(_ZWNJ) == 1, "ZWNJ must survive normalization"
    # The engine returned Persian script and the analysis still carries it: a
    # Latin-script result can only ever come from the engine, never from here.
    assert _ZWNJ in analysis.as_context_text()


@pytest.mark.asyncio
async def test_the_provider_facing_message_carries_the_transcript_verbatim(stub):
    stub(stt_text=_PERSIAN)
    media_service.set_stt_engine(_engine())
    payload = _ogg_opus()
    analysis = await media_service.analyze_media(
        _FakeClient(payload=payload), OWNER, _voice_message(payload),
    )

    request_text = "این ویس رو stt کن"
    messages = media_ai_service.build_media_messages(request_text, analysis)

    assert [m["role"] for m in messages] == ["system", "user"]
    user_content = messages[1]["content"]
    # Exactly the authored request plus the controlled rendering — no caption,
    # no chat/message id, no filename, no reply or history context.
    assert user_content == f"{request_text}\n\n{analysis.as_context_text()}"
    assert _PERSIAN in user_content
    for forbidden in (CAPTION, FILE_NAME, str(CHAT), str(MESSAGE_ID)):
        assert forbidden not in user_content, forbidden


# ── 4. The hardening is STT-only ──


def test_the_language_contract_is_stt_only_and_ocr_is_untouched():
    ocr = OCR_INSTRUCTION.lower()
    for stt_only in ("persian", "identify the spoken language", "transliterate",
                     "romanize", "latin letters", "own writing system"):
        assert stt_only not in ocr, stt_only
    # OCR keeps its own existing contract, unchanged.
    assert "extract the visible text" in ocr
    assert "do not interpret the image" in ocr
    assert "do not invent" in ocr


# ── 5. Why the instruction, and not a request parameter ──


@pytest.mark.asyncio
async def test_the_transmitted_stt_request_offers_no_language_or_script_parameter(stub):
    """The outbound request really has no language knob — the instruction is it.

    Pins WHY the fix had to be the instruction: the transmitted body is the
    contents (instruction + audio) plus a generationConfig of temperature and
    maxOutputTokens, and every generationConfig key is a sampling/output bound,
    never a language or script selector.
    """
    transport = stub(stt_text=_PERSIAN)
    _engine().transcribe(_ogg_opus())
    request = transport.generate_requests[0]
    body = _body(request)

    assert str(request.url) == f"{GEMINI_API_BASE}/models/{MODEL}:generateContent"
    assert set(body) == {"contents", "generationConfig"}
    assert set(body["generationConfig"]) == {"temperature", "maxOutputTokens"}
    assert all("lang" not in key.lower() for key in body["generationConfig"])
    assert all("script" not in key.lower() for key in body["generationConfig"])
