"""Text-to-Speech service, tool and surface — Media Processing M3.0.

Covers the whole first TTS path at the behavior level:

  1. the boundary's contract — validation, the bounded input (REFUSED, never
     truncated), the output validation, the normalized clip;
  2. the closed failure taxonomy and its stage tokens;
  3. the deterministic capability decision (fail-closed, no provider health claim);
  4. temporary-resource discipline — NO temporary file exists on this path;
  5. the awaited timeout and the propagation of an already-classified failure, with
     a programming error never converted into a provider failure;
  6. the tool — the AI may REQUEST speech and nothing more: one bounded ``text``
     argument, a destination from trusted runtime context, ONE voice message, a
     result that carries no Telegram identifier back into the conversation;
  7. the read-only Media Analysis surface and its hub row;
  8. capability isolation — synthesis shares no seam with speech recognition and
     cannot touch the provisioned STT engine.

Nothing here claims speech QUALITY, Persian pronunciation, provider health or live
Telegram behavior: the provider is a fake engine and the transport a recording
double, and no byte leaves the process.
"""
from __future__ import annotations

import asyncio
import inspect
import logging
import os
import tempfile
from typing import Any

import pytest

from backend.ai.tools.context import ToolContext
from backend.services import openai_tts_engine
from backend.services import tts_service
from backend.services.tts_service import TtsError

AUDIO = b"OggS\x00\x02\x00\x00\x00\x00\x00\x00\x00\x00OpusHead" + b"\x22" * 64
SPOKEN = "سلام، این یک آزمون است"
CHAT_ID = -1001234567890


# ── Doubles ──────────────────────────────────────────────────────────────


class _FakeEngine:
    """A scripted synthesis engine: records the text, returns or raises."""

    provider = openai_tts_engine.PROVIDER_NAME
    model = openai_tts_engine.SPEECH_MODEL
    voice = openai_tts_engine.DEFAULT_VOICE
    mime_type = openai_tts_engine.AUDIO_MIME
    file_name = openai_tts_engine.AUDIO_FILE_NAME
    key_env_var = "AI_OPENAI_API_KEY"

    def __init__(
        self,
        *,
        audio: bytes = AUDIO,
        error: Exception | None = None,
        delay: float = 0.0,
    ) -> None:
        self.audio = audio
        self.error = error
        self.delay = delay
        self.texts: list[str] = []
        self.budgets: list[float] = []

    async def speak(self, text: str, *, timeout_s: float = 0.0) -> bytes:
        self.texts.append(text)
        self.budgets.append(timeout_s)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error is not None:
            raise self.error
        return self.audio


class _FakeTelegram:
    """A recording Telegram facade: only the bounded voice transfer."""

    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.sent: list[tuple[Any, bytes, str]] = []

    async def send_voice(self, chat_id, audio, mime_type="audio/ogg", timeout=None):
        if self.error is not None:
            raise self.error
        self.sent.append((chat_id, bytes(audio), mime_type))
        return {"id": 1, "chat_id": chat_id}


@pytest.fixture(autouse=True)
def _clean_environment(monkeypatch):
    """No credential leaks between tests, so the capability state is explicit."""
    for name in openai_tts_engine.API_KEY_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv(openai_tts_engine.BASE_URL_ENV_VAR, raising=False)
    yield


def _install(monkeypatch, engine: _FakeEngine | None, reason: str = "") -> None:
    """"Make ``tts_service`` resolve exactly this engine (or this refusal)."""
    monkeypatch.setattr(
        openai_tts_engine, "build_engine",
        lambda *args, **kwargs: (engine, reason),
    )


def _context(telegram: Any, *, chat_id: Any = CHAT_ID, owner: int = 7770001, **extra):
    payload = {"chat_id": chat_id}
    payload.update(extra)
    return ToolContext(telegram=telegram, owner_id=owner, tz_str="UTC", extra=payload)


def _nav(_builder):
    """The shared navigation helper is SYNC; the doubled one must be too."""
    return None


# ── 1. Validation and the input bound ────────────────────────────────────


def test_only_leading_and_trailing_whitespace_is_removed():
    assert tts_service.normalize_request_text("  سلام  ") == "سلام"
    # Interior whitespace is CONTENT: it is never collapsed or rewritten.
    assert tts_service.normalize_request_text("a  b\nc") == "a  b\nc"
    assert tts_service.normalize_request_text(None) == ""
    assert tts_service.normalize_request_text(123) == "123"


def test_empty_and_whitespace_only_input_are_refused():
    for value in ("", "   ", "\n\t ", None):
        with pytest.raises(TtsError) as caught:
            tts_service.validate(value)
        assert caught.value.failure_class == tts_service.FAILURE_EMPTY_INPUT
        assert caught.value.stage == tts_service.TTS_STAGE_VALIDATION


def test_the_input_bound_is_refused_not_truncated():
    oversized = "ا" * (tts_service.MAX_TTS_INPUT_CHARS + 1)

    with pytest.raises(TtsError) as caught:
        tts_service.validate(oversized)

    assert caught.value.failure_class == tts_service.FAILURE_INPUT_TOO_LARGE
    assert caught.value.retryable is False


def test_the_boundary_of_the_input_limit_is_inclusive():
    at_limit = "x" * tts_service.MAX_TTS_INPUT_CHARS

    assert tts_service.validate(at_limit) == at_limit


def test_the_input_bound_is_finite_and_documented():
    assert tts_service.MAX_TTS_INPUT_CHARS == 1000
    assert tts_service.MIN_TTS_INPUT_CHARS == 1
    assert tts_service.TTS_TIMEOUT_S == 60.0
    assert tts_service.MAX_TTS_AUDIO_BYTES == 5 * 1024 * 1024


@pytest.mark.asyncio
async def test_an_oversized_request_never_reaches_the_provider(monkeypatch):
    engine = _FakeEngine()
    _install(monkeypatch, engine)

    with pytest.raises(TtsError):
        await tts_service.synthesize("x" * (tts_service.MAX_TTS_INPUT_CHARS + 1))

    assert engine.texts == []


# ── 2. The capability decision ───────────────────────────────────────────


def test_a_missing_credential_is_the_capability_reason(monkeypatch):
    _install(monkeypatch, None, tts_service.FAILURE_MISSING_CREDENTIAL)

    assert tts_service.capability_reason() == tts_service.FAILURE_MISSING_CREDENTIAL
    assert tts_service.is_configured() is False


def test_a_runnable_provider_is_ready(monkeypatch):
    _install(monkeypatch, _FakeEngine())

    assert tts_service.capability_reason() == ""
    assert tts_service.is_configured() is True


def test_a_broken_capability_probe_is_never_fatal(monkeypatch):
    def _explode(*args, **kwargs):
        raise RuntimeError("probe failed")

    monkeypatch.setattr(openai_tts_engine, "build_engine", _explode)

    assert tts_service.capability_reason() == tts_service.FAILURE_UNAVAILABLE


def test_the_described_capability_is_the_registered_one(monkeypatch):
    _install(monkeypatch, _FakeEngine())

    described = tts_service.describe()

    assert described["provider"] == openai_tts_engine.PROVIDER_NAME
    assert described["model"] == openai_tts_engine.SPEECH_MODEL
    assert described["voice"] == openai_tts_engine.DEFAULT_VOICE
    assert described["mime_type"] == openai_tts_engine.AUDIO_MIME
    assert described["reason"] == ""


@pytest.mark.asyncio
async def test_an_unavailable_provider_is_a_classified_failure(monkeypatch):
    engine = _FakeEngine()
    _install(monkeypatch, None, tts_service.FAILURE_MISSING_CREDENTIAL)

    with pytest.raises(TtsError) as caught:
        await tts_service.synthesize(SPOKEN)

    assert caught.value.failure_class == tts_service.FAILURE_MISSING_CREDENTIAL
    assert caught.value.stage == tts_service.TTS_STAGE_CREDENTIAL
    assert engine.texts == []


# ── 3. The happy path and the clip contract ──────────────────────────────


@pytest.mark.asyncio
async def test_a_successful_synthesis_returns_the_normalized_clip(monkeypatch):
    engine = _FakeEngine()
    _install(monkeypatch, engine)

    clip = await tts_service.synthesize(SPOKEN, timeout_s=30.0)

    assert clip.audio == AUDIO
    assert clip.mime_type == openai_tts_engine.AUDIO_MIME
    assert clip.file_name == openai_tts_engine.AUDIO_FILE_NAME
    assert clip.characters == len(SPOKEN)
    assert clip.provider == openai_tts_engine.PROVIDER_NAME
    assert clip.model == openai_tts_engine.SPEECH_MODEL
    assert clip.voice == openai_tts_engine.DEFAULT_VOICE


@pytest.mark.asyncio
async def test_the_provider_receives_exactly_the_requested_text(monkeypatch):
    engine = _FakeEngine()
    _install(monkeypatch, engine)

    await tts_service.synthesize(f"  {SPOKEN}  ", timeout_s=30.0)

    assert engine.texts == [SPOKEN]


def test_the_clip_carries_no_telegram_metadata():
    fields = set(tts_service.SpeechClip.__dataclass_fields__)

    assert fields == {
        "audio", "mime_type", "file_name", "characters", "provider", "model",
        "voice", "duration_s",
    }


@pytest.mark.asyncio
async def test_the_clip_is_bounded_resident_bytes_never_a_path(monkeypatch):
    _install(monkeypatch, _FakeEngine())

    clip = await tts_service.synthesize(SPOKEN, timeout_s=30.0)

    assert isinstance(clip.audio, bytes)
    assert os.sep not in clip.file_name
    assert "path" not in tts_service.SpeechClip.__dataclass_fields__


# ── 4. Output validation ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_an_empty_response_body_is_a_classified_failure(monkeypatch):
    _install(monkeypatch, _FakeEngine(audio=b""))

    with pytest.raises(TtsError) as caught:
        await tts_service.synthesize(SPOKEN, timeout_s=30.0)

    assert caught.value.failure_class == tts_service.FAILURE_EMPTY_AUDIO
    assert caught.value.stage == tts_service.TTS_STAGE_OUTPUT


@pytest.mark.asyncio
async def test_an_over_sized_response_body_is_a_classified_failure(monkeypatch):
    _install(monkeypatch, _FakeEngine(audio=b"\x00" * (tts_service.MAX_TTS_AUDIO_BYTES + 1)))

    with pytest.raises(TtsError) as caught:
        await tts_service.synthesize(SPOKEN, timeout_s=30.0)

    assert caught.value.failure_class == tts_service.FAILURE_OUTPUT_TOO_LARGE


@pytest.mark.asyncio
async def test_a_non_bytes_response_body_is_a_classified_failure(monkeypatch):
    engine = _FakeEngine()
    engine.audio = None  # type: ignore[assignment]
    _install(monkeypatch, engine)

    with pytest.raises(TtsError) as caught:
        await tts_service.synthesize(SPOKEN, timeout_s=30.0)

    assert caught.value.failure_class == tts_service.FAILURE_EMPTY_AUDIO


# ── 5. Timeout, propagation and programming errors ───────────────────────


@pytest.mark.asyncio
async def test_a_slow_provider_hits_the_awaited_bound(monkeypatch):
    _install(monkeypatch, _FakeEngine(delay=1.0))

    with pytest.raises(TtsError) as caught:
        await tts_service.synthesize(SPOKEN, timeout_s=0.05)

    assert caught.value.failure_class == tts_service.FAILURE_TIMEOUT
    assert caught.value.stage == tts_service.TTS_STAGE_TIMEOUT
    assert caught.value.retryable is True


@pytest.mark.asyncio
async def test_an_already_classified_provider_failure_propagates_unchanged(monkeypatch):
    original = TtsError(
        "provider said no", stage=tts_service.TTS_STAGE_PROVIDER,
        failure_class=tts_service.FAILURE_RATE_LIMIT, http_status=429,
    )
    _install(monkeypatch, _FakeEngine(error=original))

    with pytest.raises(TtsError) as caught:
        await tts_service.synthesize(SPOKEN, timeout_s=30.0)

    assert caught.value is original
    assert caught.value.http_status == 429


@pytest.mark.asyncio
async def test_a_programming_error_is_never_dressed_up_as_a_provider_failure(monkeypatch):
    _install(monkeypatch, _FakeEngine(error=RuntimeError("bug")))

    with pytest.raises(RuntimeError):
        await tts_service.synthesize(SPOKEN, timeout_s=30.0)


@pytest.mark.asyncio
async def test_cancellation_is_re_raised_unchanged(monkeypatch):
    _install(monkeypatch, _FakeEngine(error=asyncio.CancelledError()))

    with pytest.raises(asyncio.CancelledError):
        await tts_service.synthesize(SPOKEN, timeout_s=30.0)


def test_the_caller_budget_is_clamped_to_the_boundary_ceiling():
    assert tts_service.call_timeout(30.0) == 30.0
    assert tts_service.call_timeout(600.0) == tts_service.TTS_TIMEOUT_S
    assert tts_service.call_timeout(0) == tts_service.TTS_TIMEOUT_S
    assert tts_service.call_timeout(None) == tts_service.TTS_TIMEOUT_S
    assert tts_service.call_timeout("nonsense") == tts_service.TTS_TIMEOUT_S


# ── 6. Temporary-resource discipline ─────────────────────────────────────


def test_the_boundary_declares_that_it_creates_no_temporary_file():
    source = inspect.getsource(tts_service)

    for forbidden in ("tempfile", "NamedTemporaryFile", "mkstemp", "mkdtemp", "shutil"):
        assert forbidden not in source
    assert tts_service._EMPTY_TEMP_CONTRACT is True


@pytest.mark.asyncio
async def test_a_synthesis_leaves_no_artifact_behind(monkeypatch, tmp_path):
    _install(monkeypatch, _FakeEngine())
    before = set(os.listdir(tmp_path))
    temp_before = set(os.listdir(tempfile.gettempdir()))

    await tts_service.synthesize(SPOKEN, timeout_s=30.0)

    assert set(os.listdir(tmp_path)) == before
    assert set(os.listdir(tempfile.gettempdir())) == temp_before


@pytest.mark.asyncio
async def test_a_failed_synthesis_leaves_no_artifact_behind(monkeypatch):
    _install(monkeypatch, _FakeEngine(error=TtsError(
        "no", stage=tts_service.TTS_STAGE_PROVIDER,
        failure_class=tts_service.FAILURE_SERVER,
    )))
    temp_before = set(os.listdir(tempfile.gettempdir()))

    with pytest.raises(TtsError):
        await tts_service.synthesize(SPOKEN, timeout_s=30.0)

    assert set(os.listdir(tempfile.gettempdir())) == temp_before


# ── 7. Async / event-loop safety ─────────────────────────────────────────


def test_the_synthesis_path_is_async_and_uses_no_blocking_client():
    assert inspect.iscoroutinefunction(tts_service.synthesize)
    assert inspect.iscoroutinefunction(openai_tts_engine.OpenAiSpeechEngine.speak)
    service_source = inspect.getsource(tts_service)
    adapter_source = inspect.getsource(openai_tts_engine)

    assert "asyncio.to_thread" not in service_source
    assert "httpx" not in service_source
    assert "httpx.Client(" not in adapter_source
    assert "httpx.AsyncClient" in adapter_source


# ── 8. The closed taxonomy and content-free traces ───────────────────────


def test_every_failure_token_belongs_to_the_closed_taxonomy():
    tokens = {
        value for name, value in vars(tts_service).items()
        if name.startswith("FAILURE_") and isinstance(value, str)
    }

    assert tokens
    assert tokens == set(tts_service.FAILURE_CLASSES)
    assert tts_service._TRANSIENT_CLASSES <= tokens


def test_a_failure_carries_its_own_shape():
    error = TtsError(
        "x", stage=tts_service.TTS_STAGE_PROVIDER,
        failure_class=tts_service.FAILURE_SERVER, http_status=503,
    )

    assert (error.stage, error.failure_class, error.http_status) == (
        tts_service.TTS_STAGE_PROVIDER, tts_service.FAILURE_SERVER, 503,
    )
    assert error.retryable is True
    assert TtsError("y", failure_class=tts_service.FAILURE_EMPTY_INPUT).retryable is False


def test_failure_class_of_never_returns_empty():
    assert tts_service.failure_class_of(TtsError("x")) == tts_service.FAILURE_TRANSPORT
    assert tts_service.failure_class_of(
        TtsError("x", failure_class=tts_service.FAILURE_AUTH)
    ) == tts_service.FAILURE_AUTH


@pytest.mark.asyncio
async def test_the_trace_never_carries_the_spoken_text(monkeypatch, caplog):
    _install(monkeypatch, _FakeEngine())

    with caplog.at_level(logging.INFO, logger=tts_service.__name__):
        await tts_service.synthesize(SPOKEN, request_id="req-1", timeout_s=30.0)

    lines = "\n".join(record.getMessage() for record in caplog.records)
    assert "req-1" in lines
    assert tts_service.TTS_STAGE_COMPLETED in lines
    assert f"chars={len(SPOKEN)}" in lines
    assert SPOKEN not in lines


# ── 9. The tool: request, destination and delivery ───────────────────────


def _tool(telegram: Any, **kwargs: Any):
    from backend.ai.tools.speech import SpeakTool

    return SpeakTool(_context(telegram, **kwargs))


async def _speak(telegram: Any, arguments: dict[str, Any], **kwargs: Any):
    """Execute the tool with the context the executor would hand it."""
    from backend.ai.tools.speech import SpeakTool

    context = _context(telegram, **kwargs)
    return await SpeakTool(context).execute(context, arguments)


@pytest.mark.asyncio
async def test_a_request_is_answered_with_exactly_one_voice_message(monkeypatch):
    _install(monkeypatch, _FakeEngine())
    telegram = _FakeTelegram()

    result = await _speak(telegram, {"text": SPOKEN})

    assert result.success is True
    assert telegram.sent == [(CHAT_ID, AUDIO, openai_tts_engine.AUDIO_MIME)]
    assert result.message == f"🔊 Spoke {len(SPOKEN)} characters as a voice message."


@pytest.mark.asyncio
async def test_the_destination_comes_from_trusted_context_not_arguments(monkeypatch):
    _install(monkeypatch, _FakeEngine())
    telegram = _FakeTelegram()

    await _speak(
        telegram,
        {"text": SPOKEN, "chat_id": 12345, "destination": 12345},
        chat_id=-999,
    )

    assert telegram.sent[0][0] == -999


@pytest.mark.asyncio
async def test_the_owner_chat_is_the_fallback_destination(monkeypatch):
    _install(monkeypatch, _FakeEngine())
    telegram = _FakeTelegram()

    await _speak(telegram, {"text": SPOKEN}, chat_id=None, owner=555)

    assert telegram.sent[0][0] == 555


@pytest.mark.asyncio
async def test_an_untrusted_destination_sends_nothing(monkeypatch):
    engine = _FakeEngine()
    _install(monkeypatch, engine)
    telegram = _FakeTelegram()

    result = await _speak(telegram, {"text": SPOKEN}, chat_id=None, owner=0)

    assert result.success is False
    assert telegram.sent == []
    assert engine.texts == []


@pytest.mark.asyncio
async def test_a_missing_text_argument_speaks_nothing(monkeypatch):
    engine = _FakeEngine()
    _install(monkeypatch, engine)
    telegram = _FakeTelegram()

    for arguments in ({}, {"text": ""}, {"text": "   "}, {"text": 5}):
        result = await _speak(telegram, arguments)
        assert result.success is False

    assert telegram.sent == []
    assert engine.texts == []


@pytest.mark.asyncio
async def test_a_missing_transport_speaks_nothing(monkeypatch):
    engine = _FakeEngine()
    _install(monkeypatch, engine)
    from backend.ai.tools.speech import SpeakTool

    context = _context(None)
    result = await SpeakTool(context).execute(context, {"text": SPOKEN})

    assert result.success is False
    assert engine.texts == []


@pytest.mark.asyncio
async def test_an_oversized_request_is_answered_honestly_without_sending(monkeypatch):
    engine = _FakeEngine()
    _install(monkeypatch, engine)
    telegram = _FakeTelegram()

    result = await _speak(telegram, {"text": "x" * (tts_service.MAX_TTS_INPUT_CHARS + 1)})

    assert result.success is False
    assert result.data["failure_class"] == tts_service.FAILURE_INPUT_TOO_LARGE
    assert telegram.sent == []
    assert engine.texts == []


@pytest.mark.asyncio
async def test_an_unconfigured_provider_is_reported_by_class(monkeypatch):
    _install(monkeypatch, None, tts_service.FAILURE_MISSING_CREDENTIAL)
    telegram = _FakeTelegram()

    result = await _speak(telegram, {"text": SPOKEN})

    assert result.success is False
    assert result.data["failure_class"] == tts_service.FAILURE_MISSING_CREDENTIAL
    assert telegram.sent == []


@pytest.mark.asyncio
async def test_a_provider_failure_is_reported_with_its_bounded_class(monkeypatch):
    _install(monkeypatch, _FakeEngine(error=TtsError(
        "provider unavailable", stage=tts_service.TTS_STAGE_PROVIDER,
        failure_class=tts_service.FAILURE_SERVER, http_status=503,
    )))
    telegram = _FakeTelegram()

    result = await _speak(telegram, {"text": SPOKEN})

    assert result.success is False
    assert result.data["failure_class"] == tts_service.FAILURE_SERVER
    assert result.data["stage"] == tts_service.TTS_STAGE_PROVIDER
    assert telegram.sent == []


@pytest.mark.asyncio
async def test_a_delivery_failure_is_reported_and_not_swallowed(monkeypatch):
    _install(monkeypatch, _FakeEngine())
    telegram = _FakeTelegram(error=RuntimeError("telegram refused"))

    result = await _speak(telegram, {"text": SPOKEN})

    assert result.success is False
    assert result.data["error_class"] == "RuntimeError"


@pytest.mark.asyncio
async def test_the_result_carries_no_telegram_identifier(monkeypatch):
    _install(monkeypatch, _FakeEngine())

    result = await _speak(_FakeTelegram(), {"text": SPOKEN})

    assert set(result.data) == {"characters", "mime_type", "voice", "model"}
    assert str(CHAT_ID) not in str(result.data)
    assert str(CHAT_ID) not in result.message


@pytest.mark.asyncio
async def test_the_provider_receives_no_telegram_context_through_the_tool(monkeypatch):
    """The service is called with the text and a budget — and nothing else."""
    observed: dict[str, Any] = {}
    real = tts_service.synthesize

    async def _spy(text, **kwargs):
        observed["args"] = (text,)
        observed["kwargs"] = kwargs
        return await real(text, **kwargs)

    monkeypatch.setattr(tts_service, "synthesize", _spy)
    _install(monkeypatch, _FakeEngine())

    await _speak(_FakeTelegram(), {"text": SPOKEN})

    assert observed["args"] == (SPOKEN,)
    assert set(observed["kwargs"]) == {"request_id", "timeout_s"}


@pytest.mark.asyncio
async def test_the_synthesis_budget_is_the_requests_own_envelope(monkeypatch):
    engine = _FakeEngine()
    _install(monkeypatch, engine)

    await _speak(_FakeTelegram(), {"text": SPOKEN}, request_timeout_s=12.5)

    assert engine.budgets == [12.5]


@pytest.mark.asyncio
async def test_the_synthesis_budget_never_exceeds_the_boundary_ceiling(monkeypatch):
    engine = _FakeEngine()
    _install(monkeypatch, engine)

    await _speak(_FakeTelegram(), {"text": SPOKEN}, request_timeout_s=999)

    assert engine.budgets == [tts_service.TTS_TIMEOUT_S]


def test_the_tool_metadata_is_the_documented_contract():
    from backend.ai.tools.base import PermissionLevel
    from backend.ai.tools.speech import SpeakTool

    tool = SpeakTool(_context(_FakeTelegram()))

    assert tool.name == "text_to_speech"
    assert tool.required_arguments == ("text",)
    assert tool.permission_level == PermissionLevel.READ_WRITE
    assert tool.safe is True
    assert tool.long_running is True
    assert tool.requires_reply_context is False
    assert set(tool.parameters) == {"text"}
    assert tool.parameters["text"]["maxLength"] == tts_service.MAX_TTS_INPUT_CHARS
    assert tool.parameters["text"]["minLength"] == 1


def test_the_tool_is_registered_in_the_one_registry(owner_id):
    from backend.ai.tools.registry import create_default_registry

    registry = create_default_registry(
        ToolContext(telegram=None, owner_id=owner_id, tz_str="UTC")
    )

    tool = registry.get("text_to_speech")
    assert tool is not None
    assert registry.has("text_to_speech")
    schemas = [schema for schema in registry.list_schemas() if schema["name"] == "text_to_speech"]
    assert len(schemas) == 1
    assert "text" in schemas[0]["parameters"]


# ── 10. Capability isolation ─────────────────────────────────────────────


def test_the_tts_path_shares_no_seam_with_speech_recognition():
    for module in (tts_service, openai_tts_engine):
        source = inspect.getsource(module)
        for forbidden in (
            "stt_fallback", "stt_credential_pool", "stt_control_plane",
            "stt_engine_factory", "stt_chunking", "stt_consensus",
            "stt_provider_probe", "gemini_media_engine", "groq_stt_engine",
            "speechmatics_stt_engine",
        ):
            assert forbidden not in source, f"{module.__name__} references {forbidden}"
    assert not hasattr(tts_service, "transcribe")
    assert not hasattr(openai_tts_engine.OpenAiSpeechEngine, "transcribe")


@pytest.mark.asyncio
async def test_a_synthesis_never_touches_the_provisioned_stt_engine(monkeypatch):
    from backend.services import media_service

    calls: list[Any] = []
    monkeypatch.setattr(media_service, "set_stt_engine", lambda engine: calls.append(engine))
    _install(monkeypatch, _FakeEngine())

    await tts_service.synthesize(SPOKEN, timeout_s=30.0)
    await _speak(_FakeTelegram(), {"text": SPOKEN})

    assert calls == []


# ── 11. The read-only Media Analysis surface ─────────────────────────────


def test_the_surface_registers_under_media_analysis(monkeypatch):
    from backend.bot.handlers import ai_tts_settings as module

    panels: list[tuple[str, str]] = []
    monkeypatch.setattr(
        module, "register_panel",
        lambda panel_id, handler, parent="menu", title="": panels.append((panel_id, parent)),
    )
    monkeypatch.setattr(module, "register_inline_builder", lambda *a, **k: None)

    module.register(None, 1)

    assert ("ai_media_tts", "ai_media") in panels


@pytest.mark.asyncio
async def test_the_surface_reports_the_registered_capability(monkeypatch):
    from backend.bot.handlers import ai as ai_module
    from backend.bot.handlers import ai_tts_settings as module

    monkeypatch.setattr(ai_module, "_nav_buttons", _nav)
    _install(monkeypatch, _FakeEngine())

    title, body, _buttons = await module._ai_media_tts_panel_handler(None, "")

    assert title == "Text-to-Speech"
    assert openai_tts_engine.PROVIDER_NAME in body
    assert openai_tts_engine.SPEECH_MODEL in body
    assert openai_tts_engine.DEFAULT_VOICE in body
    assert str(tts_service.MAX_TTS_INPUT_CHARS) in body


@pytest.mark.asyncio
async def test_the_surface_says_so_plainly_when_nothing_can_run(monkeypatch):
    from backend.bot.handlers import ai as ai_module
    from backend.bot.handlers import ai_tts_settings as module

    monkeypatch.setattr(ai_module, "_nav_buttons", _nav)
    _install(monkeypatch, None, tts_service.FAILURE_MISSING_CREDENTIAL)

    _title, body, _buttons = await module._ai_media_tts_panel_handler(None, "")

    assert "No credential on this runtime" in body
    assert "Nothing is sent" in body


@pytest.mark.asyncio
async def test_the_surface_offers_no_control_that_does_not_exist(monkeypatch):
    from backend.bot.handlers import ai as ai_module
    from backend.bot.handlers import ai_tts_settings as module

    monkeypatch.setattr(ai_module, "_nav_buttons", _nav)
    _install(monkeypatch, _FakeEngine())

    _title, body, buttons = await module._ai_media_tts_panel_handler(None, "")

    assert "No owner controls" in body
    # Every button is navigation: this phase registers no action and no input.
    assert all(
        getattr(getattr(button, "data", b""), "startswith", lambda _p: False)("panel:")
        or getattr(button, "data", b"") == b""
        for row in buttons
        for button in (row if isinstance(row, list) else [row])
    )


def test_the_status_line_is_one_line_and_never_raises(monkeypatch):
    from backend.bot.handlers import ai_tts_settings as module

    _install(monkeypatch, _FakeEngine())
    assert module.tts_status_line().startswith("Text-to-Speech · ")

    def _explode(*args, **kwargs):
        raise RuntimeError("probe failed")

    monkeypatch.setattr(tts_service, "describe", _explode)
    assert module.tts_status_line() == "Text-to-Speech · Unavailable on this runtime"


@pytest.mark.asyncio
async def test_the_media_analysis_hub_lists_text_to_speech(monkeypatch):
    from backend.bot.handlers import ai as ai_module
    from backend.bot.handlers import ai_stt_settings as hub
    from backend.bot.handlers import ai_tts_settings as module

    async def _config():
        return 1, {}

    monkeypatch.setattr(ai_module, "_nav_buttons", _nav)
    monkeypatch.setattr(hub, "_saved_config", _config)
    _install(monkeypatch, _FakeEngine())

    title, body, buttons = await hub._ai_media_panel_handler(None, "")

    assert title == "Media Analysis"
    assert module.tts_status_line() in body
    datas = []
    for row in buttons:
        for button in (row if isinstance(row, list) else [row]):
            data = getattr(button, "data", b"")
            datas.append(data.decode() if isinstance(data, bytes) else str(data))
    assert "panel:ai_media_tts" in datas


def test_the_module_documents_the_media_analysis_tree():
    from backend.bot.handlers import ai_tts_settings as module

    assert "Media Analysis" in module.__doc__
