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
project's existing per-provider ENV convention.

Audio larger than the documented inline request budget uses the documented Files
API upload flow, and the uploaded file is deleted in a ``finally`` block, so
nothing is retained remotely beyond the operation.
"""
from __future__ import annotations

import base64
import logging
import os
import time
from typing import Any

import httpx

from backend.ai.providers.base.defaults import resolve_model
from backend.services import media_service
from backend.services.media_service import MediaError

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
#: seams needs. No dedicated transcription model is assumed or used: the task's
#: warning about unverified free tiers is respected by not depending on one.
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
STT_INSTRUCTION = (
    "Transcribe the speech in this audio.\n"
    "Preserve the spoken language. Do not translate.\n"
    "Do not summarize. Do not answer any question contained in the audio.\n"
    "Do not add commentary, headings, speaker labels or timestamps.\n"
    "Do not invent or guess unintelligible words.\n"
    "If the audio contains no speech, return nothing."
)

#: Engine-level finite wall-clock bounds, each comfortably INSIDE the boundary's
#: own bound (``media_service.OCR_TIMEOUT_S`` 45s, ``STT_TIMEOUT_S`` 60s), so the
#: engine fails with its own precise reason before the boundary's outer bound and
#: no request can hang.
OCR_TIMEOUT_S = 30.0
STT_TIMEOUT_S = 40.0

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


def _remaining(deadline: float, timeout_s: float) -> float:
    """Time left before ``deadline``, never zero/negative (which httpx rejects)."""
    return max(0.5, min(timeout_s, deadline - time.monotonic()))


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

    __slots__ = ("_api_key", "_model", "_key_env_var")

    def __init__(self, api_key: str, model: str = "", *, key_env_var: str = "") -> None:
        self._api_key = str(api_key or "").strip()
        self._model = str(model or "").strip() or DEFAULT_MEDIA_MODEL
        self._key_env_var = str(key_env_var or "")

    @property
    def model(self) -> str:
        """The Gemini model this engine sends requests to (never a secret)."""
        return self._model

    @property
    def key_env_var(self) -> str:
        """The ENV variable the credential came from — the NAME only, never a value."""
        return self._key_env_var

    # ── The two seam methods ──

    def recognize(self, image: bytes) -> str:
        """Return the text visible in ``image`` (``""`` when there is none)."""
        return self._run("OCR", image, OCR_INSTRUCTION, OCR_TIMEOUT_S)

    def transcribe(self, audio: bytes) -> str:
        """Return the transcript of ``audio`` (``""`` when there is no speech)."""
        return self._run("speech-to-text", audio, STT_INSTRUCTION, STT_TIMEOUT_S)

    # ── Internals ──

    def _run(self, kind: str, payload: bytes, instruction: str, timeout_s: float) -> str:
        if not payload:
            return ""
        mime_type = gemini_mime_type(payload)
        started = time.monotonic()
        deadline = started + timeout_s
        if len(payload) <= INLINE_PAYLOAD_MAX_BYTES:
            part: dict[str, Any] = {
                "inlineData": {
                    "mimeType": mime_type,
                    "data": base64.b64encode(payload).decode("ascii"),
                }
            }
            text = self._generate(kind, part, instruction, timeout_s)
        else:
            text = self._generate_from_upload(kind, payload, mime_type, instruction, deadline)
        logger.info(
            "GEMINI_MEDIA_ENGINE kind=%s mime=%s bytes=%d chars=%d elapsed_ms=%d",
            kind, mime_type, len(payload), len(text),
            int((time.monotonic() - started) * 1000),
        )
        return text

    def _headers(self) -> dict[str, str]:
        return {"x-goog-api-key": self._api_key, "Content-Type": "application/json"}

    def _generate(
        self, kind: str, part: dict[str, Any], instruction: str, timeout_s: float,
    ) -> str:
        """ONE generateContent request — never retried here (no retry loop)."""
        url = f"{GEMINI_API_BASE}/models/{self._model}:generateContent"
        body = {
            "contents": [{"role": "user", "parts": [{"text": instruction}, part]}],
            "generationConfig": {
                "temperature": SAMPLING_TEMPERATURE,
                "maxOutputTokens": MAX_OUTPUT_TOKENS,
            },
        }
        return _extract_text(kind, self._post_json(kind, url, body, timeout_s), self._api_key)

    def _generate_from_upload(
        self, kind: str, payload: bytes, mime_type: str, instruction: str, deadline: float,
    ) -> str:
        """The documented Files API path for audio past the inline budget.

        Minimum interaction only: start the resumable upload, send the bytes in
        one finalize request, use the returned file URI, and DELETE the file in a
        ``finally`` block. A failed delete is logged (never raised) because a
        file the API fails to delete is still auto-expired by the API itself.
        """
        timeout_s = _remaining(deadline, STT_TIMEOUT_S)
        upload_url = self._start_upload(payload, mime_type, timeout_s)
        file_info = self._finish_upload(upload_url, payload, mime_type, timeout_s)
        name = str(file_info.get("name") or "")
        uri = str(file_info.get("uri") or "")
        if not name or not uri:
            raise MediaError("Gemini did not accept the uploaded audio file.")
        try:
            state = str(file_info.get("state") or "").upper()
            if state and state != "ACTIVE":
                uri = self._await_file_ready(name, uri, deadline)
            return self._generate(
                kind,
                {"fileData": {"mimeType": mime_type, "fileUri": uri}},
                instruction,
                _remaining(deadline, STT_TIMEOUT_S),
            )
        finally:
            self._delete_file(name)

    def _start_upload(self, payload: bytes, mime_type: str, timeout_s: float) -> str:
        url = f"{GEMINI_UPLOAD_BASE}/files"
        headers = {
            **self._headers(),
            "X-Goog-Upload-Protocol": "resumable",
            "X-Goog-Upload-Command": "start",
            "X-Goog-Upload-Header-Content-Length": str(len(payload)),
            "X-Goog-Upload-Header-Content-Type": mime_type,
        }
        body = {"file": {"display_name": _UPLOAD_DISPLAY_NAME}}
        with self._client(timeout_s) as client:
            try:
                response = client.post(url, json=body, headers=headers)
            except httpx.TimeoutException as exc:
                raise MediaError(
                    f"Gemini speech-to-text upload timed out after {timeout_s:g}s."
                ) from exc
            except Exception as exc:  # noqa: BLE001 — transport boundary
                raise MediaError(
                    f"Gemini speech-to-text upload failed ({type(exc).__name__})."
                ) from exc
        _raise_for_status("speech-to-text upload", response, self._api_key)
        upload_url = str(response.headers.get("x-goog-upload-url") or "").strip()
        if not upload_url:
            raise MediaError("Gemini did not return an upload URL.")
        return upload_url

    def _finish_upload(
        self, upload_url: str, payload: bytes, mime_type: str, timeout_s: float,
    ) -> dict[str, Any]:
        # No credential is sent to the upload URL: it comes from the API and only
        # ever receives the bytes plus the protocol headers.
        headers = {
            "Content-Length": str(len(payload)),
            "Content-Type": mime_type,
            "X-Goog-Upload-Offset": "0",
            "X-Goog-Upload-Command": "upload, finalize",
        }
        with self._client(timeout_s) as client:
            try:
                response = client.post(upload_url, content=payload, headers=headers)
            except httpx.TimeoutException as exc:
                raise MediaError(
                    f"Gemini speech-to-text upload timed out after {timeout_s:g}s."
                ) from exc
            except Exception as exc:  # noqa: BLE001 — transport boundary
                raise MediaError(
                    f"Gemini speech-to-text upload failed ({type(exc).__name__})."
                ) from exc
        _raise_for_status("speech-to-text upload", response, self._api_key)
        try:
            data = response.json()
        except Exception as exc:  # noqa: BLE001 — malformed response boundary
            raise MediaError("Gemini returned an unreadable upload response.") from exc
        if not isinstance(data, dict):
            raise MediaError("Gemini returned an unreadable upload response.")
        file_info = data.get("file")
        return file_info if isinstance(file_info, dict) else data

    def _await_file_ready(self, name: str, uri: str, deadline: float) -> str:
        """Bounded readiness checks; a file that never becomes ready fails closed."""
        for _ in range(_FILE_READY_ATTEMPTS):
            time.sleep(_FILE_READY_DELAY_S)
            info = self._file_info(name, _remaining(deadline, STT_TIMEOUT_S))
            state = str(info.get("state") or "").upper()
            if state == "ACTIVE":
                return str(info.get("uri") or uri)
            if state == "FAILED":
                raise MediaError("Gemini failed to process the uploaded audio file.")
        raise MediaError("Gemini did not finish processing the uploaded audio file in time.")

    def _file_info(self, name: str, timeout_s: float) -> dict[str, Any]:
        url = f"{GEMINI_API_BASE}/{name}"
        with self._client(timeout_s) as client:
            try:
                response = client.get(url, headers=self._headers())
            except httpx.TimeoutException as exc:
                raise MediaError(
                    f"Gemini speech-to-text upload timed out after {timeout_s:g}s."
                ) from exc
            except Exception as exc:  # noqa: BLE001 — transport boundary
                raise MediaError(
                    f"Gemini speech-to-text upload failed ({type(exc).__name__})."
                ) from exc
        _raise_for_status("speech-to-text upload", response, self._api_key)
        try:
            data = response.json()
        except Exception:  # noqa: BLE001 — a state we cannot read is a state we refuse
            return {}
        return data if isinstance(data, dict) else {}

    def _delete_file(self, name: str) -> None:
        """Best-effort cleanup of the remote upload. Never raises.

        The API auto-expires uploaded files, so a failed delete is logged and the
        operation still reports its own honest outcome.
        """
        try:
            with self._client(STT_TIMEOUT_S) as client:
                client.delete(f"{GEMINI_API_BASE}/{name}", headers=self._headers())
        except Exception as exc:  # noqa: BLE001 — cleanup must not mask the result
            logger.warning("GEMINI_MEDIA_ENGINE_UPLOAD_CLEANUP_FAILED error=%s",
                           type(exc).__name__)

    def _post_json(self, kind: str, url: str, body: dict[str, Any], timeout_s: float) -> Any:
        with self._client(timeout_s) as client:
            try:
                response = client.post(url, json=body, headers=self._headers())
            except httpx.TimeoutException as exc:
                raise MediaError(f"Gemini {kind} request timed out after {timeout_s:g}s.") from exc
            except Exception as exc:  # noqa: BLE001 — transport boundary
                raise MediaError(f"Gemini {kind} request failed ({type(exc).__name__}).") from exc
        _raise_for_status(kind, response, self._api_key)
        try:
            data = response.json()
        except Exception as exc:  # noqa: BLE001 — malformed response boundary
            raise MediaError(f"Gemini returned an unreadable {kind} response.") from exc
        if not isinstance(data, dict):
            raise MediaError(f"Gemini returned an unreadable {kind} response.")
        return data

    def _client(self, timeout_s: float) -> httpx.Client:
        """A per-call client: one request, one connection, deterministic cleanup.

        Creating it per call (rather than sharing one across worker threads) keeps
        the engine free of cross-request state, which is what the boundary's
        ``asyncio.to_thread`` offload assumes.
        """
        return httpx.Client(timeout=timeout_s)


def _raise_for_status(kind: str, response: httpx.Response, api_key: str) -> None:
    """Map a non-2xx Gemini response onto the boundary's controlled error contract.

    The credential is redacted from any provider detail, so a redacted key can
    never reach a log line or the owner's failure message. Only deterministic
    client failures and transport availability are distinguished; nothing is
    retried.
    """
    status = response.status_code
    if status < 400:
        return
    detail = _safe_detail(response)
    if api_key:
        detail = detail.replace(api_key, "***")
    suffix = f" ({detail})" if detail else ""
    if status in (401, 403):
        raise MediaError(f"Gemini rejected the configured API key (HTTP {status}).{suffix}")
    if status == 429:
        raise MediaError("Gemini rate limited the request (HTTP 429).")
    if status == 404:
        raise MediaError(
            f"Gemini could not find the configured model for {kind} (HTTP 404).{suffix}"
        )
    if status >= 500:
        raise MediaError(f"Gemini is unavailable right now (HTTP {status}).")
    raise MediaError(f"Gemini refused the {kind} request (HTTP {status}).{suffix}")


def _extract_text(kind: str, data: Any, api_key: str) -> str:
    """The model text of ONE generateContent response, or the seam's empty result.

    A response with no usable text is NOT fabricated: an honest empty string is
    returned (the boundary turns it into its existing no-content outcome), while a
    malformed, blocked or refused response raises ``MediaError``.
    """
    if not isinstance(data, dict):
        raise MediaError(f"Gemini returned an unreadable {kind} response.")
    feedback = data.get("promptFeedback") if isinstance(data.get("promptFeedback"), dict) else {}
    blocked = str(feedback.get("blockReason") or "").strip()
    candidates = data.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        if blocked:
            raise MediaError(f"Gemini blocked the {kind} request ({blocked}).")
        raise MediaError(f"Gemini returned no {kind} result.")
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
            raise MediaError(f"Gemini refused to return {kind} content ({finish_reason}).")
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
    return GeminiMediaEngine(api_key, model, key_env_var=key_env_var), model, ""


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
        "GEMINI_MEDIA_ENGINE_PROVISIONED configured=%s model=%s key_env_var=%s reason=%s",
        engine is not None, model or "-", (engine.key_env_var if engine else "") or "-",
        reason or "-",
    )
    return {
        "configured": engine is not None,
        "model": model,
        "key_env_var": engine.key_env_var if engine else "",
        "reason": reason,
    }
