"""Grok (xAI) speech adapter — a REAL text-to-speech provider for this project.

This file pins the adapter contract:

  1. the documented request — ONE POST to ``https://api.x.ai/v1/tts`` with a
     bearer credential and a body of exactly four documented fields;
  2. ``language`` is REQUIRED by the provider and is sent as its documented
     ``auto`` value: this adapter never guesses the language of the owner's text;
  3. ``output_format`` is the documented object at the provider's documented
     default (MP3, 24 kHz, 128 kbps), written out explicitly so the request cannot
     drift with a provider-side default change — and MP3 is a format Telegram
     accepts for a voice message, so the response is delivered unchanged;
  4. the response body IS the audio; a JSON body on the success path is a
     provider-side anomaly and is refused rather than delivered;
  5. the closed capability set — the provider exposes no model parameter, so the
     registered model is the explicit empty string, and only one of the 28
     documented voices can be built;
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
from typing import Any

import httpx
import pytest

from backend.services import grok_tts_engine as adapter
from backend.services import tts_service
from backend.services.tts_service import FAILURE_CLASSES, TtsError

_HTTPX_ASYNC_CLIENT = httpx.AsyncClient

API_KEY = "xai-tts-adapter-suite-key-must-never-be-logged"
DECOY_KEY = "decoy-must-never-be-read"
SPOKEN = "سلام، این یک آزمون است"
#: A minimal MP3 frame header, so the payload under test is real MPEG audio
#: bytes rather than arbitrary filler.
AUDIO = b"\xff\xfb\x90\x00" + b"\x22" * 128


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
        self.headers = headers if headers is not None else {"content-type": "audio/mpeg"}
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


def _engine(**kwargs: Any) -> adapter.GrokSpeechEngine:
    built, reason = adapter.build_engine(adapter.PROVIDER_MODEL, api_key=API_KEY, **kwargs)
    assert built is not None, reason
    return built


def _payload(request: httpx.Request) -> dict[str, Any]:
    return json.loads(request.content)


# ── 1. The documented request ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_one_post_to_the_documented_tts_endpoint(script):
    recording = script()
    audio = await _engine().speak(SPOKEN, timeout_s=30.0)

    assert audio == AUDIO, "the provider's bytes are delivered unchanged"
    assert len(recording.requests) == 1
    request = recording.requests[0]
    assert request.method == "POST"
    assert str(request.url) == "https://api.x.ai/v1/tts"
    assert request.headers["authorization"] == f"Bearer {API_KEY}"
    assert "application/json" in request.headers["content-type"]


@pytest.mark.asyncio
async def test_the_body_is_the_documented_request_and_nothing_more(script):
    recording = script()
    await _engine().speak(SPOKEN, timeout_s=30.0)

    payload = _payload(recording.requests[0])
    assert set(payload) == {"text", "voice_id", "language", "output_format"}
    assert payload["text"] == SPOKEN
    assert payload["voice_id"] == adapter.DEFAULT_VOICE
    assert payload["language"] == adapter.LANGUAGE == "auto"
    assert payload["output_format"] == {
        "codec": "mp3", "sample_rate": 24000, "bit_rate": 128000,
    }


@pytest.mark.asyncio
async def test_a_registered_voice_is_the_one_sent(script):
    recording = script()
    voice = adapter.VOICE_ORDER[7]
    built, reason = adapter.build_engine(adapter.PROVIDER_MODEL, voice=voice, api_key=API_KEY)
    assert built is not None, reason

    await built.speak(SPOKEN, timeout_s=30.0)

    assert _payload(recording.requests[0])["voice_id"] == voice


@pytest.mark.asyncio
async def test_the_body_can_never_carry_telegram_context(script):
    recording = script()
    await _engine().speak(SPOKEN, timeout_s=30.0)

    body = recording.requests[0].content.decode()
    for forbidden in ("chat_id", "message_id", "sender", "username", "caption",
                      "filename", "reply", "history", "memory"):
        assert forbidden not in body, forbidden


def test_the_adapter_has_no_parameter_that_could_carry_such_context():
    for method in (adapter.GrokSpeechEngine.speak, adapter.build_engine):
        for name in inspect.signature(method).parameters:
            assert name not in {
                "chat_id", "message_id", "sender", "username", "caption",
                "filename", "reply_text", "history", "memory", "owner_id",
            }, f"{method.__name__} exposes {name}"


# ── 2. Output normalization ──────────────────────────────────────────────


def test_the_declared_output_identity_is_the_adapters_own():
    assert (adapter.OUTPUT_FORMAT, adapter.AUDIO_MIME, adapter.AUDIO_FILE_NAME) == (
        "mp3", "audio/mpeg", "speech.mp3",
    )
    assert adapter.OUTPUT_FORMAT_OBJECT["codec"] == adapter.OUTPUT_FORMAT


def test_the_voice_allowlist_is_closed_and_ordered():
    assert set(adapter.VOICE_ORDER) == set(adapter.SUPPORTED_VOICES)
    assert adapter.VOICE_ORDER[0] == adapter.DEFAULT_VOICE == "eve"
    assert len(adapter.VOICE_ORDER) == 28
    assert len(set(adapter.VOICE_ORDER)) == 28


def test_the_provider_has_no_model_parameter_and_none_is_invented():
    assert adapter.SUPPORTED_MODELS == ("",)
    assert adapter.PROVIDER_MODEL == ""
    engine, reason = adapter.build_engine("", api_key=API_KEY)
    assert engine is not None and reason == ""
    assert engine.model == ""


def test_the_provider_token_is_the_one_the_credential_store_keys_on():
    assert adapter.PROVIDER_NAME == "grok"


# ── 3. The response contract ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_json_body_on_the_success_path_is_refused(script):
    script(body=b'{"error":"not audio"}', headers={"content-type": "application/json"})

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


@pytest.mark.asyncio
async def test_a_plain_text_content_type_is_still_delivered(script):
    """The endpoint's content type is not the classifier — the bytes are."""
    script(headers={"content-type": "application/octet-stream"})

    assert await _engine().speak(SPOKEN, timeout_s=30.0) == AUDIO


# ── 4. Failure classification ────────────────────────────────────────────


def _error_body(message: str, code: str = "invalid_request") -> bytes:
    return json.dumps({"error": {"code": code, "message": message}}).encode()


@pytest.mark.parametrize(
    ("status", "body", "expected"),
    [
        (401, _error_body("invalid key", "unauthorized"), tts_service.FAILURE_AUTH),
        (403, _error_body("no access", "forbidden"), tts_service.FAILURE_FORBIDDEN),
        (429, _error_body("slow down", "rate_limit"), tts_service.FAILURE_RATE_LIMIT),
        (404, _error_body("no such endpoint", "not_found"), tts_service.FAILURE_UNSUPPORTED_MODEL),
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
        ("model not supported", tts_service.FAILURE_UNSUPPORTED_MODEL),
        ("unsupported language", tts_service.FAILURE_INVALID_REQUEST),
        ("text is too long", tts_service.FAILURE_INPUT_TOO_LARGE),
        ("whatever else", tts_service.FAILURE_INVALID_REQUEST),
    ],
)
@pytest.mark.asyncio
async def test_a_400_is_narrowed_by_the_providers_own_token(script, message, expected):
    script(status=400, body=_error_body(message), headers={"content-type": "application/json"})

    with pytest.raises(TtsError) as caught:
        await _engine().speak(SPOKEN, timeout_s=30.0)

    assert caught.value.failure_class == expected


@pytest.mark.asyncio
async def test_a_429_that_says_the_quota_is_spent_is_classified_as_such(script):
    script(
        status=429,
        body=_error_body("insufficient credits", "rate_limit"),
        headers={"content-type": "application/json"},
    )

    with pytest.raises(TtsError) as caught:
        await _engine().speak(SPOKEN, timeout_s=30.0)

    assert caught.value.failure_class == tts_service.FAILURE_QUOTA_EXCEEDED


@pytest.mark.asyncio
async def test_a_5xx_is_retryable_and_a_4xx_is_not(script):
    script(status=500, body=b"")
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
    assert inspect.iscoroutinefunction(adapter.GrokSpeechEngine.speak)


def test_this_module_is_a_synthesis_declaration_only():
    for name in ("transcribe", "recognize", "speech_to_text", "realtime"):
        assert not hasattr(adapter.GrokSpeechEngine, name)


# ── 5. Credentials ───────────────────────────────────────────────────────


def test_the_declared_variables_follow_the_projects_existing_convention():
    assert adapter.API_KEY_ENV_VARS == ("AI_XAI_API_KEY", "XAI_API_KEY")


def test_the_first_declared_variable_wins_and_the_rest_are_not_read(monkeypatch):
    monkeypatch.setenv(adapter.API_KEY_ENV_VARS[0], API_KEY)
    monkeypatch.setenv(adapter.API_KEY_ENV_VARS[1], DECOY_KEY)

    assert adapter.resolve_api_key() == (API_KEY, adapter.API_KEY_ENV_VARS[0])


def test_the_public_variable_works_when_the_project_one_is_absent(monkeypatch):
    monkeypatch.delenv(adapter.API_KEY_ENV_VARS[0], raising=False)
    monkeypatch.setenv(adapter.API_KEY_ENV_VARS[1], API_KEY)

    assert adapter.resolve_api_key() == (API_KEY, adapter.API_KEY_ENV_VARS[1])


def test_no_credential_yields_a_bounded_reason_not_an_exception(monkeypatch):
    for name in adapter.API_KEY_ENV_VARS:
        monkeypatch.delenv(name, raising=False)

    engine, reason = adapter.build_engine(adapter.PROVIDER_MODEL)

    assert engine is None
    assert reason == tts_service.FAILURE_MISSING_CREDENTIAL


def test_an_explicit_credential_is_used_without_reading_the_environment(monkeypatch):
    for name in adapter.API_KEY_ENV_VARS:
        monkeypatch.setenv(name, DECOY_KEY)

    engine, reason = adapter.build_engine(adapter.PROVIDER_MODEL, api_key=API_KEY)

    assert reason == ""
    assert engine.key_env_var == "explicit"
    assert engine._api_key == API_KEY


def test_an_invented_model_is_refused_before_any_request():
    for model in ("xai-tts", "grok-tts", "unknown"):
        engine, reason = adapter.build_engine(model, api_key=API_KEY)
        assert engine is None
        assert reason == tts_service.FAILURE_UNSUPPORTED_MODEL


def test_an_unregistered_voice_is_refused_before_any_request():
    engine, reason = adapter.build_engine(
        adapter.PROVIDER_MODEL, voice="not-a-voice", api_key=API_KEY,
    )
    assert engine is None
    assert reason == tts_service.FAILURE_UNSUPPORTED_VOICE


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
        body=_error_body(f"bad key {API_KEY}", "unauthorized"),
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
    assert "GROK_TTS_ENGINE" in caplog.text
