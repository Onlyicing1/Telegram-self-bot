"""Credential management service — the ONE owner-facing management boundary.

This is the layer between a Telegram surface and the credential store, and it
exists so the surface never touches the database or Vault:

    Telegram owner
        ↓
    AI → Media Analysis → API Credentials     backend/bot/handlers/ai_credentials.py
        ↓
    THIS module                               validation, ordering, bounded reasons
        ↓
    the SECURITY DEFINER management RPCs      supabase/migrations/20260919000002_*.sql
        ├── public.api_credentials            (metadata ONLY — there is no secret column)
        └── vault.secrets                     (the raw key, via the vault.* API)

The credential STORE stays generic: every function here takes a free-form provider
token, so the Speech-to-Text providers (`gemini`, `groq`, `speechmatics`) and the
Text-to-Speech provider (`openai`) are managed by the same code, the same table
and the same functions. No `stt_credentials.py`, no `tts_credentials.py`, no
second secret architecture.

THE SECRET RULE

A raw secret is accepted by exactly two functions — :func:`create_credential` and
:func:`replace_secret` — as their last argument, and it goes straight into the RPC
body. It is never:

  * placed in a dataclass, a module global, a cache or any returned value,
  * logged, or included in an exception message,
  * rendered into a Telegram message or a callback payload,
  * written to any ordinary database column.

Nothing in this module can READ a secret back out: the management RPCs return the
metadata projection of `api_credentials` and cannot return a secret, and
:func:`list_credentials` additionally REFUSES an entire response that contains a
secret-bearing key, so a future schema mistake surfaces as a bounded failure
instead of a leak. There is no "show key" operation anywhere.

A Python `str` cannot be zeroed, so the secret lives in one function's local
scope for the duration of one call and is not retained anywhere after it; the
Telegram message that carried it is deleted by the handler (see
``backend/bot/handlers/ai_credentials.py``).

IDENTITY WITHOUT SECRETS

The owner-facing surface addresses a credential by a short non-secret HANDLE
(:func:`handle_for`) derived from its `credential_id` — never from the secret.
That keeps a Telegram callback payload inside the 64-byte limit whatever the
credential id's length, keeps no process state between a render and a tap, and is
not a security boundary: every call re-reads the owner's own metadata and every
RPC filters on `owner_id`.

FAILURE MODEL

Every operation returns a bounded reason token instead of raising. A transport
failure, a store that is not configured, a refused statement and an unexpected
shape each map to their own class, and the database's own error TEXT is never
logged — only the bounded class and, at most, its SQLSTATE-style code — so a
refused statement can never echo anything sensitive into a log line.
"""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

logger = logging.getLogger(__name__)

# ── The management RPCs (created by the PART 2 migration) ──────────────────

RPC_LIST = "api_credential_list"
RPC_CREATE = "api_credential_create"
RPC_REPLACE = "api_credential_replace_secret"
RPC_UPDATE = "api_credential_update"
RPC_DELETE = "api_credential_delete"

#: Bounds. They mirror the SQL constraints exactly, so a request the database
#: would refuse is refused here first, with a precise class and without a round
#: trip. The secret ceiling is generous for an API key and finite for everything.
MAX_LABEL_CHARS = 80
MIN_SECRET_CHARS = 1
MAX_SECRET_CHARS = 8192
MAX_PRIORITY = 1_000_000
MIN_PRIORITY = 0
MAX_LISTED = 64

#: ONE management call shares the durable-read budget: a management round trip is
#: a single PostgREST statement, and a Telegram tap must not hang on it.
MANAGEMENT_TIMEOUT_S = 10.0

#: The short handle length (hex characters of the credential id's digest).
HANDLE_LENGTH = 12

# ── Bounded reason vocabulary (closed) ─────────────────────────────────────

REASON_OK = ""
REASON_NOT_CONFIGURED = "store_not_configured"
REASON_UNAVAILABLE = "store_unavailable"
REASON_INVALID_OWNER = "invalid_owner"
REASON_INVALID_PROVIDER = "invalid_provider"
REASON_INVALID_LABEL = "invalid_label"
REASON_INVALID_PRIORITY = "invalid_priority"
REASON_INVALID_CREDENTIAL = "invalid_credential"
REASON_EMPTY_SECRET = "empty_secret"
REASON_SECRET_TOO_LONG = "secret_too_long"
REASON_SECRET_SHAPE = "secret_shape"
REASON_NOT_FOUND = "credential_not_found"
REASON_REJECTED = "rejected"
REASON_FAILED = "failed"

#: Owner-facing wording for each bounded reason. A reason with no entry is
#: rendered by its own token, so an unmapped class degrades to something honest
#: rather than to a claim of success.
REASON_LABELS: dict[str, str] = {
    REASON_NOT_CONFIGURED: "credential store not configured on this deployment",
    REASON_UNAVAILABLE: "credential store unavailable",
    REASON_INVALID_OWNER: "invalid owner",
    REASON_INVALID_PROVIDER: "unknown provider",
    REASON_INVALID_LABEL: f"label must be 1–{MAX_LABEL_CHARS} characters",
    REASON_INVALID_PRIORITY: f"priority must be {MIN_PRIORITY}–{MAX_PRIORITY}",
    REASON_INVALID_CREDENTIAL: "invalid credential identifier",
    REASON_EMPTY_SECRET: "the key was empty — nothing was stored",
    REASON_SECRET_TOO_LONG: f"the key is longer than {MAX_SECRET_CHARS} characters",
    REASON_SECRET_SHAPE: (
        "that does not look like an API key — a key is ONE token with no "
        "spaces or line breaks. Nothing was stored"
    ),
    REASON_NOT_FOUND: "that credential no longer exists",
    REASON_REJECTED: "the store refused the change",
    REASON_FAILED: "the operation failed",
}

#: The exception tokens the management functions raise, mapped to this module's
#: bounded classes. Matched as substrings of the database's error text — the text
#: is never logged and never returned, only the class it produced.
_EXCEPTION_TOKENS: tuple[tuple[str, str], ...] = (
    ("invalid_owner", REASON_INVALID_OWNER),
    ("invalid_provider", REASON_INVALID_PROVIDER),
    ("invalid_label", REASON_INVALID_LABEL),
    ("invalid_priority", REASON_INVALID_PRIORITY),
    ("invalid_credential", REASON_INVALID_CREDENTIAL),
    ("empty_secret", REASON_EMPTY_SECRET),
    ("secret_too_long", REASON_SECRET_TOO_LONG),
    ("secret_shape", REASON_SECRET_SHAPE),
    ("credential_not_found", REASON_NOT_FOUND),
    ("credential_id_collision", REASON_REJECTED),
)

#: What an absent management function looks like. PostgREST reports a missing RPC
#: as PGRST202 ("Could not find the function … in the schema cache") and PostgreSQL
#: as `undefined_function`. Either way the honest answer is "not configured": the
#: owner has not applied the PART 2 SQL, and the runtime keeps working without it.
_NOT_CONFIGURED_MARKERS = (
    "pgrst202",
    "could not find the function",
    "undefined_function",
    "42883",
)

#: Keys that must never appear in a metadata response. A row carrying any of them
#: makes the WHOLE response unusable — fail-closed, so a schema change that started
#: returning a secret would be a visible refusal instead of a silent leak.
_SECRET_BEARING_KEYS = frozenset({
    "secret", "secrets", "api_key", "apikey", "key", "token", "password",
    "passwd", "credential_secret", "decrypted_secret", "vault_secret",
    "vault_secret_id", "session", "bearer", "hash",
})

#: The metadata projection every management RPC returns. A response missing one of
#: these fields is malformed and contributes nothing.
_METADATA_FIELDS = ("credential_id", "provider", "label", "enabled", "priority")

#: Display labels for the providers this project actually executes. The LIST of
#: providers is derived from the registries (:func:`registered_providers`); this
#: table only decides how a known token is spelled for the owner, and an unknown
#: token falls back to its own token, capitalised.
_PROVIDER_LABELS: dict[str, str] = {
    "gemini": "Gemini",
    "groq": "Groq",
    "speechmatics": "Speechmatics",
    "openai": "OpenAI",
}


# ── The models ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class CredentialMetadata:
    """ONE credential's non-secret metadata — the only shape a caller receives.

    There is deliberately no secret field: this type cannot carry one, so no code
    path that builds or consumes it can leak one. ``credential_id`` is safe to log
    and ``handle`` is the short, non-secret address the Telegram surface uses.
    """

    credential_id: str
    provider: str
    label: str
    enabled: bool
    priority: int
    created_at: str = ""
    updated_at: str = ""

    @property
    def handle(self) -> str:
        return handle_for(self.credential_id)

    def state_word(self) -> str:
        return "Enabled" if self.enabled else "Disabled"


@dataclass(frozen=True)
class CredentialOutcome:
    """The bounded result of ONE management operation (never a secret)."""

    ok: bool
    reason: str = REASON_OK
    credential: CredentialMetadata | None = None

    @property
    def reason_label(self) -> str:
        return REASON_LABELS.get(self.reason, self.reason or "ok")


@dataclass(frozen=True)
class CredentialListResult:
    """The bounded result of ONE listing: metadata rows plus a bounded reason."""

    credentials: tuple[CredentialMetadata, ...] = ()
    reason: str = REASON_OK

    @property
    def ok(self) -> bool:
        return self.reason == REASON_OK

    @property
    def reason_label(self) -> str:
        return REASON_LABELS.get(self.reason, self.reason or "ok")


# The credential TEST states. Distinct from provider health on purpose: a test
# says whether the provider ACCEPTED this credential for one bounded request.
TEST_PASSED = "passed"
TEST_UNAUTHORIZED = "unauthorized"
TEST_RATE_LIMITED = "rate_limited"
TEST_TIMEOUT = "timeout"
TEST_UNAVAILABLE = "unavailable"
TEST_NOT_SUPPORTED = "not_supported"
TEST_DISABLED = "disabled"
TEST_NOT_FOUND = "not_found"
TEST_FAILED = "failed"

TEST_STATE_LABELS: dict[str, str] = {
    TEST_PASSED: "Passed",
    TEST_UNAUTHORIZED: "Unauthorized",
    TEST_RATE_LIMITED: "Rate limited",
    TEST_TIMEOUT: "Timeout",
    TEST_UNAVAILABLE: "Provider unavailable",
    TEST_NOT_SUPPORTED: "Not supported",
    TEST_DISABLED: "Disabled",
    TEST_NOT_FOUND: "Credential not found",
    TEST_FAILED: "Failed",
}

#: Outer bound of the credential test. The adapter it drives carries its own
#: tighter operation deadline, so a classified engine failure normally wins.
TEST_TIMEOUT_S = 50.0

#: Process-local test observations, keyed by credential id. Deliberately NOT
#: persisted: a test is a diagnostic observation, not configuration, and a restart
#: honestly returns every credential to "not tested in this session" instead of
#: replaying a stale claim. Bounded — the oldest entries are dropped.
TEST_MEMORY_MAX = 64

_last_test: dict[str, str] = {}


def last_test_state(credential_id: str) -> str:
    """The last test state observed in this process, or ``""`` when untested."""
    return _last_test.get(str(credential_id or ""), "")


def last_test_label(credential_id: str) -> str:
    """Owner-facing wording for the last test, or an honest "not tested"."""
    state = last_test_state(credential_id)
    return TEST_STATE_LABELS.get(state, state) if state else "not tested in this session"


def record_test(credential_id: str, state: str) -> None:
    """Remember ONE observation (bounded, process-local, never a secret)."""
    key = str(credential_id or "")
    if not key:
        return
    if len(_last_test) >= TEST_MEMORY_MAX and key not in _last_test:
        _last_test.clear()
    _last_test[key] = str(state or "")


def clear_tests() -> None:
    """Drop every observation (tests and explicit resets)."""
    _last_test.clear()


@dataclass(frozen=True)
class CredentialTestResult:
    """The bounded, non-sensitive outcome of ONE credential test.

    ``failure_class`` is a closed token from the adapter's own classification and
    ``latency_ms`` the measured wall clock; no request body, no response body, no
    transcript and no secret is ever part of a result.
    """

    state: str
    credential_id: str = ""
    provider: str = ""
    candidate_id: str = ""
    failure_class: str = ""
    latency_ms: int = 0
    reason: str = REASON_OK

    @property
    def state_label(self) -> str:
        return TEST_STATE_LABELS.get(self.state, self.state)

    @property
    def passed(self) -> bool:
        return self.state == TEST_PASSED


# ── Provider discovery (derived, never a second hard-coded list) ───────────


def registered_providers() -> tuple[str, ...]:
    """The providers this project can actually execute, in a deterministic order.

    Derived from the existing registries — the Speech-to-Text control plane's
    candidates first (in their canonical order), then the Text-to-Speech
    provider — so the credential UI can only ever offer a provider that has a
    runtime adapter, and a newly registered capability appears here without a
    second list to maintain.
    """
    providers: list[str] = []
    try:
        from backend.services.stt_credential_pool import registered_providers as stt_providers

        for provider in stt_providers():
            if provider and provider not in providers:
                providers.append(provider)
    except Exception as exc:  # noqa: BLE001 — discovery is never fatal
        logger.warning("CREDENTIAL_PROVIDER_DISCOVERY_FAILED error=%s", type(exc).__name__)
    try:
        from backend.services import tts_service

        tts_provider = str((tts_service.describe() or {}).get("provider") or "")
    except Exception as exc:  # noqa: BLE001 — discovery is never fatal
        logger.warning("CREDENTIAL_TTS_PROVIDER_DISCOVERY_FAILED error=%s", type(exc).__name__)
        tts_provider = ""
    if tts_provider and tts_provider not in providers:
        providers.append(tts_provider)
    return tuple(providers)


def is_registered_provider(provider: str) -> bool:
    return str(provider or "").strip() in registered_providers()


def provider_label(provider: str) -> str:
    """The owner-facing name of a provider token (never an invented capability)."""
    token = str(provider or "").strip()
    if token in _PROVIDER_LABELS:
        return _PROVIDER_LABELS[token]
    return token.replace("_", " ").title() or "Unknown"


def provider_candidates(provider: str) -> tuple[Any, ...]:
    """The implemented Speech-to-Text candidates of a provider, canonical order."""
    from backend.ai.stt_control_plane import all_candidates

    token = str(provider or "").strip()
    return tuple(
        candidate
        for candidate in all_candidates()
        if candidate.provider == token and candidate.implemented
    )


def provider_testable(provider: str) -> bool:
    """True when a safe credential test exists for this provider on this build."""
    return bool(provider_candidates(provider))


def env_credential_present(provider: str) -> bool:
    """Whether the deployment's OWN credential exists for ``provider``.

    Reports PRESENCE only: the record's secret is never read, returned, logged or
    compared. This is the honest way to tell the owner that a provider keeps
    working through its deployment configuration when no managed credential is
    configured — and it is never presented as provider health.
    """
    from backend.services.stt_credential_pool import env_var_names

    names = env_var_names(provider)
    if not names:
        return False
    from backend.ai import credential_source

    try:
        return credential_source.env_credential(provider, names) is not None
    except Exception as exc:  # noqa: BLE001 — a presence line is never fatal
        logger.warning("CREDENTIAL_ENV_PRESENCE_FAILED error=%s", type(exc).__name__)
        return False


# ── Handles ────────────────────────────────────────────────────────────────


def handle_for(credential_id: str) -> str:
    """A short, non-secret, deterministic handle for ONE credential id.

    A digest of the (already public) identifier, never of a secret: the same id
    always produces the same handle in every process, so a rendered button keeps
    resolving after a restart without any stored state. It is an ADDRESS, not a
    capability — every operation still re-reads the owner's own metadata and every
    RPC filters on `owner_id`.
    """
    digest = hashlib.sha256(str(credential_id or "").encode("utf-8")).hexdigest()
    return digest[:HANDLE_LENGTH]


# ── Validation (app-side, mirroring the SQL constraints) ───────────────────


def _validate_owner(owner_id: Any) -> str:
    try:
        value = int(owner_id)
    except (TypeError, ValueError):
        return REASON_INVALID_OWNER
    return REASON_OK if value > 0 else REASON_INVALID_OWNER


def _validate_provider(provider: Any) -> str:
    token = str(provider or "").strip()
    if not token or len(token) > 32:
        return REASON_INVALID_PROVIDER
    if not token[0].isalnum() or not token[0].islower() or not token[0].isascii():
        return REASON_INVALID_PROVIDER
    allowed = frozenset("abcdefghijklmnopqrstuvwxyz0123456789._-")
    if any(character not in allowed for character in token):
        return REASON_INVALID_PROVIDER
    if not is_registered_provider(token):
        return REASON_INVALID_PROVIDER
    return REASON_OK


def _validate_label(label: Any) -> str:
    text = str(label or "").strip()
    return REASON_OK if 0 < len(text) <= MAX_LABEL_CHARS else REASON_INVALID_LABEL


def _validate_priority(priority: Any) -> str:
    try:
        value = int(priority)
    except (TypeError, ValueError):
        return REASON_INVALID_PRIORITY
    return REASON_OK if MIN_PRIORITY <= value <= MAX_PRIORITY else REASON_INVALID_PRIORITY


def _validate_credential_id(credential_id: Any) -> str:
    text = str(credential_id or "").strip()
    if not text or len(text) > 64:
        return REASON_INVALID_CREDENTIAL
    allowed = frozenset(
        "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"
    )
    return REASON_OK if all(c in allowed for c in text) else REASON_INVALID_CREDENTIAL


def _validate_secret(secret: Any) -> str:
    """Validate a key's SHAPE, never its value.

    Length is bounded like the store bounds it, and the key must be ONE opaque
    token: a value containing whitespace or a control character is refused. Every
    API key this project's providers issue is such a token, and the rule makes an
    accidental ordinary chat message fail closed instead of silently becoming the
    stored credential. The value itself is never inspected, compared or logged.
    """
    text = secret if isinstance(secret, str) else ""
    if len(text) < MIN_SECRET_CHARS:
        return REASON_EMPTY_SECRET
    if len(text) > MAX_SECRET_CHARS:
        return REASON_SECRET_TOO_LONG
    if any(character.isspace() or ord(character) < 32 for character in text):
        return REASON_SECRET_SHAPE
    return REASON_OK


def _as_priority(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


# ── The database boundary ──────────────────────────────────────────────────


class _StoreUnavailable(Exception):
    """Raised inside the worker thread when Supabase is not configured."""


def _rpc_sync(name: str, payload: Mapping[str, Any]) -> Any:
    """ONE management RPC on the project's bounded Supabase dispatch.

    The payload may carry a secret (create / replace only). It is handed to the
    client and nowhere else: this function logs nothing, and the dispatcher that
    runs it names the operation by function name only, so no argument — and
    therefore no secret — can reach a log line or a diagnostic event.
    """
    from backend.db.client import get_db

    db = get_db()
    if db is None:
        raise _StoreUnavailable(name)
    response = db.rpc(str(name), dict(payload)).execute()
    return getattr(response, "data", None)


def _classify(error: BaseException) -> str:
    """The bounded class of a management failure (never its text)."""
    if isinstance(error, _StoreUnavailable):
        return REASON_NOT_CONFIGURED
    try:
        from backend.db.client import get_db

        if get_db() is None:
            return REASON_NOT_CONFIGURED
    except Exception:  # noqa: BLE001 — classification must never raise
        pass
    lowered = str(error).lower()
    if any(marker in lowered for marker in _NOT_CONFIGURED_MARKERS):
        return REASON_NOT_CONFIGURED
    for token, reason in _EXCEPTION_TOKENS:
        if token in lowered:
            return reason
    return REASON_FAILED


def _error_code(error: BaseException) -> str:
    """At most the SQLSTATE-style code of a failure — never its message."""
    for attribute in ("code", "sqlstate", "status"):
        value = getattr(error, attribute, "")
        if isinstance(value, (str, int)) and str(value):
            return str(value)[:32]
    return ""


async def _call(name: str, payload: Mapping[str, Any]) -> tuple[Any, str]:
    """Run ONE bounded management call: ``(data, bounded_reason)``.

    Never raises. On any failure the reason is a bounded class and the error's own
    text is discarded — for a create or a replace that text could in principle
    quote the request, so only the class and, at most, the code are logged.
    """
    from backend.db.client import run_sync_db

    try:
        data = await run_sync_db(_rpc_sync, name, payload, timeout=MANAGEMENT_TIMEOUT_S)
    except Exception as exc:  # noqa: BLE001 — a management failure is never fatal
        reason = _classify(exc)
        logger.warning(
            "CREDENTIAL_STORE_%s rpc=%s class=%s code=%s",
            "NOT_CONFIGURED" if reason == REASON_NOT_CONFIGURED else "FAILED",
            name, reason, _error_code(exc) or "-",
        )
        return None, reason
    return data, REASON_OK


def _rows(data: Any) -> tuple[Mapping[str, Any], ...]:
    if isinstance(data, list):
        return tuple(row for row in data if isinstance(row, Mapping))
    return ()


def _first(data: Any) -> Mapping[str, Any] | None:
    rows = _rows(data)
    return rows[0] if rows else None


def _scalar_bool(data: Any) -> bool:
    if isinstance(data, bool):
        return data
    if isinstance(data, list):
        return bool(data) and data[0] is True
    return False


def _metadata_from_row(row: Mapping[str, Any]) -> CredentialMetadata | None:
    """Parse ONE metadata row, or ``None`` when it is unusable or unsafe.

    Any secret-bearing key refuses the row outright: the management RPCs cannot
    return a secret, so a response that carries one is a schema change, and
    refusing it is the fail-closed behavior.
    """
    lowered = {str(key).lower() for key in row.keys()}
    if lowered & _SECRET_BEARING_KEYS:
        logger.error("CREDENTIAL_METADATA_CONTAINED_A_SECRET_BEARING_FIELD")
        return None
    if any(field not in row for field in _METADATA_FIELDS):
        return None
    credential_id = str(row.get("credential_id") or "").strip()
    provider = str(row.get("provider") or "").strip()
    if not credential_id or not provider:
        return None
    return CredentialMetadata(
        credential_id=credential_id,
        provider=provider,
        label=str(row.get("label") or ""),
        enabled=bool(row.get("enabled", False)),
        priority=_as_priority(row.get("priority")),
        created_at=str(row.get("created_at") or ""),
        updated_at=str(row.get("updated_at") or ""),
    )


def _parse_metadata(data: Any) -> tuple[tuple[CredentialMetadata, ...], bool]:
    """``(credentials, clean)`` — ``clean`` is False when a row was refused."""
    parsed: list[CredentialMetadata] = []
    for row in _rows(data)[:MAX_LISTED]:
        metadata = _metadata_from_row(row)
        if metadata is None:
            # A malformed row is dropped rather than repaired; a row that carried
            # a secret-bearing field refuses the whole response.
            if {str(key).lower() for key in row.keys()} & _SECRET_BEARING_KEYS:
                return (), False
            continue
        parsed.append(metadata)
    return tuple(parsed), True


def _payload(**values: Any) -> dict[str, Any]:
    return {key: value for key, value in values.items()}


# ── Listing ────────────────────────────────────────────────────────────────


async def list_credentials(
    owner_id: int, provider: str | None = None,
) -> CredentialListResult:
    """The owner's credential METADATA, optionally for one provider.

    Returns metadata only — the RPC has no secret to return — in the store's own
    deterministic order (provider, priority, created_at, credential_id), which is
    the order the credential pool resolves in.
    """
    reason = _validate_owner(owner_id)
    if reason:
        return CredentialListResult(reason=reason)
    token = str(provider).strip() if provider else ""
    data, reason = await _call(RPC_LIST, _payload(p_owner_id=int(owner_id), p_provider=token or None))
    if reason:
        return CredentialListResult(reason=reason)
    credentials, clean = _parse_metadata(data)
    if not clean:
        return CredentialListResult(reason=REASON_FAILED)
    return CredentialListResult(credentials=credentials)


async def credential(owner_id: int, credential_id: str) -> CredentialMetadata | None:
    """The owner's credential with this exact id, re-read from the store.

    The one lookup every management call goes through: the id alone is never
    trusted, because what matters is that THIS OWNER has a row with it.
    """
    wanted = str(credential_id or "").strip()
    if not wanted:
        return None
    result = await list_credentials(owner_id)
    if not result.ok:
        return None
    return next(
        (item for item in result.credentials if item.credential_id == wanted), None,
    )


async def resolve_handle(owner_id: int, handle: str) -> CredentialMetadata | None:
    """The owner's credential addressed by a handle, or ``None``.

    The digest is recomputed over the owner's OWN metadata, so a handle can only
    ever resolve to a credential that owner may manage.
    """
    wanted = str(handle or "").strip()
    if not wanted:
        return None
    result = await list_credentials(owner_id)
    if not result.ok:
        return None
    matches = [item for item in result.credentials if item.handle == wanted]
    return matches[0] if len(matches) == 1 else None


# ── Mutations ──────────────────────────────────────────────────────────────


async def create_credential(
    owner_id: int,
    provider: str,
    label: str,
    secret: str,
    *,
    priority: int = 0,
    enabled: bool = True,
) -> CredentialOutcome:
    """Create ONE credential: the raw secret crosses into Vault and is not sent back.

    The secret is this function's last argument and the only copy of it lives in
    the request body built here; nothing about it is retained afterwards, and the
    returned :class:`CredentialOutcome` carries metadata only.
    """
    for reason in (
        _validate_owner(owner_id),
        _validate_provider(provider),
        _validate_label(label),
        _validate_priority(priority),
        _validate_secret(secret),
    ):
        if reason:
            return CredentialOutcome(ok=False, reason=reason)

    data, reason = await _call(RPC_CREATE, _payload(
        p_owner_id=int(owner_id),
        p_provider=str(provider).strip(),
        p_label=str(label).strip(),
        p_secret=secret,
        p_priority=int(priority),
        p_enabled=bool(enabled),
    ))
    if reason:
        return CredentialOutcome(ok=False, reason=reason)
    return _outcome_from(data)


async def replace_secret(owner_id: int, credential_id: str, secret: str) -> CredentialOutcome:
    """Replace ONE credential's secret. Never returns the old or the new key."""
    for reason in (
        _validate_owner(owner_id),
        _validate_credential_id(credential_id),
        _validate_secret(secret),
    ):
        if reason:
            return CredentialOutcome(ok=False, reason=reason)

    data, reason = await _call(RPC_REPLACE, _payload(
        p_owner_id=int(owner_id),
        p_credential_id=str(credential_id).strip(),
        p_secret=secret,
    ))
    if reason:
        return CredentialOutcome(ok=False, reason=reason)
    return _outcome_from(data)


async def update_metadata(
    owner_id: int,
    credential_id: str,
    *,
    label: str | None = None,
    enabled: bool | None = None,
    priority: int | None = None,
) -> CredentialOutcome:
    """Update label / enabled / priority of ONE credential.

    Metadata only: this call never reads and never rotates the secret, so an
    enable/disable or a reorder cannot disturb the stored key.
    """
    reason = _validate_owner(owner_id) or _validate_credential_id(credential_id)
    if reason:
        return CredentialOutcome(ok=False, reason=reason)
    if label is not None:
        reason = _validate_label(label)
        if reason:
            return CredentialOutcome(ok=False, reason=reason)
    if priority is not None:
        reason = _validate_priority(priority)
        if reason:
            return CredentialOutcome(ok=False, reason=reason)
    if label is None and enabled is None and priority is None:
        return CredentialOutcome(ok=False, reason=REASON_REJECTED)

    data, reason = await _call(RPC_UPDATE, _payload(
        p_owner_id=int(owner_id),
        p_credential_id=str(credential_id).strip(),
        p_label=(str(label).strip() if label is not None else None),
        p_enabled=(bool(enabled) if enabled is not None else None),
        p_priority=(int(priority) if priority is not None else None),
    ))
    if reason:
        return CredentialOutcome(ok=False, reason=reason)
    return _outcome_from(data)


async def delete_credential(owner_id: int, credential_id: str) -> CredentialOutcome:
    """Delete ONE credential and its Vault secret.

    The store removes the secret first (which cascades the metadata row) and only
    then the row, so a success means neither remains; a failed secret removal
    raises in the store and is reported here as a bounded failure rather than a
    false success.
    """
    reason = _validate_owner(owner_id) or _validate_credential_id(credential_id)
    if reason:
        return CredentialOutcome(ok=False, reason=reason)

    data, reason = await _call(RPC_DELETE, _payload(
        p_owner_id=int(owner_id),
        p_credential_id=str(credential_id).strip(),
    ))
    if reason:
        return CredentialOutcome(ok=False, reason=reason)
    if not _scalar_bool(data):
        return CredentialOutcome(ok=False, reason=REASON_NOT_FOUND)
    return CredentialOutcome(ok=True)


def _outcome_from(data: Any) -> CredentialOutcome:
    row = _first(data)
    if row is None:
        return CredentialOutcome(ok=False, reason=REASON_REJECTED)
    metadata = _metadata_from_row(row)
    if metadata is None:
        return CredentialOutcome(ok=False, reason=REASON_FAILED)
    return CredentialOutcome(ok=True, credential=metadata)


async def refresh_provider(provider: str) -> int:
    """Reload ONE provider's credential pool after a management change.

    The pool is the existing bounded, process-local credential cache the runtime
    resolves through, so a credential added, enabled, disabled or removed from
    Telegram is in effect on the very next request with no restart. Never fatal:
    a store that cannot be reached simply leaves the pool as it was.
    """
    from backend.services import stt_credential_pool

    try:
        counts = await stt_credential_pool.prepare((str(provider).strip(),))
    except Exception as exc:  # noqa: BLE001 — a refresh is never fatal
        logger.warning("CREDENTIAL_POOL_REFRESH_FAILED error=%s", type(exc).__name__)
        return 0
    return int(counts.get(str(provider).strip(), 0))


# ── The credential test ────────────────────────────────────────────────────


def _test_classification() -> tuple[frozenset[str], frozenset[str], frozenset[str], frozenset[str]]:
    """The closed classification sets, taken from the adapters' OWN tokens.

    Sourced from the adapters so a renamed token cannot silently reclassify a
    failure; anything not in these sets is ``failed``, which is the fail-closed
    default.
    """
    from backend.services import (
        gemini_media_engine as gemini,
        groq_stt_engine as groq,
        speechmatics_stt_engine as speechmatics,
    )

    unauthorized = frozenset({
        groq.FAILURE_AUTH, groq.FAILURE_FORBIDDEN, groq.FAILURE_MISSING_CREDENTIAL,
        speechmatics.FAILURE_AUTH, speechmatics.FAILURE_FORBIDDEN,
        speechmatics.FAILURE_MISSING_CREDENTIAL,
    })
    rate_limited = frozenset({
        groq.FAILURE_RATE_LIMIT, speechmatics.FAILURE_RATE_LIMIT,
    })
    timed_out = frozenset({
        groq.FAILURE_TIMEOUT, groq.FAILURE_DEADLINE,
        speechmatics.FAILURE_TIMEOUT, speechmatics.FAILURE_DEADLINE,
        gemini.FAILURE_DEADLINE, gemini.FAILURE_UPLOAD_TIMEOUT,
        gemini.FAILURE_INTERACTION_TIMEOUT, gemini.FAILURE_REQUEST_TIMEOUT,
    })
    unavailable = frozenset({
        groq.FAILURE_TRANSPORT, groq.FAILURE_SERVER,
        speechmatics.FAILURE_TRANSPORT, speechmatics.FAILURE_SERVER,
        gemini.FAILURE_TRANSPORT, gemini.FAILURE_UPLOAD,
        gemini.FAILURE_INTERACTION, gemini.FAILURE_REQUEST,
        gemini.FAILURE_FILE_PROCESSING,
    })
    return unauthorized, rate_limited, timed_out, unavailable


def _classify_failure(error: BaseException) -> tuple[str, str]:
    """``(test_state, failure_class)`` for ONE failed adapter request."""
    failure_class = str(getattr(error, "failure_class", "") or "")
    status = 0
    try:
        status = int(getattr(error, "http_status", 0) or 0)
    except (TypeError, ValueError):
        status = 0
    if status in (401, 403):
        return TEST_UNAUTHORIZED, failure_class
    if status == 429:
        return TEST_RATE_LIMITED, failure_class
    try:
        unauthorized, rate_limited, timed_out, unavailable = _test_classification()
    except Exception:  # noqa: BLE001 — classification must never raise
        return TEST_FAILED, failure_class
    if failure_class in unauthorized:
        return TEST_UNAUTHORIZED, failure_class
    if failure_class in rate_limited:
        return TEST_RATE_LIMITED, failure_class
    if failure_class in timed_out:
        return TEST_TIMEOUT, failure_class
    if failure_class in unavailable:
        return TEST_UNAVAILABLE, failure_class
    return TEST_FAILED, failure_class


async def test_credential(owner_id: int, credential_id: str) -> CredentialTestResult:
    """Test whether ``provider`` ACCEPTS this credential, with ONE bounded request.

    Deliberately separate from the provider probe: the payload is the existing
    probe's synthetic tone, so the test proves credential acceptance, transport and
    response parsing — NOT recognition quality — and its result never marks the
    provider (or the STT panel's provider-test state) failed or healthy.

    The credential is resolved through the runtime's OWN credential pool, so the
    test exercises the same path a real request does, and the secret never leaves
    this function.
    """
    metadata = await credential(owner_id, credential_id)
    if metadata is None:
        return CredentialTestResult(state=TEST_NOT_FOUND, reason=REASON_NOT_FOUND)
    if not metadata.enabled:
        return CredentialTestResult(
            state=TEST_DISABLED, credential_id=metadata.credential_id,
            provider=metadata.provider,
        )

    provider = metadata.provider
    candidates = provider_candidates(provider)
    if not candidates:
        return CredentialTestResult(
            state=TEST_NOT_SUPPORTED, credential_id=metadata.credential_id,
            provider=provider,
        )

    from backend.ai import credential_source, stt_provider_probe
    from backend.services import stt_credential_pool, stt_engine_factory

    await refresh_provider(provider)
    wanted_id = f"{credential_source.SOURCE_VAULT}:{metadata.credential_id}"
    record = next(
        (item for item in stt_credential_pool.credentials_for(provider)
         if item.credential_id == wanted_id),
        None,
    )
    if record is None:
        return CredentialTestResult(
            state=TEST_NOT_FOUND, credential_id=metadata.credential_id,
            provider=provider, reason=REASON_NOT_CONFIGURED,
        )

    candidate = candidates[0]
    engine, reason = stt_engine_factory.build_engine_with_credential(candidate, record)
    if engine is None:
        return CredentialTestResult(
            state=TEST_NOT_SUPPORTED, credential_id=metadata.credential_id,
            provider=provider, candidate_id=candidate.candidate_id, reason=reason,
        )

    return await _run_test(engine, metadata, candidate, stt_provider_probe)


async def _run_test(engine: Any, metadata: CredentialMetadata, candidate: Any, probe: Any) -> CredentialTestResult:
    """Run ONE bounded request and classify it, never raising."""
    import asyncio
    import time

    from backend.services.media_service import MediaError

    payload = probe.test_audio_payload()
    started = time.monotonic()
    state, failure_class = TEST_PASSED, ""
    try:
        await asyncio.wait_for(
            asyncio.to_thread(engine.transcribe, payload), timeout=TEST_TIMEOUT_S,
        )
    except asyncio.CancelledError:
        raise
    except asyncio.TimeoutError:
        state, failure_class = TEST_TIMEOUT, "test_timeout"
    except MediaError as exc:
        state, failure_class = _classify_failure(exc)
    except Exception as exc:  # noqa: BLE001 — the adapter boundary
        state, failure_class = TEST_FAILED, type(exc).__name__

    latency_ms = int((time.monotonic() - started) * 1000)
    result = CredentialTestResult(
        state=state,
        credential_id=metadata.credential_id,
        provider=metadata.provider,
        candidate_id=candidate.candidate_id,
        failure_class=failure_class,
        latency_ms=latency_ms,
    )
    record_test(metadata.credential_id, state)
    logger.info(
        "CREDENTIAL_TEST credential=%s provider=%s candidate=%s state=%s "
        "failure_class=%s elapsed_ms=%d",
        metadata.credential_id, metadata.provider, candidate.candidate_id,
        state, failure_class or "-", latency_ms,
    )
    return result


def summarise(credentials: Sequence[CredentialMetadata]) -> dict[str, int]:
    """Bounded counts for a panel line: total / enabled, never a secret."""
    return {
        "total": len(credentials),
        "enabled": sum(1 for item in credentials if item.enabled),
    }


def group_by_provider(
    credentials: Iterable[CredentialMetadata],
) -> dict[str, tuple[CredentialMetadata, ...]]:
    """The owner's credentials grouped by provider, preserving the store's order."""
    grouped: dict[str, list[CredentialMetadata]] = {}
    for item in credentials:
        grouped.setdefault(item.provider, []).append(item)
    return {provider: tuple(items) for provider, items in grouped.items()}
