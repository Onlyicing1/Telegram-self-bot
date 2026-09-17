"""
Media Processing — the BOUNDED STT TRANSPORT of the explicit Voice/Audio path.

`INVESTIGATION.md` §20 records two facts about the previous STT engine that this
suite pins the replacement of:

  1. the engine's bound was a PER-SOCKET-PHASE ``httpx.Client(timeout=40.0)`` — not
     a bound on the operation, so a live incident could only report "timed out
     after 40s" without knowing which leg or which socket phase was still open;
  2. every failure collapsed into that one sentence, so an upload failure, an
     interaction stall, an HTTP rejection and a spent deadline were
     indistinguishable afterwards.

The engine now carries ONE explicit operation deadline (``inside``
``media_service.STT_TIMEOUT_S``), derives every request timeout from what is left
of it, CLASSIFIES each failure, and spends at most ONE bounded second attempt —
only for transient conditions, and only with budget left. This file pins:

  * the transport plan (documented ``uri`` form first, inline only as the bounded
    fallback, never above the inline budget);
  * the deadline arithmetic and the derived phase bounds;
  * the failure classes and the recorded socket PHASE;
  * the bounded retry/fallback rules (transient only, at most two attempts, never
    without budget, never a loop, never two attempts in flight);
  * cleanup (per attempt, bounded, never masking the outcome);
  * observability (per-leg lines, no transcript / audio / credential / Telegram
    metadata) and the per-leg/per-operation timings;
  * event-loop safety and bounded concurrency.

The HTTP boundary is a scripted ``httpx`` transport: no test needs a credential,
no byte leaves the process, and — as in every media suite here — nothing below
proves anything about recognition QUALITY. That requires the owner's live
comparison, and is out of this file's scope.
"""
from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import struct
import threading
import time
import wave
from typing import Any

import httpx
import pytest

from backend.services import gemini_media_engine as engine_module
from backend.services import media_service
from backend.services.gemini_media_engine import (
    DEFAULT_MEDIA_MODEL,
    DEDICATED_TRANSCRIPTION_MODEL,
    GEMINI_API_BASE,
    INTERACTIONS_ENDPOINT,
    STT_LANGUAGE_ENV_VAR,
    STT_MODEL_ENV_VAR,
    GeminiMediaEngine,
    _dedicated_transports,
    _failure_field,
    _required_budget,
    _request_timeout,
    _timeout_phase,
)
from backend.services.media_service import MediaError

API_KEY = "reliability-suite-key-not-a-credential"
TRANSCRIPT = "transcript-that-must-never-be-logged-3f7a"
_FILE_NAME = "files/reliability1"
_FILE_URI = f"{GEMINI_API_BASE}/{_FILE_NAME}"
_UPLOAD_URL = "https://upload.example.invalid/files?upload_id=reliability"


# ── Scripted transport: per-LEG control, zero network I/O ──


def _interaction(text: str = TRANSCRIPT, status: str = "completed") -> dict[str, Any]:
    content: list[dict[str, Any]] = []
    if text:
        content.append({"type": "text", "text": text})
    return {
        "id": "interactions/reliability",
        "status": status,
        "steps": [{"id": "s1", "type": "model_output", "content": content}],
    }


class _Script(httpx.BaseTransport):
    """A recording transport with one scriptable hook per LEG.

    A hook is one of:

      * ``None``                  — the documented success response for that leg;
      * an ``Exception`` instance  — raised (transport/timeout simulation);
      * an ``int``                 — that HTTP status, with a JSON error body;
      * a ``dict``                 — HTTP 200 with that JSON body;
      * ``bytes``                  — HTTP 200 with that raw (unreadable) body;
      * a ``list``                 — the N-th value, last one repeating.

    ``delays`` injects a real sleep per leg, so the tests can measure that the
    remaining budget (and therefore the derived timeouts) shrink as legs run.
    """

    def __init__(
        self,
        *,
        upload_start: Any = None,
        upload_finalize: Any = None,
        file_status: Any = None,
        interaction: Any = None,
        generate: Any = None,
        delete: Any = None,
        delays: dict[str, float] | None = None,
        transcript: str = TRANSCRIPT,
        file_state: str = "ACTIVE",
    ) -> None:
        self.hooks = {
            "upload_start": upload_start,
            "upload_finalize": upload_finalize,
            "file_status": file_status,
            "interaction": interaction,
            "generate": generate,
            "delete": delete,
        }
        self.delays = dict(delays or {})
        self.transcript = transcript
        self.file_state = file_state
        self.requests: list[httpx.Request] = []
        self.legs: list[str] = []
        self.threads: list[str] = []
        self.deleted: list[str] = []
        self.timeouts: list[Any] = []
        self._calls: dict[str, int] = {}

    # ── transport protocol ──

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        self.threads.append(threading.current_thread().name)
        leg = self._leg(request)
        self.legs.append(leg)
        delay = self.delays.get(leg, 0.0)
        if delay:
            time.sleep(delay)
        index = self._calls.get(leg, 0)
        self._calls[leg] = index + 1
        return self._respond(leg, self._pick(self.hooks.get(leg), index), request)

    def _leg(self, request: httpx.Request) -> str:
        url = str(request.url)
        if request.method == "POST" and url.endswith("/upload/v1beta/files"):
            return "upload_start"
        if request.method == "POST" and url == _UPLOAD_URL:
            return "upload_finalize"
        if request.method == "DELETE":
            return "delete"
        if request.method == "GET":
            return "file_status"
        if url == INTERACTIONS_ENDPOINT:
            return "interaction"
        if url.endswith(":generateContent"):
            return "generate"
        return "unknown"   # pragma: no cover - the engine has no other endpoint

    def _pick(self, value: Any, index: int) -> Any:
        if isinstance(value, list):
            return value[min(index, len(value) - 1)]
        return value

    def _respond(self, leg: str, value: Any, request: httpx.Request) -> httpx.Response:
        if isinstance(value, Exception):
            raise value
        if isinstance(value, int) and value >= 400:
            return httpx.Response(
                value,
                json={"error": {"message": "scripted failure", "code": value}},
                request=request,
            )
        if isinstance(value, bytes):
            return httpx.Response(200, content=value, request=request)
        if isinstance(value, dict):
            return httpx.Response(200, json=value, request=request)
        if leg == "delete":
            self.deleted.append(str(request.url))
            return httpx.Response(200, json={}, request=request)
        if leg == "upload_start":
            return httpx.Response(
                200, headers={"x-goog-upload-url": _UPLOAD_URL}, request=request,
            )
        if leg == "upload_finalize":
            return httpx.Response(
                200,
                json={"file": {"name": _FILE_NAME, "uri": _FILE_URI,
                               "state": self.file_state}},
                request=request,
            )
        if leg == "file_status":
            return httpx.Response(
                200,
                json={"name": _FILE_NAME, "uri": _FILE_URI, "state": self.file_state},
                request=request,
            )
        if leg == "interaction":
            return httpx.Response(200, json=_interaction(self.transcript), request=request)
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {"content": {"parts": [{"text": self.transcript}]},
                     "finishReason": "STOP"}
                ]
            },
            request=request,
        )

    # ── helpers ──

    def leg_requests(self, leg: str) -> list[httpx.Request]:
        return [r for r, name in zip(self.requests, self.legs) if name == leg]


@pytest.fixture
def script(monkeypatch):
    """Install a scripted transport, recording the timeout passed to each client."""

    def _install(**kwargs: Any) -> _Script:
        transport = _Script(**kwargs)
        real_client = httpx.Client

        def factory(*args: Any, **inner: Any) -> httpx.Client:
            inner["transport"] = transport
            transport.timeouts.append(inner.get("timeout"))
            return real_client(*args, **inner)

        monkeypatch.setattr(httpx, "Client", factory)
        return transport

    return _install


# ── Real container fixtures (stdlib only) ──


def _opus_head() -> bytes:
    return (
        b"OpusHead" + bytes([1, 1]) + (312).to_bytes(2, "little")
        + (48_000).to_bytes(4, "little") + (0).to_bytes(2, "little") + bytes([0])
    )


def _ogg_page(header_type: int, granule: int, sequence: int, packet: bytes) -> bytes:
    lacing: list[int] = []
    remaining = len(packet)
    while remaining >= 255:
        lacing.append(255)
        remaining -= 255
    lacing.append(remaining)
    return (
        b"OggS" + bytes([0, header_type]) + granule.to_bytes(8, "little")
        + (1).to_bytes(4, "little") + sequence.to_bytes(4, "little")
        + (0).to_bytes(4, "little") + bytes([len(lacing)]) + bytes(lacing) + packet
    )


def _ogg_opus(duration_s: float = 2.0) -> bytes:
    granule = int(duration_s * 48_000)
    return (
        _ogg_page(0x02, 0, 0, _opus_head())
        + _ogg_page(0x04, granule, 1, b"\x00" * 40)
    )


def _wav(duration_s: float = 1.0, *, channels: int = 1, sample_rate: int = 16_000) -> bytes:
    buffer = io.BytesIO()
    handle = wave.open(buffer, "wb")
    handle.setnchannels(channels)
    handle.setsampwidth(2)
    handle.setframerate(sample_rate)
    handle.writeframes(b"\x00\x00" * int(duration_s * sample_rate) * channels)
    handle.close()
    return buffer.getvalue()


def _silence_wav() -> bytes:
    """A valid, readable container with NO speech in it."""
    return _wav(0.2)


def _large_wav() -> bytes:
    """Inside the STT bound, past the inline budget ⇒ the Files API is required."""
    payload = _wav(290.0, channels=2, sample_rate=16_000)
    assert engine_module.INLINE_PAYLOAD_MAX_BYTES < len(payload) <= media_service.MAX_STT_INPUT_BYTES
    return payload


def _dedicated(language: str = "") -> GeminiMediaEngine:
    return GeminiMediaEngine(
        API_KEY, DEFAULT_MEDIA_MODEL, key_env_var="AI_GEMINI_API_KEY",
        stt_model=DEDICATED_TRANSCRIPTION_MODEL, stt_language=language,
    )


def _general() -> GeminiMediaEngine:
    """No dedicated model configured: the general media route answers STT."""
    return GeminiMediaEngine(API_KEY, DEFAULT_MEDIA_MODEL, key_env_var="AI_GEMINI_API_KEY")


def _body(request: httpx.Request) -> dict[str, Any]:
    return json.loads(request.content.decode("utf-8"))


def _item(request: httpx.Request) -> dict[str, Any]:
    body = _body(request)
    assert len(body["input"]) == 1, "one audio item only"
    return body["input"][0]


@pytest.fixture(autouse=True)
def _reset_engines_and_env(monkeypatch):
    media_service.set_ocr_engine(None)
    media_service.set_stt_engine(None)
    for name in (
        "AI_GEMINI_API_KEY", "GEMINI_API_KEY", "AI_GEMINI_MEDIA_MODEL", "AI_GEMINI_MODEL",
        STT_MODEL_ENV_VAR, STT_LANGUAGE_ENV_VAR,
    ):
        monkeypatch.delenv(name, raising=False)
    yield
    media_service.set_ocr_engine(None)
    media_service.set_stt_engine(None)


# ── 1. The transport plan: documented, deterministic, bounded ──


def test_the_dedicated_plan_uses_the_documented_uri_form_first():
    """The documentation shows only ``uri`` for this model ⇒ it is primary."""
    assert _dedicated_transports(1) == ("uri", "inline")
    assert _dedicated_transports(engine_module.INLINE_PAYLOAD_MAX_BYTES) == ("uri", "inline")


def test_the_plan_never_exceeds_the_inline_budget_for_the_inline_form():
    """Past the documented inline budget the fallback repeats the URI form."""
    over = engine_module.INLINE_PAYLOAD_MAX_BYTES + 1
    assert _dedicated_transports(over) == ("uri", "uri")


@pytest.mark.parametrize("size", [1, 4096, engine_module.INLINE_PAYLOAD_MAX_BYTES,
                                  engine_module.INLINE_PAYLOAD_MAX_BYTES + 1, 10 ** 7])
def test_the_plan_is_bounded_and_never_a_loop(size):
    plan = _dedicated_transports(size)
    assert len(plan) == engine_module.STT_MAX_ATTEMPTS == 2
    assert plan[0] == "uri", "the documented representation is always the first attempt"


def test_the_dedicated_route_starts_with_the_uri_form_for_a_tiny_note(script):
    transport = script()

    _dedicated().transcribe(_ogg_opus())

    first = transport.leg_requests("interaction")[0]
    item = _item(first)
    assert item["type"] == "audio"
    assert item["uri"] == _FILE_URI and "data" not in item
    assert item["mime_type"] == "audio/ogg"
    # The bytes that were uploaded are the validated bytes, unchanged.
    assert transport.leg_requests("upload_finalize")[0].content == _ogg_opus()
    assert len(transport.leg_requests("upload_start")) == 1
    assert transport.leg_requests("upload_start")[0].headers["X-Goog-Upload-Command"] == "start"
    assert transport.deleted == [f"{GEMINI_API_BASE}/{_FILE_NAME}"]


def test_the_inline_form_is_never_used_for_the_first_attempt(script):
    transport = script()

    _dedicated().transcribe(_wav())

    assert "data" not in _item(transport.leg_requests("interaction")[0])
    assert len(transport.leg_requests("upload_start")) == 1


# ── 2. The deadline, and the phase bounds derived from it ──


def test_the_derived_phase_bounds_come_from_the_remaining_budget():
    generous = _request_timeout(30.0)
    assert generous.connect == engine_module.STT_CONNECT_TIMEOUT_S
    assert generous.write == engine_module.STT_WRITE_TIMEOUT_S
    assert generous.read == 30.0
    assert generous.pool == engine_module.STT_CONNECT_TIMEOUT_S

    tight = _request_timeout(2.0)
    assert (tight.connect, tight.read, tight.write, tight.pool) == (2.0, 2.0, 2.0, 2.0)


def test_the_worst_case_still_fits_inside_the_boundary_bound():
    assert engine_module.STT_OPERATION_DEADLINE_S < media_service.STT_TIMEOUT_S
    assert engine_module.STT_CLEANUP_TIMEOUT_S < media_service.STT_TIMEOUT_S
    assert (
        engine_module.STT_OPERATION_DEADLINE_S + engine_module.STT_CLEANUP_TIMEOUT_S
        < media_service.STT_TIMEOUT_S
    )
    assert engine_module.STT_MIN_ATTEMPT_S < engine_module.STT_OPERATION_DEADLINE_S


def test_a_spent_budget_is_its_own_failure_class():
    with pytest.raises(MediaError) as exc:
        _required_budget(time.monotonic() - 1.0, "speech-to-text upload", 45.0)

    assert exc.value.failure_class == engine_module.FAILURE_DEADLINE
    assert "budget" in str(exc.value)


def test_a_budget_still_in_hand_is_returned_unchanged():
    left = _required_budget(time.monotonic() + 12.0, "speech-to-text request", 45.0)

    assert 11.0 < left <= 12.0


@pytest.mark.asyncio
async def test_a_spent_operation_deadline_sends_nothing_and_fails_closed(
    script, monkeypatch,
):
    transport = script()
    monkeypatch.setattr(engine_module, "STT_OPERATION_DEADLINE_S", 0.0)

    with pytest.raises(MediaError) as exc:
        _dedicated().transcribe(_ogg_opus())

    assert exc.value.failure_class == engine_module.FAILURE_DEADLINE
    assert transport.requests == [], "a spent deadline must not start a request"


@pytest.mark.asyncio
async def test_leg_timeouts_shrink_with_the_remaining_budget_and_stay_bounded(script):
    """A slow leg consumes the budget; the NEXT leg's bound is smaller.

    This is the property the previous per-phase ``timeout=40.0`` could not have:
    four equal, independent bounds with no notion of the operation.
    """
    transport = script(delays={"upload_finalize": 0.25, "interaction": 0.15})

    started = time.monotonic()
    text = _dedicated().transcribe(_ogg_opus())
    elapsed = time.monotonic() - started

    assert text == TRANSCRIPT
    assert elapsed < 5.0, "the operation is bounded and short"
    reads = [t.read for t in transport.timeouts]
    assert len(reads) == len(transport.requests), "one client per leg"
    assert reads[0] <= engine_module.STT_OPERATION_DEADLINE_S
    for earlier, later in zip(reads, reads[1:]):
        assert later <= earlier, "each leg's bound is derived from what is left"
    for timeout in transport.timeouts:
        assert timeout.connect <= engine_module.STT_CONNECT_TIMEOUT_S
        assert timeout.write <= engine_module.STT_WRITE_TIMEOUT_S


# ── 3. Failure classes and the recorded socket phase ──


def test_every_failure_class_token_is_distinct_and_bounded():
    tokens = [
        engine_module.FAILURE_UPLOAD, engine_module.FAILURE_UPLOAD_TIMEOUT,
        engine_module.FAILURE_FILE_PROCESSING, engine_module.FAILURE_INTERACTION,
        engine_module.FAILURE_INTERACTION_TIMEOUT, engine_module.FAILURE_REQUEST,
        engine_module.FAILURE_REQUEST_TIMEOUT, engine_module.FAILURE_HTTP,
        engine_module.FAILURE_MALFORMED, engine_module.FAILURE_TRANSPORT,
        engine_module.FAILURE_DEADLINE,
    ]
    assert len(set(tokens)) == len(tokens)
    for token in tokens:
        assert token and " " not in token and token.islower()


@pytest.mark.parametrize("error,phase", [
    (httpx.ConnectTimeout("x"), "connect"),
    (httpx.ReadTimeout("x"), "read"),
    (httpx.WriteTimeout("x"), "write"),
    (httpx.PoolTimeout("x"), "pool"),
    (httpx.TimeoutException("x"), "request"),
])
def test_the_expired_socket_phase_is_recorded(error, phase):
    assert _timeout_phase(error) == phase


def test_the_failure_field_carries_the_class_and_its_detail():
    assert _failure_field(engine_module._error("m", engine_module.FAILURE_UPLOAD)) == "upload_failed"
    timed_out = engine_module._error(
        "m", engine_module.FAILURE_INTERACTION_TIMEOUT, retryable=True, phase="read",
    )
    assert _failure_field(timed_out) == "interaction_timeout:read"
    rejected = engine_module._error("m", engine_module.FAILURE_HTTP, http_status=429)
    assert _failure_field(rejected) == "http_rejection:http=429"


@pytest.mark.asyncio
async def test_an_upload_timeout_records_the_connect_phase(script, caplog):
    # A persistent upload timeout (payload past the inline budget, so the bounded
    # second attempt must upload again) is classified with its socket phase.
    transport = script(upload_start=httpx.ConnectTimeout("scripted connect timeout"))

    with caplog.at_level("INFO", logger="backend.services.gemini_media_engine"):
        with pytest.raises(MediaError) as exc:
            _dedicated().transcribe(_large_wav())

    assert exc.value.failure_class == engine_module.FAILURE_UPLOAD_TIMEOUT
    assert exc.value.phase == "connect"
    assert "connect phase" in str(exc.value)
    assert len(transport.leg_requests("upload_start")) == engine_module.STT_MAX_ATTEMPTS
    assert transport.leg_requests("interaction") == []
    line = next(
        r.getMessage() for r in caplog.records
        if r.getMessage().startswith("GEMINI_MEDIA_ENGINE kind=")
    )
    assert "failure_class=upload_timeout:connect" in line
    assert "attempts=2" in line


@pytest.mark.asyncio
async def test_an_interaction_stall_is_classified_with_the_read_phase(script, caplog):
    transport = script(interaction=httpx.ReadTimeout("scripted read timeout"))

    with caplog.at_level("INFO", logger="backend.services.gemini_media_engine"):
        with pytest.raises(MediaError) as exc:
            _dedicated().transcribe(_ogg_opus())

    assert exc.value.failure_class == engine_module.FAILURE_INTERACTION_TIMEOUT
    assert exc.value.phase == "read"
    assert "read phase" in str(exc.value)
    assert len(transport.leg_requests("interaction")) == engine_module.STT_MAX_ATTEMPTS
    line = next(
        r.getMessage() for r in caplog.records
        if r.getMessage().startswith("GEMINI_MEDIA_ENGINE kind=")
    )
    assert "failure_class=interaction_timeout:read" in line


@pytest.mark.asyncio
async def test_a_file_that_never_becomes_ready_fails_closed_with_its_own_class(
    script, monkeypatch,
):
    transport = script(file_state="PROCESSING")
    monkeypatch.setattr(engine_module, "_FILE_READY_DELAY_S", 0.0)

    with pytest.raises(MediaError) as exc:
        _dedicated().transcribe(_ogg_opus())

    assert exc.value.failure_class == engine_module.FAILURE_FILE_PROCESSING
    assert "processing" in str(exc.value)
    # Bounded readiness checks, and the interaction never ran.
    assert len(transport.leg_requests("file_status")) == engine_module._FILE_READY_ATTEMPTS
    assert transport.leg_requests("interaction") == []
    assert transport.deleted == [f"{GEMINI_API_BASE}/{_FILE_NAME}"]


@pytest.mark.asyncio
async def test_a_failed_file_processing_state_fails_closed(script):
    transport = script(file_state="FAILED")

    with pytest.raises(MediaError) as exc:
        _dedicated().transcribe(_ogg_opus())

    assert exc.value.failure_class == engine_module.FAILURE_FILE_PROCESSING
    assert transport.leg_requests("interaction") == []


@pytest.mark.asyncio
async def test_an_http_rejection_carries_its_status_and_is_deterministic(script):
    transport = script(interaction=400)

    with pytest.raises(MediaError) as exc:
        _dedicated().transcribe(_ogg_opus())

    assert exc.value.failure_class == engine_module.FAILURE_HTTP
    assert exc.value.http_status == 400
    assert exc.value.retryable is False
    assert len(transport.leg_requests("interaction")) == 1, "a 4xx is never re-sent"


@pytest.mark.asyncio
async def test_a_rate_limit_is_transient_and_gets_exactly_one_more_attempt(script):
    transport = script(interaction=[429, 200])

    assert _dedicated().transcribe(_ogg_opus()) == TRANSCRIPT

    assert len(transport.leg_requests("interaction")) == 2
    assert transport.leg_requests("interaction")[1].content  # the retry carried a real body


@pytest.mark.asyncio
async def test_a_malformed_response_is_never_retried(script):
    transport = script(interaction=b"<html>not json</html>")

    with pytest.raises(MediaError) as exc:
        _dedicated().transcribe(_ogg_opus())

    assert exc.value.failure_class == engine_module.FAILURE_MALFORMED
    assert "unreadable" in str(exc.value)
    assert len(transport.leg_requests("interaction")) == 1


@pytest.mark.asyncio
async def test_a_non_completed_interaction_is_never_retried(script):
    transport = script(interaction={"id": "x", "status": "failed", "steps": []})

    with pytest.raises(MediaError) as exc:
        _dedicated().transcribe(_ogg_opus())

    assert exc.value.failure_class == engine_module.FAILURE_MALFORMED
    assert len(transport.leg_requests("interaction")) == 1


@pytest.mark.asyncio
async def test_an_empty_transcript_is_honest_and_never_retried(script):
    transport = script(interaction=_interaction(""))

    assert _dedicated().transcribe(_ogg_opus()) == ""
    assert len(transport.leg_requests("interaction")) == 1, "empty is not a failure"


@pytest.mark.asyncio
async def test_an_unsupported_container_is_refused_before_any_request(script):
    """The accepted-format list is not broadened: an MP3 container is refused
    locally, with an honest error, and nothing is transmitted."""
    transport = script()

    with pytest.raises(MediaError) as exc:
        _dedicated().transcribe(b"ID3\x03\x00\x00\x00\x00\x00\x00" + b"\x00" * 64)

    assert "not a container Gemini can read" in str(exc.value)
    assert transport.requests == []


@pytest.mark.asyncio
async def test_an_empty_payload_never_reaches_the_network(script):
    transport = script()

    assert _dedicated().transcribe(b"") == ""
    assert transport.requests == []


# ── 4. The bounded retry / fallback rules ──


@pytest.mark.asyncio
async def test_a_transient_first_attempt_uses_the_inline_fallback_once(script):
    transport = script(interaction=[500, 200])

    text = _dedicated().transcribe(_ogg_opus())

    assert text == TRANSCRIPT
    interactions = transport.leg_requests("interaction")
    assert len(interactions) == 2
    assert "uri" in _item(interactions[0])
    assert "data" in _item(interactions[1]), "the ONE bounded fallback is the inline form"
    assert base64.b64decode(_item(interactions[1])["data"]) == _ogg_opus()
    assert len(transport.leg_requests("upload_start")) == 1, "the fallback needs no upload"


@pytest.mark.asyncio
async def test_a_persistent_transient_failure_never_exceeds_two_attempts(script):
    transport = script(interaction=503)

    with pytest.raises(MediaError) as exc:
        _dedicated().transcribe(_ogg_opus())

    assert exc.value.failure_class == engine_module.FAILURE_HTTP
    assert len(transport.leg_requests("interaction")) == engine_module.STT_MAX_ATTEMPTS
    assert len(transport.leg_requests("upload_start")) == 1


@pytest.mark.asyncio
async def test_a_deterministic_upload_failure_never_falls_back(script):
    transport = script(upload_start=401)

    with pytest.raises(MediaError) as exc:
        _dedicated().transcribe(_ogg_opus())

    assert exc.value.failure_class == engine_module.FAILURE_HTTP
    assert exc.value.http_status == 401
    assert len(transport.leg_requests("upload_start")) == 1
    assert transport.leg_requests("interaction") == []
    assert transport.deleted == [], "no file existed yet"


@pytest.mark.asyncio
async def test_the_fallback_is_skipped_when_the_budget_cannot_cover_it(script, monkeypatch):
    """A retry runs only with a meaningful budget left — never past the deadline."""
    transport = script(interaction=503)
    monkeypatch.setattr(engine_module, "STT_MIN_ATTEMPT_S", 10 ** 6)

    with pytest.raises(MediaError) as exc:
        _dedicated().transcribe(_ogg_opus())

    assert exc.value.failure_class == engine_module.FAILURE_HTTP
    assert len(transport.leg_requests("interaction")) == 1, "no attempt without budget"


@pytest.mark.asyncio
async def test_attempts_are_sequential_and_never_concurrent(script):
    """One attempt at a time: no two interaction requests are ever in flight."""
    transport = script(interaction=[500, 200])
    order: list[str] = []
    real_handle = transport.handle_request

    def handle(request: httpx.Request) -> httpx.Response:
        order.append(f"start:{transport._leg(request)}")
        response = real_handle(request)
        order.append(f"end:{transport._leg(request)}")
        return response

    transport.handle_request = handle          # type: ignore[method-assign]

    assert _dedicated().transcribe(_ogg_opus()) == TRANSCRIPT
    assert order.count("start:interaction") == 2
    assert order == [
        "start:upload_start", "end:upload_start",
        "start:upload_finalize", "end:upload_finalize",
        "start:interaction", "end:interaction",
        "start:delete", "end:delete",
        "start:interaction", "end:interaction",
    ]


# ── 5. Cleanup ──


@pytest.mark.asyncio
async def test_the_remote_file_is_deleted_before_the_fallback_runs(script):
    transport = script(interaction=[500, 200])

    assert _dedicated().transcribe(_ogg_opus()) == TRANSCRIPT

    assert transport.deleted == [f"{GEMINI_API_BASE}/{_FILE_NAME}"], (
        "the first attempt's file is removed before the second attempt runs"
    )
    delete_index = transport.legs.index("delete")
    second_interaction = [i for i, leg in enumerate(transport.legs) if leg == "interaction"][1]
    assert delete_index < second_interaction


@pytest.mark.asyncio
async def test_a_failed_cleanup_never_masks_the_result(script):
    transport = script(delete=500)

    assert _dedicated().transcribe(_ogg_opus()) == TRANSCRIPT


@pytest.mark.asyncio
async def test_a_failed_cleanup_never_masks_a_failure(script):
    transport = script(interaction=400, delete=500)

    with pytest.raises(MediaError) as exc:
        _dedicated().transcribe(_ogg_opus())

    assert exc.value.failure_class == engine_module.FAILURE_HTTP
    assert "refused" in str(exc.value)


@pytest.mark.asyncio
async def test_the_cleanup_bound_is_its_own_small_timeout(script):
    """Cleanup is bounded on its own — it is not part of the operation deadline."""
    transport = script()

    _dedicated().transcribe(_ogg_opus())

    delete_client = transport.timeouts[transport.legs.index("delete")]
    assert delete_client.read == engine_module.STT_CLEANUP_TIMEOUT_S
    assert engine_module.STT_CLEANUP_TIMEOUT_S < engine_module.STT_OPERATION_DEADLINE_S


# ── 6. Observability: per-leg traces, no content, both routes ──


@pytest.mark.asyncio
async def test_every_leg_is_traced_and_nothing_sensitive_is_ever_logged(
    script, caplog,
):
    transport = script(transcript=TRANSCRIPT)

    with caplog.at_level("INFO", logger="backend.services.gemini_media_engine"):
        _dedicated("fa-IR").transcribe(_ogg_opus())

    messages = [r.getMessage() for r in caplog.records]
    stages = [m for m in messages if "GEMINI_MEDIA_ENGINE_STAGE" in m]
    for stage in ("request_start", "upload_start", "upload_finalize", "interaction"):
        assert any(f"stage={stage}" in m for m in stages), stage
    # Per-leg timing exists, so a stall is attributable after the fact.
    assert any("stage=interaction" in m and "elapsed_ms=" in m for m in stages)
    assert any("stage=upload_finalize" in m and "bytes=" in m for m in stages)

    run = next(m for m in messages if m.startswith("GEMINI_MEDIA_ENGINE kind="))
    for field in (
        "kind=speech-to-text", "engine=GeminiMediaEngine",
        f"model={DEDICATED_TRANSCRIPTION_MODEL}", "transport=interactions",
        "mime=audio/ogg", "bytes=", "language=fa-IR", "mode=verbatim", "chars=",
        "elapsed_ms=", "status=ok", "attempts=1", "deadline_s=45", "failure_class=-",
    ):
        assert field in run, field

    # Never the transcript, the raw audio, the credential or Telegram metadata.
    encoded = base64.b64encode(_ogg_opus()).decode("ascii")
    for message in messages:
        assert TRANSCRIPT not in message
        assert API_KEY not in message
        assert encoded not in message
        assert _FILE_URI not in message
        assert "caption" not in message.lower()
        assert "filename" not in message.lower()


@pytest.mark.asyncio
async def test_a_failed_run_is_traced_once_with_its_outcome(script, caplog):
    transport = script(interaction=400)

    with caplog.at_level("INFO", logger="backend.services.gemini_media_engine"):
        with pytest.raises(MediaError):
            _dedicated().transcribe(_ogg_opus())

    runs = [
        r.getMessage() for r in caplog.records
        if r.getMessage().startswith("GEMINI_MEDIA_ENGINE kind=")
    ]
    assert len(runs) == 1, "one outcome line per operation"
    assert "status=failed" in runs[0]
    assert "failure_class=http_rejection:http=400" in runs[0]


@pytest.mark.asyncio
async def test_the_general_route_keeps_its_own_bounds_and_failure_class(script, caplog):
    transport = script(generate=httpx.ReadTimeout("scripted timeout"))

    with caplog.at_level("INFO", logger="backend.services.gemini_media_engine"):
        with pytest.raises(MediaError) as exc:
            _general().transcribe(_wav())

    assert exc.value.failure_class == engine_module.FAILURE_REQUEST_TIMEOUT
    assert exc.value.phase == "read"
    assert len(transport.leg_requests("generate")) == engine_module.STT_MAX_ATTEMPTS
    run = next(
        r.getMessage() for r in caplog.records
        if r.getMessage().startswith("GEMINI_MEDIA_ENGINE kind=")
    )
    assert "failure_class=request_timeout:read" in run
    assert f"deadline_s={engine_module.STT_OPERATION_DEADLINE_S:g}" in run


@pytest.mark.asyncio
async def test_ocr_is_untouched_by_the_stt_policy(script, caplog):
    """OCR keeps ONE attempt: the bounded retry belongs to the STT route only."""
    transport = script(generate=httpx.ReadTimeout("scripted timeout"))
    engine = _dedicated()

    with pytest.raises(MediaError):
        engine.recognize(_png())

    assert len(transport.leg_requests("generate")) == 1


def _png(size: int = 4) -> bytes:
    import zlib

    def chunk(tag: bytes, payload: bytes) -> bytes:
        body = tag + payload
        return struct.pack(">I", len(payload)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)
    raw = b"".join(b"\x00" + b"\x00\x00\x00" * size for _ in range(size))
    return (
        b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")
    )


# ── 7. Event loop, concurrency, no shared state ──


@pytest.mark.asyncio
async def test_a_slow_dedicated_transcription_never_blocks_the_event_loop(script):
    transport = script(delays={"upload_finalize": 0.2, "interaction": 0.25})
    engine = _dedicated()
    ticks = 0

    async def _ticker() -> None:
        nonlocal ticks
        while True:
            await asyncio.sleep(0.01)
            ticks += 1

    ticker = asyncio.create_task(_ticker())
    try:
        text = await asyncio.to_thread(engine.transcribe, _ogg_opus())
    finally:
        ticker.cancel()
        with pytest.raises(asyncio.CancelledError):
            await ticker

    assert text == TRANSCRIPT
    assert ticks >= 10, "the event loop kept running while the engine worked"
    assert threading.main_thread().name not in transport.threads


@pytest.mark.asyncio
async def test_concurrent_transcriptions_stay_bounded_and_per_operation(script):
    transport = script()
    engine = _dedicated()

    results = await asyncio.gather(
        *[asyncio.to_thread(engine.transcribe, _ogg_opus()) for _ in range(4)]
    )

    assert results == [TRANSCRIPT] * 4
    assert len(transport.leg_requests("interaction")) == 4, "one request per operation"
    assert len(transport.leg_requests("upload_start")) == 4
    assert len(transport.deleted) == 4
    assert len(set(transport.threads)) <= 4, "bounded work, not an unbounded pool"
    assert all(len(_body(r)["input"]) == 1 for r in transport.leg_requests("interaction"))


@pytest.mark.asyncio
async def test_the_engine_owns_no_http_state_between_operations(script):
    transport = script()
    engine = _dedicated()

    engine.transcribe(_ogg_opus())
    engine.transcribe(_ogg_opus())

    # A per-call client: one client per leg, and nothing retained on the engine.
    assert len(transport.timeouts) == len(transport.requests)
    assert not any(
        type(value).__name__ in {"Client", "AsyncClient"}
        and type(value).__module__.startswith("httpx")
        for value in vars(engine_module).values()
    ), "no module-level HTTP client (would be cross-request mutable state)"
    assert set(GeminiMediaEngine.__slots__) == {
        "_api_key", "_model", "_key_env_var", "_stt_model", "_stt_language",
    }


# ── 8. Audio fixtures: the honest boundary between code and recognition ──


@pytest.mark.asyncio
async def test_a_silent_container_is_sent_unchanged_and_an_empty_result_is_honest(script):
    """Silence is a VALID payload: it is transmitted unmodified, and an empty
    transcript stays empty — no fabricated text, no retry."""
    transport = script(interaction=_interaction(""))
    payload = _silence_wav()

    assert _dedicated().transcribe(payload) == ""
    assert transport.leg_requests("upload_finalize")[0].content == payload
    assert len(transport.leg_requests("interaction")) == 1


@pytest.mark.asyncio
async def test_persian_and_code_switching_text_survives_the_engine_unchanged(script):
    """The engine is value-preserving; nothing here proves recognition quality."""
    mixed = "سلام، این یک تست است. Then I said: code switching OK."
    zwnj = "می\u200cکند"
    transport = script(interaction=_interaction(f"{zwnj} {mixed}"))

    text = _dedicated("fa-IR").transcribe(_ogg_opus())

    assert text == f"{zwnj} {mixed}"
    assert "\u200c" in text and text.count("\u200c") == 1


@pytest.mark.asyncio
async def test_a_truncated_container_is_still_sent_byte_identical(script):
    """A short/odd-but-valid container is not repaired, padded or re-encoded."""
    payload = _wav(0.05)
    transport = script()

    _dedicated().transcribe(payload)

    assert transport.leg_requests("upload_finalize")[0].content == payload
    assert _body(transport.leg_requests("upload_start")[0]) == {
        "file": {"display_name": "lifeos-media"},
    }
