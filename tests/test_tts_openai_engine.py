"""OpenAI speech adapter — the ONE Text-to-Speech provider of M3.0.

This file pins the adapter contract:

  1. the documented request — ONE POST to ``{base}/audio/speech`` with a bearer
     credential, a JSON body of exactly four fields, and the audio itself as the
     accepted response body;
  2. the closed capability set — only the registered model and one of the
     documented voices can be built, so neither can be typed or computed in;
  3. credential resolution — the repository's OWN declared OpenAI variables, in
     their existing precedence order, and never an environment sweep;
  4. the closed failure taxonomy — every status, transport condition and response
     anomaly is classified, carries its HTTP status, and marks only the genuinely
     transient families retryable;
  5. bounded work — ONE request per call (no retry loop), every ``httpx`` phase
     bound derived from the caller's remaining budget, and an explicit deadline
     failure instead of a request that was started too late to finish;
  6. zero egress of the credential and of Telegram context — the key never
     appears in an error or a log line, and no chat/message/sender field exists
     on the request at all.

The HTTP boundary is a scripted ``httpx`` async transport: no test needs a
credential and no byte leaves the process. Nothing here claims speech QUALITY or
provider reachability — only the contract.
"""
from __future__ import annotations

import inspect
import json
import logging
from typing import Any

import httpx
import pytest

from backend.services import openai_tts_engine as adapter
from backend.services import tts_service
from backend.services.tts_service import FAILURE_CLASSES, TtsError

#: The UNDECORATED async client, captured before any test monkeypatches it —
#: re-reading ``httpx.AsyncClient`` inside ``script()`` would pick up a previous
#: test's own patched factory and silently reuse ITS transport.
_HTTPX_ASYNC_CLIENT = httpx.AsyncClient

API_KEY = "sk-tts-adapter-suite-key-must-never-be-logged"
DECOY_KEY = "decoy-must-never-be-read"
SPOKEN = "سلام، این یک آزمون است"
AUDIO = b"OggS\x00\x02\x00\x00\x00\x00\x00\x00\x00\x00OpusHead" + b"\x11" * 64


# ── Scripted transport: per-leg control, zero network I/O ────────────────


class _Script(httpx.AsyncBaseTransport):
    """A recording async transport with one scriptable response.

    ``error`` (an exception) is raised instead of answering, which is how a
    timeout and a transport failure are simulated without a socket.
    """

    def __init__(
        self,
        *,
        status: int = 200,
        body: bytes = AUDIO,
        headers: dict[str, str] | None = None,
        error: Exception | None = None,
    ) -> None:
        self.status = status
        self.body = body
        self.headers = headers if headers is not None else {"content-type": "audio/ogg"}
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


def _engine(**kwargs: Any) -> adapter.OpenAiSpeechEngine:
    built, reason = adapter.build_engine(
        adapter.SPEECH_MODEL, api_key=API_KEY,
        base_url="https://api.openai.com/v1", **kwargs,
    )
    assert built is not None, reason
    return built


def _fields() -> list[str]:
    return [name for name in dir(adapter) if name.startswith("FAILURE_")]


# ── 1. The documented request ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_one_post_to_the_documented_speech_endpoint(script):
    recording = script()
    audio = await _engine().speak(SPOKEN, timeout_s=30.0)

    assert audio == AUDIO
    assert len(recording.requests) == 1
    request = recording.requests[0]
    assert request.method == "POST"
    assert str(request.url) == "https://api.openai.com/v1/audio/speech"
    assert request.headers["authorization"] == f"Bearer {API_KEY}"
    assert "application/json" in request.headers["content-type"]


@pytest.mark.asyncio
async def test_the_body_carries_the_request_contract_and_nothing_more(script):
    recording = script()
    await _engine().speak(SPOKEN, timeout_s=30.0)

    await recording.requests[0].aread()
    payload = json.loads(recording.requests[0].content)
    assert set(payload) == {"model", "input", "voice", "response_format"}
    assert payload["model"] == adapter.SPEECH_MODEL
    assert payload["voice"] == adapter.DEFAULT_VOICE
    assert payload["response_format"] == adapter.RESPONSE_FORMAT
    # The provider receives the text being spoken, verbatim, and nothing else.
    assert payload["input"] == SPOKEN


@pytest.mark.asyncio
async def test_the_body_can_never_carry_telegram_context(script):
    recording = script()
    await _engine().speak(SPOKEN, timeout_s=30.0)

    await recording.requests[0].aread()
    raw = recording.requests[0].content.decode("utf-8")
    for forbidden in ("chat", "message_id", "msg_id", "caption", "sender",
                      "username", "reply", "owner", "history"):
        assert forbidden not in raw


def test_the_provider_call_takes_text_and_a_budget_and_nothing_else():
    signature = inspect.signature(adapter.OpenAiSpeechEngine.speak)
    assert set(signature.parameters) == {"self", "text", "timeout_s"}
    service_signature = inspect.signature(tts_service.synthesize)
    assert set(service_signature.parameters) == {"text", "request_id", "timeout_s"}


def test_the_configured_base_url_is_honored(monkeypatch):
    monkeypatch.setenv(adapter.BASE_URL_ENV_VAR, "https://gateway.internal/v1/")
    built, reason = adapter.build_engine(adapter.SPEECH_MODEL, api_key=API_KEY)
    assert built is not None, reason

    assert built.endpoint == "https://gateway.internal/v1/audio/speech"


def test_the_public_base_is_the_default(monkeypatch):
    monkeypatch.delenv(adapter.BASE_URL_ENV_VAR, raising=False)

    assert adapter.resolve_base_url() == adapter.DEFAULT_API_BASE


# ── 2. The closed capability set ─────────────────────────────────────────


def test_only_the_registered_model_can_be_built():
    built, reason = adapter.build_engine("tts-1", api_key=API_KEY)

    assert built is None
    assert reason == tts_service.FAILURE_UNSUPPORTED_MODEL


def test_only_a_documented_voice_can_be_built():
    built, reason = adapter.build_engine(
        adapter.SPEECH_MODEL, voice="not-a-voice", api_key=API_KEY,
    )

    assert built is None
    assert reason == tts_service.FAILURE_UNSUPPORTED_VOICE


def test_the_registered_model_and_voice_are_the_only_declared_ones():
    assert adapter.SUPPORTED_MODELS == (adapter.SPEECH_MODEL,)
    assert adapter.DEFAULT_VOICE in adapter.SUPPORTED_VOICES
    assert adapter.SPEECH_MODEL == "gpt-4o-mini-tts"


# ── 3. Credential resolution ─────────────────────────────────────────────


def test_the_repositorys_own_openai_variables_are_the_credential(monkeypatch):
    for name in adapter.API_KEY_ENV_VARS:
        monkeypatch.delenv(name, raising=False)

    assert adapter.API_KEY_ENV_VARS == ("AI_OPENAI_API_KEY", "OPENAI_API_KEY")

    monkeypatch.setenv("OPENAI_API_KEY", "from-compat-var")
    key, name = adapter.resolve_api_key()
    assert (key, name) == ("from-compat-var", "OPENAI_API_KEY")

    monkeypatch.setenv("AI_OPENAI_API_KEY", "from-primary-var")
    key, name = adapter.resolve_api_key()
    assert (key, name) == ("from-primary-var", "AI_OPENAI_API_KEY")


def test_a_decoy_variable_is_never_read(monkeypatch):
    for name in adapter.API_KEY_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("OPENAI_TTS_KEY", DECOY_KEY)

    assert adapter.resolve_api_key() == ("", "")


def test_a_missing_credential_is_a_bounded_reason_not_an_exception(monkeypatch):
    for name in adapter.API_KEY_ENV_VARS:
        monkeypatch.delenv(name, raising=False)

    built, reason = adapter.build_engine(adapter.SPEECH_MODEL)

    assert built is None
    assert reason == tts_service.FAILURE_MISSING_CREDENTIAL


def test_the_traced_credential_identity_is_a_variable_name_never_a_value(monkeypatch):
    monkeypatch.setenv("AI_OPENAI_API_KEY", API_KEY)
    engine = _engine()

    assert engine.key_env_var == "explicit"  # built with an explicit credential


# ── 4. The closed failure taxonomy ───────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,expected",
    [
        (401, tts_service.FAILURE_AUTH),
        (403, tts_service.FAILURE_FORBIDDEN),
        (404, tts_service.FAILURE_UNSUPPORTED_MODEL),
        (429, tts_service.FAILURE_RATE_LIMIT),
        (500, tts_service.FAILURE_SERVER),
        (503, tts_service.FAILURE_SERVER),
        (418, tts_service.FAILURE_REJECTION),
    ],
)
async def test_every_status_is_classified_with_its_http_status(script, status, expected):
    script(status=status, body=b"{}")

    with pytest.raises(TtsError) as caught:
        await _engine().speak(SPOKEN, timeout_s=30.0)

    assert caught.value.failure_class == expected
    assert caught.value.http_status == status
    assert caught.value.stage == tts_service.TTS_STAGE_PROVIDER


@pytest.mark.asyncio
async def test_only_the_transient_families_are_marked_retryable(script):
    script(status=503)
    with pytest.raises(TtsError) as server:
        await _engine().speak(SPOKEN, timeout_s=30.0)
    assert server.value.retryable is True

    script(status=401)
    with pytest.raises(TtsError) as auth:
        await _engine().speak(SPOKEN, timeout_s=30.0)
    assert auth.value.retryable is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "code,expected",
    [
        ('{"error":{"code":"voice_not_found"}}', tts_service.FAILURE_UNSUPPORTED_VOICE),
        ('{"error":{"code":"model_not_found"}}', tts_service.FAILURE_UNSUPPORTED_MODEL),
        ('{"error":{"code":"input_too_long"}}', tts_service.FAILURE_INPUT_TOO_LARGE),
        ('{"error":{"type":"invalid_request_error"}}', tts_service.FAILURE_INVALID_REQUEST),
    ],
)
async def test_a_400_is_narrowed_by_the_providers_own_token(script, code, expected):
    script(status=400, body=code.encode())

    with pytest.raises(TtsError) as caught:
        await _engine().speak(SPOKEN, timeout_s=30.0)

    assert caught.value.failure_class == expected


@pytest.mark.asyncio
async def test_a_spent_quota_is_its_own_class(script):
    script(status=429, body=b'{"error":{"code":"insufficient_quota"}}')

    with pytest.raises(TtsError) as caught:
        await _engine().speak(SPOKEN, timeout_s=30.0)

    assert caught.value.failure_class == tts_service.FAILURE_QUOTA_EXCEEDED
    assert caught.value.retryable is False


@pytest.mark.asyncio
async def test_a_timeout_is_classified_and_retryable(script):
    script(error=httpx.ReadTimeout("read timed out"))

    with pytest.raises(TtsError) as caught:
        await _engine().speak(SPOKEN, timeout_s=30.0)

    assert caught.value.failure_class == tts_service.FAILURE_TIMEOUT
    assert caught.value.retryable is True


@pytest.mark.asyncio
async def test_a_transport_failure_is_classified(script):
    script(error=httpx.ConnectError("no route"))

    with pytest.raises(TtsError) as caught:
        await _engine().speak(SPOKEN, timeout_s=30.0)

    assert caught.value.failure_class == tts_service.FAILURE_TRANSPORT
    assert caught.value.retryable is True


@pytest.mark.asyncio
async def test_an_empty_body_is_not_silent_speech(script):
    script(status=200, body=b"")

    with pytest.raises(TtsError) as caught:
        await _engine().speak(SPOKEN, timeout_s=30.0)

    assert caught.value.failure_class == tts_service.FAILURE_EMPTY_AUDIO


@pytest.mark.asyncio
async def test_a_json_body_on_the_success_path_is_an_anomaly(script):
    script(status=200, body=b"{}", headers={"content-type": "application/json"})

    with pytest.raises(TtsError) as caught:
        await _engine().speak(SPOKEN, timeout_s=30.0)

    assert caught.value.failure_class == tts_service.FAILURE_MALFORMED


@pytest.mark.asyncio
async def test_an_over_sized_response_is_refused_at_the_adapter(script):
    script(status=200, body=b"\x00" * (tts_service.MAX_TTS_AUDIO_BYTES + 1))

    with pytest.raises(TtsError) as caught:
        await _engine().speak(SPOKEN, timeout_s=30.0)

    assert caught.value.failure_class == tts_service.FAILURE_OUTPUT_TOO_LARGE


@pytest.mark.asyncio
async def test_a_deadline_already_spent_is_its_own_class_with_no_request(script):
    recording = script()

    with pytest.raises(TtsError) as caught:
        await _engine().speak(SPOKEN, timeout_s=1.0)

    assert caught.value.failure_class == tts_service.FAILURE_DEADLINE
    assert recording.requests == []


def test_every_class_this_adapter_can_raise_belongs_to_the_closed_taxonomy():
    assert _fields()
    for name in _fields():
        assert getattr(adapter, name) in FAILURE_CLASSES
    assert tts_service._TRANSIENT_CLASSES <= FAILURE_CLASSES


def test_every_classified_error_is_a_tts_error():
    error = adapter._error("x", tts_service.FAILURE_SERVER, retryable=True)

    assert isinstance(error, TtsError)
    assert error.stage == tts_service.TTS_STAGE_PROVIDER
    assert (error.failure_class, error.retryable) == (tts_service.FAILURE_SERVER, True)


# ── 5. Bounded work ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_exactly_one_request_per_call_no_retry_loop(script):
    recording = script()
    engine = _engine()

    for _ in range(3):
        await engine.speak(SPOKEN, timeout_s=30.0)

    assert len(recording.requests) == 3


@pytest.mark.asyncio
async def test_a_rejected_request_is_never_re_sent(script):
    recording = script(status=401, body=b"{}")

    with pytest.raises(TtsError):
        await _engine().speak(SPOKEN, timeout_s=30.0)

    assert len(recording.requests) == 1


def test_the_caller_budget_is_clamped_to_the_adapter_ceiling():
    assert adapter._effective_timeout(30.0) == 30.0
    assert adapter._effective_timeout(600.0) == adapter.MAX_REQUEST_TIMEOUT_S
    assert adapter._effective_timeout(0) == adapter.MAX_REQUEST_TIMEOUT_S
    assert adapter._effective_timeout(None) == adapter.MAX_REQUEST_TIMEOUT_S


def test_every_httpx_phase_bound_is_derived_from_the_remaining_budget():
    timeout = adapter._request_timeout(12.0)

    assert timeout.read == 12.0
    assert timeout.connect == min(adapter.CONNECT_TIMEOUT_S, 12.0)
    tight = adapter._request_timeout(3.0)
    assert (tight.connect, tight.read) == (3.0, 3.0)


def test_the_provider_call_is_async_and_uses_no_blocking_client():
    assert inspect.iscoroutinefunction(adapter.OpenAiSpeechEngine.speak)
    source = inspect.getsource(adapter)
    assert "httpx.AsyncClient" in source
    assert "httpx.Client(" not in source
    assert "asyncio.to_thread" not in source


# ── 6. Zero credential egress ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_credential_never_appears_in_a_failure_message(script):
    script(status=401, body=json.dumps(
        {"error": {"message": f"Invalid key {API_KEY}"}}
    ).encode())

    with pytest.raises(TtsError) as caught:
        await _engine().speak(SPOKEN, timeout_s=30.0)

    assert API_KEY not in str(caught.value)
    assert "***" in str(caught.value)


@pytest.mark.asyncio
async def test_the_credential_never_enters_a_log_line(script, caplog):
    script(status=503, body=b"{}")

    with caplog.at_level(logging.INFO, logger=adapter.__name__):
        with pytest.raises(TtsError):
            await _engine().speak(SPOKEN, timeout_s=30.0)

    assert caplog.records
    assert all(API_KEY not in record.getMessage() for record in caplog.records)


@pytest.mark.asyncio
async def test_the_logged_facts_are_bounded_and_content_free(script, caplog):
    script()

    with caplog.at_level(logging.INFO, logger=adapter.__name__):
        await _engine().speak(SPOKEN, timeout_s=30.0)

    line = "\n".join(record.getMessage() for record in caplog.records)
    assert adapter.SPEECH_MODEL in line
    assert f"chars={len(SPOKEN)}" in line
    # The spoken text itself is never logged.
    assert SPOKEN not in line
