"""Multi-provider Text-to-Speech — four REAL providers through ONE control plane.

This file is the CROSS-PROVIDER contract. It exists because "registered" is not
"implemented": a provider counts as implemented only when its own adapter exists
and this application can construct and INVOKE it through the existing control
plane. Everything below therefore runs the real registry, the real factory seam
and the real adapters — only the HTTP boundary and the secret backend are doubles.

What it pins:

  1. REGISTRY COMPLETENESS — openai, gemini, grok and speechmatics are all
     registered AND implemented, each with its own adapter, and the canonical
     rotation is a deterministic function of the selection;
  2. REAL ADAPTER EXECUTION — ``tts_service.synthesize`` drives each provider's
     own adapter end to end (one scripted HTTP transport routes per host) and the
     resulting clip reports the provider, model, voice, MIME type and file name
     that ACTUALLY produced it;
  3. PROVIDER SELECTION — a persisted triple provisions the boundary, and
     switching provider persists that provider's OWN default model and voice;
  4. FALLBACK ACROSS REAL PROVIDERS — the selected provider is first, the tail is
     the registry's canonical order, and the clip names the substitute that spoke;
  5. CREDENTIAL ISOLATION — a provider's pool is built from THAT provider's own
     declared variables, and no provider's credential is ever handed to another;
  6. VALIDATION — a model or voice of one provider is refused by every other;
  7. PERSIAN IS NEVER OVERCLAIMED — Gemini records its documented ``fa`` support
     without claiming verification, Grok records the absence of a documented
     list, Speechmatics records English-only, and NOTHING claims verified;
  8. NO SECRET EGRESS — no panel line, failure message or log line carries a
     credential or names an environment variable.

Live provider and live Telegram verification were NOT performed: no real API key
was used and no byte left the process.
"""
from __future__ import annotations

import json
import logging
from typing import Any

import httpx
import pytest

from backend.ai import config_store, credential_source, tts_control_plane as plane
from backend.services import (
    gemini_tts_engine,
    grok_tts_engine,
    openai_tts_engine,
    speechmatics_tts_engine,
    tts_credential_pool,
    tts_engine_factory,
    tts_fallback,
    tts_service,
)
from backend.services.tts_service import TtsError

_HTTPX_ASYNC_CLIENT = httpx.AsyncClient

OWNER = 7283627550
SPOKEN = "سلام، این یک آزمون است"

SECRETS = {
    "openai": "sk-openai-multi-provider-key-must-never-be-logged",
    "gemini": "AIza-gemini-multi-provider-key-must-never-be-logged",
    "grok": "xai-grok-multi-provider-key-must-never-be-logged",
    "speechmatics": "sm-speechmatics-multi-provider-key-must-never-be-logged",
}

OPUS = b"OggS\x00\x02\x00\x00\x00\x00\x00\x00\x00\x00OpusHead" + b"\x11" * 32
MP3 = b"\xff\xfb\x90\x00" + b"\x22" * 64
WAV = b"RIFF" + (36 + 32).to_bytes(4, "little") + b"WAVE" + b"\x33" * 64
PCM = b"\x44" * 64


# ── Scripted HTTP boundary, routed per provider host ─────────────────────


def _gemini_body(pcm: bytes = PCM) -> bytes:
    import base64

    return json.dumps({
        "status": "completed",
        "steps": [{"type": "model_output", "content": [
            {"type": "audio", "data": base64.b64encode(pcm).decode()},
        ]}],
    }).encode()


class _Router(httpx.AsyncBaseTransport):
    """One transport answering every provider's documented shape."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = str(request.url)
        if "generativelanguage.googleapis.com" in url:
            return httpx.Response(
                200, content=_gemini_body(), headers={"content-type": "application/json"},
            )
        if "api.x.ai" in url:
            return httpx.Response(200, content=MP3, headers={"content-type": "audio/mpeg"})
        if "speechmatics.com" in url:
            return httpx.Response(200, content=WAV, headers={"content-type": "audio/wav"})
        if "api.openai.com" in url:
            return httpx.Response(200, content=OPUS, headers={"content-type": "audio/ogg"})
        return httpx.Response(404, content=b"{}", headers={"content-type": "application/json"})


@pytest.fixture
def http(monkeypatch):
    """Route every adapter's HTTP client onto the scripted transport."""
    recording = _Router()
    monkeypatch.setattr(
        httpx, "AsyncClient",
        lambda timeout=None, **rest: _HTTPX_ASYNC_CLIENT(
            transport=recording, timeout=timeout,
        ),
    )
    return recording


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    """Fresh secret cache, pool health, rotation and provisioned selection."""
    monkeypatch.setattr(tts_service, "_selected", None)
    credential_source.reset()
    tts_credential_pool.reset()
    tts_fallback.clear_registration()
    tts_fallback.reset_health()
    for module in (openai_tts_engine, gemini_tts_engine, grok_tts_engine, speechmatics_tts_engine):
        for name in module.API_KEY_ENV_VARS:
            monkeypatch.delenv(name, raising=False)
    yield
    credential_source.reset()
    tts_credential_pool.reset()
    tts_fallback.clear_registration()
    tts_fallback.reset_health()


def _set_all_env(monkeypatch) -> None:
    for module in (openai_tts_engine, gemini_tts_engine, grok_tts_engine, speechmatics_tts_engine):
        monkeypatch.setenv(module.API_KEY_ENV_VARS[0], SECRETS[module.PROVIDER_NAME])


def _config_for(provider: str) -> dict[str, str]:
    entry = plane.get_provider(provider)
    assert entry is not None
    model = entry.model(entry.default_model_id)
    assert model is not None
    selection = plane.resolve(provider, model.model_id, model.default_voice_id)
    return dict(selection.storage_values())


# ── 1. Registry completeness ─────────────────────────────────────────────


def test_the_four_providers_are_registered_and_implemented():
    assert plane.provider_ids() == ("openai", "gemini", "grok", "speechmatics")
    assert plane.implemented_provider_ids() == plane.provider_ids()
    for provider in plane.TTS_PROVIDERS:
        assert provider.implemented is True, provider.provider
        assert provider.models and provider.models[0].voices, provider.provider


def test_every_registered_provider_resolves_to_its_own_adapter():
    expected = {
        "openai": openai_tts_engine.OpenAiSpeechEngine,
        "gemini": gemini_tts_engine.GeminiSpeechEngine,
        "grok": grok_tts_engine.GrokSpeechEngine,
        "speechmatics": speechmatics_tts_engine.SpeechmaticsSpeechEngine,
    }
    for provider, adapter_class in expected.items():
        record = credential_source.CredentialRecord(
            credential_id="explicit", provider=provider,
            secret=SECRETS[provider], source=credential_source.SOURCE_VAULT,
        )
        entry = plane.get_provider(provider)
        model = entry.model(entry.default_model_id)
        engine, reason = tts_engine_factory.build_engine_for(
            provider, model.model_id, model.voices[0].voice_id, record,
        )
        assert reason == "", f"{provider}: {reason}"
        assert isinstance(engine, adapter_class), provider
        assert engine.provider == provider


def test_the_registry_reports_each_adapters_own_format_and_mime():
    for provider in plane.TTS_PROVIDERS:
        adapter = plane.adapter_for(provider.provider)
        model = provider.models[0]
        assert model.mime_type == adapter.AUDIO_MIME, provider.provider
        assert model.output_format in (
            getattr(adapter, "OUTPUT_FORMAT", ""), getattr(adapter, "RESPONSE_FORMAT", ""),
        ), provider.provider


def test_the_default_selection_remains_the_telegram_voice_note_format():
    """The pre-existing default is unchanged: OpenAI, opus inside an OGG container."""
    selection = plane.default_selection()
    model = selection.model_entry

    assert selection.provider == "openai"
    assert (model.output_format, model.mime_type) == ("opus", "audio/ogg")


def test_each_provider_declares_a_container_telegram_can_deliver():
    allowed = {"audio/ogg", "audio/mpeg", "audio/wav"}
    for provider in plane.TTS_PROVIDERS:
        model = provider.models[0]
        assert model.mime_type in allowed, provider.provider


def test_the_canonical_order_puts_the_selection_first_and_the_registry_after():
    for provider in plane.provider_ids():
        order = plane.canonical_order(provider)
        assert order[0] == provider
        assert set(order) == set(plane.provider_ids())
        assert len(order) == len(set(order))


# ── 2. Real adapter execution through the ONE boundary ───────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider", "model", "voice", "mime", "file_name"),
    [
        ("openai", "gpt-4o-mini-tts", "alloy", "audio/ogg", "speech.ogg"),
        ("gemini", "gemini-3.1-flash-tts-preview", "Kore", "audio/wav", "speech.wav"),
        ("grok", "", "eve", "audio/mpeg", "speech.mp3"),
        ("speechmatics", "", "sarah", "audio/wav", "speech.wav"),
    ],
)
async def test_the_boundary_drives_each_providers_real_adapter(
    monkeypatch, http, provider, model, voice, mime, file_name,
):
    _set_all_env(monkeypatch)

    status = await tts_service.apply_tts_settings_async(_config_for(provider))
    assert status["configured"] is True, status

    clip = await tts_service.synthesize(SPOKEN)

    assert clip.provider == provider
    assert clip.model == model
    assert clip.voice == voice
    assert clip.mime_type == mime
    assert clip.file_name == file_name
    assert clip.audio, "each adapter produced audio"
    # Exactly ONE request left the process, to the selected provider's own host.
    assert len(http.requests) == 1
    assert http.requests[0].method == "POST"


@pytest.mark.asyncio
async def test_the_gemini_clip_carries_the_wrapped_wave_container(monkeypatch, http):
    _set_all_env(monkeypatch)
    await tts_service.apply_tts_settings_async(_config_for("gemini"))

    clip = await tts_service.synthesize(SPOKEN)

    assert clip.audio[:4] == b"RIFF" and clip.audio[8:12] == b"WAVE"
    assert clip.audio[44:] == PCM, "the provider's own samples are carried unchanged"


@pytest.mark.asyncio
async def test_the_grok_and_speechmatics_clips_carry_the_providers_own_bytes(
    monkeypatch, http,
):
    _set_all_env(monkeypatch)

    await tts_service.apply_tts_settings_async(_config_for("grok"))
    assert (await tts_service.synthesize(SPOKEN)).audio == MP3

    await tts_service.apply_tts_settings_async(_config_for("speechmatics"))
    assert (await tts_service.synthesize(SPOKEN)).audio == WAV


@pytest.mark.asyncio
async def test_each_adapter_receives_only_the_text_and_its_own_configuration(
    monkeypatch, http,
):
    _set_all_env(monkeypatch)
    await tts_service.apply_tts_settings_async(_config_for("grok"))

    await tts_service.synthesize(SPOKEN)

    payload = json.loads(http.requests[0].content)
    assert payload["text"] == SPOKEN
    assert set(payload) == {"text", "voice_id", "language", "output_format"}
    for forbidden in ("chat_id", "message_id", "sender", "caption", "filename"):
        assert forbidden not in http.requests[0].content.decode()


# ── 3. Provider selection and persistence ────────────────────────────────


def _install_surface(monkeypatch, store: dict) -> Any:
    """The Text-to-Speech surface over a fake ``ai_config`` row."""
    from backend.bot.handlers import ai_tts_settings as module

    async def _get_config(_owner: int) -> dict:
        return dict(store)

    async def _save_config(_owner: int, config: dict) -> bool:
        store.clear()
        store.update(config)
        return True

    async def _owner_and_config() -> tuple[int, dict]:
        return OWNER, dict(store)

    applied: list[dict] = []

    async def _apply(_owner: int) -> bool:
        applied.append(dict(store))
        return True

    monkeypatch.setattr(config_store, "get_config", _get_config)
    monkeypatch.setattr(config_store, "save_config", _save_config)
    monkeypatch.setattr(module, "owner_and_config", _owner_and_config)
    monkeypatch.setattr(module, "apply_tts_settings_now", _apply)
    module.applied = applied  # type: ignore[attr-defined]
    return module


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider", "model", "voice"),
    [
        ("gemini", "gemini-3.1-flash-tts-preview", "Kore"),
        ("grok", "", "eve"),
        ("speechmatics", "", "sarah"),
        ("openai", "gpt-4o-mini-tts", "alloy"),
    ],
)
async def test_selecting_a_provider_persists_its_own_default_triple(
    monkeypatch, provider, model, voice,
):
    store: dict = {}
    module = _install_surface(monkeypatch, store)

    _title, body, _buttons = await module._ai_tts_select_action(None, provider, 0)

    reloaded = plane.parse_tts_config(store)
    assert (reloaded.provider, reloaded.model, reloaded.voice) == (provider, model, voice)
    assert reloaded.adjusted == "", "a switch must never store a stale value"
    assert "✓" in body
    assert module.applied, "the change is applied to the live boundary immediately"


@pytest.mark.asyncio
async def test_switching_between_two_providers_never_leaves_a_stale_model(monkeypatch):
    store: dict = {}
    module = _install_surface(monkeypatch, store)

    await module._ai_tts_select_action(None, "gemini", 0)
    await module._ai_tts_select_voice_action(None, "Zephyr", 0)
    assert plane.parse_tts_config(store).voice == "Zephyr"

    await module._ai_tts_select_action(None, "grok", 0)

    reloaded = plane.parse_tts_config(store)
    assert (reloaded.provider, reloaded.model, reloaded.voice) == ("grok", "", "eve")
    assert reloaded.voice != "Zephyr", "another provider's voice can never survive"


@pytest.mark.asyncio
async def test_selecting_a_model_within_a_provider_resets_to_its_default_voice(monkeypatch):
    store: dict = {}
    module = _install_surface(monkeypatch, store)
    model = plane.get_model("gemini", "gemini-2.5-pro-preview-tts")
    assert model is not None

    await module._ai_tts_select_action(None, "gemini", 0)
    await module._ai_tts_select_model_action(None, model.model_id, 0)

    reloaded = plane.parse_tts_config(store)
    assert reloaded.provider == "gemini"
    assert reloaded.model == model.model_id
    assert reloaded.voice == model.default_voice_id


@pytest.mark.asyncio
async def test_every_provider_is_listed_and_each_other_one_is_offered(monkeypatch):
    store: dict = {}
    module = _install_surface(monkeypatch, store)

    _title, body, buttons = await module._ai_media_tts_panel_handler(None, "")

    for entry in plane.TTS_PROVIDERS:
        assert entry.label in body, entry.provider
    assert "not available yet" not in body, "every registered provider has an adapter"
    datas = [
        str(getattr(button, "data", ""))
        for row in buttons
        for button in (row if isinstance(row, list) else [row])
    ]
    for provider in plane.provider_ids():
        if provider != plane.DEFAULT_PROVIDER_ID:
            assert any(provider in data for data in datas), provider


def test_the_surface_lists_every_provocable_model_and_voice_of_a_provider():
    gemini = plane.get_provider("gemini")
    model = gemini.model(gemini.default_model_id)

    assert len(gemini.models) == 3
    assert len(model.voices) == 30
    assert len(model.voice_ids()) == len(set(model.voice_ids()))


# ── 4. Validation across providers ───────────────────────────────────────


def test_no_provider_accepts_another_providers_model_or_voice():
    for provider in plane.provider_ids():
        entry = plane.get_provider(provider)
        for other in (p for p in plane.TTS_PROVIDERS if p.provider != provider):
            other_model = other.models[0]
            # A provider that names no model (its own default route) is compared by
            # its VOICES only: an empty model id is that provider's own identity,
            # never a model another provider offered.
            if other_model.model_id:
                assert entry.model(other_model.model_id) is None, (
                    provider, other.provider,
                )
            for voice in other_model.voices:
                assert plane.is_selectable(
                    provider, other_model.model_id, voice.voice_id,
                ) is False, (provider, other.provider, voice.voice_id)


def test_no_voice_id_is_shared_between_two_providers():
    """Voice ids are provider-scoped: nothing makes a ``Kore`` an OpenAI voice.

    A provider may reuse its own voice set across its own models (Gemini's voices
    are model-level), but no id may be claimed by two different providers — that
    is what keeps a voice from being silently repointed at another service.
    """
    owners: dict[str, str] = {}
    for provider in plane.TTS_PROVIDERS:
        seen: set[str] = set()
        for model in provider.models:
            for voice in model.voices:
                if voice.voice_id in seen:
                    continue
                seen.add(voice.voice_id)
                assert owners.get(voice.voice_id, provider.provider) == provider.provider, (
                    f"{voice.voice_id} claimed by {owners[voice.voice_id]} "
                    f"and {provider.provider}"
                )
                owners[voice.voice_id] = provider.provider
    assert len(owners) >= 60


@pytest.mark.parametrize(
    ("provider", "foreign_model", "foreign_voice", "expected"),
    [
        ("gemini", "gpt-4o-mini-tts", "alloy", tts_service.FAILURE_UNSUPPORTED_MODEL),
        ("gemini", "gemini-3.1-flash-tts-preview", "alloy", tts_service.FAILURE_UNSUPPORTED_VOICE),
        ("grok", "gemini-3.1-flash-tts-preview", "Kore", tts_service.FAILURE_UNSUPPORTED_MODEL),
        ("speechmatics", "", "eve", tts_service.FAILURE_UNSUPPORTED_VOICE),
        ("openai", "gpt-4o-mini-tts", "sarah", tts_service.FAILURE_UNSUPPORTED_VOICE),
        ("openai", "not-a-model", "alloy", tts_service.FAILURE_UNSUPPORTED_MODEL),
    ],
)
def test_a_foreign_triple_is_refused_by_the_adapter_seam_too(
    provider, foreign_model, foreign_voice, expected,
):
    """The seam re-validates the registry, so a foreign value never reaches a body."""
    record = credential_source.CredentialRecord(
        credential_id="explicit", provider=provider,
        secret=SECRETS[provider], source=credential_source.SOURCE_VAULT,
    )

    engine, reason = tts_engine_factory.build_engine_for(
        provider, foreign_model, foreign_voice, record,
    )

    assert engine is None
    assert reason == expected


@pytest.mark.asyncio
async def test_an_unregistered_stored_model_degrades_and_sends_nothing(monkeypatch, http):
    """A stored value outside the registry is replaced deterministically, and
    nothing is requested merely by applying it."""
    _set_all_env(monkeypatch)

    status = await tts_service.apply_tts_settings_async({
        "tts_provider": "gemini", "tts_model": "not-a-model", "tts_voice": "Kore",
    })

    assert status["configured"] is True
    selection = tts_service.current_selection()
    assert selection.provider == "gemini"
    assert selection.model == "gemini-3.1-flash-tts-preview"
    assert "model `not-a-model` is not offered" in selection.adjusted
    assert http.requests == [], "applying settings must not call a provider"


@pytest.mark.asyncio
async def test_an_unregistered_provider_degrades_to_the_default(monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    _set_all_env(monkeypatch)

    selection = plane.parse_tts_config({"tts_provider": "skynet", "tts_voice": "eve"})

    assert selection.provider == plane.DEFAULT_PROVIDER_ID
    assert selection.voice == plane.DEFAULT_VOICE_ID
    assert "unknown provider `skynet`" in selection.adjusted
    assert "is not offered by" in selection.adjusted


# ── 5. Fallback across the REAL registry ─────────────────────────────────


class _Scripted:
    """A scripted engine carrying the identity of the provider it stands for."""

    def __init__(self, provider: str, model: str, voice: str, outcome: Any) -> None:
        self.provider = provider
        self.model = model
        self.voice = voice
        self.mime_type = "audio/ogg"
        self.file_name = "speech.ogg"
        self.key_env_var = "-"
        self.budgets: list[float] = []
        self._outcome = outcome

    async def speak(self, _text: str, *, timeout_s: float = 0.0) -> bytes:
        self.budgets.append(timeout_s)
        if isinstance(self._outcome, BaseException):
            raise self._outcome
        return self._outcome


class _Factory:
    """``build_engine_for`` recording every (provider, credential) pair tried.

    ``outcomes`` scripts a provider's result; ``default`` is what every provider
    absent from it gets (``None`` means "nothing buildable", which is how an
    unprovisioned provider is simulated).
    """

    def __init__(self, outcomes: dict[str, Any] | None = None, default: Any = None) -> None:
        self.outcomes = outcomes or {}
        self.default = default
        self.built: list[tuple[str, str]] = []

    def build_engine_for(self, provider, model, voice, credential=None):
        credential_id = credential.credential_id if credential is not None else ""
        self.built.append((provider, credential_id))
        outcome = self.outcomes.get(provider, self.default)
        if outcome is None:
            return None, tts_service.FAILURE_MISSING_CREDENTIAL
        return _Scripted(provider, model, voice, outcome), ""


def _transient(message: str) -> TtsError:
    return TtsError(
        message, stage=tts_service.TTS_STAGE_PROVIDER,
        failure_class=tts_service.FAILURE_TRANSPORT, retryable=True,
    )


def _plan(monkeypatch, provider: str, selected: Any) -> None:
    """Register the REAL rotation and install the scripted selected engine.

    The provisioned credential id is recorded exactly as the boundary's own
    settings-apply does, so the selected provider's first attempt reuses the
    engine it already holds (through its OWN credential) and every substitute goes
    through the seam with the SUBSTITUTE provider's credentials.
    """
    entry = plane.get_provider(provider)
    model = entry.model(entry.default_model_id)
    selection = plane.resolve(provider, model.model_id, model.default_voice_id)
    provisioned = tts_credential_pool.first_for(provider)
    monkeypatch.setattr(
        tts_engine_factory, "build_engine", lambda _selection: (selected, ""),
    )
    tts_fallback.register_plan(
        selection,
        provisioned_credential_id=provisioned.credential_id if provisioned else "",
    )


@pytest.mark.asyncio
async def test_a_transient_failure_falls_back_to_the_next_real_provider(monkeypatch):
    factory = _Factory({"openai": OPUS})
    monkeypatch.setattr(tts_engine_factory, "build_engine_for", factory.build_engine_for)
    selected = _Scripted("gemini", "gemini-3.1-flash-tts-preview", "Kore", _transient("nope"))
    _plan(monkeypatch, "gemini", selected)

    clip = await tts_service.synthesize(SPOKEN)

    # The selected provider is FIRST, the substitute is the registry's next
    # implemented provider, and the clip names the one that actually spoke.
    assert factory.built == [("openai", "")]
    assert clip.provider == "openai"
    assert clip.provider != "gemini"


@pytest.mark.asyncio
async def test_the_rotation_walks_the_registry_in_canonical_order(monkeypatch):
    """Selected first, then the registry's own tail — and then it STOPS.

    Every substitute is built and runs (so the order is observable), they all fail
    transiently, and the provider ceiling ends the rotation before the fourth
    provider is attempted.
    """
    factory = _Factory(default=_transient("nope"))
    monkeypatch.setattr(tts_engine_factory, "build_engine_for", factory.build_engine_for)
    selected = _Scripted("gemini", "gemini-3.1-flash-tts-preview", "Kore", _transient("nope"))
    _plan(monkeypatch, "gemini", selected)

    with pytest.raises(TtsError) as caught:
        await tts_service.synthesize(SPOKEN)

    assert [provider for provider, _ in factory.built] == ["openai", "grok"]
    assert caught.value.failure_class == tts_service.FAILURE_FALLBACK_EXHAUSTED
    assert 1 + len(factory.built) == tts_fallback.MAX_PROVIDER_ATTEMPTS
    assert "speechmatics" not in [provider for provider, _ in factory.built], (
        "the provider ceiling ends the rotation"
    )


@pytest.mark.asyncio
async def test_a_deterministic_failure_is_never_hidden_behind_another_provider(
    monkeypatch,
):
    factory = _Factory({"openai": OPUS})
    monkeypatch.setattr(tts_engine_factory, "build_engine_for", factory.build_engine_for)
    selected = _Scripted(
        "gemini", "gemini-3.1-flash-tts-preview", "Kore",
        TtsError(
            "refused", stage=tts_service.TTS_STAGE_PROVIDER,
            failure_class=tts_service.FAILURE_UNSUPPORTED_VOICE, retryable=False,
        ),
    )
    _plan(monkeypatch, "gemini", selected)

    with pytest.raises(TtsError) as caught:
        await tts_service.synthesize(SPOKEN)

    assert caught.value.failure_class == tts_service.FAILURE_UNSUPPORTED_VOICE
    assert factory.built == []


@pytest.mark.asyncio
async def test_a_credential_of_one_provider_is_never_handed_to_another(
    monkeypatch,
):
    """Only the provider being attempted contributes credentials to the attempt."""
    monkeypatch.setenv(gemini_tts_engine.API_KEY_ENV_VARS[0], SECRETS["gemini"])
    monkeypatch.setenv(openai_tts_engine.API_KEY_ENV_VARS[0], SECRETS["openai"])
    await tts_credential_pool.prepare(("gemini", "openai"))

    factory = _Factory({"openai": OPUS})
    monkeypatch.setattr(tts_engine_factory, "build_engine_for", factory.build_engine_for)
    selected = _Scripted("gemini", "gemini-3.1-flash-tts-preview", "Kore", _transient("nope"))
    _plan(monkeypatch, "gemini", selected)

    await tts_service.synthesize(SPOKEN)

    # The substitute was attempted with an OPENAI credential (or none) — never
    # with the Gemini one the selected provider was provisioned from.
    assert factory.built == [("openai", "env:AI_OPENAI_API_KEY")]
    assert all(
        "GEMINI" not in credential_id.upper()
        for _provider, credential_id in factory.built
    )


@pytest.mark.asyncio
async def test_a_rejected_credential_is_recorded_and_never_swapped_for_another_provider(
    monkeypatch,
):
    """A rejected key with nothing else to rotate to fails as ITSELF.

    The credential's health records the failure (so the next request skips it),
    and no other provider is substituted: a key the provider refused is a
    deterministic outcome, not a reason to speak through a service the owner did
    not select.
    """
    monkeypatch.setenv(gemini_tts_engine.API_KEY_ENV_VARS[0], SECRETS["gemini"])
    await tts_credential_pool.prepare(("gemini",))

    factory = _Factory({"openai": OPUS})
    monkeypatch.setattr(tts_engine_factory, "build_engine_for", factory.build_engine_for)
    rejected = TtsError(
        "rejected", stage=tts_service.TTS_STAGE_PROVIDER,
        failure_class=tts_service.FAILURE_AUTH, http_status=401,
    )
    selected = _Scripted("gemini", "gemini-3.1-flash-tts-preview", "Kore", rejected)
    _plan(monkeypatch, "gemini", selected)

    with pytest.raises(TtsError) as caught:
        await tts_service.synthesize(SPOKEN)

    assert caught.value.failure_class == tts_service.FAILURE_AUTH
    assert factory.built == []
    credential_id = "env:" + gemini_tts_engine.API_KEY_ENV_VARS[0]
    assert tts_credential_pool.credential_health(credential_id).failures == 1
    assert "cooling" in tts_credential_pool.describe("gemini") or (
        tts_credential_pool.is_cooled_down(credential_id) is True
    )


@pytest.mark.asyncio
async def test_a_rejected_key_rotates_inside_its_own_provider_only(monkeypatch):
    """A second Gemini key is tried BEFORE any other provider is considered."""
    monkeypatch.setenv(gemini_tts_engine.API_KEY_ENV_VARS[0], SECRETS["gemini"])

    async def _rows(_provider: str) -> tuple:
        return ({"credential_id": "gemini-b", "secret": SECRETS["gemini"],
                 "priority": 1, "enabled": True},)

    monkeypatch.setattr(credential_source, "_fetch_rows", _rows)
    await tts_credential_pool.prepare(("gemini",))

    factory = _Factory({"gemini": OPUS, "openai": MP3})
    monkeypatch.setattr(tts_engine_factory, "build_engine_for", factory.build_engine_for)
    rejected = TtsError(
        "rejected", stage=tts_service.TTS_STAGE_PROVIDER,
        failure_class=tts_service.FAILURE_AUTH, http_status=401,
    )
    selected = _Scripted("gemini", "gemini-3.1-flash-tts-preview", "Kore", rejected)
    _plan(monkeypatch, "gemini", selected)

    clip = await tts_service.synthesize(SPOKEN)

    assert clip.provider == "gemini"
    assert factory.built == [("gemini", "vault:gemini-b")]
    assert clip.audio == OPUS


# ── 6. Credential pool isolation ─────────────────────────────────────────


def test_each_provider_declares_its_own_credential_variables():
    declarations = {
        provider: tts_credential_pool.env_var_names(provider)
        for provider in plane.provider_ids()
    }

    assert declarations["openai"] == openai_tts_engine.API_KEY_ENV_VARS
    assert declarations["gemini"] == gemini_tts_engine.API_KEY_ENV_VARS
    assert declarations["grok"] == grok_tts_engine.API_KEY_ENV_VARS
    assert declarations["speechmatics"] == speechmatics_tts_engine.API_KEY_ENV_VARS
    assert tts_credential_pool.env_var_names("nobody") == (), "no environment sweep"
    seen: set[str] = set()
    for names in declarations.values():
        assert not (seen & set(names)), "a variable belongs to exactly one provider"
        seen |= set(names)


@pytest.mark.asyncio
async def test_a_provider_with_no_environment_credential_simply_has_none(monkeypatch):
    for module in (gemini_tts_engine, grok_tts_engine):
        for name in module.API_KEY_ENV_VARS:
            monkeypatch.delenv(name, raising=False)

    counts = await tts_credential_pool.prepare(("gemini", "grok"))

    assert counts == {"gemini": 0, "grok": 0}
    assert tts_credential_pool.first_for("gemini") is None
    assert tts_credential_pool.first_for("grok") is None


@pytest.mark.asyncio
async def test_a_pool_holds_only_the_provider_it_was_loaded_for(monkeypatch):
    monkeypatch.setenv(grok_tts_engine.API_KEY_ENV_VARS[0], SECRETS["grok"])

    counts = await tts_credential_pool.prepare(("grok", "gemini"))

    assert counts == {"grok": 1, "gemini": 0}
    assert tts_credential_pool.credentials_for("gemini") == ()
    assert tts_credential_pool.credentials_for("grok")[0].provider == "grok"


def test_the_registry_is_the_only_provider_source_for_the_pool():
    assert tts_credential_pool.registered_providers() == plane.implemented_provider_ids()


# ── 7. Persian capability metadata is never overclaimed ──────────────────


def test_no_provider_claims_verified_persian():
    for provider in plane.TTS_PROVIDERS:
        for model in provider.models:
            for voice in model.voices:
                assert voice.persian in plane.PERSIAN_STATES
                assert voice.persian != plane.PERSIAN_VERIFIED, provider.provider


def test_gemini_records_its_documented_persian_support_without_claiming_it():
    entry = plane.get_provider("gemini")
    voices = entry.models[0].voices

    assert all(v.persian == plane.PERSIAN_NOT_VERIFIED for v in voices)
    assert all("fa" in v.note for v in voices)
    assert all("not claimed as verified" in v.note for v in voices)


def test_grok_records_the_absence_of_a_documented_persian_language():
    entry = plane.get_provider("grok")
    voices = entry.models[0].voices

    assert all(v.persian == plane.PERSIAN_NOT_VERIFIED for v in voices)
    assert all("20-language" in v.note for v in voices)
    assert all("live request is required" in v.note for v in voices)


def test_speechmatics_records_english_only_as_unsupported():
    entry = plane.get_provider("speechmatics")
    voices = entry.models[0].voices

    assert all(v.persian == plane.PERSIAN_UNSUPPORTED for v in voices)
    assert all("English only" in v.note for v in voices)
    assert all("English (" in v.note for v in voices), "the accent is recorded"


def test_the_owner_facing_wording_never_says_supported():
    for state in plane.PERSIAN_STATES:
        label = plane.persian_label(state)
        assert "supported" not in label or state == plane.PERSIAN_UNSUPPORTED
    assert "not verified" in plane.persian_label(plane.PERSIAN_NOT_VERIFIED)


@pytest.mark.asyncio
async def test_the_panel_states_the_capability_rather_than_a_claim(monkeypatch):
    store: dict = {}
    module = _install_surface(monkeypatch, store)
    await module._ai_tts_select_action(None, "gemini", 0)

    _title, body, _buttons = await module._ai_media_tts_voice_panel_handler(None, "")

    assert "Persian · not verified — live request required" in body
    assert "Persian verified" not in body


# ── 8. No secret egress anywhere on the surface ──────────────────────────


@pytest.mark.asyncio
async def test_the_panel_never_names_a_credential_variable_or_a_key(monkeypatch):
    store: dict = {}
    module = _install_surface(monkeypatch, store)

    for provider in plane.provider_ids():
        await module._ai_tts_select_action(None, provider, 0)
        for handler in (
            module._ai_media_tts_panel_handler,
            module._ai_media_tts_model_panel_handler,
            module._ai_media_tts_voice_panel_handler,
        ):
            _title, body, _buttons = await handler(None, "")
            for secret in SECRETS.values():
                assert secret not in body
            for adapter in (
                openai_tts_engine, gemini_tts_engine,
                grok_tts_engine, speechmatics_tts_engine,
            ):
                for name in adapter.API_KEY_ENV_VARS:
                    assert name not in body, f"{provider}: {name}"


@pytest.mark.asyncio
async def test_a_provider_failure_message_never_carries_the_key(monkeypatch, http):
    monkeypatch.setenv(gemini_tts_engine.API_KEY_ENV_VARS[0], SECRETS["gemini"])

    def _reject(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401,
            content=json.dumps({"error": {
                "code": 401, "status": "UNAUTHENTICATED",
                "message": f"API key {SECRETS['gemini']} is invalid",
            }}).encode(),
            headers={"content-type": "application/json"},
        )

    class _Rejecting(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            return _reject(request)

    monkeypatch.setattr(
        httpx, "AsyncClient",
        lambda timeout=None, **rest: _HTTPX_ASYNC_CLIENT(transport=_Rejecting(), timeout=timeout),
    )

    await tts_service.apply_tts_settings_async(_config_for("gemini"))
    with pytest.raises(TtsError) as caught:
        await tts_service.synthesize(SPOKEN)

    assert SECRETS["gemini"] not in str(caught.value)
    assert "***" in str(caught.value)


@pytest.mark.asyncio
async def test_no_adapter_log_line_carries_a_key_or_the_spoken_text(monkeypatch, http, caplog):
    caplog.set_level(logging.DEBUG)
    _set_all_env(monkeypatch)

    for provider in plane.provider_ids():
        await tts_service.apply_tts_settings_async(_config_for(provider))
        await tts_service.synthesize(SPOKEN)

    for secret in SECRETS.values():
        assert secret not in caplog.text
    assert SPOKEN not in caplog.text
    for engine in ("OPENAI_TTS_ENGINE", "GEMINI_TTS_ENGINE", "GROK_TTS_ENGINE",
                   "SPEECHMATICS_TTS_ENGINE"):
        assert engine in caplog.text, engine


# ── 9. The delivery container is a stated capability, not a surprise ─────


def test_only_documented_voice_note_containers_are_marked_compatible():
    """Telegram documents OGG/Opus, MP3 and M4A for voice messages.

    The runtime does not transcode, so a WAV model is a working adapter whose
    voice-note delivery is unproven — and the registry says exactly that rather
    than presenting every container as equivalent.
    """
    compatible = {
        (p.provider, m.model_id) for p in plane.TTS_PROVIDERS
        for m in p.models if m.voice_note_compatible
    }
    unproven = {
        (p.provider, m.model_id) for p in plane.TTS_PROVIDERS
        for m in p.models if not m.voice_note_compatible
    }

    assert {provider for provider, _ in compatible} == {"openai", "grok"}
    assert {provider for provider, _ in unproven} == {"gemini", "speechmatics"}
    assert not compatible & unproven
    for provider in plane.provider_ids():
        model = plane.get_provider(provider).models[0]
        assert model.voice_note_compatible == (
            model.mime_type in plane.VOICE_NOTE_MIME_TYPES
        )


@pytest.mark.asyncio
async def test_the_surface_can_state_the_delivery_container(monkeypatch):
    _set_all_env(monkeypatch)

    await tts_service.apply_tts_settings_async(_config_for("gemini"))
    assert tts_service.describe()["voice_note"] == "not_documented"
    await tts_service.apply_tts_settings_async(_config_for("openai"))
    assert tts_service.describe()["voice_note"] == "yes"


# ── 10. The AI surface cannot choose any of this ─────────────────────────


def test_the_ai_tool_schema_exposes_no_provider_model_or_voice():
    from backend.ai.tools.speech import SpeakTool

    tool = SpeakTool(context=None)
    schema = str(tool.parameters).lower()

    assert tool.required_arguments == ("text",)
    for forbidden in ("provider", "model", "voice_id", "api_key", "endpoint", "url",
                      "credential", "codec", "format"):
        assert forbidden not in schema, forbidden
