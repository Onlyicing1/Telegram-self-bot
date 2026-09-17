"""Gemini STT settings — the Telegram-manageable Voice transcription controls.

Covers the whole seam at the behavior level:

  1. deterministic defaults (default model, automatic language, one pass);
  2. persistence through the EXISTING AI config store (owner-specific, reloadable);
  3. validation bounds — 1..3 for passes, shape checks for model/language;
  4. the AI Settings panel / Advanced panel showing the current values;
  5. the Telegram input handlers changing each value;
  6. the live Gemini STT engine consuming the PERSISTED settings, with the
     behavioral values independent of Render ENV;
  7. context isolation — nothing but the three settings can reach the engine.

Nothing here claims recognition QUALITY: these tests prove configuration
plumbing, not transcription accuracy.
"""
from __future__ import annotations

import inspect

import pytest

from backend.ai import config_store
from backend.services import media_service

API_KEY = "test-stt-settings-key"
API_KEY_VAR = "AI_GEMINI_API_KEY"


def _stt_engine_settings():
    engine = media_service.get_stt_engine()
    assert engine is not None
    return engine.stt_model, engine.stt_language, engine.stt_passes


@pytest.fixture(autouse=True)
def _clean_engine(monkeypatch):
    """No credential and no STT engine leaks between tests."""
    monkeypatch.delenv(API_KEY_VAR, raising=False)
    media_service.set_stt_engine(None)
    yield
    media_service.set_stt_engine(None)


# ── 1. Defaults ──


def test_default_stt_settings_are_the_documented_ones():
    assert config_store._DEFAULTS["stt_model"] == ""
    assert config_store._DEFAULTS["stt_language"] == ""
    assert config_store._DEFAULTS["stt_passes"] == 1


@pytest.mark.asyncio
async def test_unconfigured_owner_gets_defaults():
    config = await config_store.get_config(777000101)

    assert config["stt_model"] == ""
    assert config["stt_language"] == ""
    assert config["stt_passes"] == 1


def test_the_engine_defaults_are_the_same_behavior():
    from backend.services.gemini_media_engine import stt_settings_from

    resolved = stt_settings_from(None)

    assert resolved == {"stt_model": "", "stt_language": "", "stt_passes": 1}
    assert stt_settings_from({}) == resolved


# ── 2. Persistence through the existing config store ──


@pytest.mark.asyncio
async def test_persisting_a_custom_stt_model_and_language():
    owner = 777000102

    assert await config_store.update_setting(owner, "stt_model", "gemini-2.5-pro")
    assert await config_store.update_setting(owner, "stt_language", "fa-IR")

    reloaded = await config_store.get_config(owner)
    assert reloaded["stt_model"] == "gemini-2.5-pro"
    assert reloaded["stt_language"] == "fa-IR"


@pytest.mark.asyncio
@pytest.mark.parametrize("passes", [1, 2, 3])
async def test_persisting_each_valid_pass_count(passes):
    owner = 777000110 + passes

    assert await config_store.update_setting(owner, "stt_passes", passes)

    assert (await config_store.get_config(owner))["stt_passes"] == passes


@pytest.mark.asyncio
async def test_an_unset_language_stays_automatic_after_a_reload():
    owner = 777000103

    await config_store.update_setting(owner, "stt_language", "fa-IR")
    await config_store.update_setting(owner, "stt_language", "")

    assert (await config_store.get_config(owner))["stt_language"] == ""


def test_the_upsert_payload_carries_all_three_keys():
    """A missing column degrades the whole upsert to RAM — so the payload must
    carry the three keys, exactly like every other ai_config setting."""
    source = inspect.getsource(config_store._save_config_sync)

    for key in ("stt_model", "stt_language", "stt_passes"):
        assert f'"{key}"' in source


# ── 3. Validation bounds ──


def test_the_engine_bound_is_the_documented_maximum():
    from backend.services.gemini_media_engine import STT_MAX_PASSES

    assert STT_MAX_PASSES == 3


@pytest.mark.parametrize("value", [0, -1, 4, 9, 100])
def test_out_of_range_passes_are_refused_by_the_engine_seam(value):
    """The engine's own invariant: never above the maximum, never below one."""
    from backend.services.gemini_media_engine import stt_settings_from

    assert stt_settings_from({"stt_passes": value})["stt_passes"] == max(1, min(value, 3))


def test_auto_language_alias_becomes_automatic():
    from backend.services.gemini_media_engine import stt_settings_from

    assert stt_settings_from({"stt_language": "auto"})["stt_language"] == ""
    assert stt_settings_from({"stt_language": "AUTO"})["stt_language"] == ""


def test_a_deprecated_model_is_substituted_and_an_unknown_one_passes_through():
    from backend.services.gemini_media_engine import stt_settings_from

    assert stt_settings_from({"stt_model": "gemini-2.0-flash"})["stt_model"] == "gemini-2.5-flash"
    assert stt_settings_from({"stt_model": "gemini-9.9-custom"})["stt_model"] == "gemini-9.9-custom"


# ── 4. Panels show the current values ──


def _flatten(buttons) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for row in buttons:
        cells = row if isinstance(row, list) else [row]
        for btn in cells:
            data = getattr(btn, "data", None) or ""
            if isinstance(data, bytes):
                data = data.decode("utf-8", errors="replace")
            out.append((str(getattr(btn, "text", "") or ""), str(data)))
    return out


_BASE_CONFIG = {
    "provider": "gemini", "model": "gemini-2.5-flash",
    "temperature": 0.7, "max_tokens": 4096, "history_budget": 4000,
    "system_prompt": "", "is_configured": True,
    "trigger_en": "Nova", "trigger_fa": "",
    "show_question": False,
}


@pytest.mark.asyncio
async def test_advanced_panel_shows_the_current_values_and_controls(monkeypatch):
    from backend.bot.handlers import ai as ai_module

    config = dict(_BASE_CONFIG, stt_model="gemini-2.5-pro", stt_language="fa-IR", stt_passes=3)
    monkeypatch.setattr(ai_module, "_get_saved_config", _async_return(config))
    monkeypatch.setattr(ai_module, "_get_owner_id", _async_return(1))

    title, body, buttons = await ai_module._ai_settings_adv_panel_handler(None, "")

    assert title == "Advanced"
    assert "Voice transcription model · gemini-2.5-pro" in body
    assert "Voice transcription language · fa-IR" in body
    assert "Voice recognition passes · 3" in body
    datas = [d for _, d in _flatten(buttons)]
    for key in ("stt_model", "stt_language", "stt_passes"):
        assert f"input:ai_settings:{key}" in datas


@pytest.mark.asyncio
async def test_advanced_panel_labels_the_defaults_plainly(monkeypatch):
    from backend.bot.handlers import ai as ai_module

    monkeypatch.setattr(ai_module, "_get_saved_config", _async_return(dict(_BASE_CONFIG)))
    monkeypatch.setattr(ai_module, "_get_owner_id", _async_return(1))

    _title, body, _buttons = await ai_module._ai_settings_adv_panel_handler(None, "")

    assert "Voice transcription model · Default" in body
    assert "Voice transcription language · Auto" in body
    assert "Voice recognition passes · 1 (single pass)" in body


@pytest.mark.asyncio
async def test_personal_settings_surface_states_the_transcription_state(monkeypatch):
    from backend.bot.handlers import ai as ai_module

    config = dict(_BASE_CONFIG, stt_model="gemini-2.5-pro", stt_language="", stt_passes=2)
    monkeypatch.setattr(ai_module, "_get_saved_config", _async_return(config))
    monkeypatch.setattr(ai_module, "_get_owner_id", _async_return(1))

    _title, body, _buttons = await ai_module._ai_settings_panel_handler(None, "")

    assert "Voice transcription · gemini-2.5-pro · auto · 2 passes" in body


@pytest.mark.asyncio
async def test_personal_settings_admits_an_unreadable_store(monkeypatch):
    from backend.bot.handlers import ai as ai_module

    config = dict(_BASE_CONFIG, **{config_store.DEGRADED_READ_KEY: True})
    monkeypatch.setattr(ai_module, "_get_saved_config", _async_return(config))
    monkeypatch.setattr(ai_module, "_get_owner_id", _async_return(1))

    _title, body, _buttons = await ai_module._ai_settings_panel_handler(None, "")

    assert "Voice transcription · unavailable (database read failed)" in body
    assert "Voice transcription · default model" not in body


def test_panel_labels_never_name_an_environment_variable():
    from backend.bot.handlers import ai as ai_module
    from backend.bot.handlers import ai_stt_settings

    source = inspect.getsource(ai_module._ai_settings_adv_panel_handler)
    source += inspect.getsource(ai_stt_settings)

    assert "AI_GEMINI_STT" not in source
    assert "os.getenv" not in source


# ── 5. The Telegram inputs change each value ──


def _async_return(value):
    async def _inner(*_args, **_kwargs):
        return value
    return _inner


class _Recorder:
    """Captures saves + panel edits from one input-handler run."""

    def __init__(self, monkeypatch, owner=4242):
        from backend.bot.handlers import ai as ai_module

        self.saved: list[tuple[int, str, object]] = []
        self.notices: list[str] = []
        self.applied_with: dict = {}
        #: The REAL store write, captured before the spy replaces it.
        self._real_update = config_store.update_setting
        self._real_get = config_store.get_config
        monkeypatch.setattr(ai_module, "_get_owner_id", _async_return(owner))
        monkeypatch.setattr(ai_module, "_get_saved_config", _async_return(dict(_BASE_CONFIG)))
        monkeypatch.setattr(
            ai_stt_settings_module(), "apply_stt_settings_now", self._apply,
        )
        self._monkeypatch = monkeypatch
        self.owner = owner

    async def _apply(self, owner_id):
        self.applied_with = await self._real_get(owner_id)
        return True

    async def _update(self, owner_id, key, value):
        self.saved.append((owner_id, key, value))
        await self._real_update(owner_id, key, value)
        return True

    def install_store(self):
        self._monkeypatch.setattr(config_store, "update_setting", self._update)

    def install_finish(self):
        from backend.bot.handlers import ai_stt_settings as module

        async def _finish(notice, chat_id, msg_id, inline_chat_id, inline_msg_id):
            self.notices.append(notice)

        self._monkeypatch.setattr(module, "_finish", _finish)

    def notice(self) -> str:
        assert self.notices, "the input flow never rendered a notice"
        return self.notices[-1]


def ai_stt_settings_module():
    from backend.bot.handlers import ai_stt_settings

    return ai_stt_settings


@pytest.mark.asyncio
async def test_input_sets_the_stt_model(monkeypatch):
    module = ai_stt_settings_module()
    rec = _Recorder(monkeypatch)
    rec.install_store()
    rec.install_finish()

    await module._ai_stt_model_input("gemini-2.5-pro", 100, 11, 100, 55)

    assert rec.saved == [(rec.owner, "stt_model", "gemini-2.5-pro")]
    assert rec.notice().startswith("✓")
    assert rec.applied_with["stt_model"] == "gemini-2.5-pro"


@pytest.mark.asyncio
async def test_input_resets_the_stt_model_to_the_default(monkeypatch):
    module = ai_stt_settings_module()
    rec = _Recorder(monkeypatch)
    rec.install_store()
    rec.install_finish()

    await module._ai_stt_model_input("reset", 100, 11, 100, 55)

    assert rec.saved == [(rec.owner, "stt_model", "")]
    assert "reset" in rec.notice()


@pytest.mark.asyncio
async def test_input_rejects_a_model_with_spaces(monkeypatch):
    module = ai_stt_settings_module()
    rec = _Recorder(monkeypatch)
    rec.install_store()
    rec.install_finish()

    await module._ai_stt_model_input("gemini 2.5 flash", 100, 11, 100, 55)

    assert rec.saved == []
    assert rec.notice().startswith("×")


@pytest.mark.asyncio
async def test_input_sets_an_explicit_language(monkeypatch):
    module = ai_stt_settings_module()
    rec = _Recorder(monkeypatch)
    rec.install_store()
    rec.install_finish()

    await module._ai_stt_language_input("fa-IR", 100, 11, 100, 55)

    assert rec.saved == [(rec.owner, "stt_language", "fa-IR")]
    assert rec.applied_with["stt_language"] == "fa-IR"


@pytest.mark.asyncio
@pytest.mark.parametrize("typed", ["auto", "AUTO", "reset", ""])
async def test_input_returns_the_language_to_automatic(monkeypatch, typed):
    module = ai_stt_settings_module()
    rec = _Recorder(monkeypatch)
    rec.install_store()
    rec.install_finish()

    await module._ai_stt_language_input(typed, 100, 11, 100, 55)

    assert rec.saved == [(rec.owner, "stt_language", "")]
    assert "automatic" in rec.notice()


@pytest.mark.asyncio
@pytest.mark.parametrize("typed", ["persian", "fa IR", "fa_IR", "f", "-IR"])
async def test_input_rejects_a_malformed_language(monkeypatch, typed):
    module = ai_stt_settings_module()
    rec = _Recorder(monkeypatch)
    rec.install_store()
    rec.install_finish()

    await module._ai_stt_language_input(typed, 100, 11, 100, 55)

    assert rec.saved == []
    assert rec.notice().startswith("×")


@pytest.mark.asyncio
@pytest.mark.parametrize("passes", [1, 2, 3])
async def test_input_accepts_each_valid_pass_count(monkeypatch, passes):
    module = ai_stt_settings_module()
    rec = _Recorder(monkeypatch)
    rec.install_store()
    rec.install_finish()

    await module._ai_stt_passes_input(str(passes), 100, 11, 100, 55)

    assert rec.saved == [(rec.owner, "stt_passes", passes)]
    assert rec.applied_with["stt_passes"] == passes


@pytest.mark.asyncio
@pytest.mark.parametrize("typed", ["0", "-1", "4", "9", "abc", "2.0", ""])
async def test_input_refuses_a_pass_count_outside_one_to_three(monkeypatch, typed):
    module = ai_stt_settings_module()
    rec = _Recorder(monkeypatch)
    rec.install_store()
    rec.install_finish()

    await module._ai_stt_passes_input(typed, 100, 11, 100, 55)

    assert rec.saved == []          # nothing stored — refused, not clamped
    assert rec.notice().startswith("×")


# ── 6. Runtime: the persisted settings reach the live Gemini STT engine ──


def test_the_live_engine_takes_the_persisted_settings(monkeypatch):
    from backend.services.gemini_media_engine import apply_stt_settings

    monkeypatch.setenv(API_KEY_VAR, API_KEY)

    status = apply_stt_settings(
        {"stt_model": "gemini-2.5-pro", "stt_language": "fa-IR", "stt_passes": 2}
    )

    assert status["configured"] is True
    assert _stt_engine_settings() == ("gemini-2.5-pro", "fa-IR", 2)


def test_behavioral_settings_do_not_require_render_env(monkeypatch):
    """ENV is not the settings database: the persisted values win outright."""
    from backend.services.gemini_media_engine import apply_stt_settings

    monkeypatch.setenv(API_KEY_VAR, API_KEY)
    monkeypatch.setenv("AI_GEMINI_STT_MODEL", "gemini-2.5-flash")
    monkeypatch.setenv("AI_GEMINI_STT_LANGUAGE", "en-US")
    monkeypatch.setenv("AI_GEMINI_STT_PASSES", "3")

    apply_stt_settings({"stt_model": "gemini-2.5-pro", "stt_language": "fa-IR", "stt_passes": 1})

    assert _stt_engine_settings() == ("gemini-2.5-pro", "fa-IR", 1)


def test_an_unconfigured_owner_is_provisioned_with_the_defaults(monkeypatch):
    from backend.services.gemini_media_engine import apply_stt_settings

    monkeypatch.setenv(API_KEY_VAR, API_KEY)

    apply_stt_settings({})

    assert _stt_engine_settings() == ("", "", 1)


def test_no_credential_stays_fail_closed(monkeypatch):
    from backend.services.gemini_media_engine import apply_stt_settings

    monkeypatch.delenv(API_KEY_VAR, raising=False)

    status = apply_stt_settings({"stt_model": "gemini-2.5-pro"})

    assert status["configured"] is False
    assert media_service.stt_available() is False


def test_context_isolation_nothing_but_the_three_settings_can_reach_the_engine(monkeypatch):
    from backend.services.gemini_media_engine import apply_stt_settings

    monkeypatch.setenv(API_KEY_VAR, API_KEY)
    poisoned = {
        "stt_model": "gemini-2.5-pro", "stt_language": "fa-IR", "stt_passes": 2,
        "owner_id": 12345, "chat_id": -100999, "message_id": 987,
        "sender": "someone", "username": "@someone", "caption": "secret caption",
        "filename": "voice.ogg", "history": ["earlier turn"], "memory": {"x": 1},
        "reply_text": "replied body",
    }

    apply_stt_settings(poisoned)

    engine = media_service.get_stt_engine()
    assert (engine.stt_model, engine.stt_language, engine.stt_passes) == (
        "gemini-2.5-pro", "fa-IR", 2,
    )
    blob = repr(engine) + repr(engine.__slots__)
    for leak in ("someone", "secret caption", "voice.ogg", "earlier turn"):
        assert leak not in blob


def test_the_engine_holds_no_owner_or_telegram_state():
    from backend.services.gemini_media_engine import GeminiMediaEngine

    slots = set(GeminiMediaEngine.__slots__)

    assert slots == {"_api_key", "_model", "_key_env_var", "_stt_model", "_stt_language", "_stt_passes"}


# ── 7. Startup application (the supervisor seam) ──


@pytest.mark.asyncio
async def test_startup_applies_the_persisted_settings(monkeypatch):
    from backend.runtime.supervisor import RuntimeSupervisor

    monkeypatch.setenv(API_KEY_VAR, API_KEY)
    supervisor = RuntimeSupervisor({
        "API_ID": 1, "API_HASH": "hash", "SESSION_STRING": "session",
        "OWNER_ID": 4242, "TZ": "UTC",
    })

    async def _config(owner_id):
        return {
            "stt_model": "gemini-2.5-pro", "stt_language": "fa-IR", "stt_passes": 3,
        }

    monkeypatch.setattr(config_store, "get_config", _config)

    await supervisor._apply_persisted_stt_settings()

    assert _stt_engine_settings() == ("gemini-2.5-pro", "fa-IR", 3)


@pytest.mark.asyncio
async def test_startup_keeps_the_bootstrap_settings_when_the_store_is_unreadable(monkeypatch):
    from backend.runtime.supervisor import RuntimeSupervisor

    monkeypatch.setenv(API_KEY_VAR, API_KEY)
    supervisor = RuntimeSupervisor({
        "API_ID": 1, "API_HASH": "hash", "SESSION_STRING": "session",
        "OWNER_ID": 4242, "TZ": "UTC",
    })

    async def _config(owner_id):
        return {
            "stt_model": "", "stt_language": "", "stt_passes": 1,
            config_store.DEGRADED_READ_KEY: True,
        }

    monkeypatch.setattr(config_store, "get_config", _config)
    from backend.services.gemini_media_engine import apply_stt_settings
    apply_stt_settings({"stt_model": "gemini-2.5-pro"})

    await supervisor._apply_persisted_stt_settings()

    assert _stt_engine_settings()[0] == "gemini-2.5-pro"  # bootstrap kept


# ── 8. Registration: one registry, the existing panel ──


def test_the_three_inputs_register_on_the_existing_settings_panel(monkeypatch):
    from backend.bot.handlers import ai_stt_settings as module

    registered: list[tuple[str, str]] = []
    monkeypatch.setattr(
        module, "register_input",
        lambda scope, key, cfg: registered.append((scope, key)),
    )

    module.register(None, 1)

    assert registered == [
        ("ai_settings", "stt_model"),
        ("ai_settings", "stt_language"),
        ("ai_settings", "stt_passes"),
    ]


def test_the_router_registers_the_stt_settings_module():
    import backend.bot.router as router

    source = inspect.getsource(router)

    assert "ai_stt_settings.register(client, owner_id)" in source


def test_no_second_configuration_store_was_created():
    """The values live on the existing ai_config row — nowhere else."""
    from backend.bot.handlers import ai_stt_settings as module

    source = inspect.getsource(module)

    assert "config_store" in source or "update_setting" in source
    assert "os.getenv" not in source
    assert "_fallback" not in source
    assert "supabase" not in source.lower()
