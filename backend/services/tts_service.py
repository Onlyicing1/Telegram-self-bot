"""Text-to-Speech service — the ONE boundary between a TTS request and a provider.

Speech SYNTHESIS is a capability of its own, deliberately kept apart from speech
RECOGNITION: this module shares no type, no seam and no failure token with the STT
subsystem, so nothing here can be mistaken for a transcription and no STT
behavior changes.

    AI request (one structured action)
        ↓
    deterministic capability decision        this module (fail-closed)
        ↓
    the owner's persisted selection            ``backend/ai/tts_control_plane.py``
    (provider → model → voice, registry-validated; the default selection when the
    owner has configured nothing)
        ↓
    THIS boundary: input validation, bounds, the ONE bounded attempt plan under
    ONE awaited timeout, output validation, the normalized clip, the trace
        ↓
    the credential pool (``backend/services/tts_credential_pool.py``) and the
    bounded fallback rotation (``backend/services/tts_fallback.py``), resolved
    through the ONE provider → engine seam
    (``backend/services/tts_engine_factory.py``)
        ↓
    the provider adapter (``backend/services/openai_tts_engine.py``): ONE
    documented HTTP request to the provider's speech endpoint
        ↓
    the EXISTING Telegram delivery infrastructure (ONE voice note)

The owner's selection is a CAPABILITY fact, never a hint: an unregistered or
unimplemented provider/model/voice can never be selected, and a failure that is
DETERMINISTIC (a refused model or voice, a refused request, a malformed or empty
response) is never hidden behind a substitute provider.

The provider receives ONLY the text being synthesized and the minimum synthesis
configuration. There is no parameter on this path that could carry a chat id, a
message id, a sender, a caption, reply text, a filename, conversation history,
memory or any other Telegram context — the same zero-context rule the media
boundary enforces, enforced here by the shape of the call.

The boundary — not a handler and not an adapter — owns:

  * text validation and the bounded input size (a request above the bound FAILS:
    the requested content is never silently truncated to something the owner did
    not ask for);
  * the credential decision, reported as a bounded class instead of an exception
    at import time;
  * the wall-clock timeout around the ONE provider call;
  * output validation: non-empty audio inside a finite byte ceiling;
  * the normalized :class:`SpeechClip` — the only thing the caller receives;
  * temporary-resource discipline: the clip is bounded resident bytes and the
    boundary creates NO temporary file, so there is nothing to leak on success,
    provider failure, validation failure, timeout, cancellation or an unexpected
    exception (see the module's ``_EMPTY_TEMP_CONTRACT`` note below).

Failure taxonomy (closed, deterministic — see the ``FAILURE_*`` tokens): every
failure carries ONE token from this vocabulary plus, where the provider answered,
its HTTP status. The adapter that talked to the provider owns the classification
of a provider response; this boundary owns the classes for its own legs
(validation, credential, the awaited timeout, output validation) and propagates a
classified failure UNCHANGED. A programming error is never converted into a
provider failure: only :class:`TtsError` is handled here.

No secret is ever logged, returned, persisted or placed in a message: the only
credential fact this module may emit is the NAME of the environment variable a
credential came from (the adapter's ``key_env_var``), never its value.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

# ── Bounds ───────────────────────────────────────────────────────────────────
#: The owner-facing input ceiling for ONE synthesis. Finite and deliberately far
#: below the provider's own request bound (the speech model is a 2 000-token
#: model, which no supported script reaches at 1 000 characters), so a request
#: that is refused here was never at risk of being silently cut by the provider.
MAX_TTS_INPUT_CHARS = 1000

#: The smallest meaningful request: whitespace alone is not speech.
MIN_TTS_INPUT_CHARS = 1

#: Wall-clock bound for ONE synthesis, measured around the provider call only.
#: Finite, and comfortably inside the AI request envelope the tool passes down.
TTS_TIMEOUT_S = 60.0

#: The byte ceiling for ONE synthesized clip. A bounded resident buffer, never a
#: file: 5 MB is roughly three minutes of the requested speech format, so the
#: ceiling cannot be reached by a bounded input and an over-sized response is a
#: provider anomaly rather than a legitimate result.
MAX_TTS_AUDIO_BYTES = 5 * 1024 * 1024

#: No temporary file is created anywhere on this path. Documented as an explicit
#: contract so the guarantee is auditable rather than incidental: the clip is
#: bounded resident bytes handed to the existing Telegram delivery, which owns its
#: own transfer. Cleanup is therefore unconditional — there is nothing to clean.
_EMPTY_TEMP_CONTRACT = True

# ── Stage tokens (one per leg, so ONE live request identifies its own leg) ───
TTS_STAGE_VALIDATION = "tts_validation"
TTS_STAGE_CREDENTIAL = "tts_credential"
TTS_STAGE_PROVIDER = "tts_provider"
TTS_STAGE_TIMEOUT = "tts_timeout"
TTS_STAGE_OUTPUT = "tts_output"
TTS_STAGE_COMPLETED = "tts_completed"

# ── The closed failure taxonomy ──────────────────────────────────────────────
#: The credential is absent, so nothing was sent.
FAILURE_MISSING_CREDENTIAL = "missing_credential"
#: The provider rejected the credential (401).
FAILURE_AUTH = "auth"
#: The credential is valid but not authorized for this endpoint (403).
FAILURE_FORBIDDEN = "forbidden"
#: The provider's own rate limit (429).
FAILURE_RATE_LIMIT = "rate_limit"
#: The account's own quota is spent (a 429 the provider reports as quota).
FAILURE_QUOTA_EXCEEDED = "quota_exceeded"
#: This request is not one the provider accepts (a 400 we cannot narrow further).
FAILURE_INVALID_REQUEST = "invalid_request"
#: The registered model is not one this adapter may send.
FAILURE_UNSUPPORTED_MODEL = "unsupported_model"
#: The configured voice is not one this adapter may send.
FAILURE_UNSUPPORTED_VOICE = "unsupported_voice"
#: Nothing to synthesize (empty after normalization).
FAILURE_EMPTY_INPUT = "empty_input"
#: The requested text exceeds the bounded input ceiling — refused, not truncated.
FAILURE_INPUT_TOO_LARGE = "input_too_large"
#: The provider (or the boundary's own awaited bound) did not answer in time.
FAILURE_TIMEOUT = "timeout"
#: The provider could not be reached at all.
FAILURE_TRANSPORT = "transport"
#: The provider answered 5xx.
FAILURE_SERVER = "server"
#: The provider answered with a body this adapter cannot read.
FAILURE_MALFORMED = "malformed_response"
#: The provider answered successfully with no audio at all.
FAILURE_EMPTY_AUDIO = "empty_audio"
#: The provider answered with more audio than the byte ceiling allows.
FAILURE_OUTPUT_TOO_LARGE = "output_too_large"
#: The provider refused the request (any other non-2xx).
FAILURE_REJECTION = "provider_rejection"
#: The operation deadline was already spent when the provider leg started.
FAILURE_DEADLINE = "deadline"
#: No provider for this capability is registered on this runtime.
FAILURE_UNAVAILABLE = "provider_unavailable"
#: Every eligible provider (and credential) failed. Reported by the fallback
#: layer so an exhausted rotation is diagnosable as itself rather than being
#: mistaken for the selected provider's own failure.
FAILURE_FALLBACK_EXHAUSTED = "fallback_exhausted"

#: Every token above, so a test can prove the taxonomy is closed and that no
#: classification path can invent a new one.
FAILURE_CLASSES = frozenset({
    FAILURE_MISSING_CREDENTIAL,
    FAILURE_AUTH,
    FAILURE_FORBIDDEN,
    FAILURE_RATE_LIMIT,
    FAILURE_QUOTA_EXCEEDED,
    FAILURE_INVALID_REQUEST,
    FAILURE_UNSUPPORTED_MODEL,
    FAILURE_UNSUPPORTED_VOICE,
    FAILURE_EMPTY_INPUT,
    FAILURE_INPUT_TOO_LARGE,
    FAILURE_TIMEOUT,
    FAILURE_TRANSPORT,
    FAILURE_SERVER,
    FAILURE_MALFORMED,
    FAILURE_EMPTY_AUDIO,
    FAILURE_OUTPUT_TOO_LARGE,
    FAILURE_REJECTION,
    FAILURE_DEADLINE,
    FAILURE_UNAVAILABLE,
    FAILURE_FALLBACK_EXHAUSTED,
})

#: The classes that are transient BY NATURE (a retry of the SAME request could
#: legitimately succeed). Honest metadata only: the boundary itself performs NO
#: automatic retry, so this set is consumed by the fallback layer — which tries a
#: DIFFERENT provider or credential, never the same request again — to decide
#: whether a substitute is even eligible. An exhaustion is deliberately absent:
#: "every provider failed" is not a transient condition to retry.
_TRANSIENT_CLASSES = frozenset({
    FAILURE_TIMEOUT,
    FAILURE_TRANSPORT,
    FAILURE_SERVER,
    FAILURE_RATE_LIMIT,
    FAILURE_DEADLINE,
})


class TtsError(Exception):
    """Raised when speech cannot be synthesized honestly.

    Carries the SAME bounded, machine-readable shape the media path's failures
    carry, so one incident is diagnosable from the log alone and no caller has to
    parse prose:

      * ``stage``          — which leg failed (a ``TTS_STAGE_*`` token);
      * ``failure_class``  — ONE token from the closed taxonomy above;
      * ``http_status``    — the provider's status when it answered, else ``0``;
      * ``retryable``      — whether the class is transient by nature.

    The message is bounded and sanitized by whoever raises it (the provider
    adapter redacts the credential), and it never carries audio, transcript
    content or a Telegram identifier.
    """

    def __init__(
        self,
        message: str,
        *,
        stage: str = "",
        failure_class: str = "",
        http_status: int = 0,
        retryable: bool | None = None,
    ) -> None:
        super().__init__(message)
        self.stage = stage
        self.failure_class = str(failure_class or "")
        self.http_status = int(http_status or 0)
        self.retryable = (
            self.failure_class in _TRANSIENT_CLASSES
            if retryable is None
            else bool(retryable)
        )


@dataclass(frozen=True)
class SpeechClip:
    """The normalized result of ONE synthesis — and nothing else.

    Deliberately minimal: exactly what the existing Telegram delivery needs plus
    honest provenance. It holds NO chat id, message id, sender, username, caption,
    reply text, conversation history or arbitrary Telegram metadata, and it is
    never persisted.

    ``audio`` is bounded resident bytes; no path or file handle is ever exposed,
    so a caller cannot leak a temporary resource it does not own.
    """

    audio: bytes
    mime_type: str
    file_name: str
    characters: int
    provider: str
    model: str
    voice: str
    duration_s: float = 0.0


def _trace(request_id: str, stage: str, level: int = logging.INFO, **fields: Any) -> None:
    """Emit ONE bounded, content-free TTS trace line.

    Same ``key=value`` convention as the media and AI traces, so one request is
    followed stage by stage with the request id the AI layer already owns. Fields
    are closed tokens, counts, byte sizes and bounded reasons — never a credential
    value, the synthesized text or a Telegram identifier.
    """
    extra = " ".join(f"{key}={value}" for key, value in fields.items())
    logger.log(
        level,
        "TTS_STAGE request_id=%s stage=%s%s",
        request_id or "-", stage, f" {extra}" if extra else "",
    )


def bounded_reason(error: Any, limit: int = 200) -> str:
    """A bounded, single-line diagnostic reason (the media path's own helper)."""
    from backend.services.media_service import bounded_reason as _bounded

    return _bounded(error, limit)


def failure_class_of(error: BaseException) -> str:
    """The bounded failure-class token of a failed synthesis (never empty)."""
    return str(getattr(error, "failure_class", "") or FAILURE_TRANSPORT)


def normalize_request_text(text: Any) -> str:
    """The exact text to synthesize: stripped, and otherwise UNCHANGED.

    Only leading and trailing whitespace is removed (it is not content, and the
    provider would not speak it). The interior text is never collapsed, wrapped,
    translated, truncated or otherwise rewritten — the owner gets exactly the
    words that were requested, or an honest refusal.
    """
    return str(text or "").strip()


# ── Provider resolution (the ONE seam to the adapter) ────────────────────────


#: The provisioned selection. ``None`` means "nothing was applied yet", which is
#: a DIFFERENT state from a selection that happens to be the default: it is the
#: pre-existing single-provider behaviour, so a runtime that never applied a
#: persisted settings row behaves exactly as it did before the control plane
#: existed. Process-local on purpose (the persisted value lives on the owner's
#: ``ai_config`` row); a restart re-applies it from the store.
_selected: Any | None = None


def current_selection() -> Any:
    """The provisioned selection, or the registry's default when none was applied."""
    from backend.ai.tts_control_plane import default_selection

    return _selected if _selected is not None else default_selection()


def _engine() -> tuple[Any | None, str]:
    """``(engine, reason)`` for the provisioned (or default) selection.

    The adapter is resolved LAZILY and through ONE seam
    (``backend/services/tts_engine_factory``), so this boundary never imports a
    provider-specific HTTP shape and a provider cannot be substituted for
    another here: a selection this build cannot run returns ``None`` with a
    bounded reason rather than falling back to something the owner did not
    select.
    """
    from backend.services import tts_engine_factory

    return tts_engine_factory.build_engine(current_selection())


def capability_reason() -> str:
    """The deterministic capability decision: ``""`` when synthesis can run.

    Fail-closed and side-effect free: it builds the same engine a request would
    use, so the answer the panel shows and the answer a request gets can never
    disagree. When it is non-empty it is ONE ``FAILURE_*`` token — a missing
    credential is reported as itself, and nothing here claims provider health.
    """
    try:
        engine, reason = _engine()
    except Exception as exc:  # noqa: BLE001 — a capability probe is never fatal
        logger.warning("TTS_CAPABILITY_FAILED error=%s", type(exc).__name__)
        return FAILURE_UNAVAILABLE
    return "" if engine is not None else (reason or FAILURE_UNAVAILABLE)


def is_configured() -> bool:
    """True when this runtime can actually synthesize speech right now."""
    return capability_reason() == ""


def describe() -> dict[str, str]:
    """The provisioned selection as plain, non-secret labels for the panel.

    Never reads or returns a credential: it reports the provider, the model, the
    voice, the output format and the Persian capability state this build would
    use, plus the bounded capability state, so the owner-facing surface states
    facts that are true. A registered-but-unimplemented provider is never
    described as runnable and the Persian state is never rendered as a claim.
    """
    from backend.ai.tts_control_plane import persian_label

    selection = current_selection()
    model_entry = selection.model_entry
    return {
        "provider": selection.provider,
        "provider_label": selection.provider_label,
        "model": selection.model,
        "model_label": selection.model_label,
        "voice": selection.voice,
        "voice_label": selection.voice_label,
        "format": model_entry.output_format if model_entry is not None else "",
        "mime_type": model_entry.mime_type if model_entry is not None else "",
        "persian": selection.persian,
        "persian_label": persian_label(selection.persian),
        "adjusted": selection.adjusted,
        "reason": capability_reason(),
    }


# ── The settings-apply entry points (the ONE seam the surfaces call) ─────────


def apply_tts_settings(config: Any) -> dict[str, Any]:
    """Install the owner's persisted TTS selection onto this boundary.

    The ONE entry point called by the places that own this state: the runtime
    supervisor at startup (so the persisted values are in effect from the first
    synthesis) and the Telegram surface immediately after a change (so a change is
    effective on the NEXT request, with no redeploy and no restart). The caller
    reads the store; this function only receives plain values, so no Telegram
    object, owner id, chat id or message id can reach an engine.

    The fallback rotation is derived from the SAME resolved selection, and the
    credential the selected provider is provisioned with is recorded (never
    stored, never displayed) so the runtime's first attempt reuses the engine it
    already has.

    Never raises — a settings change must not be able to break either the panel or
    startup. Returns a sanitized status dict for the caller's trace; it contains
    the provider, the model, the voice and a bounded reason, never a credential.
    """
    global _selected
    try:
        from backend.ai.tts_control_plane import parse_tts_config
        from backend.services import tts_engine_factory, tts_fallback

        selection = parse_tts_config(config)
        _selected = selection
        credential = tts_engine_factory.provisioning_credential(selection)
        tts_fallback.register_plan(
            selection,
            provisioned_credential_id=(
                credential.credential_id if credential is not None else ""
            ),
        )
        reason = capability_reason()
        status: dict[str, Any] = {
            "configured": not reason,
            "provider": selection.provider,
            "tts_model": selection.model,
            "tts_voice": selection.voice,
            "persian": selection.persian,
            "adjusted": selection.adjusted,
            "reason": reason,
        }
        if reason:
            logger.warning(
                "TTS_ENGINE_UNPROVISIONED provider=%s model=%s voice=%s reason=%s",
                selection.provider, selection.model, selection.voice, reason,
            )
        else:
            logger.info(
                "TTS_ENGINE_APPLIED provider=%s model=%s voice=%s",
                selection.provider, selection.model, selection.voice,
            )
        return status
    except Exception as exc:  # noqa: BLE001 — a settings apply is never fatal
        logger.warning("TTS_ENGINE_APPLY_FAILED error=%s", type(exc).__name__)
        return {"configured": False, "reason": type(exc).__name__}


async def apply_tts_settings_async(config: Any) -> dict[str, Any]:
    """Credential-aware settings apply: load the pools, then provision.

    Ordered deliberately, exactly as the Speech-to-Text apply is: the pools are
    read FIRST, so the selection and its rotation are provisioned against the
    credentials that will actually be attempted, and a secret backend that is slow
    or unavailable delays provisioning by at most its own bounded read instead of
    failing it. Both halves are already never-fatal, so this adds no new failure
    mode to startup or to the settings panel.
    """
    try:
        from backend.ai.tts_control_plane import canonical_order, parse_tts_config
        from backend.services import tts_credential_pool

        selection = parse_tts_config(config)
        targets = canonical_order(selection.provider) or (selection.provider,)
        counts = await tts_credential_pool.prepare(targets)
        logger.info(
            "TTS_CREDENTIAL_PREPARE providers=%s credentials=%s",
            len(counts), sum(counts.values()),
        )
    except Exception as exc:  # noqa: BLE001 — an optional pool is never fatal
        logger.warning("TTS_CREDENTIAL_PREPARE_FAILED error=%s", type(exc).__name__)
    return apply_tts_settings(config)


# ── The boundary ─────────────────────────────────────────────────────────────


def validate(text: str, *, request_id: str = "") -> str:
    """Validate ONE request's text, or fail with the matching bounded class.

    The order is fixed: an empty request is its own class (nothing to say) and an
    over-long request is REFUSED rather than truncated — silently synthesizing a
    prefix would deliver something the owner did not ask for.
    """
    normalized = normalize_request_text(text)
    if len(normalized) < MIN_TTS_INPUT_CHARS:
        raise TtsError(
            "There is no text to speak.", stage=TTS_STAGE_VALIDATION,
            failure_class=FAILURE_EMPTY_INPUT,
        )
    if len(normalized) > MAX_TTS_INPUT_CHARS:
        raise TtsError(
            f"The text is longer than the {MAX_TTS_INPUT_CHARS}-character limit "
            "for one voice message.",
            stage=TTS_STAGE_VALIDATION,
            failure_class=FAILURE_INPUT_TOO_LARGE,
        )
    return normalized


def call_timeout(timeout_s: Any = None) -> float:
    """The provider call's bound: the ceiling ∩ the caller's remaining budget."""
    try:
        value = float(timeout_s)
    except (TypeError, ValueError):
        return TTS_TIMEOUT_S
    if value <= 0:
        return TTS_TIMEOUT_S
    return min(TTS_TIMEOUT_S, value)


async def synthesize(
    text: Any,
    *,
    request_id: str = "",
    timeout_s: Any = None,
) -> SpeechClip:
    """Synthesize ``text`` into ONE bounded clip, or raise :class:`TtsError`.

    The whole contract, in order: validate → resolve the provisioned selection's
    provider and credentials → the ONE bounded attempt plan (the selected
    provider first, then at most its registered substitutes and the credentials of
    each, all sharing ONE deadline) → validate the audio → return the normalized
    clip. Nothing else is attempted, no attempt is ever retried as itself, and no
    temporary file is created.

    Raises:
        TtsError:          a classified failure from the closed taxonomy.
        asyncio.CancelledError: re-raised unchanged.
    """
    normalized = validate(text, request_id=request_id)
    bound = call_timeout(timeout_s)
    _trace(
        request_id, "tts_requested",
        chars=len(normalized), budget_s=f"{bound:.1f}",
    )

    engine, reason = _engine()
    if engine is None:
        _trace(
            request_id, "tts_credential_unavailable",
            reason=reason or FAILURE_UNAVAILABLE, level=logging.WARNING,
        )
        raise TtsError(
            "No speech provider is configured for this runtime; nothing was sent.",
            stage=TTS_STAGE_CREDENTIAL,
            failure_class=reason or FAILURE_UNAVAILABLE,
        )

    started = time.monotonic()
    # The fallback layer is consulted LAZILY: with no registered rotation (an
    # unconfigured installation, and every legacy single-provider state) it
    # returns ``None`` and this boundary keeps its exact fail-closed
    # single-engine behavior.
    from backend.services import tts_fallback

    plan = tts_fallback.attempt_plan(engine)
    if plan is None:
        audio = await _run_engine(engine, normalized, bound, request_id=request_id)
        producer = engine
    else:
        producer, audio = await plan.run(
            normalized, bound, _run_engine, request_id=request_id,
        )

    audio = _validate_audio(audio, request_id=request_id)
    clip = SpeechClip(
        audio=audio,
        # The clip reports the engine that ACTUALLY produced the result: after a
        # fallback that is the substitute provider, never the selection, so the
        # caller (and its trace) always knows which provider/model/voice spoke.
        mime_type=producer.mime_type,
        file_name=producer.file_name,
        characters=len(normalized),
        provider=producer.provider,
        model=producer.model,
        voice=producer.voice,
        # The speech response carries no duration metadata and the container is
        # deliberately not parsed here, so the duration stays explicitly unknown
        # rather than guessed. Telegram renders the voice note regardless.
        duration_s=0.0,
    )
    _trace(
        request_id, TTS_STAGE_COMPLETED,
        provider=clip.provider, model=clip.model, voice=clip.voice,
        chars=clip.characters, bytes=len(clip.audio),
        elapsed_ms=int((time.monotonic() - started) * 1000),
        fallback=producer.provider != engine.provider,
    )
    return clip


async def _run_engine(
    engine: Any, text: str, bound_s: Any, *, request_id: str = "",
) -> bytes:
    """ONE bounded provider call, under ONE awaited timeout.

    The attempt primitive the fallback layer drives: it owns the awaited timeout,
    the per-attempt trace and the classification of a provider failure, and it
    never retries — a second attempt is a DIFFERENT provider or credential, which
    is the fallback layer's decision, not this function's. ``bound_s`` is the
    caller's remaining budget, so every attempt shares one deadline.
    """
    try:
        bound = max(0.001, float(bound_s))
    except (TypeError, ValueError):
        bound = TTS_TIMEOUT_S
    started = time.monotonic()
    _trace(request_id, "tts_provider_call_started", provider=engine.provider)
    try:
        audio = await asyncio.wait_for(
            engine.speak(text, timeout_s=bound), timeout=bound,
        )
    except asyncio.CancelledError:
        raise
    except TtsError as exc:
        _trace(
            request_id, "tts_provider_call_failed",
            provider=engine.provider,
            failure_class=failure_class_of(exc),
            http_status=exc.http_status or "-",
            elapsed_ms=int((time.monotonic() - started) * 1000),
            level=logging.WARNING,
        )
        raise
    except asyncio.TimeoutError as exc:
        _trace(
            request_id, "tts_provider_call_failed",
            provider=engine.provider, failure_class=FAILURE_TIMEOUT,
            elapsed_ms=int((time.monotonic() - started) * 1000),
            level=logging.WARNING,
        )
        raise TtsError(
            f"Speech synthesis did not finish within {bound:g}s.",
            stage=TTS_STAGE_TIMEOUT, failure_class=FAILURE_TIMEOUT,
        ) from exc
    # The response is returned UNCHANGED: its shape is the output leg's contract
    # (``_validate_audio`` owns that classification), so a body this runtime
    # cannot use is refused there rather than raising an unclassified TypeError
    # here.
    return audio


def _validate_audio(audio: Any, *, request_id: str = "") -> bytes:
    """The accepted response's audio, or the matching bounded failure.

    Only two conditions are checked, and both are deterministic: the response
    must carry bytes (an empty body is a provider anomaly, never silent speech)
    and it must be inside the byte ceiling. The container is deliberately NOT
    re-parsed: the clip is only ever handed to Telegram, which validates it, and
    a local container guess could refuse a legitimate format change.
    """
    if not isinstance(audio, (bytes, bytearray)) or not audio:
        _trace(request_id, "tts_empty_audio", level=logging.WARNING)
        raise TtsError(
            "The speech provider returned no audio.",
            stage=TTS_STAGE_OUTPUT, failure_class=FAILURE_EMPTY_AUDIO,
        )
    data = bytes(audio)
    if len(data) > MAX_TTS_AUDIO_BYTES:
        _trace(
            request_id, "tts_output_too_large", bytes=len(data),
            limit=MAX_TTS_AUDIO_BYTES, level=logging.WARNING,
        )
        raise TtsError(
            "The speech provider returned more audio than this runtime accepts.",
            stage=TTS_STAGE_OUTPUT, failure_class=FAILURE_OUTPUT_TOO_LARGE,
        )
    return data
