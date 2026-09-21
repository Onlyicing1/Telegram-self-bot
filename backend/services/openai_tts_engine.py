"""OpenAI speech adapter — the ONE Text-to-Speech provider of this phase.

This adapter implements the speech SYNTHESIS side of the media/AI stack. It is
deliberately not an STT component and shares nothing with the transcription
adapters: different endpoint, different direction, different failure vocabulary,
and a separate seam (``backend/services/tts_service``) that owns the request.

What it owns: ONE documented HTTP request to the provider's speech endpoint and
its classification.

    POST {AI_OPENAI_BASE_URL}/audio/speech
    Authorization: Bearer <credential>          Content-Type: application/json
    { "model": "...", "input": "<text>", "voice": "...",
      "response_format": "opus" }
    → 200 with the audio itself as the response body

The provider is chosen because the repository ALREADY declares everything it
needs and nothing new is introduced: the credential variables below are the same
ones ``backend/ai/providers/factory.py`` already resolves for OpenAI
(``AI_OPENAI_API_KEY``, then the ``OPENAI_API_KEY`` compatibility fallback), and
the base URL variable is that factory's own ``AI_OPENAI_BASE_URL``. No new
environment variable, no new dependency (the ``httpx`` stack every provider
adapter already uses), no SDK, and no credential handling beyond the existing
convention — an installation that already enabled OpenAI for chat needs no
additional key to speak.

Request discipline:

  * the model is CLOSED over :data:`SUPPORTED_MODELS` and the voice over
    :data:`SUPPORTED_VOICES`, so a typed or computed model/voice id can never
    reach the provider;
  * the input is the caller's already-validated text and nothing else: the body
    carries no chat id, message id, sender, caption, filename or conversation
    state, and this module has no parameter that could carry one;
  * the speech response format is requested explicitly (``opus``), which is the
    low-latency format Telegram's voice-note representation uses, so no local
    transcoding step and no audio processing dependency is introduced;
  * ONE request per call, no retry loop, every ``httpx`` phase bound DERIVED from
    the remaining budget the caller handed in, and the boundary above remains the
    single owner of the awaited timeout.

Every failure is classified into the boundary's closed taxonomy
(``backend/services/tts_service``) and carries its HTTP status when the provider
answered, so one incident is diagnosable from the log alone. A non-2xx is never
re-sent, and the credential is redacted from every message this module produces —
no error string, log line or exception can carry the key.
"""
from __future__ import annotations

import logging
import os
import time

import httpx

from backend.services.tts_service import (
    FAILURE_AUTH,
    FAILURE_DEADLINE,
    FAILURE_EMPTY_AUDIO,
    FAILURE_FORBIDDEN,
    FAILURE_INPUT_TOO_LARGE,
    FAILURE_INVALID_REQUEST,
    FAILURE_MALFORMED,
    FAILURE_MISSING_CREDENTIAL,
    FAILURE_OUTPUT_TOO_LARGE,
    FAILURE_QUOTA_EXCEEDED,
    FAILURE_RATE_LIMIT,
    FAILURE_REJECTION,
    FAILURE_SERVER,
    FAILURE_TIMEOUT,
    FAILURE_TRANSPORT,
    FAILURE_UNSUPPORTED_MODEL,
    FAILURE_UNSUPPORTED_VOICE,
    MAX_TTS_AUDIO_BYTES,
    TTS_STAGE_PROVIDER,
    TtsError,
)

logger = logging.getLogger(__name__)

#: The provider this adapter implements. Used for traces only.
PROVIDER_NAME = "openai"

#: The credential variables the repository ALREADY declares for OpenAI, in the
#: same precedence order as ``backend/ai/providers/factory.py``. No new secret and
#: no parallel configuration system: a deployment that already enabled OpenAI for
#: chat needs nothing added to speak.
API_KEY_ENV_VARS = ("AI_OPENAI_API_KEY", "OPENAI_API_KEY")

#: The base URL variable the same factory already declares, with the provider's
#: documented public base as the default. Read only when it is set, so a
#: deployment that never configured one (or configured a self-hosted gateway)
#: keeps its existing routing.
BASE_URL_ENV_VAR = "AI_OPENAI_BASE_URL"
DEFAULT_API_BASE = "https://api.openai.com/v1"

#: The ONE documented synthesis path.
SPEECH_PATH = "/audio/speech"

#: The ONLY model this adapter may be built with. It is the speech model the docs
#: recommend for the audio API; an arbitrary string is never accepted, so the
#: owner can never reach a model by typing one.
SPEECH_MODEL = "gpt-4o-mini-tts"
SUPPORTED_MODELS: tuple[str, ...] = (SPEECH_MODEL,)

#: The voice this phase speaks with. Deliberately ONE fixed voice: the first
#: phase is a clean execution path, not a voice-management system, and a fixed
#: voice keeps the synthesized result deterministic for the same text.
DEFAULT_VOICE = "alloy"

#: The voices the speech endpoint documents for this model. Closed on purpose —
#: ``build_engine`` refuses anything else, so a voice id can never be invented or
#: passed through from model output.
SUPPORTED_VOICES = frozenset({
    "alloy", "ash", "ballad", "coral", "echo", "fable", "nova", "onyx",
    "sage", "shimmer", "verse", "marin", "cedar",
})

#: The same allowlist as an ORDERED tuple, so a consumer that must present a
#: deterministic choice (the TTS control plane's voice list) does not have to
#: iterate a set. The default voice is first, and the two declarations are
#: pinned equal by the control plane's own registry build.
VOICE_ORDER: tuple[str, ...] = (
    DEFAULT_VOICE, "ash", "ballad", "coral", "echo", "fable", "nova", "onyx",
    "sage", "shimmer", "verse", "marin", "cedar",
)

#: The requested output format and the representation it produces. ``opus`` is
#: the documented low-latency speech format; Telegram's voice-note representation
#: is an OGG container with Opus audio, so the response is delivered as-is with no
#: local transcoding. The MIME type and file name are the adapter's, not the
#: caller's, so no untrusted name can reach Telegram.
RESPONSE_FORMAT = "opus"
AUDIO_MIME = "audio/ogg"
AUDIO_FILE_NAME = "speech.ogg"

#: ── Bounds ──
#:
#: The hard ceiling for ONE request, whatever budget a caller passes. The
#: boundary's own bound (``tts_service.TTS_TIMEOUT_S``) is shorter, so in practice
#: the caller's budget is what applies — this value only prevents a future caller
#: from turning the adapter into an unbounded request.
MAX_REQUEST_TIMEOUT_S = 120.0

#: A request is not worth starting with less than this left of the budget.
MIN_REQUEST_TIMEOUT_S = 8.0

#: The connect phase gets its own shorter bound, so a black-holed connection
#: fails fast instead of consuming the whole budget.
CONNECT_TIMEOUT_S = 10.0

#: The transport errors that are transient by nature (a retry of the SAME request
#: could legitimately succeed). Used only to mark ``retryable`` honestly.
_TRANSIENT_TRANSPORT_ERRORS = (
    httpx.TimeoutException,
    httpx.ConnectError,
    httpx.ReadError,
    httpx.WriteError,
    httpx.RemoteProtocolError,
    httpx.PoolTimeout,
)


def _error(
    message: str,
    failure_class: str,
    *,
    retryable: bool | None = None,
    http_status: int = 0,
) -> TtsError:
    """ONE classified adapter failure, in the boundary's own shape."""
    return TtsError(
        message,
        stage=TTS_STAGE_PROVIDER,
        failure_class=failure_class,
        http_status=http_status,
        retryable=retryable,
    )


def resolve_api_key() -> tuple[str, str]:
    """``(key, env_var_name)`` from the provider's OWN declared variables.

    The first variable that carries a value wins and the rest are not inspected,
    so this is the existing precedence order and never an environment sweep. The
    returned name is a variable NAME (never its value) and exists so a trace can
    say where the credential came from without naming the credential.
    """
    for name in API_KEY_ENV_VARS:
        value = (os.getenv(name) or "").strip()
        if value:
            return value, name
    return "", ""


def resolve_base_url() -> str:
    """The configured base URL, or the provider's documented public base."""
    configured = (os.getenv(BASE_URL_ENV_VAR) or "").strip()
    return (configured or DEFAULT_API_BASE).rstrip("/")


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
    """The provider's own bounded error identity (``code``/``type``), lowercased.

    Read only to tell the documented failure families apart — a refused voice, a
    refused model and a refused input are all 400s, and the provider's own token
    is the deterministic classifier. Free-form prose is never parsed.
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
    """Map a non-2xx response onto the boundary's controlled error contract.

    Every status is CLASSIFIED and carries its own HTTP status, and only the
    genuinely transient families (429 and 5xx) are marked retryable — a rejected
    credential, an unusable voice/model or a refused input is DETERMINISTIC and is
    never re-sent.
    """
    status = response.status_code
    if status < 400:
        return
    detail = _safe_detail(response, api_key)
    suffix = f" ({detail})" if detail else ""
    if status == 401:
        raise _error(
            f"The speech provider rejected the configured API key (HTTP {status}).{suffix}",
            FAILURE_AUTH, http_status=status,
        )
    if status == 403:
        raise _error(
            f"The speech provider refused this key access (HTTP {status}).{suffix}",
            FAILURE_FORBIDDEN, http_status=status,
        )
    if status == 429:
        code = _error_code(response)
        if "quota" in code or "billing" in code or "insufficient" in code:
            raise _error(
                f"The speech provider reports the account quota is spent "
                f"(HTTP {status}).{suffix}",
                FAILURE_QUOTA_EXCEEDED, http_status=status,
            )
        raise _error(
            "The speech provider rate limited the request (HTTP 429).",
            FAILURE_RATE_LIMIT, retryable=True, http_status=status,
        )
    if status == 404:
        raise _error(
            f"The speech provider has no such synthesis model (HTTP {status}).{suffix}",
            FAILURE_UNSUPPORTED_MODEL, http_status=status,
        )
    if status == 400:
        code = _error_code(response)
        if "voice" in code:
            raise _error(
                f"The speech provider refused the configured voice (HTTP {status}).{suffix}",
                FAILURE_UNSUPPORTED_VOICE, http_status=status,
            )
        if "model" in code:
            raise _error(
                f"The speech provider refused the synthesis model (HTTP {status}).{suffix}",
                FAILURE_UNSUPPORTED_MODEL, http_status=status,
            )
        if any(token in code for token in ("length", "token", "input", "long", "max")):
            raise _error(
                f"The speech provider refused the request as too long "
                f"(HTTP {status}).{suffix}",
                FAILURE_INPUT_TOO_LARGE, http_status=status,
            )
        raise _error(
            f"The speech provider refused the synthesis request (HTTP {status}).{suffix}",
            FAILURE_INVALID_REQUEST, http_status=status,
        )
    if status >= 500:
        raise _error(
            f"The speech provider is unavailable right now (HTTP {status}).",
            FAILURE_SERVER, retryable=True, http_status=status,
        )
    raise _error(
        f"The speech provider rejected the synthesis request (HTTP {status}).{suffix}",
        FAILURE_REJECTION, http_status=status,
    )


def _extract_audio(response: httpx.Response) -> bytes:
    """The audio of ONE accepted response, or a classified failure.

    The accepted body IS the audio (the endpoint returns the media itself, not a
    JSON envelope). A body this adapter cannot use is DETERMINISTIC: an empty body
    is ``empty_audio`` — never silent speech — and an over-sized body is
    ``output_too_large``, refused here so an anomalous response can never reach
    Telegram or the memory ceiling.
    """
    body = bytes(response.content or b"")
    if not body:
        raise _error(
            "The speech provider returned no audio.", FAILURE_EMPTY_AUDIO,
        )
    if len(body) > MAX_TTS_AUDIO_BYTES:
        raise _error(
            "The speech provider returned more audio than this runtime accepts.",
            FAILURE_OUTPUT_TOO_LARGE,
        )
    content_type = str(response.headers.get("content-type") or "").lower()
    if content_type and "json" in content_type:
        # A JSON body on the success path is a provider-side anomaly, not audio.
        raise _error(
            "The speech provider returned a response that is not audio.",
            FAILURE_MALFORMED,
        )
    return body


class OpenAiSpeechEngine:
    """ONE speech-synthesis engine behind the TTS service boundary.

    Async by construction: the provider call is an ``httpx.AsyncClient`` request,
    so no blocking network call can enter the event loop and no worker thread is
    involved. The engine is stateless apart from its credential, model, voice and
    base URL — it holds no request state, caches nothing and persists nothing, so
    provisioning one cannot alter the TTS contract.
    """

    __slots__ = ("_api_key", "_model", "_voice", "_base_url", "_key_env_var",
                 "_last_failure_class")

    def __init__(
        self,
        api_key: str,
        model: str,
        *,
        voice: str = DEFAULT_VOICE,
        base_url: str = DEFAULT_API_BASE,
        key_env_var: str = "",
    ) -> None:
        self._api_key = str(api_key or "").strip()
        self._model = str(model or "").strip()
        self._voice = str(voice or DEFAULT_VOICE).strip()
        self._base_url = str(base_url or DEFAULT_API_BASE).rstrip("/")
        self._key_env_var = str(key_env_var or "")
        self._last_failure_class = ""

    # ── Identity (no secret is ever exposed by these) ──

    @property
    def provider(self) -> str:
        """The provider this engine speaks through."""
        return PROVIDER_NAME

    @property
    def model(self) -> str:
        """The registered synthesis model this engine uses."""
        return self._model

    @property
    def voice(self) -> str:
        """The fixed voice this engine speaks with."""
        return self._voice

    @property
    def key_env_var(self) -> str:
        """The ENV variable the credential came from — the NAME only."""
        return self._key_env_var

    @property
    def mime_type(self) -> str:
        """The MIME type of the audio this adapter produces."""
        return AUDIO_MIME

    @property
    def file_name(self) -> str:
        """The file name the produced audio is delivered under."""
        return AUDIO_FILE_NAME

    @property
    def endpoint(self) -> str:
        """The synthesis URL this engine posts to."""
        return f"{self._base_url}{SPEECH_PATH}"

    @property
    def last_failure_class(self) -> str:
        """The bounded class of the most recent failure (or ``""``)."""
        return self._last_failure_class

    # ── The provider call ──

    async def speak(self, text: str, *, timeout_s: float = 0.0) -> bytes:
        """Synthesize ``text`` and return the audio bytes.

        ``timeout_s`` is the budget the caller already holds: this method derives
        every ``httpx`` phase bound from it and never invents a second deadline.
        The caller owns the awaited timeout around this call.

        Raises:
            TtsError: a classified failure.
        """
        deadline = time.monotonic() + _effective_timeout(timeout_s)
        left = deadline - time.monotonic()
        if left < MIN_REQUEST_TIMEOUT_S:
            error = _error(
                "The speech request did not start inside its own budget.",
                FAILURE_DEADLINE,
            )
            self._last_failure_class = error.failure_class
            raise error
        payload = {
            "model": self._model,
            "input": text,
            "voice": self._voice,
            "response_format": RESPONSE_FORMAT,
        }
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        started = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=_request_timeout(left)) as client:
                response = await client.post(self.endpoint, headers=headers, json=payload)
        except httpx.TimeoutException as exc:
            error = _error(
                f"Speech synthesis timed out after {left:g}s "
                f"({type(exc).__name__}).",
                FAILURE_TIMEOUT, retryable=True,
            )
            self._log_run(len(text), 0, started, failed=True, error=error)
            self._last_failure_class = error.failure_class
            raise error from exc
        except Exception as exc:  # noqa: BLE001 — the transport boundary
            error = _error(
                f"The speech request failed ({type(exc).__name__}).",
                FAILURE_TRANSPORT,
                retryable=isinstance(exc, _TRANSIENT_TRANSPORT_ERRORS),
            )
            self._log_run(len(text), 0, started, failed=True, error=error)
            self._last_failure_class = error.failure_class
            raise error from exc

        try:
            _raise_for_status(response, self._api_key)
            audio = _extract_audio(response)
        except TtsError as error:
            self._log_run(len(text), 0, started, failed=True, error=error)
            self._last_failure_class = error.failure_class
            raise
        self._log_run(len(text), len(audio), started)
        return audio

    # ── Observability (bounded, never payload/credential/Telegram metadata) ──

    def _log_run(
        self,
        text_chars: int,
        audio_bytes: int,
        started: float,
        *,
        failed: bool = False,
        error: TtsError | None = None,
    ) -> None:
        """ONE bounded, non-sensitive line per synthesis.

        Carries exactly what a live incident needs — provider, endpoint, model,
        voice, the INPUT LENGTH, the output size, elapsed time, the outcome and
        the failure class with its HTTP status — and never the synthesized text,
        the audio, the credential (only the variable NAME it came from) or a
        Telegram identifier.
        """
        logger.info(
            "OPENAI_TTS_ENGINE provider=%s endpoint=%s model=%s voice=%s "
            "format=%s chars=%d bytes=%d elapsed_ms=%d status=%s key_env_var=%s "
            "failure_class=%s",
            PROVIDER_NAME, SPEECH_PATH, self._model or "-", self._voice or "-",
            RESPONSE_FORMAT, text_chars, audio_bytes,
            int((time.monotonic() - started) * 1000) if started else 0,
            "failed" if failed else "ok",
            self._key_env_var or "-",
            _failure_field(error) if error is not None else "-",
        )


def _failure_field(error: TtsError) -> str:
    """The ``failure_class`` trace field: ``<class>`` or ``<class>:http=<status>``."""
    name = str(getattr(error, "failure_class", "") or FAILURE_TRANSPORT)
    status = int(getattr(error, "http_status", 0) or 0)
    return f"{name}:http={status}" if status else name


def _effective_timeout(timeout_s: float) -> float:
    """Clamp a caller-supplied budget to the adapter's own finite ceiling."""
    try:
        value = float(timeout_s)
    except (TypeError, ValueError):
        return MAX_REQUEST_TIMEOUT_S
    if value <= 0:
        return MAX_REQUEST_TIMEOUT_S
    return min(value, MAX_REQUEST_TIMEOUT_S)


def _request_timeout(left: float) -> httpx.Timeout:
    """Phase bounds DERIVED from the remaining budget (never a second deadline)."""
    return httpx.Timeout(
        connect=min(CONNECT_TIMEOUT_S, left),
        read=left,
        write=min(CONNECT_TIMEOUT_S, left),
        pool=min(CONNECT_TIMEOUT_S, left),
    )


def build_engine(
    model: str,
    *,
    voice: str = DEFAULT_VOICE,
    api_key: str = "",
    base_url: str = "",
) -> tuple[OpenAiSpeechEngine | None, str]:
    """``(engine, reason)`` for the REGISTERED synthesis model.

    ``engine`` is ``None`` when this runtime cannot speak and ``reason`` is the
    bounded failure class then: an unregistered model or voice is refused HERE
    too (so neither can be typed or computed into a request), and a missing
    credential yields ``missing_credential`` instead of an exception — speech
    synthesis is optional by contract, and the caller reports that state honestly
    rather than substituting a provider the owner did not select.
    """
    resolved_model = str(model or "").strip()
    if resolved_model not in SUPPORTED_MODELS:
        return None, FAILURE_UNSUPPORTED_MODEL
    resolved_voice = str(voice or "").strip()
    if resolved_voice not in SUPPORTED_VOICES:
        return None, FAILURE_UNSUPPORTED_VOICE
    key = str(api_key or "").strip()
    key_env_var = ""
    if key:
        key_env_var = "explicit"
    else:
        key, key_env_var = resolve_api_key()
    if not key:
        return None, FAILURE_MISSING_CREDENTIAL
    return (
        OpenAiSpeechEngine(
            key,
            resolved_model,
            voice=resolved_voice,
            base_url=str(base_url or "").strip() or resolve_base_url(),
            key_env_var=key_env_var,
        ),
        "",
    )
