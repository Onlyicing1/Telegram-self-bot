"""STT provider health + bounded automatic fallback — the ordered attempt layer,
extended with CREDENTIAL rotation inside one provider.

This is the EXECUTION half of STT resilience, beside the seams it consumes and
never duplicates:

    the control plane (``backend/ai/stt_control_plane.py``) registers the
    candidates and owns the owner's persisted selection — including the
    canonical, deterministic candidate ORDER;
        ↓
    this module: health state, failure classification, cooldown and the
    bounded attempt loop over THAT order — and, inside one provider, over the
    credential pool of that provider;
        ↓
    the credential pool (``backend/services/stt_credential_pool.py``) owns the
    credential order, credential cooldown and the credential-vs-provider
    classification, and the secret boundary behind it
    (``backend/ai/credential_source.py``) owns where a credential comes from;
        ↓
    the engine factory (``backend/services/stt_engine_factory.py``) is the
    ONE candidate → engine seam, consulted lazily per candidate and per
    credential;
        ↓
    the media boundary (``backend/services/media_service.py``) drives one
    attempt through its existing ``_run_stt`` primitive and its existing
    per-chunk / aggregate deadlines.

What this layer decides: which candidate to try next, in what order, which of
its credentials to try first, whether a candidate or a credential is
temporarily unhealthy, whether a failure is fallback-eligible, and when to
stop. What it never does: talk to a provider itself, touch a Telegram object,
rewrite the owner's selection, or replace the boundary — the selected engine
stays the FIRST attempt of every request, and a fallback (provider or
credential) is an internal substitution for ONE request only.

The two resilience axes stay apart, because they answer different questions:

  * a CREDENTIAL failure (a rejected, revoked, spent or rate-limited key) means
    this deployment has another key for a provider that is working — it rotates
    inside the provider and never marks the provider unhealthy;
  * a PROVIDER failure (5xx, timeout, transport, capacity) means the provider is
    the problem — the rest of its credentials would fail the same way, so the
    pool is NOT burned and the existing provider-level fallback runs.

Selected-provider semantics: the configured candidate is always attempted
first, on every request, whatever its health — the owner's preference must
not silently lose priority because it failed once. Cooldown prunes only the
FALLBACK rotation: a provider that repeatedly fails temporarily leaves the
list of substitutes, and its cooldown expires deterministically. A successful
request restores health immediately. Nothing here touches the Telegram UI
state or the persisted ``ai_config`` selection. The same rule applies one level
down: a credential that succeeded stays pinned for the later chunks of the SAME
request, and a credential that failed takes its own bounded cooldown instead of
dragging its provider out of the rotation.

Failure classification is fail-closed: only failures that are transient BY
NATURE (provider timeout, transport, 5xx, rate limit, spent engine deadline)
allow another candidate to be tried; deterministic failures (bad credential,
unsupported audio/model, malformed or empty response, provider rejection)
and anything the boundary cannot classify — including programming errors —
propagate unchanged instead of cascading through every provider. Adapter
engines attach their own bounded ``failure_class`` / ``retryable`` metadata
to the ``MediaError`` they raise; when that metadata is present it WINS,
because the adapter that talked to the provider is the honest classifier. A
credential-specific classification (the pool's own, derived from those same
tokens and from the adapter's ``http_status``) is consulted FIRST, and only
where a pool actually exists: with a single configured credential the runtime
behaves exactly as it did before the pool existed, so a rejection never
silently becomes a provider sweep.

An exhausted rotation is reported as itself ONLY when a substitute actually
ran: if no other candidate was runnable (no credential, no engine) the
SELECTED provider's own classified failure propagates unchanged, so arming
this layer never rewrites the identity of a single-provider failure. The same
holds for credential exhaustion: a provider that exhausted a REAL pool moves to
the provider-level fallback, while a provider with one credential keeps its own
failure verbatim.

Bounds: at most :data:`MAX_PROVIDER_ATTEMPTS` provider attempts and at most
:data:`MAX_TOTAL_ATTEMPTS` attempts in total per ``transcribe`` unit (one
chunk, or one single-piece audio), each sharing the ONE budget the boundary
hands in — a later attempt receives only the REMAINING budget, never a fresh
one. Credential rotation therefore cannot multiply the pre-existing worst case
by more than its documented factor, and a provider's own credential ceiling
(:data:`stt_credential_pool.MAX_CREDENTIAL_ATTEMPTS_PER_PROVIDER`) bounds it
further. Multi-pass/consensus behavior is untouched: it lives inside each
engine, and one ``transcribe()`` call is one attempt here however many passes
it contains. A failed provider operation is never treated as a recognition
hypothesis, and a failed credential is never treated as one either.

Health state is process-local and deliberately NOT persisted: it is runtime
posture, not configuration, and a restart honestly resets it.

Zero context: the only inputs on this path are candidate and credential
identifiers, the owner's plain language/pass settings and the already-validated
audio bytes. No chat id, message id, sender, caption, filename, reply text, AI
history or memory can enter — the module has no parameter that could carry one.

Traces are structured, bounded and content-free: candidate ids, credential ids,
attempt indices, failure classes, elapsed time and cooldown durations — never a
credential VALUE, audio bytes, transcript contents or a Telegram identifier. A
credential id is the only credential fact that is ever logged, and the secret
boundary guarantees it is non-secret.

Import direction: this module is bound by the media boundary
(``backend.services.media_service``), so it declares NO module-level import of
that boundary, of the control plane, of the credential pool or of the engine
factory — reaching any of them pulls ``backend.services.gemini_media_engine``,
which imports the boundary. The dependency therefore points ONE way at import
time; every one of them is resolved on first use.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Awaitable, Callable

if TYPE_CHECKING:  # pragma: no cover - typing only, never executed
    from backend.ai.credential_source import CredentialRecord
    from backend.ai.stt_control_plane import SttControlPlane

logger = logging.getLogger(__name__)

#: The failure class of the FINAL error when every eligible candidate failed.
#: Reported as the ``failure_class`` trace field so an exhausted rotation is
#: diagnosable as itself — never mistaken for a single provider's failure.
STT_FALLBACK_EXHAUSTED = "fallback_exhausted"

#: Hard ceiling on provider attempts for ONE transcription unit (one chunk of
#: a divided recording, or one single-piece audio). The selected provider plus
#: at most two substitutes: reliability, not an unbounded provider sweep. The
#: boundary's aggregate deadline remains the wall-clock ceiling above this.
MAX_PROVIDER_ATTEMPTS = 3

#: Hard ceiling on ALL attempts of one unit — providers and credential
#: rotations together. Twice the provider ceiling, deliberately: credential
#: rotation may at most DOUBLE the pre-existing worst case (the providers the
#: layer already swept plus their credential rotations), so a pool can never
#: turn one media operation into an unbounded provider workload. The shared
#: budget and the per-provider credential ceiling bound it further, and the
#: boundary's aggregate deadline remains the absolute upper bound.
MAX_TOTAL_ATTEMPTS = 2 * MAX_PROVIDER_ATTEMPTS

#: A substitute attempt starts only with at least this much of the unit's
#: budget left — the same floor the provider adapters use for their own
#: bounded retries, so a starved attempt is never started just to time out.
MIN_ATTEMPT_S = 8.0

#: Cooldown for a provider that failed: bounded doubling from this base,
#: capped at :data:`COOLDOWN_MAX_S`. Deterministic, finite, and short enough
#: that a blipped provider is eligible again within minutes.
COOLDOWN_BASE_S = 60.0
COOLDOWN_MAX_S = 600.0

#: The failure classes that are transient BY NATURE when an adapter did not
#: attach its own ``retryable`` verdict. Everything else — credentials,
#: unsupported audio/models, malformed or empty responses, rejections, and
#: anything unrecognized — is fail-closed. These are the engines' own closed
#: tokens (``gemini_media_engine``, ``groq_stt_engine``,
#: ``speechmatics_stt_engine``); the vocabulary is mirrored here, not invented.
#: The deterministic tokens the adapters also emit (``upload_failed``,
#: ``request_failed``, ``interaction_failed``, ``file_processing``,
#: ``malformed_response``, ``empty_transcription``, ``provider_rejection``,
#: ``auth``, ``forbidden``, ``missing_credential``, ``unsupported_*``,
#: ``invalid_*``) are deliberately ABSENT: they never authorize a substitute.
_FALLBACK_ELIGIBLE_CLASSES = frozenset({
    "timeout",
    "transport",
    "transport_failure",
    "server",
    "rate_limit",
    "operation_deadline",
    "upload_timeout",
    "request_timeout",
    "interaction_timeout",
})


def fallback_eligible(exc: BaseException) -> bool:
    """Whether ``exc`` may allow the NEXT candidate to be tried.

    Fail-closed by construction: only a ``MediaError`` can be eligible, the
    adapter's own ``retryable`` verdict wins when present, the boundary's
    awaited-timeout leg (``media_stt_timeout`` — the provider was too slow for
    THIS budget) is eligible, and a recognized transient class is the last
    resort. A non-MediaError (a programming error surfacing through the engine
    boundary) is never eligible and is never hidden behind fallback.
    """
    # Imported lazily: this module is bound by the boundary, so the dependency
    # points one way at import time.
    from backend.services.media_service import MEDIA_STAGE_STT_TIMEOUT, MediaError

    if not isinstance(exc, MediaError):
        return False
    retryable = getattr(exc, "retryable", None)
    if retryable is not None:
        return bool(retryable)
    if getattr(exc, "stage", "") == MEDIA_STAGE_STT_TIMEOUT:
        return True
    failure_class = str(getattr(exc, "failure_class", "") or "")
    return failure_class in _FALLBACK_ELIGIBLE_CLASSES


def failure_class_of(exc: BaseException) -> str:
    """The bounded failure-class token of a failed attempt (never empty)."""
    from backend.services.media_service import MEDIA_STAGE_STT_TIMEOUT

    failure_class = str(getattr(exc, "failure_class", "") or "")
    if failure_class:
        return failure_class
    if getattr(exc, "stage", "") == MEDIA_STAGE_STT_TIMEOUT:
        return "timeout"
    return "unknown"


# ── Health state (process-local, never persisted) ──


@dataclass
class _Health:
    """Bounded failure posture of one candidate."""

    failures: int = 0
    cooldown_until: float = 0.0
    last_failure_class: str = ""


_health: dict[str, _Health] = {}


def _clock() -> float:
    return time.monotonic()


def is_cooled_down(candidate_id: str) -> bool:
    """True while the candidate is serving its post-failure cooldown."""
    entry = _health.get(candidate_id)
    return bool(entry and entry.cooldown_until > _clock())


def record_failure(candidate_id: str, failure_class: str) -> float:
    """Mark one failure; return the cooldown seconds now in force."""
    entry = _health.setdefault(candidate_id, _Health())
    entry.failures += 1
    entry.last_failure_class = failure_class
    cooldown_s = min(COOLDOWN_BASE_S * (2 ** (entry.failures - 1)), COOLDOWN_MAX_S)
    entry.cooldown_until = _clock() + cooldown_s
    logger.info(
        "STT_FALLBACK_COOLDOWN candidate=%s failures=%d cooldown_s=%.0f "
        "failure_class=%s",
        candidate_id, entry.failures, cooldown_s, failure_class,
    )
    return cooldown_s


def record_success(candidate_id: str) -> None:
    """A completed request restores the candidate's health immediately."""
    _health.pop(candidate_id, None)


def reset_health() -> None:
    """Drop every health record (tests and explicit resets)."""
    _health.clear()


# ── The registered rotation (from the control plane, at settings-apply time) ──


@dataclass(frozen=True)
class _Registration:
    """The deterministic fallback rotation derived from the owner's selection."""

    active_id: str
    fallback_ids: tuple[str, ...]
    language: str
    passes: int
    provisioned_credential_id: str = ""


_registration: _Registration | None = None


def register_plan(
    plane: SttControlPlane, *, provisioned_credential_id: str = "",
) -> None:
    """Adopt the rotation implied by the owner's persisted STT selection.

    Called by the engine factory's ONE apply entry point, so the rotation is
    always derived from the SAME parsed configuration that provisioned the
    selected engine — never from a second read of the store. An unresolved
    legacy value clears the rotation: a legacy configuration keeps its exact
    pre-fallback single-engine behavior.

    ``provisioned_credential_id`` is the pool credential the selected engine was
    built with (empty when the provider's pool was never loaded). It is recorded
    — never stored, never displayed — so the first attempt can reuse the engine
    the runtime already has instead of building an equivalent one, and so a
    credential reported in a trace is the one actually used.
    """
    from backend.ai.stt_control_plane import get_candidate

    global _registration
    if plane.is_legacy or not plane.active_id:
        _registration = None
        logger.info("STT_FALLBACK_PLAN state=inactive reason=unresolved_selection")
        return
    fallback_ids = tuple(
        candidate.candidate_id
        for candidate in (get_candidate(cid) for cid in plane.fallback_ids)
        if candidate is not None and candidate.implemented
    )
    _registration = _Registration(
        active_id=plane.active_id,
        fallback_ids=fallback_ids,
        language=plane.language,
        passes=plane.passes,
        provisioned_credential_id=str(provisioned_credential_id or ""),
    )
    logger.info(
        "STT_FALLBACK_PLAN state=active selected=%s fallback_candidates=%d",
        plane.active_id, len(fallback_ids),
    )


def registration() -> _Registration | None:
    """The registered rotation, or ``None`` when fallback is inactive."""
    return _registration


def clear_registration() -> None:
    """Drop the registered rotation (tests and explicit resets)."""
    global _registration
    _registration = None


def _candidate_provider(candidate_id: str) -> str:
    """The provider a registered candidate belongs to (``""`` when unknown)."""
    from backend.ai.stt_control_plane import get_candidate

    candidate = get_candidate(candidate_id)
    return candidate.provider if candidate is not None else ""


def _build_candidate_engine(
    candidate_id: str, credential: CredentialRecord | None = None,
) -> tuple[Any | None, str]:
    """``(engine, reason)`` for one REGISTERED candidate and one credential.

    The engine factory's seam, consulted lazily so a substitute is built only
    when a request actually reaches it. A candidate that cannot run on this
    build (no credential, unimplemented) yields ``None`` and is skipped — it
    is never invoked and never counted as an attempt. With no credential the
    factory's pre-existing single-credential route is used unchanged.
    """
    from backend.ai.stt_control_plane import get_candidate
    from backend.services import stt_engine_factory

    candidate = get_candidate(candidate_id)
    if candidate is None:
        return None, "unknown_candidate"
    registration_ = _registration
    return stt_engine_factory.build_engine_with_credential(
        candidate,
        credential,
        language=registration_.language if registration_ else "",
        passes=registration_.passes if registration_ else 1,
    )


# ── The per-request attempt plan ──


class AttemptPlan:
    """ONE request's ordered provider × credential attempts, with its pin and
    failure memory.

    Created by :func:`attempt_plan` per media request and shared across the
    chunks of a divided recording, so a provider AND credential chosen
    mid-recording stay chosen (``_pinned``) and a combination that already
    failed THIS request is not retried within it (``_failed_credentials`` for a
    rejected credential, ``_failed_candidates`` for a provider that failed
    provider-wide) — while the SELECTED candidate keeps its first-attempt
    priority on the NEXT request.
    """

    __slots__ = ("_selected_id", "_selected_engine", "_fallback_ids", "_engines",
                 "_pinned_candidate", "_pinned_credential", "_failed_candidates",
                 "_failed_credentials", "_provisioned_id")

    def __init__(
        self,
        selected_id: str,
        selected_engine: Any,
        fallback_ids: tuple[str, ...],
        provisioned_credential_id: str = "",
    ) -> None:
        self._selected_id = selected_id
        self._selected_engine = selected_engine
        self._fallback_ids = fallback_ids
        self._engines: dict[tuple[str, str], Any] = {}
        self._pinned_candidate: str = ""
        self._pinned_credential: str = ""
        self._failed_candidates: set[str] = set()
        self._failed_credentials: set[tuple[str, str]] = set()
        self._provisioned_id = str(provisioned_credential_id or "")

    @property
    def selected_id(self) -> str:
        """The owner's selected candidate — the plan's FIRST attempt."""
        return self._selected_id

    @property
    def candidate_count(self) -> int:
        """Registered candidates this request may attempt (ceiling included)."""
        return 1 + len(self._fallback_ids)

    @staticmethod
    def _credential_id(credential: CredentialRecord | None) -> str:
        """The bounded, non-secret identifier of one pool entry (``""`` when none)."""
        return credential.credential_id if credential is not None else ""

    def _credential_entries(
        self, candidate_id: str,
    ) -> tuple[CredentialRecord | None, ...]:
        """This candidate's credentials, in the order ONE request should try them.

        The credential pool's own deterministic order, minus anything that
        already failed THIS request, with the credential that just worked pinned
        first — so a provider chosen mid-recording is not re-keyed for its later
        chunks for no reason. A provider whose pool was never loaded (the
        pre-existing single-credential route, and every legacy configuration)
        yields ONE credential-less entry, which preserves that route exactly.
        """
        from backend.services import stt_credential_pool as pool

        provider = _candidate_provider(candidate_id)
        records = pool.rotation_for(provider) if provider else ()
        if not records:
            return (None,)
        pinned = (
            self._pinned_credential
            if candidate_id == self._pinned_candidate
            else ""
        )
        ordered: list[CredentialRecord] = []
        for record in records:
            if record.credential_id == pinned:
                ordered.append(record)
        for record in records:
            if any(item.credential_id == record.credential_id for item in ordered):
                continue
            if (candidate_id, record.credential_id) in self._failed_credentials:
                continue
            ordered.append(record)
        return tuple(ordered)

    def _engine_for(
        self, candidate_id: str, credential: CredentialRecord | None,
    ) -> Any | None:
        """The engine for ONE (candidate, credential) pair, built at most once.

        The pair that was PROVISIONED reuses the engine the runtime already
        holds — the owner's own selection and the credential it was provisioned
        with — and every other pair is resolved through the one factory seam.
        ``None`` means the pair cannot run on this build and is simply skipped.
        """
        credential_id = self._credential_id(credential)
        key = (candidate_id, credential_id)
        if key in self._engines:
            return self._engines[key]
        engine: Any | None = None
        if candidate_id == self._selected_id and not credential_id:
            engine = self._selected_engine
        elif (
            candidate_id == self._selected_id
            and credential_id == self._provisioned_id
        ):
            engine = self._selected_engine
        if engine is None:
            engine, reason = _build_candidate_engine(candidate_id, credential)
            if engine is None:
                # Cached so one request asks the factory once per pair.
                self._engines[key] = None
                logger.warning(
                    "STT_FALLBACK_SKIPPED candidate=%s reason=%s",
                    candidate_id, reason,
                )
                return None
        self._engines[key] = engine
        return engine

    def _order(self) -> tuple[str, ...]:
        """Pinned first, then the selected candidate, then canonical fallbacks.

        Candidates that already failed provider-wide THIS request never
        reappear; everything else keeps the control plane's canonical order.
        """
        ordered: list[str] = []
        if self._pinned_candidate:
            ordered.append(self._pinned_candidate)
        for candidate_id in (self._selected_id, *self._fallback_ids):
            if candidate_id not in ordered and candidate_id not in self._failed_candidates:
                ordered.append(candidate_id)
        return tuple(ordered)

    def _pin(self, candidate_id: str, credential_id: str) -> None:
        self._pinned_candidate = candidate_id
        self._pinned_credential = credential_id

    async def run(
        self,
        audio: bytes,
        bound_s: float,
        run_engine: Callable[..., Awaitable[str]],
        *,
        request_id: str = "",
    ) -> str:
        """Transcribe ``audio`` through the ordered candidates and credentials.

        ``run_engine`` is the boundary's own attempt primitive — it receives
        the engine, the already-validated audio and the REMAINING budget of
        this unit, and it owns the awaited timeout and the attempt's trace.
        The budget is shared by every attempt, provider AND credential: a later
        attempt never gets a fresh one.

        Success returns the engine's own string, normalized by the caller.
        Failure returns nothing: either the selected provider's failure
        (propagated unchanged when no substitute actually ran) or the one
        exhaustion error (when it did).
        """
        # The boundary's failure type and the credential pool are resolved per
        # call: the boundary binds this module at import time, so the dependency
        # stays one-way.
        from backend.services import stt_credential_pool as pool
        from backend.services.media_service import MediaError

        deadline = _clock() + max(0.0, float(bound_s))
        attempts = 0
        providers_run = 0
        last_error: MediaError | None = None
        for candidate_id in self._order():
            if providers_run >= MAX_PROVIDER_ATTEMPTS or attempts >= MAX_TOTAL_ATTEMPTS:
                break
            # The provider-level gates run BEFORE any engine is built: a
            # substitute that is cooling down, or whose share of the budget is
            # already spent, is never even constructed.
            if providers_run and is_cooled_down(candidate_id):
                logger.info(
                    "STT_FALLBACK_SKIPPED candidate=%s reason=cooldown "
                    "attempts=%d", candidate_id, attempts,
                )
                continue
            remaining = deadline - _clock()
            if attempts and remaining < MIN_ATTEMPT_S:
                logger.warning(
                    "STT_FALLBACK_STOPPED reason=insufficient_budget "
                    "remaining_s=%.1f attempts=%d", remaining, attempts,
                )
                break
            provider = _candidate_provider(candidate_id)
            entries = self._credential_entries(candidate_id)
            # "A real pool exists" is asked of the provider's CONFIGURATION, not
            # of what happens to be cooling down: a provider configured with one
            # credential must keep its exact pre-pool behavior, and only a
            # provider whose pool was actually a pool may hand a rejected key to
            # the provider-level fallback.
            pooled = len(pool.credentials_for(provider)) > 1
            provider_run = False
            credentials_run = 0
            for index, credential in enumerate(entries):
                if attempts >= MAX_TOTAL_ATTEMPTS:
                    break
                remaining = deadline - _clock()
                if attempts and remaining < MIN_ATTEMPT_S:
                    logger.warning(
                        "STT_FALLBACK_STOPPED reason=insufficient_budget "
                        "remaining_s=%.1f attempts=%d", remaining, attempts,
                    )
                    break
                engine = self._engine_for(candidate_id, credential)
                if engine is None:
                    continue
                credential_id = self._credential_id(credential)
                if not provider_run:
                    provider_run = True
                    providers_run += 1
                attempts += 1
                credentials_run += 1
                logger.info(
                    "STT_FALLBACK_ATTEMPT candidate=%s index=%d ceiling=%d "
                    "budget_s=%.1f",
                    candidate_id, attempts, MAX_PROVIDER_ATTEMPTS, remaining,
                )
                if credential is not None:
                    logger.info(
                        "STT_CREDENTIAL_ATTEMPT credential=%s provider=%s "
                        "index=%d ceiling=%d",
                        credential_id, provider, credentials_run,
                        pool.MAX_CREDENTIAL_ATTEMPTS_PER_PROVIDER,
                    )
                try:
                    text = await run_engine(
                        engine, audio, remaining, request_id=request_id,
                    )
                except asyncio.CancelledError:
                    raise
                except MediaError as exc:
                    failure_class = pool.failure_class_of(exc)
                    credential_specific = (
                        credential is not None and pool.is_credential_specific(exc)
                    )
                    eligible = fallback_eligible(exc)
                    logger.warning(
                        "STT_FALLBACK_FAILURE candidate=%s attempt=%d "
                        "failure_class=%s eligible=%s",
                        candidate_id, attempts, failure_class, eligible,
                    )
                    last_error = exc
                    if credential_specific:
                        logger.warning(
                            "STT_CREDENTIAL_FAILURE credential=%s failure_class=%s",
                            credential_id, failure_class,
                        )
                        self._failed_credentials.add((candidate_id, credential_id))
                        pool.record_failure(credential_id, failure_class)
                        pool.mark_source_stale(provider)
                        if (
                            index + 1 < len(entries)
                            and credentials_run
                            < pool.MAX_CREDENTIAL_ATTEMPTS_PER_PROVIDER
                        ):
                            # Another credential of THIS provider is still
                            # untried and the credential ceiling has room: rotate
                            # inside the provider, and leave the provider's own
                            # health untouched.
                            continue
                        if pooled:
                            # A REAL pool exists and is now exhausted: hand the
                            # failure to the provider layer, which is exactly the
                            # transition this phase adds.
                            logger.warning(
                                "STT_CREDENTIAL_POOL_EXHAUSTED provider=%s "
                                "credentials=%d attempts=%d",
                                provider, len(pool.credentials_for(provider)),
                                attempts,
                            )
                            logger.warning(
                                "STT_FALLBACK_POOL_TO_PROVIDER from=%s attempts=%d",
                                candidate_id, attempts,
                            )
                            self._failed_candidates.add(candidate_id)
                            record_failure(candidate_id, failure_class)
                            break
                    if eligible:
                        self._failed_candidates.add(candidate_id)
                        record_failure(candidate_id, failure_class)
                        break
                    raise
                if credential is not None:
                    pool.record_success(credential_id)
                record_success(candidate_id)
                self._pin(candidate_id, credential_id)
                logger.info(
                    "STT_FALLBACK_SUCCESS candidate=%s attempt=%d chars=%d",
                    candidate_id, attempts,
                    len(text) if isinstance(text, str) else 0,
                )
                return text if isinstance(text, str) else ""
        if last_error is not None and attempts <= 1:
            # No substitute was runnable (single-candidate rotation, a single
            # configured credential, or every substitute skipped): the SELECTED
            # provider's own classified failure is the honest diagnosis, so it
            # propagates unchanged and this layer never rewrites a
            # single-provider failure's identity.
            raise last_error
        raise self._exhausted(attempts, last_error) from last_error

    def _exhausted(self, attempts: int, last_error: MediaError | None) -> MediaError:
        """The ONE controlled failure when no eligible candidate succeeded.

        Honest by contract: this says the PROVIDERS were exhausted — never
        that the audio itself was invalid. The last failure's bounded reason
        (already sanitized by the adapter that raised it) is preserved.
        """
        reason = ""
        if last_error is not None:
            from backend.services.media_service import bounded_reason

            reason = bounded_reason(last_error)
        message = (
            f"Speech-to-text failed on every eligible provider "
            f"({attempts} attempt{'s' if attempts != 1 else ''})."
        )
        if reason:
            message = f"{message} Last failure: {reason}"
        from backend.services.media_service import (
            MEDIA_STAGE_STT_EXHAUSTED,
            MediaError,
        )

        error = MediaError(message, stage=MEDIA_STAGE_STT_EXHAUSTED)
        error.failure_class = STT_FALLBACK_EXHAUSTED
        logger.warning(
            "STT_FALLBACK_EXHAUSTED attempts=%d last_failure_class=%s",
            attempts, failure_class_of(last_error) if last_error else "-",
        )
        return error


def attempt_plan(selected_engine: Any) -> AttemptPlan | None:
    """The request's attempt plan for the PROVISIONED selected engine.

    ``None`` when fallback is inactive (no registered rotation — the legacy
    and unconfigured states) or no engine is provisioned: the boundary keeps
    its exact fail-closed single-engine behavior in both cases. The plan's
    first candidate is always the provisioned engine itself — the owner's
    selection, never a substitute chosen by this layer.
    """
    registration_ = _registration
    if registration_ is None or selected_engine is None:
        return None
    return AttemptPlan(
        registration_.active_id,
        selected_engine,
        registration_.fallback_ids,
        registration_.provisioned_credential_id,
    )
