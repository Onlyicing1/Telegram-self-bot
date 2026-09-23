"""Speechmatics speech adapter — a REAL text-to-speech provider for this project.

The previous phase registered this provider as a documented DEFERRAL, on the
reasoning that its WAV/PCM-only output could not be delivered. That reasoning is
corrected here: the documented default output is a COMPLETE WAV file with
headers (16 kHz, 16-bit signed, mono), which is one self-describing audio file
this project delivers unchanged — no transcode, no new dependency. This file
pins the resulting contract:

  1. the documented request — ONE POST to
     ``{preview}/generate/{voice}?output_format=wav_16000`` with a bearer
     credential and a ``{"text": …}`` body of exactly one field;
  2. the voice travels in the URL PATH, so an unregistered voice can never reach
     the service — the allowlist is closed before the request is built;
  3. the response body IS the file; a JSON body on the success path is a
     provider-side anomaly and is refused rather than delivered;
  4. the provider exposes no model parameter, so the registered model is the
     explicit empty string;
  5. the credential is the SAME declaration the Speechmatics transcription
     adapter uses, so one key serves both directions;
  6. the closed failure taxonomy, with the transient families marked retryable;
  7. bounded work — ONE request per call, phase bounds derived from the caller's
     remaining budget;
  8. zero egress of the credential and of Telegram context.

The HTTP boundary is a scripted ``httpx`` async transport: no test needs a
credential and no byte leaves the process. Live provider verification was NOT
performed.
"""
from __future__ import annotations

import inspect
import json
import logging
import struct
from typing import Any

import httpx
import pytest

from backend.services import speechmatics_tts_engine as adapter
from backend.services import tts_service
from backend.services.tts_service import FAILURE_CLASSES, TtsError

_HTTPX_ASYNC_CLIENT = httpx.AsyncClient

API_KEY = "sm-tts-adapter-suite-key-must-never-be-logged"
DECOY_KEY = "decoy-must-never-be-read"
SPOKEN = "Hello, this is a synthesis test."


def _wav(payload: bytes = b"\x11" * 64) -> bytes:
    """A complete 16 kHz / 16-bit / mono WAV file, as the service documents."""
    return struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF", 36 + len(payload), b"WAVE", b"fmt ", 16, 1, 1, 16000, 32000, 2, 16,
        b"data", len(payload),
    ) + payload


AUDIO = _wav()


class _Script(httpx.AsyncBaseTransport):
    def __init__(
        self,
        *,
        status: int = 200,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
        error: Exception | None = None,
    ) -> None:
        self.status = status
        self.body = AUDIO if body is None else body
        self.headers = headers if headers is not None else {"content-type": "audio/wav"}
        self.error = error
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return httpx.Response(self.status, content=self.body, headers=self.headers)


@pytest.fixture
def script(monkeypatch):
    def _install(**kwargs: Any) -> _Script:
        recording = _Script(**kwargs)
        monkeypatch.setattr(
            adapter.httpx,
            "AsyncClient",
            lambda timeout=None, **rest: _HTTPX_ASYNC_CLIENT(
                transport=recording, timeout=timeout,
            ),
        )
        return recording

    return _install


def _engine(**kwargs: Any) -> adapter.SpeechmaticsSpeechEngine:
    built, reason = adapter.build_engine(adapter.PROVIDER_MODEL, api_key=API_KEY, **kwargs)
    assert built is not None, reason
    return built


# ── 1. The documented request ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_one_post_to_the_documented_generate_endpoint(script):
    recording = script()
    audio = await _engine().speak(SPOKEN, timeout_s=30.0)

    assert audio == AUDIO, "the provider's complete WAV file is delivered unchanged"
    assert len(recording.requests) == 1
    request = recording.requests[0]
    assert request.method == "POST"
    assert str(request.url) == (
        "https://preview.tts.speechmatics.com/generate/sarah?output_format=wav_16000"
    )
    assert request.headers["authorization"] == f"Bearer {API_KEY}"
    assert "application/json" in request.headers["content-type"]


@pytest.mark.asyncio
async def test_the_body_is_the_documented_request_and_nothing_more(script):
    recording = script()
    await _engine().speak(SPOKEN, timeout_s=30.0)

    payload = json.loads(recording.requests[0].content)
    assert set(payload) == {"text"}
    assert payload["text"] == SPOKEN


@pytest.mark.asyncio
async def test_the_voice_is_in_the_path_and_the_format_in_the_query(script):
    recording = script()
    voice = adapter.VOICE_ORDER[-1]
    built, reason = adapter.build_engine(adapter.PROVIDER_MODEL, voice=voice, api_key=API_KEY)
    assert built is not None, reason

    await built.speak(SPOKEN, timeout_s=30.0)

    url = str(recording.requests[0].url)
    assert url.endswith(f"/generate/{voice}?output_format={adapter.OUTPUT_FORMAT}")


@pytest.mark.asyncio
async def test_the_body_can_never_carry_telegram_context(script):
    recording = script()
    await _engine().speak(SPOKEN, timeout_s=30.0)

    body = recording.requests[0].content.decode()
    for forbidden in ("chat_id", "message_id", "sender", "username", "caption",
                      "filename", "reply", "history", "memory"):
        assert forbidden not in body, forbidden


def test_the_adapter_has_no_parameter_that_could_carry_such_context():
    for method in (adapter.SpeechmaticsSpeechEngine.speak, adapter.build_engine):
        for name in inspect.signature(method).parameters:
            assert name not in {
                "chat_id", "message_id", "sender", "username", "caption",
                "filename", "reply_text", "history", "memory", "owner_id",
            }, f"{method.__name__} exposes {name}"


# ── 2. Output normalization ──────────────────────────────────────────────


def test_the_declared_output_identity_is_the_adapters_own():
    assert (adapter.OUTPUT_FORMAT, adapter.AUDIO_MIME, adapter.AUDIO_FILE_NAME) == (
        "wav_16000", "audio/wav", "speech.wav",
    )


def test_the_delivered_bytes_are_a_real_container_telegram_can_carry():
    assert AUDIO[:4] == b"RIFF" and AUDIO[8:12] == b"WAVE"
    channels, rate, _, _, bits = struct.unpack("<HIIHH", AUDIO[22:36])
    assert (channels, rate, bits) == (1, 16000, 16)


def test_the_voice_allowlist_is_closed_and_ordered():
    assert set(adapter.VOICE_ORDER) == set(adapter.SUPPORTED_VOICES)
    assert adapter.VOICE_ORDER[0] == adapter.DEFAULT_VOICE == "sarah"
    assert adapter.VOICE_ORDER == ("sarah", "theo", "megan", "jack")


def test_the_provider_has_no_model_parameter_and_none_is_invented():
    assert adapter.SUPPORTED_MODELS == ("",)
    assert adapter.PROVIDER_MODEL == ""
    engine, reason = adapter.build_engine("", api_key=API_KEY)
    assert engine is not None and reason == ""
    assert engine.model == ""


def test_the_provider_token_is_shared_with_the_transcription_adapter():
    """One Speechmatics credential serves BOTH directions of this provider."""
    from backend.services import speechmatics_stt_engine

    assert adapter.PROVIDER_NAME == speechmatics_stt_engine.PROVIDER_NAME == "speechmatics"


# ── 3. The response contract ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_json_body_on_the_success_path_is_refused(script):
    script(body=b'{"audio":"nope"}', headers={"content-type": "application/json"})

    with pytest.raises(TtsError) as caught:
        await _engine().speak(SPOKEN, timeout_s=30.0)

    assert caught.value.failure_class == tts_service.FAILURE_MALFORMED


@pytest.mark.asyncio
async def test_an_empty_body_is_never_treated_as_silent_speech(script):
    script(body=b"")

    with pytest.raises(TtsError) as caught:
        await _engine().speak(SPOKEN, timeout_s=30.0)

    assert caught.value.failure_class == tts_service.FAILURE_EMPTY_AUDIO


@pytest.mark.asyncio
async def test_audio_past_the_byte_ceiling_is_refused_here(script, monkeypatch):
    monkeypatch.setattr(adapter, "MAX_TTS_AUDIO_BYTES", 64)
    script(body=b"\x00" * 128)

    with pytest.raises(TtsError) as caught:
        await _engine().speak(SPOKEN, timeout_s=30.0)

    assert caught.value.failure_class == tts_service.FAILURE_OUTPUT_TOO_LARGE


# ── 4. Failure classification ────────────────────────────────────────────


def _error_body(message: str) -> bytes:
    return json.dumps({"message": message}).encode()


@pytest.mark.parametrize(
    ("status", "body", "expected"),
    [
        (401, _error_body("invalid key"), tts_service.FAILURE_AUTH),
        (403, _error_body("no access"), tts_service.FAILURE_FORBIDDEN),
        (429, _error_body("slow down"), tts_service.FAILURE_RATE_LIMIT),
        (404, _error_body("no such voice"), tts_service.FAILURE_UNSUPPORTED_VOICE),
        (500, b"", tts_service.FAILURE_SERVER),
        (503, b"", tts_service.FAILURE_SERVER),
        (418, b"", tts_service.FAILURE_REJECTION),
    ],
)
@pytest.mark.asyncio
async def test_every_status_maps_onto_the_closed_taxonomy(script, status, body, expected):
    script(status=status, body=body, headers={"content-type": "application/json"})

    with pytest.raises(TtsError) as caught:
        await _engine().speak(SPOKEN, timeout_s=30.0)

    assert caught.value.failure_class == expected
    assert caught.value.failure_class in FAILURE_CLASSES
    assert caught.value.http_status == status


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("voice not found", tts_service.FAILURE_UNSUPPORTED_VOICE),
        ("invalid output_format", tts_service.FAILURE_INVALID_REQUEST),
        ("text too long", tts_service.FAILURE_INPUT_TOO_LARGE),
        ("whatever else", tts_service.FAILURE_INVALID_REQUEST),
    ],
)
@pytest.mark.asyncio
async def test_a_400_is_narrowed_by_the_providers_own_wording(script, message, expected):
    script(status=400, body=_error_body(message), headers={"content-type": "application/json"})

    with pytest.raises(TtsError) as caught:
        await _engine().speak(SPOKEN, timeout_s=30.0)

    assert caught.value.failure_class == expected


@pytest.mark.asyncio
async def test_a_429_that_says_the_quota_is_spent_is_classified_as_such(script):
    script(
        status=429,
        body=_error_body("monthly quota exhausted"),
        headers={"content-type": "application/json"},
    )

    with pytest.raises(TtsError) as caught:
        await _engine().speak(SPOKEN, timeout_s=30.0)

    assert caught.value.failure_class == tts_service.FAILURE_QUOTA_EXCEEDED


@pytest.mark.asyncio
async def test_a_5xx_is_retryable_and_a_4xx_is_not(script):
    script(status=502, body=b"")
    with pytest.raises(TtsError) as caught:
        await _engine().speak(SPOKEN, timeout_s=30.0)
    assert caught.value.retryable is True

    script(status=400, body=_error_body("nope"), headers={"content-type": "application/json"})
    with pytest.raises(TtsError) as caught:
        await _engine().speak(SPOKEN, timeout_s=30.0)
    assert caught.value.retryable is False


@pytest.mark.asyncio
async def test_a_timeout_is_classified_and_marked_retryable(script):
    script(error=httpx.ReadTimeout("stalled"))

    with pytest.raises(TtsError) as caught:
        await _engine().speak(SPOKEN, timeout_s=30.0)

    assert caught.value.failure_class == tts_service.FAILURE_TIMEOUT
    assert caught.value.retryable is True


@pytest.mark.asyncio
async def test_a_connect_failure_is_a_retryable_transport_failure(script):
    script(error=httpx.ConnectError("no route"))

    with pytest.raises(TtsError) as caught:
        await _engine().speak(SPOKEN, timeout_s=30.0)

    assert caught.value.failure_class == tts_service.FAILURE_TRANSPORT
    assert caught.value.retryable is True


@pytest.mark.asyncio
async def test_a_budget_too_small_to_finish_fails_before_any_request(script):
    recording = script()

    with pytest.raises(TtsError) as caught:
        await _engine().speak(SPOKEN, timeout_s=1.0)

    assert caught.value.failure_class == tts_service.FAILURE_DEADLINE
    assert recording.requests == [], "a starved request must not be sent"


def test_the_adapter_owns_no_retry_loop():
    source = inspect.getsource(adapter)
    assert "for attempt" not in source
    assert "while True" not in source


def test_the_entry_point_is_async_only():
    assert inspect.iscoroutinefunction(adapter.SpeechmaticsSpeechEngine.speak)


def test_this_module_is_a_synthesis_declaration_only():
    for name in ("transcribe", "recognize", "speech_to_text", "batch"):
        assert not hasattr(adapter.SpeechmaticsSpeechEngine, name)


# ── 5. Credentials ───────────────────────────────────────────────────────


def test_the_credential_declaration_is_the_transcription_adapters():
    from backend.services import speechmatics_stt_engine

    assert adapter.API_KEY_ENV_VARS == speechmatics_stt_engine.API_KEY_ENV_VARS


def test_the_declared_variable_is_read_and_a_missing_one_is_bounded(monkeypatch):
    monkeypatch.setenv(adapter.API_KEY_ENV_VARS[0], API_KEY)
    assert adapter.resolve_api_key() == (API_KEY, adapter.API_KEY_ENV_VARS[0])

    monkeypatch.delenv(adapter.API_KEY_ENV_VARS[0], raising=False)
    engine, reason = adapter.build_engine(adapter.PROVIDER_MODEL)
    assert engine is None
    assert reason == tts_service.FAILURE_MISSING_CREDENTIAL


def test_an_explicit_credential_is_used_without_reading_the_environment(monkeypatch):
    monkeypatch.setenv(adapter.API_KEY_ENV_VARS[0], DECOY_KEY)

    engine, reason = adapter.build_engine(adapter.PROVIDER_MODEL, api_key=API_KEY)

    assert reason == ""
    assert engine.key_env_var == "explicit"
    assert engine._api_key == API_KEY


def test_an_invented_model_is_refused_before_any_request():
    for model in ("standard", "enhanced", "unknown"):
        engine, reason = adapter.build_engine(model, api_key=API_KEY)
        assert engine is None
        assert reason == tts_service.FAILURE_UNSUPPORTED_MODEL


def test_an_unregistered_voice_is_refused_before_any_request(script):
    recording = script()

    engine, reason = adapter.build_engine(
        adapter.PROVIDER_MODEL, voice="not-a-voice", api_key=API_KEY,
    )

    assert engine is None
    assert reason == tts_service.FAILURE_UNSUPPORTED_VOICE
    assert recording.requests == [], "an unregistered voice must never reach the path"


def test_every_documented_voice_is_accepted():
    for voice in adapter.VOICE_ORDER:
        engine, reason = adapter.build_engine(
            adapter.PROVIDER_MODEL, voice=voice, api_key=API_KEY,
        )
        assert engine is not None, f"{voice}: {reason}"


# ── 6. No secret egress ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_credential_never_appears_in_a_failure_message(script):
    script(
        status=401,
        body=_error_body(f"bad key {API_KEY}"),
        headers={"content-type": "application/json"},
    )

    with pytest.raises(TtsError) as caught:
        await _engine().speak(SPOKEN, timeout_s=30.0)

    assert API_KEY not in str(caught.value)
    assert "***" in str(caught.value)


@pytest.mark.asyncio
async def test_the_credential_and_the_text_never_appear_in_a_log_line(script, caplog):
    caplog.set_level(logging.DEBUG)
    script()

    await _engine().speak(SPOKEN, timeout_s=30.0)

    assert API_KEY not in caplog.text
    assert SPOKEN not in caplog.text
    assert "SPEECHMATICS_TTS_ENGINE" in caplog.text
