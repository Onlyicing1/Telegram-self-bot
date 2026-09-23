"""Speechmatics speech adapter — the Speechmatics implementation of Text-to-Speech.

This is the SPEECH SYNTHESIS half of the Speechmatics stack. It shares the
provider's credential with the Speechmatics TRANSCRIPTION adapter
(``speechmatics_stt_engine``) and NOTHING else: a different service, the opposite
direction, and its own failure classification into the TTS boundary's closed
taxonomy (``backend/services/tts_service``).

What it owns: ONE documented HTTP request to the provider's speech endpoint and
its classification.

    POST https://preview.tts.speechmatics.com/generate/{voice}?output_format=wav_16000
    Authorization: Bearer <credential>          Content-Type: application/json
    { "text": "<text>" }
    → 200 with a complete WAV file as the response body

Verified against the provider's own documentation (Speechmatics → Text to speech
→ Quickstart), which is what turned this provider from a documented deferral into
an implementation:

  * the endpoint, the bearer header, the ``{"text": …}`` body and the
    ``output_format`` query parameter are the documented REST request;
  * ``wav_16000`` is the documented DEFAULT output format and is a **complete WAV
    file with headers** (16 kHz, 16-bit signed, mono) — one self-describing audio
    file, so the response is delivered as-is with no local transcoding and no
    audio dependency. The previous phase's deferral rested on a WAV container
    being unusable; a WAV clip is a format Telegram carries, so that reasoning is
    corrected here rather than left standing;
  * the documented voices are the four ids in :data:`VOICE_ORDER` (``sarah``,
    ``theo``, ``megan``, ``jack``), so a voice id can never be invented;
  * the provider exposes **no model parameter** for this service, so this
    adapter's registered "model" is the explicit empty string — the provider's own
    route, never an invented model name.

PERSIAN IS NOT CLAIMED. The provider's own documentation states plainly that it
supports **English** ("We support English. We plan to launch additional languages
…"), and all four documented voices are English (UK/US). Its registered voices
therefore carry the ``unsupported`` Persian capability state, with that evidence
recorded beside them — the same honest treatment the previous phase gave it.

Model/voice discipline is CLOSED: the model must be the registered empty default
and the voice must be in :data:`SUPPORTED_VOICES`, so a typed or computed
identifier can never reach the provider. The request carries the text being spoken
and nothing else — there is no parameter on this path that could carry a chat id,
message id, sender, caption, filename or conversation state.

ONE request per call, no retry loop, every ``httpx`` phase bound DERIVED from the
remaining budget the caller handed in; the TTS boundary above remains the single
owner of the awaited timeout. The credential is redacted from every message this
module produces, and the only credential fact it ever emits is the NAME of the
environment variable a credential came from.
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

#: The provider this adapter implements. Deliberately the SAME token the
#: transcription adapter uses, so one Speechmatics credential in the boundary
#: serves both directions — a test pins the credential declarations equal.
PROVIDER_NAME = "speechmatics"

#: The credential variable the repository's Speechmatics adapter ALREADY
#: declares, in the same precedence order. No new secret, no parallel
#: configuration system: a deployment that already transcribes with Speechmatics
#: needs nothing added to speak.
API_KEY_ENV_VARS = ("AI_SPEECHMATICS_API_KEY",)

#: The provider's documented TTS service host and path. Declared, not read from a
#: further environment variable.
API_BASE = "https://preview.tts.speechmatics.com"
GENERATE_PATH = "/generate"

#: The provider exposes no model parameter for this service. The registered
#: "model" is therefore the explicit empty string — the provider's own route — and
#: an invented model name can never be sent.
PROVIDER_MODEL = ""
SUPPORTED_MODELS: tuple[str, ...] = (PROVIDER_MODEL,)

#: The four documented voices, in the provider's own listing order. ``sarah`` is
#: first because the provider's own quickstart uses it. Closed on purpose — a
#: voice id can never be invented or passed through from model output.
DEFAULT_VOICE = "sarah"
VOICE_ORDER: tuple[str, ...] = ("sarah", "theo", "megan", "jack")
SUPPORTED_VOICES = frozenset(VOICE_ORDER)

#: The documented output format: a complete WAV file with headers (16 kHz, 16-bit
#: signed, mono). Sent EXPLICITLY even though it is the documented default, so the
#: request cannot drift with a provider-side default change. The MIME type and
#: file name are the adapter's, not the caller's.
OUTPUT_FORMAT = "wav_16000"
AUDIO_MIME = "audio/wav"
AUDIO_FILE_NAME = "speech.wav"

#: ── Bounds ──
#: The hard ceiling for ONE request, whatever budget a caller passes. The
#: boundary's own bound (``tts_service.TTS_TIMEOUT_S``) is shorter, so in practice
#: the caller's budget is what applies; this value only prevents a future caller
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


def endpoint(voice: str) -> str:
    """The ONE documented synthesis URL for a voice, format pinned in the query."""
    return (
        f"{API_BASE}{GENERATE_PATH}/{str(voice or DEFAULT_VOICE).strip()}"
        f"?output_format={OUTPUT_FORMAT}"
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
    """``(key, env_var_name)`` from this adapter's OWN declared variables.

    The first variable that carries a value wins and the rest are not inspected,
    so this is a declared precedence order and never an environment sweep. The
    returned name is a variable NAME (never its value).
    """
    for name in API_KEY_ENV_VARS:
        value = (os.getenv(name) or "").strip()
        if value:
            return value, name
    return "", ""


def _safe_detail(response: httpx.Response, api_key: str) -> str:
    """A short, sanitized provider detail for a failed request.

    The response body is never logged or surfaced beyond this bounded,
    whitespace-collapsed message, and the credential is redacted from it, so no
    request payload and no key can leak through an error string.
    """
    try:
        data = response.json()
    except Exception:  # noqa: BLE001 — a non-JSON error body is not a crash
        data = None
    message = ""
    if isinstance(data, dict):
        for key in ("message", "detail", "error"):
            value = data.get(key)
            if isinstance(value, str) and value:
                message = value
                break
            if isinstance(value, dict):
                inner = value.get("message")
                if isinstance(inner, str) and inner:
                    message = inner
                    break
    if not message:
        return ""
    detail = " ".join(message.split())[:160]
    if api_key:
        detail = detail.replace(api_key, "***")
    return detail


def _raise_for_status(response: httpx.Response, api_key: str) -> None:
    """Map a non-2xx response onto the boundary's controlled error contract.

    Every status is CLASSIFIED and carries its own HTTP status, and only the
    genuinely transient families (429 and 5xx) are marked retryable — a rejected
    credential, an unusable voice or a refused input is DETERMINISTIC and is never
    re-sent. The voice travels in the URL path, so the provider's own message is
    the only bounded classifier available for a refused voice.
    """
    status = response.status_code
    if status < 400:
        return
    detail = _safe_detail(response, api_key)
    suffix = f" ({detail})" if detail else ""
    lowered = detail.lower()
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
        if "quota" in lowered or "credit" in lowered or "billing" in lowered:
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
            f"The speech provider has no such voice (HTTP {status}).{suffix}",
            FAILURE_UNSUPPORTED_VOICE, http_status=status,
        )
    if status == 400:
        if "voice" in lowered:
            raise _error(
                f"The speech provider refused the configured voice (HTTP {status}).{suffix}",
                FAILURE_UNSUPPORTED_VOICE, http_status=status,
            )
        if "output_format" in lowered or "format" in lowered:
            raise _error(
                f"The speech provider refused the requested output format "
                f"(HTTP {status}).{suffix}",
                FAILURE_INVALID_REQUEST, http_status=status,
            )
        if any(token in lowered for token in ("length", "token", "too long", "maximum")):
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


class SpeechmaticsSpeechEngine:
    """ONE speech-synthesis engine behind the TTS service boundary.

    Async by construction: the provider call is an ``httpx.AsyncClient`` request,
    so no blocking network call can enter the event loop and no worker thread is
    involved. The engine is stateless apart from its credential and voice — it
    holds no request state, caches nothing and persists nothing.
    """

    __slots__ = ("_api_key", "_model", "_voice", "_key_env_var", "_last_failure_class")

    def __init__(
        self,
        api_key: str,
        model: str = PROVIDER_MODEL,
        *,
        voice: str = DEFAULT_VOICE,
        key_env_var: str = "",
    ) -> None:
        self._api_key = str(api_key or "").strip()
        self._model = str(model or "").strip()
        self._voice = str(voice or DEFAULT_VOICE).strip()
        self._key_env_var = str(key_env_var or "")
        self._last_failure_class = ""

    # ── Identity (no secret is ever exposed by these) ──

    @property
    def provider(self) -> str:
        """The provider this engine speaks through."""
        return PROVIDER_NAME

    @property
    def model(self) -> str:
        """The registered model token — the provider's own route."""
        return self._model

    @property
    def voice(self) -> str:
        """The chosen voice this engine speaks with."""
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
        return endpoint(self._voice)

    @property
    def last_failure_class(self) -> str:
        """The bounded class of the most recent failure (or ``""``)."""
        return self._last_failure_class

    def request_body(self, text: str) -> dict[str, object]:
        """The documented request body for ONE synthesis (no extra field)."""
        return {"text": text}

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
        payload = self.request_body(text)
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        started = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=_request_timeout(left)) as client:
                response = await client.post(
                    self.endpoint, headers=headers, json=payload,
                )
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

        Carries exactly what a live incident needs — provider, path, voice, the
        INPUT LENGTH, the output size, elapsed time, the outcome and the failure
        class with its HTTP status — and never the synthesized text, the audio,
        the credential (only the variable NAME it came from) or a Telegram
        identifier.
        """
        logger.info(
            "SPEECHMATICS_TTS_ENGINE provider=%s path=%s model=%s voice=%s "
            "format=%s chars=%d bytes=%d elapsed_ms=%d status=%s key_env_var=%s "
            "failure_class=%s",
            PROVIDER_NAME, GENERATE_PATH, self._model or "provider-default",
            self._voice or "-", OUTPUT_FORMAT, text_chars, audio_bytes,
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
) -> tuple[SpeechmaticsSpeechEngine | None, str]:
    """``(engine, reason)`` for the registered synthesis voice.

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
        SpeechmaticsSpeechEngine(
            key, resolved_model, voice=resolved_voice, key_env_var=key_env_var,
        ),
        "",
    )
