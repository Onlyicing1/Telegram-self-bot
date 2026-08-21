"""
TASK 36 — AI Settings UX hierarchy regression tests.

  1. The personal Settings surface carries informational STATE in its text
     and ACTIONS in its buttons — never technical knobs, never value-in-
     label duplication.
  2. Technical controls live only behind Advanced (same input keys — no
     duplicate registrations).
  3. Finishing an input restores the panel in ONE edit (notice on top,
     buttons intact) instead of stranding the user on a bare "✅".
  4. The personality-prompt 'reset' promise actually resets.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.helper import InlinePanelBuilder


def _flatten(buttons) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for row in buttons:
        cells = row if isinstance(row, list) else [row]
        for btn in cells:
            text = getattr(btn, "text", None) or ""
            data = getattr(btn, "data", None) or ""
            if isinstance(data, bytes):
                data = data.decode("utf-8", errors="replace")
            out.append((str(text), str(data)))
    return out


_CONFIG = {
    "provider": "gemini", "model": "gemini-2.5-flash",
    "temperature": 0.7, "max_tokens": 4096, "history_budget": 4000,
    "system_prompt": "", "is_configured": True,
    "trigger_en": "Nova", "trigger_fa": "",
}


def _settings_patches(config=None):
    from backend.bot.handlers import ai as ai_module
    return (
        patch.object(ai_module, "_get_saved_config", AsyncMock(return_value=config or dict(_CONFIG))),
        patch.object(ai_module, "_get_owner_id", AsyncMock(return_value=1)),
    )


# ── 1. Personal surface: state in text, actions in buttons ──


@pytest.mark.asyncio
async def test_settings_shows_state_in_text_not_in_buttons():
    from backend.bot.handlers import ai as ai_module

    p1, p2 = _settings_patches()
    with p1, p2:
        title, body, buttons = await ai_module._ai_settings_panel_handler(None, "")

    assert title == "Settings"
    # Wake-word values are informational state — they live in the text…
    assert 'Say "Nova" to talk to the assistant.' in body
    assert "Reply stats · Off" in body
    labels = [t for t, _ in _flatten(buttons)]
    # …never duplicated into administrative button labels.
    assert all("Nova" not in t for t in labels)


@pytest.mark.asyncio
async def test_settings_keeps_technical_knobs_off_the_personal_surface():
    from backend.bot.handlers import ai as ai_module

    p1, p2 = _settings_patches()
    with p1, p2:
        title, body, buttons = await ai_module._ai_settings_panel_handler(None, "")

    combined = body + " ".join(t for t, _ in _flatten(buttons))
    for technical in ("Temperature", "Creativity", "Max Tokens", "Response length",
                      "Context Budget", "System Prompt", "Personality"):
        assert technical not in combined
    datas = [d for _, d in _flatten(buttons)]
    assert "panel:ai_settings_adv" in datas
    assert "action:ai_toggle_telemetry" in datas
    # Only the two wake-word inputs sit on the personal surface.
    assert datas.count("input:ai_settings:trigger_en") == 1
    assert datas.count("input:ai_settings:trigger_fa") == 1
    assert "input:ai_settings:temperature" not in datas


@pytest.mark.asyncio
async def test_settings_warns_when_no_wake_word():
    from backend.bot.handlers import ai as ai_module

    config = dict(_CONFIG, trigger_en="", trigger_fa="")
    p1, p2 = _settings_patches(config)
    with p1, p2:
        title, body, buttons = await ai_module._ai_settings_panel_handler(None, "")

    assert "No wake word yet" in body


# ── 2. Advanced holds the technical knobs ──


@pytest.mark.asyncio
async def test_advanced_panel_holds_technical_controls_in_plain_terms():
    from backend.bot.handlers import ai as ai_module

    config = dict(_CONFIG, system_prompt="be terse")
    p1, p2 = _settings_patches(config)
    with p1, p2:
        title, body, buttons = await ai_module._ai_settings_adv_panel_handler(None, "")

    assert title == "Advanced"
    assert "Creativity 0.7" in body
    assert "Response length up to 4,096 tokens" in body
    assert "Remembers about 4,000 tokens" in body
    assert "Personality prompt · Custom" in body

    datas = [d for _, d in _flatten(buttons)]
    for key in ("temperature", "max_tokens", "history_budget", "system_prompt"):
        assert f"input:ai_settings:{key}" in datas


# ── 3. Registration: new panel, no duplicate inputs ──


def test_registration_adds_advanced_panel_and_keeps_inputs_unique():
    from backend.bot.handlers import ai as ai_module

    panels, inputs = [], []
    with patch.object(ai_module, "register_panel", side_effect=lambda *a, **k: panels.append(a[0])), \
         patch.object(ai_module, "register_action"), \
         patch.object(ai_module, "register_inline_builder"), \
         patch.object(ai_module, "register_input",
                      side_effect=lambda scope, key, cfg: inputs.append((scope, key))):
        ai_module.register(None, 0)

    assert "ai_settings_adv" in panels
    keys = [k for scope, k in inputs if scope == "ai_settings"]
    assert len(keys) == len(set(keys)) == 6


# ── 4. Input completion restores the panel in ONE edit ──


@pytest.mark.asyncio
async def test_finish_input_restores_panel_with_notice_and_buttons():
    from backend.bot.handlers import ai as ai_module
    import backend.helper.client as helper_client
    import backend.helper.inline_engine as inline_engine

    helper = MagicMock()
    helper.edit_message = AsyncMock()
    with patch.object(helper_client, "get_client", return_value=helper), \
         patch.object(inline_engine, "_self_client", None), \
         patch.object(ai_module, "_get_saved_config", AsyncMock(return_value=dict(_CONFIG))), \
         patch.object(ai_module, "_get_owner_id", AsyncMock(return_value=1)):
        await ai_module._finish_input(
            "✅ Creativity set to 0.7", ai_module._ai_settings_adv_panel_handler,
            100, 11, 100, 55,
        )

    assert helper.edit_message.await_count == 1
    args, kwargs = helper.edit_message.await_args
    text = args[2]
    assert text.startswith("✅ Creativity set to 0.7")
    assert "**Advanced**" in text
    assert kwargs.get("buttons")  # navigation restored, user is not stranded


@pytest.mark.asyncio
async def test_finish_input_falls_back_to_notice_when_panel_render_fails():
    from backend.bot.handlers import ai as ai_module
    import backend.helper.client as helper_client
    import backend.helper.inline_engine as inline_engine

    helper = MagicMock()
    helper.edit_message = AsyncMock()
    with patch.object(helper_client, "get_client", return_value=helper), \
         patch.object(inline_engine, "_self_client", None):
        await ai_module._finish_input(
            "✅ Saved", AsyncMock(side_effect=RuntimeError("boom")),
            100, 11, 100, 55,
        )

    assert helper.edit_message.await_count == 1
    args, _kwargs = helper.edit_message.await_args
    assert args[2] == "✅ Saved"


@pytest.mark.asyncio
async def test_system_prompt_reset_clears_to_default():
    from backend.bot.handlers import ai as ai_module
    import backend.helper.client as helper_client
    import backend.helper.inline_engine as inline_engine

    saved = {}
    helper = MagicMock()
    helper.edit_message = AsyncMock()
    async def _update(owner_id, key, value):
        saved[(owner_id, key)] = value

    with patch("backend.ai.config_store.update_setting", _update), \
         patch.object(helper_client, "get_client", return_value=MagicMock(edit_message=AsyncMock())), \
         patch.object(inline_engine, "_self_client", None), \
         patch.object(ai_module, "_get_owner_id", AsyncMock(return_value=7)):
        await ai_module._ai_system_prompt_input("reset", 100, 11, 0, 0)

    assert saved[(7, "system_prompt")] == ""


@pytest.mark.asyncio
async def test_wake_word_input_rejects_multi_word_without_saving():
    from backend.bot.handlers import ai as ai_module
    import backend.helper.client as helper_client
    import backend.helper.inline_engine as inline_engine

    saved = {}

    async def _update(owner_id, key, value):
        saved[key] = value

    helper = MagicMock()
    helper.edit_message = AsyncMock()
    with patch("backend.ai.config_store.update_setting", _update), \
         patch.object(helper_client, "get_client", return_value=helper), \
         patch.object(inline_engine, "_self_client", None), \
         patch.object(ai_module, "_get_saved_config", AsyncMock(return_value=dict(_CONFIG))), \
         patch.object(ai_module, "_get_owner_id", AsyncMock(return_value=1)):
        await ai_module._ai_trigger_en_input("two words", 100, 11, 100, 55)

    assert "trigger_en" not in saved  # nothing saved on invalid input
    args, _kwargs = helper.edit_message.await_args
    assert "single word" in args[2]
