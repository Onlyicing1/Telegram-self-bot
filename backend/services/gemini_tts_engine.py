"""Gemini speech adapter — the Gemini implementation of Text-to-Speech.

This is the SPEECH SYNTHESIS half of the Gemini media stack. It shares the
Gemini API dialect with the transcription adapter (``gemini_media_engine``) but
NOTHING else: a different endpoint, the opposite direction, and its own failure
classification into the TTS boundary's closed taxonomy
(``backend/services/tts_service``).

What it owns: ONE documented HTTP request to the provider's speech-generation
endpoint and its classification.

    POST {GEMINI_API_BASE}/interactions
    x-goog-api-key: <credential>                Content-Type: application/json
    { "model": "...", "input": "<text>",
      "response_format": { "type": "audio" },
      "generation_config": { "speech_config": [ { "voice": "..." } ] } }
    → 200 with a completed interaction whose output block carries base64 audio

Verified against the provider's own documentation (Gemini API → "Text-to-speech
generation"):

  * the endpoint, the ``x-goog-api-key`` header and the exact body above are the
    documented REST request;
  * the documented TTS models are Gemini 3.1 Flash TTS Preview, Gemini 2.5 Flash
    Preview TTS and Gemini 2.5 Pro Preview TTS — all three are registered here
    and an unregistered model is refused;
  * the documented prebuilt voices are the 30 names in
    :data:`SUPPORTED_VOICES`;
  * the documented output is **raw PCM** (24 kHz, 16-bit, mono), so this adapter
    wraps it in a RIFF/WAVE container. That is a 44-byte header written in pure
    Python (``struct``) — NOT a transcode: no codec, no ffmpeg, no new
    dependency. The provider's own documented sample code does exactly this (it
    writes the decoded bytes into a wave file at 24000 Hz, 2 bytes, 1 channel);
  * the provider's own documentation lists Persian (``fa``) among the languages
    its TTS models support.

Model/voice discipline is CLOSED, exactly as the OpenAI adapter's is: the model
is closed over :data:`SUPPORTED_MODELS` and the voice over
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

import base64
import logging
import os
import struct
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
PROVIDER_NAME = "gemini"

#: The credential variables the repository ALREADY declares for Gemini, in the
#: same precedence order as ``backend/ai/providers/factory.py`` and the media
#: engine. No new secret and no parallel configuration system: a deployment that
#: already enabled Gemini for chat or transcription needs nothing added to speak.
#: A test pins this tuple equal to ``gemini_media_engine.API_KEY_ENV_VARS`` so the
#: two declarations cannot drift.
API_KEY_ENV_VARS = ("AI_GEMINI_API_KEY", "GEMINI_API_KEY")

#: The Gemini v1beta base the media engine and the provider adapter already use.
#: Declared here (not read from a new environment variable) so the TTS provider
#: cannot be pointed somewhere the rest of the project does not use; a test pins
#: it equal to ``gemini_media_engine.GEMINI_API_BASE``.
GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta"
INTERACTIONS_PATH = "/interactions"

#: The documented TTS models, in the provider's own presentation order. The first
#: entry is the default: it is the current (3.1) TTS model, the only one the
#: provider documents as supporting streaming, and the one all of its own
#: examples use. An unregistered string is never accepted, so the owner can never
#: reach a model by typing one.
DEFAULT_MODEL = "gemini-3.1-flash-tts-preview"
SUPPORTED_MODELS: tuple[str, ...] = (
    DEFAULT_MODEL,
    "gemini-2.5-flash-preview-tts",
    "gemini-2.5-pro-preview-tts",
)

#: The voice this adapter speaks with by default. The provider publishes no
#: single "default" voice, and every one of its own single-speaker examples uses
#: ``Kore``; deriving the default from that is a documented-by-example choice, not
#: an invented one.
DEFAULT_VOICE = "Kore"

#: The 30 prebuilt output voices the provider documents. The default voice is
#: FIRST (the same convention ``openai_tts_engine.VOICE_ORDER`` uses) and the
#: remaining 29 follow the provider's own listing order, so the presented sequence
#: is a deterministic function of one declaration. Closed on purpose —
#: ``build_engine`` refuses anything else, so a voice id can never be invented or
#: passed through from model output.
VOICE_ORDER: tuple[str, ...] = (
    "Kore", "Zephyr", "Puck", "Charon", "Fenrir", "Leda", "Orus", "Aoede",
    "Callirrhoe", "Autonoe", "Enceladus", "Iapetus", "Umbriel", "Algieba",
    "Despina", "Erinome", "Algenib", "Rasalgethi", "Laomedeia", "Achernar",
    "Alnilam", "Schedar", "Gacrux", "Pulcherrima", "Achird", "Zubenelgenubi",
    "Vindemiatrix", "Sadachbia", "Sadaltager", "Sulafat",
)
SUPPORTED_VOICES = frozenset(VOICE_ORDER)

#: The documented response modality: audio only. Sent explicitly so the request
#: cannot drift into a text answer.
RESPONSE_FORMAT: dict[str, str] = {"type": "audio"}

#: The synthesis response is raw PCM at the provider's documented rate/size and
#: a single channel. Wrapped in a RIFF/WAVE container so the clip is one
#: self-describing audio file Telegram can carry; the MIME type and file name are
#: the adapter's, never the caller's, so no untrusted name can reach Telegram.
PCM_SAMPLE_RATE = 24000
PCM_SAMPLE_WIDTH = 2
PCM_CHANNELS = 1
OUTPUT_FORMAT = "wav"
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


def endpoint() -> str:
    """The ONE documented synthesis URL."""
    return f"{GEMINI_API_BASE}{INTERACTIONS_PATH}"


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
    returned name is a variable NAME (never its value).
    """
    for name in API_KEY_ENV_VARS:
        value = (os.getenv(name) or "").strip()
        if value:
            return value, name
    return "", ""


def wrap_pcm_as_wav(
    pcm: bytes,
    *,
    sample_rate: int = PCM_SAMPLE_RATE,
    sample_width: int = PCM_SAMPLE_WIDTH,
    channels: int = PCM_CHANNELS,
) -> bytes:
    """A RIFF/WAVE container around raw PCM — a header, never a transcode.

    Deterministic and dependency-free (``struct`` only): the same 44-byte
    canonical header the provider's own documentation writes, followed by the
    provider's bytes unchanged, so no sample is touched.
    """
    payload = bytes(pcm)
    block_align = channels * sample_width
    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF",
        36 + len(payload),
        b"WAVE",
        b"fmt ",
        16,
        1,  # PCM
        channels,
        sample_rate,
        sample_rate * block_align,
        block_align,
        sample_width * 8,
        b"data",
        len(payload),
    )
    return header + payload


def _provider_error(response: httpx.Response, api_key: str) -> tuple[str, str]:
    """``(code_token, detail)`` from the provider's bounded error envelope.

    Only the provider's own machine-readable identity is read (its ``status``/
    ``code``/``type`` tokens and a short, whitespace-collapsed message), and the
    credential is redacted from whatever is kept. Free-form prose is never parsed
    to make a decision.
    """
    try:
        data = response.json()
    except Exception:  # noqa: BLE001 — a non-JSON error body is not a crash
        return "", ""
    if not isinstance(data, dict):
        return "", ""
    error = data.get("error")
    if isinstance(error, str):
        detail = " ".join(error.split())[:160]
        return "", detail.replace(api_key, "***") if api_key else detail
    if not isinstance(error, dict):
        return "", ""
    status = str(error.get("status") or "")
    code = str(error.get("code") or "")
    message = " ".join(str(error.get("message") or "").split())[:160]
    if api_key and message:
        message = message.replace(api_key, "***")
    return f"{status} {code}".strip().lower(), message


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
    code, detail = _provider_error(response, api_key)
    suffix = f" ({detail})" if detail else ""
    lowered = f"{code} {detail}".lower()
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
        # The provider answers 429 with ONE status token (``RESOURCE_EXHAUSTED``)
        # for both a per-minute limit and a spent quota, so the token alone cannot
        # tell them apart — only its own wording can. Without that wording the
        # class is ``rate_limit``: the honest reading of a 429 that says nothing
        # further, and the one the bounded pool/rotation machinery can act on
        # (rotate the key, then a substitute provider).
        if any(token in lowered for token in ("quota", "billing", "insufficient")):
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
        if "api key" in lowered or "api_key" in lowered:
            raise _error(
                f"The speech provider rejected the configured API key (HTTP {status}).{suffix}",
                FAILURE_AUTH, http_status=status,
            )
        if "voice" in lowered:
            raise _error(
                f"The speech provider refused the configured voice (HTTP {status}).{suffix}",
                FAILURE_UNSUPPORTED_VOICE, http_status=status,
            )
        if "model" in lowered:
            raise _error(
                f"The speech provider refused the synthesis model (HTTP {status}).{suffix}",
                FAILURE_UNSUPPORTED_MODEL, http_status=status,
            )
        if any(
            token in lowered
            for token in ("length", "token", "too long", "maximum", "max_output")
        ):
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


def _audio_payload(data: object) -> str:
    """The base64 audio string of ONE audio content item (or ``""``)."""
    if not isinstance(data, dict):
        return ""
    payload = data.get("data")
    if isinstance(payload, str) and payload:
        return payload
    for key in ("inline_data", "inlineData"):
        nested = data.get(key)
        if isinstance(nested, dict):
            value = nested.get("data")
            if isinstance(value, str) and value:
                return value
    return ""


def _extract_audio(data: object, api_key: str) -> bytes:
    """The audio of ONE completed synthesis interaction, or a classified failure.

    Reads the documented Interactions response shape the same way the
    transcription adapter reads the transcript: the ``model_output`` steps carry
    the content items, and the audio item is the one whose type is ``audio`` and
    whose ``data`` is the base64 payload. The provider documents its accessor as
    returning the LAST generated audio block, so the last one wins — never a
    concatenation that could interleave two blocks. ``output_audio`` (the
    documented accessor name) is accepted as a fallback when no such item exists,
    so one response shape is not silently assumed. A non-completed status, an
    unreadable body or an absent audio block is DETERMINISTIC and fails closed.
    """
    if not isinstance(data, dict):
        raise _error(
            "The speech provider returned an unreadable synthesis response.",
            FAILURE_MALFORMED,
        )
    status = str(data.get("status") or "").strip().lower()
    if status and status != "completed":
        raise _error(
            f"The speech provider did not complete the synthesis request ({status}).",
            FAILURE_MALFORMED,
        )

    payload = ""
    steps = data.get("steps")
    if isinstance(steps, list):
        for step in steps:
            if not isinstance(step, dict):
                continue
            if str(step.get("type") or "") != "model_output":
                continue
            content = step.get("content")
            if not isinstance(content, list):
                continue
            for item in content:
                if not isinstance(item, dict):
                    continue
                if str(item.get("type") or "").strip().lower() != "audio":
                    continue
                found = _audio_payload(item)
                if found:
                    payload = found
    if not payload:
        out = data.get("output_audio")
        if isinstance(out, dict):
            payload = _audio_payload(out)
        elif isinstance(out, str):
            payload = out
    if not payload:
        raise _error(
            "The speech provider returned a response with no audio.",
            FAILURE_EMPTY_AUDIO,
        )
    try:
        audio = base64.b64decode(payload, validate=False)
    except Exception as exc:  # noqa: BLE001 — a malformed payload is a provider anomaly
        raise _error(
            f"The speech provider returned audio this runtime cannot read "
            f"({type(exc).__name__}).",
            FAILURE_MALFORMED,
        ) from exc
    if not audio:
        raise _error(
            "The speech provider returned no audio.",
            FAILURE_EMPTY_AUDIO,
        )
    return audio


class GeminiSpeechEngine:
    """ONE speech-synthesis engine behind the TTS service boundary.

    Async by construction: the provider call is an ``httpx.AsyncClient`` request,
    so no blocking network call can enter the event loop and no worker thread is
    involved. The engine is stateless apart from its credential, model and voice —
    it holds no request state, caches nothing and persists nothing.
    """

    __slots__ = ("_api_key", "_model", "_voice", "_key_env_var", "_last_failure_class")

    def __init__(
        self,
        api_key: str,
        model: str,
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
        """The registered synthesis model this engine uses."""
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
            "model": self._model,
            "input": text,
            "response_format": dict(RESPONSE_FORMAT),
            "generation_config": {"speech_config": [{"voice": self._voice}]},
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
            "x-goog-api-key": self._api_key,
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
            pcm = _extract_audio(_decode_json(response), self._api_key)
            if len(pcm) + 44 > MAX_TTS_AUDIO_BYTES:
                raise _error(
                    "The speech provider returned more audio than this runtime accepts.",
                    FAILURE_OUTPUT_TOO_LARGE,
                )
            audio = wrap_pcm_as_wav(pcm)
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
            "GEMINI_TTS_ENGINE provider=%s endpoint=%s model=%s voice=%s "
            "format=%s chars=%d bytes=%d elapsed_ms=%d status=%s key_env_var=%s "
            "failure_class=%s",
            PROVIDER_NAME, INTERACTIONS_PATH, self._model or "-", self._voice or "-",
            OUTPUT_FORMAT, text_chars, audio_bytes,
            int((time.monotonic() - started) * 1000) if started else 0,
            "failed" if failed else "ok",
            self._key_env_var or "-",
            _failure_field(error) if error is not None else "-",
        )


def _decode_json(response: httpx.Response) -> object:
    """The accepted response's JSON body, or a classified failure."""
    try:
        return response.json()
    except Exception as exc:  # noqa: BLE001 — a non-JSON success body is an anomaly
        raise _error(
            f"The speech provider returned a response this runtime cannot read "
            f"({type(exc).__name__}).",
            FAILURE_MALFORMED,
        ) from exc


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
) -> tuple[GeminiSpeechEngine | None, str]:
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
        GeminiSpeechEngine(
            key, resolved_model, voice=resolved_voice, key_env_var=key_env_var,
        ),
        "",
    )
