"""Speechmatics speech-to-text adapter — the hosted batch transcription capability.

M2.2 pins the THIRD engine behind the EXISTING ``media_service.SttEngine`` seam.
The Gemini and Groq routes are untouched; this file proves the Speechmatics route
is:

  * capability-specific — it speaks the official batch REST API (create job →
    poll status → fetch transcript) and never a chat completion, and it does not
    import the generic provider architecture;
  * bounded — ONE operation deadline inside the boundary's own bound, every HTTP
    timeout derived from what is left of it, a bounded poll, at most two
    sequential (transient) attempts and a hard transcript character ceiling;
  * closed over model choice — only the model the control plane registers can be
    built, so no typed or arbitrary model identifier is ever sent;
  * classified — every failure carries one closed token (credential, auth,
    authorization, invalid request/model, unsupported audio, timeout, transport,
    rate limit, server, malformed, empty transcription, provider rejection,
    deadline);
  * context-isolated — the seam takes bytes and nothing else: no owner, chat,
    message, sender, caption, filename or conversation state can reach it.

The HTTP boundary is a scripted ``httpx`` transport: no test needs a credential,
no byte leaves the process. Nothing here proves recognition QUALITY on real
speech — that is the owner's live comparison, and a synthetic payload is never
presented as a benchmark.
"""
from __future__ import annotations

import inspect
import json
import logging
from typing import Any

import httpx
import pytest

from backend.ai import stt_control_plane
from backend.services import media_service
from backend.services import speechmatics_stt_engine as adapter
from backend.services.media_service import MediaError
from backend.services.speechmatics_stt_engine import (
    API_BASE,
    FAILURE_AUTH,
    FAILURE_DEADLINE,
    FAILURE_EMPTY,
    FAILURE_FORBIDDEN,
    FAILURE_INVALID_REQUEST,
    FAILURE_MALFORMED,
    FAILURE_MISSING_CREDENTIAL,
    FAILURE_RATE_LIMIT,
    FAILURE_REJECTION,
    FAILURE_SERVER,
    FAILURE_TIMEOUT,
    FAILURE_TRANSPORT,
    FAILURE_UNSUPPORTED_AUDIO,
    FAILURE_UNSUPPORTED_MODEL,
    JOBS_PATH,
    MAX_ATTEMPTS,
    MAX_POLL_FAILURES,
    MAX_TRANSCRIPT_CHARS,
    OPERATION_DEADLINE_S,
    SUPPORTED_MODELS,
    SpeechmaticsBatchEngine,
    build_engine,
    container_for,
    language_code,
)

#: The UNDECORATED ``httpx`` client, captured before any test monkeypatches it —
#: re-reading ``httpx.Client`` inside ``script()`` would pick up a previous
#: test's own patched factory and silently reuse ITS transport.
_HTTPX_CLIENT = httpx.Client

API_KEY = "sm_adapter-suite-key-must-never-be-logged"
JOB_ID = "job-0000-adapter-suite"
TRANSCRIPT = "این یک متن آزمایشی است"

OGG_OPUS = b"OggS\x00\x02\x00\x00\x00\x00\x00\x00\x00\x00" + b"OpusHead" + b"\x01" * 32
FLAC = b"fLaC" + b"\x00" * 32


def _wav(payload: bytes = b"\x00" * 64) -> bytes:
    return b"RIFF" + (36 + len(payload)).to_bytes(4, "little") + b"WAVE" + payload


def _response(status: int, body: Any, request: httpx.Request) -> httpx.Response:
    if isinstance(body, bytes):
        return httpx.Response(status, content=body, request=request)
    return httpx.Response(status, json=body, request=request)


class _Transport(httpx.BaseTransport):
    """A recording transport scripted PER API LEG, with zero network I/O.

    ``create`` scripts the job-submission leg and ``transcript`` the transcript
    leg; each hook is one of:

      * ``None``        — the leg's documented success response;
      * an ``Exception``— raised (timeout/transport simulation);
      * an ``int``      — that HTTP status with a bounded error body;
      * a ``tuple``     — ``(status, body)``;
      * a ``dict``      — HTTP 200 with that JSON body;
      * ``bytes``       — HTTP 200 with that raw body;
      * a ``list``      — the N-th value, the last one repeating.

    ``statuses`` walks the polling leg the same way — a LIST is a per-call
    sequence (its last value repeating) while anything else is a single hook —
    and a plain string is the job status that leg reports, so a malformed body or
    a transient read failure can be scripted as precisely as a rejection.
    """

    def __init__(
        self,
        *,
        create: Any = None,
        statuses: Any = ["done"],
        transcript: Any = None,
        text: str = TRANSCRIPT,
    ) -> None:
        self.create = create
        self.statuses = statuses
        self.transcript = transcript
        self.text = text
        self.requests: list[httpx.Request] = []
        self.timeouts: list[Any] = []
        self._created = 0
        self._status_index = 0

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        # A real transport reads the (streaming) request body itself; this one
        # must do it too, so the recorded request is inspectable afterwards.
        request.read()
        self.requests.append(request)
        path = request.url.path
        if request.method == "POST" and path.endswith(JOBS_PATH):
            self._created += 1
            return self._create_response(request)
        if path.endswith("/transcript"):
            return self._transcript_response(request)
        return self._status_response(request)

    # ── the three legs ──

    def _create_response(self, request: httpx.Request) -> httpx.Response:
        hook = self._pick(self.create, self._created - 1)
        if hook is None:
            return _response(201, {"id": JOB_ID}, request)
        return self._from_hook(hook, request, fallback_status=201)

    def _status_response(self, request: httpx.Request) -> httpx.Response:
        hook = self._pick(self.statuses, self._status_index)
        self._status_index += 1
        if hook is None:
            return _response(200, {"job": {"status": "done"}}, request)
        if isinstance(hook, str):
            return _response(200, {"job": {"status": hook}}, request)
        return self._from_hook(hook, request, fallback_status=200)

    def _transcript_response(self, request: httpx.Request) -> httpx.Response:
        if self.transcript is None:
            return httpx.Response(
                200, content=self.text.encode("utf-8"),
                headers={"content-type": "text/plain; charset=utf-8"}, request=request,
            )
        return self._from_hook(self.transcript, request, fallback_status=200)

    def _from_hook(
        self, hook: Any, request: httpx.Request, *, fallback_status: int,
    ) -> httpx.Response:
        if isinstance(hook, BaseException):
            raise hook
        if isinstance(hook, tuple):
            return _response(hook[0], hook[1], request)
        if isinstance(hook, int):
            return _response(hook, {"detail": f"scripted error {hook}"}, request)
        if isinstance(hook, dict):
            return _response(fallback_status, hook, request)
        if isinstance(hook, bytes):
            return _response(200, hook, request)
        return _response(fallback_status, {"id": JOB_ID}, request)

    @staticmethod
    def _pick(value: Any, index: int) -> Any:
        # Only a LIST is a sequence of hooks; a tuple is one ``(status, body)``.
        if isinstance(value, list):
            return value[min(index, len(value) - 1)]
        return value

    @property
    def legs(self) -> list[str]:
        out = []
        for request in self.requests:
            if request.method == "POST":
                out.append("create")
            elif request.url.path.endswith("/transcript"):
                out.append("transcript")
            else:
                out.append("status")
        return out


def script(monkeypatch, **kwargs: Any) -> _Transport:
    """Install a scripted transport and remove every real poll delay."""
    transport = _Transport(**kwargs)
    monkeypatch.setattr(adapter, "POLL_INTERVAL_S", 0.0)

    def factory(*args: Any, **inner: Any) -> httpx.Client:
        inner["transport"] = transport
        transport.timeouts.append(inner.get("timeout"))
        return _HTTPX_CLIENT(*args, **inner)

    monkeypatch.setattr(httpx, "Client", factory)
    return transport


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    """No credential and no provisioned engine leaks between tests."""
    monkeypatch.delenv("AI_SPEECHMATICS_API_KEY", raising=False)
    media_service.set_stt_engine(None)
    yield
    media_service.set_stt_engine(None)


def _engine(**kwargs: Any) -> SpeechmaticsBatchEngine:
    options: dict[str, Any] = {"api_key": API_KEY, "model": "standard"}
    options.update(kwargs)
    engine, reason = build_engine(**options)
    assert reason == "", reason
    assert engine is not None
    return engine


def _field(request: httpx.Request, name: str) -> str:
    """The text value of ONE multipart form field, or ``""`` when absent."""
    body = request.content
    marker = f'name="{name}"'.encode()
    index = body.find(marker)
    if index < 0:
        return ""
    after = body[index + len(marker):]
    separator = after.find(b"\r\n\r\n")
    if separator < 0:
        return ""
    tail = after[separator + 4:]
    end = tail.find(b"\r\n")
    return tail[:end].decode("utf-8", "replace") if end >= 0 else ""


# ── 1. The registered model list is the ONLY model list ────────────────


def test_the_adapter_supports_exactly_the_registered_speechmatics_models():
    registered = {
        candidate.model
        for candidate in stt_control_plane.all_candidates()
        if candidate.provider == "speechmatics"
    }

    assert registered == set(SUPPORTED_MODELS)


def test_every_registered_speechmatics_candidate_has_an_operating_point():
    for candidate in stt_control_plane.all_candidates():
        if candidate.provider != "speechmatics":
            continue
        assert candidate.model in adapter.OPERATING_POINTS


@pytest.mark.parametrize(
    "model", ["whisper-large-v3", "enhanced", "", "STANDARD", "standard-v2"]
)
def test_an_unregistered_model_is_refused(model):
    engine, reason = build_engine(model, api_key=API_KEY)

    assert engine is None
    assert reason == FAILURE_UNSUPPORTED_MODEL


def test_a_missing_credential_is_reported_without_any_request():
    engine, reason = build_engine("standard")

    assert engine is None
    assert reason == FAILURE_MISSING_CREDENTIAL


def test_the_credential_comes_from_the_declared_environment_variable(monkeypatch):
    monkeypatch.setenv("AI_SPEECHMATICS_API_KEY", API_KEY)

    engine, reason = build_engine("standard")

    assert reason == "" and engine is not None
    assert engine.key_env_var == "AI_SPEECHMATICS_API_KEY"


def test_the_credential_has_exactly_one_declared_variable():
    assert adapter.API_KEY_ENV_VARS == ("AI_SPEECHMATICS_API_KEY",)


def test_no_behavioral_environment_variable_is_read():
    """ENV is a secret store, not a settings database."""
    source = inspect.getsource(adapter)

    for forbidden in ("AI_SPEECHMATICS_STT_MODEL", "AI_SPEECHMATICS_STT_LANGUAGE",
                      "AI_SPEECHMATICS_STT_PASSES", "SPEECHMATICS_MODEL",
                      "SPEECHMATICS_API_URL"):
        assert forbidden not in source


# ── 2. Endpoint, authorization and the multipart job request ────────────


def test_the_create_job_endpoint_is_the_documented_batch_url(monkeypatch):
    transport = script(monkeypatch)

    _engine().transcribe(OGG_OPUS)

    create = transport.requests[0]
    assert create.method == "POST"
    assert str(create.url) == f"{API_BASE}{JOBS_PATH}"
    assert API_BASE == "https://asr.api.speechmatics.com/v2"


def test_authorization_is_a_bearer_token(monkeypatch):
    transport = script(monkeypatch)

    _engine().transcribe(OGG_OPUS)

    assert transport.requests[0].headers["Authorization"] == f"Bearer {API_KEY}"


def test_the_job_is_submitted_as_multipart_form_data(monkeypatch):
    transport = script(monkeypatch)

    _engine().transcribe(OGG_OPUS)

    create = transport.requests[0]
    body = create.content.decode("utf-8", "replace")
    assert create.headers["Content-Type"].startswith("multipart/form-data")
    assert 'name="config"' in body
    assert 'name="data_file"' in body


def test_the_uploaded_audio_is_the_bounded_payload_under_a_static_name(monkeypatch):
    transport = script(monkeypatch)

    _engine().transcribe(OGG_OPUS)

    body = transport.requests[0].content
    assert b'filename="audio.ogg"' in body
    assert OGG_OPUS in body


def test_a_wav_payload_keeps_its_documented_container(monkeypatch):
    transport = script(monkeypatch)

    _engine().transcribe(_wav())

    assert b'filename="audio.wav"' in transport.requests[0].content


def test_the_job_config_carries_the_documented_shape(monkeypatch):
    transport = script(monkeypatch)

    _engine(language="fa-IR").transcribe(_wav())

    config = json.loads(_field(transport.requests[0], "config"))
    assert config["type"] == "transcription"
    assert config["transcription_config"]["language"] == "fa"
    assert config["transcription_config"]["operating_point"] == "standard"


def test_the_operating_point_is_the_registered_model(monkeypatch):
    transport = script(monkeypatch)

    _engine().transcribe(OGG_OPUS)

    config = json.loads(_field(transport.requests[0], "config"))
    assert config["transcription_config"]["operating_point"] == _engine().model


def test_the_config_asks_for_no_diarization_and_no_extra_output():
    """The experiment's control variables: no diarization, no timestamps."""
    config = _engine().job_config()

    assert set(config) == {"type", "transcription_config"}
    assert set(config["transcription_config"]) == {"language", "operating_point"}


def test_an_untrusted_filename_can_never_reach_the_provider(monkeypatch):
    transport = script(monkeypatch)

    _engine().transcribe(_wav())

    body = transport.requests[0].content
    assert b"voice-note-from-telegram.ogg" not in body
    assert b'filename="audio.wav"' in body


# ── 3. The audio contract (unchanged, never broadened) ─────────────────


@pytest.mark.parametrize(
    "payload,expected",
    [
        (OGG_OPUS, ("audio.ogg", "audio/ogg")),
        (_wav(), ("audio.wav", "audio/wav")),
        (FLAC, ("audio.flac", "audio/flac")),
    ],
)
def test_the_documented_boundary_containers_are_recognised(payload, expected):
    assert container_for(payload) == expected


@pytest.mark.parametrize("payload", [b"", b"ID3\x03\x00", b"not-audio", b"Ogg", b"fLa"])
def test_an_undocumented_container_is_refused_locally(payload):
    assert container_for(payload) is None


def test_an_undocumented_container_fails_without_a_request(monkeypatch):
    transport = script(monkeypatch)

    with pytest.raises(MediaError) as caught:
        _engine().transcribe(b"ID3\x03\x00\x00\x00\x00")

    assert adapter.failure_class_of(caught.value) == FAILURE_UNSUPPORTED_AUDIO
    assert transport.requests == []


def test_empty_input_is_the_seams_honest_empty_string(monkeypatch):
    transport = script(monkeypatch)

    assert _engine().transcribe(b"") == ""
    assert transport.requests == []


# ── 4. Language handling ───────────────────────────────────────────────


@pytest.mark.parametrize(
    "given,expected",
    [("fa-IR", "fa"), ("fa", "fa"), ("en-US", "en"), ("", "auto"), ("PERSIAN", "auto")],
)
def test_language_is_reduced_to_the_documented_iso_code(given, expected):
    assert language_code(given) == expected


def test_automatic_language_uses_the_providers_own_token():
    engine = _engine(language="")

    assert engine.language == "auto"
    assert engine.job_config()["transcription_config"]["language"] == "auto"


def test_an_explicit_language_is_never_forced_or_translated(monkeypatch):
    transport = script(monkeypatch)

    _engine(language="fa-IR").transcribe(OGG_OPUS)

    config = json.loads(_field(transport.requests[0], "config"))
    assert config["transcription_config"]["language"] == "fa"


def test_persian_output_stays_persian(monkeypatch):
    script(monkeypatch)

    assert _engine(language="fa").transcribe(OGG_OPUS) == TRANSCRIPT


# ── 5. The job cycle and the response contract ─────────────────────────


def test_one_recognition_is_create_then_status_then_transcript(monkeypatch):
    transport = script(monkeypatch)

    transcript = _engine().transcribe(OGG_OPUS)

    assert transcript == TRANSCRIPT
    assert transport.legs == ["create", "status", "transcript"]


def test_the_transcript_is_requested_as_plain_text(monkeypatch):
    transport = script(monkeypatch)

    _engine().transcribe(OGG_OPUS)

    fetch = transport.requests[-1]
    assert fetch.method == "GET"
    assert fetch.url.path.endswith("/transcript")
    assert fetch.url.params["format"] == "txt"
    assert fetch.headers["Accept"] == "text/plain"


def test_the_transcript_is_fetched_from_the_submitted_job(monkeypatch):
    transport = script(monkeypatch)

    _engine().transcribe(OGG_OPUS)

    assert f"{JOBS_PATH}/{JOB_ID}" in str(transport.requests[1].url)
    assert f"{JOBS_PATH}/{JOB_ID}" in str(transport.requests[2].url)


def test_running_jobs_are_polled_until_done(monkeypatch):
    transport = script(monkeypatch, statuses=["running", "running", "done"])

    transcript = _engine().transcribe(OGG_OPUS)

    assert transcript == TRANSCRIPT
    assert transport.legs == ["create", "status", "status", "status", "transcript"]


def test_a_rejected_job_is_an_honest_failure(monkeypatch):
    transport = script(monkeypatch, statuses=["rejected"])

    with pytest.raises(MediaError) as caught:
        _engine().transcribe(OGG_OPUS)

    assert adapter.failure_class_of(caught.value) == FAILURE_REJECTION
    assert "transcript" not in transport.legs


def test_a_failed_job_is_an_honest_failure(monkeypatch):
    script(monkeypatch, statuses=["failed"])

    with pytest.raises(MediaError) as caught:
        _engine().transcribe(OGG_OPUS)

    assert adapter.failure_class_of(caught.value) == FAILURE_REJECTION


def test_a_job_status_is_normalised_case_insensitively(monkeypatch):
    script(monkeypatch, statuses=["DONE"])

    assert _engine().transcribe(OGG_OPUS) == TRANSCRIPT


def test_an_empty_transcript_is_a_failure_never_a_success(monkeypatch):
    script(monkeypatch, text="   ")

    with pytest.raises(MediaError) as caught:
        _engine().transcribe(OGG_OPUS)

    assert adapter.failure_class_of(caught.value) == FAILURE_EMPTY


def test_a_create_response_without_a_job_id_is_malformed(monkeypatch):
    transport = script(monkeypatch, create={"unexpected": True})

    with pytest.raises(MediaError) as caught:
        _engine().transcribe(OGG_OPUS)

    assert adapter.failure_class_of(caught.value) == FAILURE_MALFORMED
    assert transport.legs == ["create"]


def test_an_unreadable_create_body_is_malformed(monkeypatch):
    script(monkeypatch, create=b"not json at all")

    with pytest.raises(MediaError) as caught:
        _engine().transcribe(OGG_OPUS)

    assert adapter.failure_class_of(caught.value) == FAILURE_MALFORMED


def test_a_status_response_without_a_status_is_malformed(monkeypatch):
    script(monkeypatch, statuses=[{"unexpected": True}])

    with pytest.raises(MediaError) as caught:
        _engine().transcribe(OGG_OPUS)

    assert adapter.failure_class_of(caught.value) == FAILURE_MALFORMED


def test_the_transcript_is_capped_at_the_shared_ceiling(monkeypatch):
    script(monkeypatch, text="پ" * (MAX_TRANSCRIPT_CHARS + 500))

    transcript = _engine().transcribe(OGG_OPUS)

    assert len(transcript) == MAX_TRANSCRIPT_CHARS
    assert transcript.endswith("…")


def test_the_ceiling_is_the_media_boundarys_own_constant():
    assert MAX_TRANSCRIPT_CHARS == media_service.MAX_STT_CHARS


# ── 6. Failure classification ──────────────────────────────────────────


@pytest.mark.parametrize(
    "status,expected",
    [
        (401, FAILURE_AUTH),
        (403, FAILURE_FORBIDDEN),
        (404, FAILURE_REJECTION),
        (422, FAILURE_REJECTION),
        (429, FAILURE_RATE_LIMIT),
        (500, FAILURE_SERVER),
        (503, FAILURE_SERVER),
    ],
)
def test_http_statuses_are_classified(monkeypatch, status, expected):
    script(monkeypatch, create=status)

    with pytest.raises(MediaError) as caught:
        _engine().transcribe(OGG_OPUS)

    assert adapter.failure_class_of(caught.value) == expected


def test_a_400_naming_the_audio_is_an_unsupported_audio_failure(monkeypatch):
    script(monkeypatch, create=(400, {"detail": "unsupported audio format"}))

    with pytest.raises(MediaError) as caught:
        _engine().transcribe(OGG_OPUS)

    assert adapter.failure_class_of(caught.value) == FAILURE_UNSUPPORTED_AUDIO


def test_a_400_naming_the_operating_point_is_an_unsupported_model_failure(monkeypatch):
    script(monkeypatch, create=(400, {"detail": "unknown operating_point"}))

    with pytest.raises(MediaError) as caught:
        _engine().transcribe(OGG_OPUS)

    assert adapter.failure_class_of(caught.value) == FAILURE_UNSUPPORTED_MODEL


def test_any_other_400_is_an_invalid_request_failure(monkeypatch):
    script(monkeypatch, create=(400, {"detail": "transcription_config is required"}))

    with pytest.raises(MediaError) as caught:
        _engine().transcribe(OGG_OPUS)

    assert adapter.failure_class_of(caught.value) == FAILURE_INVALID_REQUEST


def test_the_failure_carries_its_own_http_status(monkeypatch):
    script(monkeypatch, create=401)

    with pytest.raises(MediaError) as caught:
        _engine().transcribe(OGG_OPUS)

    assert getattr(caught.value, "http_status", 0) == 401


def test_a_timeout_is_classified_with_its_socket_phase(monkeypatch):
    script(monkeypatch, create=httpx.ConnectTimeout("scripted"))

    with pytest.raises(MediaError) as caught:
        _engine().transcribe(OGG_OPUS)

    assert adapter.failure_class_of(caught.value) == FAILURE_TIMEOUT
    assert getattr(caught.value, "phase", "") == "connect"


def test_a_transport_failure_is_classified(monkeypatch):
    script(monkeypatch, create=httpx.ConnectError("scripted"))

    with pytest.raises(MediaError) as caught:
        _engine().transcribe(OGG_OPUS)

    assert adapter.failure_class_of(caught.value) == FAILURE_TRANSPORT


def test_a_failed_status_read_is_classified(monkeypatch):
    script(monkeypatch, statuses=[httpx.ReadError("scripted")])

    with pytest.raises(MediaError) as caught:
        _engine().transcribe(OGG_OPUS)

    assert adapter.failure_class_of(caught.value) == FAILURE_TRANSPORT


def test_a_failed_transcript_read_is_classified(monkeypatch):
    script(monkeypatch, transcript=httpx.ReadError("scripted"))

    with pytest.raises(MediaError) as caught:
        _engine().transcribe(OGG_OPUS)

    assert adapter.failure_class_of(caught.value) == FAILURE_TRANSPORT


def test_a_transcript_status_error_is_classified(monkeypatch):
    script(monkeypatch, transcript=500)

    with pytest.raises(MediaError) as caught:
        _engine().transcribe(OGG_OPUS)

    assert adapter.failure_class_of(caught.value) == FAILURE_SERVER


# ── 7. Bounded retry, bounded polling and the operation deadline ───────


def test_a_transient_create_failure_is_retried_once(monkeypatch):
    transport = script(monkeypatch, create=[500, None])

    transcript = _engine().transcribe(OGG_OPUS)

    assert transcript == TRANSCRIPT
    assert transport.legs.count("create") == 2


def test_a_deterministic_create_failure_is_never_re_sent(monkeypatch):
    transport = script(monkeypatch, create=401)

    with pytest.raises(MediaError):
        _engine().transcribe(OGG_OPUS)

    assert transport.legs.count("create") == 1


def test_the_single_pass_route_stops_at_the_attempt_ceiling(monkeypatch):
    transport = script(monkeypatch, create=500)

    with pytest.raises(MediaError) as caught:
        _engine().transcribe(OGG_OPUS)

    assert transport.legs.count("create") == MAX_ATTEMPTS
    assert adapter.failure_class_of(caught.value) == FAILURE_SERVER


def test_a_transient_status_read_repeats_the_read_not_the_job(monkeypatch):
    transport = script(
        monkeypatch,
        statuses=[httpx.ReadError("scripted")] * MAX_POLL_FAILURES + ["done"],
    )

    assert _engine().transcribe(OGG_OPUS) == TRANSCRIPT
    assert transport.legs.count("create") == 1


def test_the_status_read_retries_are_bounded(monkeypatch):
    transport = script(monkeypatch, statuses=[httpx.ReadError("scripted")] * 20)

    with pytest.raises(MediaError) as caught:
        _engine().transcribe(OGG_OPUS)

    assert transport.legs.count("status") == MAX_POLL_FAILURES + 1
    assert transport.legs.count("create") == 1
    assert adapter.failure_class_of(caught.value) == FAILURE_TRANSPORT


def test_an_unfinished_job_hits_the_deadline_honestly(monkeypatch):
    transport = script(monkeypatch, statuses=["running"])
    monkeypatch.setattr(adapter, "OPERATION_DEADLINE_S", 0.0)

    with pytest.raises(MediaError) as caught:
        _engine().transcribe(OGG_OPUS)

    assert adapter.failure_class_of(caught.value) == FAILURE_DEADLINE
    assert "transcript" not in transport.legs


def test_the_operation_deadline_is_inside_the_boundarys_own_bound():
    assert 0 < OPERATION_DEADLINE_S < media_service.STT_TIMEOUT_S


def test_a_spent_deadline_starts_no_request(monkeypatch):
    transport = script(monkeypatch)
    monkeypatch.setattr(adapter, "OPERATION_DEADLINE_S", 0.0)

    with pytest.raises(MediaError) as caught:
        _engine().transcribe(OGG_OPUS)

    assert adapter.failure_class_of(caught.value) == FAILURE_DEADLINE
    assert transport.requests == []


def test_the_deadline_is_re_derived_for_every_leg(monkeypatch):
    """A poll never inherits the previous leg's bound."""
    transport = script(monkeypatch, statuses=["running", "done"])

    _engine().transcribe(OGG_OPUS)

    timeouts = [timeout for timeout in transport.timeouts if timeout is not None]
    assert len(timeouts) == len(transport.requests)
    for timeout in timeouts:
        assert isinstance(timeout, httpx.Timeout)
        assert 0 < float(timeout.read) <= OPERATION_DEADLINE_S


# ── 8. Recognition passes stay separate from transport retries ─────────


def test_passes_default_to_one(monkeypatch):
    transport = script(monkeypatch)

    _engine().transcribe(OGG_OPUS)

    assert transport.legs.count("create") == 1


def test_multi_pass_runs_one_job_per_pass_and_reconciles(monkeypatch):
    transport = script(monkeypatch)

    transcript = _engine(passes=3).transcribe(OGG_OPUS)

    assert transcript == TRANSCRIPT
    assert transport.legs.count("create") == 3


def test_the_pass_count_is_clamped_to_the_existing_bound():
    assert _engine(passes=99).passes == 3
    assert _engine(passes=0).passes == 1
    assert _engine(passes="x").passes == 1


def test_a_deterministic_pass_failure_stops_the_loop(monkeypatch):
    transport = script(monkeypatch, statuses=["rejected"])

    with pytest.raises(MediaError) as caught:
        _engine(passes=3).transcribe(OGG_OPUS)

    assert adapter.failure_class_of(caught.value) == FAILURE_REJECTION
    assert transport.legs.count("create") == 1


# ── 9. Context isolation and secret hygiene ────────────────────────────


def test_the_engine_holds_no_owner_or_telegram_state():
    assert set(SpeechmaticsBatchEngine.__slots__) == {
        "_api_key", "_model", "_operating_point", "_language", "_passes",
        "_base_url", "_key_env_var", "_last_failure_class",
    }


def test_nothing_but_the_audio_and_the_settings_can_reach_the_provider(monkeypatch):
    transport = script(monkeypatch)

    _engine(language="fa-IR").transcribe(OGG_OPUS)

    blob = b"".join(request.content for request in transport.requests)
    for leak in (b"owner", b"chat_id", b"message_id", b"@someone",
                 b"secret caption", b"earlier turn", b"memory"):
        assert leak not in blob


def test_the_request_carries_no_telegram_identifier(monkeypatch):
    transport = script(monkeypatch)

    _engine().transcribe(OGG_OPUS)

    for request in transport.requests:
        assert "chat" not in str(request.url)
        assert "message" not in str(request.url)
        for header, value in request.headers.items():
            assert "@" not in value


def test_the_credential_is_never_logged(monkeypatch, caplog):
    script(monkeypatch)

    with caplog.at_level(logging.DEBUG):
        _engine().transcribe(OGG_OPUS)

    assert API_KEY not in caplog.text
    assert "SPEECHMATICS_STT_ENGINE" in caplog.text


def test_the_transcript_is_never_logged(monkeypatch, caplog):
    script(monkeypatch)

    with caplog.at_level(logging.DEBUG):
        _engine().transcribe(OGG_OPUS)

    assert TRANSCRIPT not in caplog.text


def test_the_trace_reports_only_bounded_operational_metadata(monkeypatch, caplog):
    script(monkeypatch)

    with caplog.at_level(logging.DEBUG):
        _engine().transcribe(OGG_OPUS)

    assert "provider=speechmatics" in caplog.text
    assert "endpoint=/jobs" in caplog.text
    assert f"chars={len(TRANSCRIPT)}" in caplog.text
    assert "status=ok" in caplog.text


def test_a_failure_logs_its_bounded_class(monkeypatch, caplog):
    script(monkeypatch, create=401)

    with caplog.at_level(logging.DEBUG):
        with pytest.raises(MediaError):
            _engine().transcribe(OGG_OPUS)

    assert API_KEY not in caplog.text
    assert "failure_class=auth:http=401" in caplog.text


def test_an_error_body_cannot_echo_the_credential(monkeypatch):
    script(monkeypatch, create=(401, {"detail": f"bad key {API_KEY}"}))

    with pytest.raises(MediaError) as caught:
        _engine().transcribe(OGG_OPUS)

    assert API_KEY not in str(caught.value)
    assert "***" in str(caught.value)


def test_the_adapter_imports_no_telegram_handler_or_provider_layer():
    tree = inspect.getsource(adapter)

    for forbidden in ("telethon", "backend.bot", "backend.helper", "backend.db",
                      "backend.runtime", "backend.ai.providers", "backend.ai.engine"):
        assert f"import {forbidden}" not in tree
        assert f"from {forbidden}" not in tree


def test_the_adapter_never_uses_a_chat_completion_path():
    source = inspect.getsource(adapter)

    assert "chat/completions" not in source
    assert "completions/" not in source
    assert "ProviderManager(" not in source
    assert "/audio/transcriptions" not in source


def test_the_adapter_does_not_depend_on_the_probe_or_the_factory():
    """The dependency direction stays one-way: engine ← factory ← probe."""
    source = inspect.getsource(adapter)

    assert "stt_provider_probe" not in source
    assert "stt_engine_factory" not in source


def test_no_speechmatics_client_package_is_imported():
    """The existing httpx stack only — no extra dependency."""
    source = inspect.getsource(adapter)

    assert "import httpx" in source
    assert "speechmatics.batch" not in source
    assert "batch_client" not in source
