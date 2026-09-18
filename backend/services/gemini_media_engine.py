"""
Gemini media engines — the remote OCR / speech-to-text engines behind the
EXISTING media seams.

This module adds no pipeline. ``backend/services/media_service.py`` remains the
single controlled media boundary: it resolves the media target deterministically,
transfers it under explicit limits, validates the container, enforces every bound,
owns the timeout, removes the temporary directory and normalizes the text. The
only thing this module supplies is the engine that turns ALREADY VALIDATED bytes
into text, through the two seams that boundary already declares:

    ``media_service.OcrEngine.recognize(image: bytes) -> str``
    ``media_service.SttEngine.transcribe(audio: bytes) -> str``

It therefore:
  * never downloads Telegram media (it never sees a Telegram object at all),
  * never resolves a message, never reads a caption, sender, chat id, message id,
    filename or conversation history — the engine's only inputs are the payload
    bytes and a fixed instruction,
  * never calls ``ProviderManager``, a provider adapter or ``vision()``; provider
    orchestration is untouched, and the media ANSWER still goes through the
    existing plain-string ``ProviderManager.chat`` path,
  * adds no local model, no Whisper/PyTorch/ONNX/Tesseract, no ffmpeg and no new
    dependency (the ``httpx`` stack the provider adapters already use),
  * creates no retry loop and no second timeout authority: ONE request per
    operation, bounded by the engine's own finite timeout, inside the boundary's
    existing OCR/STT timeout.

API dialect: the repository's Gemini provider adapter
(``backend/ai/providers/gemini.py``) already speaks the documented Generate
Content API (``POST {base}/models/{model}:generateContent``) — the same base URL,
the same ``contents`` / ``parts`` shape, the same camelCase field names. This
module speaks exactly that dialect, so the project has one Gemini API style, and
the key travels in the ``x-goog-api-key`` header (never in the URL, so a log line
or an error string can never echo it).

Model: :data:`DEFAULT_MEDIA_MODEL`, the smallest CURRENT Gemini model that accepts
both image and audio input on the documented free tier (see the module constant's
own note and ``IMPLEMENTATION_REPORT.md``). It is overridable through the
project's existing per-provider ENV convention. OCR ALWAYS uses that general
media model — it is never moved onto a speech model.

STT has a second, EXPLICITLY SELECTABLE route. When
:data:`STT_MODEL_ENV_VAR` names the documented dedicated speech-to-text model
(:data:`DEDICATED_TRANSCRIPTION_MODEL`), ``transcribe`` uses that model's OWN
documented request (the Interactions API, ``POST /v1beta/interactions`` with a
``generation_config.transcription_config``), not the Generate Content request:
the two API surfaces differ, and the dedicated model is only reachable through
its own. With the variable unset, ``transcribe`` keeps the M1.5c behaviour
exactly (the general media model answering ``STT_INSTRUCTION``). The route is a
deployment decision, never a runtime fallback: the dedicated model failing
raises the boundary's honest ``MediaError`` and the operation does NOT retry on
another model, so the recognition comparison stays interpretable.

Bounded transport, ONE deadline, and classified failures. One media operation now
carries a SINGLE explicit wall-clock deadline (inside the boundary's own bound),
and every HTTP timeout is DERIVED from what is left of it: the connect/write/pool
phases get tight fixed bounds and the read phase gets the remaining budget, so no
socket phase can outlive the operation. A spent budget raises its OWN explicit
deadline failure instead of silently degrading into a near-zero HTTP timeout —
which is what the previous per-socket-phase bound did.

Bounded multi-pass recognition (the accuracy seam, OFF by default). The
recognition-quality class (``INVESTIGATION.md`` §19) is addressed by exactly ONE
opt-in mechanism: ``AI_GEMINI_STT_PASSES``. Unset — or ``1`` — keeps the
single-pass behaviour above, byte-identical, so no installation changes without
asking. With ``2`` or ``3``, ``transcribe`` performs that many SEQUENTIAL
recognition passes over the SAME audio under the SAME operation deadline (the
audio is uploaded ONCE and every pass references that one uploaded file) and
reconciles the hypotheses with the pure STT-only consensus in
``backend/services/stt_consensus.py``.

A pass is a RECOGNITION attempt and never a transport retry: it is counted only
when it actually returned a transcript, a deterministic failure stops the pass
loop outright instead of being re-sent, and a transient one simply contributes no
hypothesis. The loop is bounded by the configured count AND by the deadline (a
pass starts only with budget left), so no configuration can exceed the deadline
or issue more requests than the configured number of passes. The two concepts
stay separate in the code and in the traces: ``attempts`` counts provider
requests, ``passes``/``hypotheses`` count recognition results.

The dedicated transcription route sends the audio representation the official
documentation shows for its model (the Files API ``uri`` form). The documented
inline ``data`` form is used only as ONE bounded fallback when the FIRST attempt
fails for a TRANSIENT reason, the payload fits the inline budget and the deadline
still has room. At most :data:`STT_MAX_ATTEMPTS` SEQUENTIAL attempts exist per
operation: no retry loop, no concurrency, no second model.

Audio larger than the documented inline request budget uses the SAME Files API
upload flow, and the uploaded file is deleted in a ``finally`` block, so nothing
is retained remotely beyond the operation — no second download, no second upload
subsystem. Every failure is CLASSIFIED (upload / upload-timeout / file-processing
/ interaction / interaction-timeout / HTTP rejection / malformed response /
deadline) and a timeout records the socket PHASE that expired, so a live incident
is diagnosable afterwards instead of collapsing into one generic sentence.
"""
from __future__ import annotations

import base64
import logging
import os
import time
from typing import Any, Mapping

import httpx

from backend.ai.providers.base.defaults import resolve_model
from backend.services import media_service
from backend.services.media_service import MediaError
from backend.services.stt_consensus import reconcile_hypotheses

logger = logging.getLogger(__name__)

#: The API base and the resumable-upload base of the SAME Gemini API the
#: repository's provider adapter already uses (``backend/ai/providers/gemini.py``
#: defines the identical v1beta base). One dialect, one base URL.
GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta"
GEMINI_UPLOAD_BASE = "https://generativelanguage.googleapis.com/upload/v1beta"

#: The Gemini credential variables the repository ALREADY declares, in the same
#: precedence order as ``backend/ai/providers/factory.py`` and the discovery
#: catalog (``backend/ai/discovery.py``). No parallel configuration system and no
#: new secret is introduced: an installation that already enabled Gemini for chat
#: needs no additional key for media.
API_KEY_ENV_VARS = ("AI_GEMINI_API_KEY", "GEMINI_API_KEY")

#: Media processing gets its OWN optional model override, so OCR/STT can stay on
#: a small, fast, cheap model even when the CHAT model is a larger one. Both names
#: follow the project's existing ``AI_<PROVIDER>_MODEL`` convention.
MEDIA_MODEL_ENV_VAR = "AI_GEMINI_MEDIA_MODEL"
CHAT_MODEL_ENV_VAR = "AI_GEMINI_MODEL"

#: The smallest CURRENT Gemini model that satisfies BOTH media contracts.
#: Verified against the official documentation on 2026-09-16: the Gemini API
#: pricing page lists ``gemini-3.5-flash-lite`` with a free tier of free input and
#: output tokens (paid input $0.30 per 1M for text/image/video/audio), and the
#: audio-understanding model/MIME table lists Gemini 3.5 Flash-Lite among the
#: models that accept audio input. It is the cheapest current Flash-Lite that
#: accepts both image and audio, which is exactly what one engine serving two
#: seams needs. No dedicated transcription model is depended on here: the STT
#: section below is OPT-IN, so the default path uses no model this project
#: cannot verify.
DEFAULT_MEDIA_MODEL = "gemini-3.5-flash-lite"

#: The ONLY instruction sent for OCR. Deliberately minimal, deterministic and
#: static: it names no chat, no person, no message and no conversation, and it
#: forbids exactly what the boundary forbids (summarizing, interpreting,
#: inventing text).
OCR_INSTRUCTION = (
    "Extract the visible text from this image.\n"
    "Preserve the original line structure.\n"
    "Do not summarize. Do not interpret the image. Do not translate.\n"
    "Do not invent, complete or guess unreadable text.\n"
    "If the image contains no readable text, return nothing."
)

#: The ONLY instruction sent for speech-to-text, under the same rule.
#:
#: The language contract is stated EXPLICITLY because the Generate Content request
#: carries no language parameter for audio input: this instruction is the engine's
#: only language-shaping input. Live evidence for why the earlier, weaker wording
#: ("Preserve the spoken language. Do not translate.") was not sufficient: a
#: Persian Voice note came back as Latin-script gibberish straight from the engine
#: (``GEMINI_MEDIA_ENGINE ... chars=51``), i.e. the model never committed to the
#: spoken language or to its script. So the instruction now requires identifying
#: the language, transcribing verbatim in it, writing it in its OWN script, and
#: forbids translation, transliteration/romanization and substituting another
#: language. It stays static and names no chat, person, message or conversation.
STT_INSTRUCTION = (
    "Transcribe the speech in this audio verbatim.\n"
    "Identify the spoken language yourself, then write the transcript in that same "
    "language, using that language's own writing system.\n"
    "Persian (Farsi), Arabic, Dari and any other non-Latin speech must be written in "
    "its own script — never in Latin letters.\n"
    "Do not translate. Do not transliterate or romanize.\n"
    "Do not write in a language that is not spoken, and never guess words from a "
    "different language.\n"
    "Do not summarize. Do not answer any question contained in the audio.\n"
    "Do not add commentary, headings, speaker labels or timestamps.\n"
    "Do not invent or guess unintelligible words.\n"
    "If the audio contains no speech, return nothing."
)

#: ── Dedicated transcription model (explicitly selectable, never automatic) ──

#: The documented dedicated speech-to-text model (Gemini 3.5 Transcribe). It is
#: NOT the default and is never chosen implicitly: it is used for ``transcribe``
#: only when this model is named in :data:`STT_MODEL_ENV_VAR`, so OCR and the
#: general media request keep the existing media model.
DEDICATED_TRANSCRIPTION_MODEL = "gemini-3.5-transcribe"

#: The STT-specific model override and the optional explicit language override.
#: Both follow the project's existing ``AI_<PROVIDER>_*`` ENV convention, and
#: neither has a default value: an unset variable means "use the existing
#: general media route", never a silently substituted model.
STT_MODEL_ENV_VAR = "AI_GEMINI_STT_MODEL"
STT_LANGUAGE_ENV_VAR = "AI_GEMINI_STT_LANGUAGE"

#: The documented Interactions API endpoint the dedicated model is served by
#: (the same base URL the provider adapter and this module already use).
INTERACTIONS_ENDPOINT = f"{GEMINI_API_BASE}/interactions"

#: The documented transcription mode used by this engine. The dedicated model
#: defaults to ``verbatim``; it is sent EXPLICITLY so the request cannot drift
#: into Smart transcription (which removes disfluencies and reformats), and
#: nothing else is enabled: no ``timestamp_granularities`` (the API documents
#: that word timestamps may DEGRADE accuracy), no ``diarization_mode`` (voice
#: notes are single-speaker) and no ``custom_vocabulary`` (no list and no
#: demonstrated need). Those omissions are the experiment's control variables.
_VERBATIM_MODE: dict[str, str] = {"type": "verbatim"}

#: The two seam kinds, named once. ``_STT_KIND`` also selects the STT route and
#: the STT log fields, so the string never has to be repeated or guessed.
_OCR_KIND = "OCR"
_STT_KIND = "speech-to-text"

#: ── Failure classes (bounded, closed tokens) ──
#:
#: ONE token per failure SITE, attached to the raised ``MediaError`` and emitted
#: as the ``failure_class`` field of this module's own trace line. They are the
#: missing half of the previous observability: an incident could say "timed out"
#: but never WHICH leg, WHICH socket phase or WHICH HTTP status produced it. They
#: are operational metadata only — never payload, a credential or a Telegram id.
FAILURE_UPLOAD = "upload_failed"
FAILURE_UPLOAD_TIMEOUT = "upload_timeout"
FAILURE_FILE_PROCESSING = "file_processing"
FAILURE_INTERACTION = "interaction_failed"
FAILURE_INTERACTION_TIMEOUT = "interaction_timeout"
FAILURE_REQUEST = "request_failed"
FAILURE_REQUEST_TIMEOUT = "request_timeout"
FAILURE_HTTP = "http_rejection"
FAILURE_MALFORMED = "malformed_response"
FAILURE_TRANSPORT = "transport_failure"
FAILURE_DEADLINE = "operation_deadline"

#: The two audio representations of the dedicated route. ``uri`` is the form the
#: official documentation shows for the dedicated transcription model (the Files
#: API); ``inline`` is the documented Interactions audio form (base64 ``data``)
#: that the model's own transcription guide does not demonstrate, which is why it
#: is only ever the bounded FALLBACK.
_TRANSPORT_URI = "uri"
_TRANSPORT_INLINE = "inline"

#: The ``httpx`` timeout subclasses, most specific first, mapped onto the socket
#: phase they expired in. Recording the phase is what separates "the connection
#: never came up" from "the provider never finished answering".
_TIMEOUT_PHASES: tuple[tuple[type[BaseException], str], ...] = (
    (httpx.ConnectTimeout, "connect"),
    (httpx.WriteTimeout, "write"),
    (httpx.PoolTimeout, "pool"),
    (httpx.ReadTimeout, "read"),
)

#: The ONLY failures a bounded second attempt may repeat: transport conditions
#: that are transient by nature. Deterministic failures — HTTP 4xx (except 429),
#: an unreadable body, a malformed shape, an empty transcript, a refused container
#: — are deliberately absent, so no deterministic error is ever re-sent.
_TRANSIENT_TRANSPORT_ERRORS: tuple[type[BaseException], ...] = (
    httpx.TimeoutException,
    httpx.ConnectError,
    httpx.ReadError,
    httpx.WriteError,
    httpx.RemoteProtocolError,
)

#: Engine-level finite wall-clock bounds, each comfortably INSIDE the boundary's
#: own bound (``media_service.OCR_TIMEOUT_S`` 45s, ``STT_TIMEOUT_S`` 60s), so the
#: engine fails with its own precise reason before the boundary's outer bound and
#: no request can hang.
OCR_TIMEOUT_S = 30.0

#: ── The ONE STT operation deadline (replaces the per-phase HTTP bound) ──
#:
#: ``httpx.Client(timeout=X)`` bounds each SOCKET PHASE (connect, write, read,
#: pool) separately — it is not a bound on the operation. The previous STT bound
#: was such a per-phase 40 s value, which is why a live incident could only say
#: "timed out after 40s" without being able to say which leg or which phase was
#: still in flight (INVESTIGATION §20.7, §20.8, §20.17). This constant is instead
#: the WHOLE operation: upload + file readiness + the transcription request + the
#: single bounded retry/fallback. Every request timeout is derived from what is
#: left of it, so the sum of the legs can never exceed it and the deadline is
#: enforced as an explicit failure rather than as a hidden socket bound.
#:
#: Value derivation (not an arbitrary inflation of 40): the boundary's own bound
#: is ``media_service.STT_TIMEOUT_S`` = 60 s, and the worst case here is one
#: deadline plus one bounded cleanup (45 + 5 = 50 s), leaving ~10 s of margin for
#: the boundary's worker-thread return path — so the media failure the owner sees
#: is always the engine's precise reason and never the boundary's generic one.
STT_OPERATION_DEADLINE_S = 45.0

#: Phase bounds INSIDE the operation deadline: a stalled handshake or a stalled
#: request transmission may never consume the whole operation, while the response
#: body is allowed to use whatever budget is left.
STT_CONNECT_TIMEOUT_S = 10.0
STT_WRITE_TIMEOUT_S = 20.0

#: The remote DELETE is cleanup, not part of the outcome: it is bounded on its own
#: so a slow cleanup can never delay (or outlive) the owner-visible failure, which
#: the previous full per-phase cleanup bound could do.
STT_CLEANUP_TIMEOUT_S = 5.0

#: A second attempt (a transient retry, or the fallback from the documented ``uri``
#: representation to the inline one) starts only when at least this much of the
#: operation deadline is left, so a retry can never push the operation past its
#: own deadline.
STT_MIN_ATTEMPT_S = 8.0

#: Hard ceiling on provider attempts per operation: the initial attempt plus at
#: most ONE retry/fallback. No loop can exceed it, and only ONE attempt is ever in
#: flight at a time.
STT_MAX_ATTEMPTS = 2

#: ── Bounded multi-pass recognition (the accuracy seam, OFF by default) ──
#:
#: The configured number of SEQUENTIAL recognition passes over the SAME audio,
#: reconciled by the pure STT-only consensus
#: (``backend/services/stt_consensus.py``). Unset — and therefore the default — is
#: ONE pass: exactly the single-pass behaviour above, byte for byte.
#:
#: ``3`` is the only other count that can resolve anything, and that is a
#: property of the consensus rule rather than tuning: a two-hypothesis
#: disagreement is a 1-1 tie, and a tie resolves to the first pass, so two passes
#: spend a second model call to change nothing. ``2`` is nevertheless accepted,
#: because the repeat-run consistency measurement of the benchmark needs to be
#: able to run exactly two passes. The ceiling is ``3`` because every pass is a
#: full model call: a three-pass run that cannot fit the deadline yields fewer
#: hypotheses and the operation still succeeds honestly.
STT_PASSES_ENV_VAR = "AI_GEMINI_STT_PASSES"
STT_MAX_PASSES = 3

#: Finite output ceiling per request. The boundary caps characters at
#: ``MAX_OCR_CHARS`` / ``MAX_STT_CHARS`` (16 000 ≈ 4 000–8 000 tokens), so this is
#: generous enough never to be the binding limit for text the project would keep,
#: while still being a hard, explicit bound.
MAX_OUTPUT_TOKENS = 8_192

#: Deterministic sampling: media recognition is extraction, not generation.
SAMPLING_TEMPERATURE = 0.0

#: Inline (base64 in the request) budget. The documented inline request limit is
#: 20 MB total, and base64 inflates the payload by ≈4/3, so 15 MiB of raw bytes
#: stays inside it with room for the prompt. Anything larger uses the documented
#: Files API upload flow — the existing ``MAX_STT_INPUT_BYTES`` (20 MiB) bound is
#: therefore still honoured rather than being narrowed to fit an inline request.
INLINE_PAYLOAD_MAX_BYTES = 15 * 1024 * 1024

#: The remote display name for an uploaded file. Deliberately static and
#: non-identifying: a Telegram filename must never reach the remote API.
_UPLOAD_DISPLAY_NAME = "lifeos-media"

#: Bounded readiness checks for an uploaded file (Files API), so a still-PROCESSING
#: file is never used and the wait can never be unlimited. No polling loop exists
#: beyond this.
_FILE_READY_ATTEMPTS = 5
_FILE_READY_DELAY_S = 1.0

#: Maps the MIME types this project's media boundary validates onto the MIME types
#: Gemini documents for image input. The boundary already proved the container
#: signature, so this mapping only normalises aliases (``image/jpg`` is the same
#: JPEG container Gemini documents as ``image/jpeg``). Types the boundary accepts
#: but Gemini does NOT document (BMP, GIF) have no entry and are refused
#: deterministically instead of being sent and rejected remotely.
_GEMINI_IMAGE_MIME_TYPES: dict[str, str] = {
    "image/png": "image/png",
    "image/jpeg": "image/jpeg",
    "image/jpg": "image/jpeg",
    "image/webp": "image/webp",
}

#: Same for audio: every entry is an OGG / WAV / FLAC container the boundary
#: verified, mapped onto the MIME type Gemini documents for audio input. The list
#: is NOT broadened — no MP3, M4A, webm or AAC support is added here.
_GEMINI_AUDIO_MIME_TYPES: dict[str, str] = {
    "audio/ogg": "audio/ogg",
    "audio/opus": "audio/ogg",
    "application/ogg": "audio/ogg",
    "audio/wav": "audio/wav",
    "audio/x-wav": "audio/wav",
    "audio/wave": "audio/wav",
    "audio/vnd.wave": "audio/wav",
    "audio/flac": "audio/flac",
    "audio/x-flac": "audio/flac",
}

#: The audio MIME types Gemini documents for the dedicated transcription model —
#: exactly the ones :func:`gemini_mime_type` can produce for audio. Used to
#: refuse a non-audio payload on the dedicated route instead of sending an image
#: to a speech model. The accepted-format list is NOT broadened.
_TRANSCRIPTION_AUDIO_MIME_TYPES = frozenset(_GEMINI_AUDIO_MIME_TYPES.values())

#: Container signatures, mirrored from the boundary's own validators. The bytes
#: reaching an engine have already been corroborated against a declared type by
#: ``media_service``; sniffing here only decides WHICH documented Gemini MIME type
#: describes the payload that was validated, since the seam's one-argument
#: contract carries bytes and no MIME.
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_GIF_MAGICS = (b"GIF87a", b"GIF89a")
_OGG_MAGIC = b"OggS"
_FLAC_MAGIC = b"fLaC"


def _sniff_mime(data: bytes) -> str:
    """Return the container MIME type the payload itself declares, or ``""``.

    Deterministic and dependency-free: only the bytes already validated by the
    media boundary are inspected, and nothing is decoded.
    """
    if data.startswith(_PNG_MAGIC):
        return "image/png"
    if data[:2] == b"\xff\xd8":
        return "image/jpeg"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:6] in _GIF_MAGICS:
        return "image/gif"
    if data[:2] == b"BM":
        return "image/bmp"
    if data.startswith(_OGG_MAGIC):
        return "audio/ogg"
    if data.startswith(b"RIFF") and data[8:12] == b"WAVE":
        return "audio/wav"
    if data.startswith(_FLAC_MAGIC):
        return "audio/flac"
    return ""


def gemini_mime_type(data: bytes) -> str:
    """The documented Gemini MIME type for ``data``, or raise ``MediaError``.

    The payload's own container decides; the declared Telegram MIME type is not
    consulted (the boundary already corroborated it) and is never forwarded. A
    container Gemini does not document for the relevant input is refused
    deterministically, before any request is sent.
    """
    sniffed = _sniff_mime(data)
    if sniffed in _GEMINI_IMAGE_MIME_TYPES:
        return _GEMINI_IMAGE_MIME_TYPES[sniffed]
    if sniffed in _GEMINI_AUDIO_MIME_TYPES:
        return _GEMINI_AUDIO_MIME_TYPES[sniffed]
    if sniffed:
        raise MediaError(
            f"Gemini does not accept {sniffed} media for extraction on this runtime."
        )
    raise MediaError("The media payload is not a container Gemini can read.")


def stt_instruction(language_code: str = "") -> str:
    """The instruction for the GENERAL media model's STT request.

    Byte-identical to :data:`STT_INSTRUCTION` when no language is configured, so
    the existing multilingual contract is untouched by default. A configured
    language only APPENDS one deterministic sentence naming its BCP-47 code —
    it never replaces the instruction, never names a chat/person/message and
    never varies per request.
    """
    code = str(language_code or "").strip()
    if not code:
        return STT_INSTRUCTION
    return (
        STT_INSTRUCTION
        + f"\nThe spoken language is identified by the BCP-47 code {code};"
        " transcribe it in that language and in its own writing system."
    )


def resolve_stt_passes() -> tuple[int, str]:
    """``(passes, env_var_name)`` for STT; ``(1, "")`` means the single-pass route.

    Unset — the default — is ONE pass, i.e. exactly the existing behaviour. A
    value below ``1`` or above :data:`STT_MAX_PASSES` is clamped instead of
    trusted, and a value that is not an integer falls back to one pass with a
    bounded warning (the value itself is never logged), so a typo in ENV can
    neither fail startup nor create an unbounded number of model calls.
    """
    raw = (os.getenv(STT_PASSES_ENV_VAR) or "").strip()
    if not raw:
        return 1, ""
    try:
        value = int(raw)
    except ValueError:
        logger.warning("GEMINI_MEDIA_ENGINE_STT_PASSES_INVALID reason=not-an-integer")
        return 1, STT_PASSES_ENV_VAR
    return max(1, min(value, STT_MAX_PASSES)), STT_PASSES_ENV_VAR


#: ── The OWNER'S persisted STT settings (the AI Settings surface) ──
#:
#: The three behavioral STT settings are owned by the owner's persisted AI
#: configuration (``ai_config`` through ``backend/ai/config_store.py``) and edited
#: from Telegram (AI -> Settings -> Advanced). The three keys are ``stt_model``,
#: ``stt_language`` and ``stt_passes``. This module never reads that store
#: and never learns an owner id: the CALLER — the runtime supervisor at startup,
#: the AI Settings handler after a save — reads the store and hands the plain
#: values in. The credential and the general media model keep their existing ENV
#: resolution, because those are deployment concerns rather than owner settings.

#: The default of each setting when the owner has configured nothing. Every one
#: of them is a REAL behavior, not an absence marker: no dedicated model is the
#: general media model, an empty language is automatic detection, and one pass is
#: the single-pass route.
STT_SETTING_DEFAULTS: dict[str, Any] = {
    "stt_model": "",
    "stt_language": "",
    "stt_passes": 1,
}

#: The documented value of the language setting that means "detect the language
#: automatically". The Settings control writes it explicitly when the owner asks
#: for automatic detection (Telegram cannot send an empty message, and an empty
#: stored value has to stay what it is: nothing configured). It becomes the empty
#: engine value, which is what makes the request send no ``language_codes``.
STT_LANGUAGE_AUTO = "auto"


def stt_settings_from(config: Mapping[str, Any] | None) -> dict[str, Any]:
    """The three behavioral STT settings as ENGINE values → ``{key: value}``.

    Pure and deterministic: the owner's stored values are the input (the caller
    reads them through the existing config store), a missing key yields the
    documented default, and nothing here reads ENV, the database or Telegram. A
    model id passes through the project's existing deprecation map — a retired
    alias is substituted exactly as it is for every other model selection, and an
    unknown id is returned verbatim, never replaced by an invented one. The
    language ``auto`` becomes the empty value, and the pass count is held inside
    ``1..STT_MAX_PASSES`` as the engine's own invariant (the Settings control
    rejects an out-of-range value instead of storing one).
    """
    config = config or {}
    model = str(config.get("stt_model") or "").strip()
    if model:
        model = resolve_model("gemini", model)
    language = str(config.get("stt_language") or "").strip()
    if language.lower() == STT_LANGUAGE_AUTO:
        language = ""
    try:
        passes = int(config.get("stt_passes"))
    except (TypeError, ValueError):
        passes = STT_SETTING_DEFAULTS["stt_passes"]
    return {
        "stt_model": model,
        "stt_language": language,
        "stt_passes": max(1, min(passes, STT_MAX_PASSES)),
    }


def apply_stt_settings(
    stt_settings: Mapping[str, Any] | None,
    credential: tuple[str, str] | None = None,
) -> dict[str, Any]:
    """Rebuild the STT seam's engine from the owner's PERSISTED STT settings.

    ONE entry point, called by the two places that own this state: the runtime
    supervisor at startup (so the persisted values are in effect from the first
    transcription) and the AI Settings handler immediately after a save (so a
    Telegram change is effective on the NEXT media operation, with no redeploy
    and no restart). The caller reads the store; this module only receives plain
    values, so no Telegram object, owner id, chat id or message id can reach the
    engine, and there is no second wiring site that could construct an engine.

    The credential and the general media model are deployment configuration and
    keep their existing resolution. ``credential`` — an ``(api_key, label)`` pair in
    the very shape :func:`resolve_api_key` returns — is supplied by the STT engine
    factory when the selected candidate's provider is served by a credential POOL
    (``backend/services/stt_credential_pool.py``), so the pool decides which
    credential this engine carries and this module never reaches for one itself.
    A runtime without a credential stays exactly as fail-closed as before: nothing
    is provisioned and the boundary keeps reporting the missing engine. Never
    raises — a settings change must not be able to break either the panel or
    startup.
    """
    try:
        api_key, key_env_var = credential if credential is not None else resolve_api_key()
        if not api_key:
            return {"configured": False, "reason": "no Gemini credential"}
        model, _model_env_var = resolve_media_model()
        resolved = stt_settings_from(stt_settings)
        engine = GeminiMediaEngine(
            api_key,
            model,
            key_env_var=key_env_var,
            stt_model=resolved["stt_model"],
            stt_language=resolved["stt_language"],
            stt_passes=resolved["stt_passes"],
        )
        media_service.set_stt_engine(engine)
        logger.info(
            "GEMINI_MEDIA_ENGINE_STT_SETTINGS_APPLIED stt_model=%s stt_language=%s "
            "stt_passes=%d",
            resolved["stt_model"] or "general-media-model",
            resolved["stt_language"] or "auto",
            resolved["stt_passes"],
        )
        return {"configured": True, **resolved}
    except Exception as exc:  # noqa: BLE001 — a settings apply is never fatal
        logger.warning(
            "GEMINI_MEDIA_ENGINE_STT_SETTINGS_FAILED error=%s", type(exc).__name__,
        )
        return {"configured": False, "reason": type(exc).__name__}


#: The smallest budget a leg may START with. Below it the operation is reported
#: as an explicit deadline failure instead of being converted into an HTTP timeout
#: too short to mean anything.
_DEADLINE_FLOOR_S = 0.5


def _error(
    message: str,
    failure_class: str,
    *,
    retryable: bool = False,
    http_status: int = 0,
    phase: str = "",
) -> MediaError:
    """Build the boundary's ``MediaError`` with bounded OPERATIONAL metadata.

    ``stage`` stays empty: the media boundary owns stage attribution
    (``media_stt_engine``). The extra attributes are consumed by this module's own
    trace line and by the bounded retry decision — the failure CLASS, whether this
    failure may be repeated within the deadline, the HTTP status when a response
    did arrive, and the socket phase a timeout expired in. None of them can carry
    payload, a credential or a Telegram identifier.
    """
    error = MediaError(message)
    error.failure_class = failure_class
    error.retryable = retryable
    error.http_status = http_status
    error.phase = phase
    return error


def _failure_field(error: BaseException) -> str:
    """The bounded ``failure_class`` trace field of a failed operation.

    ``<class>``, or ``<class>:<phase>`` for a timeout, or
    ``<class>:http=<status>`` for an HTTP rejection — the three facts (which leg,
    which socket phase, which status) that a bare ``timed out`` sentence cannot
    carry.
    """
    name = str(getattr(error, "failure_class", "") or FAILURE_TRANSPORT)
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


def _required_budget(deadline: float, leg: str, deadline_s: float) -> float:
    """The remaining budget, or an explicit, controlled deadline failure.

    A spent deadline is its OWN failure class: it is never silently converted into
    a tiny HTTP timeout, which is exactly what the previous per-phase bound did
    and what made a local bound look like a provider-side timeout.
    """
    left = _budget(deadline)
    if left < _DEADLINE_FLOOR_S:
        raise _error(
            f"Gemini {leg} did not start within the {deadline_s:g}s operation "
            "budget.",
            FAILURE_DEADLINE,
        )
    return left


def _request_timeout(left: float) -> httpx.Timeout:
    """Phase bounds DERIVED from the remaining operation budget.

    Connect/write/pool are bounded tightly (a stalled handshake or a stalled
    request transmission can never eat the operation) and the read is allowed to
    use the remaining budget. One input, one derived object: no phase can outlive
    the operation deadline and the client carries no hidden timeout of its own.
    """
    return httpx.Timeout(
        connect=min(STT_CONNECT_TIMEOUT_S, left),
        read=left,
        write=min(STT_WRITE_TIMEOUT_S, left),
        pool=min(STT_CONNECT_TIMEOUT_S, left),
    )


def _timeout_phase(error: httpx.TimeoutException) -> str:
    """Which socket phase an ``httpx`` timeout expired in (bounded token)."""
    for klass, name in _TIMEOUT_PHASES:
        if isinstance(error, klass):
            return name
    return "request"


def _dedicated_transports(size: int) -> tuple[str, str]:
    """The bounded, ORDERED transport plan for ONE dedicated transcription.

    ``uri`` is primary because the official documentation for the dedicated model
    shows ONLY the Files API URI form. The inline ``data`` form is documented for
    the Interactions API but is not demonstrated for this model, so it is used
    only as the single bounded fallback — and only when the payload fits the
    documented inline budget; a larger payload repeats the URI form once instead,
    which is a plain transient retry and needs no new representation.
    """
    if size <= INLINE_PAYLOAD_MAX_BYTES:
        return (_TRANSPORT_URI, _TRANSPORT_INLINE)
    return (_TRANSPORT_URI, _TRANSPORT_URI)


def _decode_json(response: httpx.Response, leg: str) -> Any:
    """The JSON body of a received response, or a classified malformed failure.

    A response that arrived but cannot be read is a DETERMINISTIC failure: it is
    classified as such and never retried.
    """
    try:
        data = response.json()
    except Exception as exc:  # noqa: BLE001 — malformed response boundary
        raise _error(
            f"Gemini returned an unreadable {leg} response.", FAILURE_MALFORMED,
        ) from exc
    if not isinstance(data, dict):
        raise _error(
            f"Gemini returned an unreadable {leg} response.", FAILURE_MALFORMED,
        )
    return data


def _safe_detail(response: httpx.Response) -> str:
    """A short, sanitized provider detail for a failed request.

    The response body is never logged or surfaced beyond this bounded,
    whitespace-collapsed message, so no request payload can leak. Reads only the
    documented ``error.message`` field.
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
    if not message:
        return ""
    return " ".join(message.split())[:160]


class GeminiMediaEngine:
    """One synchronous engine implementing BOTH media seams over Gemini.

    Deliberately synchronous: ``media_service`` runs engines in a worker thread
    (``asyncio.to_thread``), so the HTTP call here must not touch the event loop,
    and the boundary stays the single owner of the awaited timeout.

    The engine is stateless apart from its credential and model, so provisioning
    one is a deployment decision that cannot alter the media contract.
    """

    __slots__ = (
        "_api_key", "_model", "_key_env_var", "_stt_model", "_stt_language",
        "_stt_passes",
    )

    def __init__(
        self,
        api_key: str,
        model: str = "",
        *,
        key_env_var: str = "",
        stt_model: str = "",
        stt_language: str = "",
        stt_passes: int | None = None,
    ) -> None:
        self._api_key = str(api_key or "").strip()
        self._model = str(model or "").strip() or DEFAULT_MEDIA_MODEL
        self._key_env_var = str(key_env_var or "")
        #: Empty means "the general media model answers the STT instruction".
        self._stt_model = str(stt_model or "").strip()
        #: Empty means automatic language detection; a BCP-47 code pins it.
        self._stt_language = str(stt_language or "").strip()
        #: ``None`` means the caller stated no pass count, so the deployment's own
        #: ``AI_GEMINI_STT_PASSES`` decides; with that unset the value is ``1`` and
        #: the engine keeps the single-pass route EXACTLY as before. Clamped to the
        #: hard ceiling, so neither a caller nor a stale env value can create an
        #: unbounded number of model calls.
        if stt_passes is None:
            stt_passes, _passes_env_var = resolve_stt_passes()
        try:
            passes = int(stt_passes)
        except (TypeError, ValueError):  # pragma: no cover - resolve_stt_passes guards it
            passes = 1
        self._stt_passes = max(1, min(passes, STT_MAX_PASSES))

    @property
    def model(self) -> str:
        """The Gemini model OCR and the general media route use (no secret)."""
        return self._model

    @property
    def stt_model(self) -> str:
        """The dedicated transcription model, or ``""`` when not configured."""
        return self._stt_model

    @property
    def stt_language(self) -> str:
        """The configured BCP-47 language code, or ``""`` for automatic."""
        return self._stt_language

    @property
    def stt_passes(self) -> int:
        """The configured number of recognition passes (``1`` = single pass)."""
        return self._stt_passes

    @property
    def stt_transport(self) -> str:
        """The API surface ``transcribe`` uses: ``interactions`` or
        ``generate_content``. Deterministic from configuration alone."""
        return "interactions" if self._stt_model else "generate_content"

    @property
    def key_env_var(self) -> str:
        """The ENV variable the credential came from — the NAME only, never a value."""
        return self._key_env_var

    # ── The two seam methods ──

    def recognize(self, image: bytes) -> str:
        """Return the text visible in ``image`` (``""`` when there is none).

        Always the general media model: the dedicated speech model is never used
        for OCR, whatever STT is configured with.
        """
        return self._run(_OCR_KIND, image, OCR_INSTRUCTION, OCR_TIMEOUT_S)

    def transcribe(self, audio: bytes) -> str:
        """Return the transcript of ``audio`` (``""`` when there is no speech).

        Two deterministic, mutually exclusive routes chosen ONLY by
        configuration:

          * a dedicated transcription model is configured ⇒ its own documented
            transcription request (Interactions API);
          * otherwise the general media model answers the STT instruction,
            exactly as before.

        The ROUTE never falls back to the other model (a failing dedicated request
        is never re-asked on the general model — that would make the recognition
        comparison uninterpretable). Within one route, the bounded retry/
        fallback documented at :data:`STT_MAX_ATTEMPTS` may instead repeat the
        same request or switch the audio REPRESENTATION.

        With :data:`STT_PASSES_ENV_VAR` configured above one, the SAME audio is
        recognised that many times instead (:meth:`_run_consensus`) and the
        hypotheses are reconciled by the STT-only consensus — the accuracy seam,
        which is off unless it is explicitly asked for.
        """
        if self._stt_passes > 1:
            return self._run_consensus(audio)
        if self._stt_model:
            return self._run_dedicated_transcription(audio)
        return self._run(
            _STT_KIND, audio, stt_instruction(self._stt_language),
            STT_OPERATION_DEADLINE_S,
        )

    # ── Internals ──

    def _log_run(
        self,
        kind: str,
        mime_type: str,
        byte_count: int,
        started: float,
        model: str,
        language_mode: str,
        transcription_mode: str,
        *,
        chars: int = 0,
        failed: bool = False,
        deadline_s: float = 0.0,
        attempts: int = 1,
        failure_class: str = "",
    ) -> None:
        """ONE bounded, non-sensitive line per media operation.

        It carries exactly what the recognition experiment and a live incident
        need (engine, model, API surface, container MIME, payload size, language
        mode, transcription mode, elapsed time, output LENGTH, success/failure,
        the operation deadline, how many provider attempts were spent, the
        configured recognition PASS count (so a multi-pass deployment is visible
        on every line) and the failure CLASS with its socket phase or HTTP status)
        and never the
        transcript, the raw audio, the credential, a Telegram id, a filename or a
        caption.
        """
        logger.info(
            "GEMINI_MEDIA_ENGINE kind=%s engine=%s model=%s transport=%s mime=%s "
            "bytes=%d language=%s mode=%s chars=%d elapsed_ms=%d status=%s "
            "attempts=%d deadline_s=%g failure_class=%s stt_passes=%d",
            kind, type(self).__name__, model,
            self.stt_transport if kind == _STT_KIND else "generate_content",
            mime_type, byte_count,
            language_mode or ("auto" if kind == _STT_KIND else "-"),
            transcription_mode or "-", chars,
            int((time.monotonic() - started) * 1000), "failed" if failed else "ok",
            attempts, deadline_s, failure_class or "-", self._stt_passes,
        )

    def _trace_stage(
        self,
        stage: str,
        kind: str = _STT_KIND,
        *,
        attempt: int = 1,
        transport: str = "",
        byte_count: int = 0,
        elapsed_ms: int = -1,
    ) -> None:
        """ONE bounded operational line per leg of a media operation.

        This closes the observability gap that made a live incident undecidable:
        the engine used to emit only its completed/failed aggregate, so a request
        still in flight produced no line at all. The fields are the leg, the
        attempt number, the bounded payload size and the leg's own elapsed time —
        never audio, a transcript, a credential or a Telegram identifier.
        """
        logger.info(
            "GEMINI_MEDIA_ENGINE_STAGE kind=%s stage=%s attempt=%d transport=%s "
            "bytes=%d elapsed_ms=%s",
            kind, stage, attempt, transport or "-", byte_count,
            str(elapsed_ms) if elapsed_ms >= 0 else "-",
        )

    def _run(self, kind: str, payload: bytes, instruction: str, deadline_s: float) -> str:
        """The Generate Content route: OCR and the general-model STT request.

        ONE operation deadline (``deadline_s``): every request timeout is derived
        from what is left of it, so no socket phase can outlive the operation. OCR
        keeps its single-attempt contract; the STT route may spend ONE bounded
        second attempt on a transient transport failure — never on a deterministic
        one, and never without budget.
        """
        if not payload:
            return ""
        mime_type = gemini_mime_type(payload)
        is_stt = kind == _STT_KIND
        language_mode = self._stt_language if is_stt else ""
        transcription_mode = "instruction" if is_stt else ""
        started = time.monotonic()
        deadline = started + deadline_s
        inline = len(payload) <= INLINE_PAYLOAD_MAX_BYTES
        self._trace_stage(
            "request_start", kind,
            transport=_TRANSPORT_INLINE if inline else _TRANSPORT_URI,
            byte_count=len(payload),
        )
        max_attempts = STT_MAX_ATTEMPTS if is_stt else 1
        attempts = 0
        failure: MediaError | None = None
        while attempts < max_attempts:
            if attempts and _budget(deadline) < STT_MIN_ATTEMPT_S:
                break
            attempts += 1
            if attempts > 1:
                self._trace_stage(
                    "attempt", kind, attempt=attempts, byte_count=len(payload),
                    transport=_TRANSPORT_INLINE if inline else _TRANSPORT_URI,
                )
            try:
                if inline:
                    part: dict[str, Any] = {
                        "inlineData": {
                            "mimeType": mime_type,
                            "data": base64.b64encode(payload).decode("ascii"),
                        }
                    }
                    text = self._generate(kind, part, instruction, deadline, deadline_s, attempts)
                else:
                    text = self._generate_from_upload(
                        kind, payload, mime_type, instruction, deadline, deadline_s, attempts,
                    )
            except MediaError as exc:
                failure = exc
                if not getattr(exc, "retryable", False):
                    break
                continue
            self._log_run(
                kind, mime_type, len(payload), started, self._model,
                language_mode, transcription_mode, chars=len(text),
                deadline_s=deadline_s, attempts=attempts,
            )
            return text
        error = failure or _error(
            f"Gemini {kind} produced no result.", FAILURE_TRANSPORT,
        )
        self._log_run(
            kind, mime_type, len(payload), started, self._model,
            language_mode, transcription_mode, failed=True,
            deadline_s=deadline_s, attempts=max(attempts, 1),
            failure_class=_failure_field(error),
        )
        raise error

    # ── The bounded multi-pass route (the opt-in accuracy seam) ──

    def _run_consensus(self, audio: bytes) -> str:
        """N SEQUENTIAL recognition passes of the configured route, ONE deadline.

        Bounded in every dimension: the pass count is the configured ceiling, only
        ONE request is ever in flight, the audio is uploaded ONCE and reused by
        every pass, and the whole operation runs inside
        :data:`STT_OPERATION_DEADLINE_S` — a pass starts only with meaningful
        budget left, and a spent deadline ends the loop with the hypotheses
        already in hand.

        A pass that failed contributes NO hypothesis (a transport problem can
        therefore never be mistaken for a recognition result) and is never
        retried: the configured passes ARE the transient-recovery budget here,
        which is why this route deliberately spends no per-pass transport retry.
        A DETERMINISTIC failure ends the loop at once — the same request is never
        re-sent — and when no pass produced a transcript at all the operation
        fails closed with the first, most informative failure.
        """
        if not audio:
            return ""
        mime_type = gemini_mime_type(audio)
        dedicated = bool(self._stt_model)
        if dedicated and mime_type not in _TRANSCRIPTION_AUDIO_MIME_TYPES:
            raise MediaError(
                "The dedicated transcription model accepts audio input only."
            )
        instruction = "" if dedicated else stt_instruction(self._stt_language)
        transcription_mode = _VERBATIM_MODE["type"] if dedicated else "instruction"
        started = time.monotonic()
        deadline = started + STT_OPERATION_DEADLINE_S
        transport = _TRANSPORT_URI
        uploaded_name = ""
        hypotheses: list[str] = []
        failure: MediaError | None = None
        passes = 0
        try:
            uploaded_name, transport, item = self._prepare_consensus_input(
                audio, mime_type, dedicated, deadline,
            )
            self._trace_stage(
                "consensus_start", transport=transport, byte_count=len(audio),
            )
            for index in range(1, self._stt_passes + 1):
                if index > 1 and _budget(deadline) < STT_MIN_ATTEMPT_S:
                    break
                pass_started = time.monotonic()
                try:
                    if dedicated:
                        text = self._transcribe_interaction(item, deadline, attempt=index)
                    else:
                        text = self._generate(
                            _STT_KIND, item, instruction, deadline,
                            _required_budget(
                                deadline, "speech-to-text request",
                                STT_OPERATION_DEADLINE_S,
                            ),
                            index,
                        )
                except MediaError as exc:
                    if failure is None:
                        failure = exc
                    passes += 1
                    self._trace_stage(
                        "pass_failed", attempt=index, transport=transport,
                        byte_count=len(audio),
                        elapsed_ms=int((time.monotonic() - pass_started) * 1000),
                    )
                    if not getattr(exc, "retryable", False):
                        break
                    continue
                passes += 1
                hypotheses.append(text)
                self._trace_stage(
                    "pass", attempt=index, transport=transport,
                    byte_count=len(audio),
                    elapsed_ms=int((time.monotonic() - pass_started) * 1000),
                )
        finally:
            if uploaded_name:
                self._delete_file(uploaded_name)

        if not hypotheses:
            error = failure or _error(
                "Gemini speech-to-text produced no result.", FAILURE_INTERACTION,
            )
            self._log_run(
                _STT_KIND, mime_type, len(audio), started,
                self._stt_model or self._model, self._stt_language,
                transcription_mode, failed=True,
                deadline_s=STT_OPERATION_DEADLINE_S, attempts=max(passes, 1),
                failure_class=_failure_field(error),
            )
            raise error

        # The reconciler sees the hypotheses and NOTHING else: no chat id, no
        # sender, no filename, no caption, no reply and no history exist in its
        # signature, so no Telegram or conversational context can reach it.
        result = reconcile_hypotheses(hypotheses)
        logger.info(
            "GEMINI_MEDIA_ENGINE_CONSENSUS model=%s transport=%s mime=%s bytes=%d "
            "passes=%d hypotheses=%d positions=%d changed=%d dropped=%d elapsed_ms=%d",
            self._stt_model or self._model, transport, mime_type, len(audio),
            passes, result.hypotheses, result.positions, result.changed,
            result.dropped, int((time.monotonic() - started) * 1000),
        )
        self._log_run(
            _STT_KIND, mime_type, len(audio), started,
            self._stt_model or self._model, self._stt_language,
            transcription_mode, chars=len(result.text),
            deadline_s=STT_OPERATION_DEADLINE_S, attempts=max(passes, 1),
        )
        return result.text

    def _prepare_consensus_input(
        self, audio: bytes, mime_type: str, dedicated: bool, deadline: float,
    ) -> tuple[str, str, dict[str, Any]]:
        """``(uploaded_name, transport, input)`` — the representation, prepared ONCE.

        Each route keeps the representation it already documents, and either way
        exactly ONE upload happens per operation, so the extra passes cost a model
        call each instead of a whole fresh upload each:

        * the general route sends a payload inside the inline budget INLINE (no
          upload is needed at all) and uploads once above it — the M1.5c rule,
          unchanged;
        * the dedicated route uploads once (the ``uri`` form its documentation
          shows) and falls back to the inline form only when that upload fails
          TRANSIENTLY with the payload inside the documented inline budget; a
          deterministic failure fails closed.
        """
        if not dedicated and len(audio) <= INLINE_PAYLOAD_MAX_BYTES:
            return "", _TRANSPORT_INLINE, self._inline_item(audio, mime_type, dedicated)
        try:
            upload_url = self._start_upload(audio, mime_type, deadline, attempt=1)
            file_info = self._finish_upload(
                upload_url, audio, mime_type, deadline, attempt=1,
            )
        except MediaError as exc:
            if len(audio) <= INLINE_PAYLOAD_MAX_BYTES and getattr(exc, "retryable", False):
                return "", _TRANSPORT_INLINE, self._inline_item(audio, mime_type, dedicated)
            raise
        name = str(file_info.get("name") or "")
        uri = str(file_info.get("uri") or "")
        if not name or not uri:
            raise _error(
                "Gemini did not accept the uploaded audio file.", FAILURE_UPLOAD,
            )
        state = str(file_info.get("state") or "").upper()
        if state and state != "ACTIVE":
            uri = self._await_file_ready(name, uri, deadline)
        return name, _TRANSPORT_URI, self._uri_item(uri, mime_type, dedicated)

    @staticmethod
    def _inline_item(audio: bytes, mime_type: str, dedicated: bool) -> dict[str, Any]:
        """The documented INLINE audio representation of the selected route."""
        data = base64.b64encode(audio).decode("ascii")
        if dedicated:
            return {"type": "audio", "data": data, "mime_type": mime_type}
        return {"inlineData": {"mimeType": mime_type, "data": data}}

    @staticmethod
    def _uri_item(uri: str, mime_type: str, dedicated: bool) -> dict[str, Any]:
        """The documented UPLOADED-file representation of the selected route."""
        if dedicated:
            return {"type": "audio", "uri": uri, "mime_type": mime_type}
        return {"fileData": {"mimeType": mime_type, "fileUri": uri}}

    # ── The dedicated transcription route ──

    def _run_dedicated_transcription(self, audio: bytes) -> str:
        """ONE dedicated transcription, bounded by ONE operation deadline.

        The representation is the Files API URI form the official documentation
        shows for this model. A TRANSIENT failure of the first attempt may use the
        ONE bounded alternative — the documented inline audio representation when
        the payload fits the inline budget (it needs no upload at all), otherwise
        the URI form once more. At most :data:`STT_MAX_ATTEMPTS` sequential
        attempts, decided BEFORE the operation and never at runtime: no retry
        loop, no concurrency, no second model, and never any attempt without
        budget left.
        """
        if not audio:
            return ""
        mime_type = gemini_mime_type(audio)
        if mime_type not in _TRANSCRIPTION_AUDIO_MIME_TYPES:
            raise MediaError(
                "The dedicated transcription model accepts audio input only."
            )
        started = time.monotonic()
        deadline = started + STT_OPERATION_DEADLINE_S
        transports = _dedicated_transports(len(audio))
        self._trace_stage(
            "request_start", transport=transports[0], byte_count=len(audio),
        )
        attempts = 0
        failure: MediaError | None = None
        while attempts < STT_MAX_ATTEMPTS:
            # A second attempt runs only with a meaningful budget left, so the
            # bounded fallback can never push the operation past its deadline.
            if attempts and _budget(deadline) < STT_MIN_ATTEMPT_S:
                break
            transport = transports[attempts]
            attempts += 1
            if attempts > 1:
                self._trace_stage(
                    "attempt", transport=transport, attempt=attempts,
                    byte_count=len(audio),
                )
            try:
                text = self._dedicated_attempt(
                    transport, audio, mime_type, deadline, attempts,
                )
            except MediaError as exc:
                failure = exc
                if not getattr(exc, "retryable", False):
                    break
                continue
            self._log_run(
                _STT_KIND, mime_type, len(audio), started, self._stt_model,
                self._stt_language, _VERBATIM_MODE["type"], chars=len(text),
                deadline_s=STT_OPERATION_DEADLINE_S, attempts=attempts,
            )
            return text
        error = failure or _error(
            "Gemini speech-to-text produced no result.", FAILURE_INTERACTION,
        )
        self._log_run(
            _STT_KIND, mime_type, len(audio), started, self._stt_model,
            self._stt_language, _VERBATIM_MODE["type"], failed=True,
            deadline_s=STT_OPERATION_DEADLINE_S, attempts=max(attempts, 1),
            failure_class=_failure_field(error),
        )
        raise error

    def _dedicated_attempt(
        self, transport: str, audio: bytes, mime_type: str, deadline: float, attempt: int,
    ) -> str:
        """ONE attempt of the bounded transport plan (never a retry loop)."""
        if transport == _TRANSPORT_INLINE:
            item: dict[str, Any] = {
                "type": "audio",
                "data": base64.b64encode(audio).decode("ascii"),
                "mime_type": mime_type,
            }
            return self._transcribe_interaction(item, deadline, attempt=attempt)
        return self._transcribe_interaction_from_upload(
            audio, mime_type, deadline, attempt=attempt,
        )

    def _transcription_body(self, audio_item: dict[str, Any]) -> dict[str, Any]:
        """The documented request body for the dedicated transcription model.

        Field inventory, and nothing beyond it:
          * ``model`` — the selected dedicated model;
          * ``input`` — ONE audio item plus its documented ``mime_type``. The
            PRIMARY attempt uses the uploaded ``uri`` form (the representation the
            documentation shows for this model); the inline ``data`` (base64)
            form is only ever the bounded fallback;
          * ``generation_config.transcription_config.language_codes`` — a
            single BCP-47 code, included ONLY when a language is configured, so
            the automatic-detection mode sends no language field at all;
          * ``generation_config.transcription_config.mode`` — the verbatim mode
            object, sent explicitly;
          * ``store: false`` — the documented opt-out from the API's default
            server-side retention of the interaction, so a voice note is not
            kept remotely for a day (it is compatible with every feature used
            here; it is incompatible only with background execution, which this
            engine never requests).

        No text instruction is sent: the dedicated model is documented to accept
        the audio alone. No ``temperature``/``topK``/``topP``/``candidateCount``/
        ``maxOutputTokens`` and no ``system_instruction`` are sent (they are not
        part of this request's documented transcription contract, and sampling
        controls are not a reliable ASR-fidelity lever).
        """
        transcription_config: dict[str, Any] = {"mode": dict(_VERBATIM_MODE)}
        if self._stt_language:
            transcription_config["language_codes"] = [self._stt_language]
        return {
            "model": self._stt_model,
            "input": [audio_item],
            "generation_config": {"transcription_config": transcription_config},
            "store": False,
        }

    def _transcribe_interaction(
        self, audio_item: dict[str, Any], deadline: float, *, attempt: int = 1,
    ) -> str:
        """ONE ``POST /interactions`` for ONE attempt (never a retry loop)."""
        body = self._transcription_body(audio_item)
        response = self._perform(
            deadline,
            "interaction",
            lambda client: client.post(
                INTERACTIONS_ENDPOINT, json=body, headers=self._headers(),
            ),
            budget_leg="speech-to-text request",
            message_leg="speech-to-text request",
            timeout_class=FAILURE_INTERACTION_TIMEOUT,
            failure_class=FAILURE_INTERACTION,
            deadline_s=STT_OPERATION_DEADLINE_S,
            attempt=attempt,
        )
        _raise_for_status("speech-to-text", response, self._api_key)
        return _extract_interaction_text(
            _STT_KIND, _decode_json(response, "speech-to-text"), self._api_key,
        )

    def _transcribe_interaction_from_upload(
        self, audio: bytes, mime_type: str, deadline: float, *, attempt: int = 1,
    ) -> str:
        """The documented Files API flow, reused for the dedicated route.

        The uploaded file is referenced by its returned URI and deleted in a
        ``finally`` block, so nothing outlives the ATTEMPT — including a failed
        one, before the bounded fallback can run.
        """
        upload_url = self._start_upload(audio, mime_type, deadline, attempt=attempt)
        file_info = self._finish_upload(
            upload_url, audio, mime_type, deadline, attempt=attempt,
        )
        name = str(file_info.get("name") or "")
        uri = str(file_info.get("uri") or "")
        if not name or not uri:
            raise _error(
                "Gemini did not accept the uploaded audio file.", FAILURE_UPLOAD,
            )
        try:
            state = str(file_info.get("state") or "").upper()
            if state and state != "ACTIVE":
                uri = self._await_file_ready(name, uri, deadline)
            return self._transcribe_interaction(
                {"type": "audio", "uri": uri, "mime_type": mime_type},
                deadline, attempt=attempt,
            )
        finally:
            self._delete_file(name)

    def _headers(self) -> dict[str, str]:
        return {"x-goog-api-key": self._api_key, "Content-Type": "application/json"}

    def _generate(
        self, kind: str, part: dict[str, Any], instruction: str, deadline: float,
        deadline_s: float, attempt: int = 1,
    ) -> str:
        """ONE generateContent request per attempt (never a retry loop)."""
        url = f"{GEMINI_API_BASE}/models/{self._model}:generateContent"
        body = {
            "contents": [{"role": "user", "parts": [{"text": instruction}, part]}],
            "generationConfig": {
                "temperature": SAMPLING_TEMPERATURE,
                "maxOutputTokens": MAX_OUTPUT_TOKENS,
            },
        }
        response = self._perform(
            deadline,
            "generate_content",
            lambda client: client.post(url, json=body, headers=self._headers()),
            budget_leg=f"{kind} request",
            message_leg=f"{kind} request",
            timeout_class=FAILURE_REQUEST_TIMEOUT,
            failure_class=FAILURE_REQUEST,
            deadline_s=deadline_s,
            attempt=attempt,
            kind=kind,
        )
        _raise_for_status(kind, response, self._api_key)
        return _extract_text(kind, _decode_json(response, kind), self._api_key)

    def _generate_from_upload(
        self, kind: str, payload: bytes, mime_type: str, instruction: str,
        deadline: float, deadline_s: float, attempt: int = 1,
    ) -> str:
        """The documented Files API path for audio past the inline budget.

        Minimum interaction only: start the resumable upload, send the bytes in
        one finalize request, use the returned file URI, and DELETE the file in a
        ``finally`` block. A failed delete is logged (never raised) because a
        file the API fails to delete is still auto-expired by the API itself.
        """
        upload_url = self._start_upload(payload, mime_type, deadline, attempt=attempt)
        file_info = self._finish_upload(
            upload_url, payload, mime_type, deadline, attempt=attempt,
        )
        name = str(file_info.get("name") or "")
        uri = str(file_info.get("uri") or "")
        if not name or not uri:
            raise _error(
                "Gemini did not accept the uploaded audio file.", FAILURE_UPLOAD,
            )
        try:
            state = str(file_info.get("state") or "").upper()
            if state and state != "ACTIVE":
                uri = self._await_file_ready(name, uri, deadline)
            return self._generate(
                kind,
                {"fileData": {"mimeType": mime_type, "fileUri": uri}},
                instruction,
                deadline,
                deadline_s,
                attempt,
            )
        finally:
            self._delete_file(name)

    def _perform(
        self,
        deadline: float,
        stage: str,
        call: Any,
        *,
        budget_leg: str,
        message_leg: str,
        timeout_class: str,
        failure_class: str,
        deadline_s: float,
        attempt: int = 1,
        byte_count: int = 0,
        kind: str = _STT_KIND,
    ) -> httpx.Response:
        """ONE bounded HTTP call, inside the operation deadline.

        The request timeout is DERIVED from what is left of the deadline (tight
        connect/write/pool bounds, the remaining budget for the read), so no socket
        phase can outlive the operation. Every failure is CLASSIFIED: an explicit
        deadline failure when the budget is spent, a timeout carrying the expired
        socket PHASE, an HTTP rejection carrying its status, or a named transport
        failure — with ``retryable`` set ONLY for the conditions a bounded second
        attempt may repeat.
        """
        left = _required_budget(deadline, budget_leg, deadline_s)
        timeout = _request_timeout(left)
        call_started = time.monotonic()
        try:
            with self._client(timeout) as client:
                response = call(client)
        except httpx.TimeoutException as exc:
            phase = _timeout_phase(exc)
            raise _error(
                f"Gemini {message_leg} timed out after {left:g}s ({phase} phase).",
                timeout_class, retryable=True, phase=phase,
            ) from exc
        except Exception as exc:  # noqa: BLE001 — the transport boundary
            raise _error(
                f"Gemini {message_leg} failed ({type(exc).__name__}).",
                failure_class,
                retryable=isinstance(exc, _TRANSIENT_TRANSPORT_ERRORS),
            ) from exc
        self._trace_stage(
            stage, kind, attempt=attempt, byte_count=byte_count,
            elapsed_ms=int((time.monotonic() - call_started) * 1000),
        )
        return response

    def _start_upload(
        self, payload: bytes, mime_type: str, deadline: float, *, attempt: int = 1,
    ) -> str:
        url = f"{GEMINI_UPLOAD_BASE}/files"
        headers = {
            **self._headers(),
            "X-Goog-Upload-Protocol": "resumable",
            "X-Goog-Upload-Command": "start",
            "X-Goog-Upload-Header-Content-Length": str(len(payload)),
            "X-Goog-Upload-Header-Content-Type": mime_type,
        }
        body = {"file": {"display_name": _UPLOAD_DISPLAY_NAME}}
        response = self._perform(
            deadline,
            "upload_start",
            lambda client: client.post(url, json=body, headers=headers),
            budget_leg="speech-to-text upload",
            message_leg="speech-to-text upload",
            timeout_class=FAILURE_UPLOAD_TIMEOUT,
            failure_class=FAILURE_UPLOAD,
            deadline_s=STT_OPERATION_DEADLINE_S,
            attempt=attempt,
            byte_count=len(payload),
        )
        _raise_for_status("speech-to-text upload", response, self._api_key)
        upload_url = str(response.headers.get("x-goog-upload-url") or "").strip()
        if not upload_url:
            raise _error("Gemini did not return an upload URL.", FAILURE_UPLOAD)
        return upload_url

    def _finish_upload(
        self, upload_url: str, payload: bytes, mime_type: str, deadline: float,
        *, attempt: int = 1,
    ) -> dict[str, Any]:
        # No credential is sent to the upload URL: it comes from the API and only
        # ever receives the bytes plus the protocol headers.
        headers = {
            "Content-Length": str(len(payload)),
            "Content-Type": mime_type,
            "X-Goog-Upload-Offset": "0",
            "X-Goog-Upload-Command": "upload, finalize",
        }
        response = self._perform(
            deadline,
            "upload_finalize",
            lambda client: client.post(upload_url, content=payload, headers=headers),
            budget_leg="speech-to-text upload",
            message_leg="speech-to-text upload",
            timeout_class=FAILURE_UPLOAD_TIMEOUT,
            failure_class=FAILURE_UPLOAD,
            deadline_s=STT_OPERATION_DEADLINE_S,
            attempt=attempt,
            byte_count=len(payload),
        )
        _raise_for_status("speech-to-text upload", response, self._api_key)
        data = _decode_json(response, "upload")
        file_info = data.get("file")
        return file_info if isinstance(file_info, dict) else data

    def _await_file_ready(self, name: str, uri: str, deadline: float) -> str:
        """Bounded readiness checks; a file that never becomes ready fails closed.

        The FIRST check runs immediately — the finalize response usually already
        reports ``ACTIVE``, and a gratuitous sleep would only add latency — and the
        wait is bounded by BOTH an attempt count and the operation deadline. No
        polling loop exists beyond this.
        """
        for attempt in range(1, _FILE_READY_ATTEMPTS + 1):
            if attempt > 1:
                time.sleep(_FILE_READY_DELAY_S)
            info = self._file_info(name, deadline, attempt=attempt)
            state = str(info.get("state") or "").upper()
            if state == "ACTIVE":
                self._trace_stage("file_ready", attempt=attempt)
                return str(info.get("uri") or uri)
            if state == "FAILED":
                raise _error(
                    "Gemini failed to process the uploaded audio file.",
                    FAILURE_FILE_PROCESSING,
                )
        raise _error(
            "Gemini did not finish processing the uploaded audio file in time.",
            FAILURE_FILE_PROCESSING,
        )

    def _file_info(self, name: str, deadline: float, *, attempt: int = 1) -> dict[str, Any]:
        url = f"{GEMINI_API_BASE}/{name}"
        response = self._perform(
            deadline,
            "file_status",
            lambda client: client.get(url, headers=self._headers()),
            budget_leg="uploaded-file status check",
            message_leg="speech-to-text upload status check",
            timeout_class=FAILURE_UPLOAD_TIMEOUT,
            failure_class=FAILURE_UPLOAD,
            deadline_s=STT_OPERATION_DEADLINE_S,
            attempt=attempt,
        )
        _raise_for_status("speech-to-text upload", response, self._api_key)
        try:
            data = response.json()
        except Exception:  # noqa: BLE001 — a state we cannot read is a state we refuse
            return {}
        return data if isinstance(data, dict) else {}

    def _delete_file(self, name: str) -> None:
        """Best-effort cleanup of the remote upload, bounded on its own. Never raises.

        The DELETE is deliberately NOT part of the operation deadline: cleanup must
        never delay or outlive the owner-visible outcome, which is why it gets its
        own small finite bound instead of the previous full per-phase timeout (that
        one could push a failure past the boundary's outer bound). The API
        auto-expires uploaded files, so a failed delete is only logged.
        """
        try:
            with self._client(_request_timeout(STT_CLEANUP_TIMEOUT_S)) as client:
                client.delete(f"{GEMINI_API_BASE}/{name}", headers=self._headers())
        except Exception as exc:  # noqa: BLE001 — cleanup must not mask the result
            logger.warning("GEMINI_MEDIA_ENGINE_UPLOAD_CLEANUP_FAILED error=%s",
                           type(exc).__name__)

    def _client(self, timeout: httpx.Timeout) -> httpx.Client:
        """A per-call client carrying the DERIVED phase bounds.

        Deliberately not a shared/pooled client: the engine is called from the
        boundary's ``asyncio.to_thread`` workers, so module-level client state
        would be cross-request mutable state (stale connections after a failure,
        an unbounded pool, no deterministic shutdown) — the engine's ``__slots__``
        contract is exactly that it holds none. What used to be a hidden timeout
        (``timeout=40.0``, i.e. four independent per-phase bounds) is now an object
        derived from the remaining operation budget, so the client can never carry
        a bound the operation does not know about.
        """
        return httpx.Client(timeout=timeout)


def _raise_for_status(kind: str, response: httpx.Response, api_key: str) -> None:
    """Map a non-2xx Gemini response onto the boundary's controlled error contract.

    The credential is redacted from any provider detail, so a redacted key can
    never reach a log line or the owner's failure message. Every status is
    CLASSIFIED and carries its own HTTP status, and only the two genuinely
    transient statuses (429, >= 500) are marked retryable — a rejection, a
    missing model or a refused request is DETERMINISTIC and is never re-sent.
    """
    status = response.status_code
    if status < 400:
        return
    detail = _safe_detail(response)
    if api_key:
        detail = detail.replace(api_key, "***")
    suffix = f" ({detail})" if detail else ""
    if status in (401, 403):
        raise _error(
            f"Gemini rejected the configured API key (HTTP {status}).{suffix}",
            FAILURE_HTTP, http_status=status,
        )
    if status == 429:
        raise _error(
            "Gemini rate limited the request (HTTP 429).",
            FAILURE_HTTP, retryable=True, http_status=status,
        )
    if status == 404:
        raise _error(
            f"Gemini could not find the configured model for {kind} (HTTP 404).{suffix}",
            FAILURE_HTTP, http_status=status,
        )
    if status >= 500:
        raise _error(
            f"Gemini is unavailable right now (HTTP {status}).",
            FAILURE_HTTP, retryable=True, http_status=status,
        )
    raise _error(
        f"Gemini refused the {kind} request (HTTP {status}).{suffix}",
        FAILURE_HTTP, http_status=status,
    )


def _extract_interaction_text(kind: str, data: Any, api_key: str) -> str:
    """The model text of ONE completed transcription interaction.

    Reads the documented Interactions response shape: the transcript is the
    ``text`` of every ``type: "text"`` content item inside the ``model_output``
    steps, joined in reading order (the same join rule as
    :func:`_extract_text`). ``output_text`` — the documented accessor name — is
    accepted only as a fallback when no such item exists, so one response shape
    is not silently assumed. A non-completed status, an unreadable body or a
    malformed shape raises ``MediaError``; genuinely absent speech returns the
    seam's honest empty string, never a fabricated transcript.
    """
    if not isinstance(data, dict):
        raise _error(f"Gemini returned an unreadable {kind} response.", FAILURE_MALFORMED)
    status = str(data.get("status") or "").strip().lower()
    if status and status != "completed":
        raise _error(
            f"Gemini did not complete the {kind} request ({status}).",
            FAILURE_MALFORMED,
        )
    chunks: list[str] = []
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
                if str(item.get("type") or "") != "text":
                    continue
                text = item.get("text")
                if isinstance(text, str) and text:
                    chunks.append(text)
    if not chunks:
        fallback = data.get("output_text")
        if isinstance(fallback, str) and fallback.strip():
            chunks.append(fallback)
    if not chunks:
        return ""
    return "\n".join(chunks)


def _extract_text(kind: str, data: Any, api_key: str) -> str:
    """The model text of ONE generateContent response, or the seam's empty result.

    A response with no usable text is NOT fabricated: an honest empty string is
    returned (the boundary turns it into its existing no-content outcome), while a
    malformed, blocked or refused response raises ``MediaError``.
    """
    if not isinstance(data, dict):
        raise _error(f"Gemini returned an unreadable {kind} response.", FAILURE_MALFORMED)
    feedback = data.get("promptFeedback") if isinstance(data.get("promptFeedback"), dict) else {}
    blocked = str(feedback.get("blockReason") or "").strip()
    candidates = data.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        if blocked:
            raise _error(f"Gemini blocked the {kind} request ({blocked}).", FAILURE_MALFORMED)
        raise _error(f"Gemini returned no {kind} result.", FAILURE_MALFORMED)
    candidate = candidates[0] if isinstance(candidates[0], dict) else {}
    content = candidate.get("content") if isinstance(candidate.get("content"), dict) else {}
    parts = content.get("parts") if isinstance(content.get("parts"), list) else []
    chunks: list[str] = []
    for part in parts:
        if isinstance(part, dict) and isinstance(part.get("text"), str) and part["text"]:
            chunks.append(part["text"])
    finish_reason = str(candidate.get("finishReason") or "").upper()
    if chunks and finish_reason == "MAX_TOKENS":
        # The response hit the explicit output ceiling: the text is real but may be
        # incomplete, so it is reported (with a trace) rather than silently trusted.
        # The boundary still applies the shared character ceiling and its own
        # ``truncated`` flag to whatever is returned.
        logger.warning(
            "GEMINI_MEDIA_ENGINE_OUTPUT_TRUNCATED kind=%s finish_reason=%s",
            kind, finish_reason,
        )
    if not chunks:
        if finish_reason in {"SAFETY", "RECITATION", "PROHIBITED_CONTENT", "BLOCKLIST"}:
            raise _error(
                f"Gemini refused to return {kind} content ({finish_reason}).",
                FAILURE_MALFORMED,
            )
        # Genuinely empty output: no readable text / no speech. Reported honestly.
        return ""
    return "\n".join(chunks)


# ── Configuration (existing project conventions only) ──


def resolve_api_key() -> tuple[str, str]:
    """``(api_key, env_var_name)`` from the variables the repository already uses."""
    for name in API_KEY_ENV_VARS:
        value = (os.getenv(name) or "").strip()
        if value:
            return value, name
    return "", ""


def resolve_stt_model() -> tuple[str, str]:
    """``(model, env_var_name)`` for STT; ``("", "")`` when unset.

    No default and no substitution: an unset variable means the EXISTING
    general media route (the general model answering ``STT_INSTRUCTION``), which
    is exactly why activating the dedicated model is an explicit act. The value
    passes through the project's existing deprecation map.
    """
    value = (os.getenv(STT_MODEL_ENV_VAR) or "").strip()
    if not value:
        return "", ""
    return resolve_model("gemini", value), STT_MODEL_ENV_VAR


def resolve_stt_language() -> tuple[str, str]:
    """``(bcp47_code, env_var_name)`` for STT; ``("", "")`` means automatic.

    The code is passed through as configured (trimmed) — no reinterpretation and
    no invented validation. An unset/blank value keeps the multilingual,
    automatic-detection behaviour, so this can never make every voice note
    Persian.
    """
    value = (os.getenv(STT_LANGUAGE_ENV_VAR) or "").strip()
    if not value:
        return "", ""
    return value, STT_LANGUAGE_ENV_VAR


def resolve_media_model() -> tuple[str, str]:
    """``(model, env_var_name)``; the media override wins, then the chat model.

    Falls back to :data:`DEFAULT_MEDIA_MODEL`. The resolved value passes through
    the project's existing deprecation map (``resolve_model``), so a retired model
    configured in ENV can never poison the request.
    """
    for name in (MEDIA_MODEL_ENV_VAR, CHAT_MODEL_ENV_VAR):
        value = (os.getenv(name) or "").strip()
        if value:
            return resolve_model("gemini", value), name
    return DEFAULT_MEDIA_MODEL, ""


def build_gemini_media_engine() -> tuple[GeminiMediaEngine | None, str, str]:
    """``(engine, model, reason)`` — ``engine`` is ``None`` when unconfigured.

    A missing credential yields ``None`` and an explicit reason instead of an
    exception: the media boundary's engine contract is designed around OPTIONAL
    provisioning, so an unconfigured runtime must stay fail-closed and must not
    fail startup.
    """
    api_key, key_env_var = resolve_api_key()
    if not api_key:
        return None, "", (
            "Gemini media engines are not configured: set "
            + " or ".join(API_KEY_ENV_VARS) + "."
        )
    model, _model_env_var = resolve_media_model()
    stt_model, _stt_env_var = resolve_stt_model()
    stt_language, _language_env_var = resolve_stt_language()
    return (
        GeminiMediaEngine(
            api_key,
            model,
            key_env_var=key_env_var,
            stt_model=stt_model,
            stt_language=stt_language,
        ),
        model,
        "",
    )


def provision_gemini_media_engines() -> dict[str, Any]:
    """Provision (or clear) BOTH existing media seams from the Gemini config.

    This is the ONLY wiring entry point and it is called from the runtime
    supervisor's startup path. It never raises, never performs a request, and
    never partially provisions: without a credential both seams are left/set
    unprovisioned, so images and audio keep failing closed exactly as M1.3/M1.4
    record, and startup is unaffected.

    Returns a sanitized status dict for the caller's trace (it contains the model
    and the ENV VARIABLE NAME the key came from — never the key itself).
    """
    try:
        engine, model, reason = build_gemini_media_engine()
    except Exception as exc:  # noqa: BLE001 — provisioning must never break startup
        logger.warning("GEMINI_MEDIA_ENGINE_PROVISION_FAILED error=%s", type(exc).__name__)
        media_service.set_ocr_engine(None)
        media_service.set_stt_engine(None)
        return {"configured": False, "model": "", "key_env_var": "", "reason": "provisioning failed"}

    media_service.set_ocr_engine(engine)
    media_service.set_stt_engine(engine)
    logger.info(
        "GEMINI_MEDIA_ENGINE_PROVISIONED configured=%s model=%s stt_model=%s "
        "stt_language=%s key_env_var=%s reason=%s",
        engine is not None, model or "-",
        (engine.stt_model if engine else "") or "general-media-model",
        (engine.stt_language if engine else "") or "auto",
        (engine.key_env_var if engine else "") or "-",
        reason or "-",
    )
    return {
        "configured": engine is not None,
        "model": model,
        "key_env_var": engine.key_env_var if engine else "",
        "reason": reason,
    }
