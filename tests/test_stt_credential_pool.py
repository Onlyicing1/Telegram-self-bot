"""
Media Processing M2.4 — the STT CREDENTIAL POOL and rotation inside one provider.

The provider fallback layer already decides WHICH provider is tried; until this
phase a provider had exactly ONE credential, so a rejected, revoked or
rate-limited key failed the whole provider. This suite pins the two new modules
that close that gap — the secret boundary
(``backend/ai/credential_source.py``) and the credential pool
(``backend/services/stt_credential_pool.py``) — and the rotation they drive inside
``backend/services/stt_fallback.py``:

  1. ORDER is deterministic: explicit priority first, then the source order, with
     the deployment's environment credential as the first entry — so an
     installation with one key keeps the pre-pool behaviour byte for byte, and
     nothing rotates randomly or is re-ranked by "quality";
  2. CLASSIFICATION is fail-closed: a credential-specific failure (a rejected key,
     a per-key quota, a rate limit, a credential the backend could not decrypt)
     rotates inside the provider, while a provider condition (5xx, timeout,
     transport, malformed or empty response, a programming error) never burns the
     rest of the pool;
  3. HEALTH is per credential and separate from provider health: a failed
     credential takes its own bounded cooldown, the provider keeps serving through
     its other credentials, and nothing is persisted;
  4. BOUNDS are finite and shared: the provider ceiling is unchanged, the
     credential ceiling and the per-unit total are bounded, and every later
     attempt receives only the REMAINING budget — never a fresh one;
  5. the HIERARCHY composes: credentials inside the selected provider first, then
     the existing provider fallback, whose next provider uses ITS OWN pool;
  6. CHUNKING is untouched: a credential that failed on a later chunk never
     retranscribes the chunks that already succeeded, and a merged transcript
     stays ordered and whole — never a partial transcript;
  7. SECURITY holds at every layer: a raw key never reaches a log line, a
     transcript, a failure message or an error returned to Telegram, nothing is
     persisted outside the approved secret backend, and the environment is never
     scanned for a variable the provider did not declare.

The engines are scripted and the secret backend is a fake, so nothing in this file
says anything about recognition QUALITY, no provider is claimed healthy and no
live Supabase Vault has been configured. Live Telegram verification of this phase
was NOT performed.
"""
from __future__ import annotations

import ast
import asyncio
import inspect
import logging
import pathlib
from typing import Any

import pytest

from backend.ai import credential_source, stt_control_plane
from backend.services import (
    media_service,
    stt_credential_pool,
    stt_engine_factory,
    stt_fallback,
)
from backend.services.media_service import (
    MEDIA_STAGE_STT_ENGINE,
    MEDIA_STAGE_STT_EXHAUSTED,
    MediaError,
)
from tests.test_media_stt import OWNER, _FakeClient, _ogg_opus, _voice_message
from tests.test_media_stt_chunking import _ogg_opus_paged

SPEECHMATICS_ID = "speechmatics:standard"
GROQ_ID = "groq:whisper-large-v3"
SPEECHMATICS = "speechmatics"
GROQ = "groq"
GEMINI = "gemini"

#: Secrets of the fake secret store. Distinctive enough that their appearance in
#: ANY log line, transcript or error message is unambiguous.
SECRET_ENV = "sm-env-key-4c1f9d"
SECRET_A = "sm-vault-key-a-77b2e5"
SECRET_B = "sm-vault-key-b-3d90aa"
SECRET_C = "sm-vault-key-c-1e4f77"
SECRET_G = "groq-vault-key-g-5a2c31"
ALL_SECRETS = (SECRET_ENV, SECRET_A, SECRET_B, SECRET_C, SECRET_G)

VAULT_A = "sm-a"
VAULT_B = "sm-b"
VAULT_C = "sm-c"
VAULT_G = "groq-g"

#: The id the pool gives a credential: the source's prefix plus the owner's own
#: non-secret identifier (or the declared variable's NAME for the environment).
CRED_A = f"{credential_source.SOURCE_VAULT}:{VAULT_A}"
CRED_B = f"{credential_source.SOURCE_VAULT}:{VAULT_B}"
CRED_C = f"{credential_source.SOURCE_VAULT}:{VAULT_C}"
CRED_G = f"{credential_source.SOURCE_VAULT}:{VAULT_G}"
CRED_ENV_SM = f"{credential_source.SOURCE_ENV}:AI_SPEECHMATICS_API_KEY"

SMT_MODULE = pathlib.Path(
    stt_fallback.__file__ if stt_fallback.__file__ else "backend/services/stt_fallback.py"
).read_text()
POOL_MODULE = pathlib.Path(
    stt_credential_pool.__file__ or "backend/services/stt_credential_pool.py"
).read_text()
SOURCE_MODULE = pathlib.Path(
    credential_source.__file__ or "backend/ai/credential_source.py"
).read_text()


# ── The provider environment must not leak in from the host ──


@pytest.fixture(autouse=True)
def _no_ambient_provider_keys(monkeypatch):
    """The credential pool reads the provider's OWN declared variables.

    A host that happens to export one would silently arm a pool inside an
    unrelated test, so every declared name is cleared first and the tests that
    need an environment credential install it explicitly.
    """
    for name in (
        "AI_GEMINI_API_KEY",
        "GEMINI_API_KEY",
        "AI_GROQ_API_KEY",
        "GROQ_API_KEY",
        "AI_SPEECHMATICS_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)


# ── Scripted engines, engines per (candidate, credential), attempt recorder ──


class _Engine:
    """One scripted engine: one outcome per call, then ``default``."""

    def __init__(self, *outcomes: Any, default: str = "") -> None:
        self.outcomes = list(outcomes)
        self.default = default
        self.calls: list[bytes] = []

    def transcribe(self, audio: bytes) -> str:
        self.calls.append(audio)
        if not self.outcomes:
            return self.default
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def _provider_failure(
    failure_class: str = "timeout",
    *,
    retryable: bool | None = None,
    http_status: int = 0,
    message: str = "the provider failed",
) -> MediaError:
    """A failure shaped exactly like the ones the provider adapters raise."""
    error = MediaError(message, stage=MEDIA_STAGE_STT_ENGINE)
    error.failure_class = failure_class
    if retryable is not None:
        error.retryable = retryable
    if http_status:
        error.http_status = http_status
    return error


class _CredFactory:
    """The candidate × credential → engine seam, scripted and recorded.

    Mirrors ``stt_engine_factory.build_engine_with_credential``'s contract.
    ``asked`` records every pair the runtime requested and ``built`` only the
    pairs an engine was actually constructed for, which is how "the credential of
    the current attempt, and no other, is handed to an adapter" is proven.
    """

    def __init__(self, engines: dict[tuple[str, str], Any]) -> None:
        self.engines = dict(engines)
        self.asked: list[tuple[str, str]] = []
        self.built: list[tuple[str, str]] = []
        self.settings: dict[str, tuple[str, int]] = {}

    def build_engine_with_credential(
        self, candidate: Any, credential: Any, *, language: str = "", passes: int = 1,
    ):
        credential_id = credential.credential_id if credential is not None else ""
        key = (candidate.candidate_id, credential_id)
        self.asked.append(key)
        self.settings[candidate.candidate_id] = (language, passes)
        engine = self.engines.get(key)
        if engine is None:
            return None, "not_implemented"
        self.built.append(key)
        return engine, ""

    def build_engine(self, candidate: Any, *, language: str = "", passes: int = 1):
        """The pre-pool route: the credential-aware seam with no credential."""
        return self.build_engine_with_credential(
            candidate, None, language=language, passes=passes,
        )


class _Attempts:
    """The boundary's attempt primitive, recorded instead of executed."""

    def __init__(self, *, delay: float = 0.0) -> None:
        self.calls: list[tuple[Any, bytes, float, str]] = []
        self.delay = delay

    async def __call__(
        self, engine: Any, data: bytes, timeout_s: float, *, request_id: str = "",
    ) -> str:
        self.calls.append((engine, data, timeout_s, request_id))
        if self.delay:
            await asyncio.sleep(self.delay)
        return engine.transcribe(data)

    @property
    def engines(self) -> list[Any]:
        return [call[0] for call in self.calls]

    @property
    def bounds(self) -> list[float]:
        return [call[2] for call in self.calls]


@pytest.fixture
def factory(monkeypatch):
    """Install a scripted candidate × credential → engine resolver."""

    def _install(engines: dict[tuple[str, str], Any]) -> _CredFactory:
        installed = _CredFactory(engines)
        monkeypatch.setattr(
            stt_engine_factory, "build_engine_with_credential",
            installed.build_engine_with_credential,
        )
        monkeypatch.setattr(stt_engine_factory, "build_engine", installed.build_engine)
        return installed

    return _install


@pytest.fixture
def vault(monkeypatch):
    """Install a fake secret backend: ``{provider: [rows]}``.

    Patched at the ONE seam between the boundary's cache/ordering logic and its
    transport (``credential_source._fetch_rows``), so the real load path — the
    environment merge, the row validation, the ordering and the cache — is the
    code under test while the database is never reached.
    """

    def _install(pools: dict[str, list[dict[str, Any]]]) -> None:
        async def _fetch(provider: str) -> tuple[Any, ...]:
            return tuple(pools.get(provider, ()))

        monkeypatch.setattr(credential_source, "_fetch_rows", _fetch)

    return _install


@pytest.fixture
def failing_vault(monkeypatch):
    """Install a secret backend that refuses every read."""

    async def _fetch(provider: str) -> tuple[Any, ...]:
        raise RuntimeError("the secret backend is unavailable")

    monkeypatch.setattr(credential_source, "_fetch_rows", _fetch)


@pytest.fixture
def clock(monkeypatch):
    """A controllable monotonic clock for the credential pool."""
    now = [1000.0]
    monkeypatch.setattr(stt_credential_pool, "_clock", lambda: now[0])
    return now


def _row(
    credential_id: str,
    secret: str,
    *,
    priority: int = 0,
    enabled: bool = True,
) -> dict[str, Any]:
    return {
        "credential_id": credential_id,
        "priority": priority,
        "enabled": enabled,
        "secret": secret,
    }


def _arm(
    active_id: str, provisioned_credential_id: str = "",
) -> stt_control_plane.SttControlPlane:
    """Arm the rotation the way the engine factory does."""
    plane = stt_control_plane.parse_stt_config(
        {stt_control_plane.STORAGE_KEY_ACTIVE: stt_control_plane.storage_value(active_id)}
    )
    stt_fallback.register_plan(
        plane, provisioned_credential_id=provisioned_credential_id,
    )
    return plane


def _plan(selected_engine: Any) -> stt_fallback.AttemptPlan:
    plan = stt_fallback.attempt_plan(selected_engine)
    assert plan is not None, "the rotation must be armed for this plan to exist"
    return plan


# ══ 1. Ordering: deterministic, bounded, environment-first ══


@pytest.mark.asyncio
async def test_the_environment_credential_is_the_first_and_only_default(monkeypatch, vault):
    vault({})
    monkeypatch.setenv("AI_SPEECHMATICS_API_KEY", SECRET_ENV)

    await stt_credential_pool.prepare()

    records = stt_credential_pool.credentials_for(SPEECHMATICS)
    assert [record.credential_id for record in records] == [CRED_ENV_SM]
    assert records[0].is_env is True
    assert records[0].source == credential_source.SOURCE_ENV
    assert stt_credential_pool.first_for(SPEECHMATICS) is records[0]


@pytest.mark.asyncio
async def test_the_environment_is_never_scanned_for_an_undeclared_name(monkeypatch, vault):
    """Only the provider's OWN declared variables count — never a numbered list."""
    vault({})
    monkeypatch.setenv("SPEECHMATICS_KEY_1", SECRET_A)
    monkeypatch.setenv("SPEECHMATICS_API_KEY_2", SECRET_B)
    monkeypatch.setenv("SM_KEY_3", SECRET_C)

    await stt_credential_pool.prepare()

    assert stt_credential_pool.credentials_for(SPEECHMATICS) == ()
    assert stt_credential_pool.first_for(SPEECHMATICS) is None


@pytest.mark.asyncio
async def test_the_deployment_credential_is_first_and_the_pool_follows(monkeypatch, vault):
    monkeypatch.setenv("AI_SPEECHMATICS_API_KEY", SECRET_ENV)
    vault({SPEECHMATICS: [_row(VAULT_A, SECRET_A), _row(VAULT_B, SECRET_B)]})

    await stt_credential_pool.prepare()

    assert [
        record.credential_id
        for record in stt_credential_pool.credentials_for(SPEECHMATICS)
    ] == [CRED_ENV_SM, CRED_A, CRED_B]


@pytest.mark.asyncio
async def test_an_explicit_priority_outranks_the_source_order(vault):
    vault({
        SPEECHMATICS: [
            _row(VAULT_A, SECRET_A, priority=5),
            _row(VAULT_B, SECRET_B, priority=-1),
            _row(VAULT_C, SECRET_C, priority=5),
        ]
    })

    await stt_credential_pool.prepare()

    assert [
        record.credential_id
        for record in stt_credential_pool.credentials_for(SPEECHMATICS)
    ] == [CRED_B, CRED_A, CRED_C]


@pytest.mark.asyncio
async def test_a_disabled_or_incomplete_row_contributes_nothing(vault):
    vault({
        SPEECHMATICS: [
            _row(VAULT_A, SECRET_A),
            _row(VAULT_B, SECRET_B, enabled=False),
            _row("", SECRET_C),
            _row("sm c/../x", SECRET_C),
            _row(VAULT_C, ""),
            "not-a-row",
        ]
    })

    await stt_credential_pool.prepare()

    assert [
        record.credential_id
        for record in stt_credential_pool.credentials_for(SPEECHMATICS)
    ] == [CRED_A]


@pytest.mark.asyncio
async def test_the_pool_is_bounded_per_provider(vault):
    vault({SPEECHMATICS: [_row(f"sm-{i}", f"{SECRET_A}-{i}") for i in range(12)]})

    await stt_credential_pool.prepare()

    records = stt_credential_pool.credentials_for(SPEECHMATICS)
    assert len(records) == credential_source.MAX_CREDENTIALS_PER_PROVIDER
    assert credential_source.MAX_CREDENTIALS_PER_PROVIDER <= 8


@pytest.mark.asyncio
async def test_a_refusing_secret_backend_keeps_the_environment_credential(
    monkeypatch, failing_vault
):
    """Fail-closed on the OPTIONAL source: the pre-existing key keeps working."""
    monkeypatch.setenv("AI_SPEECHMATICS_API_KEY", SECRET_ENV)

    await stt_credential_pool.prepare()

    assert stt_credential_pool.is_configured(SPEECHMATICS) is True
    assert [
        record.credential_id
        for record in stt_credential_pool.credentials_for(SPEECHMATICS)
    ] == [CRED_ENV_SM]


@pytest.mark.asyncio
async def test_an_unloaded_provider_is_not_configured(vault):
    vault({})

    assert stt_credential_pool.is_configured(SPEECHMATICS) is False
    assert stt_credential_pool.credentials_for(SPEECHMATICS) == ()
    assert stt_credential_pool.first_for(SPEECHMATICS) is None

    await stt_credential_pool.prepare()

    assert stt_credential_pool.is_configured(SPEECHMATICS) is True
    assert stt_credential_pool.credentials_for(SPEECHMATICS) == ()


@pytest.mark.asyncio
async def test_a_stale_marking_never_drops_the_runtime_s_credentials(vault):
    """A failure must never leave the runtime with FEWER credentials than before."""
    vault({SPEECHMATICS: [_row(VAULT_A, SECRET_A), _row(VAULT_B, SECRET_B)]})
    await stt_credential_pool.prepare()

    stt_credential_pool.mark_source_stale(SPEECHMATICS)

    assert len(stt_credential_pool.credentials_for(SPEECHMATICS)) == 2


@pytest.mark.asyncio
async def test_the_pool_description_never_carries_a_secret(vault):
    vault({SPEECHMATICS: [_row(VAULT_A, SECRET_A)]})
    await stt_credential_pool.prepare()

    described = stt_credential_pool.describe(SPEECHMATICS)

    assert CRED_A in described and credential_source.SOURCE_VAULT in described
    for secret in ALL_SECRETS:
        assert secret not in described
    assert stt_credential_pool.describe("unknown-provider") == "unconfigured"


def test_an_unconfigured_database_yields_no_vault_credentials(monkeypatch):
    """No Supabase client → no Vault read, and nothing raises."""
    monkeypatch.setattr("backend.db.client.get_db", lambda: None)

    assert credential_source._vault_rows_sync(SPEECHMATICS) == ()


def test_the_vault_read_uses_the_one_documented_contract(monkeypatch):
    """The RPC name and the parameter are the contract the user implements."""
    calls: list[tuple[str, Any]] = []

    class _FakeDb:
        def rpc(self, name: str, params: Any) -> "_FakeDb":
            calls.append((name, params))
            return self

        def execute(self) -> Any:
            return type("_Response", (), {"data": [_row(VAULT_A, SECRET_A)]})()

    monkeypatch.setattr("backend.db.client.get_db", lambda: _FakeDb())

    assert credential_source._vault_rows_sync(SPEECHMATICS) == (
        _row(VAULT_A, SECRET_A),
    )
    assert calls == [(credential_source.VAULT_RPC, {"p_provider": SPEECHMATICS})]


# ══ 2. Classification: credential problem vs provider problem ══


@pytest.mark.parametrize(
    "failure_class",
    ["auth", "forbidden", "missing_credential", "rate_limit", "quota_exceeded"],
)
def test_a_credential_class_is_credential_specific(failure_class):
    assert stt_credential_pool.is_credential_specific(
        _provider_failure(failure_class)
    ) is True


@pytest.mark.parametrize(
    "failure_class",
    [
        "server",
        "timeout",
        "transport",
        "transport_failure",
        "upload_failed",
        "file_processing",
        "malformed_response",
        "empty_transcription",
        "provider_rejection",
        "unsupported_audio",
        "unsupported_model",
        "operation_deadline",
        "something_a_future_adapter_invented",
    ],
)
def test_a_provider_class_is_never_credential_specific(failure_class):
    assert stt_credential_pool.is_credential_specific(
        _provider_failure(failure_class)
    ) is False


@pytest.mark.parametrize("status", [401, 403, 429])
def test_the_http_status_classifies_what_an_adapter_did_not(status):
    """Gemini reports a rejection as ``http_rejection``; its status is the classifier."""
    assert stt_credential_pool.is_credential_specific(
        _provider_failure("http_rejection", http_status=status)
    ) is True


@pytest.mark.parametrize("status", [500, 503, 504, 400, 404])
def test_a_provider_outage_is_never_a_credential_problem(status):
    assert stt_credential_pool.is_credential_specific(
        _provider_failure("server", http_status=status, retryable=True)
    ) is False


@pytest.mark.parametrize("error", [RuntimeError("boom"), ValueError("x"), KeyError("k")])
def test_a_programming_error_is_never_credential_specific(error):
    """A bug in the engine is not a bad key: it must never burn the pool."""
    assert stt_credential_pool.is_credential_specific(error) is False


def test_a_bare_media_error_is_not_credential_specific():
    assert stt_credential_pool.is_credential_specific(MediaError("bare")) is False
    assert stt_credential_pool.failure_class_of(MediaError("bare")) == "unknown"
    assert stt_credential_pool.failure_class_of(
        _provider_failure("server")
    ) == "server"


# ══ 3. Credential health: its own cooldown, separate from the provider ══


def test_a_failed_credential_takes_its_own_bounded_cooldown(clock):
    cooldown = stt_credential_pool.record_failure(CRED_A, "auth")

    assert cooldown == stt_credential_pool.COOLDOWN_BASE_S
    assert stt_credential_pool.is_cooled_down(CRED_A) is True
    assert stt_credential_pool.is_cooled_down(CRED_B) is False
    health = stt_credential_pool.credential_health(CRED_A)
    assert health.failures == 1 and health.last_failure_class == "auth"


def test_the_credential_cooldown_grows_boundedly_and_is_capped(clock):
    seen = [stt_credential_pool.record_failure(CRED_A, "rate_limit") for _ in range(8)]

    assert seen[:3] == [
        stt_credential_pool.COOLDOWN_BASE_S,
        stt_credential_pool.COOLDOWN_BASE_S * 2,
        stt_credential_pool.COOLDOWN_BASE_S * 4,
    ]
    assert seen[-1] == stt_credential_pool.COOLDOWN_MAX_S


def test_a_success_restores_the_credential_immediately(clock):
    stt_credential_pool.record_failure(CRED_A, "auth")
    assert stt_credential_pool.is_cooled_down(CRED_A) is True

    stt_credential_pool.record_success(CRED_A)

    assert stt_credential_pool.is_cooled_down(CRED_A) is False
    health = stt_credential_pool.credential_health(CRED_A)
    assert health.failures == 0
    assert health.last_failure_class == ""
    assert health.last_success_at > 0.0


@pytest.mark.asyncio
async def test_a_cooled_down_credential_leaves_the_rotation(vault, clock):
    vault({SPEECHMATICS: [_row(VAULT_A, SECRET_A), _row(VAULT_B, SECRET_B)]})
    await stt_credential_pool.prepare()
    stt_credential_pool.record_failure(CRED_A, "auth")

    assert [
        record.credential_id
        for record in stt_credential_pool.rotation_for(SPEECHMATICS)
    ] == [CRED_B]


@pytest.mark.asyncio
async def test_a_cooldown_expiry_restores_eligibility(vault, clock):
    vault({SPEECHMATICS: [_row(VAULT_A, SECRET_A), _row(VAULT_B, SECRET_B)]})
    await stt_credential_pool.prepare()
    stt_credential_pool.record_failure(CRED_A, "auth")

    clock[0] += stt_credential_pool.COOLDOWN_BASE_S + 1.0

    assert stt_credential_pool.is_cooled_down(CRED_A) is False
    assert [
        record.credential_id
        for record in stt_credential_pool.rotation_for(SPEECHMATICS)
    ] == [CRED_A, CRED_B]


@pytest.mark.asyncio
async def test_every_credential_cooling_down_still_attempts_the_provider(vault, clock):
    """A temporary credential condition must never fail the provider outright."""
    vault({SPEECHMATICS: [_row(VAULT_A, SECRET_A), _row(VAULT_B, SECRET_B)]})
    await stt_credential_pool.prepare()
    stt_credential_pool.record_failure(CRED_A, "auth")
    stt_credential_pool.record_failure(CRED_B, "auth")

    assert [
        record.credential_id
        for record in stt_credential_pool.rotation_for(SPEECHMATICS)
    ] == [CRED_A, CRED_B]


def test_credential_health_is_process_local_and_resettable(clock):
    stt_credential_pool.record_failure(CRED_A, "auth")

    stt_credential_pool.reset()

    assert stt_credential_pool.is_cooled_down(CRED_A) is False
    assert stt_credential_pool.credential_health(CRED_A).failures == 0
    assert stt_credential_pool.credentials_for(SPEECHMATICS) == ()


# ══ 4. Rotation inside one provider ══


@pytest.mark.asyncio
async def test_the_first_credential_serves_the_request_alone(vault, factory):
    vault({SPEECHMATICS: [_row(VAULT_A, SECRET_A), _row(VAULT_B, SECRET_B)]})
    await stt_credential_pool.prepare()
    selected = _Engine(default="transcript")
    installed = factory({})
    _arm(SPEECHMATICS_ID, CRED_A)

    text = await _plan(selected).run(b"audio", 30.0, _Attempts(), request_id="r1")

    assert text == "transcript"
    assert installed.asked == [], "a healthy credential never even asks for another"


@pytest.mark.asyncio
async def test_a_rejected_credential_rotates_to_the_next_one(vault, factory):
    vault({SPEECHMATICS: [_row(VAULT_A, SECRET_A), _row(VAULT_B, SECRET_B)]})
    await stt_credential_pool.prepare()
    selected = _Engine(_provider_failure("auth", retryable=False, message="bad key"))
    substitute = _Engine(default="second credential")
    installed = factory({(SPEECHMATICS_ID, CRED_B): substitute})
    _arm(SPEECHMATICS_ID, CRED_A)
    attempts = _Attempts()

    text = await _plan(selected).run(b"audio", 30.0, attempts, request_id="r1")

    assert text == "second credential"
    assert attempts.engines == [selected, substitute]
    assert installed.built == [(SPEECHMATICS_ID, CRED_B)]
    assert stt_credential_pool.is_cooled_down(CRED_A) is True
    assert stt_credential_pool.is_cooled_down(CRED_B) is False


@pytest.mark.asyncio
async def test_the_first_two_credentials_fail_and_the_third_succeeds(vault, factory):
    vault({
        SPEECHMATICS: [
            _row(VAULT_A, SECRET_A),
            _row(VAULT_B, SECRET_B),
            _row(VAULT_C, SECRET_C),
        ]
    })
    await stt_credential_pool.prepare()
    selected = _Engine(_provider_failure("auth"))
    second = _Engine(_provider_failure("forbidden"))
    third = _Engine(default="third credential")
    installed = factory({
        (SPEECHMATICS_ID, CRED_B): second,
        (SPEECHMATICS_ID, CRED_C): third,
    })
    _arm(SPEECHMATICS_ID, CRED_A)

    text = await _plan(selected).run(b"audio", 60.0, _Attempts(), request_id="r1")

    assert text == "third credential"
    assert installed.built == [(SPEECHMATICS_ID, CRED_B), (SPEECHMATICS_ID, CRED_C)]
    assert len(selected.calls) == len(second.calls) == len(third.calls) == 1


@pytest.mark.asyncio
async def test_a_rate_limited_credential_rotates_before_the_provider_layer(vault, factory):
    """A per-key quota is a CREDENTIAL condition: rotate, do not leave the provider."""
    vault({
        SPEECHMATICS: [_row(VAULT_A, SECRET_A), _row(VAULT_B, SECRET_B)],
        GROQ: [_row(VAULT_G, SECRET_G)],
    })
    await stt_credential_pool.prepare()
    selected = _Engine(_provider_failure("rate_limit", retryable=True))
    substitute = _Engine(default="after the 429")
    installed = factory({
        (SPEECHMATICS_ID, CRED_B): substitute,
        (GROQ_ID, CRED_G): _Engine(default="a different provider"),
    })
    _arm(SPEECHMATICS_ID, CRED_A)

    text = await _plan(selected).run(b"audio", 30.0, _Attempts(), request_id="r1")

    assert text == "after the 429"
    assert installed.built == [(SPEECHMATICS_ID, CRED_B)]


@pytest.mark.asyncio
async def test_a_credential_the_backend_could_not_decrypt_rotates(vault, factory):
    vault({SPEECHMATICS: [_row(VAULT_A, SECRET_A), _row(VAULT_B, SECRET_B)]})
    await stt_credential_pool.prepare()
    selected = _Engine(_provider_failure("missing_credential"))
    substitute = _Engine(default="healthy")
    factory({(SPEECHMATICS_ID, CRED_B): substitute})
    _arm(SPEECHMATICS_ID, CRED_A)

    assert await _plan(selected).run(b"audio", 30.0, _Attempts()) == "healthy"


@pytest.mark.asyncio
async def test_a_provider_outage_does_not_burn_the_pool(vault, factory):
    """A 503 is the PROVIDER's condition: the remaining credentials would fail too."""
    vault({
        SPEECHMATICS: [_row(VAULT_A, SECRET_A), _row(VAULT_B, SECRET_B)],
        GROQ: [_row(VAULT_G, SECRET_G)],
    })
    await stt_credential_pool.prepare()
    selected = _Engine(_provider_failure("server", http_status=503, retryable=True))
    recovered = _Engine(default="the next provider answered")
    installed = factory({(GROQ_ID, CRED_G): recovered})
    _arm(SPEECHMATICS_ID, CRED_A)
    attempts = _Attempts()

    text = await _plan(selected).run(b"audio", 60.0, attempts, request_id="r1")

    assert text == "the next provider answered"
    assert attempts.engines == [selected, recovered]
    assert installed.built == [(GROQ_ID, CRED_G)], "no other credential was built"
    assert (SPEECHMATICS_ID, CRED_B) not in installed.asked
    assert stt_credential_pool.is_cooled_down(CRED_A) is False


@pytest.mark.asyncio
async def test_a_programming_error_never_rotates_a_credential(vault, factory):
    vault({SPEECHMATICS: [_row(VAULT_A, SECRET_A), _row(VAULT_B, SECRET_B)]})
    await stt_credential_pool.prepare()
    selected = _Engine(RuntimeError("engine exploded"))
    installed = factory({})
    _arm(SPEECHMATICS_ID, CRED_A)
    attempts = _Attempts()

    with pytest.raises(RuntimeError, match="engine exploded"):
        await _plan(selected).run(b"audio", 30.0, attempts, request_id="r1")

    assert attempts.engines == [selected]
    assert installed.asked == []


@pytest.mark.asyncio
async def test_every_credential_failing_reports_exhaustion_with_the_last_reason(
    vault, factory
):
    vault({
        SPEECHMATICS: [_row(VAULT_A, SECRET_A), _row(VAULT_B, SECRET_B)],
        GROQ: [_row(VAULT_G, SECRET_G)],
    })
    await stt_credential_pool.prepare()
    selected = _Engine(_provider_failure("auth", message="first key rejected"))
    second = _Engine(_provider_failure("auth", message="second key rejected"))
    fallback = _Engine(_provider_failure("transport", message="the last provider died"))
    factory({(SPEECHMATICS_ID, CRED_B): second, (GROQ_ID, CRED_G): fallback})
    _arm(SPEECHMATICS_ID, CRED_A)
    attempts = _Attempts()

    with pytest.raises(MediaError) as error:
        await _plan(selected).run(b"audio", 60.0, attempts, request_id="r1")

    assert len(attempts.calls) == 3
    assert error.value.stage == MEDIA_STAGE_STT_EXHAUSTED
    assert error.value.failure_class == stt_fallback.STT_FALLBACK_EXHAUSTED
    assert "the last provider died" in str(error.value)
    assert "audio" not in str(error.value).lower()


@pytest.mark.asyncio
async def test_the_provider_pool_exhaustion_is_handed_to_the_provider_layer(
    vault, factory, caplog
):
    caplog.set_level(logging.INFO)
    vault({
        SPEECHMATICS: [_row(VAULT_A, SECRET_A), _row(VAULT_B, SECRET_B)],
        GROQ: [_row(VAULT_G, SECRET_G)],
    })
    await stt_credential_pool.prepare()
    selected = _Engine(_provider_failure("auth"))
    second = _Engine(_provider_failure("auth"))
    fallback = _Engine(default="provider fallback")
    factory({(SPEECHMATICS_ID, CRED_B): second, (GROQ_ID, CRED_G): fallback})
    _arm(SPEECHMATICS_ID, CRED_A)

    text = await _plan(selected).run(b"audio", 60.0, _Attempts(), request_id="r1")

    assert text == "provider fallback"
    assert (
        f"STT_CREDENTIAL_POOL_EXHAUSTED provider={SPEECHMATICS} credentials=2 "
        "attempts=2"
    ) in caplog.text
    assert (
        f"STT_FALLBACK_POOL_TO_PROVIDER from={SPEECHMATICS_ID} attempts=2"
        in caplog.text
    )
    assert stt_fallback.is_cooled_down(SPEECHMATICS_ID) is True


@pytest.mark.asyncio
async def test_one_bad_credential_leaves_the_provider_healthy(vault, factory):
    vault({SPEECHMATICS: [_row(VAULT_A, SECRET_A), _row(VAULT_B, SECRET_B)]})
    await stt_credential_pool.prepare()
    selected = _Engine(_provider_failure("auth"))
    substitute = _Engine(default="healthy")
    factory({(SPEECHMATICS_ID, CRED_B): substitute})
    _arm(SPEECHMATICS_ID, CRED_A)

    await _plan(selected).run(b"audio", 30.0, _Attempts())

    assert stt_fallback.is_cooled_down(SPEECHMATICS_ID) is False
    registration = stt_fallback.registration()
    assert registration is not None and registration.active_id == SPEECHMATICS_ID


@pytest.mark.asyncio
async def test_the_next_provider_uses_its_own_pool(vault, factory):
    vault({
        SPEECHMATICS: [_row(VAULT_A, SECRET_A), _row(VAULT_B, SECRET_B)],
        GROQ: [_row(VAULT_G, SECRET_G)],
    })
    await stt_credential_pool.prepare()
    selected = _Engine(_provider_failure("auth"))
    second = _Engine(_provider_failure("auth"))
    fallback = _Engine(default="groq's own credential")
    installed = factory({(SPEECHMATICS_ID, CRED_B): second, (GROQ_ID, CRED_G): fallback})
    _arm(SPEECHMATICS_ID, CRED_A)

    text = await _plan(selected).run(b"audio", 60.0, _Attempts())

    assert text == "groq's own credential"
    assert installed.built == [(SPEECHMATICS_ID, CRED_B), (GROQ_ID, CRED_G)]
    assert stt_credential_pool.is_cooled_down(CRED_G) is False


@pytest.mark.asyncio
async def test_a_cooldown_prunes_only_the_credential_rotation(vault, factory):
    """The provider is still attempted first, through its healthy credential."""
    vault({SPEECHMATICS: [_row(VAULT_A, SECRET_A), _row(VAULT_B, SECRET_B)]})
    await stt_credential_pool.prepare()
    stt_credential_pool.record_failure(CRED_A, "auth")
    selected = _Engine(default="unused")
    healthy = _Engine(default="the healthy credential")
    installed = factory({(SPEECHMATICS_ID, CRED_B): healthy})
    _arm(SPEECHMATICS_ID, CRED_A)
    attempts = _Attempts()

    plan = _plan(selected)
    text = await plan.run(b"audio", 30.0, attempts)

    assert plan._order()[0] == SPEECHMATICS_ID, "the selection keeps its priority"
    assert text == "the healthy credential"
    assert attempts.engines == [healthy], "the cooling credential is not even tried"
    assert selected.calls == []
    assert installed.asked == [(SPEECHMATICS_ID, CRED_B)]


@pytest.mark.asyncio
async def test_a_single_configured_credential_keeps_the_pre_pool_behavior(
    monkeypatch, vault, factory
):
    """One key is not a pool: a rejected key keeps its exact pre-M2.4 identity."""
    vault({SPEECHMATICS: []})
    monkeypatch.setenv("AI_SPEECHMATICS_API_KEY", SECRET_ENV)
    await stt_credential_pool.prepare()
    original = _provider_failure("auth", retryable=False, message="bad key")
    selected = _Engine(original)
    installed = factory({})
    _arm(SPEECHMATICS_ID, CRED_ENV_SM)
    attempts = _Attempts()

    with pytest.raises(MediaError) as error:
        await _plan(selected).run(b"audio", 30.0, attempts, request_id="r1")

    assert error.value is original
    assert attempts.engines == [selected]
    assert installed.asked == [], "a single credential is never a credential sweep"


# ══ 5. Bounds and the shared budget ══


@pytest.mark.asyncio
async def test_the_credential_ceiling_per_provider_is_enforced(monkeypatch, vault, factory):
    """A four-entry pool still costs at most three attempts on that provider."""
    vault({
        SPEECHMATICS: [
            _row(VAULT_A, SECRET_A),
            _row(VAULT_B, SECRET_B),
            _row(VAULT_C, SECRET_C),
        ]
    })
    monkeypatch.setenv("AI_SPEECHMATICS_API_KEY", SECRET_ENV)
    await stt_credential_pool.prepare()
    assert len(stt_credential_pool.credentials_for(SPEECHMATICS)) == 4

    installed = factory({
        (SPEECHMATICS_ID, CRED_A): _Engine(_provider_failure("auth")),
        (SPEECHMATICS_ID, CRED_B): _Engine(_provider_failure("auth")),
        (SPEECHMATICS_ID, CRED_C): _Engine(_provider_failure("auth")),
    })
    selected = _Engine(_provider_failure("auth"))
    _arm(SPEECHMATICS_ID, CRED_ENV_SM)
    attempts = _Attempts()

    with pytest.raises(MediaError) as error:
        await _plan(selected).run(b"audio", 60.0, attempts, request_id="r1")

    assert len(attempts.calls) == stt_credential_pool.MAX_CREDENTIAL_ATTEMPTS_PER_PROVIDER
    assert len(installed.built) == 2, "the fourth credential was never reached"
    assert error.value.stage == MEDIA_STAGE_STT_EXHAUSTED
    assert stt_fallback.is_cooled_down(SPEECHMATICS_ID) is True


@pytest.mark.asyncio
async def test_the_total_attempt_ceiling_is_finite_and_shared(monkeypatch, vault, factory):
    """Providers and credential rotations share ONE per-unit ceiling."""
    vault({
        SPEECHMATICS: [
            _row(VAULT_A, SECRET_A),
            _row(VAULT_B, SECRET_B),
            _row(VAULT_C, SECRET_C),
        ],
        GROQ: [
            _row("groq-1", SECRET_G),
            _row("groq-2", f"{SECRET_G}-2"),
            _row("groq-3", f"{SECRET_G}-3"),
        ],
    })
    monkeypatch.setenv("AI_SPEECHMATICS_API_KEY", SECRET_ENV)
    await stt_credential_pool.prepare()
    groq_ids = [
        record.credential_id for record in stt_credential_pool.credentials_for(GROQ)
    ]

    installed = factory({
        (SPEECHMATICS_ID, CRED_A): _Engine(_provider_failure("auth")),
        (SPEECHMATICS_ID, CRED_B): _Engine(_provider_failure("auth")),
        (GROQ_ID, groq_ids[0]): _Engine(_provider_failure("auth")),
        (GROQ_ID, groq_ids[1]): _Engine(_provider_failure("auth")),
        (GROQ_ID, groq_ids[2]): _Engine(_provider_failure("auth")),
    })
    selected = _Engine(_provider_failure("auth"))
    _arm(SPEECHMATICS_ID, CRED_ENV_SM)
    attempts = _Attempts()

    with pytest.raises(MediaError) as error:
        await _plan(selected).run(b"audio", 600.0, attempts, request_id="r1")

    # 3 credentials of the selected provider + 3 of the next one hit the per-unit
    # ceiling exactly: the fourth credential of neither pool is ever reached.
    assert len(attempts.calls) == stt_fallback.MAX_TOTAL_ATTEMPTS
    assert stt_fallback.MAX_TOTAL_ATTEMPTS == 2 * stt_fallback.MAX_PROVIDER_ATTEMPTS
    assert error.value.stage == MEDIA_STAGE_STT_EXHAUSTED
    assert installed.built == [
        (SPEECHMATICS_ID, CRED_A),
        (SPEECHMATICS_ID, CRED_B),
        (GROQ_ID, groq_ids[0]),
        (GROQ_ID, groq_ids[1]),
        (GROQ_ID, groq_ids[2]),
    ]


@pytest.mark.asyncio
async def test_the_remaining_budget_is_propagated_to_every_credential(vault, factory):
    """Credential rotation never resets the deadline: a later attempt gets less."""
    vault({SPEECHMATICS: [_row(VAULT_A, SECRET_A), _row(VAULT_B, SECRET_B)]})
    await stt_credential_pool.prepare()
    selected = _Engine(_provider_failure("auth"))
    substitute = _Engine(default="second")
    factory({(SPEECHMATICS_ID, CRED_B): substitute})
    _arm(SPEECHMATICS_ID, CRED_A)
    attempts = _Attempts(delay=0.05)

    await _plan(selected).run(b"audio", 25.0, attempts, request_id="r1")

    bounds = attempts.bounds
    assert len(bounds) == 2
    assert bounds[1] < bounds[0], "the first attempt's elapsed time is not given back"
    assert bounds[0] <= 25.0 and bounds[1] <= 25.0


@pytest.mark.asyncio
async def test_a_starved_credential_is_never_started(vault, factory, caplog):
    caplog.set_level(logging.INFO)
    vault({SPEECHMATICS: [_row(VAULT_A, SECRET_A), _row(VAULT_B, SECRET_B)]})
    await stt_credential_pool.prepare()
    original = _provider_failure("auth", message="rejected quickly")
    selected = _Engine(original)
    substitute = _Engine(default="second")
    installed = factory({(SPEECHMATICS_ID, CRED_B): substitute})
    _arm(SPEECHMATICS_ID, CRED_A)
    attempts = _Attempts(delay=0.25)

    with pytest.raises(MediaError) as error:
        await _plan(selected).run(
            b"audio", stt_fallback.MIN_ATTEMPT_S + 0.05, attempts, request_id="r1",
        )

    assert error.value is original
    assert len(attempts.calls) == 1
    assert substitute.calls == []
    assert installed.asked == [], "a starved credential is not even constructed"
    assert "STT_FALLBACK_STOPPED reason=insufficient_budget" in caplog.text


def test_the_credential_bounds_are_finite_and_small():
    assert 1 <= stt_credential_pool.MAX_CREDENTIAL_ATTEMPTS_PER_PROVIDER <= 3
    assert 0 < stt_credential_pool.COOLDOWN_BASE_S <= stt_credential_pool.COOLDOWN_MAX_S
    assert stt_fallback.MAX_TOTAL_ATTEMPTS == 2 * stt_fallback.MAX_PROVIDER_ATTEMPTS
    assert credential_source.MAX_CREDENTIALS_PER_PROVIDER <= 8
    assert credential_source.MAX_CACHED_PROVIDERS >= len(
        stt_credential_pool.registered_providers()
    )


# ══ 6. Chunking: no retranscription, one ordered transcript ══


@pytest.mark.asyncio
async def test_a_credential_failure_on_a_later_chunk_keeps_earlier_chunks(vault, factory):
    payload = _ogg_opus_paged(840.0)
    vault({SPEECHMATICS: [_row(VAULT_A, SECRET_A), _row(VAULT_B, SECRET_B)]})
    await stt_credential_pool.prepare()
    first_credential = _Engine("A1", _provider_failure("auth"))
    second_credential = _Engine("B2", "B3")
    factory({(SPEECHMATICS_ID, CRED_B): second_credential})
    _arm(SPEECHMATICS_ID, CRED_A)
    media_service.set_stt_engine(first_credential)

    analysis = await media_service.analyze_media(
        _FakeClient(payload=payload), OWNER, _voice_message(payload),
    )

    assert analysis.content == "A1\nB2\nB3", "the merge stays ordered and whole"
    assert len(first_credential.calls) == 2, "chunk 1 was never retranscribed"
    assert len(second_credential.calls) == 2, "the healthy credential kept the rest"
    assert stt_credential_pool.is_cooled_down(CRED_A) is True
    assert stt_credential_pool.is_cooled_down(CRED_B) is False


@pytest.mark.asyncio
async def test_a_healthy_credential_serves_every_chunk(vault, factory):
    payload = _ogg_opus_paged(840.0)
    vault({SPEECHMATICS: [_row(VAULT_A, SECRET_A), _row(VAULT_B, SECRET_B)]})
    await stt_credential_pool.prepare()
    selected = _Engine("one", "two", "three")
    installed = factory({})
    _arm(SPEECHMATICS_ID, CRED_A)
    media_service.set_stt_engine(selected)

    analysis = await media_service.analyze_media(
        _FakeClient(payload=payload), OWNER, _voice_message(payload),
    )

    assert analysis.content == "one\ntwo\nthree"
    assert len(selected.calls) == 3
    assert installed.asked == [], "no credential switch is paid for without a reason"


@pytest.mark.asyncio
async def test_an_exhausted_pool_mid_recording_never_returns_a_partial_transcript(
    vault, factory
):
    payload = _ogg_opus_paged(840.0)
    vault({SPEECHMATICS: [_row(VAULT_A, SECRET_A), _row(VAULT_B, SECRET_B)]})
    await stt_credential_pool.prepare()
    selected = _Engine("A1", _provider_failure("auth"))
    second = _Engine(_provider_failure("auth"), _provider_failure("auth"))
    factory({(SPEECHMATICS_ID, CRED_B): second})
    _arm(SPEECHMATICS_ID, CRED_A)
    media_service.set_stt_engine(selected)

    with pytest.raises(MediaError) as error:
        await media_service.analyze_media(
            _FakeClient(payload=payload), OWNER, _voice_message(payload),
        )

    assert error.value.stage in (MEDIA_STAGE_STT_EXHAUSTED, MEDIA_STAGE_STT_ENGINE)
    assert "A1" not in str(error.value)


@pytest.mark.asyncio
async def test_a_single_piece_request_rotates_inside_the_boundary(vault, factory):
    """End to end through the media boundary: the pool is used, not re-implemented."""
    payload = _ogg_opus(2.0)
    vault({SPEECHMATICS: [_row(VAULT_A, SECRET_A), _row(VAULT_B, SECRET_B)]})
    await stt_credential_pool.prepare()
    selected = _Engine(_provider_failure("auth"))
    substitute = _Engine(default="the second credential")
    factory({(SPEECHMATICS_ID, CRED_B): substitute})
    _arm(SPEECHMATICS_ID, CRED_A)
    media_service.set_stt_engine(selected)
    client = _FakeClient(payload=payload)

    analysis = await media_service.analyze_media(
        client, OWNER, _voice_message(payload),
    )

    assert analysis.content == "the second credential"
    downloads = [call for call in client.calls if call["op"] == "download_media"]
    assert len(downloads) == 1, "the pool adds no second transfer"
    assert substitute.calls == [payload]


# ══ 7. Security: a secret never leaves the credential record ══


@pytest.mark.asyncio
async def test_no_secret_ever_reaches_a_log_line(vault, factory, caplog):
    caplog.set_level(logging.DEBUG)
    vault({
        SPEECHMATICS: [_row(VAULT_A, SECRET_A), _row(VAULT_B, SECRET_B)],
        GROQ: [_row(VAULT_G, SECRET_G)],
    })
    await stt_credential_pool.prepare()
    selected = _Engine(_provider_failure("auth", message="the key was rejected"))
    second = _Engine(_provider_failure("auth", message="the key was rejected"))
    factory({(SPEECHMATICS_ID, CRED_B): second})
    _arm(SPEECHMATICS_ID, CRED_A)

    with pytest.raises(MediaError):
        await _plan(selected).run(b"audio", 60.0, _Attempts(), request_id="r1")

    logged = caplog.text
    for secret in ALL_SECRETS:
        assert secret not in logged
    assert CRED_A in logged and CRED_B in logged


@pytest.mark.asyncio
async def test_a_successful_rotation_never_logs_a_secret(vault, factory, caplog):
    caplog.set_level(logging.DEBUG)
    vault({SPEECHMATICS: [_row(VAULT_A, SECRET_A), _row(VAULT_B, SECRET_B)]})
    await stt_credential_pool.prepare()
    selected = _Engine(_provider_failure("auth"))
    substitute = _Engine(default="recovered")
    factory({(SPEECHMATICS_ID, CRED_B): substitute})
    _arm(SPEECHMATICS_ID, CRED_A)

    text = await _plan(selected).run(b"audio", 30.0, _Attempts(), request_id="r1")

    assert text == "recovered"
    for secret in ALL_SECRETS:
        assert secret not in caplog.text
    assert f"STT_CREDENTIAL_SUCCESS credential={CRED_B}" in caplog.text


@pytest.mark.asyncio
async def test_the_failure_message_never_carries_a_credential(vault, factory):
    vault({SPEECHMATICS: [_row(VAULT_A, SECRET_A), _row(VAULT_B, SECRET_B)]})
    await stt_credential_pool.prepare()
    selected = _Engine(_provider_failure("auth", message="rejected"))
    second = _Engine(_provider_failure("auth", message="rejected"))
    factory({(SPEECHMATICS_ID, CRED_B): second})
    _arm(SPEECHMATICS_ID, CRED_A)

    with pytest.raises(MediaError) as error:
        await _plan(selected).run(b"audio", 60.0, _Attempts(), request_id="r1")

    for secret in (SECRET_A, SECRET_B):
        assert secret not in str(error.value)


def test_a_provisioned_engine_never_exposes_its_credential():
    """The engine's public surface carries the LABEL, never the secret."""
    credential = credential_source.CredentialRecord(
        credential_id=CRED_B,
        provider=SPEECHMATICS,
        secret=SECRET_B,
        source=credential_source.SOURCE_VAULT,
        order_index=1,
    )

    engine, reason = stt_engine_factory.build_engine_with_credential(
        stt_control_plane.get_candidate(SPEECHMATICS_ID), credential,
    )

    assert reason == "" and engine is not None
    assert engine.key_env_var == "explicit", "a pooled credential has no ENV name"
    assert SECRET_B not in str(engine.key_env_var)
    assert SECRET_B not in repr(engine)


def test_an_environment_credential_keeps_the_adapters_own_resolution(monkeypatch):
    """The deployment's key is resolved by the adapter, exactly as before M2.4."""
    monkeypatch.setenv("AI_SPEECHMATICS_API_KEY", SECRET_ENV)
    credential = credential_source.CredentialRecord(
        credential_id=CRED_ENV_SM,
        provider=SPEECHMATICS,
        secret=SECRET_ENV,
        source=credential_source.SOURCE_ENV,
        order_index=0,
    )

    engine, _reason = stt_engine_factory.build_engine_with_credential(
        stt_control_plane.get_candidate(SPEECHMATICS_ID), credential,
    )

    assert engine is not None
    assert engine.key_env_var == "AI_SPEECHMATICS_API_KEY"


def _imports(source: str) -> list[str]:
    imported: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported += [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)
    return imported


@pytest.mark.parametrize("source", [POOL_MODULE, SOURCE_MODULE])
def test_the_credential_layer_imports_no_transport_or_telegram(source):
    imported = _imports(source)
    banned = (
        "telethon", "telegram", "backend.bot", "httpx", "requests", "subprocess",
        "socket", "config_store",
    )
    assert not [name for name in imported if any(word in name for word in banned)]


def test_the_credential_layer_persists_nothing():
    for source in (POOL_MODULE, SOURCE_MODULE):
        for statement in ("open(", "write_text", "json.dump", "to_thread", "pickle"):
            assert statement not in source
        assert "os.environ" not in source
    assert "getenv" in SOURCE_MODULE


def test_the_rotation_carries_no_telegram_context_by_construction():
    parameters = list(inspect.signature(stt_fallback.AttemptPlan.run).parameters)
    assert parameters == ["self", "audio", "bound_s", "run_engine", "request_id"]
    for forbidden in ("chat", "message", "sender", "caption", "reply", "telegram", "user"):
        assert not any(forbidden in name for name in parameters)
    assert "getenv" not in SMT_MODULE and "os.environ" not in SMT_MODULE


def test_the_pool_never_reaches_a_persisted_store():
    """Credential health and the snapshot are runtime posture, never configuration."""
    for source in (POOL_MODULE, SMT_MODULE):
        assert "supabase" not in source.lower()
        assert "backend.db" not in source


# ══ 8. Provisioning: the engine and the rotation come from ONE configuration ══


@pytest.mark.asyncio
async def test_the_provisioned_engine_records_the_credential_it_uses(monkeypatch, vault):
    monkeypatch.setenv("AI_SPEECHMATICS_API_KEY", SECRET_ENV)
    vault({SPEECHMATICS: [_row(VAULT_A, SECRET_A)]})

    status = await stt_engine_factory.apply_stt_config_async(
        {stt_control_plane.STORAGE_KEY_ACTIVE: SPEECHMATICS_ID}
    )

    engine = media_service.get_stt_engine()
    registration = stt_fallback.registration()
    assert status["configured"] is True
    assert engine is not None
    assert engine.key_env_var == "AI_SPEECHMATICS_API_KEY"
    assert registration is not None
    assert registration.provisioned_credential_id == CRED_ENV_SM
    assert SECRET_ENV not in str(status)


@pytest.mark.asyncio
async def test_an_explicit_priority_vault_credential_is_what_provisioning_uses(vault):
    vault({SPEECHMATICS: [_row(VAULT_B, SECRET_B, priority=-5)]})

    await stt_engine_factory.apply_stt_config_async(
        {stt_control_plane.STORAGE_KEY_ACTIVE: SPEECHMATICS_ID}
    )

    engine = media_service.get_stt_engine()
    registration = stt_fallback.registration()
    assert engine is not None and engine.key_env_var == "explicit"
    assert registration is not None
    assert registration.provisioned_credential_id == CRED_B
    assert SECRET_B not in repr(registration)


@pytest.mark.asyncio
async def test_a_refusing_secret_backend_leaves_provisioning_unchanged(
    monkeypatch, failing_vault
):
    monkeypatch.setenv("AI_SPEECHMATICS_API_KEY", SECRET_ENV)

    status = await stt_engine_factory.apply_stt_config_async(
        {stt_control_plane.STORAGE_KEY_ACTIVE: SPEECHMATICS_ID}
    )

    engine = media_service.get_stt_engine()
    registration = stt_fallback.registration()
    assert status["configured"] is True
    assert engine is not None and engine.key_env_var == "AI_SPEECHMATICS_API_KEY"
    assert registration is not None
    assert registration.provisioned_credential_id == CRED_ENV_SM


@pytest.mark.asyncio
async def test_provisioning_uses_the_same_order_the_rotation_uses(vault):
    """The engine and the first attempt can never disagree about the credential."""
    vault({
        SPEECHMATICS: [
            _row(VAULT_A, SECRET_A, priority=9),
            _row(VAULT_B, SECRET_B, priority=-9),
        ]
    })

    await stt_engine_factory.apply_stt_config_async(
        {stt_control_plane.STORAGE_KEY_ACTIVE: SPEECHMATICS_ID}
    )

    registration = stt_fallback.registration()
    assert registration is not None
    assert registration.provisioned_credential_id == CRED_B
    assert registration.provisioned_credential_id == (
        stt_credential_pool.rotation_for(SPEECHMATICS)[0].credential_id
    )
