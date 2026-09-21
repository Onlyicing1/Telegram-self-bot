"""TTS control plane — the capability registry, the persisted selection and the
Telegram surface that edits it.

This suite pins the CONFIGURATION half of speech synthesis, which until this
phase did not exist: the boundary had exactly one provider, one model and one
fixed voice, and no owner-facing setting that changed any of them.

  1. the REGISTRY is the single authority on provider → model → voice: an
     unregistered value can never become a selection, and a model of one provider
     can never be sent to another;
  2. PERSIAN is never overclaimed: every voice carries an explicit capability
     state, nothing is reported as verified without a recorded verification, and
     the provider whose own documentation is English-only says exactly that;
  3. an INVALID stored triple is degraded deterministically level by level and
     reported, and the Telegram writes always persist a CONSISTENT triple — so
     changing the provider cannot leave the previous provider's model or voice
     behind;
  4. the SELECTION is persisted through the EXISTING per-owner ``ai_config`` store
     (no second store, no environment variable) and applied to the live boundary
     immediately, so a change needs no redeploy and no restart;
  5. the SURFACE offers only finite registered choices — no typed model or voice
     identifier, no unregistered target, and no environment-variable name;
  6. the AI tool is unchanged: it accepts bounded TEXT only, so the model cannot
     pick a provider, a model, a voice, a credential or an endpoint.

The provider engines are scripted doubles and the store is a fake, so nothing here
is a statement about synthesis QUALITY or about a live provider. Live Telegram and
live provider verification were NOT performed.
"""
from __future__ import annotations

import inspect
from typing import Any

import pytest

from backend.ai import config_store, tts_control_plane as plane
from backend.services import openai_tts_engine, tts_service

OWNER = 7283627550
SPOKEN = "سلام، این یک آزمون است"

#: The registered defaults, read from the registry rather than re-typed.
PROVIDER = plane.DEFAULT_PROVIDER_ID
MODEL = plane.DEFAULT_MODEL_ID
VOICE = plane.DEFAULT_VOICE_ID


# ── Doubles ──────────────────────────────────────────────────────────────────


class _Response:
    def __init__(self, data: Any) -> None:
        self.data = data


class _Query:
    """A minimal PostgREST-shaped chain over one in-memory row set."""

    def __init__(self, store: "_Store", mode: str = "select", payload: Any = None) -> None:
        self._store = store
        self._mode = mode
        self._payload = payload
        self._filters: dict[str, Any] = {}

    def select(self, *_args: Any, **_kwargs: Any) -> "_Query":
        return self

    def eq(self, key: str, value: Any) -> "_Query":
        self._filters[key] = value
        return self

    def maybe_single(self) -> "_Query":
        return self

    def update(self, payload: dict) -> "_Query":
        self._mode, self._payload = "update", payload
        return self

    def insert(self, payload: dict) -> "_Query":
        self._mode, self._payload = "insert", payload
        return self

    def execute(self) -> _Response:
        rows = self._store.rows
        if self._mode == "select":
            for row in rows.values():
                if all(row.get(k) == v for k, v in self._filters.items()):
                    return _Response(dict(row))
            return _Response(None)
        if self._mode == "update":
            for row in rows.values():
                if all(row.get(k) == v for k, v in self._filters.items()):
                    row.update(self._payload)
                    return _Response([dict(row)])
            return _Response([])
        row = dict(self._payload)
        rows[row.get("owner_id")] = row
        return _Response([dict(row)])


class _Table:
    def __init__(self, store: "_Store") -> None:
        self._store = store

    def select(self, *_a: Any, **_k: Any) -> _Query:
        return _Query(self._store, "select")

    def update(self, payload: dict) -> _Query:
        return _Query(self._store, "update", payload)

    def insert(self, payload: dict) -> _Query:
        return _Query(self._store, "insert", payload)


class _Store:
    """A fake ``supabase`` client holding one ``ai_config`` table."""

    def __init__(self) -> None:
        self.rows: dict[int, dict] = {}

    def table(self, _name: str) -> _Table:
        return _Table(self)

    def rpc(self, _name: str, _params: Any) -> _Query:
        return _Query(self, "select")


class _Engine:
    """A scripted synthesis engine: records the text, returns fixed audio."""

    provider = openai_tts_engine.PROVIDER_NAME
    model = openai_tts_engine.SPEECH_MODEL
    voice = openai_tts_engine.DEFAULT_VOICE
    mime_type = openai_tts_engine.AUDIO_MIME
    file_name = openai_tts_engine.AUDIO_FILE_NAME
    key_env_var = "AI_OPENAI_API_KEY"

    def __init__(self) -> None:
        self.texts: list[str] = []

    async def speak(self, text: str, *, timeout_s: float = 0.0) -> bytes:
        self.texts.append(text)
        return b"OggS\x00" + b"\x22" * 32


@pytest.fixture(autouse=True)
def store(monkeypatch):
    """A fake durable store; the credential backend contributes nothing."""
    fake = _Store()
    monkeypatch.setattr("backend.db.client.get_db", lambda: fake)
    monkeypatch.setattr(config_store, "_fallback_config", {})
    monkeypatch.setattr(tts_service, "_selected", None)

    async def _no_rows(_provider: str) -> tuple:
        return ()

    from backend.ai import credential_source
    from backend.services import tts_credential_pool

    monkeypatch.setattr(credential_source, "_fetch_rows", _no_rows)
    credential_source.reset()
    tts_credential_pool.reset()
    for name in openai_tts_engine.API_KEY_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    yield fake
    credential_source.reset()
    tts_credential_pool.reset()


def _inject_provider(monkeypatch, provider: str, *, model: str = "fake-model",
                     voice: str = "fake-voice", implemented: bool = True):
    """Register an extra provider for the duration of one test (a test double)."""
    entry = plane.TtsProvider(
        provider=provider,
        label=provider.title(),
        models=(
            plane.TtsModel(
                model_id=model, label=model,
                voices=(plane.TtsVoice(voice_id=voice, label=voice.title()),),
                implemented=implemented, output_format="opus", mime_type="audio/ogg",
            ),
        ),
        implemented=implemented, note="test double",
    )
    providers = plane.TTS_PROVIDERS + (entry,)
    monkeypatch.setattr(plane, "TTS_PROVIDERS", providers)
    monkeypatch.setattr(plane, "_BY_PROVIDER", {p.provider: p for p in providers})
    return entry


def _nav(_builder):
    return None


# ══ 1. The registry ══════════════════════════════════════════════════════════


def test_the_registry_order_is_a_deterministic_tuple():
    assert plane.provider_ids() == tuple(p.provider for p in plane.TTS_PROVIDERS)
    assert plane.provider_ids()[0] == plane.DEFAULT_PROVIDER_ID
    assert len(plane.provider_ids()) == len(set(plane.provider_ids()))


def test_the_default_provider_is_implemented():
    entry = plane.get_provider(plane.DEFAULT_PROVIDER_ID)
    assert entry is not None and entry.implemented is True


def test_the_voice_allowlist_is_the_adapters_own_declaration():
    """One declaration of the voices: order and allowlist cannot drift."""
    assert set(openai_tts_engine.VOICE_ORDER) == set(openai_tts_engine.SUPPORTED_VOICES)
    assert plane.voice_ids(PROVIDER, MODEL) == openai_tts_engine.VOICE_ORDER
    assert plane.voice_ids(PROVIDER, MODEL)[0] == plane.DEFAULT_VOICE_ID


def test_the_default_model_is_the_adapters_own_model():
    assert plane.model_ids(PROVIDER) == openai_tts_engine.SUPPORTED_MODELS
    assert MODEL in openai_tts_engine.SUPPORTED_MODELS


def test_a_registered_unimplemented_provider_is_never_selectable():
    assert plane.get_provider("speechmatics") is not None
    assert plane.get_provider("speechmatics").implemented is False
    model = plane.get_provider("speechmatics").default_model_id
    voice = plane.voice_ids("speechmatics", model)[0]
    assert plane.is_selectable("speechmatics", model, voice) is False


# ══ 2. provider → model → voice validation ═══════════════════════════════════


def test_a_valid_registered_triple_is_selectable():
    assert plane.is_selectable(PROVIDER, MODEL, VOICE) is True


def test_an_unregistered_provider_is_refused():
    assert plane.get_provider("skynet") is None
    assert plane.is_selectable("skynet", MODEL, VOICE) is False
    assert plane.model_ids("skynet") == ()
    assert plane.canonical_order("skynet") == plane.implemented_provider_ids()


def test_a_model_of_another_provider_is_refused():
    _provider = plane.get_provider(PROVIDER)
    other = plane.get_provider("speechmatics")
    assert other is not None
    assert _provider.model(other.default_model_id) is None
    assert plane.is_selectable(PROVIDER, other.default_model_id, VOICE) is False


def test_an_unregistered_voice_is_refused():
    assert plane.get_voice(PROVIDER, MODEL, "not-a-voice") is None
    assert plane.is_selectable(PROVIDER, MODEL, "not-a-voice") is False


def test_a_voice_of_another_model_is_refused(monkeypatch):
    _inject_provider(monkeypatch, "other", model="other-model", voice="other-voice")
    assert plane.is_selectable(PROVIDER, MODEL, "other-voice") is False
    assert plane.is_selectable("other", "other-model", VOICE) is False


def test_the_canonical_order_puts_the_selection_first(monkeypatch):
    _inject_provider(monkeypatch, "second")
    order = plane.canonical_order("second")
    assert order[0] == "second"
    assert set(order) == set(plane.implemented_provider_ids())


# ══ 3. Persian capability metadata never overclaims ══════════════════════════


def test_no_registered_voice_claims_verified_persian():
    for provider in plane.TTS_PROVIDERS:
        for model in provider.models:
            for voice in model.voices:
                assert voice.persian in plane.PERSIAN_STATES
                assert voice.persian != plane.PERSIAN_VERIFIED, (
                    "no live verification is recorded for this build, so nothing "
                    "may be reported as verified"
                )


def test_the_english_only_provider_is_recorded_as_unsupported():
    entry = plane.get_provider("speechmatics")
    assert entry is not None
    model = entry.model(entry.default_model_id)
    assert model is not None
    assert model.voices, "the documented voices must be recorded"
    for voice in model.voices:
        assert voice.persian == plane.PERSIAN_UNSUPPORTED
        assert voice.note, "the capability statement must carry its evidence"


def test_the_unverified_state_is_worded_as_a_requirement_not_a_claim():
    assert "not verified" in plane.persian_label(plane.PERSIAN_NOT_VERIFIED)
    assert plane.persian_label(plane.PERSIAN_UNSUPPORTED) == "not supported"
    assert plane.persian_label("nonsense") == plane.PERSIAN_LABELS[plane.PERSIAN_NOT_VERIFIED]


# ══ 4. Resolution: a stored triple is always degraded to a valid one ═════════


def test_the_default_selection_is_a_valid_triple():
    selection = plane.default_selection()
    assert (selection.provider, selection.model, selection.voice) == (PROVIDER, MODEL, VOICE)
    assert selection.is_valid is True
    assert selection.adjusted == ""


def test_an_unknown_provider_degrades_to_the_default_and_says_so():
    selection = plane.resolve("skynet", "anything", "anything")
    assert selection.provider == PROVIDER
    assert selection.is_valid is True
    assert "unknown provider `skynet`" in selection.adjusted


def test_an_unimplemented_provider_degrades_to_the_default_and_says_so():
    selection = plane.resolve("speechmatics", "", "")
    assert selection.provider == PROVIDER
    assert "not available on this runtime" in selection.adjusted


def test_a_model_of_another_provider_degrades_to_the_providers_default():
    selection = plane.resolve(PROVIDER, "gpt-4o-mini-tts-other", VOICE)
    assert selection.model == MODEL
    assert selection.voice == VOICE
    assert "is not offered by" in selection.adjusted


def test_a_voice_of_another_model_degrades_to_the_models_default():
    selection = plane.resolve(PROVIDER, MODEL, "not-a-voice")
    assert selection.voice == VOICE
    assert selection.adjusted
    assert selection.is_valid is True


def test_the_default_selection_is_stored_as_an_empty_triple():
    assert plane.storage_values(plane.default_selection()) == {
        plane.STORAGE_KEY_PROVIDER: "",
        plane.STORAGE_KEY_MODEL: "",
        plane.STORAGE_KEY_VOICE: "",
    }


def test_a_non_default_selection_is_stored_verbatim():
    selection = plane.resolve(PROVIDER, MODEL, "nova")
    values = plane.storage_values(selection)
    assert values[plane.STORAGE_KEY_VOICE] == "nova"
    assert values[plane.STORAGE_KEY_PROVIDER] == ""
    assert values[plane.STORAGE_KEY_MODEL] == ""


def test_an_empty_config_resolves_to_the_default_selection():
    assert plane.parse_tts_config({}) == plane.default_selection()
    assert plane.parse_tts_config(None) == plane.default_selection()


def test_a_persisted_selection_round_trips_through_the_store_shape():
    selection = plane.resolve(PROVIDER, MODEL, "verse")
    stored = plane.storage_values(selection)
    assert plane.parse_tts_config(stored).voice == "verse"


# ══ 5. Persistence through the EXISTING store ════════════════════════════════


def test_the_config_defaults_carry_the_three_tts_keys():
    for key in plane.STORAGE_KEYS:
        assert key in config_store._DEFAULTS, key
    assert config_store._DEFAULTS[plane.STORAGE_KEY_PROVIDER] == ""
    assert config_store._DEFAULTS[plane.STORAGE_KEY_MODEL] == ""
    assert config_store._DEFAULTS[plane.STORAGE_KEY_VOICE] == ""


@pytest.mark.asyncio
async def test_a_saved_selection_is_read_back_from_the_durable_row(store):
    from backend.bot.handlers import ai_tts_settings as module

    selection = plane.resolve(PROVIDER, MODEL, "shimmer")
    assert await module.persist_selection(OWNER, selection) is True
    assert store.rows[OWNER][plane.STORAGE_KEY_VOICE] == "shimmer"

    reloaded = await config_store.get_config(OWNER)
    assert plane.parse_tts_config(reloaded).voice == "shimmer"
    # The rest of the AI config is untouched by a speech change.
    assert reloaded["stt_passes"] == 1


@pytest.mark.asyncio
async def test_the_default_triple_is_stored_as_absence_not_a_literal(store):
    from backend.bot.handlers import ai_tts_settings as module

    await module.persist_selection(OWNER, plane.default_selection())
    row = store.rows[OWNER]
    assert row[plane.STORAGE_KEY_PROVIDER] is None
    assert row[plane.STORAGE_KEY_MODEL] is None
    assert row[plane.STORAGE_KEY_VOICE] is None


@pytest.mark.asyncio
async def test_the_stt_settings_are_unchanged_by_a_tts_write(store):
    """Regression: the TTS keys never overwrite the speech-to-text keys."""
    from backend.bot.handlers import ai_tts_settings as module

    config = await config_store.get_config(OWNER)
    config.update({"stt_model": "groq:whisper-large-v3", "stt_language": "fa-IR", "stt_passes": 3})
    await config_store.save_config(OWNER, config)

    await module.persist_selection(OWNER, plane.resolve(PROVIDER, MODEL, "nova"))

    reloaded = await config_store.get_config(OWNER)
    assert reloaded["stt_model"] == "groq:whisper-large-v3"
    assert reloaded["stt_language"] == "fa-IR"
    assert reloaded["stt_passes"] == 3
    assert reloaded[plane.STORAGE_KEY_VOICE] == "nova"


# ══ 6. Runtime application (no redeploy, no restart) ═════════════════════════


@pytest.mark.asyncio
async def test_applying_a_settings_mapping_provisions_the_selection(monkeypatch):
    engine = _Engine()
    monkeypatch.setattr(openai_tts_engine, "build_engine", lambda *a, **k: (engine, ""))

    status = await tts_service.apply_tts_settings_async({
        plane.STORAGE_KEY_VOICE: "echo",
    })

    assert status["configured"] is True
    assert status["provider"] == PROVIDER
    assert status["tts_voice"] == "echo"
    assert tts_service.current_selection().voice == "echo"
    assert tts_service.describe()["voice"] == "echo"


@pytest.mark.asyncio
async def test_a_missing_credential_is_reported_and_never_fatal(monkeypatch):
    from backend.services import tts_engine_factory

    monkeypatch.setattr(
        tts_engine_factory, "build_engine",
        lambda _selection: (None, tts_service.FAILURE_MISSING_CREDENTIAL),
    )
    status = await tts_service.apply_tts_settings_async({})
    assert status["configured"] is False
    assert status["reason"] == tts_service.FAILURE_MISSING_CREDENTIAL
    assert tts_service.capability_reason() == tts_service.FAILURE_MISSING_CREDENTIAL


@pytest.mark.asyncio
async def test_a_broken_apply_is_reported_and_never_raises(monkeypatch):
    monkeypatch.setattr(plane, "parse_tts_config", lambda _config: (_ for _ in ()).throw(RuntimeError("boom")))
    status = await tts_service.apply_tts_settings_async({})
    assert status["configured"] is False
    assert status["reason"] == "RuntimeError"


def test_the_startup_hook_applies_the_persisted_selection():
    supervisor_source = inspect.getsource(
        __import__("backend.runtime.supervisor", fromlist=["x"])
    )
    assert "_apply_persisted_tts_settings" in supervisor_source
    assert "apply_tts_settings_async" in supervisor_source


# ══ 7. The Telegram surface ══════════════════════════════════════════════════


def _install_surface(monkeypatch, owner: int = OWNER) -> Any:
    from backend.bot.handlers import ai as ai_module
    from backend.bot.handlers import ai_tts_settings as module

    async def _owner() -> int:
        return owner

    async def _config(owner_id: int) -> dict:
        return await config_store.get_config(owner_id)

    monkeypatch.setattr(ai_module, "_get_owner_id", _owner)
    monkeypatch.setattr(ai_module, "_get_saved_config", _config)
    monkeypatch.setattr(ai_module, "_nav_buttons", _nav)
    return module


@pytest.mark.asyncio
async def test_the_panel_shows_the_current_selection_and_its_capability(monkeypatch, store):
    module = _install_surface(monkeypatch)
    await module.persist_selection(OWNER, plane.resolve(PROVIDER, MODEL, "sage"))

    title, body, _buttons = await module._ai_media_tts_panel_handler(None, "")

    assert title == "Text-to-Speech"
    assert f"Provider · {plane.get_provider(PROVIDER).label}" in body
    assert "Model · " + plane.get_model(PROVIDER, MODEL).label in body
    assert "Voice · Sage" in body
    assert "Persian · not verified" in body
    assert "Credentials ·" in body


@pytest.mark.asyncio
async def test_the_panel_offers_a_switch_only_for_a_provider_that_can_run(monkeypatch, store):
    module = _install_surface(monkeypatch)
    _title, body, _buttons = await module._ai_media_tts_panel_handler(None, "")

    assert "not available yet" in body
    assert "No owner controls" not in body


def test_the_model_and_voice_screens_are_registered_under_the_panel(monkeypatch):
    from backend.bot.handlers import ai_tts_settings as module

    panels: list[tuple[str, str]] = []
    monkeypatch.setattr(
        module, "register_panel",
        lambda panel_id, handler, parent="menu", title="": panels.append((panel_id, parent)),
    )
    monkeypatch.setattr(module, "register_inline_builder", lambda *a, **k: None)
    actions: list[str] = []
    monkeypatch.setattr(module, "register_action", lambda action_id, handler: actions.append(action_id))

    module.register(None, 1)

    assert ("ai_media_tts", "ai_media") in panels
    assert ("ai_media_tts_model", "ai_media_tts") in panels
    assert ("ai_media_tts_voice", "ai_media_tts") in panels
    assert sorted(actions) == ["ai_tts_select", "ai_tts_select_model", "ai_tts_select_voice"]


@pytest.mark.asyncio
async def test_the_model_screen_lists_only_this_providers_models(monkeypatch, store):
    module = _install_surface(monkeypatch)
    _inject_provider(monkeypatch, "other", model="other-model", voice="other-voice")

    _title, body, _buttons = await module._ai_media_tts_model_panel_handler(None, "")

    assert MODEL in body
    assert "other-model" not in body


@pytest.mark.asyncio
async def test_the_voice_screen_offers_only_this_models_other_voices(monkeypatch, store):
    module = _install_surface(monkeypatch)

    _title, body, buttons = await module._ai_media_tts_voice_panel_handler(None, "")

    assert "Persian · not verified" in body
    texts = [
        str(getattr(button, "text", ""))
        for row in buttons
        for button in (row if isinstance(row, list) else [row])
    ]
    for voice in plane.voice_ids(PROVIDER, MODEL)[1:]:
        assert any(voice.title() in text for text in texts), voice
    assert not any(plane.DEFAULT_VOICE_ID.title() == text for text in texts), (
        "the current voice is reported, not offered as a control"
    )


@pytest.mark.asyncio
async def test_selecting_a_model_resets_the_voice_to_that_models_default(monkeypatch, store):
    module = _install_surface(monkeypatch)
    _inject_provider(monkeypatch, "other", model="other-model", voice="other-voice")
    await module.persist_selection(OWNER, plane.resolve(PROVIDER, MODEL, "verse"))

    await module._ai_tts_select_model_action(None, MODEL, 0)

    reloaded = plane.parse_tts_config(await config_store.get_config(OWNER))
    assert (reloaded.provider, reloaded.model) == (PROVIDER, MODEL)
    assert reloaded.voice == plane.voice_ids(PROVIDER, MODEL)[0]
    assert reloaded.voice != "verse", "a model change must not keep another model's voice"


@pytest.mark.asyncio
async def test_selecting_a_voice_leaves_provider_and_model_alone(monkeypatch, store):
    module = _install_surface(monkeypatch)

    await module._ai_tts_select_voice_action(None, "verse", 0)

    reloaded = plane.parse_tts_config(await config_store.get_config(OWNER))
    assert (reloaded.provider, reloaded.model, reloaded.voice) == (PROVIDER, MODEL, "verse")


@pytest.mark.asyncio
async def test_switching_provider_persists_a_consistent_triple(monkeypatch, store):
    """The previous provider's model and voice can never survive the switch."""
    module = _install_surface(monkeypatch)
    _inject_provider(monkeypatch, "other", model="other-model", voice="other-voice")
    await module.persist_selection(OWNER, plane.resolve(PROVIDER, MODEL, "verse"))

    await module._ai_tts_select_action(None, "other", 0)

    reloaded = plane.parse_tts_config(await config_store.get_config(OWNER))
    assert reloaded.provider == "other"
    assert reloaded.model == "other-model"
    assert reloaded.voice == "other-voice"
    assert reloaded.adjusted == "", "a switch must never store a stale value"


@pytest.mark.asyncio
async def test_an_unimplemented_provider_switch_changes_nothing(monkeypatch, store):
    module = _install_surface(monkeypatch)

    _title, body, _buttons = await module._ai_tts_select_action(None, "speechmatics", 0)

    assert "not available on this runtime" in body
    reloaded = plane.parse_tts_config(await config_store.get_config(OWNER))
    assert reloaded.provider == PROVIDER


@pytest.mark.asyncio
async def test_an_unknown_voice_changes_nothing(monkeypatch, store):
    module = _install_surface(monkeypatch)

    _title, body, _buttons = await module._ai_tts_select_voice_action(None, "not-a-voice", 0)

    assert "Unknown voice" in body
    reloaded = plane.parse_tts_config(await config_store.get_config(OWNER))
    assert reloaded.voice == VOICE


@pytest.mark.asyncio
async def test_a_change_is_applied_to_the_live_boundary_immediately(monkeypatch, store):
    module = _install_surface(monkeypatch)
    engine = _Engine()
    monkeypatch.setattr(openai_tts_engine, "build_engine", lambda *a, **k: (engine, ""))

    await module._ai_tts_select_voice_action(None, "onyx", 0)

    assert tts_service.describe()["voice"] == "onyx"


@pytest.mark.asyncio
async def test_the_panel_names_no_environment_variable(monkeypatch, store):
    module = _install_surface(monkeypatch)
    _title, body, _buttons = await module._ai_media_tts_panel_handler(None, "")

    for name in openai_tts_engine.API_KEY_ENV_VARS:
        assert name not in body
    assert openai_tts_engine.BASE_URL_ENV_VAR not in body


def test_the_status_line_is_one_line_and_never_raises(monkeypatch):
    from backend.bot.handlers import ai_tts_settings as module

    monkeypatch.setattr(tts_service, "describe", lambda: (_ for _ in ()).throw(RuntimeError("x")))
    assert module.tts_status_line() == "Text-to-Speech · Unavailable on this runtime"


# ══ 8. Output format + capability isolation ══════════════════════════════════


def test_the_default_selection_produces_the_telegram_voice_format():
    described = tts_service.describe()
    assert described["mime_type"] == openai_tts_engine.AUDIO_MIME
    assert described["format"] == openai_tts_engine.RESPONSE_FORMAT


def test_the_deferred_provider_records_why_it_is_deferred():
    entry = plane.get_provider("speechmatics")
    assert entry is not None
    assert not entry.implemented
    assert "WAV" in entry.note and "English-only" in entry.note


# ══ 9. The AI tool accepts bounded text only ═════════════════════════════════


def test_the_ai_tool_has_no_provider_model_or_voice_parameter():
    from backend.ai.tools.speech import SpeakTool

    tool = SpeakTool(context=None)
    assert tool.required_arguments == ("text",)
    assert set(tool.parameters) == {"text"}


def test_no_ai_tool_field_can_carry_a_credential_or_an_endpoint():
    from backend.ai.tools.speech import SpeakTool

    tool = SpeakTool(context=None)
    schema = str(tool.parameters).lower()
    for forbidden in ("provider", "model", "voice", "api_key", "endpoint", "url", "credential"):
        assert forbidden not in schema
