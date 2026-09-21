"""TTS credential pool + bounded provider fallback.

This suite pins the EXECUTION half of speech-synthesis resilience:

  1. the CREDENTIAL POOL reads the ONE secret boundary (deployment environment
     first, then the Vault RPC), orders it deterministically, and is never fatal;
  2. CLASSIFICATION is fail-closed: a credential-specific failure (a rejected key,
     a per-key quota, a rate limit) rotates INSIDE the provider, while a provider
     condition (5xx, timeout, transport) never burns the rest of the pool and only
     moves to the provider-level fallback;
  3. HEALTH is per credential and separate from provider health, process-local and
     never persisted;
  4. FALLBACK is bounded and deterministic: the selected provider is always tried
     first, at most three providers and six attempts run, and every attempt
     receives only the REMAINING budget;
  5. a DETERMINISTIC failure (an unsupported model or voice, a refused request, a
     malformed or empty response) propagates with its own identity instead of
     being hidden behind a substitute, and an exhausted rotation is reported as
     its own closed-taxonomy class;
  6. NO SECRET reaches a log line, a failure message or a returned value.

Every engine is scripted and the secret backend is a fake, so nothing here is a
statement about synthesis QUALITY or provider health. Live provider verification
was NOT performed.
"""
from __future__ import annotations

import logging
from typing import Any

import pytest

from backend.ai import credential_source, tts_control_plane as plane
from backend.services import (
    openai_tts_engine,
    tts_credential_pool,
    tts_engine_factory,
    tts_fallback,
    tts_service,
)
from backend.services.tts_service import (
    FAILURE_AUTH,
    FAILURE_EMPTY_AUDIO,
    FAILURE_FALLBACK_EXHAUSTED,
    FAILURE_MISSING_CREDENTIAL,
    FAILURE_SERVER,
    FAILURE_TIMEOUT,
    FAILURE_TRANSPORT,
    FAILURE_UNSUPPORTED_VOICE,
    TTS_STAGE_PROVIDER,
    TtsError,
)

PROVIDER = plane.DEFAULT_PROVIDER_ID
MODEL = plane.DEFAULT_MODEL_ID
VOICE = plane.DEFAULT_VOICE_ID
SUBSTITUTE = "second"
SUBSTITUTE_MODEL = "second-model"
SUBSTITUTE_VOICE = "second-voice"

SPOKEN = "این یک آزمون است"

AUDIO = b"OggS\x00\x02\x00\x00\x00\x00\x00\x00\x00\x00OpusHead" + b"\x22" * 32
SUBSTITUTE_AUDIO = b"OggS\x00\x02\x00\x00\x00\x00\x00\x00\x00\x00OpusHead" + b"\x33" * 32

#: Distinctive enough that its appearance anywhere is unambiguous.
SECRET_ENV = "sk-env-tts-4c1f9d2a"
SECRET_VAULT = "sk-vault-tts-77b2e5c3"
VAULT_ID = "tts-a"
CRED_VAULT = f"{credential_source.SOURCE_VAULT}:{VAULT_ID}"
CRED_ENV = f"{credential_source.SOURCE_ENV}:{openai_tts_engine.API_KEY_ENV_VARS[0]}"


# ── Doubles ──────────────────────────────────────────────────────────────────


class _Engine:
    """A scripted synthesis engine: raises or returns, and records its budgets."""

    def __init__(self, provider: str, model: str, voice: str, outcome: Any) -> None:
        self.provider = provider
        self.model = model
        self.voice = voice
        self.mime_type = "audio/ogg"
        self.file_name = "speech.ogg"
        self.key_env_var = "-"
        self._outcome = outcome
        self.budgets: list[float] = []

    async def speak(self, _text: str, *, timeout_s: float = 0.0) -> bytes:
        self.budgets.append(timeout_s)
        if isinstance(self._outcome, BaseException):
            raise self._outcome
        return self._outcome


class _Factory:
    """``build_engine_for`` with a per-(provider, credential) scripted outcome."""

    def __init__(self, outcomes: dict[tuple[str, str], Any] | None = None,
                 default: Any = None) -> None:
        self.outcomes = outcomes or {}
        self.default = default
        self.built: list[tuple[str, str]] = []

    def build_engine_for(self, provider: str, model: str, voice: str, credential: Any = None):
        credential_id = credential.credential_id if credential is not None else ""
        key = (provider, credential_id)
        self.built.append(key)
        outcome = self.outcomes.get(key, self.default)
        if outcome is None:
            return None, FAILURE_MISSING_CREDENTIAL
        return _Engine(provider, model, voice, outcome), ""


def _inject_provider(monkeypatch, provider: str = SUBSTITUTE, *, implemented: bool = True):
    entry = plane.TtsProvider(
        provider=provider,
        label=provider.title(),
        models=(
            plane.TtsModel(
                model_id=f"{provider}-model", label=f"{provider}-model",
                voices=(plane.TtsVoice(voice_id=f"{provider}-voice", label="Voice"),),
                implemented=implemented, output_format="opus", mime_type="audio/ogg",
            ),
        ),
        implemented=implemented, note="test double",
    )
    providers = plane.TTS_PROVIDERS + (entry,)
    monkeypatch.setattr(plane, "TTS_PROVIDERS", providers)
    monkeypatch.setattr(plane, "_BY_PROVIDER", {p.provider: p for p in providers})
    return entry


def _rows(*specs: tuple[str, str, int]) -> tuple[dict[str, Any], ...]:
    return tuple(
        {"credential_id": identifier, "secret": secret, "priority": priority, "enabled": True}
        for identifier, secret, priority in specs
    )


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    """Fresh pool, registration, health and provisioned selection per test."""
    monkeypatch.setattr(tts_service, "_selected", None)
    _set_env(monkeypatch)
    credential_source.reset()
    tts_credential_pool.reset()
    tts_fallback.clear_registration()
    tts_fallback.reset_health()
    yield
    credential_source.reset()
    tts_credential_pool.reset()
    tts_fallback.clear_registration()
    tts_fallback.reset_health()


def _set_env(monkeypatch, value: str = "") -> None:
    for name in openai_tts_engine.API_KEY_ENV_VARS:
        if value:
            monkeypatch.setenv(name, value)
        else:
            monkeypatch.delenv(name, raising=False)


def _plan(monkeypatch, selected: Any, *, provisioned: str = "") -> None:
    """Register the rotation and install the scripted selected engine."""
    monkeypatch.setattr(
        tts_engine_factory, "build_engine", lambda _selection: (selected, ""),
    )
    tts_fallback.register_plan(plane.default_selection(), provisioned_credential_id=provisioned)


# ══ 1. The credential pool ═══════════════════════════════════════════════════


def test_the_provider_variables_are_the_adapters_own_declaration():
    assert tts_credential_pool.env_var_names(PROVIDER) == openai_tts_engine.API_KEY_ENV_VARS
    assert tts_credential_pool.env_var_names("nobody") == (), "no environment sweep"


def test_the_registered_providers_come_from_the_registry():
    assert tts_credential_pool.registered_providers() == plane.provider_ids()


@pytest.mark.asyncio
async def test_the_pool_is_environment_first_then_the_vault_in_priority_order(monkeypatch):
    _set_env(monkeypatch, SECRET_ENV)

    async def _fetch(_provider: str) -> tuple:
        return _rows((VAULT_ID, SECRET_VAULT, 5), ("tts-b", "sk-vault-tts-b", 1))

    monkeypatch.setattr(credential_source, "_fetch_rows", _fetch)

    counts = await tts_credential_pool.prepare((PROVIDER,))

    assert counts[PROVIDER] == 3
    records = tts_credential_pool.credentials_for(PROVIDER)
    assert records[0].credential_id == CRED_ENV
    assert records[0].is_env is True
    assert [r.credential_id for r in records[1:]] == [
        f"{credential_source.SOURCE_VAULT}:tts-b", CRED_VAULT,
    ]
    assert tts_credential_pool.first_for(PROVIDER).credential_id == CRED_ENV


@pytest.mark.asyncio
async def test_a_refusing_secret_backend_leaves_the_environment_credential(monkeypatch):
    _set_env(monkeypatch, SECRET_ENV)

    async def _fetch(_provider: str) -> tuple:
        raise RuntimeError("the secret backend is unavailable")

    monkeypatch.setattr(credential_source, "_fetch_rows", _fetch)

    counts = await tts_credential_pool.prepare((PROVIDER,))

    assert counts[PROVIDER] == 1
    assert tts_credential_pool.credentials_for(PROVIDER)[0].credential_id == CRED_ENV


@pytest.mark.asyncio
async def test_a_credential_in_cooldown_is_skipped_and_recovered(monkeypatch):
    _set_env(monkeypatch, SECRET_ENV)

    async def _fetch(_provider: str) -> tuple:
        return _rows((VAULT_ID, SECRET_VAULT, 9))

    monkeypatch.setattr(credential_source, "_fetch_rows", _fetch)
    await tts_credential_pool.prepare((PROVIDER,))

    assert [r.credential_id for r in tts_credential_pool.rotation_for(PROVIDER)] == [
        CRED_ENV, CRED_VAULT,
    ]
    tts_credential_pool.record_failure(CRED_ENV, FAILURE_AUTH)
    assert tts_credential_pool.is_cooled_down(CRED_ENV) is True
    assert [r.credential_id for r in tts_credential_pool.rotation_for(PROVIDER)] == [CRED_VAULT]

    tts_credential_pool.record_success(CRED_ENV)
    assert tts_credential_pool.credential_health(CRED_ENV).failures == 0
    assert tts_credential_pool.is_cooled_down(CRED_ENV) is False


@pytest.mark.asyncio
async def test_when_every_credential_is_cooling_the_pool_is_still_attempted(monkeypatch):
    _set_env(monkeypatch, SECRET_ENV)

    async def _fetch(_provider: str) -> tuple:
        return _rows((VAULT_ID, SECRET_VAULT, 0))

    monkeypatch.setattr(credential_source, "_fetch_rows", _fetch)
    await tts_credential_pool.prepare((PROVIDER,))
    tts_credential_pool.record_failure(CRED_ENV, FAILURE_AUTH)
    tts_credential_pool.record_failure(CRED_VAULT, FAILURE_AUTH)

    assert len(tts_credential_pool.rotation_for(PROVIDER)) == 2


def _error(failure_class: str, *, status: int = 0, retryable: bool | None = None) -> TtsError:
    return TtsError(
        "scripted", stage=TTS_STAGE_PROVIDER, failure_class=failure_class,
        http_status=status, retryable=retryable,
    )


@pytest.mark.parametrize(
    "failure_class", ["auth", "forbidden", "missing_credential", "rate_limit", "quota_exceeded"],
)
def test_a_credential_class_is_credential_specific(failure_class):
    assert tts_credential_pool.is_credential_specific(_error(failure_class)) is True


@pytest.mark.parametrize(
    "failure_class",
    ["timeout", "transport", "server", "malformed_response", "empty_audio",
     "unsupported_voice", "provider_rejection", FAILURE_FALLBACK_EXHAUSTED],
)
def test_a_provider_class_is_never_credential_specific(failure_class):
    assert tts_credential_pool.is_credential_specific(_error(failure_class)) is False


@pytest.mark.parametrize("status", [401, 403, 429])
def test_the_http_status_alone_identifies_the_credential(status):
    assert tts_credential_pool.is_credential_specific(
        _error("anything", status=status)
    ) is True


def test_a_programming_error_is_never_credential_specific():
    assert tts_credential_pool.is_credential_specific(RuntimeError("boom")) is False


def test_the_pool_description_names_no_secret():
    assert tts_credential_pool.describe("nobody") == "unconfigured"


# ══ 2. The bounded attempt plan ══════════════════════════════════════════════


@pytest.mark.asyncio
async def test_no_registered_rotation_keeps_the_single_engine_path(monkeypatch):
    """Unconfigured installations behave exactly as before the fallback existed."""
    engine = _Engine(PROVIDER, MODEL, VOICE, AUDIO)
    monkeypatch.setattr(tts_engine_factory, "build_engine", lambda _s: (engine, ""))

    clip = await tts_service.synthesize(SPOKEN)

    assert clip.audio == AUDIO
    assert clip.provider == PROVIDER
    assert engine.budgets == [tts_service.TTS_TIMEOUT_S]


@pytest.mark.asyncio
async def test_a_transient_failure_falls_back_to_the_next_provider(monkeypatch):
    _inject_provider(monkeypatch)
    selected = _Engine(PROVIDER, MODEL, VOICE, _error(FAILURE_TIMEOUT, retryable=True))
    factory = _Factory({(SUBSTITUTE, ""): SUBSTITUTE_AUDIO})
    monkeypatch.setattr(tts_engine_factory, "build_engine_for", factory.build_engine_for)
    _plan(monkeypatch, selected)

    clip = await tts_service.synthesize(SPOKEN)

    assert clip.audio == SUBSTITUTE_AUDIO
    assert clip.provider == SUBSTITUTE
    assert clip.model == f"{SUBSTITUTE}-model"
    assert clip.mime_type == "audio/ogg"
    assert factory.built == [(SUBSTITUTE, "")]


@pytest.mark.asyncio
async def test_a_deterministic_failure_propagates_without_a_substitute(monkeypatch):
    _inject_provider(monkeypatch)
    selected = _Engine(PROVIDER, MODEL, VOICE, _error(FAILURE_UNSUPPORTED_VOICE))
    factory = _Factory({(SUBSTITUTE, ""): SUBSTITUTE_AUDIO})
    monkeypatch.setattr(tts_engine_factory, "build_engine_for", factory.build_engine_for)
    _plan(monkeypatch, selected)

    with pytest.raises(TtsError) as caught:
        await tts_service.synthesize(SPOKEN)

    assert caught.value.failure_class == FAILURE_UNSUPPORTED_VOICE
    assert caught.value.failure_class != FAILURE_FALLBACK_EXHAUSTED
    assert factory.built == [], "a configuration error must not be hidden behind a substitute"


@pytest.mark.asyncio
async def test_a_credential_failure_rotates_inside_the_provider(monkeypatch):
    _set_env(monkeypatch, SECRET_ENV)

    async def _fetch(_provider: str) -> tuple:
        return _rows((VAULT_ID, SECRET_VAULT, 9))

    monkeypatch.setattr(credential_source, "_fetch_rows", _fetch)
    await tts_credential_pool.prepare((PROVIDER,))

    # The runtime's engine is ALWAYS provisioned from the pool's first credential
    # (``tts_engine_factory.provisioning_credential``), so that credential's
    # rejection is what makes the pool rotate.
    selected = _Engine(PROVIDER, MODEL, VOICE, _error(FAILURE_AUTH))
    factory = _Factory({(PROVIDER, CRED_VAULT): AUDIO})
    monkeypatch.setattr(tts_engine_factory, "build_engine_for", factory.build_engine_for)
    _plan(monkeypatch, selected, provisioned=CRED_ENV)

    clip = await tts_service.synthesize(SPOKEN)

    assert clip.audio == AUDIO
    assert factory.built == [(PROVIDER, CRED_VAULT)]
    assert tts_credential_pool.credential_health(CRED_ENV).failures == 1
    assert tts_credential_pool.credential_health(CRED_VAULT).failures == 0


@pytest.mark.asyncio
async def test_a_provider_failure_never_burns_the_rest_of_the_pool(monkeypatch):
    _set_env(monkeypatch, SECRET_ENV)

    async def _fetch(_provider: str) -> tuple:
        return _rows((VAULT_ID, SECRET_VAULT, 9))

    monkeypatch.setattr(credential_source, "_fetch_rows", _fetch)
    await tts_credential_pool.prepare((PROVIDER,))

    selected = _Engine(PROVIDER, MODEL, VOICE, _error(FAILURE_SERVER, retryable=True))
    factory = _Factory({(PROVIDER, CRED_VAULT): AUDIO})
    monkeypatch.setattr(tts_engine_factory, "build_engine_for", factory.build_engine_for)
    _plan(monkeypatch, selected, provisioned=CRED_ENV)

    with pytest.raises(TtsError):
        await tts_service.synthesize(SPOKEN)

    assert factory.built == [], "a provider-wide failure spends no other key"
    assert tts_credential_pool.credential_health(CRED_ENV).failures == 0
    assert tts_credential_pool.credential_health(CRED_VAULT).failures == 0


@pytest.mark.asyncio
async def test_an_exhausted_rotation_is_reported_as_its_own_class(monkeypatch):
    _inject_provider(monkeypatch)
    selected = _Engine(PROVIDER, MODEL, VOICE, _error(FAILURE_TRANSPORT, retryable=True))
    factory = _Factory({(SUBSTITUTE, ""): _error(FAILURE_TRANSPORT, retryable=True)})
    monkeypatch.setattr(tts_engine_factory, "build_engine_for", factory.build_engine_for)
    _plan(monkeypatch, selected)

    with pytest.raises(TtsError) as caught:
        await tts_service.synthesize(SPOKEN)

    assert caught.value.failure_class == FAILURE_FALLBACK_EXHAUSTED
    assert caught.value.retryable is False
    assert caught.value.failure_class in tts_service.FAILURE_CLASSES


@pytest.mark.asyncio
async def test_a_single_runnable_provider_keeps_its_own_failure_identity(monkeypatch):
    """No substitute ran, so the SELECTED provider's diagnosis is not rewritten."""
    selected = _Engine(PROVIDER, MODEL, VOICE, _error(FAILURE_TRANSPORT, retryable=True))
    _plan(monkeypatch, selected)

    with pytest.raises(TtsError) as caught:
        await tts_service.synthesize(SPOKEN)

    assert caught.value.failure_class == FAILURE_TRANSPORT


@pytest.mark.asyncio
async def test_an_unbuildable_substitute_is_skipped_and_never_counted(monkeypatch):
    _inject_provider(monkeypatch)
    selected = _Engine(PROVIDER, MODEL, VOICE, _error(FAILURE_TRANSPORT, retryable=True))
    factory = _Factory(default=None)  # nothing buildable
    monkeypatch.setattr(tts_engine_factory, "build_engine_for", factory.build_engine_for)
    _plan(monkeypatch, selected)

    with pytest.raises(TtsError) as caught:
        await tts_service.synthesize(SPOKEN)

    assert factory.built == [(SUBSTITUTE, "")]
    assert caught.value.failure_class == FAILURE_TRANSPORT


@pytest.mark.asyncio
async def test_every_attempt_shares_one_budget(monkeypatch):
    _inject_provider(monkeypatch)
    selected = _Engine(PROVIDER, MODEL, VOICE, _error(FAILURE_TIMEOUT, retryable=True))
    substitute = _Engine(SUBSTITUTE, SUBSTITUTE_MODEL, SUBSTITUTE_VOICE,
                         _error(FAILURE_TIMEOUT, retryable=True))
    monkeypatch.setattr(
        tts_engine_factory, "build_engine_for",
        lambda *_a, **_k: (substitute, ""),
    )
    _plan(monkeypatch, selected)

    with pytest.raises(TtsError):
        await tts_service.synthesize(SPOKEN, timeout_s=30.0)

    assert selected.budgets and substitute.budgets
    assert max(selected.budgets) <= 30.0
    assert max(substitute.budgets) <= 30.0
    assert substitute.budgets[0] <= selected.budgets[0], "no fresh budget for a later attempt"


@pytest.mark.asyncio
async def test_the_attempt_ceiling_is_finite(monkeypatch):
    """A provider that always fails cannot turn one request into a sweep."""
    for index in range(5):
        _inject_provider(monkeypatch, f"p{index}")

    selected = _Engine(PROVIDER, MODEL, VOICE, _error(FAILURE_TRANSPORT, retryable=True))
    factory = _Factory(default=_error(FAILURE_TRANSPORT, retryable=True))
    monkeypatch.setattr(tts_engine_factory, "build_engine_for", factory.build_engine_for)
    _plan(monkeypatch, selected)

    with pytest.raises(TtsError):
        await tts_service.synthesize(SPOKEN)

    attempts = 1 + len(factory.built)
    assert attempts <= tts_fallback.MAX_TOTAL_ATTEMPTS
    assert len(factory.built) <= tts_fallback.MAX_PROVIDER_ATTEMPTS - 1


@pytest.mark.asyncio
async def test_a_provider_in_cooldown_leaves_the_fallback_rotation(monkeypatch):
    _inject_provider(monkeypatch)
    selected = _Engine(PROVIDER, MODEL, VOICE, _error(FAILURE_TRANSPORT, retryable=True))
    factory = _Factory(default=_error(FAILURE_TRANSPORT, retryable=True))
    monkeypatch.setattr(tts_engine_factory, "build_engine_for", factory.build_engine_for)
    _plan(monkeypatch, selected)

    with pytest.raises(TtsError):
        await tts_service.synthesize(SPOKEN)
    assert tts_fallback.is_cooled_down(SUBSTITUTE) is True

    factory.built.clear()
    tts_fallback.clear_registration()
    _plan(monkeypatch, selected)
    with pytest.raises(TtsError):
        await tts_service.synthesize(SPOKEN)

    assert factory.built == [], "a cooling provider is not attempted again"


@pytest.mark.asyncio
async def test_a_missing_credential_is_reported_before_any_attempt(monkeypatch):
    monkeypatch.setattr(
        tts_engine_factory, "build_engine",
        lambda _s: (None, FAILURE_MISSING_CREDENTIAL),
    )
    with pytest.raises(TtsError) as caught:
        await tts_service.synthesize(SPOKEN)
    assert caught.value.failure_class == FAILURE_MISSING_CREDENTIAL
    assert caught.value.stage == tts_service.TTS_STAGE_CREDENTIAL


def test_the_provider_and_credential_ceilings_are_finite_and_documented():
    assert tts_fallback.MAX_PROVIDER_ATTEMPTS == 3
    assert tts_fallback.MAX_TOTAL_ATTEMPTS == 6
    assert tts_fallback.MIN_ATTEMPT_S > 0
    assert tts_credential_pool.MAX_CREDENTIAL_ATTEMPTS_PER_PROVIDER == 3
    assert tts_credential_pool.COOLDOWN_MAX_S > tts_credential_pool.COOLDOWN_BASE_S


def test_an_empty_response_is_a_deterministic_failure():
    """The output leg's own class is not fallback-eligible metadata."""
    assert FAILURE_EMPTY_AUDIO in tts_service.FAILURE_CLASSES
    assert tts_fallback.fallback_eligible(_error(FAILURE_EMPTY_AUDIO)) is False


def test_the_exhaustion_class_is_part_of_the_closed_taxonomy():
    assert FAILURE_FALLBACK_EXHAUSTED in tts_service.FAILURE_CLASSES
    assert FAILURE_FALLBACK_EXHAUSTED not in tts_service._TRANSIENT_CLASSES


# ══ 3. Security ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_no_secret_reaches_a_log_line_or_a_failure_message(monkeypatch, caplog):
    _set_env(monkeypatch, SECRET_ENV)

    async def _fetch(_provider: str) -> tuple:
        return _rows((VAULT_ID, SECRET_VAULT, 9))

    monkeypatch.setattr(credential_source, "_fetch_rows", _fetch)
    await tts_credential_pool.prepare((PROVIDER,))

    selected = _Engine(PROVIDER, MODEL, VOICE, _error(FAILURE_AUTH))
    factory = _Factory({(PROVIDER, CRED_VAULT): _error(FAILURE_AUTH, status=401)})
    monkeypatch.setattr(tts_engine_factory, "build_engine_for", factory.build_engine_for)
    _plan(monkeypatch, selected, provisioned=CRED_ENV)

    with caplog.at_level(logging.DEBUG):
        with pytest.raises(TtsError) as caught:
            await tts_service.synthesize(SPOKEN)

    blob = caplog.text + str(caught.value)
    assert SECRET_ENV not in blob
    assert SECRET_VAULT not in blob
    assert SPOKEN not in blob
    # The non-secret identifiers ARE what a trace may name.
    assert CRED_ENV in caplog.text


def test_the_pool_and_fallback_modules_share_no_seam_with_speech_recognition():
    import inspect

    for module in (tts_credential_pool, tts_fallback):
        source = inspect.getsource(module)
        for forbidden in (
            "stt_fallback", "stt_credential_pool", "stt_control_plane",
            "stt_engine_factory", "gemini_media_engine", "groq_stt_engine",
            "speechmatics_stt_engine",
        ):
            assert forbidden not in source, f"{module.__name__} references {forbidden}"


def test_the_pool_reads_the_one_secret_boundary():
    import inspect

    source = inspect.getsource(tts_credential_pool)
    assert "credential_source" in source
    assert "db.rpc(" not in source and "get_db(" not in source
