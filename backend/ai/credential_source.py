"""Credential source — the ONE boundary that answers "which credentials may this
provider use?".

This module is the SECRET half of the credential pool and nothing else. It knows
where a credential MAY come from and it hands back bounded, ORDERED records; it
never talks to a provider, never decides which provider to try and never decides
whether a credential is healthy. A capability asks for credentials for provider X
and receives candidates — it does not care whether they came from the deployment's
environment or from an encrypted secret store, which is exactly what keeps
provider adapters free of backend-specific reads:

    capability request (any provider)
        ↓
    provider adapters            (only the credential of the CURRENT attempt)
        ↓
    the credential pool          (order, health, classification — provider-agnostic)
        ↓
    THIS module                  (where a credential comes from)
        ├── the deployment's environment (the pre-existing single-key route)
        └── Supabase Vault (the multi-credential route, via ONE documented RPC)

Nothing here is Speech-to-Text specific: the module takes a provider token and the
provider's OWN declared environment variable names, so a Speech-to-Text provider,
a Text-to-Speech provider (``openai``) and any later AI/media provider share this
ONE secret architecture instead of growing a second one. The STT half lives in
``backend/services/stt_credential_pool.py`` and is its first consumer, not its
owner.

Two sources, ONE precedence rule. The environment keeps working as the FIRST
credential, so an installation that never configures a pool behaves EXACTLY as
it did before this phase — one provider, one key — while an installation that
does gets the ordered set `[environment, vault…]` and rotates inside it.
Deployment ENV is deliberately NOT expanded into a list of numbered variables
(``PROVIDER_KEY_1``/``_2``/…): the environment holds ONE credential per provider,
and additional credentials live in the secret backend, which is the whole point
of this boundary. Nothing here scans the environment for a name it was not
given: the caller passes the provider's OWN declared variable names, which are
the same constants the provider adapters already read.

Supabase Vault contract (the user configures this side MANUALLY)

This module executes NO SQL and creates NO secret. It calls ONE PostgREST RPC
with the project's existing service-role client — the same client and the same
bounded dispatch (``backend.db.client.run_sync_db``) every other durable read
uses — and it fails CLOSED when that RPC is absent or refuses:

    POST /rest/v1/rpc/api_credential_pool   body: {"p_provider": "<provider>"}
    → [ { "credential_id": "<stable, non-secret id>",
          "priority":      <int, optional>,
          "enabled":       <bool, optional>,
          "secret":        "<the API key, decrypted from Vault>" } , … ]

The function is created by ``supabase/migrations/20260919000001_create_api_credential_vault.sql``
(apply it manually — see DATABASE_ARCHITECTURE.md §29) and is OWNED by
``postgres`` as SECURITY DEFINER, so it reads ``vault.decrypted_secrets`` on the
caller's behalf and the application never touches ``vault.*`` directly: the
secret store's own schema, naming and access policy stay the user's. The
parameter shape is deliberately unchanged from the M2.4 contract (``p_provider``
only; the function's second parameter is an optional owner filter the runtime
omits), so this call is byte-identical to what the previous phase documented.

The older ``stt_credential_pool`` name still works — the migration keeps it as a
thin alias resolving to this same function — but this module targets the generic
name, because the boundary is not STT specific.

A missing function, a permission error, an unexpected shape, an empty secret or a
row without an id is reported as a bounded reason and that provider simply keeps
the credentials it already had — this boundary can never fail a media request and
can never invent a credential.

Caching: deliberately process-local and bounded. A snapshot is loaded by
``load()`` (at startup and after every STT settings change — the two places that
already own this state), it is never written to disk, never logged and never
returned in an error, and ``mark_stale()`` records that a credential failed so
the NEXT load knows it is refreshing a compromised snapshot. Serving the last
snapshot until then is deliberate: dropping it mid-incident would leave the
runtime with fewer credentials than it started with.

Zero secret egress: a record's ``credential_id`` is the ONLY identifier this
module ever logs. A secret is never logged, never persisted, never returned to
Telegram and never put in a message — no function here formats one.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

logger = logging.getLogger(__name__)

#: The two sources a credential can come from. Kept as bounded tokens so a
#: consumer (and a trace) can say WHERE a credential came from without ever
#: naming or exposing it.
SOURCE_ENV = "env"
SOURCE_VAULT = "vault"

#: The documented Supabase RPC this boundary consumes. ONE name, no environment
#: variable to configure: a deployment that wants a pool applies the migration
#: that creates this function (over Vault) and nothing else changes.
VAULT_RPC = "api_credential_pool"

#: The M2.4 name of the same contract. The migration keeps it as a thin alias,
#: and this module names it only so a trace or a test can refer to the older
#: spelling without a string literal. The runtime always calls :data:`VAULT_RPC`.
LEGACY_VAULT_RPC = "stt_credential_pool"

#: Bounds. A pool is a bounded list, not an unbounded secret sweep: the number of
#: credentials one provider may contribute and the number of provider snapshots
#: kept in memory are both finite, so credential rotation can never become an
#: unbounded workload.
MAX_CREDENTIALS_PER_PROVIDER = 4
MAX_CACHED_PROVIDERS = 16

#: The credential read shares the request-independent bound every other durable
#: read uses, but shorter: a settings apply must not wait a database timeout for
#: an OPTIONAL pool, because the environment credential already works.
VAULT_TIMEOUT_S = 5.0

#: A credential identifier is a label, never a secret. Anything outside this
#: small alphabet is refused rather than sanitized, so an id can never smuggle a
#: key fragment into a log line.
_ID_ALLOWED = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"
)
_MAX_ID_LEN = 64


@dataclass(frozen=True)
class CredentialRecord:
    """ONE credential candidate: a stable identifier plus the secret itself.

    ``secret`` is the only field that must never leave the process. Everything
    else is safe metadata: ``credential_id`` is what a trace names, ``source``
    says which backend answered, ``priority`` is the owner's explicit ordering
    and ``order_index`` is the deterministic tie-breaker (the environment is 0,
    so the pre-existing single key stays FIRST unless an explicit priority
    outranks it).
    """

    credential_id: str
    provider: str
    secret: str
    source: str
    priority: int = 0
    order_index: int = 0

    @property
    def is_env(self) -> bool:
        """True for the deployment's own single configured credential."""
        return self.source == SOURCE_ENV

    @property
    def order_key(self) -> tuple[int, int]:
        """The deterministic ordering key: explicit priority, then source order."""
        return (self.priority, self.order_index)


#: provider → the snapshot last loaded for it. ``None`` (absent) means "never
#: loaded", which is a DIFFERENT state from "loaded and legitimately empty".
_cache: dict[str, tuple[CredentialRecord, ...]] = {}

#: providers whose last loaded snapshot contained a credential that failed.
#: Runtime posture only: it is never persisted and it changes nothing about what
#: is served until the next load replaces the snapshot.
_stale: set[str] = set()


def reset() -> None:
    """Drop every snapshot (tests and explicit resets)."""
    _cache.clear()
    _stale.clear()


def cached(provider: str) -> tuple[CredentialRecord, ...] | None:
    """The last loaded snapshot, or ``None`` when nothing was ever loaded."""
    return _cache.get(str(provider or ""))


def is_loaded(provider: str) -> bool:
    """True when a snapshot exists — including a legitimately EMPTY one.

    The distinction matters: "never loaded" means the runtime must keep its
    pre-existing single-credential route, while "loaded and empty" means the
    backend was asked and had nothing, which is a real, reportable answer.
    """
    return str(provider or "") in _cache


def mark_stale(provider: str) -> None:
    """Record that a credential of ``provider`` failed.

    The snapshot stays SERVED (a credential failure must never reduce the
    runtime's usable credentials) and the next :func:`load` reloads it, so a
    revoked credential is refreshed at the next settings apply.
    """
    _stale.add(str(provider or ""))


def env_credential(
    provider: str, env_var_names: Sequence[str]
) -> CredentialRecord | None:
    """The deployment's ONE credential for ``provider``, or ``None``.

    The variable names are the provider's OWN declared ones (the same constants
    its adapter reads), in the adapter's own precedence order — the first one
    that carries a value wins and the rest are not inspected, so this can never
    become an environment sweep.
    """
    resolved = str(provider or "").strip()
    if not resolved:
        return None
    for name in env_var_names:
        value = (os.getenv(str(name)) or "").strip()
        if value:
            return CredentialRecord(
                credential_id=f"{SOURCE_ENV}:{name}",
                provider=resolved,
                secret=value,
                source=SOURCE_ENV,
                priority=0,
                order_index=0,
            )
    return None


def _safe_id(raw: Any) -> str:
    """A bounded, non-secret credential id, or ``""`` when it is unusable."""
    text = str(raw or "").strip()
    if not text or len(text) > _MAX_ID_LEN:
        return ""
    if any(character not in _ID_ALLOWED for character in text):
        return ""
    return text


def _parse_rows(provider: str, rows: Iterable[Any]) -> tuple[CredentialRecord, ...]:
    """The usable Vault rows of ONE response, in the order the RPC returned them.

    Every unusable row is skipped rather than repaired: no id, no secret, an
    explicitly disabled credential and a non-mapping row all contribute nothing,
    so a half-configured secret store degrades to the credentials that DO work
    instead of failing the request.
    """
    records: list[CredentialRecord] = []
    for row in rows or ():
        if not isinstance(row, Mapping):
            continue
        if row.get("enabled") is False:
            continue
        identifier = _safe_id(row.get("credential_id"))
        secret = str(row.get("secret") or "")
        if not identifier or not secret:
            continue
        try:
            priority = int(row.get("priority") or 0)
        except (TypeError, ValueError):
            priority = 0
        records.append(
            CredentialRecord(
                credential_id=f"{SOURCE_VAULT}:{identifier}",
                provider=provider,
                secret=secret,
                source=SOURCE_VAULT,
                priority=priority,
                order_index=len(records) + 1,
            )
        )
        if len(records) >= MAX_CREDENTIALS_PER_PROVIDER:
            break
    return tuple(records)


def _vault_rows_sync(provider: str) -> tuple[Any, ...]:
    """The raw RPC rows for ``provider`` — the ONE synchronous database call.

    Runs on the project's bounded Supabase dispatch thread, with the shared
    service-role client, exactly like every other durable read. Returns no rows
    when the database is not configured, so an installation without Supabase
    keeps its environment credential and nothing else happens.
    """
    from backend.db.client import get_db

    db = get_db()
    if db is None:
        return ()
    response = db.rpc(VAULT_RPC, {"p_provider": provider}).execute()
    return tuple(getattr(response, "data", None) or ())


async def load(provider: str, env_var_names: Sequence[str]) -> tuple[CredentialRecord, ...]:
    """Load the provider's ordered credentials: environment first, then Vault.

    Never raises and never fails a caller: any problem with the secret backend
    is reported as a bounded reason and contributes nothing, so the environment
    credential (or the provider's own previously configured credential) keeps the
    provider operating. Both the environment record and the Vault records are
    ordered by (explicit priority, source order), which makes the result stable
    across processes and requests — never a random rotation.
    """
    resolved = str(provider or "").strip()
    if not resolved:
        return ()

    records: list[CredentialRecord] = []
    environment = env_credential(resolved, env_var_names)
    if environment is not None:
        records.append(environment)

    try:
        rows = await _fetch_rows(resolved)
    except Exception as exc:  # noqa: BLE001 — an optional pool is never fatal
        rows = ()
        logger.warning(
            "STT_CREDENTIAL_VAULT_READ_FAILED provider=%s error=%s",
            resolved, type(exc).__name__,
        )
    vault = _parse_rows(resolved, rows)
    records.extend(vault)
    # The bound applies to the PROVIDER's pool, after the deterministic order is
    # known — so it is the owner's explicit priorities that decide who survives a
    # pool larger than the ceiling, never the order two sources happened to be
    # merged in.
    ordered = tuple(
        sorted(records, key=lambda record: record.order_key)
    )[:MAX_CREDENTIALS_PER_PROVIDER]

    refreshing = resolved in _stale
    _stale.discard(resolved)
    if len(_cache) >= MAX_CACHED_PROVIDERS and resolved not in _cache:
        _cache.clear()
    _cache[resolved] = ordered
    logger.info(
        "STT_CREDENTIAL_POOL_LOADED provider=%s credentials=%d env=%d vault=%d "
        "refreshed=%s",
        resolved, len(ordered), 1 if environment is not None else 0, len(vault),
        refreshing,
    )
    return ordered


async def _fetch_rows(provider: str) -> tuple[Any, ...]:
    """The bounded Vault read, dispatched off the event loop."""
    from backend.db.client import run_sync_db

    return await run_sync_db(_vault_rows_sync, provider, timeout=VAULT_TIMEOUT_S)
