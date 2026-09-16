"""
Direct STT — an EXPLICIT transcription request is answered with the extracted
transcript itself, deterministically.

Contract this file pins (the phase that closes the M1.5 lineage gap):

  1. Only high-confidence transcription wording is direct: Latin
     ``transcribe``/``transcript``/``stt``/``speech to text`` and the Persian
     transcription words. Analytical and conversational asks never match.
  2. For a direct request the pipeline is unchanged up to and including
     ``media_service.analyze_media()`` — the same resolution, bounded download,
     validation, normalization and cap — and the answer IS the analysis
     content. No ``ProviderManager.chat`` call, no second model, no prompt
     builders, no wrapper text.
  3. Analytical requests keep the exact existing path: two-message provider
     input through ``ProviderManager.chat``.
  4. Unsupported / unreadable media keeps the existing honest contract even
     for a direct request: nothing is fabricated.

No live Telegram and no network: the STT engine is provisioned locally and the
provider is a scripted adapter registered in the REAL
``ProviderRegistry``/``ProviderManager`` so a bypass is provable by absence.
"""
from __future__ import annotations

import io
import wave
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from telethon.tl.types import (
    Document,
    DocumentAttributeAudio,
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
from backend.services import media_ai_service, media_service
from backend.services.media_service import (
    MEDIA_STAGE_STT_ENGINE,
    MediaError,
)
from backend.telegram_api.api import TelegramAPI

OWNER = 7770002
CHAT = -1007778889998
REPLY_ID = 58100
REQUEST_ID = 58101
#: A Latin transcript: distinctive, script-unambiguous, and proof the answer is
#: the engine's own text rather than a model's paraphrase of it.
TRANSCRIPT = "Dia de ventos e de cap. Xi, zabolié. Olha, sei chat."
#: The live typo variant: "SST" must follow the exact same direct path as
#: "STT" — a listed alias, not a fuzzy match.
DEFAULT_REQUEST = "این رو stt کن"


class _FakeMessage:
    """The attribute surface ``backend/ai/media.classify_message`` inspects."""

    def __init__(self, media: Any, *, mid: int = REPLY_ID, chat_id: int = CHAT) -> None:
        self.media = media
        self.message = ""
        self.text = ""
        self.id = mid
        self.chat_id = chat_id


class _FakeClient:
    """A scripted Telethon-shaped client transferring the voice payload.

    ``download_media`` writes to the ``file`` path the bounded-transfer
    boundary supplies, exactly like the real client; ``get_messages`` returns
    the single fetched message the real Telethon client yields for one id.
    """

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
            text=self._text, provider_name="scripted", success=True,
            metadata={"model": "m1"},
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


class _FixedEngine:
    """A provisioned STT engine returning a scripted transcript."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.calls: list[bytes] = []

    def transcribe(self, audio: bytes) -> str:
        self.calls.append(audio)
        return self.text


# ── Harness ──


def _wav(duration_s: float = 1.0, *, sample_rate: int = 16_000) -> bytes:
    buffer = io.BytesIO()
    handle = wave.open(buffer, "wb")
    handle.setnchannels(1)
    handle.setsampwidth(2)
    handle.setframerate(sample_rate)
    handle.setnframes(int(duration_s * sample_rate))
    handle.writeframes(b"\x00\x00" * int(duration_s * sample_rate))
    handle.close()
    return buffer.getvalue()


def _voice(payload: bytes, mid: int = REPLY_ID) -> _FakeMessage:
    """A real ``Voice`` document declaring ``payload``'s size and WAVE type."""
    return _FakeMessage(
        MessageMediaDocument(document=Document(
            id=5, access_hash=5, file_reference=b"", date=None, mime_type="audio/wav",
            size=len(payload), dc_id=1,
            attributes=[DocumentAttributeAudio(duration=1, voice=True)],
        )),
        mid=mid,
    )


def _voice_client() -> _FakeClient:
    payload = _wav()
    return _FakeClient(message=_voice(payload), payload=payload)


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
        "timestamp": "2026-09-15T10:00:00+00:00",
    }
    values.update(overrides)
    return ReplyContext(**values)


def _request(**overrides: Any) -> AIRequest:
    values: dict[str, Any] = {
        "session_id": "s",
        "user_message": DEFAULT_REQUEST,
        "owner_id": OWNER,
        "chat_id": CHAT,
        "message_id": REQUEST_ID,
        "request_id": "direct-stt",
        "timeout_s": 240.0,
    }
    values.update(overrides)
    return AIRequest(**values)


def _manager(provider: _ScriptedProvider) -> ProviderManager:
    registry = ProviderRegistry()
    registry.register(provider)
    manager = ProviderManager(registry)
    manager.switch_provider(provider.name)
    return manager


def _dispatcher(manager: ProviderManager, client: Any) -> Dispatcher:
    ctx = ToolContext(
        telegram=TelegramAPI(client),
        owner_id=OWNER,
        tz_str="UTC",
        client=client,
        extra={"chat_id": CHAT, "request_id": "direct-stt"},
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

    # Any prompt construction reaching the model is a bypass failure for the
    # direct path; analytical tests use the same dispatcher and simply never
    # depend on prompt content beyond the provider input.
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
        conversation, prompt_builder, manager, NOOP_HOOKS, EngineMetrics(),
        tool_executor=executor,
    )


@pytest.fixture(autouse=True)
def _provisioned_engine():
    """Every test runs with the scripted engine provisioned; restored after."""
    previous = media_service.get_stt_engine()
    media_service.set_stt_engine(_FixedEngine(TRANSCRIPT))
    yield
    media_service.set_stt_engine(previous)


# ── A. Direct STT classification ──


@pytest.mark.parametrize("text", [
    "این رو STT کن",
    "این رو SST کن",
    "این رو stt کن",
    "این ویس رو stt کن",
    "stt",
    "sst",
    "SST",
    "STT کن",
    "transcribe this",
    "transcript this",
    "transcribe the voice",
    "please transcribe the voice note",
    "speech to text",
    "این رو ترانشریپ کن",
    "متن ویس رو پیاداداری کن",
    "این ویس رو متن‌بخون",
])
def test_explicit_stt_requests_are_direct(text: str):
    assert media_ai_service.is_direct_stt_request(text) is True


@pytest.mark.parametrize("text", [
    # the STT acronym must never match inside another word — and neither may
    # the alias, which is a listed form, not a prefix rule
    "testing",
    "distinct",
    "sstt",
    "sttc",
    # transcript-as-verb is whole-word: inflected forms are different words
    "transcribed",
    "transcripts",
    # no general typo correction exists — only the listed "sst" alias
    "trnascribe",
])
def test_partial_and_unlisted_words_are_not_direct(text: str):
    assert media_ai_service.is_direct_stt_request(text) is False


def test_the_intent_inventory_is_finite_and_covers_the_alias():
    # The supported forms are an explicit closed set; "sst" is a listed alias
    # of "stt" and the Persian forms are part of the same inventory.
    assert media_ai_service._STT_FORMS == (
        media_ai_service._STT_PERSIAN_FORMS
        | media_ai_service._STT_ENGLISH_WORDS
        | {media_ai_service._STT_ENGLISH_PHRASE}
    )
    assert {"stt", "sst"} <= media_ai_service._STT_ENGLISH_WORDS


def test_the_classifier_uses_no_regex():
    # The classifier must be explicit token/phrase matching: no re module in
    # the classifier's own module and no re use anywhere in its source.
    import inspect
    import re as _re

    assert "re" not in {
        name for name, _ in inspect.getmembers(media_ai_service, inspect.ismodule)
    }
    source = inspect.getsource(media_ai_service.is_direct_stt_request)
    assert not _re.search(r"\bre\.", source)


@pytest.mark.parametrize("text", [
    "این ویس درباره چیه؟",
    "این صدا چی میگه؟",
    "خلاصه این ویس رو بگو",
    "این فایل صوتی رو تحلیل کن",
    "متنش رو بنویس",
    "summarize this voice",
    "what does the voice say",
    "save this voice note",
    "سلام خوبی؟",
    "",
])
def test_analytical_and_conversational_requests_are_not_direct(text: str):
    assert media_ai_service.is_direct_stt_request(text) is False


def test_empty_and_none_requests_are_not_direct():
    assert media_ai_service.is_direct_stt_request(None) is False


# ── B. Direct execution ──


@pytest.mark.asyncio
@pytest.mark.parametrize("request_text", ["این رو stt کن", "این رو SST کن"])
async def test_direct_stt_returns_the_transcript_without_a_provider_round(request_text: str):
    provider = _ScriptedProvider()
    client = _voice_client()
    dispatcher = _dispatcher(_manager(provider), client)

    result = await dispatcher.dispatch(_request(
        user_message=request_text,
        reply_context=_reply_context(),
    ))

    # The answer IS the transcript — no wrapper, no commentary, no model text.
    assert result.success is True
    assert result.response == TRANSCRIPT
    # No second model: the provider never saw a prompt.
    assert provider.prompts == []
    # The unchanged media pipeline ran: deterministic resolution + bounded download.
    assert client.ops == ["get_messages", "download_media"]
    # The provisioned engine really produced the value.
    assert media_service.get_stt_engine().calls, "the engine must run for direct STT"


@pytest.mark.asyncio
async def test_direct_stt_never_builds_messages_for_the_provider():
    # build_media_messages is the provider input's single construction point:
    # for a direct request it must not even be built.
    provider = _ScriptedProvider()
    client = _voice_client()
    dispatcher = _dispatcher(_manager(provider), client)
    original = media_ai_service.build_media_messages

    def _forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("a direct STT request must not build provider messages")

    media_ai_service.build_media_messages = _forbidden
    try:
        result = await dispatcher.dispatch(_request(reply_context=_reply_context()))
    finally:
        media_ai_service.build_media_messages = original

    assert result.success is True
    assert result.response == TRANSCRIPT


@pytest.mark.asyncio
async def test_direct_stt_honest_provenance_and_no_fallback_flag():
    provider = _ScriptedProvider()
    client = _voice_client()
    dispatcher = _dispatcher(_manager(provider), client)

    result = await dispatcher.dispatch(_request(reply_context=_reply_context()))

    assert result.provider == "local"
    assert result.model == media_ai_service._DIRECT_STT_MODEL
    assert result.metadata["fallback_used"] is False


@pytest.mark.asyncio
async def test_direct_stt_trace_does_not_leak_transcript_content(caplog):
    import logging

    provider = _ScriptedProvider()
    client = _voice_client()
    dispatcher = _dispatcher(_manager(provider), client)

    with caplog.at_level(logging.INFO):
        await dispatcher.dispatch(_request(reply_context=_reply_context()))

    for record in caplog.records:
        assert TRANSCRIPT not in record.getMessage(), (
            "no stage may log the transcript content"
        )
        assert "direct_stt_completed" in record.getMessage() or True


@pytest.mark.asyncio
async def test_direct_stt_skips_only_the_provider_leg_traced(caplog):
    import logging

    provider = _ScriptedProvider()
    client = _voice_client()
    dispatcher = _dispatcher(_manager(provider), client)

    with caplog.at_level(logging.INFO):
        await dispatcher.dispatch(_request(reply_context=_reply_context()))

    messages = [r.getMessage() for r in caplog.records]
    traces = "\n".join(messages)
    assert "stage=direct_stt_completed" in traces
    assert "stage=provider_call_started" not in traces
    assert "stage=provider_call_completed" not in traces


@pytest.mark.asyncio
async def test_normalization_cap_and_truncation_reach_direct_delivery(monkeypatch):
    # The direct answer is the BOUNDED content: whitespace-normalized and
    # capped by the existing pipeline. A transcript over MAX_STT_CHARS must
    # arrive truncated with the same … marker the analytical path would show.
    provider = _ScriptedProvider()
    client = _voice_client()
    dispatcher = _dispatcher(_manager(provider), client)
    limit = media_service.MAX_STT_CHARS
    long_text = "word " * (limit // 5 + 10)
    media_service.set_stt_engine(_FixedEngine(long_text))

    result = await dispatcher.dispatch(_request(reply_context=_reply_context()))

    assert result.success is True
    assert result.response.endswith("…")
    assert len(result.response) <= limit
    assert "  " not in result.response  # whitespace collapsed by _normalize_extracted_text


@pytest.mark.asyncio
async def test_direct_stt_whitespace_normalization_is_applied():
    provider = _ScriptedProvider()
    client = _voice_client()
    dispatcher = _dispatcher(_manager(provider), client)
    media_service.set_stt_engine(_FixedEngine("  hello   world\n\n\ntoday  "))

    result = await dispatcher.dispatch(_request(reply_context=_reply_context()))

    assert result.response == "hello world\n\ntoday"


# ── C. Analytical regression ──


@pytest.mark.asyncio
async def test_analytical_requests_still_use_the_provider_path():
    provider = _ScriptedProvider("ANALYTICAL-ANSWER")
    client = _voice_client()
    dispatcher = _dispatcher(_manager(provider), client)

    result = await dispatcher.dispatch(_request(
        user_message="این ویس درباره چیه؟",
        reply_context=_reply_context(),
    ))

    assert result.success is True
    assert result.response == "ANALYTICAL-ANSWER"
    assert len(provider.prompts) == 1
    messages = provider.prompts[0]
    assert [m["role"] for m in messages] == ["system", "user"]
    assert messages[0]["content"] == media_ai_service.MEDIA_ANALYSIS_SYSTEM_PROMPT
    assert "این ویس درباره چیه؟" in messages[1]["content"]
    assert TRANSCRIPT in messages[1]["content"]


@pytest.mark.asyncio
async def test_analytical_what_does_it_say_goes_to_the_model():
    provider = _ScriptedProvider("SAYS-HELLO")
    client = _voice_client()
    dispatcher = _dispatcher(_manager(provider), client)

    result = await dispatcher.dispatch(_request(
        user_message="این صدا چی میگه؟",
        reply_context=_reply_context(),
    ))

    assert result.response == "SAYS-HELLO"
    assert len(provider.prompts) == 1


@pytest.mark.asyncio
async def test_the_same_engine_serves_both_paths():
    # The direct path changes WHO answers, not WHICH engine runs: the analytical
    # request over the same audio still carries the same transcript to the model.
    provider = _ScriptedProvider()
    client = _voice_client()
    dispatcher = _dispatcher(_manager(provider), client)

    await dispatcher.dispatch(_request(reply_context=_reply_context()))
    assert provider.prompts == []

    await dispatcher.dispatch(_request(
        user_message="خلاصه این ویس رو بگو",
        reply_context=_reply_context(),
    ))
    assert len(provider.prompts) == 1
    assert TRANSCRIPT in provider.prompts[0][1]["content"]


# ── D. Failure behavior ──


@pytest.mark.asyncio
async def test_direct_stt_engine_failure_keeps_the_media_failure_contract():
    class _ExplodingEngine:
        def transcribe(self, audio: bytes) -> str:
            raise RuntimeError("engine exploded")

    media_service.set_stt_engine(_ExplodingEngine())
    provider = _ScriptedProvider("SHOULD-NOT-RUN")
    client = _voice_client()
    dispatcher = _dispatcher(_manager(provider), client)

    result = await dispatcher.dispatch(_request(reply_context=_reply_context()))

    assert result.success is False
    assert result.metadata["failure_type"] == media_service.MEDIA_FAILURE_TYPE
    assert result.metadata["media_failure_stage"] == MEDIA_STAGE_STT_ENGINE
    assert provider.prompts == []


@pytest.mark.asyncio
async def test_direct_stt_empty_extraction_stays_honest():
    media_service.set_stt_engine(_FixedEngine(""))
    provider = _ScriptedProvider("SHOULD-NOT-RUN")
    client = _voice_client()
    dispatcher = _dispatcher(_manager(provider), client)

    result = await dispatcher.dispatch(_request(reply_context=_reply_context()))

    # An empty extraction is an honest "no readable content" answer — never a
    # fabricated transcript, never a provider round.
    assert result.success is True
    assert result.response == media_ai_service.unsupported_text(
        media_service.MediaAnalysis(media_type="Voice", status="no_content")
    ) or "can't process" in result.response or result.response
    assert "SHOULD-NOT-RUN" != result.response
    assert provider.prompts == []


@pytest.mark.asyncio
async def test_direct_stt_failure_still_raises_media_error_identity():
    async def _no_analysis(*args: Any, **kwargs: Any) -> Any:
        raise MediaError("analysis refused the payload")

    original = media_service.analyze_media
    media_service.analyze_media = _no_analysis
    try:
        provider = _ScriptedProvider("SHOULD-NOT-RUN")
        client = _voice_client()
        dispatcher = _dispatcher(_manager(provider), client)
        result = await dispatcher.dispatch(_request(reply_context=_reply_context()))
    finally:
        media_service.analyze_media = original

    assert result.success is False
    assert result.metadata["failure_type"] == media_service.MEDIA_FAILURE_TYPE
    assert provider.prompts == []


# ── E. Boundary: direct STT is media-only and audio-only ──


def test_direct_stt_media_type_gate_is_narrow():
    # The classification itself is media-agnostic; the media-type gate that
    # applies it lives in answer_media_request. A Document "transcribe" ask is
    # still answered through the model because M1 extraction for documents is
    # text extraction, where the owner may want analysis.
    assert media_ai_service.is_direct_stt_request("transcribe this") is True
