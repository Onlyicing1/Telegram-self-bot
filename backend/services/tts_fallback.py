"""TTS provider health + bounded automatic fallback — the ordered attempt layer,
with CREDENTIAL rotation inside one provider.

This is the EXECUTION half of TTS resilience, beside the seams it consumes and
never duplicates:

    the control plane (``backend/ai/tts_control_plane.py``) registers the
    providers/models/voices and owns the owner's persisted selection — including
    the canonical, deterministic provider ORDER;
        ↓
    this module: health state, failure classification, cooldown and the bounded
    attempt loop over THAT order — and, inside one provider, over the credential
    pool of that provider;
        ↓
    the credential pool (``backend/services/tts_credential_pool.py``) owns the
    credential order, credential cooldown and the credential-vs-provider
    classification, and the secret boundary behind it
    (``backend/ai/credential_source.py``) owns where a credential comes from;
        ↓
    the engine factory (``backend/services/tts_engine_factory.py``) is the ONE
    provider → engine seam, consulted lazily per provider and per credential;
        ↓
    the TTS boundary (``backend/services/tts_service.py``) drives one attempt
    through its ONE awaited provider call.

What this layer decides: which provider to try next, in what order, which of its
credentials to try first, whether a provider is temporarily unhealthy, whether a
failure is fallback-eligible, and when to stop. What it never does: talk to a
provider itself, touch a Telegram object, rewrite the owner's selection, or
replace the boundary — the selected provider stays the FIRST attempt of every
request, and a fallback (provider or credential) is an internal substitution for
ONE request only.

The two resilience axes stay apart, because they answer different questions:

  * a CREDENTIAL failure (a rejected, revoked, spent or rate-limited key) means
    this deployment has another key for a provider that is working — it rotates
    inside the provider and never marks the provider unhealthy;
  * a PROVIDER failure (5xx, timeout, transport, deadline) means the provider is
    the problem — the rest of its credentials would fail the same way, so the
    pool is NOT burned and the provider-level fallback runs.

Selected-provider semantics: the configured provider is always attempted first,
on every request, whatever its health — the owner's preference must not silently
lose priority because it failed once. Cooldown prunes only the FALLBACK rotation.
A successful request restores health immediately. Nothing here touches the
Telegram UI state or the persisted selection.

Failure classification is fail-closed: only failures that are transient BY NATURE
(provider timeout, transport, 5xx, rate limit, a spent operation deadline) or
that the adapter itself marked ``retryable`` allow another provider to be tried;
deterministic failures (rejected credential, an unsupported model or voice, a
malformed or empty response, an input the provider refused, a provider rejection)
and anything the boundary cannot classify — including programming errors —
propagate unchanged instead of cascading through every provider. A configuration
error therefore fails CLOSED with its own identity rather than being hidden behind
a substitute.

Bounds: at most :data:`MAX_PROVIDER_ATTEMPTS` provider attempts and at most
:data:`MAX_TOTAL_ATTEMPTS` attempts in total per synthesis, each sharing the ONE
budget the boundary hands in — a later attempt receives only the REMAINING budget,
never a fresh one. Credential rotation therefore cannot multiply the pre-existing
worst case by more than its documented factor, and a provider's own credential
ceiling (``tts_credential_pool.MAX_CREDENTIAL_ATTEMPTS_PER_PROVIDER``) bounds it
further.

Health state is process-local and deliberately NOT persisted: it is runtime
posture, not configuration, and a restart honestly resets it.

Zero context: the only inputs on this path are provider and credential
identifiers, the resolved selection and the already-validated text. No chat id,
message id, sender, caption, filename, reply text, AI history or memory can enter
— the module has no parameter that could carry one.

Traces are structured, bounded and content-free: provider ids, credential ids,
attempt indices, failure classes, elapsed time and cooldown durations — never a
credential VALUE, the spoken text or a Telegram identifier.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from backend.ai.tts_control_plane import TtsSelection, canonical_order, get_provider

logger = logging.getLogger(__name__)

#: The failure class of the FINAL error when every eligible provider failed.
#: Reported as the ``failure_class`` trace field so an exhausted rotation is
#: diagnosable as itself — never mistaken for a single provider's failure.
TTS_FALLBACK_EXHAUSTED = "fallback_exhausted"

#: Hard ceiling on provider attempts for ONE synthesis. The selected provider plus
#: at most two substitutes: reliability, not an unbounded provider sweep. The
#: boundary's awaited timeout remains the wall-clock ceiling above this.
MAX_PROVIDER_ATTEMPTS = 3

#: Hard ceiling on ALL attempts of one synthesis — providers and credential
#: rotations together. Twice the provider ceiling, deliberately: credential
#: rotation may at most DOUBLE the pre-existing worst case, so a pool can never
#: turn one request into an unbounded workload.
MAX_TOTAL_ATTEMPTS = 2 * MAX_PROVIDER_ATTEMPTS

#: A substitute attempt starts only with at least this much of the budget left —
#: the same floor the provider adapters use for their own bounded retries, so a
#: starved attempt is never started just to time out.
MIN_ATTEMPT_S = 8.0

#: Cooldown for a provider that failed: bounded doubling from this base, capped at
#: :data:`COOLDOWN_MAX_S`.
COOLDOWN_BASE_S = 60.0
COOLDOWN_MAX_S = 600.0

#: The failure classes that are transient BY NATURE when the adapter did not
#: attach its own ``retryable`` verdict. Everything else — credentials, an
#: unsupported model or voice, a malformed or empty response, a refused input, a
#: provider rejection, and anything unrecognized — is fail-closed.
_FALLBACK_ELIGIBLE_CLASSES = frozenset({
    "timeout",
    "transport",
    "server",
    "rate_limit",
    "deadline",
})


def fallback_eligible(error: BaseException) -> bool:
    """Whether ``error`` may allow the NEXT provider to be tried.

    Fail-closed by construction: only a ``TtsError`` can be eligible, the
    adapter's own ``retryable`` verdict wins when present, and a recognized
    transient class is the last resort. A non-``TtsError`` (a programming error
    surfacing through the adapter seam) is never eligible and never hidden behind
    fallback.
    """
    from backend.services.tts_service import TtsError

    if not isinstance(error, TtsError):
        return False
    retryable = getattr(error, "retryable", None)
    if retryable is not None:
        return bool(retryable)
    return str(getattr(error, "failure_class", "") or "") in _FALLBACK_ELIGIBLE_CLASSES


def failure_class_of(error: BaseException) -> str:
    """The bounded failure-class token of a failed attempt (never empty)."""
    value = str(getattr(error, "failure_class", "") or "")
    return value or "unknown"


# ── Provider health (process-local, never persisted) ──


@dataclass
class _Health:
    """Bounded failure posture of one provider."""

    failures: int = 0
    cooldown_until: float = 0.0
    last_failure_class: str = ""


_health: dict[str, _Health] = {}


def _clock() -> float:
    return time.monotonic()


def is_cooled_down(provider: str) -> bool:
    """True while the provider is serving its post-failure cooldown."""
    entry = _health.get(str(provider or ""))
    return bool(entry and entry.cooldown_until > _clock())


def record_failure(provider: str, failure_class: str) -> float:
    """Mark one provider failure; return the cooldown seconds now in force."""
    resolved = str(provider or "")
    entry = _health.setdefault(resolved, _Health())
    entry.failures += 1
    entry.last_failure_class = str(failure_class or "")
    cooldown_s = min(COOLDOWN_BASE_S * (2 ** (entry.failures - 1)), COOLDOWN_MAX_S)
    entry.cooldown_until = _clock() + cooldown_s
    logger.info(
        "TTS_FALLBACK_COOLDOWN provider=%s failures=%d cooldown_s=%.0f failure_class=%s",
        resolved, entry.failures, cooldown_s, entry.last_failure_class or "unknown",
    )
    return cooldown_s


def record_success(provider: str) -> None:
    """A completed request restores the provider's health immediately."""
    _health.pop(str(provider or ""), None)


def reset_health() -> None:
    """Drop every health record (tests and explicit resets)."""
    _health.clear()


# ── The registered rotation (from the control plane, at settings-apply time) ──


@dataclass(frozen=True)
class _Registration:
    """The deterministic fallback rotation derived from the owner's selection."""

    selected: TtsSelection
    providers: tuple[str, ...]
    provisioned_credential_id: str = ""


_registration: _Registration | None = None


def register_plan(
    selection: TtsSelection, *, provisioned_credential_id: str = "",
) -> None:
    """Adopt the rotation implied by the owner's resolved selection.

    Called by the boundary's ONE settings-apply entry point, so the rotation is
    always derived from the SAME resolved selection that provisioned the engine —
    never from a second read of the store. The selected provider is first and the
    tail is the registry's canonical order over the providers this build can
    actually execute, so an unimplemented provider is never armed as a substitute.
    """
    global _registration
    providers = canonical_order(selection.provider)
    _registration = _Registration(
        selected=selection,
        providers=providers,
        provisioned_credential_id=str(provisioned_credential_id or ""),
    )
    logger.info(
        "TTS_FALLBACK_PLAN state=active selected=%s fallback_providers=%d",
        selection.provider, max(0, len(providers) - 1),
    )


def registration() -> _Registration | None:
    """The registered rotation, or ``None`` when fallback is inactive."""
    return _registration


def clear_registration() -> None:
    """Drop the registered rotation (tests and explicit resets)."""
    global _registration
    _registration = None


def _substitute_selection(provider: str) -> TtsSelection | None:
    """The selection a SUBSTITUTE provider is attempted with.

    Derived from the registry alone: the provider's own default model and that
    model's default voice. A substitute is therefore never attempted with the
    selected provider's model, and no model/voice can be invented here.
    """
    entry = get_provider(provider)
    if entry is None or not entry.implemented:
        return None
    model = entry.model(entry.default_model_id)
    if model is None or not model.voices:
        return None
    return TtsSelection(provider=provider, model=model.model_id, voice=model.voices[0].voice_id)


# ── The per-request attempt plan ──


class AttemptPlan:
    """ONE request's ordered provider × credential attempts, with its pin and
    failure memory.

    Created per synthesis. A provider that already failed provider-wide THIS
    request is not retried within it, and a credential that already failed THIS
    request is not retried either — while the SELECTED provider keeps its
    first-attempt priority on the NEXT request.
    """

    __slots__ = ("_selected", "_selected_engine", "_providers", "_engines",
                 "_pinned_provider", "_pinned_credential", "_failed_providers",
                 "_failed_credentials", "_provisioned_id")

    def __init__(
        self,
        selected: TtsSelection,
        selected_engine: Any,
        providers: tuple[str, ...],
        provisioned_credential_id: str = "",
    ) -> None:
        self._selected = selected
        self._selected_engine = selected_engine
        self._providers = providers
        self._engines: dict[tuple[str, str, str], Any] = {}
        self._pinned_provider: str = ""
        self._pinned_credential: str = ""
        self._failed_providers: set[str] = set()
        self._failed_credentials: set[tuple[str, str]] = set()
        self._provisioned_id = str(provisioned_credential_id or "")

    # ── identity helpers ──

    @property
    def selected_provider(self) -> str:
        """The owner's selected provider — the plan's FIRST attempt."""
        return self._selected.provider

    @staticmethod
    def _credential_id(credential: Any) -> str:
        return credential.credential_id if credential is not None else ""

    def _credentials(self, provider: str) -> tuple[Any, ...]:
        """This provider's credentials, in the order ONE request should try them.

        A provider whose pool was never loaded yields ONE credential-less entry,
        which preserves the pre-existing single-credential route exactly and lets
        the adapter resolve its own environment credential.
        """
        from backend.services import tts_credential_pool as pool

        if not pool.is_configured(provider):
            return (None,)
        records = pool.rotation_for(provider)
        if not records:
            return (None,)
        pinned = (
            self._pinned_credential if provider == self._pinned_provider else ""
        )
        ordered: list[Any] = []
        for record in records:
            if record.credential_id == pinned:
                ordered.append(record)
        for record in records:
            if any(item.credential_id == record.credential_id for item in ordered):
                continue
            if (provider, record.credential_id) in self._failed_credentials:
                continue
            ordered.append(record)
        return tuple(ordered)

    def _order(self) -> tuple[str, ...]:
        """Pinned first, then the selected provider, then the canonical tail."""
        ordered: list[str] = []
        if self._pinned_provider:
            ordered.append(self._pinned_provider)
        for provider in self._providers:
            if provider not in ordered and provider not in self._failed_providers:
                ordered.append(provider)
        return tuple(ordered)

    def _engine_for(self, provider: str, credential: Any) -> Any | None:
        """The engine for ONE (provider, credential) pair, built at most once.

        The pair that was PROVISIONED reuses the engine the runtime already holds
        — the owner's own selection and the credential it was provisioned with —
        and every other pair is resolved through the one factory seam. ``None``
        means the pair cannot run on this build and is simply skipped.
        """
        credential_id = self._credential_id(credential)
        selection = (
            self._selected if provider == self._selected.provider
            else _substitute_selection(provider)
        )
        if selection is None:
            return None
        key = (provider, selection.model, credential_id)
        if key in self._engines:
            return self._engines[key]
        engine: Any | None = None
        # The provisioned engine is reused for the credential it was built from:
        # either the pool was never loaded (a credential-less attempt, which is
        # the adapter's own environment resolution) or the attempt's credential id
        # IS the recorded provisioning one. Any other pair is resolved through the
        # one factory seam.
        if (
            provider == self._selected.provider
            and credential_id in ("", self._provisioned_id)
        ):
            engine = self._selected_engine
        if engine is None:
            from backend.services import tts_engine_factory

            engine, reason = tts_engine_factory.build_engine_for(
                selection.provider, selection.model, selection.voice, credential,
            )
            if engine is None:
                self._engines[key] = None
                logger.warning(
                    "TTS_FALLBACK_SKIPPED provider=%s reason=%s", provider, reason,
                )
                return None
        self._engines[key] = engine
        return engine

    async def run(
        self,
        text: str,
        bound_s: float,
        run_engine: Callable[..., Awaitable[bytes]],
        *,
        request_id: str = "",
    ) -> tuple[Any, bytes]:
        """Synthesize ``text`` through the ordered providers and credentials.

        ``run_engine`` is the boundary's own attempt primitive — it receives the
        engine, the already-validated text and the REMAINING budget, and it owns
        the awaited timeout and the attempt's trace. The budget is shared by every
        attempt, provider AND credential: a later attempt never gets a fresh one.

        Success returns ``(producing engine, audio)`` so the caller reports the
        provider/model/voice that ACTUALLY produced the result. Failure returns
        nothing: either the selected provider's failure (propagated unchanged when
        no substitute actually ran) or the one exhaustion error.
        """
        from backend.services import tts_credential_pool as pool
        from backend.services.tts_service import TtsError

        deadline = _clock() + max(0.0, float(bound_s))
        attempts = 0
        providers_run = 0
        last_error: TtsError | None = None
        for provider in self._order():
            if providers_run >= MAX_PROVIDER_ATTEMPTS or attempts >= MAX_TOTAL_ATTEMPTS:
                break
            if providers_run and is_cooled_down(provider):
                logger.info(
                    "TTS_FALLBACK_SKIPPED provider=%s reason=cooldown attempts=%d",
                    provider, attempts,
                )
                continue
            remaining = deadline - _clock()
            if attempts and remaining < MIN_ATTEMPT_S:
                logger.warning(
                    "TTS_FALLBACK_STOPPED reason=insufficient_budget remaining_s=%.1f "
                    "attempts=%d", remaining, attempts,
                )
                break
            entries = self._credentials(provider)
            # "A real pool exists" is asked of the provider's CONFIGURATION, not of
            # what happens to be cooling down: a provider configured with one
            # credential must keep its exact pre-pool behavior, and only a provider
            # whose pool was actually a pool may hand a rejected key to the
            # provider-level fallback.
            pooled = len(pool.credentials_for(provider)) > 1
            provider_run = False
            credentials_run = 0
            for index, credential in enumerate(entries):
                if attempts >= MAX_TOTAL_ATTEMPTS:
                    break
                remaining = deadline - _clock()
                if attempts and remaining < MIN_ATTEMPT_S:
                    logger.warning(
                        "TTS_FALLBACK_STOPPED reason=insufficient_budget "
                        "remaining_s=%.1f attempts=%d", remaining, attempts,
                    )
                    break
                engine = self._engine_for(provider, credential)
                if engine is None:
                    continue
                credential_id = self._credential_id(credential)
                if not provider_run:
                    provider_run = True
                    providers_run += 1
                attempts += 1
                credentials_run += 1
                logger.info(
                    "TTS_FALLBACK_ATTEMPT provider=%s index=%d ceiling=%d budget_s=%.1f",
                    provider, attempts, MAX_PROVIDER_ATTEMPTS, remaining,
                )
                if credential_id:
                    logger.info(
                        "TTS_CREDENTIAL_ATTEMPT credential=%s provider=%s index=%d "
                        "ceiling=%d", credential_id, provider, credentials_run,
                        pool.MAX_CREDENTIAL_ATTEMPTS_PER_PROVIDER,
                    )
                try:
                    audio = await run_engine(
                        engine, text, remaining, request_id=request_id,
                    )
                except asyncio.CancelledError:
                    raise
                except TtsError as exc:
                    failure_class = pool.failure_class_of(exc)
                    credential_specific = (
                        credential is not None and pool.is_credential_specific(exc)
                    )
                    eligible = fallback_eligible(exc)
                    logger.warning(
                        "TTS_FALLBACK_FAILURE provider=%s attempt=%d failure_class=%s "
                        "eligible=%s", provider, attempts, failure_class, eligible,
                    )
                    last_error = exc
                    if credential_specific:
                        logger.warning(
                            "TTS_CREDENTIAL_FAILURE credential=%s failure_class=%s",
                            credential_id, failure_class,
                        )
                        self._failed_credentials.add((provider, credential_id))
                        pool.record_failure(credential_id, failure_class)
                        pool.mark_source_stale(provider)
                        if (
                            index + 1 < len(entries)
                            and credentials_run < pool.MAX_CREDENTIAL_ATTEMPTS_PER_PROVIDER
                        ):
                            # Another credential of THIS provider is still untried
                            # and the credential ceiling has room: rotate inside the
                            # provider, and leave the provider's own health untouched.
                            continue
                        if pooled:
                            # A REAL pool exists and is now exhausted: hand the
                            # failure to the provider layer.
                            logger.warning(
                                "TTS_CREDENTIAL_POOL_EXHAUSTED provider=%s credentials=%d "
                                "attempts=%d", provider, len(pool.credentials_for(provider)),
                                attempts,
                            )
                            self._failed_providers.add(provider)
                            record_failure(provider, failure_class)
                            break
                    if eligible:
                        self._failed_providers.add(provider)
                        record_failure(provider, failure_class)
                        break
                    raise
                if credential_id:
                    pool.record_success(credential_id)
                record_success(provider)
                self._pin(provider, credential_id)
                logger.info(
                    "TTS_FALLBACK_SUCCESS provider=%s attempt=%d bytes=%d",
                    provider, attempts, len(audio) if isinstance(audio, (bytes, bytearray)) else 0,
                )
                return engine, bytes(audio) if isinstance(audio, (bytes, bytearray)) else b""
        if last_error is not None and attempts <= 1:
            # No substitute was runnable: the SELECTED provider's own classified
            # failure is the honest diagnosis, so it propagates unchanged and this
            # layer never rewrites a single-provider failure's identity.
            raise last_error
        raise self._exhausted(attempts, last_error) from last_error

    def _pin(self, provider: str, credential_id: str) -> None:
        self._pinned_provider = provider
        self._pinned_credential = credential_id

    def _exhausted(self, attempts: int, last_error: Any) -> Any:
        """The ONE controlled failure when no eligible provider succeeded.

        Honest by contract: this says the PROVIDERS were exhausted — never that
        the text itself was invalid. The last failure's bounded reason (already
        sanitized by the adapter that raised it) is preserved.
        """
        from backend.services.tts_service import (
            FAILURE_FALLBACK_EXHAUSTED,
            TTS_STAGE_PROVIDER,
            TtsError,
        )

        reason = str(getattr(last_error, "args", ("",))[0] or "") if last_error else ""
        message = (
            f"Speech synthesis failed on every eligible provider "
            f"({attempts} attempt{'s' if attempts != 1 else ''})."
        )
        if reason:
            message = f"{message} Last failure: {reason}"
        logger.warning(
            "TTS_FALLBACK_EXHAUSTED attempts=%d last_failure_class=%s",
            attempts, failure_class_of(last_error) if last_error else "-",
        )
        return TtsError(
            message,
            stage=TTS_STAGE_PROVIDER,
            failure_class=FAILURE_FALLBACK_EXHAUSTED,
            retryable=False,
        )


def attempt_plan(selected_engine: Any) -> AttemptPlan | None:
    """The request's attempt plan for the PROVISIONED selected engine.

    ``None`` when fallback is inactive (no registered rotation) or no engine is
    provisioned: the boundary keeps its exact fail-closed single-engine behavior
    in both cases. The plan's first provider is always the provisioned engine
    itself — the owner's selection, never a substitute chosen by this layer.
    """
    registration_ = _registration
    if registration_ is None or selected_engine is None:
        return None
    return AttemptPlan(
        registration_.selected,
        selected_engine,
        registration_.providers,
        registration_.provisioned_credential_id,
    )
