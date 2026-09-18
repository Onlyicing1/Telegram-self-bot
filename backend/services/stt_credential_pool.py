"""STT credential pool — ordering, health and classification for the credentials
of ONE provider.

This is the CREDENTIAL half of STT resilience. It sits under the provider
fallback layer (``backend/services/stt_fallback.py``, which owns which PROVIDER is
tried) and above the secret backend boundary
(``backend/ai/credential_source.py``, which owns where a credential comes from):

    provider fallback BETWEEN providers        stt_fallback   (unchanged)
        ↓
    credential rotation INSIDE one provider    THIS module
        ↓
    where a credential comes from              credential_source
        ↓
    one provider adapter, one attempt          the existing engine seam

What this module owns:

  * the bounded, DETERMINISTIC order of a provider's credentials — explicit
    priority first, then the source order (the deployment's environment
    credential is index 0, so an installation with one key behaves exactly as it
    did before this phase), with the source's own order as the stable
    tie-breaker. Nothing here rotates randomly or reshuffles per request, and
    there is no quality ranking: a pool is ordered, not scored;
  * CREDENTIAL health, which is deliberately SEPARATE from provider health. A
    credential that fails takes a bounded cooldown of its own; the provider stays
    healthy and keeps serving through its other credentials, and only a provider
    whose usable credentials are all gone reaches the provider-level fallback;
  * the classification that decides whether a failure is about the CREDENTIAL
    (rotate) or about the PROVIDER (do not burn the rest of the pool). It reuses
    the adapters' existing, bounded vocabulary and their already-attached
    ``http_status``: 401/403/429 and the credential classes rotate, while a 5xx,
    a timeout or a transport failure is a provider condition and is handed
    straight to the provider layer, exactly as before.

What this module never does: talk to a provider, read a transcript, touch the
owner's selection, or hold a secret anywhere but in the record it was handed. No
function here logs a secret: the only credential identifier in a trace is the
record's ``credential_id``, which the source guarantees is non-secret (an
environment VARIABLE name, or an id the owner chose), and a failure reason is
whatever the adapter already sanitized.

Health is process-local posture, never configuration: it is not persisted, and a
restart honestly resets it.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Iterable

from backend.ai.credential_source import CredentialRecord, SOURCE_ENV, SOURCE_VAULT

logger = logging.getLogger(__name__)

#: Hard ceiling on credential attempts for ONE provider inside ONE transcription
#: unit (one chunk, or one single-piece audio). The provider may be attempted at
#: most this many times, whatever the pool contains, so a large pool can never
#: become an unbounded provider sweep.
MAX_CREDENTIAL_ATTEMPTS_PER_PROVIDER = 3

#: Cooldown of a credential that failed: bounded doubling from this base, capped
#: at :data:`COOLDOWN_MAX_S`. Shorter at the base than the provider cooldown on
#: purpose — a spent per-key quota is usually the shortest-lived condition in the
#: pool, and a credential that recovers early is picked up again on its own.
COOLDOWN_BASE_S = 60.0
COOLDOWN_MAX_S = 600.0

#: The failure classes that mean THIS credential is unusable — the adapters' own
#: closed tokens, mirrored rather than invented. ``missing_credential`` is here
#: because a pool entry whose secret the backend could not decrypt is a
#: credential problem, and ``rate_limit`` because the services' quotas are
#: per-key: rotating is the FIRST, cheapest response, and the per-provider
#: ceiling keeps a provider-wide 429 from becoming a credential sweep.
_CREDENTIAL_SPECIFIC_CLASSES = frozenset({
    "auth",
    "forbidden",
    "missing_credential",
    "rate_limit",
    "quota_exceeded",
})

#: The HTTP statuses that identify the CREDENTIAL regardless of the adapter's own
#: class vocabulary — the Gemini adapter reports a rejection as ``http_rejection``
#: with its status attached, and the status is the honest classifier there.
_CREDENTIAL_SPECIFIC_STATUSES = frozenset({401, 403, 429})

#: provider → the environment variable names that provider's adapter already
#: declares, as the SOURCE of the deployment's single credential. Imported from
#: the adapters so there is exactly ONE declaration of each name; the Gemini
#: names are resolved lazily beside the adapter that owns them, following the
#: same pattern the engine factory uses.
_ENV_VAR_NAMES: dict[str, tuple[str, ...]] = {}


def _env_var_names(provider: str) -> tuple[str, ...]:
    """The provider's OWN declared credential variables (never a sweep)."""
    resolved = str(provider or "").strip()
    if not resolved:
        return ()
    names = _ENV_VAR_NAMES.get(resolved)
    if names is not None:
        return names
    if resolved == "gemini":
        from backend.services.gemini_media_engine import API_KEY_ENV_VARS

        return tuple(API_KEY_ENV_VARS)
    return ()


def _register_adapter_env_names() -> None:
    """Learn the adapter providers' variable names, once, from the adapters."""
    from backend.services import groq_stt_engine, speechmatics_stt_engine

    _ENV_VAR_NAMES.setdefault(
        groq_stt_engine.PROVIDER_NAME, tuple(groq_stt_engine.API_KEY_ENV_VARS)
    )
    _ENV_VAR_NAMES.setdefault(
        speechmatics_stt_engine.PROVIDER_NAME,
        tuple(speechmatics_stt_engine.API_KEY_ENV_VARS),
    )


# ── Loading (bounded, explicit, never fatal) ──


def registered_providers() -> tuple[str, ...]:
    """Every provider the control plane's registry can select, in canonical order.

    Derived from the registry — never from a hard-coded list here and never from
    the environment — so the pool can only ever hold credentials for a provider
    the runtime can actually execute.
    """
    from backend.ai.stt_control_plane import all_candidates

    seen: list[str] = []
    for candidate in all_candidates():
        if candidate.provider not in seen:
            seen.append(candidate.provider)
    return tuple(seen)


async def prepare(providers: Iterable[str] | None = None) -> dict[str, int]:
    """Load the credential pools the persisted selection may draw on.

    Called by the ONE settings-apply entry point, so the pools are always read
    from the SAME configuration that provisions the engines: at startup (so a
    persisted selection has its credentials from the first transcription) and
    immediately after a Telegram settings change. Bounded and never fatal — a
    secret backend that is missing, slow or refusing contributes nothing, and the
    provider keeps its environment credential.
    """
    _register_adapter_env_names()
    targets = tuple(providers) if providers is not None else registered_providers()
    counts: dict[str, int] = {}
    for provider in targets:
        records = await _load(provider)
        counts[provider] = len(records)
    return counts


async def _load(provider: str) -> tuple[CredentialRecord, ...]:
    from backend.ai import credential_source

    return await credential_source.load(str(provider or ""), _env_var_names(provider))


def reset() -> None:
    """Drop every credential snapshot and health record (tests, explicit resets)."""
    from backend.ai import credential_source

    credential_source.reset()
    _health.clear()


# ── The ordered pool ──


def is_configured(provider: str) -> bool:
    """True when the secret backend has answered for ``provider``.

    Before the first load the runtime keeps its pre-existing single-credential
    route; after a load — even an empty one — the pool is authoritative and the
    rotation uses it.
    """
    from backend.ai import credential_source

    return credential_source.is_loaded(str(provider or ""))


def credentials_for(provider: str) -> tuple[CredentialRecord, ...]:
    """The provider's credentials in deterministic configuration order.

    Health-independent, so this is the order PROVISIONING uses and the order the
    runtime falls back to when nothing is cooling down.
    """
    from backend.ai import credential_source

    return credential_source.cached(str(provider or "")) or ()


def first_for(provider: str) -> CredentialRecord | None:
    """The credential the provider is provisioned with (its first, or ``None``)."""
    records = credentials_for(provider)
    return records[0] if records else None


def rotation_for(provider: str) -> tuple[CredentialRecord, ...]:
    """The provider's credentials in the order ONE request should try them.

    A credential serving its own cooldown is SKIPPED — that is the whole point of
    tracking credential health separately from provider health, so a spent key
    does not cost every later request an attempt. The one exception is deliberate
    and mirrors the provider layer's own rule: when EVERY credential of the
    provider is cooling down, the pool is returned unchanged, because refusing to
    attempt the provider at all would turn a temporary credential condition into
    a guaranteed media failure. Order within each group is preserved, so this is a
    stable reordering, never a rotation.
    """
    records = credentials_for(provider)
    if not records:
        return ()
    healthy = tuple(
        record for record in records if not is_cooled_down(record.credential_id)
    )
    return healthy or records


# ── Classification: credential problem vs provider problem ──


def failure_class_of(error: BaseException) -> str:
    """The bounded failure-class token of a failed attempt (never empty)."""
    from backend.services.media_service import MEDIA_STAGE_STT_TIMEOUT

    failure_class = str(getattr(error, "failure_class", "") or "")
    if failure_class:
        return failure_class
    if getattr(error, "stage", "") == MEDIA_STAGE_STT_TIMEOUT:
        return "timeout"
    return "unknown"


def is_credential_specific(error: BaseException) -> bool:
    """Whether ``error`` says THIS credential is unusable (bounded ``failure_class``, then ``http_status``).

    Fail-closed by construction: anything the boundary cannot classify — a
    programming error, an unclassified ``MediaError``, a provider-wide 5xx, a
    timeout, a transport failure — is NOT credential-specific, so the rest of the
    pool is never burned on a provider outage and only the provider layer reacts.
    """
    from backend.services.media_service import MediaError

    if not isinstance(error, MediaError):
        return False
    status = int(getattr(error, "http_status", 0) or 0)
    if status in _CREDENTIAL_SPECIFIC_STATUSES:
        return True
    return failure_class_of(error) in _CREDENTIAL_SPECIFIC_CLASSES


# ── Credential health (process-local, never persisted) ──


@dataclass
class CredentialHealth:
    """The bounded runtime metadata of ONE credential — never its secret.

    This is the metadata a credential needs and nothing more: how many times it
    failed, the class of the last failure, when its cooldown ends and when it last
    worked. Identity, enabled state, priority and order come from the source
    (the owner's configuration); quota exhaustion is expressed as the cooldown a
    credential-specific rate-limit failure produces.
    """

    credential_id: str
    failures: int = 0
    cooldown_until: float = 0.0
    last_failure_class: str = ""
    last_success_at: float = 0.0


_health: dict[str, CredentialHealth] = {}


def _clock() -> float:
    return time.monotonic()


def is_cooled_down(credential_id: str) -> bool:
    """True while the credential is serving its post-failure cooldown."""
    entry = _health.get(str(credential_id or ""))
    return bool(entry and entry.cooldown_until > _clock())


def credential_health(credential_id: str) -> CredentialHealth:
    """The credential's runtime metadata, or a zeroed record when never seen."""
    resolved = str(credential_id or "")
    entry = _health.get(resolved)
    return entry if entry is not None else CredentialHealth(credential_id=resolved)


def record_failure(credential_id: str, failure_class: str) -> float:
    """Mark ONE credential failure; return the cooldown seconds now in force."""
    resolved = str(credential_id or "")
    entry = _health.setdefault(resolved, CredentialHealth(credential_id=resolved))
    entry.failures += 1
    entry.last_failure_class = str(failure_class or "")
    cooldown_s = min(COOLDOWN_BASE_S * (2 ** (entry.failures - 1)), COOLDOWN_MAX_S)
    entry.cooldown_until = _clock() + cooldown_s
    logger.info(
        "STT_CREDENTIAL_COOLDOWN credential=%s failures=%d cooldown_s=%.0f "
        "failure_class=%s",
        resolved, entry.failures, cooldown_s, entry.last_failure_class or "unknown",
    )
    return cooldown_s


def record_success(credential_id: str) -> None:
    """A completed request restores the credential's health immediately."""
    resolved = str(credential_id or "")
    entry = _health.setdefault(resolved, CredentialHealth(credential_id=resolved))
    entry.failures = 0
    entry.cooldown_until = 0.0
    entry.last_failure_class = ""
    entry.last_success_at = _clock()
    logger.info("STT_CREDENTIAL_SUCCESS credential=%s", resolved)


def mark_source_stale(provider: str) -> None:
    """Tell the secret backend a credential of ``provider`` failed.

    The backend keeps serving its last snapshot (a failure must never reduce the
    runtime's usable credentials) and reloads it at the next settings apply, so a
    revoked credential is refreshed without an in-request database read.
    """
    from backend.ai import credential_source

    credential_source.mark_stale(str(provider or ""))


def describe(provider: str) -> str:
    """A bounded, secret-free description of a pool, for ONE trace field."""
    records = credentials_for(provider)
    if not records:
        return "unconfigured"
    parts = []
    for record in records:
        source = SOURCE_ENV if record.is_env else SOURCE_VAULT
        state = "cooling" if is_cooled_down(record.credential_id) else "ready"
        parts.append(f"{record.credential_id}({source},{state})")
    return ";".join(parts)
