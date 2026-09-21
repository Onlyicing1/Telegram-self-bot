"""TTS credential pool — ordering, health and classification for the credentials
of ONE speech-synthesis provider.

This is the CREDENTIAL half of TTS resilience. It sits under the provider
fallback layer (``backend/services/tts_fallback.py``, which owns which PROVIDER is
tried) and above the secret backend boundary
(``backend/ai/credential_source.py``, which owns where a credential comes from):

    provider fallback BETWEEN providers     tts_fallback
        ↓
    credential rotation INSIDE one provider THIS module
        ↓
    where a credential comes from           credential_source
        ↓
    one provider adapter, one attempt       the TTS engine factory

The credential STORE is NOT re-implemented here: this module is the TTS face of
the SAME boundary the Speech-to-Text pool uses — the same ``credential_source``,
the same deployment-environment-first precedence and the same
``api_credential_pool`` Vault RPC — so there is exactly ONE secret architecture
and ONE place a key can come from. What is TTS-specific is only the failure
vocabulary: a speech-synthesis failure is classified against the TTS boundary's
own closed taxonomy (``backend/services/tts_service.py``), never against the
transcription adapters'.

What this module owns:

  * the bounded, DETERMINISTIC order of a provider's credentials — the
    deployment's environment credential is index 0, so an installation with one
    key behaves exactly as it did before this phase, and additional credentials
    come from the Vault in the owner's explicit priority order. Nothing rotates
    randomly and there is no quality ranking: a pool is ordered, not scored;
  * CREDENTIAL health, deliberately separate from provider health. A credential
    that fails takes a bounded cooldown of its own; the provider stays healthy and
    keeps serving through its other credentials, and only a provider whose usable
    credentials are all gone reaches the provider-level fallback;
  * the classification that decides whether a failure is about the CREDENTIAL
    (rotate) or about the PROVIDER (do not burn the rest of the pool).

What this module never does: talk to a provider, read the text being spoken,
touch the owner's selection, or hold a secret anywhere but in the record it was
handed. No function here logs a secret: the only credential identifier in a trace
is the record's ``credential_id``, which the source guarantees is non-secret.

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

#: Hard ceiling on credential attempts for ONE provider inside ONE synthesis. The
#: provider may be attempted at most this many times, whatever the pool contains,
#: so a large pool can never become an unbounded credential sweep.
MAX_CREDENTIAL_ATTEMPTS_PER_PROVIDER = 3

#: Cooldown of a credential that failed: bounded doubling from this base, capped
#: at :data:`COOLDOWN_MAX_S`.
COOLDOWN_BASE_S = 60.0
COOLDOWN_MAX_S = 600.0

#: The failure classes that mean THIS credential is unusable — the TTS boundary's
#: own closed tokens, mirrored rather than invented. ``missing_credential`` is
#: here because a pool entry whose secret the backend could not decrypt is a
#: credential problem, and ``rate_limit`` because provider quotas are per-key:
#: rotating is the FIRST, cheapest response, and the per-provider ceiling keeps a
#: provider-wide 429 from becoming a credential sweep.
_CREDENTIAL_SPECIFIC_CLASSES = frozenset({
    "auth",
    "forbidden",
    "missing_credential",
    "rate_limit",
    "quota_exceeded",
})

#: The HTTP statuses that identify the CREDENTIAL regardless of the adapter's own
#: class vocabulary — the status is the honest classifier there.
_CREDENTIAL_SPECIFIC_STATUSES = frozenset({401, 403, 429})


def env_var_names(provider: str) -> tuple[str, ...]:
    """The provider's OWN declared credential variables, as public read-only data.

    Taken from the ADAPTER that owns them (never a second table, never an
    environment sweep), so the deployment's single key keeps working as the
    pool's first credential and a presence check can report the truth.
    """
    token = str(provider or "").strip()
    if token == "openai":
        from backend.services import openai_tts_engine

        return tuple(openai_tts_engine.API_KEY_ENV_VARS)
    return ()


def registered_providers() -> tuple[str, ...]:
    """Every provider the TTS registry registers, in canonical order.

    Derived from the control plane — never from a hard-coded list here and never
    from the environment — so a pool can only ever hold credentials for a provider
    the registry knows about.
    """
    from backend.ai.tts_control_plane import provider_ids

    return provider_ids()


# ── Loading (bounded, explicit, never fatal) ──


async def prepare(providers: Iterable[str] | None = None) -> dict[str, int]:
    """Load the credential pools the persisted selection may draw on.

    Called by the ONE settings-apply entry point, so the pools are always read
    from the SAME configuration that provisions the selection: at startup (so a
    persisted selection has its credentials from the first synthesis) and
    immediately after a Telegram settings change. Bounded and never fatal — a
    secret backend that is missing, slow or refusing contributes nothing, and the
    provider keeps its environment credential.
    """
    targets = tuple(providers) if providers is not None else registered_providers()
    counts: dict[str, int] = {}
    for provider in targets:
        records = await _load(provider)
        counts[provider] = len(records)
    return counts


async def _load(provider: str) -> tuple[CredentialRecord, ...]:
    from backend.ai import credential_source

    return await credential_source.load(str(provider or ""), env_var_names(provider))


def reset() -> None:
    """Drop every credential snapshot and health record (tests, explicit resets)."""
    from backend.ai import credential_source

    credential_source.reset()
    _health.clear()


# ── The ordered pool ──


def is_configured(provider: str) -> bool:
    """True when the secret backend has answered for ``provider``.

    Before the first load the runtime keeps its pre-existing single-credential
    route; after a load — even an empty one — the pool is authoritative.
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

    A credential serving its own cooldown is SKIPPED, so a spent key does not cost
    every later request an attempt. The one deliberate exception mirrors the
    provider layer's own rule: when EVERY credential of the provider is cooling
    down the pool is returned unchanged, because refusing to attempt the provider
    at all would turn a temporary credential condition into a guaranteed failure.
    Order within each group is preserved, so this is a stable reordering.
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
    value = str(getattr(error, "failure_class", "") or "")
    return value or "unknown"


def is_credential_specific(error: BaseException) -> bool:
    """Whether ``error`` says THIS credential is unusable.

    Fail-closed by construction: anything the boundary cannot classify — a
    programming error, a provider-wide 5xx, a timeout, a transport failure — is
    NOT credential-specific, so the rest of the pool is never burned on a
    provider outage and only the provider layer reacts.
    """
    from backend.services.tts_service import TtsError

    if not isinstance(error, TtsError):
        return False
    try:
        status = int(getattr(error, "http_status", 0) or 0)
    except (TypeError, ValueError):
        status = 0
    if status in _CREDENTIAL_SPECIFIC_STATUSES:
        return True
    return failure_class_of(error) in _CREDENTIAL_SPECIFIC_CLASSES


# ── Credential health (process-local, never persisted) ──


@dataclass
class CredentialHealth:
    """The bounded runtime metadata of ONE credential — never its secret."""

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
        "TTS_CREDENTIAL_COOLDOWN credential=%s failures=%d cooldown_s=%.0f "
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
    logger.info("TTS_CREDENTIAL_SUCCESS credential=%s", resolved)


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


def summarise(provider: str) -> dict[str, int]:
    """Bounded counts for a panel line: total / ready, never a secret."""
    records = credentials_for(provider)
    return {
        "total": len(records),
        "ready": len(rotation_for(provider)) if records else 0,
    }
