"""Media Analysis — the STT control plane and the AI → Media Analysis surface.

Covers the whole Phase-1 seam at the behavior level:

  1. the capability registry (provider + model candidates, deterministic order);
  2. the configuration model (active candidate, fallback order, language, passes);
  3. persistence through the EXISTING AI config store — no second store, no new
     column, no new table;
  4. the panel move: Media Analysis is under AI, and Speech-to-Text is no longer
     reachable from AI → Settings / Advanced;
  5. the bounded behavioral settings (language, passes 1..3) and the finite
     candidate selection (never a typed model identifier);
  6. legacy ``stt_model`` compatibility — an unregistered value is never
     silently re-pointed at a registered candidate;
  7. the control plane → engine conversion, with the engine still holding no
     owner, chat, message or caption state.

Nothing here claims recognition QUALITY or provider HEALTH: these tests prove
configuration plumbing, not transcription accuracy and not reachability.
"""
from __future__ import annotations

import inspect

import pytest

from backend.ai import config_store
from backend.ai import stt_control_plane as control_plane
from backend.services import media_service

API_KEY = "test-stt-settings-key"
API_KEY_VAR = "AI_GEMINI_API_KEY"


def _stt_engine_settings():
    engine = media_service.get_stt_engine()
    assert engine is not None
    return engine.stt_model, engine.stt_language, engine.stt_passes


@pytest.fixture(autouse=True)
def _clean_engine(monkeypatch):
    """No credential and no media engine leaks between tests."""
    monkeypatch.delenv(API_KEY_VAR, raising=False)
    media_service.set_stt_engine(None)
    media_service.set_ocr_engine(None)
    yield
    media_service.set_stt_engine(None)
    media_service.set_ocr_engine(None)


def _async_return(value):
    async def _inner(*_args, **_kwargs):
        return value
    return _inner


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


def _patch_ai(monkeypatch, config: dict | None = None, owner: int = 1):
    from backend.bot.handlers import ai as ai_module

    monkeypatch.setattr(ai_module, "_get_saved_config", _async_return(dict(config or _BASE_CONFIG)))
    monkeypatch.setattr(ai_module, "_get_owner_id", _async_return(owner))


# ── 1. Defaults ────────────────────────────────────────────────────────


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


def test_an_empty_selection_is_the_default_candidate_route():
    plane = control_plane.parse_stt_config({})

    assert plane.active_id == control_plane.DEFAULT_CANDIDATE_ID
    assert plane.engine_model() == ""
    assert plane.engine_settings() == {"stt_model": "", "stt_language": "", "stt_passes": 1}


def test_the_engine_defaults_are_the_same_behavior():
    from backend.services.gemini_media_engine import stt_settings_from

    resolved = stt_settings_from(control_plane.engine_settings({}))

    assert resolved == {"stt_model": "", "stt_language": "", "stt_passes": 1}


# ── 2. The candidate registry: provider + model identity ───────────────


def test_candidates_carry_provider_and_model_identity():
    for candidate in control_plane.all_candidates():
        assert candidate.candidate_id
        assert candidate.provider
        assert candidate.label
        if candidate.model:
            assert candidate.candidate_id == f"{candidate.provider}:{candidate.model}"

    default = control_plane.default_candidate()
    assert default.candidate_id == "gemini:default"
    assert default.model == ""
    assert default.is_provider_default


def test_the_registry_is_more_than_one_provider():
    providers = {c.provider for c in control_plane.all_candidates()}

    assert {"gemini", "groq", "speechmatics"} <= providers


def test_only_implemented_candidates_are_selectable():
    implemented = {c.candidate_id for c in control_plane.all_candidates() if c.implemented}
    assert control_plane.is_selectable(control_plane.DEFAULT_CANDIDATE_ID)
    assert implemented == {c.candidate_id for c in control_plane.all_candidates() if control_plane.is_selectable(c.candidate_id)}

    # Every registered capability currently executes; the registry can still hold
    # a future one, and such a candidate is never selectable.
    assert implemented == {c.candidate_id for c in control_plane.all_candidates()}
    future = control_plane.SttCandidate(
        candidate_id="future:standard", provider="future", model="standard",
        label="Future provider", implemented=False,
    )
    assert not control_plane.is_selectable(future.candidate_id)


def test_a_registered_but_unimplemented_active_candidate_is_reported(monkeypatch):
    """The defensive path stays: a capability with no execution path is never run."""
    future = control_plane.SttCandidate(
        candidate_id="future:standard", provider="future", model="standard",
        label="Future provider", implemented=False,
    )
    monkeypatch.setattr(control_plane, "get_candidate", lambda candidate_id: future)

    plane = control_plane.SttControlPlane(
        active_id=future.candidate_id, fallback_ids=(), language="", passes=1,
    )

    assert plane.active_unavailable is True
    assert plane.engine_model() == ""


def test_candidate_status_is_not_credential_presence(monkeypatch):
    """A key existing is NOT a successful test — the registry never says health."""
    monkeypatch.setenv("AI_GROQ_API_KEY", "some-key")

    for candidate in control_plane.all_candidates():
        assert not hasattr(candidate, "healthy")
        assert "credential" not in repr(candidate).lower()
        assert "api_key" not in repr(candidate).lower()


# ── 3. Deterministic ordering ──────────────────────────────────────────


def test_candidate_order_is_canonical_and_repeatable():
    first = control_plane.candidate_ids()

    assert first == control_plane.candidate_ids()
    assert first[0] == control_plane.DEFAULT_CANDIDATE_ID


def test_the_fallback_order_puts_the_active_candidate_first():
    plane = control_plane.parse_stt_config({"stt_model": "gemini:gemini-3.5-transcribe"})

    ordered = plane.ordered_candidate_ids
    assert ordered[0] == "gemini:gemini-3.5-transcribe"
    assert set(ordered) == set(control_plane.candidate_ids())
    assert plane.fallback_ids == ordered[1:]
    assert "gemini:default" in plane.fallback_ids


def test_the_fallback_order_is_stable_across_parses():
    config = {"stt_model": "gemini:gemini-3.5-transcribe"}

    assert (
        control_plane.parse_stt_config(config).ordered_candidate_ids
        == control_plane.parse_stt_config(config).ordered_candidate_ids
    )


# ── 4. Persistence through the EXISTING config store ───────────────────


@pytest.mark.asyncio
async def test_the_active_candidate_persists_and_reloads():
    owner = 777000102
    assert await config_store.update_setting(
        owner, "stt_model", control_plane.storage_value("gemini:gemini-3.5-transcribe")
    )

    reloaded = control_plane.parse_stt_config(await config_store.get_config(owner))

    assert reloaded.active_id == "gemini:gemini-3.5-transcribe"
    assert reloaded.ordered_candidate_ids[0] == "gemini:gemini-3.5-transcribe"
    assert reloaded.fallback_ids == control_plane.fallback_ids("gemini:gemini-3.5-transcribe")


@pytest.mark.asyncio
async def test_the_default_candidate_is_stored_as_nothing_configured():
    owner = 777000103

    assert control_plane.storage_value(control_plane.DEFAULT_CANDIDATE_ID) == ""
    await config_store.update_setting(owner, "stt_model", "")

    assert (await config_store.get_config(owner))["stt_model"] == ""
    assert control_plane.parse_stt_config(await config_store.get_config(owner)).active_id == (
        control_plane.DEFAULT_CANDIDATE_ID
    )


@pytest.mark.asyncio
async def test_an_unset_language_stays_automatic_after_a_reload():
    owner = 777000104

    await config_store.update_setting(owner, "stt_language", "fa-IR")
    await config_store.update_setting(owner, "stt_language", "")

    assert (await config_store.get_config(owner))["stt_language"] == ""


@pytest.mark.asyncio
@pytest.mark.parametrize("passes", [1, 2, 3])
async def test_persisting_each_valid_pass_count(passes):
    owner = 777000110 + passes

    assert await config_store.update_setting(owner, "stt_passes", passes)

    assert (await config_store.get_config(owner))["stt_passes"] == passes
    assert control_plane.parse_stt_config(await config_store.get_config(owner)).passes == passes


def test_the_upsert_payload_carries_all_three_keys():
    """A missing column degrades the whole upsert to RAM — so the payload must
    carry the three keys, exactly like every other ai_config setting, and the
    control plane needs no new column."""
    source = inspect.getsource(config_store._save_config_sync)

    for key in control_plane.STORAGE_KEYS:
        assert f'"{key}"' in source


def test_no_second_configuration_store_was_created():
    from backend.bot.handlers import ai_stt_settings as module

    source = inspect.getsource(module)
    plane_source = inspect.getsource(control_plane)

    assert "config_store" in source or "update_setting" in source
    for text in (source, plane_source):
        assert "os.getenv" not in text
        assert "_fallback" not in text
        assert "supabase" not in text.lower()


# ── 5. Language and passes bounds ──────────────────────────────────────


def test_the_engine_bound_is_the_documented_maximum():
    from backend.services.gemini_media_engine import STT_MAX_PASSES

    assert STT_MAX_PASSES == 3


@pytest.mark.parametrize("value", [0, -1, 4, 9, 100])
def test_out_of_range_passes_are_held_inside_the_bound(value):
    assert control_plane.parse_stt_config({"stt_passes": value}).passes == max(1, min(value, 3))


def test_auto_language_alias_becomes_automatic():
    assert control_plane.parse_stt_config({"stt_language": "auto"}).language == ""
    assert control_plane.parse_stt_config({"stt_language": "AUTO"}).language == ""
    assert control_plane.parse_stt_config({"stt_language": "fa-IR"}).language == "fa-IR"


# ── 6. No manual model entry ───────────────────────────────────────────


def test_storage_value_refuses_an_unregistered_candidate():
    with pytest.raises(ValueError):
        control_plane.storage_value("gemini-3.5-transcribe")


@pytest.mark.parametrize(
    "typed",
    ["gemini-3.5-transcribe", "whisper-large-v3", "gemini", "groq", "gemini:unknown-model"],
)
def test_a_typed_model_identifier_is_never_a_candidate(typed):
    assert control_plane.get_candidate(typed) is None
    assert not control_plane.is_selectable(typed)


@pytest.mark.asyncio
async def test_the_stt_settings_panel_exposes_the_two_bounded_inputs(monkeypatch):
    """The language and pass controls moved here — same ids, same payloads."""
    from backend.bot.handlers import ai_stt_settings as module

    _patch_ai(monkeypatch)
    _title, _body, buttons = await module._ai_media_stt_settings_panel_handler(None, "")

    inputs = [d for _text, d in _flatten(buttons) if d.startswith("input:")]
    assert set(inputs) == {
        "input:ai_media_stt:stt_language", "input:ai_media_stt:stt_passes",
    }


@pytest.mark.asyncio
async def test_the_stt_panel_offers_registered_candidates_only(monkeypatch):
    from backend.bot.handlers import ai_stt_settings as module

    _patch_ai(monkeypatch)
    _title, body, buttons = await module._ai_media_stt_panel_handler(None, "")

    datas = [d for _, d in _flatten(buttons)]
    offered = [d for d in datas if d.startswith("action:ai_stt_select_candidate:")]
    assert offered, "the panel must offer the registered candidates"
    for data in offered:
        candidate_id = data.split(":", 2)[2]
        assert control_plane.get_candidate(candidate_id) is not None
        assert control_plane.is_selectable(candidate_id)
    # Never a free-form model field, and no settings control on the main screen:
    # the bounded settings live one level down in STT Settings.
    assert [d for d in datas if d.startswith("input:")] == []


# ── 7. Legacy compatibility ────────────────────────────────────────────


@pytest.mark.parametrize("legacy", ["gemini-2.5-pro", "gemini-9.9-custom", "gemini:retired-model"])
def test_an_unregistered_stored_value_is_legacy_unresolved(legacy):
    plane = control_plane.parse_stt_config({"stt_model": legacy})

    assert plane.is_legacy is True
    assert plane.active_candidate is None
    assert plane.ordered_candidate_ids == ()
    # never silently substituted by a DIFFERENT registered model
    assert plane.engine_model() == legacy
    assert plane.engine_model() not in control_plane.candidate_ids()


def test_legacy_configuration_does_not_break_loading():
    plane = control_plane.parse_stt_config(
        {"stt_model": "gemini-2.5-pro", "stt_language": "fa-IR", "stt_passes": 2}
    )

    assert (plane.language, plane.passes) == ("fa-IR", 2)
    assert plane.engine_settings() == {
        "stt_model": "gemini-2.5-pro", "stt_language": "fa-IR", "stt_passes": 2,
    }


@pytest.mark.asyncio
async def test_a_legacy_value_survives_a_config_store_round_trip():
    owner = 777000120
    await config_store.update_setting(owner, "stt_model", "gemini-2.5-pro")

    plane = control_plane.parse_stt_config(await config_store.get_config(owner))

    assert plane.is_legacy and plane.engine_model() == "gemini-2.5-pro"


@pytest.mark.asyncio
async def test_the_stt_panel_reports_a_legacy_value_and_offers_the_pool(monkeypatch):
    from backend.bot.handlers import ai_stt_settings as module

    _patch_ai(monkeypatch, dict(_BASE_CONFIG, stt_model="gemini-2.5-pro"))
    _title, body, buttons = await module._ai_media_stt_panel_handler(None, "")

    assert "gemini-2.5-pro" in body
    assert "registered candidate" in body
    offered = [d for _, d in _flatten(buttons) if d.startswith("action:ai_stt_select_candidate:")]
    assert offered


# ── 8. Panels: AI → Media Analysis ─────────────────────────────────────


@pytest.mark.asyncio
async def test_media_analysis_panel_shows_ocr_and_speech_to_text(monkeypatch):
    from backend.bot.handlers import ai_stt_settings as module

    _patch_ai(monkeypatch)
    title, body, buttons = await module._ai_media_panel_handler(None, "")

    assert title == "Media Analysis"
    assert "Text recognition · " in body
    assert "Speech-to-Text · " in body
    datas = [d for _, d in _flatten(buttons)]
    assert "panel:ai_media_ocr" in datas
    assert "panel:ai_media_stt" in datas


@pytest.mark.asyncio
async def test_the_ocr_panel_is_read_only_and_honest(monkeypatch):
    from backend.bot.handlers import ai_stt_settings as module

    _patch_ai(monkeypatch)
    _title, body, buttons = await module._ai_media_ocr_panel_handler(None, "")

    assert "No OCR engine on this runtime" in body
    assert [d for _, d in _flatten(buttons) if d.startswith("input:")] == []

    class _Engine:
        def recognize(self, image: bytes) -> str:
            return ""

    media_service.set_ocr_engine(_Engine())
    _title, body, _buttons = await module._ai_media_ocr_panel_handler(None, "")
    assert "No OCR engine" not in body


@pytest.mark.asyncio
async def test_the_stt_panel_shows_the_active_candidate_and_its_state(monkeypatch):
    from backend.bot.handlers import ai_stt_settings as module

    config = dict(
        _BASE_CONFIG, stt_model="gemini:gemini-3.5-transcribe",
        stt_language="fa-IR", stt_passes=3,
    )
    _patch_ai(monkeypatch, config)
    title, body, buttons = await module._ai_media_stt_panel_handler(None, "")

    assert title == "Speech-to-Text"
    assert "Active · Gemini Transcribe" in body
    assert "Language · fa-IR" in body
    assert "Passes · 3" in body
    assert "Providers" in body
    # the active candidate is marked in the provider list and not offered again
    assert "1. Gemini Transcribe · active" in body
    datas = [d for _, d in _flatten(buttons)]
    assert "action:ai_stt_select_candidate:gemini:gemini-3.5-transcribe" not in datas


@pytest.mark.asyncio
async def test_the_stt_panel_is_compact(monkeypatch):
    """A control panel, not documentation: no long explanatory paragraph."""
    from backend.bot.handlers import ai_stt_settings as module

    _patch_ai(monkeypatch)
    _title, body, _buttons = await module._ai_media_stt_panel_handler(None, "")

    assert len(body) < 500, "the screen is a control panel, not documentation"
    assert max(len(line) for line in body.splitlines()) <= 80
    assert "Pick a registered candidate" not in body
    assert "no model names to type" not in body
    assert "Test all providers runs" not in body
    assert len([line for line in body.splitlines() if line.startswith("_")]) <= 1


@pytest.mark.asyncio
async def test_candidate_buttons_are_laid_out_two_per_row(monkeypatch):
    """Two-column grid, built from the registry, canonical order preserved."""
    from backend.bot.handlers import ai_stt_settings as module

    _patch_ai(monkeypatch)
    _title, _body, buttons = await module._ai_media_stt_panel_handler(None, "")

    candidate_rows = [
        row for row in (_flatten([row]) for row in buttons)
        if row and all(data.startswith("action:ai_stt_select_candidate:") for _text, data in row)
    ]
    offered = [data for row in candidate_rows for _text, data in row]
    expected = [
        f"action:ai_stt_select_candidate:{candidate.candidate_id}"
        for candidate in control_plane.all_candidates()
        if candidate.implemented and candidate.candidate_id != control_plane.DEFAULT_CANDIDATE_ID
    ]

    assert len(offered) == len(set(offered))
    assert set(offered) == set(expected)
    assert all(len(row) <= 2 for row in candidate_rows)
    assert any(len(row) == 2 for row in candidate_rows)
    # only the last row may be short, so the grid never leaves a hole
    for row in candidate_rows[:-1]:
        assert len(row) == 2


@pytest.mark.asyncio
async def test_candidate_rows_follow_the_registry_order(monkeypatch):
    from backend.bot.handlers import ai_stt_settings as module

    _patch_ai(monkeypatch)
    _title, _body, buttons = await module._ai_media_stt_panel_handler(None, "")
    datas = [d for _text, d in _flatten(buttons) if d.startswith("action:ai_stt_select_candidate:")]

    ordered = [
        candidate.candidate_id for candidate in control_plane.all_candidates()
        if candidate.implemented
    ]
    assert datas == [
        f"action:ai_stt_select_candidate:{candidate_id}"
        for candidate_id in ordered
        if candidate_id != control_plane.DEFAULT_CANDIDATE_ID
    ]


@pytest.mark.asyncio
async def test_the_behavioral_controls_moved_into_stt_settings(monkeypatch):
    from backend.bot.handlers import ai_stt_settings as module

    _patch_ai(monkeypatch)
    _title, main_body, main_buttons = await module._ai_media_stt_panel_handler(None, "")
    main_flat = _flatten(main_buttons)

    # No settings control on the main screen; the values stay visible.
    assert "input:ai_media_stt:stt_language" not in [d for _text, d in main_flat]
    assert "input:ai_media_stt:stt_passes" not in [d for _text, d in main_flat]
    assert ("Language…", "input:ai_media_stt:stt_language") not in main_flat
    assert ("Recognition passes…", "input:ai_media_stt:stt_passes") not in main_flat
    assert "Language · Auto" in main_body
    assert "Passes · 1" in main_body
    # One way into the nested settings panel.
    assert ("\u2699 STT Settings", "panel:ai_media_stt_settings") in main_flat


@pytest.mark.asyncio
async def test_the_stt_settings_panel_shows_the_current_values(monkeypatch):
    from backend.bot.handlers import ai_stt_settings as module

    _patch_ai(monkeypatch, dict(_BASE_CONFIG, stt_language="fa-IR", stt_passes=3))
    title, body, buttons = await module._ai_media_stt_settings_panel_handler(None, "")
    flat = _flatten(buttons)

    assert title == "STT Settings"
    assert "Language · fa-IR" in body
    assert "Passes · 3" in body
    assert ("Language…", "input:ai_media_stt:stt_language") in flat
    assert ("Recognition passes…", "input:ai_media_stt:stt_passes") in flat
    assert ("← Back", "panel:_nav:back") in flat
    assert ("⌂ Home", "panel:_nav:home") in flat


@pytest.mark.asyncio
async def test_every_stt_callback_payload_is_unchanged(monkeypatch):
    from backend.bot.handlers import ai_stt_settings as module

    _patch_ai(monkeypatch)
    _title, _body, main_buttons = await module._ai_media_stt_panel_handler(None, "")
    _title2, _body2, settings_buttons = await module._ai_media_stt_settings_panel_handler(None, "")
    datas = {d for _text, d in _flatten(main_buttons) + _flatten(settings_buttons)}

    assert "action:ai_stt_test_all" in datas
    assert "panel:ai_media_stt_settings" in datas
    assert "input:ai_media_stt:stt_language" in datas
    assert "input:ai_media_stt:stt_passes" in datas
    assert {"panel:_nav:back", "panel:_nav:home"} <= datas


@pytest.mark.asyncio
async def test_the_stt_ui_carries_no_secret_or_telegram_metadata(monkeypatch):
    from backend.bot.handlers import ai_stt_settings as module

    _patch_ai(monkeypatch, dict(_BASE_CONFIG, stt_language="fa-IR", stt_passes=2))
    rendered: list[str] = []
    for handler in (module._ai_media_stt_panel_handler, module._ai_media_stt_settings_panel_handler):
        title, body, buttons = await handler(None, "")
        rendered.append(title)
        rendered.append(body)
        rendered.extend(text for text, _data in _flatten(buttons))
    blob = "\n".join(rendered)

    for leak in ("AI_", "@", "api key", "token", "credential"):
        assert leak.lower() not in blob.lower()


@pytest.mark.asyncio
async def test_the_stt_panel_admits_an_unreadable_store(monkeypatch):
    from backend.bot.handlers import ai_stt_settings as module

    _patch_ai(monkeypatch, dict(_BASE_CONFIG, **{config_store.DEGRADED_READ_KEY: True}))
    _title, body, _buttons = await module._ai_media_stt_panel_handler(None, "")

    assert "unavailable (database read failed)" in body


@pytest.mark.asyncio
async def test_the_stt_panel_still_warns_about_an_unavailable_active_candidate(monkeypatch):
    """A real runtime warning stays visible on the compact screen.

    Every registered capability currently executes, so this state is reached
    through a forged control plane — exactly how the later test/fallback phase
    would report a candidate that exists in the registry but cannot run here.
    """
    from types import SimpleNamespace

    from backend.bot.handlers import ai_stt_settings as module

    _patch_ai(monkeypatch)
    plane = SimpleNamespace(
        is_legacy=False, active_id="future:standard", active_unavailable=True,
        active_candidate=SimpleNamespace(label="Future provider"),
        language="", passes=1, ordered_candidates=(),
    )
    monkeypatch.setattr(module, "parse_stt_config", lambda _config: plane)
    monkeypatch.setattr(module, "all_candidates", lambda: ())

    _title, body, buttons = await module._ai_media_stt_panel_handler(None, "")

    assert "Active · Future provider" in body
    assert "registered but not available on this runtime" in body
    assert "the default route is used" in body
    assert "panel:ai_media_stt_settings" in [d for _text, d in _flatten(buttons)]


@pytest.mark.asyncio
async def test_the_settings_surfaces_no_longer_show_speech_to_text(monkeypatch):
    from backend.bot.handlers import ai as ai_module

    config = dict(_BASE_CONFIG, stt_model="gemini:gemini-3.5-transcribe", stt_language="fa-IR", stt_passes=3)
    _patch_ai(monkeypatch, config)

    for handler in (ai_module._ai_settings_panel_handler, ai_module._ai_settings_adv_panel_handler):
        _title, body, buttons = await handler(None, "")
        datas = [d for _, d in _flatten(buttons)]
        assert "transcription" not in body.lower()
        assert "Voice recognition" not in body
        assert not any("stt_" in d for d in datas)


@pytest.mark.asyncio
async def test_the_ai_main_panel_links_to_media_analysis(monkeypatch):
    from backend.bot.handlers import ai as ai_module

    _patch_ai(monkeypatch)
    _title, _body, buttons = await ai_module._ai_main_panel_handler(None, "")

    assert "panel:ai_media" in [d for _, d in _flatten(buttons)]


def test_panel_labels_never_name_an_environment_variable():
    from backend.bot.handlers import ai as ai_module
    from backend.bot.handlers import ai_stt_settings

    source = inspect.getsource(ai_module._ai_settings_adv_panel_handler)
    source += inspect.getsource(ai_stt_settings)

    assert "AI_GEMINI_STT" not in source
    assert "AI_GROQ" not in source
    assert "AI_SPEECHMATICS" not in source
    assert "os.getenv" not in source


# ── 9. Registration ────────────────────────────────────────────────────


def test_the_router_registers_the_media_analysis_module():
    import backend.bot.router as router

    source = inspect.getsource(router)

    assert "ai_stt_settings.register(client, owner_id)" in source


def test_media_analysis_panels_register_under_ai(monkeypatch):
    from backend.bot.handlers import ai_stt_settings as module

    panels: list[tuple[str, str]] = []
    monkeypatch.setattr(
        module, "register_panel",
        lambda panel_id, handler, parent="menu", title="": panels.append((panel_id, parent)),
    )
    monkeypatch.setattr(module, "register_inline_builder", lambda *a, **k: None)
    monkeypatch.setattr(module, "register_action", lambda *a, **k: None)
    monkeypatch.setattr(module, "register_input", lambda *a, **k: None)

    module.register(None, 1)

    assert ("ai_media", "ai") in panels
    assert ("ai_media_ocr", "ai_media") in panels
    assert ("ai_media_stt", "ai_media") in panels
    assert ("ai_media_stt_settings", "ai_media_stt") in panels


@pytest.mark.asyncio
async def test_back_from_stt_settings_returns_to_speech_to_text(monkeypatch):
    """The shared navigation stack, not a second one: Back lands on the STT panel."""
    from backend.helper import panels as panels_module

    sessions = panels_module.get_lifecycle().sessions
    sessions.push_nav(910001, 910002, "ai_media_stt", "")
    sessions.push_nav(910001, 910002, "ai_media_stt_settings", "")

    visited: list[str] = []

    async def recorder(event, extra):
        visited.append(extra)
        return ("Speech-to-Text", "body", [])

    async def no_render(*_args, **_kwargs):
        return None

    monkeypatch.setattr(
        panels_module, "get_panel",
        lambda panel_id: recorder if panel_id == "ai_media_stt" else None,
    )
    monkeypatch.setattr(panels_module, "_render_and_edit", no_render)

    await panels_module._handle_navigation(None, "back", 910001, 910002, 4242)

    assert visited == [""]
    assert sessions.current_nav(910001, 910002) == ("ai_media_stt", "")


def test_stt_controls_register_on_the_media_panel_only(monkeypatch):
    from backend.bot.handlers import ai as ai_module
    from backend.bot.handlers import ai_stt_settings as stt_module

    inputs: list[tuple[str, str]] = []
    actions: list[str] = []
    monkeypatch.setattr(ai_module, "register_panel", lambda *a, **k: None)
    monkeypatch.setattr(ai_module, "register_inline_builder", lambda *a, **k: None)
    monkeypatch.setattr(ai_module, "register_action", lambda *a, **k: None)
    monkeypatch.setattr(
        ai_module, "register_input",
        lambda scope, key, cfg: inputs.append((scope, key)),
    )
    monkeypatch.setattr(stt_module, "register_panel", lambda *a, **k: None)
    monkeypatch.setattr(stt_module, "register_inline_builder", lambda *a, **k: None)
    monkeypatch.setattr(
        stt_module, "register_action", lambda action_id, handler: actions.append(action_id),
    )
    monkeypatch.setattr(
        stt_module, "register_input",
        lambda scope, key, cfg: inputs.append((scope, key)),
    )

    ai_module.register(None, 0)
    stt_module.register(None, 0)

    settings_scope = [k for scope, k in inputs if scope == "ai_settings"]
    assert len(settings_scope) == len(set(settings_scope)) == 6
    assert not {"stt_model", "stt_language", "stt_passes"} & set(settings_scope)

    stt_scope = sorted(k for scope, k in inputs if scope == "ai_media_stt")
    assert stt_scope == ["stt_language", "stt_passes"]
    assert {"ai_stt_select_candidate", "ai_stt_test_all"} <= set(actions)


def test_the_settings_advanced_panel_keeps_its_unrelated_controls():
    from backend.bot.handlers import ai as ai_module

    source = inspect.getsource(ai_module._ai_settings_adv_panel_handler)

    for key in ("temperature", "max_tokens", "history_budget", "system_prompt"):
        assert f"input:ai_settings:{key}" in source


# ── 10. Inputs and the candidate action ────────────────────────────────


class _Recorder:
    """Captures store writes, panel notices and live-engine applications."""

    def __init__(self, monkeypatch, owner=4242, config=None):
        from backend.bot.handlers import ai as ai_module
        from backend.bot.handlers import ai_stt_settings as module

        self.module = module
        self.saved: list[tuple[int, str, object]] = []
        self.notices: list[str] = []
        self.applied_with: dict = {}
        self._real_update = config_store.update_setting
        self._real_get = config_store.get_config
        monkeypatch.setattr(ai_module, "_get_owner_id", _async_return(owner))
        monkeypatch.setattr(
            ai_module, "_get_saved_config", _async_return(dict(config or _BASE_CONFIG)),
        )
        monkeypatch.setattr(ai_module, "_finish_input", self._finish_input)
        monkeypatch.setattr(module, "apply_stt_settings_now", self._apply)
        monkeypatch.setattr(config_store, "update_setting", self._update)
        self.owner = owner

    async def _apply(self, owner_id):
        self.applied_with = await self._real_get(owner_id)
        return True

    async def _update(self, owner_id, key, value):
        self.saved.append((owner_id, key, value))
        await self._real_update(owner_id, key, value)
        return True

    async def _finish_input(self, notice, restore_panel, chat_id, msg_id, inline_chat_id, inline_msg_id):
        self.notices.append(notice)

    def notice(self) -> str:
        assert self.notices, "the input flow never rendered a notice"
        return self.notices[-1]


@pytest.mark.asyncio
async def test_input_sets_an_explicit_language(monkeypatch):
    rec = _Recorder(monkeypatch)

    await rec.module._ai_stt_language_input("fa-IR", 100, 11, 100, 55)

    assert rec.saved == [(rec.owner, "stt_language", "fa-IR")]
    assert rec.applied_with["stt_language"] == "fa-IR"
    assert rec.notice().startswith("✓")


@pytest.mark.asyncio
@pytest.mark.parametrize("typed", ["auto", "AUTO", "reset", ""])
async def test_input_returns_the_language_to_automatic(monkeypatch, typed):
    rec = _Recorder(monkeypatch)

    await rec.module._ai_stt_language_input(typed, 100, 11, 100, 55)

    assert rec.saved == [(rec.owner, "stt_language", "")]
    assert "automatic" in rec.notice()


@pytest.mark.asyncio
@pytest.mark.parametrize("typed", ["persian", "fa IR", "fa_IR", "f", "-IR"])
async def test_input_rejects_a_malformed_language(monkeypatch, typed):
    rec = _Recorder(monkeypatch)

    await rec.module._ai_stt_language_input(typed, 100, 11, 100, 55)

    assert rec.saved == []
    assert rec.notice().startswith("×")


@pytest.mark.asyncio
@pytest.mark.parametrize("passes", [1, 2, 3])
async def test_input_accepts_each_valid_pass_count(monkeypatch, passes):
    rec = _Recorder(monkeypatch)

    await rec.module._ai_stt_passes_input(str(passes), 100, 11, 100, 55)

    assert rec.saved == [(rec.owner, "stt_passes", passes)]
    assert rec.applied_with["stt_passes"] == passes


@pytest.mark.asyncio
@pytest.mark.parametrize("typed", ["0", "-1", "4", "9", "abc", "2.0", ""])
async def test_input_refuses_a_pass_count_outside_one_to_three(monkeypatch, typed):
    rec = _Recorder(monkeypatch)

    await rec.module._ai_stt_passes_input(typed, 100, 11, 100, 55)

    assert rec.saved == []          # nothing stored — refused, not clamped
    assert rec.notice().startswith("×")


@pytest.mark.asyncio
async def test_the_candidate_action_selects_a_registered_candidate(monkeypatch):
    rec = _Recorder(monkeypatch)

    result = await rec.module._ai_stt_select_candidate_action(None, "gemini:gemini-3.5-transcribe", 1)

    assert rec.saved == [(rec.owner, "stt_model", "gemini:gemini-3.5-transcribe")]
    assert result is not None and "Gemini Transcribe" in result[1]


@pytest.mark.asyncio
async def test_the_candidate_action_selects_the_default_as_empty(monkeypatch):
    rec = _Recorder(monkeypatch)

    await rec.module._ai_stt_select_candidate_action(None, "gemini:default", 1)

    assert rec.saved == [(rec.owner, "stt_model", "")]


@pytest.mark.asyncio
async def test_the_candidate_action_refuses_an_unregistered_id(monkeypatch):
    rec = _Recorder(monkeypatch)

    result = await rec.module._ai_stt_select_candidate_action(None, "gemini-3.5-transcribe", 1)

    assert rec.saved == []
    assert result is not None and result[1].startswith("× Unknown")


@pytest.mark.asyncio
async def test_the_candidate_action_refuses_an_unimplemented_candidate(monkeypatch):
    """A registered capability with no execution path can never be selected."""
    rec = _Recorder(monkeypatch)
    future = control_plane.SttCandidate(
        candidate_id="future:standard", provider="future", model="standard",
        label="Future provider", implemented=False,
    )
    monkeypatch.setattr(rec.module, "get_candidate", lambda candidate_id: future)

    result = await rec.module._ai_stt_select_candidate_action(None, future.candidate_id, 1)

    assert rec.saved == []
    assert result is not None and "not available" in result[1]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "candidate_id",
    ["groq:whisper-large-v3", "groq:whisper-large-v3-turbo", "speechmatics:standard"],
)
async def test_the_candidate_action_accepts_every_implemented_candidate(monkeypatch, candidate_id):
    """M2.1 made the Groq candidates selectable and M2.2 the Speechmatics one."""
    rec = _Recorder(monkeypatch)

    result = await rec.module._ai_stt_select_candidate_action(None, candidate_id, 1)

    assert rec.saved == [(rec.owner, "stt_model", candidate_id)]
    assert result is not None and result[1].startswith("✓")


# ── 11. Runtime: the persisted selection reaches the live STT engine ───


def test_the_live_engine_takes_the_persisted_selection(monkeypatch):
    from backend.services.gemini_media_engine import apply_stt_settings

    monkeypatch.setenv(API_KEY_VAR, API_KEY)

    status = apply_stt_settings(
        control_plane.engine_settings({
            "stt_model": "gemini:gemini-3.5-transcribe",
            "stt_language": "fa-IR",
            "stt_passes": 2,
        })
    )

    assert status["configured"] is True
    assert _stt_engine_settings() == ("gemini-3.5-transcribe", "fa-IR", 2)


def test_behavioral_settings_do_not_require_render_env(monkeypatch):
    """ENV is not the settings database: the persisted values win outright."""
    from backend.services.gemini_media_engine import apply_stt_settings

    monkeypatch.setenv(API_KEY_VAR, API_KEY)
    monkeypatch.setenv("AI_GEMINI_STT_MODEL", "gemini-2.5-flash")
    monkeypatch.setenv("AI_GEMINI_STT_LANGUAGE", "en-US")
    monkeypatch.setenv("AI_GEMINI_STT_PASSES", "3")

    apply_stt_settings(control_plane.engine_settings(
        {"stt_model": "gemini:gemini-3.5-transcribe", "stt_language": "fa-IR", "stt_passes": 1}
    ))

    assert _stt_engine_settings() == ("gemini-3.5-transcribe", "fa-IR", 1)


def test_the_default_candidate_keeps_the_general_media_route(monkeypatch):
    from backend.services.gemini_media_engine import apply_stt_settings

    monkeypatch.setenv(API_KEY_VAR, API_KEY)

    apply_stt_settings(control_plane.engine_settings({}))

    assert _stt_engine_settings() == ("", "", 1)


def test_a_non_gemini_candidate_never_becomes_a_gemini_model(monkeypatch):
    """The Gemini leg of the conversion maps any other provider to NO model."""
    from backend.services.gemini_media_engine import apply_stt_settings

    monkeypatch.setenv(API_KEY_VAR, API_KEY)

    status = apply_stt_settings(
        control_plane.engine_settings({"stt_model": "speechmatics:standard"})
    )

    assert status["configured"] is True
    assert _stt_engine_settings()[0] == ""


def test_a_selected_speechmatics_candidate_never_reaches_the_gemini_leg(monkeypatch):
    """The runtime resolver sends the selection to its OWN provider (M2.2)."""
    from backend.services import speechmatics_stt_engine, stt_engine_factory

    monkeypatch.setenv("AI_SPEECHMATICS_API_KEY", API_KEY)

    status = stt_engine_factory.apply_stt_config({"stt_model": "speechmatics:standard"})

    assert status["configured"] is True
    assert status["provider"] == "speechmatics"
    assert isinstance(media_service.get_stt_engine(), speechmatics_stt_engine.SpeechmaticsBatchEngine)


def test_a_legacy_value_keeps_the_previous_engine_behavior(monkeypatch):
    from backend.services.gemini_media_engine import apply_stt_settings

    monkeypatch.setenv(API_KEY_VAR, API_KEY)

    apply_stt_settings(control_plane.engine_settings({"stt_model": "gemini-2.5-pro"}))

    assert _stt_engine_settings()[0] == "gemini-2.5-pro"


def test_no_credential_stays_fail_closed(monkeypatch):
    from backend.services.gemini_media_engine import apply_stt_settings

    monkeypatch.delenv(API_KEY_VAR, raising=False)

    status = apply_stt_settings(control_plane.engine_settings({"stt_model": "gemini:default"}))

    assert status["configured"] is False
    assert media_service.stt_available() is False


def test_context_isolation_nothing_but_the_three_settings_can_reach_the_engine(monkeypatch):
    from backend.services.gemini_media_engine import apply_stt_settings

    monkeypatch.setenv(API_KEY_VAR, API_KEY)
    poisoned = {
        "stt_model": "gemini:gemini-3.5-transcribe", "stt_language": "fa-IR", "stt_passes": 2,
        "owner_id": 12345, "chat_id": -100999, "message_id": 987,
        "sender": "someone", "username": "@someone", "caption": "secret caption",
        "filename": "voice.ogg", "history": ["earlier turn"], "memory": {"x": 1},
        "reply_text": "replied body",
    }

    # the control plane's conversion carries only the three settings
    assert set(control_plane.engine_settings(poisoned)) == {
        "stt_model", "stt_language", "stt_passes",
    }
    apply_stt_settings(control_plane.engine_settings(poisoned))

    engine = media_service.get_stt_engine()
    assert (engine.stt_model, engine.stt_language, engine.stt_passes) == (
        "gemini-3.5-transcribe", "fa-IR", 2,
    )
    blob = repr(engine) + repr(engine.__slots__)
    for leak in ("someone", "secret caption", "voice.ogg", "earlier turn"):
        assert leak not in blob


def test_the_engine_holds_no_owner_or_telegram_state():
    from backend.services.gemini_media_engine import GeminiMediaEngine

    slots = set(GeminiMediaEngine.__slots__)

    assert slots == {
        "_api_key", "_model", "_key_env_var", "_stt_model", "_stt_language", "_stt_passes",
    }


def test_the_control_plane_imports_nothing_from_telegram_or_the_boundary():
    """The control plane is configuration only: no Telegram, no helper layer, no
    database, and never the media boundary it configures."""
    import ast

    tree = ast.parse(inspect.getsource(control_plane))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")

    forbidden_prefixes = (
        "telethon", "backend.bot", "backend.helper", "backend.db",
        "backend.ai.config_store", "backend.services.media_service",
    )
    for module_name in imported:
        assert not module_name.startswith(forbidden_prefixes), module_name
    assert "backend.services.gemini_media_engine" in imported  # the bounded constants only


# ── 12. Startup application (the supervisor seam) ──────────────────────


@pytest.mark.asyncio
async def test_startup_applies_the_persisted_selection(monkeypatch):
    from backend.runtime.supervisor import RuntimeSupervisor

    monkeypatch.setenv(API_KEY_VAR, API_KEY)
    supervisor = RuntimeSupervisor({
        "API_ID": 1, "API_HASH": "hash", "SESSION_STRING": "session",
        "OWNER_ID": 4242, "TZ": "UTC",
    })

    async def _config(owner_id):
        return {
            "stt_model": "gemini:gemini-3.5-transcribe", "stt_language": "fa-IR", "stt_passes": 3,
        }

    monkeypatch.setattr(config_store, "get_config", _config)

    await supervisor._apply_persisted_stt_settings()

    assert _stt_engine_settings() == ("gemini-3.5-transcribe", "fa-IR", 3)


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


# ── 9. TTS freeze — hidden from the UI, implementation preserved ────────


@pytest.mark.asyncio
async def test_the_frozen_media_hub_offers_no_tts_row_or_status_line(monkeypatch):
    """TTS is frozen: no button, no menu entry and no status line may remain."""
    from backend.bot.handlers import ai_stt_settings as module

    _patch_ai(monkeypatch)
    title, body, buttons = await module._ai_media_panel_handler(None, "")

    assert title == "Media Analysis"
    datas = [d for _, d in _flatten(buttons)]
    assert "panel:ai_media_tts" not in datas
    for label, data in _flatten(buttons):
        assert "tts" not in data.lower(), f"visible TTS callback: {data}"
        assert "text-to-speech" not in label.lower(), f"visible TTS label: {label}"
    assert "Text-to-Speech · " not in body
    assert "speaks text back" not in body


@pytest.mark.asyncio
async def test_the_frozen_media_hub_keeps_the_other_capability_rows(monkeypatch):
    """Freezing TTS must not disturb the unrelated hub entries."""
    from backend.bot.handlers import ai_stt_settings as module

    _patch_ai(monkeypatch)
    _title, body, buttons = await module._ai_media_panel_handler(None, "")

    datas = [d for _, d in _flatten(buttons)]
    assert "panel:ai_media_ocr" in datas
    assert "panel:ai_media_stt" in datas
    assert "panel:ai_cred" in datas
    assert "Text recognition · " in body
    assert "Speech-to-Text · " in body
    assert "API Credentials · " in body


def test_the_tts_module_stays_registered_for_future_reactivation():
    """Frozen ≠ deleted: the panels, builders and actions remain registered."""
    import backend.bot.handlers.ai_tts_settings as module

    panels: list[tuple[str, str]] = []
    actions: list[str] = []
    builders: list[str] = []

    original_panel = module.register_panel
    original_builder = module.register_inline_builder
    original_action = module.register_action
    try:
        module.register_panel = (  # type: ignore[method-assign]
            lambda panel_id, handler, parent="menu", title="": panels.append((panel_id, parent))
        )
        module.register_inline_builder = (  # type: ignore[method-assign]
            lambda panel_id, handler: builders.append(panel_id)
        )
        module.register_action = (  # type: ignore[method-assign]
            lambda action_id, handler: actions.append(action_id)
        )
        module.register(None, 1)
    finally:
        module.register_panel = original_panel  # type: ignore[method-assign]
        module.register_inline_builder = original_builder  # type: ignore[method-assign]
        module.register_action = original_action  # type: ignore[method-assign]

    assert ("ai_media_tts", "ai_media") in panels
    assert ("ai_media_tts_model", "ai_media_tts") in panels
    assert ("ai_media_tts_voice", "ai_media_tts") in panels
    assert sorted(builders) == ["ai_media_tts", "ai_media_tts_model", "ai_media_tts_voice"]
    assert sorted(actions) == ["ai_tts_select", "ai_tts_select_model", "ai_tts_select_voice"]


def test_the_frozen_tts_panel_still_renders_when_addressed_directly(monkeypatch):
    """The frozen panel must never become a broken screen."""
    import backend.bot.handlers.ai_tts_settings as module

    from backend.bot.handlers import ai as ai_module

    async def _config(_owner):
        return {}

    async def _owner():
        return 1

    monkeypatch.setattr(ai_module, "_get_owner_id", _owner)
    monkeypatch.setattr(ai_module, "_get_saved_config", _config)

    body, buttons = module._tts_body_and_buttons(
        module.parse_tts_config({}), config_unreadable=False, session_only=False,
    )
    assert body.startswith("**Text-to-Speech**")
    assert buttons  # the frozen panel still renders its control rows
