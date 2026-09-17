"""Groq Whisper speech-to-text adapter — the hosted transcription capability.

M2.1 pins the SECOND engine behind the EXISTING ``media_service.SttEngine`` seam.
The Gemini route is untouched; this file proves the Groq route is:

  * capability-specific — it speaks the dedicated transcription API
    (``POST {base}/audio/transcriptions``, multipart) and never chat completions,
    and it does not import, call or alter the Groq CHAT provider;
  * bounded — ONE operation deadline inside the boundary's own bound, every HTTP
    timeout derived from what is left of it, at most two sequential attempts and
    a hard transcript character ceiling;
  * closed over model choice — only the two models the control plane registers can
    be built, so no typed or arbitrary model identifier is ever sent;
  * classified — every failure carries one closed token (credential, auth,
    authorization, invalid model, unsupported audio, timeout, transport, rate
    limit, server, malformed, empty transcription, provider rejection, deadline);
  * context-isolated — the seam takes bytes and nothing else: no owner, chat,
    message, sender, caption, filename or conversation state can reach it.

The HTTP boundary is a scripted ``httpx`` transport: no test needs a credential,
no byte leaves the process. Nothing here proves recognition QUALITY on real
speech — that is the owner's live comparison.
"""
from __future__ import annotations

import inspect
import json
import logging
import typing
from typing import Any

import httpx
import pytest

from backend.ai import stt_control_plane
from backend.services import groq_stt_engine as adapter
from backend.services import media_service
from backend.services.groq_stt_engine import (
    FAILURE_AUTH,
    FAILURE_DEADLINE,
    FAILURE_EMPTY,
    FAILURE_FORBIDDEN,
    FAILURE_INVALID_MODEL,
    FAILURE_MALFORMED,
    FAILURE_MISSING_CREDENTIAL,
    FAILURE_RATE_LIMIT,
    FAILURE_REJECTION,
    FAILURE_SERVER,
    FAILURE_TIMEOUT,
    FAILURE_TRANSPORT,
    FAILURE_UNSUPPORTED_AUDIO,
    FAILURE_UNSUPPORTED_MODEL,
    GROQ_API_BASE,
    MAX_TRANSCRIPT_CHARS,
    OPERATION_DEADLINE_S,
    SUPPORTED_MODELS,
    TRANSCRIPTIONS_PATH,
    GroqWhisperEngine,
    build_engine,
    container_for,
    language_code,
)
from backend.services.media_service import MediaError

#: The UNDECORATED ``httpx`` client, captured before any test monkeypatches it —
#: re-reading ``httpx.Client`` inside ``script()`` would pick up a previous
#: test's own patched factory and silently reuse ITS transport.
_HTTPX_CLIENT = httpx.Client

API_KEY = "gsk_adapter-suite-key-must-never-be-logged"
TRANSCRIPT = "این یک متن آزمایشی است"
OGG_OPUS = b"OggS\x00\x02\x00\x00\x00\x00\x00\x00\x00\x00" + b"OpusHead" + b"\x01" * 32
WAV_HEADER = b"RIFF" + (36).to_bytes(4, "little") + b"WAVE"


def _wav(payload: bytes = b"\x00" * 64) -> bytes:
    return b"RIFF" + (36 + len(payload)).to_bytes(4, "little") + b"WAVE" + payload


# ── Scripted transport: per-leg control, zero network I/O ──


class _Script(httpx.BaseTransport):
    """A recording transport with one scriptable hook for the transcription leg.

    A hook is one of:

      * ``None``            — the documented success response ``{"text": ...}``;
      * an ``Exception``    — raised (timeout/transport simulation);
      * an ``int``          — that HTTP status with a bounded error body;
      * ``(int, dict)``     — that status with that body;
      * a ``dict``          — HTTP 200 with that JSON body;
      * ``bytes``           — HTTP 200 with that raw (unreadable) body;
      * a ``list``          — the N-th value, the last one repeating.
    """

    def __init__(self, transcription: Any = None, text: str = TRANSCRIPT) -> None:
        self.hook = transcription
        self.text = text
        self.requests: list[httpx.Request] = []
        self.timeouts: list[Any] = []
        self._calls = 0

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        # A real transport reads the (streaming) request body itself; this one
        # must do it too, so the recorded request is inspectable afterwards.
        request.read()
        self.requests.append(request)
        hook = self._pick(self.hook, self._calls)
        self._calls += 1
        return self._respond(hook, request)

    @staticmethod
    def _pick(value: Any, index: int) -> Any:
        if isinstance(value, list):
            return value[min(index, len(value) - 1)]
        return value

    def _respond(self, hook: Any, request: httpx.Request) -> httpx.Response:
        if isinstance(hook, BaseException):
            raise hook
        if isinstance(hook, tuple):
            status, body = hook
            return httpx.Response(status, json=body, request=request)
        if isinstance(hook, int):
            return httpx.Response(
                status_code=hook,
                json={"error": {"message": f"scripted error {hook}"}},
                request=request,
            )
        if isinstance(hook, dict):
            return httpx.Response(200, json=hook, request=request)
        if isinstance(hook, bytes):
            return httpx.Response(200, content=hook, request=request)
        return httpx.Response(200, json={"text": self.text}, request=request)


def script(monkeypatch, transcription: Any = None, text: str = TRANSCRIPT) -> _Script:
    """Install a scripted transport, recording the timeout passed to each client."""
    transport = _Script(transcription, text)

    def factory(*args: Any, **inner: Any) -> httpx.Client:
        inner["transport"] = transport
        transport.timeouts.append(inner.get("timeout"))
        return _HTTPX_CLIENT(*args, **inner)

    monkeypatch.setattr(httpx, "Client", factory)
    return transport


# ── Helpers ──


def _field(request: httpx.Request, name: str) -> str:
    """The text value of ONE multipart form field, or ``""`` when absent."""
    body = request.content
    marker = f'name="{name}"'.encode()
    index = body.find(marker)
    if index < 0:
        return ""
    start = body.find(b"\r\n\r\n", index)
    if start < 0:
        return ""
    end = body.find(b"\r\n--", start)
    return body[start + 4: end if end > 0 else len(body)].decode("utf-8", "replace")


def _part_bytes(request: httpx.Request, name: str) -> bytes:
    """The raw bytes of ONE multipart file part, or ``b""`` when absent."""
    body = request.content
    marker = f'name="{name}"'.encode()
    index = body.find(marker)
    if index < 0:
        return b""
    start = body.find(b"\r\n\r\n", index)
    if start < 0:
        return b""
    end = body.find(b"\r\n--", start)
    return body[start + 4: end if end > 0 else len(body)]


def _engine(monkeypatch, *, model: str = adapter.WHISPER_LARGE_V3, language: str = "",
            passes: int = 1) -> GroqWhisperEngine:
    monkeypatch.setenv("AI_GROQ_API_KEY", API_KEY)
    engine, reason = build_engine(model, language=language, passes=passes)
    assert engine is not None, reason
    return engine


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("AI_GROQ_API_KEY", raising=False)
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    yield


# ── 1. Model resolution: registered models only ────────────────────────


@pytest.mark.parametrize("model", SUPPORTED_MODELS)
def test_both_registered_models_build_the_same_adapter(monkeypatch, model):
    engine = _engine(monkeypatch, model=model)

    assert isinstance(engine, GroqWhisperEngine)
    assert engine.model == model
    assert engine.endpoint == f"{GROQ_API_BASE}{TRANSCRIPTIONS_PATH}"


def test_the_supported_models_are_exactly_the_registered_groq_candidates():
    registered = {
        candidate.model
        for candidate in stt_control_plane.all_candidates()
        if candidate.provider == "groq"
    }

    assert registered == set(SUPPORTED_MODELS)


def test_the_project_configured_groq_base_is_used():
    """The project's existing Groq base URL and its documented audio path."""
    assert GROQ_API_BASE == "https://api.groq.com/openai/v1"
    assert TRANSCRIPTIONS_PATH == "/audio/transcriptions"


@pytest.mark.parametrize(
    "model",
    [
        "", "whisper", "whisper-large", "whisper-large-v3-turbo-2",
        "openai/whisper-large-v3", "llama-3.3-70b-versatile", "whisper-1",
    ],
)
def test_an_arbitrary_model_identifier_is_refused(monkeypatch, model):
    """A typed model id can never reach the provider — the registry is the list."""
    monkeypatch.setenv("AI_GROQ_API_KEY", API_KEY)

    engine, reason = build_engine(model)

    assert engine is None
    assert reason == FAILURE_UNSUPPORTED_MODEL


# ── 2. Credential resolution ───────────────────────────────────────────


def test_a_missing_credential_is_reported_deterministically(monkeypatch):
    engine, reason = build_engine(adapter.WHISPER_LARGE_V3)

    assert engine is None
    assert reason == FAILURE_MISSING_CREDENTIAL


def test_the_existing_groq_key_variable_is_the_compatibility_fallback(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "fallback-key")

    engine, reason = build_engine(adapter.WHISPER_LARGE_V3)

    assert engine is not None and reason == ""
    assert engine.key_env_var == "GROQ_API_KEY"


def test_the_ai_prefixed_variable_wins(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "fallback-key")
    monkeypatch.setenv("AI_GROQ_API_KEY", "preferred-key")

    engine, _reason = build_engine(adapter.WHISPER_LARGE_V3)

    assert engine is not None and engine.key_env_var == "AI_GROQ_API_KEY"


def test_the_credential_variables_match_the_project_convention():
    """No new secret is invented: the same pair the provider factory declares."""
    from backend.ai.providers.factory import _ENV_KEY_MAP

    assert tuple(adapter.API_KEY_ENV_VARS) == tuple(_ENV_KEY_MAP["groq"])


def test_a_blank_credential_is_not_a_credential(monkeypatch):
    monkeypatch.setenv("AI_GROQ_API_KEY", "   ")

    engine, reason = build_engine(adapter.WHISPER_LARGE_V3)

    assert engine is None and reason == FAILURE_MISSING_CREDENTIAL


# ── 3. Endpoint, model and multipart request ───────────────────────────


def test_the_documented_endpoint_model_and_bearer_key_are_sent(monkeypatch):
    transport = script(monkeypatch)
    engine = _engine(monkeypatch)

    text = engine.transcribe(OGG_OPUS)

    assert text == TRANSCRIPT
    assert len(transport.requests) == 1
    request = transport.requests[0]
    assert request.method == "POST"
    assert str(request.url) == f"{GROQ_API_BASE}/audio/transcriptions"
    assert request.headers["Authorization"] == f"Bearer {API_KEY}"
    assert request.headers["Content-Type"].startswith("multipart/form-data")
    assert _field(request, "model") == adapter.WHISPER_LARGE_V3
    assert _field(request, "response_format") == "json"


def test_the_ogg_container_is_uploaded_as_ogg(monkeypatch):
    transport = script(monkeypatch)
    engine = _engine(monkeypatch)

    engine.transcribe(OGG_OPUS)

    request = transport.requests[0]
    assert 'filename="audio.ogg"' in request.content.decode("utf-8", "replace")
    assert _part_bytes(request, "file") == OGG_OPUS
    assert container_for(OGG_OPUS) == ("audio.ogg", "audio/ogg")


def test_a_wav_container_is_uploaded_as_wav(monkeypatch):
    transport = script(monkeypatch)
    engine = _engine(monkeypatch)
    payload = _wav()

    engine.transcribe(payload)

    request = transport.requests[0]
    assert "audio.wav" in request.content.decode("utf-8", "replace")
    assert _part_bytes(request, "file") == payload


def test_no_telegram_filename_reaches_the_request(monkeypatch):
    """The multipart name is STATIC, so an untrusted Telegram name cannot leak."""
    transport = script(monkeypatch)
    engine = _engine(monkeypatch)

    engine.transcribe(OGG_OPUS)

    body = transport.requests[0].content
    assert b"voice_note_from_someone" not in body
    assert b"audio.ogg" in body


def test_the_request_carries_only_the_documented_transcription_fields(monkeypatch):
    """No timestamps and no diarization: the experiment's control variables."""
    transport = script(monkeypatch)
    engine = _engine(monkeypatch)

    engine.transcribe(OGG_OPUS)

    body = transport.requests[0].content
    for name in ("timestamp_granularities", "diarization", "prompt", "temperature"):
        assert f'name="{name}"'.encode() not in body


# ── 4. Language handling ───────────────────────────────────────────────


@pytest.mark.parametrize("language,expected", [("fa-IR", "fa"), ("fa", "fa"), ("fa_IR", "fa"), ("en-US", "en")])
def test_an_explicit_language_is_reduced_to_the_iso_code(language, expected):
    assert language_code(language) == expected


@pytest.mark.parametrize("language", ["", "  ", "auto", "persian", "f", "-IR", "1234"])
def test_an_unusable_language_falls_back_to_automatic_detection(language):
    assert language_code(language) == ""


def test_automatic_language_sends_no_language_field(monkeypatch):
    transport = script(monkeypatch)
    engine = _engine(monkeypatch, language="")

    engine.transcribe(OGG_OPUS)

    assert b'name="language"' not in transport.requests[0].content


def test_persian_is_passed_explicitly_and_returned_unchanged(monkeypatch):
    transport = script(monkeypatch, text=TRANSCRIPT)
    engine = _engine(monkeypatch, language="fa-IR")

    text = engine.transcribe(OGG_OPUS)

    assert _field(transport.requests[0], "language") == "fa"
    assert text == TRANSCRIPT, "Persian must come back in its own script, untranslated"


# ── 5. Successful and failed responses ─────────────────────────────────


def test_a_successful_response_is_parsed(monkeypatch):
    script(monkeypatch, transcription={"text": "hello", "x_groq": {"id": "req_1"}})

    assert _engine(monkeypatch).transcribe(OGG_OPUS) == "hello"


@pytest.mark.parametrize("hook", [{"text": ""}, {"text": "   "}])
def test_an_empty_transcript_is_a_failure(monkeypatch, hook):
    script(monkeypatch, transcription=hook)

    with pytest.raises(MediaError) as caught:
        _engine(monkeypatch).transcribe(OGG_OPUS)

    assert caught.value.failure_class == FAILURE_EMPTY


@pytest.mark.parametrize("hook", [b"not json at all", {"result": "nope"}, {"text": 12}, [1, 2]])
def test_a_malformed_response_is_a_failure(monkeypatch, hook):
    script(monkeypatch, transcription=hook)

    with pytest.raises(MediaError) as caught:
        _engine(monkeypatch).transcribe(OGG_OPUS)

    assert caught.value.failure_class == FAILURE_MALFORMED


# ── 6. Failure classification ──────────────────────────────────────────


@pytest.mark.parametrize(
    "hook,expected",
    [
        (401, FAILURE_AUTH),
        (403, FAILURE_FORBIDDEN),
        (404, FAILURE_INVALID_MODEL),
        (429, FAILURE_RATE_LIMIT),
        (422, FAILURE_REJECTION),
        (500, FAILURE_SERVER),
        (503, FAILURE_SERVER),
        ((400, {"error": {"message": "model not found", "code": "model_not_found"}}), FAILURE_INVALID_MODEL),
        ((400, {"error": {"message": "could not decode", "code": "invalid_audio"}}), FAILURE_UNSUPPORTED_AUDIO),
        ((400, {"error": {"message": "bad request"}}), FAILURE_REJECTION),
    ],
)
def test_http_failures_are_classified(monkeypatch, hook, expected):
    script(monkeypatch, transcription=hook)

    with pytest.raises(MediaError) as caught:
        _engine(monkeypatch).transcribe(OGG_OPUS)

    assert caught.value.failure_class == expected


def test_a_read_timeout_is_classified_with_its_socket_phase(monkeypatch):
    script(monkeypatch, transcription=httpx.ReadTimeout("scripted read timeout"))

    with pytest.raises(MediaError) as caught:
        _engine(monkeypatch).transcribe(OGG_OPUS)

    assert caught.value.failure_class == FAILURE_TIMEOUT
    assert caught.value.phase == "read"


def test_a_connect_timeout_reports_the_connect_phase(monkeypatch):
    script(monkeypatch, transcription=httpx.ConnectTimeout("scripted connect timeout"))

    with pytest.raises(MediaError) as caught:
        _engine(monkeypatch).transcribe(OGG_OPUS)

    assert (caught.value.failure_class, caught.value.phase) == (FAILURE_TIMEOUT, "connect")


def test_a_connection_failure_is_a_transport_failure(monkeypatch):
    script(monkeypatch, transcription=httpx.ConnectError("scripted connect error"))

    with pytest.raises(MediaError) as caught:
        _engine(monkeypatch).transcribe(OGG_OPUS)

    assert caught.value.failure_class == FAILURE_TRANSPORT


def test_the_failure_class_is_recorded_on_the_engine(monkeypatch):
    script(monkeypatch, transcription=(400, {"error": {"message": "bad"}}))
    engine = _engine(monkeypatch)

    with pytest.raises(MediaError):
        engine.transcribe(OGG_OPUS)

    assert engine.last_failure_class == FAILURE_REJECTION


# ── 7. Bounded retry and bounded deadline ──────────────────────────────


def test_a_transient_failure_is_retried_once_within_the_operation(monkeypatch):
    transport = script(monkeypatch, transcription=[500, None])
    engine = _engine(monkeypatch)

    assert engine.transcribe(OGG_OPUS) == TRANSCRIPT
    assert len(transport.requests) == adapter.MAX_ATTEMPTS == 2


def test_the_retry_count_is_hard_bounded(monkeypatch):
    transport = script(monkeypatch, transcription=503)

    with pytest.raises(MediaError) as caught:
        _engine(monkeypatch).transcribe(OGG_OPUS)

    assert caught.value.failure_class == FAILURE_SERVER
    assert len(transport.requests) == adapter.MAX_ATTEMPTS


def test_a_429_is_retried_but_a_rejection_is_not(monkeypatch):
    rate_limited = script(monkeypatch, transcription=[429, None])
    assert _engine(monkeypatch).transcribe(OGG_OPUS) == TRANSCRIPT
    assert len(rate_limited.requests) == 2

    rejection = script(monkeypatch, transcription=400)
    with pytest.raises(MediaError):
        _engine(monkeypatch).transcribe(OGG_OPUS)
    assert len(rejection.requests) == 1, "a deterministic rejection is never re-sent"


def test_an_empty_transcript_is_never_retried(monkeypatch):
    transport = script(monkeypatch, transcription={"text": ""})

    with pytest.raises(MediaError):
        _engine(monkeypatch).transcribe(OGG_OPUS)

    assert len(transport.requests) == 1


def test_a_spent_deadline_starts_no_request(monkeypatch):
    transport = script(monkeypatch)
    monkeypatch.setattr(adapter, "OPERATION_DEADLINE_S", 0.0)

    with pytest.raises(MediaError) as caught:
        _engine(monkeypatch).transcribe(OGG_OPUS)

    assert caught.value.failure_class == FAILURE_DEADLINE
    assert transport.requests == []


def test_the_operation_deadline_is_inside_the_boundary_bound():
    """The engine must fail with its OWN reason before the boundary's outer bound."""
    assert OPERATION_DEADLINE_S < media_service.STT_TIMEOUT_S


def test_the_client_timeouts_are_derived_from_the_operation_budget(monkeypatch):
    transport = script(monkeypatch)

    _engine(monkeypatch).transcribe(OGG_OPUS)

    assert transport.timeouts, "the engine must always carry a finite timeout"
    timeout = transport.timeouts[0]
    assert isinstance(timeout, httpx.Timeout)
    assert 0 < timeout.read <= OPERATION_DEADLINE_S
    assert 0 < timeout.connect <= adapter.CONNECT_TIMEOUT_S


def test_an_empty_payload_is_never_sent(monkeypatch):
    transport = script(monkeypatch)

    assert _engine(monkeypatch).transcribe(b"") == ""
    assert transport.requests == []


# ── 8. Containers this adapter refuses before sending ──────────────────


@pytest.mark.parametrize(
    "payload",
    [b"fLaC" + b"\x00" * 40, b"\x00\x01\x02\x03", b"ID3\x04" + b"\x00" * 20, b"RIFF" + b"\x00" * 4 + b"AVI "],
)
def test_an_undocumented_container_is_refused_without_a_request(monkeypatch, payload):
    transport = script(monkeypatch)

    with pytest.raises(MediaError) as caught:
        _engine(monkeypatch).transcribe(payload)

    assert caught.value.failure_class == FAILURE_UNSUPPORTED_AUDIO
    assert transport.requests == []


def test_only_the_boundary_supported_containers_are_recognised():
    assert container_for(OGG_OPUS) == ("audio.ogg", "audio/ogg")
    assert container_for(_wav()) == ("audio.wav", "audio/wav")
    assert container_for(b"fLaC\x00" * 8) is None


# ── 9. Output ceiling ──────────────────────────────────────────────────


def test_the_transcript_is_capped_at_the_shared_ceiling(monkeypatch):
    script(monkeypatch, transcription={"text": "ا" * (MAX_TRANSCRIPT_CHARS * 2)})

    text = _engine(monkeypatch).transcribe(OGG_OPUS)

    assert len(text) == MAX_TRANSCRIPT_CHARS
    assert text.endswith("…")
    assert MAX_TRANSCRIPT_CHARS == media_service.MAX_STT_CHARS


def test_a_transcript_inside_the_ceiling_is_untouched(monkeypatch):
    script(monkeypatch, transcription={"text": "short transcript"})

    assert _engine(monkeypatch).transcribe(OGG_OPUS) == "short transcript"


# ── 10. Recognition passes (the existing consensus seam) ───────────────


def test_one_pass_issues_exactly_one_request(monkeypatch):
    transport = script(monkeypatch)

    _engine(monkeypatch, passes=1).transcribe(OGG_OPUS)

    assert len(transport.requests) == 1


def test_three_passes_are_sequential_bounded_and_reconciled(monkeypatch):
    from backend.services.stt_consensus import reconcile_hypotheses

    hypotheses = ["سلام دنیا", "سلام دنیا", "سلام دوست"]
    transport = script(monkeypatch, transcription=[{"text": text} for text in hypotheses])

    text = _engine(monkeypatch, passes=3).transcribe(OGG_OPUS)

    assert len(transport.requests) == 3
    assert text == reconcile_hypotheses(hypotheses).text


def test_the_pass_count_is_held_inside_the_shared_bound(monkeypatch):
    _engine(monkeypatch, passes=99)

    engine = _engine(monkeypatch, passes=0)
    assert engine.passes == 1

    transport = script(monkeypatch)
    _engine(monkeypatch, passes=99).transcribe(OGG_OPUS)
    assert len(transport.requests) == 3


def test_a_deterministic_failure_stops_the_pass_loop(monkeypatch):
    transport = script(monkeypatch, transcription=400)

    with pytest.raises(MediaError) as caught:
        _engine(monkeypatch, passes=3).transcribe(OGG_OPUS)

    assert caught.value.failure_class == FAILURE_REJECTION
    assert len(transport.requests) == 1, "a deterministic failure is never re-sent"


def test_a_failed_pass_contributes_no_hypothesis(monkeypatch):
    """A transport problem can never be mistaken for a recognition result."""
    transport = script(
        monkeypatch,
        transcription=[httpx.ConnectError("scripted"), {"text": "سلام"}, {"text": "سلام"}],
    )

    text = _engine(monkeypatch, passes=3).transcribe(OGG_OPUS)

    assert text == "سلام"
    assert len(transport.requests) == 3


# ── 11. Isolation: no context and no credential in the logs ────────────


def test_the_seam_takes_bytes_and_nothing_else():
    signature = inspect.signature(GroqWhisperEngine.transcribe)

    assert list(signature.parameters) == ["self", "audio"]
    assert typing.get_type_hints(GroqWhisperEngine.transcribe)["audio"] is bytes
    assert typing.get_type_hints(GroqWhisperEngine.transcribe)["return"] is str


def test_the_engine_holds_no_telegram_or_conversational_state():
    assert set(GroqWhisperEngine.__slots__) == {
        "_api_key", "_model", "_language", "_passes", "_base_url", "_key_env_var",
        "_last_failure_class",
    }


def test_no_telegram_metadata_can_be_injected(monkeypatch):
    script(monkeypatch)
    engine = _engine(monkeypatch)

    with pytest.raises(TypeError):
        engine.transcribe(OGG_OPUS, owner_id=1, chat_id=-100, caption="secret")  # type: ignore[call-arg]

    blob = repr(engine) + repr(engine.__slots__)
    for leak in ("owner", "chat_id", "@someone", "caption"):
        assert leak not in blob


def test_the_api_key_is_never_logged(monkeypatch, caplog):
    script(monkeypatch, transcription=[500, (401, {"error": {"message": f"bad key {API_KEY}"}})])
    engine = _engine(monkeypatch)

    with caplog.at_level(logging.DEBUG):
        with pytest.raises(MediaError) as caught:
            engine.transcribe(OGG_OPUS)

    assert API_KEY not in caplog.text
    assert API_KEY not in str(caught.value)
    assert "***" in str(caught.value) or "HTTP 401" in str(caught.value)


def test_the_transcript_is_never_logged(monkeypatch, caplog):
    script(monkeypatch, text=TRANSCRIPT)

    with caplog.at_level(logging.DEBUG):
        _engine(monkeypatch).transcribe(OGG_OPUS)

    assert caplog.text, "one bounded line per operation is expected"
    assert TRANSCRIPT not in caplog.text
    assert "chars=" in caplog.text


def test_the_trace_line_carries_only_bounded_operational_fields(monkeypatch, caplog):
    script(monkeypatch)

    with caplog.at_level(logging.INFO):
        _engine(monkeypatch, language="fa-IR").transcribe(OGG_OPUS)

    records = [r.getMessage() for r in caplog.records if "GROQ_STT_ENGINE" in r.getMessage()]
    assert records
    line = records[-1]
    assert "model=whisper-large-v3" in line
    assert "language=fa" in line
    assert "status=ok" in line
    assert "failure_class=-" in line


# ── 12. The Groq CHAT provider is untouched ────────────────────────────


def test_the_adapter_does_not_import_the_chat_provider_or_its_manager():
    import ast

    source = inspect.getsource(adapter)
    tree = ast.parse(source)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")

    for module_name in imported:
        assert not module_name.startswith("backend.ai.providers"), module_name


def test_the_chat_provider_has_no_transcription_behavior():
    from backend.ai.providers import groq as chat_provider

    source = inspect.getsource(chat_provider)

    assert "audio/transcriptions" not in source
    assert "transcribe" not in source
    assert "GroqProvider" in source


def test_the_adapter_declares_no_behavioral_environment_variable():
    """ENV is not the settings database: only a credential may come from it."""
    source = inspect.getsource(adapter)

    assert adapter.API_KEY_ENV_VARS == ("AI_GROQ_API_KEY", "GROQ_API_KEY")
    for forbidden in ("AI_GROQ_STT_MODEL", "AI_GROQ_STT_LANGUAGE", "AI_GROQ_STT_PASSES"):
        assert forbidden not in source


def test_no_new_dependency_is_required():
    """The project's existing HTTP stack is sufficient (no local audio stack)."""
    import backend.services.groq_stt_engine as module

    assert module.httpx.__name__ == "httpx"
    assert not hasattr(module, "whisper")
    assert not hasattr(module, "torch")


def test_the_adapter_only_uses_the_existing_media_seam():
    source = inspect.getsource(adapter)

    assert "set_stt_engine" not in source, "provisioning belongs to the factory"
    assert "download_media" not in source
    assert "telethon" not in source.lower()


def test_a_4xx_body_that_echoes_the_key_is_not_surfaced_verbatim(monkeypatch):
    script(monkeypatch, transcription=(400, {"error": {"message": f"invalid {API_KEY}"}}))

    with pytest.raises(MediaError) as caught:
        _engine(monkeypatch).transcribe(OGG_OPUS)

    assert API_KEY not in str(caught.value)


def test_the_json_error_code_decides_the_400_family(monkeypatch):
    script(monkeypatch, transcription=(400, {"error": {"code": "model_not_found", "message": "x"}}))

    with pytest.raises(MediaError) as caught:
        _engine(monkeypatch).transcribe(OGG_OPUS)

    assert caught.value.failure_class == FAILURE_INVALID_MODEL
    assert caught.value.http_status == 400


def test_the_failure_message_names_the_provider_and_the_status(monkeypatch):
    script(monkeypatch, transcription=503)

    with pytest.raises(MediaError) as caught:
        _engine(monkeypatch).transcribe(OGG_OPUS)

    assert "Groq" in str(caught.value)
    assert "503" in str(caught.value)


def test_the_adapter_exposes_a_stable_module_identity():
    assert adapter.PROVIDER_NAME == "groq"
    assert json.dumps({"provider": adapter.PROVIDER_NAME}) == '{"provider": "groq"}'
