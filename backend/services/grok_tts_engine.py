"""xAI Grok speech adapter — the Grok implementation of Text-to-Speech.

This is the SPEECH SYNTHESIS half of the Grok voice stack. Grok's voice APIs
also offer speech-to-speech and transcription; NEITHER is implemented here,
because neither is text-to-speech and neither produces the one artifact this
project's TTS contract is about (a synthesized audio clip for given text).

What it owns: ONE documented HTTP request to the provider's speech endpoint and
its classification.

    POST https://api.x.ai/v1/tts
    Authorization: Bearer <credential>          Content-Type: application/json
    { "text": "<text>", "voice_id": "<voice>", "language": "auto",
      "output_format": { "codec": "mp3", "sample_rate": 24000,
                         "bit_rate": 128000 } }
    → 200 with the audio itself as the response body

Verified against the provider's own documentation (xAI → Model Capabilities →
Text to Speech):

  * the endpoint, the bearer header and the body above are the documented REST
    request; ``language`` is REQUIRED and ``auto`` is a documented accepted value
    for automatic language detection, which is exactly the honest choice here —
    this adapter never guesses the spoken language of the owner's text;
  * the documented voices are the 28 ids in :data:`VOICE_ORDER`, all of which the
    provider states can speak every language it supports; ``eve`` is its default;
  * ``output_format`` is the documented output-format object, and the values sent
    are the provider's own documented default (MP3 at 24 kHz / 128 kbps) written
    out EXPLICITLY so the request cannot drift with a provider-side default
    change. MP3 is one of the formats Telegram accepts for a voice message, so
    the response is delivered as-is with no local transcoding and no audio
    dependency;
  * the provider has **no model parameter for this endpoint**, so this adapter's
    registered "model" is the explicit empty string — the provider's own default
    route, never an invented model name.

Model/voice discipline is CLOSED, exactly as the other adapters' is: the model
must be the registered empty default and the voice must be in
:data:`SUPPORTED_VOICES`, so a typed or computed identifier can never reach the
provider. The request carries the text being spoken and the minimum synthesis
configuration and nothing else — there is no parameter on this path that could
carry a chat id, message id, sender, caption, filename or conversation state.

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

#: The provider this adapter implements. The project's provider token for xAI's
#: Grok voice service, and the token the credential store keys on.
PROVIDER_NAME = "grok"

#: The credential variables this adapter declares, following the project's
#: existing ``AI_<PROVIDER>_API_KEY`` convention with the provider's own public
#: variable name as the compatibility fallback — the same shape
#: ``openai_tts_engine`` and ``speechmatics_stt_engine`` already use. A pool reads
#: THIS declaration (never a second table), so the deployment's environment stays
#: the first credential of the pool and additional keys come from the Vault.
API_KEY_ENV_VARS = ("AI_XAI_API_KEY", "XAI_API_KEY")

#: The provider's documented public API base. Declared, not read from a further
#: environment variable: no new secret, no new configuration system.
API_BASE = "https://api.x.ai/v1"
SPEECH_PATH = "/tts"

#: The provider exposes no model parameter on this endpoint. The registered
#: "model" is therefore the explicit empty string — the provider's own default
#: route — and an invented model name can never be sent.
PROVIDER_MODEL = ""
SUPPORTED_MODELS: tuple[str, ...] = (PROVIDER_MODEL,)

#: The documented voices, in the provider's own listing order. ``eve`` is the
#: provider's documented default and is therefore first. Closed on purpose — a
#: voice id can never be invented or passed through from model output.
DEFAULT_VOICE = "eve"
VOICE_ORDER: tuple[str, ...] = (
    "eve", "ara", "rex", "sal", "leo", "carina", "zagan", "helix", "orion",
    "luna", "iris", "altair", "zenith", "perseus", "helios", "lux", "kepler",
    "rigel", "cosmo", "celeste", "ursa", "sirius", "lumen", "castor", "naksh",
    "atlas", "aurora", "liora",
)
SUPPORTED_VOICES = frozenset(VOICE_ORDER)

#: Automatic language detection, the provider's documented value for letting the
#: service identify the language of the text it is given. Sent explicitly: this
#: adapter must never guess which language the owner's text is in.
LANGUAGE = "auto"

#: The documented output-format object, at the provider's documented default
#: (MP3 at 24 kHz / 128 kbps) written out explicitly. MP3 is one of the audio
#: formats Telegram accepts for a voice message, so no transcoding is needed and
#: no audio dependency is introduced. The MIME type and file name are the
#: adapter's, not the caller's.
OUTPUT_FORMAT = "mp3"
AUDIO_MIME = "audio/mpeg"
AUDIO_FILE_NAME = "speech.mp3"
OUTPUT_FORMAT_OBJECT: dict[str, object] = {
    "codec": "mp3",
    "sample_rate": 24000,
    "bit_rate": 128000,
}

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


def endpoint() -> str:
    """The ONE documented synthesis URL."""
    return f"{API_BASE}{SPEECH_PATH}"


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
        return ""
    message = ""
    if isinstance(data, dict):
        error = data.get("error")
        if isinstance(error, dict):
            message = str(error.get("message") or "")
        elif isinstance(error, str):
            message = error
        if not message:
            message = str(data.get("message") or "")
    elif isinstance(data, str):
        message = data
    if not message:
        return ""
    detail = " ".join(message.split())[:160]
    if api_key:
        detail = detail.replace(api_key, "***")
    return detail


def _error_code(response: httpx.Response) -> str:
    """The provider's own bounded error identity (``code``/``type``), lowercased.

    Read only to tell the documented failure families apart — a refused voice, a
    refused language and a refused input are all 400s, and the provider's own
    token is the deterministic classifier. Free-form prose is never parsed.
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
    return str(data.get("code") or "").strip().lower()


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
        lowered = f"{code} {detail}".lower()
        if "quota" in lowered or "billing" in lowered or "insufficient" in lowered:
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
            f"The speech provider has no such synthesis endpoint (HTTP {status}).{suffix}",
            FAILURE_UNSUPPORTED_MODEL, http_status=status,
        )
    if status == 400:
        code = _error_code(response)
        lowered = f"{code} {detail}".lower()
        if "voice" in lowered:
            raise _error(
                f"The speech provider refused the configured voice (HTTP {status}).{suffix}",
                FAILURE_UNSUPPORTED_VOICE, http_status=status,
            )
        if "language" in lowered:
            raise _error(
                f"The speech provider refused the requested language (HTTP {status}).{suffix}",
                FAILURE_INVALID_REQUEST, http_status=status,
            )
        if "model" in lowered:
            raise _error(
                f"The speech provider refused the synthesis model (HTTP {status}).{suffix}",
                FAILURE_UNSUPPORTED_MODEL, http_status=status,
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

    The accepted body IS the audio (the endpoint returns media, not a JSON
    envelope, unless timestamps were requested — and this adapter never requests
    them). A body this adapter cannot use is DETERMINISTIC: an empty body is
    ``empty_audio`` — never silent speech — and an over-sized body is
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
        # A JSON body on the success path means the endpoint answered with
        # something other than the raw audio this adapter asked for.
        raise _error(
            "The speech provider returned a response that is not audio.",
            FAILURE_MALFORMED,
        )
    return body


class GrokSpeechEngine:
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
        """The registered model token — the provider's own default route."""
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
        return endpoint()

    @property
    def last_failure_class(self) -> str:
        """The bounded class of the most recent failure (or ``""``)."""
        return self._last_failure_class

    def request_body(self, text: str) -> dict[str, object]:
        """The documented request body for ONE synthesis (no extra field)."""
        return {
            "text": text,
            "voice_id": self._voice,
            "language": LANGUAGE,
            "output_format": dict(OUTPUT_FORMAT_OBJECT),
        }

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

        Carries exactly what a live incident needs — provider, endpoint, voice,
        the INPUT LENGTH, the output size, elapsed time, the outcome and the
        failure class with its HTTP status — and never the synthesized text, the
        audio, the credential (only the variable NAME it came from) or a Telegram
        identifier.
        """
        logger.info(
            "GROK_TTS_ENGINE provider=%s endpoint=%s model=%s voice=%s "
            "format=%s chars=%d bytes=%d elapsed_ms=%d status=%s key_env_var=%s "
            "failure_class=%s",
            PROVIDER_NAME, SPEECH_PATH, self._model or "provider-default",
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
) -> tuple[GrokSpeechEngine | None, str]:
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
        GrokSpeechEngine(
            key, resolved_model, voice=resolved_voice, key_env_var=key_env_var,
        ),
        "",
    )
