"""Speechmatics speech-to-text adapter — the hosted batch transcription capability.

This is the EXECUTION half of the ``speechmatics:standard`` candidate the STT
control plane (``backend/ai/stt_control_plane.py``) registers. Like the Groq
Whisper adapter it adds no pipeline: the media boundary
(``backend/services/media_service.py``) remains the single controlled media path
— it resolves the media target deterministically, transfers it under explicit
limits, validates the container, enforces every bound, owns the outer timeout,
removes the temporary directory and normalizes the text. The only thing this
module supplies is the engine behind the EXISTING seam:

    ``media_service.SttEngine.transcribe(audio: bytes) -> str``

It therefore:

  * never downloads Telegram media and never sees a Telegram object, a chat id, a
    message id, a sender, a caption, a filename or any conversation state — its
    only inputs are the already-validated payload bytes and this engine's own
    static configuration (model/operating point, language, pass count);
  * never calls ``ProviderManager``, a provider adapter or a chat completion:
    Speechmatics is a transcription service, not a conversational provider, and
    nothing here changes chat behavior;
  * adds no dependency — the ``httpx`` stack every provider adapter already uses,
    no Speechmatics client package, no local model, no ffmpeg;
  * adds no second media boundary and no behavioral environment variable: the
    model comes ONLY from the registered candidate the owner selected in
    Telegram, the credential is the one deployment secret this capability
    declares (``AI_SPEECHMATICS_API_KEY``), and language/passes stay
    Telegram-controlled and persisted in the existing ``ai_config`` row.

API contract (verified against the official Speechmatics batch v2 REST API):
Speechmatics transcribes asynchronously, so ONE recognition is a bounded job
cycle — ``POST {base}/jobs`` (multipart: a ``config`` JSON part plus the audio as
``data_file``), then ``GET {base}/jobs/{id}`` until the job reports ``done``, then
``GET {base}/jobs/{id}/transcript?format=txt`` for the plain-text transcript.
Authorization is a bearer token (the API key) and the base URL is the public
self-service batch endpoint (``https://asr.api.speechmatics.com/v2``).

Language: the owner's persisted setting stays meaningful. Empty means automatic
detection, which this API spells ``language: "auto"``; an explicit BCP-47 tag is
reduced to its ISO-639-1 primary subtag the API documents (``fa-IR`` → ``fa``).
Nothing is translated, transliterated or forced into English, and a Persian
transcript stays in Persian script because the provider returns the spoken
language.

Recognition passes: the owner's bounded pass count (``1``..``3``) is honored
through the EXISTING pure STT-only consensus
(``backend/services/stt_consensus.py``). A pass is a recognition attempt — one
full submit → wait → fetch cycle — never a transport retry, and a deterministic
failure stops the loop outright. Transport recovery is separate and bounded: a
transient failure may repeat the SUBMIT leg at most ``MAX_ATTEMPTS`` times while
the operation deadline has room, and a transient failure while WAITING for a
submitted job only repeats the status read (a bounded number of consecutive
failures), because the job already exists.

Bounded transport, ONE deadline, CLASSIFIED failures. One transcription carries a
single wall-clock deadline inside the boundary's own bound
(``media_service.STT_TIMEOUT_S``), every HTTP timeout is derived from what is left
of it, and every failure is classified into a closed token so a live incident is
diagnosable instead of collapsing into one generic sentence. A poor or
grammatically odd transcript is a SUCCESSFUL provider response: recognition
quality is never reclassified as a transport failure, and an empty transcript is
a failure rather than a successful empty transcription.
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

import httpx

from backend.services import media_service
from backend.services.gemini_media_engine import STT_MAX_PASSES
from backend.services.media_service import MediaError
from backend.services.stt_consensus import reconcile_hypotheses

logger = logging.getLogger(__name__)

#: The provider this adapter implements. Used for traces only.
PROVIDER_NAME = "speechmatics"

#: The public self-service batch endpoint and the ONE path this adapter posts to.
API_BASE = "https://asr.api.speechmatics.com/v2"
JOBS_PATH = "/jobs"
TRANSCRIPT_PATH = "/transcript"

#: The transcript representation this adapter asks for: the documented plain-text
#: form, so no diarization, no word timings and no metadata are requested. Those
#: omissions are the recognition control variables, exactly as on the other
#: adapters.
TRANSCRIPT_FORMAT = "txt"

#: The credential variable this deployment declares. ENV holds the secret only;
#: the model, language and passes are Telegram-managed configuration and are
#: never read from the environment.
API_KEY_ENV_VARS = ("AI_SPEECHMATICS_API_KEY",)

#: The ONLY model this adapter may be built with. It is the model of the
#: registered ``speechmatics:standard`` candidate; an arbitrary string is never
#: accepted, so the owner can never reach a model by typing one.
STANDARD = "standard"
SUPPORTED_MODELS: tuple[str, ...] = (STANDARD,)

#: The registered model → the API's own acoustic-model field. The candidate IS
#: the operating point, so the mapping is the identity today and stays a table
#: (rather than a passthrough) so an unregistered value can never reach the API.
OPERATING_POINTS: dict[str, str] = {STANDARD: STANDARD}

#: The API's documented value for automatic language identification. It is the
#: provider's own token — never a fabricated language code.
AUTO_LANGUAGE = "auto"

#: ── Bounds ──
#:
#: Engine-level finite wall-clock bound, comfortably INSIDE the boundary's own
#: bound (``media_service.STT_TIMEOUT_S`` = 60 s), so the engine fails with its
#: own precise reason before the boundary's outer bound.
OPERATION_DEADLINE_S = 45.0

#: Phase bounds INSIDE the operation deadline: a stalled handshake, upload or
#: status read may never consume the whole operation.
CONNECT_TIMEOUT_S = 10.0
WRITE_TIMEOUT_S = 20.0

#: How often a submitted job's status is re-read. Finite and deliberate: the
#: batch service is asynchronous, so a bounded poll is the honest way to wait
#: without either hammering the API or inventing a notification channel.
POLL_INTERVAL_S = 1.5

#: The smallest budget a further poll or submit may START with. Below it the
#: operation is reported as an explicit deadline failure instead of being
#: converted into an HTTP timeout too short to mean anything.
MIN_POLL_BUDGET_S = 1.5
MIN_ATTEMPT_S = 8.0
_DEADLINE_FLOOR_S = 0.5

#: Hard ceiling on provider attempts per operation for the SINGLE-pass route: the
#: initial submit plus at most ONE retry, and only for a transient condition.
MAX_ATTEMPTS = 2

#: Hard ceiling on CONSECUTIVE transient status-read failures for an
#: already-submitted job: the job exists, so a bounded re-read is recovery rather
#: than a second recognition attempt.
MAX_POLL_FAILURES = 3

#: The transcript ceiling, shared with every other extractor through the media
#: boundary's own constant (the project prompt budget), so this adapter can never
#: produce text larger than the boundary would keep.
MAX_TRANSCRIPT_CHARS = media_service.MAX_STT_CHARS

#: ── Failure classes (bounded, closed tokens) ──
#:
#: ONE token per failure SITE, attached to the raised ``MediaError`` and emitted
#: as the ``failure_class`` field of this module's own trace line. The vocabulary
#: is the capability-wide one the provider probe reports, so a live incident reads
#: the same whichever adapter produced it. Operational metadata only — never
#: payload, a credential or a Telegram identifier.
FAILURE_MISSING_CREDENTIAL = "missing_credential"
FAILURE_UNSUPPORTED_MODEL = "unsupported_model"
FAILURE_AUTH = "auth"
FAILURE_FORBIDDEN = "forbidden"
FAILURE_INVALID_REQUEST = "invalid_request"
FAILURE_UNSUPPORTED_AUDIO = "unsupported_audio"
FAILURE_TIMEOUT = "timeout"
FAILURE_TRANSPORT = "transport"
FAILURE_RATE_LIMIT = "rate_limit"
FAILURE_SERVER = "server"
FAILURE_MALFORMED = "malformed_response"
FAILURE_EMPTY = "empty_transcription"
FAILURE_REJECTION = "provider_rejection"
FAILURE_DEADLINE = "operation_deadline"

#: The job statuses that end a job WITHOUT a transcript. Anything outside the
#: documented running/done pair is a rejection this adapter reports honestly
#: instead of waiting for a transcript that will never exist.
_JOB_RUNNING = "running"
_JOB_DONE = "done"

#: The ``httpx`` timeout subclasses mapped onto the socket phase they expired in,
#: so a timeout line says WHICH leg stalled.
_TIMEOUT_PHASES: tuple[tuple[type[BaseException], str], ...] = (
    (httpx.ConnectTimeout, "connect"),
    (httpx.WriteTimeout, "write"),
    (httpx.PoolTimeout, "pool"),
    (httpx.ReadTimeout, "read"),
)

#: The ONLY failures a bounded repeat may cover: transport conditions that are
#: transient by nature. Deterministic failures — a rejected credential, a refused
#: payload, an unreadable body, an empty transcript, a rejected job — are
#: deliberately absent, so no deterministic error is ever re-sent.
_TRANSIENT_TRANSPORT_ERRORS: tuple[type[BaseException], ...] = (
    httpx.TimeoutException,
    httpx.ConnectError,
    httpx.ReadError,
    httpx.WriteError,
    httpx.RemoteProtocolError,
)

#: Container signatures, mirrored from the boundary's own validators. The bytes
#: reaching an engine have already been corroborated against a declared type by
#: ``media_service``; sniffing here only decides WHICH documented container the
#: payload is, since the seam's one-argument contract carries bytes and no MIME.
_OGG_MAGIC = b"OggS"
_WAV_RIFF = b"RIFF"
_WAV_WAVE = b"WAVE"
_FLAC_MAGIC = b"fLaC"

#: The multipart part names and the STATIC remote filenames. Deliberately
#: non-identifying: a Telegram filename must never reach the remote API.
_CONFIG_FIELD = "config"
_UPLOAD_FIELD = "data_file"
_OGG_UPLOAD_NAME = "audio.ogg"
_WAV_UPLOAD_NAME = "audio.wav"
_FLAC_UPLOAD_NAME = "audio.flac"


def container_for(audio: bytes) -> tuple[str, str] | None:
    """``(upload_name, mime_type)`` for ``audio``, or ``None`` when unsupported.

    Deterministic and dependency-free: only the payload's own container bytes are
    inspected (they were already validated by the media boundary) and nothing is
    decoded. OGG/Opus, WAV and FLAC are the containers the batch API documents
    AND the boundary corroborates; every unrecognised payload returns ``None`` so
    the caller fails closed before sending anything. The boundary's accepted list
    is deliberately not broadened here.
    """
    if audio.startswith(_OGG_MAGIC):
        return _OGG_UPLOAD_NAME, "audio/ogg"
    if len(audio) >= 12 and audio[:4] == _WAV_RIFF and audio[8:12] == _WAV_WAVE:
        return _WAV_UPLOAD_NAME, "audio/wav"
    if audio.startswith(_FLAC_MAGIC):
        return _FLAC_UPLOAD_NAME, "audio/flac"
    return None


def language_code(language: str) -> str:
    """The language value the job config carries.

    The owner's setting is a BCP-47 tag (``fa``, ``fa-IR``, ``en-US``) and the
    batch API documents an ISO-639-1 language, so the region subtag is dropped
    (``fa-IR`` → ``fa``). An empty — or unusable — setting becomes the provider's
    own ``auto`` (automatic language identification) rather than an invented
    code. Nothing else is rewritten: there is no table of languages to drift, no
    translation and no transliteration.
    """
    value = str(language or "").strip().replace("_", "-")
    if not value:
        return AUTO_LANGUAGE
    primary = value.split("-", 1)[0]
    if 2 <= len(primary) <= 3 and primary.isalpha():
        return primary.lower()
    return AUTO_LANGUAGE


def resolve_api_key() -> tuple[str, str]:
    """``(api_key, env_var_name)`` from the deployment's ONE declared secret."""
    for name in API_KEY_ENV_VARS:
        value = (os.getenv(name) or "").strip()
        if value:
            return value, name
    return "", ""


def _error(
    message: str,
    failure_class: str,
    *,
    retryable: bool = False,
    http_status: int = 0,
    phase: str = "",
) -> MediaError:
    """Build the boundary's ``MediaError`` with bounded OPERATIONAL metadata.

    ``stage`` stays empty: the media boundary owns stage attribution. The extra
    attributes are consumed by this module's own trace line, by the bounded retry
    decision and by the provider-test seam. None of them can carry payload, a
    credential or a Telegram identifier.
    """
    error = MediaError(message)
    error.failure_class = failure_class
    error.retryable = retryable
    error.http_status = http_status
    error.phase = phase
    error.provider = PROVIDER_NAME
    return error


def failure_class_of(error: BaseException) -> str:
    """The bounded failure-class token of a failed transcription (never empty)."""
    return str(getattr(error, "failure_class", "") or FAILURE_TRANSPORT)


def _failure_field(error: BaseException) -> str:
    """The ``failure_class`` trace field: ``<class>``, ``<class>:<phase>`` or
    ``<class>:http=<status>`` — the three facts a bare ``failed`` cannot carry."""
    name = failure_class_of(error)
    phase = str(getattr(error, "phase", "") or "")
    status = int(getattr(error, "http_status", 0) or 0)
    if phase:
        return f"{name}:{phase}"
    if status:
        return f"{name}:http={status}"
    return name


def _budget(deadline: float) -> float:
    """Seconds left before the operation deadline (may be negative)."""
    return deadline - time.monotonic()


def _remaining(deadline: float, floor: float, what: str) -> float:
    """The remaining budget, or an explicit, controlled deadline failure."""
    left = _budget(deadline)
    if left < floor:
        raise _error(
            f"Speechmatics did not {what} within the "
            f"{OPERATION_DEADLINE_S:g}s operation budget.",
            FAILURE_DEADLINE,
        )
    return left


def _request_timeout(left: float) -> httpx.Timeout:
    """Phase bounds DERIVED from the remaining operation budget."""
    return httpx.Timeout(
        connect=min(CONNECT_TIMEOUT_S, left),
        read=left,
        write=min(WRITE_TIMEOUT_S, left),
        pool=min(CONNECT_TIMEOUT_S, left),
    )


def _timeout_phase(error: httpx.TimeoutException) -> str:
    """Which socket phase an ``httpx`` timeout expired in (bounded token)."""
    for klass, name in _TIMEOUT_PHASES:
        if isinstance(error, klass):
            return name
    return "request"


def _safe_detail(response: httpx.Response, api_key: str) -> str:
    """A short, sanitized provider detail for a failed request.

    The response body is never logged or surfaced beyond this bounded,
    whitespace-collapsed message, and the credential is redacted from it, so no
    request payload and no key can leak through an error string.
    """
    try:
        data = response.json()
    except Exception:  # noqa: BLE001 — a non-JSON error body is not a crash
        return ""
    if isinstance(data, dict):
        message = data.get("detail")
        if not isinstance(message, str):
            message = data.get("message")
    else:
        message = ""
    if not isinstance(message, str) or not message:
        return ""
    detail = " ".join(message.split())[:160]
    if api_key:
        detail = detail.replace(api_key, "***")
    return detail


def _raise_for_status(response: httpx.Response, api_key: str) -> None:
    """Map a non-2xx Speechmatics response onto the controlled error contract.

    Every status is CLASSIFIED and carries its own HTTP status, and only the two
    genuinely transient families (429, >= 500) are marked retryable — a rejected
    credential or a refused payload is DETERMINISTIC and is never re-sent. The
    documented 400 body (``{"detail": ...}``) is read only to tell apart the
    rejection families the API reports; the classification stays deterministic.
    """
    status = response.status_code
    if status < 400:
        return
    detail = _safe_detail(response, api_key)
    suffix = f" ({detail})" if detail else ""
    lowered = detail.lower()
    if status == 401:
        raise _error(
            f"Speechmatics rejected the configured API key (HTTP {status}).{suffix}",
            FAILURE_AUTH, http_status=status,
        )
    if status == 403:
        raise _error(
            f"Speechmatics refused this key access to the batch API "
            f"(HTTP {status}).{suffix}",
            FAILURE_FORBIDDEN, http_status=status,
        )
    if status == 429:
        raise _error(
            "Speechmatics rate limited the transcription request (HTTP 429).",
            FAILURE_RATE_LIMIT, retryable=True, http_status=status,
        )
    if status == 400:
        if "operating_point" in lowered or "model" in lowered:
            raise _error(
                f"Speechmatics refused the transcription model (HTTP {status}).{suffix}",
                FAILURE_UNSUPPORTED_MODEL, http_status=status,
            )
        if any(
            token in lowered
            for token in ("audio", "file", "format", "codec", "media", "sample")
        ):
            raise _error(
                f"Speechmatics could not decode the audio payload (HTTP {status}).{suffix}",
                FAILURE_UNSUPPORTED_AUDIO, http_status=status,
            )
        raise _error(
            f"Speechmatics refused the transcription request (HTTP {status}).{suffix}",
            FAILURE_INVALID_REQUEST, http_status=status,
        )
    if status == 404:
        raise _error(
            f"Speechmatics has no such transcription job (HTTP {status}).{suffix}",
            FAILURE_REJECTION, http_status=status,
        )
    if status >= 500:
        raise _error(
            f"Speechmatics is unavailable right now (HTTP {status}).",
            FAILURE_SERVER, retryable=True, http_status=status,
        )
    raise _error(
        f"Speechmatics rejected the transcription request (HTTP {status}).{suffix}",
        FAILURE_REJECTION, http_status=status,
    )


def _cap(text: str) -> str:
    """Cap a transcript at the shared character ceiling, reporting it honestly."""
    if len(text) <= MAX_TRANSCRIPT_CHARS:
        return text
    return text[: MAX_TRANSCRIPT_CHARS - 1] + "…"


class SpeechmaticsBatchEngine:
    """One synchronous engine implementing the existing ``SttEngine`` seam.

    Deliberately synchronous: ``media_service`` runs engines in a worker thread
    (``asyncio.to_thread``), so the HTTP calls (including the bounded job poll)
    must not touch the event loop, and the boundary stays the single owner of the
    awaited timeout.

    The engine is stateless apart from its credential, model and the three
    behavioral settings, so provisioning one cannot alter the media contract, and
    no owner/chat/message/caption state can reach the provider.
    """

    __slots__ = (
        "_api_key", "_model", "_operating_point", "_language", "_passes",
        "_base_url", "_key_env_var", "_last_failure_class",
    )

    def __init__(
        self,
        api_key: str,
        model: str,
        *,
        language: str = "",
        passes: int = 1,
        base_url: str = API_BASE,
        key_env_var: str = "",
    ) -> None:
        self._api_key = str(api_key or "").strip()
        self._model = str(model or "").strip()
        self._operating_point = OPERATING_POINTS.get(self._model, "")
        self._language = language_code(language)
        try:
            count = int(passes)
        except (TypeError, ValueError):
            count = 1
        self._passes = max(1, min(count, STT_MAX_PASSES))
        self._base_url = str(base_url or API_BASE).rstrip("/")
        self._key_env_var = str(key_env_var or "")
        self._last_failure_class = ""

    # ── Identity (no secret is ever exposed by these) ──

    @property
    def model(self) -> str:
        """The registered Speechmatics model this engine transcribes with."""
        return self._model

    @property
    def operating_point(self) -> str:
        """The API's own acoustic-model value sent with the job."""
        return self._operating_point

    @property
    def language(self) -> str:
        """The language value sent with the job (``auto`` = automatic)."""
        return self._language

    @property
    def passes(self) -> int:
        """The configured number of recognition passes (``1`` = single pass)."""
        return self._passes

    @property
    def key_env_var(self) -> str:
        """The ENV variable the credential came from — the NAME only."""
        return self._key_env_var

    @property
    def endpoint(self) -> str:
        """The job-submission URL this engine posts to."""
        return f"{self._base_url}{JOBS_PATH}"

    @property
    def last_failure_class(self) -> str:
        """The bounded class of the most recent failure on this engine (or ``""``)."""
        return self._last_failure_class

    def job_config(self) -> dict[str, Any]:
        """The job configuration sent as the multipart ``config`` part.

        The documented shape: a transcription job whose transcription config
        carries the language and the operating point, and nothing else — no
        diarization (voice notes are single-speaker) and no output options beyond
        the plain-text transcript this adapter asks for.
        """
        return {
            "type": "transcription",
            "transcription_config": {
                "language": self._language,
                "operating_point": self._operating_point,
            },
        }

    # ── The seam method ──

    def transcribe(self, audio: bytes) -> str:
        """Return the transcript of ``audio``; raises ``MediaError`` on failure.

        Empty input returns the seam's honest empty string (nothing was sent). A
        container the batch API does not document is refused locally. With a pass
        count above one the SAME audio is recognised that many times and the
        hypotheses are reconciled by the existing STT-only consensus; otherwise
        one bounded job cycle is attempted.
        """
        if not audio:
            return ""
        container = container_for(audio)
        if container is None:
            error = _error(
                "Speechmatics does not accept this audio container for transcription.",
                FAILURE_UNSUPPORTED_AUDIO,
            )
            self._last_failure_class = FAILURE_UNSUPPORTED_AUDIO
            self._log_run(len(audio), 0, 0.0, failed=True, error=error)
            raise error
        if self._passes > 1:
            return self._run_passes(audio, container)
        return self._run_once(audio, container)

    # ── Bounded single-pass route ──

    def _run_once(self, audio: bytes, container: tuple[str, str]) -> str:
        """ONE job cycle, at most one bounded transient retry, ceiling applied.

        The retry exists only for conditions that are transient by nature and
        only while the operation deadline has room; a deterministic failure ends
        the loop immediately instead of submitting the same job again.
        """
        started = time.monotonic()
        deadline = started + OPERATION_DEADLINE_S
        attempts = 0
        failure: MediaError | None = None
        while attempts < MAX_ATTEMPTS:
            if attempts and _budget(deadline) < MIN_ATTEMPT_S:
                break
            attempts += 1
            try:
                text = self._transcribe_job(audio, container, deadline)
            except MediaError as exc:
                failure = exc
                if not getattr(exc, "retryable", False):
                    break
                continue
            transcript = _cap(text)
            self._log_run(
                len(audio), len(transcript), started, attempts=attempts, hypotheses=1,
            )
            return transcript
        error = failure or _error(
            "Speechmatics produced no transcription result.", FAILURE_TRANSPORT,
        )
        self._last_failure_class = failure_class_of(error)
        self._log_run(len(audio), 0, started, failed=True, error=error, attempts=attempts)
        raise error

    # ── Bounded multi-pass route (the existing consensus seam) ──

    def _run_passes(self, audio: bytes, container: tuple[str, str]) -> str:
        """N SEQUENTIAL passes over the SAME audio, reconciled, ONE deadline.

        Bounded in every dimension: the pass count is the owner's configured
        ceiling (already clamped by the control plane), only ONE job is ever in
        flight, and the whole operation runs inside the operation deadline. A
        pass that failed contributes NO hypothesis (a transport problem can never
        be mistaken for a recognition result) and is never retried: the
        configured passes ARE the transient-recovery budget here. A DETERMINISTIC
        failure ends the loop at once, and when no pass produced a transcript the
        operation fails closed with the first, most informative failure.
        """
        started = time.monotonic()
        deadline = started + OPERATION_DEADLINE_S
        hypotheses: list[str] = []
        failure: MediaError | None = None
        passes = 0
        for index in range(1, self._passes + 1):
            if index > 1 and _budget(deadline) < MIN_ATTEMPT_S:
                break
            try:
                text = self._transcribe_job(audio, container, deadline)
            except MediaError as exc:
                if failure is None:
                    failure = exc
                passes += 1
                if not getattr(exc, "retryable", False):
                    break
                continue
            passes += 1
            hypotheses.append(text)

        if not hypotheses:
            error = failure or _error(
                "Speechmatics produced no transcription result.", FAILURE_TRANSPORT,
            )
            self._last_failure_class = failure_class_of(error)
            self._log_run(
                len(audio), 0, started, failed=True, error=error,
                attempts=passes, hypotheses=0,
            )
            raise error

        transcript = _cap(reconcile_hypotheses(hypotheses).text)
        self._log_run(len(audio), len(transcript), started, attempts=passes,
                      hypotheses=len(hypotheses))
        return transcript

    # ── The job cycle ──

    def _transcribe_job(
        self, audio: bytes, container: tuple[str, str], deadline: float,
    ) -> str:
        """ONE recognition: submit the job, wait for it, fetch the transcript."""
        job_id = self._submit_job(audio, container, deadline)
        self._await_job(job_id, deadline)
        return self._fetch_transcript(job_id, deadline)

    def _submit_job(
        self, audio: bytes, container: tuple[str, str], deadline: float,
    ) -> str:
        """``POST /jobs`` — the multipart job submission, inside the deadline."""
        left = _remaining(deadline, _DEADLINE_FLOOR_S, "start")
        upload_name, mime_type = container
        data = {_CONFIG_FIELD: json.dumps(self.job_config(), ensure_ascii=False)}
        files = {_UPLOAD_FIELD: (upload_name, audio, mime_type)}
        response = self._send(
            "POST", JOBS_PATH, deadline=deadline, timeout_left=left,
            data=data, files=files,
        )
        _raise_for_status(response, self._api_key)
        return _job_id(response)

    def _await_job(self, job_id: str, deadline: float) -> None:
        """Poll ``GET /jobs/{id}`` until the job is done, inside the deadline.

        A transient status-read failure only repeats the READ (the job already
        exists) and only a bounded number of consecutive times — never a second
        recognition attempt. A job that ends in any status other than the
        documented running/done pair is an honest rejection, not a wait for a
        transcript that will never exist.
        """
        failures = 0
        while True:
            _remaining(deadline, MIN_POLL_BUDGET_S, "finish transcribing")
            try:
                status = self._job_status(job_id, deadline)
            except MediaError as exc:
                if not getattr(exc, "retryable", False):
                    raise
                if failures < MAX_POLL_FAILURES:
                    failures += 1
                    _sleep_briefly(deadline)
                    continue
                # The bounded re-reads ARE this leg's recovery budget: the job
                # already exists, so letting the caller repeat the whole
                # operation would silently submit a SECOND job instead of
                # reporting that waiting for this one failed.
                raise _error(
                    str(exc), failure_class_of(exc),
                    phase=str(getattr(exc, "phase", "") or ""),
                    http_status=int(getattr(exc, "http_status", 0) or 0),
                ) from exc
            failures = 0
            if status == _JOB_DONE:
                return
            if status != _JOB_RUNNING:
                raise _error(
                    f"Speechmatics ended the job with status '{status or 'unknown'}'.",
                    FAILURE_REJECTION,
                )
            _sleep_briefly(deadline)

    def _job_status(self, job_id: str, deadline: float) -> str:
        """The bounded status token of ONE submitted job."""
        response = self._send("GET", _job_path(job_id), deadline=deadline)
        _raise_for_status(response, self._api_key)
        try:
            payload = response.json()
        except Exception as exc:  # noqa: BLE001 — malformed response boundary
            raise _error(
                "Speechmatics returned an unreadable job response.", FAILURE_MALFORMED,
            ) from exc
        job = payload.get("job") if isinstance(payload, dict) else None
        if not isinstance(job, dict) or not isinstance(job.get("status"), str):
            raise _error(
                "Speechmatics' job response carried no status.", FAILURE_MALFORMED,
            )
        return job["status"].strip().lower()

    def _fetch_transcript(self, job_id: str, deadline: float) -> str:
        """``GET /jobs/{id}/transcript?format=txt`` — the plain-text transcript.

        Only the transcript text is returned. An empty or whitespace-only body is
        its own class (``empty_transcription``) — never a successful empty
        transcription, so a provider that answers nothing can never be mistaken
        for silence in the audio.
        """
        response = self._send(
            "GET", _job_path(job_id) + TRANSCRIPT_PATH, deadline=deadline,
            timeout_left=None, params={"format": TRANSCRIPT_FORMAT},
            headers={"Accept": "text/plain"},
        )
        _raise_for_status(response, self._api_key)
        text = response.text if isinstance(response.text, str) else ""
        if not text.strip():
            raise _error(
                "Speechmatics returned an empty transcript for this audio.", FAILURE_EMPTY,
            )
        return text

    # ── The provider transport ──

    def _send(
        self,
        method: str,
        path: str,
        *,
        deadline: float,
        timeout_left: float | None = None,
        **kwargs: Any,
    ) -> httpx.Response:
        """ONE authenticated request, inside the operation deadline.

        The remaining budget is re-derived per call (a poll never inherits the
        previous call's bound), and every transport failure is classified rather
        than allowed to escape as a raw ``httpx`` exception.
        """
        left = _remaining(deadline, _DEADLINE_FLOOR_S, "start") if timeout_left is None else timeout_left
        headers = {"Authorization": f"Bearer {self._api_key}"}
        headers.update(kwargs.pop("headers", None) or {})
        try:
            with self._client(_request_timeout(left)) as client:
                response = client.request(
                    method, f"{self._base_url}{path}", headers=headers, **kwargs,
                )
        except httpx.TimeoutException as exc:
            phase = _timeout_phase(exc)
            raise _error(
                f"Speechmatics transcription timed out after {left:g}s ({phase} phase).",
                FAILURE_TIMEOUT, retryable=True, phase=phase,
            ) from exc
        except Exception as exc:  # noqa: BLE001 — the transport boundary
            raise _error(
                f"Speechmatics transcription request failed ({type(exc).__name__}).",
                FAILURE_TRANSPORT,
                retryable=isinstance(exc, _TRANSIENT_TRANSPORT_ERRORS),
            ) from exc
        return response

    def _client(self, timeout: httpx.Timeout) -> httpx.Client:
        """A per-call client carrying the DERIVED phase bounds.

        Deliberately not a shared/pooled client: the engine is called from the
        boundary's ``asyncio.to_thread`` workers, so module-level client state
        would be cross-request mutable state — the ``__slots__`` contract is
        exactly that this engine holds none.
        """
        return httpx.Client(timeout=timeout)

    # ── Observability (bounded, never payload/credential/Telegram metadata) ──

    def _log_run(
        self,
        byte_count: int,
        chars: int,
        started: float,
        *,
        failed: bool = False,
        error: MediaError | None = None,
        attempts: int = 1,
        hypotheses: int = 0,
    ) -> None:
        """ONE bounded, non-sensitive line per transcription.

        It carries exactly what a live incident and the recognition experiment
        need — provider, model, API surface, payload size, language mode, elapsed
        time, output LENGTH, success/failure, attempts, the configured pass count
        and the failure CLASS with its socket phase or HTTP status — and never the
        transcript, the raw audio, the job id, the credential or a Telegram
        identifier.
        """
        logger.info(
            "SPEECHMATICS_STT_ENGINE provider=%s endpoint=%s model=%s bytes=%d "
            "language=%s chars=%d elapsed_ms=%d status=%s attempts=%d passes=%d "
            "hypotheses=%d failure_class=%s",
            PROVIDER_NAME, JOBS_PATH, self._model or "-", byte_count,
            self._language or AUTO_LANGUAGE, chars,
            int((time.monotonic() - started) * 1000) if started else 0,
            "failed" if failed else "ok", attempts, self._passes, hypotheses,
            _failure_field(error) if error is not None else "-",
        )


def _job_path(job_id: str) -> str:
    return f"{JOBS_PATH}/{job_id}"


def _job_id(response: httpx.Response) -> str:
    """The submitted job's id, or a deterministic malformed-response failure."""
    try:
        payload = response.json()
    except Exception as exc:  # noqa: BLE001 — malformed response boundary
        raise _error(
            "Speechmatics returned an unreadable job submission response.",
            FAILURE_MALFORMED,
        ) from exc
    job_id = payload.get("id") if isinstance(payload, dict) else None
    if not isinstance(job_id, str) or not job_id.strip():
        raise _error(
            "Speechmatics' job submission response carried no job id.",
            FAILURE_MALFORMED,
        )
    return job_id.strip()


def _sleep_briefly(deadline: float) -> None:
    """Sleep one poll interval, never past the operation deadline."""
    time.sleep(max(0.0, min(POLL_INTERVAL_S, _budget(deadline))))


def build_engine(
    model: str,
    *,
    language: str = "",
    passes: int = 1,
    api_key: str = "",
    base_url: str = API_BASE,
) -> tuple[SpeechmaticsBatchEngine | None, str]:
    """``(engine, reason)`` for a REGISTERED Speechmatics model; ``engine`` is
    ``None`` when this runtime cannot run it and ``reason`` is the bounded class.

    The model must be the model the control plane registers — an arbitrary string
    is refused here too, so a typed model id can never reach the provider. A
    missing credential yields ``None`` with ``missing_credential`` instead of an
    exception: provisioning is optional by contract, and the caller (the STT
    engine factory) keeps the boundary fail-closed rather than substituting a
    different provider's model.
    """
    resolved = str(model or "").strip()
    if resolved not in SUPPORTED_MODELS:
        return None, FAILURE_UNSUPPORTED_MODEL
    key = str(api_key or "").strip()
    key_env_var = ""
    if key:
        key_env_var = "explicit"
    else:
        key, key_env_var = resolve_api_key()
    if not key:
        return None, FAILURE_MISSING_CREDENTIAL
    return (
        SpeechmaticsBatchEngine(
            key, resolved, language=language, passes=passes,
            base_url=base_url, key_env_var=key_env_var,
        ),
        "",
    )
