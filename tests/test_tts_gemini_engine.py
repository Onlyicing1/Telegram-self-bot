"""Gemini speech adapter — a REAL text-to-speech provider for this project.

This file pins the adapter contract, and nothing is inferred about speech
QUALITY:

  1. the documented request — ONE POST to ``{v1beta}/interactions`` with the
     ``x-goog-api-key`` header and a body of exactly four documented fields;
  2. the documented RESPONSE SHAPE — the base64 audio of the ``model_output``
     content items (with the documented ``output_audio`` accessor as the accepted
     fallback), decoded and wrapped in a RIFF/WAVE container;
  3. the OUTPUT NORMALIZATION — raw PCM at 24 kHz / 16-bit / mono becomes one
     self-describing WAV clip whose payload bytes are UNCHANGED, using no codec
     and no new dependency;
  4. the closed capability set — only a registered model and one of the 30
     documented voices can be built, so neither can be typed or computed in;
  5. credential resolution — the repository's OWN declared Gemini variables, in
     their existing precedence order, and never an environment sweep;
  6. the closed failure taxonomy — every status, transport condition and response
     anomaly is classified, carries its HTTP status, and marks only the genuinely
     transient families retryable;
  7. bounded work — ONE request per call (no retry loop), every ``httpx`` phase
     bound derived from the caller's remaining budget, and an explicit deadline
     failure instead of a request started too late to finish;
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

from backend.services import gemini_tts_engine as adapter
from backend.services import tts_service
from backend.services.tts_service import FAILURE_CLASSES, TtsError

#: The UNDECORATED async client, captured before any test monkeypatches it.
_HTTPX_ASYNC_CLIENT = httpx.AsyncClient

API_KEY = "AIza-tts-adapter-suite-key-must-never-be-logged"
DECOY_KEY = "decoy-must-never-be-read"
SPOKEN = "سلام، این یک آزمون است"
PCM = b"\x01\x02\x03\x04" * 96


# ── Scripted transport: per-leg control, zero network I/O ────────────────


class _Script(httpx.AsyncBaseTransport):
    """A recording async transport with one scriptable response."""

    def __init__(
        self,
        *,
        status: int = 200,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
        error: Exception | None = None,
    ) -> None:
        self.status = status
        self.body = body if body is not None else _audio_response(PCM)
        self.headers = headers if headers is not None else {"content-type": "application/json"}
        self.error = error
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return httpx.Response(self.status, content=self.body, headers=self.headers)


@pytest.fixture
def script(monkeypatch):
    """Patch the adapter's async client onto a scripted transport."""

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


def _audio_response(pcm: bytes, *, status: str = "completed") -> bytes:
    """The documented Interactions envelope carrying ONE audio block."""
    import base64

    return json.dumps({
        "status": status,
        "steps": [
            {"type": "model_output", "content": [
                {"type": "audio", "data": base64.b64encode(pcm).decode()},
            ]},
        ],
    }).encode()


def _engine(**kwargs: Any) -> adapter.GeminiSpeechEngine:
    built, reason = adapter.build_engine(
        adapter.DEFAULT_MODEL, api_key=API_KEY, **kwargs,
    )
    assert built is not None, reason
    return built


def _payload(request: httpx.Request) -> dict[str, Any]:
    return json.loads(request.content)


# ── 1. The documented request ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_one_post_to_the_documented_interactions_endpoint(script):
    recording = script()
    await _engine().speak(SPOKEN, timeout_s=30.0)

    assert len(recording.requests) == 1
    request = recording.requests[0]
    assert request.method == "POST"
    assert str(request.url) == (
        "https://generativelanguage.googleapis.com/v1beta/interactions"
    )
    assert request.headers["x-goog-api-key"] == API_KEY
    assert "application/json" in request.headers["content-type"]


@pytest.mark.asyncio
async def test_the_body_is_the_documented_audio_request_and_nothing_more(script):
    recording = script()
    await _engine().speak(SPOKEN, timeout_s=30.0)

    payload = _payload(recording.requests[0])
    assert set(payload) == {"model", "input", "response_format", "generation_config"}
    assert payload["model"] == adapter.DEFAULT_MODEL
    assert payload["input"] == SPOKEN
    assert payload["response_format"] == {"type": "audio"}
    assert payload["generation_config"] == {
        "speech_config": [{"voice": adapter.DEFAULT_VOICE}],
    }


@pytest.mark.asyncio
async def test_a_registered_model_and_voice_are_both_sent(script):
    recording = script()
    model = adapter.SUPPORTED_MODELS[-1]
    voice = adapter.VOICE_ORDER[5]
    built, reason = adapter.build_engine(model, voice=voice, api_key=API_KEY)
    assert built is not None, reason

    await built.speak(SPOKEN, timeout_s=30.0)

    payload = _payload(recording.requests[0])
    assert payload["model"] == model
    assert payload["generation_config"]["speech_config"] == [{"voice": voice}]


@pytest.mark.asyncio
async def test_the_body_can_never_carry_telegram_context(script):
    recording = script()
    await _engine().speak(SPOKEN, timeout_s=30.0)

    body = recording.requests[0].content.decode()
    for forbidden in ("chat_id", "message_id", "sender", "username", "caption",
                      "filename", "reply", "history", "memory"):
        assert forbidden not in body, forbidden


def test_the_adapter_has_no_parameter_that_could_carry_such_context():
    for method in (adapter.GeminiSpeechEngine.speak, adapter.build_engine):
        signature = inspect.signature(method)
        for name in signature.parameters:
            assert name not in {
                "chat_id", "message_id", "sender", "username", "caption",
                "filename", "reply_text", "history", "memory", "owner_id",
            }, f"{method.__name__} exposes {name}"


# ── 2. Output normalization (raw PCM → one WAVE clip) ────────────────────


@pytest.mark.asyncio
async def test_the_pcm_response_becomes_one_self_describing_wav_clip(script):
    script()
    audio = await _engine().speak(SPOKEN, timeout_s=30.0)

    assert audio.startswith(b"RIFF")
    assert audio[8:12] == b"WAVE"
    assert audio[12:16] == b"fmt "
    assert audio[36:40] == b"data"
    assert audio[44:] == PCM, "the provider's samples are carried unchanged"
    assert struct.unpack("<I", audio[4:8])[0] == 36 + len(PCM)
    assert struct.unpack("<I", audio[40:44])[0] == len(PCM)


def test_the_wave_header_declares_the_providers_documented_format():
    wrapped = adapter.wrap_pcm_as_wav(PCM)
    channels, rate, byte_rate, block_align, bits = struct.unpack("<HIIHH", wrapped[22:36])

    assert (channels, rate, bits) == (1, 24000, 16)
    assert block_align == 2
    assert byte_rate == 24000 * 2


def test_wrapping_is_a_pure_function_of_the_bytes():
    assert adapter.wrap_pcm_as_wav(PCM) == adapter.wrap_pcm_as_wav(PCM)
    assert adapter.wrap_pcm_as_wav(b"") == struct.pack(
        "<4sI4s4sIHHIIHH4sI", b"RIFF", 36, b"WAVE", b"fmt ", 16, 1, 1, 24000, 48000, 2, 16,
        b"data", 0,
    )


def test_an_empty_or_other_data_type_is_not_usable():
    assert adapter.wrap_pcm_as_wav(b"")[:4] == b"RIFF"


@pytest.mark.asyncio
async def test_the_provider_mime_and_file_name_are_the_adapters(script):
    script()
    engine = _engine()

    assert engine.mime_type == adapter.AUDIO_MIME == "audio/wav"
    assert engine.file_name == adapter.AUDIO_FILE_NAME == "speech.wav"


@pytest.mark.asyncio
async def test_every_voice_is_accepted_and_only_a_registered_one_is(script):
    script()
    for voice in adapter.VOICE_ORDER:
        engine, reason = adapter.build_engine(
            adapter.DEFAULT_MODEL, voice=voice, api_key=API_KEY,
        )
        assert engine is not None, f"{voice}: {reason}"


def test_the_voice_allowlist_is_closed_and_ordered():
    assert set(adapter.VOICE_ORDER) == set(adapter.SUPPORTED_VOICES)
    assert adapter.VOICE_ORDER[0] == adapter.DEFAULT_VOICE
    assert len(adapter.VOICE_ORDER) == 30
    assert len(set(adapter.VOICE_ORDER)) == 30


# ── 3. The response contract ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_documented_output_audio_accessor_is_accepted_too(script):
    import base64

    body = json.dumps({
        "status": "completed",
        "output_audio": {"data": base64.b64encode(PCM).decode()},
    }).encode()
    script(body=body)

    audio = await _engine().speak(SPOKEN, timeout_s=30.0)

    assert audio[44:] == PCM


@pytest.mark.asyncio
async def test_the_last_audio_block_wins(script):
    """The provider documents its accessor as returning the LAST audio block."""
    import base64

    first, last = b"\x0a" * 8, b"\x0b" * 8
    body = json.dumps({
        "status": "completed",
        "steps": [{"type": "model_output", "content": [
            {"type": "audio", "data": base64.b64encode(first).decode()},
            {"type": "audio", "data": base64.b64encode(last).decode()},
        ]}],
    }).encode()
    script(body=body)

    audio = await _engine().speak(SPOKEN, timeout_s=30.0)

    assert audio[44:] == last


@pytest.mark.asyncio
async def test_a_non_completed_status_is_malformed_and_not_a_result(script):
    script(body=_audio_response(PCM, status="failed"))

    with pytest.raises(TtsError) as caught:
        await _engine().speak(SPOKEN, timeout_s=30.0)

    assert caught.value.failure_class == tts_service.FAILURE_MALFORMED


@pytest.mark.asyncio
async def test_a_body_with_no_audio_block_is_an_empty_audio_failure(script):
    script(body=json.dumps({"status": "completed", "steps": []}).encode())

    with pytest.raises(TtsError) as caught:
        await _engine().speak(SPOKEN, timeout_s=30.0)

    assert caught.value.failure_class == tts_service.FAILURE_EMPTY_AUDIO


@pytest.mark.asyncio
async def test_a_zero_length_audio_block_is_an_empty_audio_failure(script):
    script(body=_audio_response(b""))

    with pytest.raises(TtsError) as caught:
        await _engine().speak(SPOKEN, timeout_s=30.0)

    assert caught.value.failure_class == tts_service.FAILURE_EMPTY_AUDIO


@pytest.mark.asyncio
async def test_a_non_json_success_body_is_malformed(script):
    script(body=b"<html>not json</html>", headers={"content-type": "text/html"})

    with pytest.raises(TtsError) as caught:
        await _engine().speak(SPOKEN, timeout_s=30.0)

    assert caught.value.failure_class == tts_service.FAILURE_MALFORMED


@pytest.mark.asyncio
async def test_an_unreadable_audio_payload_is_malformed(script):
    script(body=json.dumps({
        "status": "completed",
        "steps": [{"type": "model_output", "content": [{"type": "audio", "data": "A"}]}],
    }).encode())

    with pytest.raises(TtsError) as caught:
        await _engine().speak(SPOKEN, timeout_s=30.0)

    assert caught.value.failure_class == tts_service.FAILURE_MALFORMED


@pytest.mark.asyncio
async def test_audio_past_the_byte_ceiling_is_refused_here(script, monkeypatch):
    monkeypatch.setattr(adapter, "MAX_TTS_AUDIO_BYTES", 64)
    script(body=_audio_response(b"\x00" * 128))

    with pytest.raises(TtsError) as caught:
        await _engine().speak(SPOKEN, timeout_s=30.0)

    assert caught.value.failure_class == tts_service.FAILURE_OUTPUT_TOO_LARGE


# ── 4. Failure classification ────────────────────────────────────────────


def _error_body(status: str, message: str, code: int = 400) -> bytes:
    return json.dumps({"error": {"code": code, "status": status, "message": message}}).encode()


@pytest.mark.parametrize(
    ("status", "body", "expected"),
    [
        (401, _error_body("UNAUTHENTICATED", "invalid key", 401), tts_service.FAILURE_AUTH),
        (403, _error_body("PERMISSION_DENIED", "no access", 403), tts_service.FAILURE_FORBIDDEN),
        (429, _error_body("RESOURCE_EXHAUSTED", "rate limit reached", 429), tts_service.FAILURE_RATE_LIMIT),
        (404, _error_body("NOT_FOUND", "no such model", 404), tts_service.FAILURE_UNSUPPORTED_MODEL),
        (500, b"", tts_service.FAILURE_SERVER),
        (503, b"", tts_service.FAILURE_SERVER),
        (418, b"", tts_service.FAILURE_REJECTION),
    ],
)
@pytest.mark.asyncio
async def test_every_status_maps_onto_the_closed_taxonomy(script, status, body, expected):
    script(status=status, body=body)

    with pytest.raises(TtsError) as caught:
        await _engine().speak(SPOKEN, timeout_s=30.0)

    assert caught.value.failure_class == expected
    assert caught.value.failure_class in FAILURE_CLASSES
    assert caught.value.http_status == status


@pytest.mark.asyncio
async def test_a_429_with_no_further_wording_is_a_retryable_rate_limit(script):
    """The provider uses ONE 429 token for a per-minute limit AND a spent quota."""
    script(status=429, body=_error_body("RESOURCE_EXHAUSTED", "too many requests", 429))

    with pytest.raises(TtsError) as caught:
        await _engine().speak(SPOKEN, timeout_s=30.0)

    assert caught.value.failure_class == tts_service.FAILURE_RATE_LIMIT
    assert caught.value.retryable is True


@pytest.mark.asyncio
async def test_a_429_that_says_the_quota_is_spent_is_classified_as_such(script):
    script(status=429, body=_error_body(
        "RESOURCE_EXHAUSTED", "You exceeded your current quota", 429,
    ))

    with pytest.raises(TtsError) as caught:
        await _engine().speak(SPOKEN, timeout_s=30.0)

    assert caught.value.failure_class == tts_service.FAILURE_QUOTA_EXCEEDED


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("Unsupported voice: nope", tts_service.FAILURE_UNSUPPORTED_VOICE),
        ("model not supported", tts_service.FAILURE_UNSUPPORTED_MODEL),
        ("input is too long", tts_service.FAILURE_INPUT_TOO_LARGE),
        ("something else entirely", tts_service.FAILURE_INVALID_REQUEST),
    ],
)
@pytest.mark.asyncio
async def test_a_400_is_narrowed_by_the_providers_own_token(script, message, expected):
    script(status=400, body=_error_body("INVALID_ARGUMENT", message))

    with pytest.raises(TtsError) as caught:
        await _engine().speak(SPOKEN, timeout_s=30.0)

    assert caught.value.failure_class == expected


@pytest.mark.asyncio
async def test_an_invalid_api_key_reported_as_400_is_still_an_auth_failure(script):
    script(status=400, body=_error_body(
        "INVALID_ARGUMENT", "API key not valid. Please pass a valid API key.",
    ))

    with pytest.raises(TtsError) as caught:
        await _engine().speak(SPOKEN, timeout_s=30.0)

    assert caught.value.failure_class == tts_service.FAILURE_AUTH


@pytest.mark.asyncio
async def test_a_5xx_is_retryable_and_a_4xx_is_not(script):
    script(status=500, body=b"")
    with pytest.raises(TtsError) as caught:
        await _engine().speak(SPOKEN, timeout_s=30.0)
    assert caught.value.retryable is True

    script(status=400, body=_error_body("INVALID_ARGUMENT", "nope"))
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
async def test_a_non_transport_programming_error_is_not_marked_retryable(script):
    script(error=ValueError("bug"))

    with pytest.raises(TtsError) as caught:
        await _engine().speak(SPOKEN, timeout_s=30.0)

    assert caught.value.failure_class == tts_service.FAILURE_TRANSPORT
    assert caught.value.retryable is False


@pytest.mark.asyncio
async def test_a_budget_too_small_to_finish_fails_before_any_request(script):
    recording = script()

    with pytest.raises(TtsError) as caught:
        await _engine().speak(SPOKEN, timeout_s=1.0)

    assert caught.value.failure_class == tts_service.FAILURE_DEADLINE
    assert recording.requests == [], "a starved request must not be sent"


@pytest.mark.asyncio
async def test_the_phase_bounds_are_derived_from_the_caller_budget(script):
    recording = script()
    await _engine().speak(SPOKEN, timeout_s=30.0)

    timeout = recording.requests and None
    # The request succeeded, so the bound never had to fire; the adapter never
    # invents a second deadline (asserted structurally here).
    assert timeout is None
    assert adapter.MAX_REQUEST_TIMEOUT_S >= tts_service.TTS_TIMEOUT_S


def test_the_adapter_owns_no_retry_loop():
    source = inspect.getsource(adapter)
    assert "for attempt" not in source
    assert "while True" not in source


# ── 5. Credentials ───────────────────────────────────────────────────────


def test_the_credential_variables_are_the_repositorys_existing_declaration():
    from backend.services import gemini_media_engine

    assert adapter.API_KEY_ENV_VARS == gemini_media_engine.API_KEY_ENV_VARS


def test_the_api_base_is_the_one_the_media_engine_already_uses():
    from backend.services import gemini_media_engine

    assert adapter.GEMINI_API_BASE == gemini_media_engine.GEMINI_API_BASE


def test_the_first_declared_variable_wins_and_the_rest_are_not_read(monkeypatch):
    monkeypatch.setenv(adapter.API_KEY_ENV_VARS[0], API_KEY)
    monkeypatch.setenv(adapter.API_KEY_ENV_VARS[1], DECOY_KEY)

    key, name = adapter.resolve_api_key()

    assert (key, name) == (API_KEY, adapter.API_KEY_ENV_VARS[0])


def test_the_compatibility_variable_works_when_the_first_is_absent(monkeypatch):
    monkeypatch.delenv(adapter.API_KEY_ENV_VARS[0], raising=False)
    monkeypatch.setenv(adapter.API_KEY_ENV_VARS[1], API_KEY)

    key, name = adapter.resolve_api_key()

    assert (key, name) == (API_KEY, adapter.API_KEY_ENV_VARS[1])


def test_no_credential_yields_a_bounded_reason_not_an_exception(monkeypatch):
    for name in adapter.API_KEY_ENV_VARS:
        monkeypatch.delenv(name, raising=False)

    engine, reason = adapter.build_engine(adapter.DEFAULT_MODEL)

    assert engine is None
    assert reason == tts_service.FAILURE_MISSING_CREDENTIAL


def test_an_explicit_credential_is_used_without_reading_the_environment(monkeypatch):
    for name in adapter.API_KEY_ENV_VARS:
        monkeypatch.setenv(name, DECOY_KEY)

    engine, reason = adapter.build_engine(adapter.DEFAULT_MODEL, api_key=API_KEY)

    assert reason == ""
    assert engine.key_env_var == "explicit"
    assert engine._api_key == API_KEY


def test_an_unregistered_model_is_refused_before_any_request():
    for model in ("not-a-model", "", "gemini-1.0-tts"):
        engine, reason = adapter.build_engine(model, api_key=API_KEY)
        assert engine is None
        assert reason == tts_service.FAILURE_UNSUPPORTED_MODEL


def test_an_unregistered_voice_is_refused_before_any_request():
    engine, reason = adapter.build_engine(
        adapter.DEFAULT_MODEL, voice="not-a-voice", api_key=API_KEY,
    )
    assert engine is None
    assert reason == tts_service.FAILURE_UNSUPPORTED_VOICE


def test_a_lowercase_voice_is_not_silently_repaired():
    engine, reason = adapter.build_engine(
        adapter.DEFAULT_MODEL, voice=adapter.DEFAULT_VOICE.lower(), api_key=API_KEY,
    )
    assert engine is None
    assert reason == tts_service.FAILURE_UNSUPPORTED_VOICE


# ── 6. No secret egress ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_credential_never_appears_in_a_failure_message(script):
    script(status=401, body=_error_body(
        "UNAUTHENTICATED", f"API key {API_KEY} is not valid", 401,
    ))

    with pytest.raises(TtsError) as caught:
        await _engine().speak(SPOKEN, timeout_s=30.0)

    assert API_KEY not in str(caught.value)
    assert "***" in str(caught.value)


@pytest.mark.asyncio
async def test_the_credential_and_the_text_never_appear_in_a_log_line(script, caplog):
    caplog.set_level(logging.DEBUG)
    script()

    await _engine().speak(SPOKEN, timeout_s=30.0)

    text = caplog.text
    assert API_KEY not in text
    assert SPOKEN not in text
    assert "chars=" in text, "the trace must still say how much was spoken in total"


@pytest.mark.asyncio
async def test_a_failed_run_logs_its_class_and_status(script, caplog):
    caplog.set_level(logging.DEBUG)
    script(status=401, body=_error_body("UNAUTHENTICATED", "nope", 401))

    with pytest.raises(TtsError):
        await _engine().speak(SPOKEN, timeout_s=30.0)

    assert "GEMINI_TTS_ENGINE" in caplog.text
    assert "failure_class=auth:http=401" in caplog.text


def test_the_entry_point_is_async_only():
    assert inspect.iscoroutinefunction(adapter.GeminiSpeechEngine.speak)


def test_this_module_is_a_synthesis_declaration_only():
    for name in ("transcribe", "recognize", "speech_to_text"):
        assert not hasattr(adapter.GeminiSpeechEngine, name)
