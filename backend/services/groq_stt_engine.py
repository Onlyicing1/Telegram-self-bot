"""Groq speech-to-text adapter — the hosted Whisper transcription capability.

This is the EXECUTION half of the Groq Whisper candidates the STT control plane
(``backend/ai/stt_control_plane.py``) registers. It adds no pipeline: the media
boundary (``backend/services/media_service.py``) remains the single controlled
media path — it resolves the media target deterministically, transfers it under
explicit limits, validates the container, enforces every bound, owns the outer
timeout, removes the temporary directory and normalizes the text. The only thing
this module supplies is the engine behind the EXISTING seam:

    ``media_service.SttEngine.transcribe(audio: bytes) -> str``

It therefore:
  * never downloads Telegram media and never sees a Telegram object, a chat id, a
    message id, a sender, a caption, a filename or any conversation state — its
    only inputs are the already-validated payload bytes and this engine's own
    static configuration (model, language, pass count);
  * never calls ``ProviderManager``, a provider adapter, ``vision()`` or the
    Groq CHAT provider (``backend/ai/providers/groq.py``). Groq's conversational
    completion adapter and Groq's audio transcription API are DIFFERENT
    capabilities on different endpoints, and nothing here changes chat behavior;
  * adds no dependency (the ``httpx`` stack the provider adapters already use),
    no local model, no Whisper/PyTorch/ONNX and no ffmpeg;
  * adds no second media boundary, no second download path and no new
    environment variable: the model comes ONLY from the registered candidate the
    owner selected in Telegram, and the credential is resolved from the
    variables the repository already declares
    (``AI_GROQ_API_KEY``, then the ``GROQ_API_KEY`` compatibility fallback).

Endpoint: the dedicated OpenAI-compatible transcription API,
``POST {base}/audio/transcriptions`` with ``multipart/form-data``, where the base
is the project's already-configured Groq base
(``https://api.groq.com/openai/v1``). Chat completions are never used.

Containers: only the audio containers Groq documents AND that the media boundary
already corroborates are sent — OGG/Opus (the Telegram voice-note container) and
WAV. Anything else the boundary accepted (FLAC today) is refused HERE,
deterministically and before any request, with the ``unsupported_audio`` class,
exactly as the Gemini engine refuses the containers its own API does not
document. The accepted list is deliberately not broadened.

Language: the owner's persisted setting stays meaningful. Empty means automatic
(the request carries no ``language`` field). An explicit BCP-47 tag is reduced to
its ISO-639-1 primary subtag the endpoint expects (``fa-IR`` → ``fa``); nothing is
translated, transliterated or forced into English, and a Persian transcript stays
in Persian script because the provider returns the spoken language.

Recognition passes: the owner's bounded pass count (``1``..``3``) is honored
through the EXISTING pure STT-only consensus
(``backend/services/stt_consensus.py``, the same seam the Gemini route uses) —
sequential passes over the SAME audio under ONE operation deadline. A pass is a
recognition attempt, never a transport retry: it is counted only when it actually
returned a transcript, and a deterministic failure stops the loop outright.

Bounded transport, ONE deadline, CLASSIFIED failures. One transcription carries a
single wall-clock deadline inside the boundary's own bound
(``media_service.STT_TIMEOUT_S``), every HTTP timeout is derived from what is
left of it, and at most :data:`MAX_ATTEMPTS` sequential attempts exist — only for
genuinely transient conditions and only with budget left. Every failure is
classified into a closed token (credential / auth / authorization / invalid model
/ unsupported audio / timeout / transport / rate limit / server / malformed /
empty transcription / provider rejection / spent deadline) so a live incident is
diagnosable instead of collapsing into one generic sentence. A poor or
grammatically odd transcript is a SUCCESSFUL provider response: recognition
quality is never reclassified as a transport failure.
"""
from __future__ import annotations

import logging
import os
import time

import httpx

from backend.services import media_service
from backend.services.gemini_media_engine import STT_MAX_PASSES
from backend.services.media_service import MediaError
from backend.services.stt_consensus import reconcile_hypotheses

logger = logging.getLogger(__name__)

#: The provider this adapter implements. Used for traces only.
PROVIDER_NAME = "groq"

#: The project's already-configured Groq API base (the same base the Groq chat
#: provider's defaults declare) and the dedicated transcription path on it.
GROQ_API_BASE = "https://api.groq.com/openai/v1"
TRANSCRIPTIONS_PATH = "/audio/transcriptions"

#: The credential variables the repository ALREADY declares for Groq, in the same
#: precedence order as ``backend/ai/providers/factory.py``. No new secret and no
#: parallel configuration system is introduced: an installation that already
#: enabled Groq for chat needs no additional key for transcription.
API_KEY_ENV_VARS = ("AI_GROQ_API_KEY", "GROQ_API_KEY")

#: The ONLY models this adapter may be built with. They are the models the
#: control plane registers as Groq candidates; an arbitrary string is never
#: accepted, so the owner can never reach a model by typing one.
WHISPER_LARGE_V3 = "whisper-large-v3"
WHISPER_LARGE_V3_TURBO = "whisper-large-v3-turbo"
SUPPORTED_MODELS: tuple[str, str] = (WHISPER_LARGE_V3, WHISPER_LARGE_V3_TURBO)

#: ── Bounds ──
#:
#: Engine-level finite wall-clock bound, comfortably INSIDE the boundary's own
#: bound (``media_service.STT_TIMEOUT_S`` = 60 s), so the engine fails with its
#: own precise reason before the boundary's outer bound. Derivation is the same
#: as the Gemini engine's: one deadline plus one bounded leg, leaving margin for
#: the boundary's worker-thread return path — the owner-facing failure is then
#: always the engine's specific class and never the boundary's generic sentence.
OPERATION_DEADLINE_S = 45.0

#: Phase bounds INSIDE the operation deadline: a stalled handshake or a stalled
#: request body may never consume the whole operation, while the response may use
#: whatever budget is left.
CONNECT_TIMEOUT_S = 10.0
WRITE_TIMEOUT_S = 20.0

#: A second attempt starts only with at least this much of the deadline left, so
#: a retry can never push the operation past its own deadline.
MIN_ATTEMPT_S = 8.0

#: Hard ceiling on provider requests per operation for the SINGLE-pass route: the
#: initial attempt plus at most ONE retry, and only for a transient condition.
MAX_ATTEMPTS = 2

#: The smallest budget a leg may START with. Below it the operation is reported as
#: an explicit deadline failure instead of being converted into an HTTP timeout
#: too short to mean anything.
_DEADLINE_FLOOR_S = 0.5

#: The transcript ceiling, shared with every other extractor through the media
#: boundary's own constant (the project prompt budget), so this adapter can never
#: produce text larger than the boundary would keep.
MAX_TRANSCRIPT_CHARS = media_service.MAX_STT_CHARS

#: ── Failure classes (bounded, closed tokens) ──
#:
#: ONE token per failure SITE, attached to the raised ``MediaError`` and emitted
#: as the ``failure_class`` field of this module's own trace line. Operational
#: metadata only — never payload, a credential or a Telegram identifier.
FAILURE_MISSING_CREDENTIAL = "missing_credential"
FAILURE_UNSUPPORTED_MODEL = "unsupported_model"
FAILURE_AUTH = "auth"
FAILURE_FORBIDDEN = "forbidden"
FAILURE_INVALID_MODEL = "invalid_model"
FAILURE_UNSUPPORTED_AUDIO = "unsupported_audio"
FAILURE_TIMEOUT = "timeout"
FAILURE_TRANSPORT = "transport"
FAILURE_RATE_LIMIT = "rate_limit"
FAILURE_SERVER = "server"
FAILURE_MALFORMED = "malformed_response"
FAILURE_EMPTY = "empty_transcription"
FAILURE_REJECTION = "provider_rejection"
FAILURE_DEADLINE = "operation_deadline"

#: The ``httpx`` timeout subclasses mapped onto the socket phase they expired in,
#: so a timeout line says WHICH leg stalled.
_TIMEOUT_PHASES: tuple[tuple[type[BaseException], str], ...] = (
    (httpx.ConnectTimeout, "connect"),
    (httpx.WriteTimeout, "write"),
    (httpx.PoolTimeout, "pool"),
    (httpx.ReadTimeout, "read"),
)

#: The ONLY failures a bounded second attempt may repeat: transport conditions
#: that are transient by nature. Deterministic failures — 4xx other than 429, an
#: unreadable body, an empty transcript, a refused container — are deliberately
#: absent, so no deterministic error is ever re-sent.
_TRANSIENT_TRANSPORT_ERRORS: tuple[type[BaseException], ...] = (
    httpx.TimeoutException,
    httpx.ConnectError,
    httpx.ReadError,
    httpx.WriteError,
    httpx.RemoteProtocolError,
)

#: Container signatures, mirrored from the boundary's own validators. The bytes
#: reaching an engine have already been corroborated against a declared type by
#: ``media_service``; sniffing here only decides WHICH documented Groq container
#: the payload is, since the seam's one-argument contract carries bytes and no
#: MIME type.
_OGG_MAGIC = b"OggS"
_WAV_RIFF = b"RIFF"
_WAV_WAVE = b"WAVE"

#: The multipart part name and the STATIC remote filename. Deliberately
#: non-identifying: a Telegram filename must never reach the remote API.
_UPLOAD_FIELD = "file"
_OGG_UPLOAD_NAME = "audio.ogg"
_WAV_UPLOAD_NAME = "audio.wav"

#: The response shape this adapter asks for. The default JSON form is the
#: documented one and carries only the transcript — no word timestamps (the API
#: documents that requesting them can DEGRADE accuracy) and no diarization
#: (voice notes are single-speaker). Those omissions are the experiment's control
#: variables, exactly as on the Gemini route.
_RESPONSE_FORMAT = "json"


def container_for(audio: bytes) -> tuple[str, str] | None:
    """``(upload_name, mime_type)`` for ``audio``, or ``None`` when unsupported.

    Deterministic and dependency-free: only the payload's own container bytes are
    inspected (they were already validated by the media boundary) and nothing is
    decoded. OGG/Opus is the Telegram voice-note container; WAV is the other
    documented container this boundary admits. FLAC — accepted by the boundary but
    NOT documented by Groq's transcription API — and every unrecognised payload
    return ``None`` so the caller fails closed before sending anything.
    """
    if audio.startswith(_OGG_MAGIC):
        return _OGG_UPLOAD_NAME, "audio/ogg"
    if len(audio) >= 12 and audio[:4] == _WAV_RIFF and audio[8:12] == _WAV_WAVE:
        return _WAV_UPLOAD_NAME, "audio/wav"
    return None


def language_code(language: str) -> str:
    """The ISO-639-1 code the endpoint expects, or ``""`` for automatic.

    The owner's setting is a BCP-47 tag (``fa``, ``fa-IR``, ``en-US``); the
    transcription request documents an ISO-639-1 language, so the region subtag is
    dropped (``fa-IR`` → ``fa``). Nothing else is rewritten: there is no table of
    languages to drift, no translation and no transliteration. A value with no
    usable primary subtag yields the empty value, i.e. automatic detection —
    the endpoint's own documented default — rather than an invented code.
    """
    value = str(language or "").strip().replace("_", "-")
    if not value:
        return ""
    primary = value.split("-", 1)[0]
    if 2 <= len(primary) <= 3 and primary.isalpha():
        return primary.lower()
    return ""


def resolve_api_key() -> tuple[str, str]:
    """``(api_key, env_var_name)`` from the variables the repository already uses."""
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
    decision and by the provider-test seam — the failure CLASS, whether this
    failure may be repeated within the deadline, the HTTP status when a response
    arrived, and the socket phase a timeout expired in. None of them can carry
    payload, a credential or a Telegram identifier.
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


def _required_budget(deadline: float) -> float:
    """The remaining budget, or an explicit, controlled deadline failure."""
    left = _budget(deadline)
    if left < _DEADLINE_FLOOR_S:
        raise _error(
            f"Groq transcription did not start within the {OPERATION_DEADLINE_S:g}s "
            "operation budget.",
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
    if not isinstance(data, dict):
        return ""
    error = data.get("error")
    message = ""
    if isinstance(error, dict):
        message = str(error.get("message") or "")
    elif isinstance(error, str):
        message = error
    if not message:
        return ""
    detail = " ".join(message.split())[:160]
    if api_key:
        detail = detail.replace(api_key, "***")
    return detail


def _error_code(response: httpx.Response) -> str:
    """The provider's own bounded error identity for a rejection (``code``/``type``).

    Used ONLY to tell apart the two documented 400 families the transcription
    endpoint reports — an unusable model versus an audio payload it cannot decode.
    Reading the provider's own token keeps the classification deterministic
    instead of guessing from free-form prose.
    """
    try:
        data = response.json()
    except Exception:  # noqa: BLE001 — a non-JSON error body is not a crash
        return ""
    if not isinstance(data, dict):
        return ""
    error = data.get("error")
    if isinstance(error, dict):
        code = str(error.get("code") or "")
        kind = str(error.get("type") or "")
        return f"{code} {kind}".strip().lower()
    return ""


def _raise_for_status(response: httpx.Response, api_key: str) -> None:
    """Map a non-2xx Groq response onto the boundary's controlled error contract.

    Every status is CLASSIFIED and carries its own HTTP status, and only the two
    genuinely transient families (429, >= 500) are marked retryable — a rejected
    credential, an unusable model or a refused payload is DETERMINISTIC and is
    never re-sent.
    """
    status = response.status_code
    if status < 400:
        return
    detail = _safe_detail(response, api_key)
    suffix = f" ({detail})" if detail else ""
    if status == 401:
        raise _error(
            f"Groq rejected the configured API key (HTTP {status}).{suffix}",
            FAILURE_AUTH, http_status=status,
        )
    if status == 403:
        raise _error(
            f"Groq refused this key access to the transcription API "
            f"(HTTP {status}).{suffix}",
            FAILURE_FORBIDDEN, http_status=status,
        )
    if status == 429:
        raise _error(
            "Groq rate limited the transcription request (HTTP 429).",
            FAILURE_RATE_LIMIT, retryable=True, http_status=status,
        )
    if status == 404:
        raise _error(
            f"Groq has no such transcription model (HTTP {status}).{suffix}",
            FAILURE_INVALID_MODEL, http_status=status,
        )
    if status == 400:
        code = _error_code(response)
        if "model" in code:
            raise _error(
                f"Groq refused the transcription model (HTTP {status}).{suffix}",
                FAILURE_INVALID_MODEL, http_status=status,
            )
        if any(token in code for token in ("audio", "file", "format", "media")):
            raise _error(
                f"Groq could not decode the audio payload (HTTP {status}).{suffix}",
                FAILURE_UNSUPPORTED_AUDIO, http_status=status,
            )
        raise _error(
            f"Groq refused the transcription request (HTTP {status}).{suffix}",
            FAILURE_REJECTION, http_status=status,
        )
    if status >= 500:
        raise _error(
            f"Groq is unavailable right now (HTTP {status}).",
            FAILURE_SERVER, retryable=True, http_status=status,
        )
    raise _error(
        f"Groq rejected the transcription request (HTTP {status}).{suffix}",
        FAILURE_REJECTION, http_status=status,
    )


def _extract_text(response: httpx.Response) -> str:
    """The transcript of ONE accepted response, or a classified failure.

    Reads only the documented ``text`` field. An unreadable body, a non-object
    shape and a missing/non-string transcript are DETERMINISTIC failures
    (``malformed_response``) and an empty transcript is its own class
    (``empty_transcription``) — never a successful empty transcription, so a
    provider that answers nothing can never be mistaken for silence in the audio.
    """
    try:
        payload = response.json()
    except Exception as exc:  # noqa: BLE001 — malformed response boundary
        raise _error(
            "Groq returned an unreadable transcription response.", FAILURE_MALFORMED,
        ) from exc
    if not isinstance(payload, dict) or "text" not in payload:
        raise _error(
            "Groq's transcription response carried no transcript.", FAILURE_MALFORMED,
        )
    text = payload.get("text")
    if not isinstance(text, str):
        raise _error(
            "Groq's transcription response carried no transcript.", FAILURE_MALFORMED,
        )
    if not text.strip():
        raise _error(
            "Groq returned an empty transcript for this audio.", FAILURE_EMPTY,
        )
    return text


def _cap(text: str) -> str:
    """Cap a transcript at the shared character ceiling, reporting it honestly."""
    if len(text) <= MAX_TRANSCRIPT_CHARS:
        return text
    return text[: MAX_TRANSCRIPT_CHARS - 1] + "…"


class GroqWhisperEngine:
    """One synchronous engine implementing the existing ``SttEngine`` seam.

    Deliberately synchronous: ``media_service`` runs engines in a worker thread
    (``asyncio.to_thread``), so the HTTP call here must not touch the event loop,
    and the boundary stays the single owner of the awaited timeout.

    The engine is stateless apart from its credential, model and the three
    behavioral settings, so provisioning one cannot alter the media contract, and
    no owner/chat/message/caption state can reach the provider.
    """

    __slots__ = (
        "_api_key", "_model", "_language", "_passes", "_base_url", "_key_env_var",
        "_last_failure_class",
    )

    def __init__(
        self,
        api_key: str,
        model: str,
        *,
        language: str = "",
        passes: int = 1,
        base_url: str = GROQ_API_BASE,
        key_env_var: str = "",
    ) -> None:
        self._api_key = str(api_key or "").strip()
        self._model = str(model or "").strip()
        self._language = language_code(language)
        try:
            count = int(passes)
        except (TypeError, ValueError):
            count = 1
        self._passes = max(1, min(count, STT_MAX_PASSES))
        self._base_url = str(base_url or GROQ_API_BASE).rstrip("/")
        self._key_env_var = str(key_env_var or "")
        self._last_failure_class = ""

    # ── Identity (no secret is ever exposed by these) ──

    @property
    def model(self) -> str:
        """The Whisper model this engine transcribes with."""
        return self._model

    @property
    def language(self) -> str:
        """The ISO-639-1 code sent with the request, or ``""`` for automatic."""
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
        """The dedicated transcription URL this engine posts to."""
        return f"{self._base_url}{TRANSCRIPTIONS_PATH}"

    @property
    def last_failure_class(self) -> str:
        """The bounded class of the most recent failure on this engine (or ``""``)."""
        return self._last_failure_class

    # ── The seam method ──

    def transcribe(self, audio: bytes) -> str:
        """Return the transcript of ``audio``; raises ``MediaError`` on failure.

        Empty input returns the seam's honest empty string (nothing was sent).
        A container Groq does not document is refused locally. With a pass count
        above one the SAME audio is recognised that many times and the hypotheses
        are reconciled by the existing STT-only consensus; otherwise one bounded
        transcription is attempted.
        """
        if not audio:
            return ""
        container = container_for(audio)
        if container is None:
            error = _error(
                "Groq does not accept this audio container for transcription.",
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
        """ONE transcription, at most one bounded transient retry, ceiling applied.

        The retry exists only for conditions that are transient by nature and
        only while the operation deadline has room; a deterministic failure ends
        the loop immediately instead of re-sending the same request. The single
        pass goes through the SAME character ceiling as the multi-pass route, so
        one route can never return more text than the other.
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
                transcript = _cap(self._request(audio, container, deadline, attempt=attempts))
            except MediaError as exc:
                failure = exc
                if not getattr(exc, "retryable", False):
                    break
                continue
            self._log_run(
                len(audio), len(transcript), started, attempts=attempts, hypotheses=1,
            )
            return transcript
        error = failure or _error(
            "Groq produced no transcription result.", FAILURE_TRANSPORT,
        )
        self._last_failure_class = failure_class_of(error)
        self._log_run(len(audio), 0, started, failed=True, error=error, attempts=attempts)
        raise error

    # ── Bounded multi-pass route (the existing consensus seam) ──

    def _run_passes(self, audio: bytes, container: tuple[str, str]) -> str:
        """N SEQUENTIAL passes over the SAME audio, reconciled, ONE deadline.

        Bounded in every dimension: the pass count is the owner's configured
        ceiling (already clamped by the control plane), only ONE request is ever
        in flight, and the whole operation runs inside the operation deadline — a
        pass starts only with meaningful budget left. A pass that failed
        contributes NO hypothesis (a transport problem can never be mistaken for a
        recognition result) and is never retried: the configured passes ARE the
        transient-recovery budget here. A DETERMINISTIC failure ends the loop at
        once, and when no pass produced a transcript the operation fails closed
        with the first, most informative failure.
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
                text = self._request(audio, container, deadline, attempt=index)
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
                "Groq produced no transcription result.", FAILURE_TRANSPORT,
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

    # ── The provider request ──

    def _request(
        self, audio: bytes, container: tuple[str, str], deadline: float, attempt: int = 1,
    ) -> str:
        """ONE multipart transcription request, inside the operation deadline."""
        left = _required_budget(deadline)
        upload_name, mime_type = container
        data: dict[str, str] = {
            "model": self._model,
            "response_format": _RESPONSE_FORMAT,
        }
        if self._language:
            data["language"] = self._language
        files = {_UPLOAD_FIELD: (upload_name, audio, mime_type)}
        headers = {"Authorization": f"Bearer {self._api_key}"}
        try:
            with self._client(_request_timeout(left)) as client:
                response = client.post(
                    self.endpoint, headers=headers, data=data, files=files,
                )
        except httpx.TimeoutException as exc:
            phase = _timeout_phase(exc)
            raise _error(
                f"Groq transcription timed out after {left:g}s ({phase} phase).",
                FAILURE_TIMEOUT, retryable=True, phase=phase,
            ) from exc
        except Exception as exc:  # noqa: BLE001 — the transport boundary
            raise _error(
                f"Groq transcription request failed ({type(exc).__name__}).",
                FAILURE_TRANSPORT,
                retryable=isinstance(exc, _TRANSIENT_TRANSPORT_ERRORS),
            ) from exc
        _raise_for_status(response, self._api_key)
        return _extract_text(response)

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
        transcript, the raw audio, the credential or a Telegram identifier.
        """
        logger.info(
            "GROQ_STT_ENGINE provider=%s endpoint=%s model=%s bytes=%d language=%s "
            "chars=%d elapsed_ms=%d status=%s attempts=%d passes=%d hypotheses=%d "
            "failure_class=%s",
            PROVIDER_NAME, TRANSCRIPTIONS_PATH, self._model or "-", byte_count,
            self._language or "auto", chars,
            int((time.monotonic() - started) * 1000) if started else 0,
            "failed" if failed else "ok", attempts, self._passes, hypotheses,
            _failure_field(error) if error is not None else "-",
        )


def build_engine(
    model: str,
    *,
    language: str = "",
    passes: int = 1,
    api_key: str = "",
    base_url: str = GROQ_API_BASE,
) -> tuple[GroqWhisperEngine | None, str]:
    """``(engine, reason)`` for a REGISTERED Groq model; ``engine`` is ``None``
    when this runtime cannot run it and ``reason`` is the bounded failure class.

    The model must be one of the models the control plane registers — an arbitrary
    string is refused here too, so a typed model id can never reach the provider.
    A missing credential yields ``None`` with ``missing_credential`` instead of an
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
        GroqWhisperEngine(
            key, resolved, language=language, passes=passes,
            base_url=base_url, key_env_var=key_env_var,
        ),
        "",
    )
