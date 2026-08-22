"""
TASK 13 — AI Model Selection → Runtime Pipeline Regression Tests

Covers:
  1. ProviderManager.apply_selection switches the active provider AND
     writes the model to the registered provider instance's OWN config
     (so chat actually uses the selected model).
  2. Unknown providers are rejected, never silently accepted.
  3. engine.apply_runtime_selection delegates to the manager (the single
     authoritative path for web + glass + chat entry points).
  4. The glass ``ai_pick_model`` action selects provider+model together,
     persists both, and applies them to the runtime.
  5. The Test Models glass action renders one-tap usable-model buttons.
  6. Web API model/provider endpoints apply the selection to the runtime
     (not just the config store).
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from backend.ai.providers.base.config import ProviderConfig
from backend.ai.providers.base.contract import BaseProvider, ProviderResponse
from backend.ai.providers.manager.manager import ProviderManager


class StubProvider(BaseProvider):
    """Minimal provider whose config we can observe after apply_selection."""

    def __init__(self, name: str, config: ProviderConfig) -> None:
        super().__init__(config)
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    async def chat(self, messages, **kwargs):
        return ProviderResponse(
            text="ok", provider_name=self._name, success=True,
            metadata={"finish_reason": "stop"},
        )

    def initialize(self) -> None:
        return None

    def shutdown(self) -> None:
        return None

    def count_tokens(self, text: str) -> int:
        return max(1, len(text) // 4)

    def health(self) -> dict:
        return {"healthy": True, "ready": True}


def _manager_with(provider_name: str) -> ProviderManager:
    manager = ProviderManager()
    config = ProviderConfig(
        provider_name=provider_name,
        default_model="factory-default",
        enabled=True,
    )
    manager.register_provider(StubProvider(provider_name, config))
    return manager


# ── ProviderManager.apply_selection ──


def test_apply_selection_switches_provider_and_sets_runtime_model():
    manager = _manager_with("stub")
    # Move active away from dummy first.
    assert manager.apply_selection("stub", "gpt-4o") is True
    assert manager.get_active_name() == "stub"
    # The model must land on the registered provider's OWN config object —
    # the exact object the Dispatcher reads at request time.
    provider = manager.registry.get("stub")
    assert provider.config.default_model == "gpt-4o"
    assert provider.config.model == "gpt-4o"


def test_apply_selection_sets_model_only_when_provided():
    manager = _manager_with("stub")
    assert manager.apply_selection("stub") is True
    assert manager.get_active_name() == "stub"
    # No model argument → config model untouched.
    assert manager.registry.get("stub").config.default_model == "factory-default"


def test_apply_selection_rejects_unknown_provider():
    manager = _manager_with("stub")
    assert manager.apply_selection("ghost", "m") is False
    assert manager.get_active_name() != "ghost"


# ── engine.apply_runtime_selection (authoritative helper) ──


def test_engine_apply_runtime_selection_delegates_to_manager():
    from backend.ai.engine import engine as engine_mod

    fake_manager = MagicMock()
    fake_manager.apply_selection.return_value = True
    fake_engine = MagicMock()
    fake_engine.provider_manager = fake_manager

    with patch.object(engine_mod, "get_engine", return_value=fake_engine):
        assert engine_mod.apply_runtime_selection("openai", "gpt-4o") is True
    fake_manager.apply_selection.assert_called_once_with("openai", "gpt-4o")


def test_engine_apply_runtime_selection_never_raises():
    from backend.ai.engine import engine as engine_mod

    with patch.object(engine_mod, "get_engine", side_effect=RuntimeError("boom")):
        assert engine_mod.apply_runtime_selection("openai", "gpt-4o") is False


# ── Glass: ai_pick_model action ──


@pytest.mark.asyncio
async def test_ai_pick_model_selects_persists_and_applies():
    from backend.bot.handlers import ai as ai_module

    saved: dict = {}

    async def fake_get_config(owner_id):
        return {"provider": "", "model": "", "is_configured": False}

    async def fake_save_config(owner_id, config):
        saved.update(config)

    with patch.object(ai_module, "_get_owner_id", AsyncMock(return_value=42)), \
         patch.object(ai_module, "_get_saved_config", side_effect=fake_get_config), \
         patch.object(ai_module, "_save_config", side_effect=fake_save_config), \
         patch.object(ai_module, "_apply_runtime_selection") as mock_apply, \
         patch("backend.ai.discovery.get_provider_info", return_value={"name": "groq", "default_model": "x"}):

        title, body, buttons = await ai_module._ai_pick_model_action(None, "groq:llama-3.3-70b-versatile", 0)

    assert title == "🧠 AI"
    assert "Model selected" in body
    assert saved["provider"] == "groq"
    assert saved["model"] == "llama-3.3-70b-versatile"
    assert saved["is_configured"] is True
    mock_apply.assert_called_once_with("groq", "llama-3.3-70b-versatile")
    datas = _flatten_button_datas(buttons)
    assert "action:ai_start_chat" in datas


@pytest.mark.asyncio
async def test_ai_pick_model_rejects_malformed_extra():
    from backend.bot.handlers import ai as ai_module

    with patch.object(ai_module, "_get_owner_id", AsyncMock(return_value=42)):
        title, body, _ = await ai_module._ai_pick_model_action(None, "onlyprovider", 0)
    assert "Invalid model selection" in body


@pytest.mark.asyncio
async def test_ai_glass_register_wires_pick_model_action():
    from backend.bot.handlers import ai as ai_module

    with patch.object(ai_module, "register_action") as mock_register_action, \
         patch.object(ai_module, "register_panel"), \
         patch.object(ai_module, "register_inline_builder"), \
         patch.object(ai_module, "register_input"):
        ai_module.register(None, 0)

    registered_ids = [call.args[0] for call in mock_register_action.call_args_list]
    assert "ai_pick_model" in registered_ids
    # Existing actions must remain registered.
    for existing in ("ai_test_models", "ai_start_chat", "ai_select_provider", "ai_select_model"):
        assert existing in registered_ids


# ── Glass: Test Models renders usable-model buttons ──


def _flatten_button_datas(buttons) -> list[str]:
    datas: list[str] = []

    def walk(rows):
        for row in rows or []:
            items = row if isinstance(row, (list, tuple)) else [row]
            for item in items:
                if item is None:
                    continue
                data = getattr(item, "data", None)
                if isinstance(data, bytes):
                    datas.append(data.decode())
                elif isinstance(data, str):
                    datas.append(data)

    walk(buttons)
    return datas


@pytest.mark.asyncio
async def test_ai_test_models_action_shows_usable_buttons_and_keeps_existing():
    from backend.bot.handlers import ai as ai_module

    payload = {
        "results": [
            {"provider": "groq", "display_name": "Groq", "icon": "⚡", "model": "openai/gpt-oss-120b",
             "status": "AVAILABLE", "latency_s": 0.42, "http_status": 200, "error": None},
            {"provider": "groq", "display_name": "Groq", "icon": "⚡", "model": "old-model",
             "status": "INVALID_MODEL", "latency_s": None, "http_status": 404, "error": "not found"},
            {"provider": "openai", "display_name": "OpenAI", "icon": "🧠", "model": "gpt-4o",
             "status": "RATE_LIMITED", "latency_s": None, "http_status": 429, "error": "rate limited"},
        ],
        "summary": {"available": 1, "failed": 2, "rate_limited": 1, "not_configured": 0,
                    "invalid": 1, "insufficient_credits": 0},
    }

    with patch("backend.ai.model_tester.test_all_models", new_callable=AsyncMock) as mock_test, \
         patch.object(ai_module, "_get_owner_id", AsyncMock(return_value=42)):
        mock_test.return_value = payload
        title, body, buttons = await ai_module._ai_test_models_action(None, "", 0)

    assert title == "🧪 Test Models"
    assert "Usable Models" in body
    datas = _flatten_button_datas(buttons)
    # Only the AVAILABLE model becomes a one-tap selection button.
    assert "action:ai_pick_model:groq:openai/gpt-oss-120b" in datas
    assert not any("INVALID_MODEL" in d or "old-model" in d for d in datas)
    # Existing buttons preserved.
    assert "action:ai_test_models" in datas
    assert "panel:ai_model" in datas
    # Obsolete ai_status removed — the entry now points at the Overview.
    assert "panel:ai" in datas
    assert "panel:ai_status" not in datas


# ── Web API applies selection to the runtime ──


def test_api_ai_set_model_endpoint_applies_runtime():
    from backend.web.app import app

    client = TestClient(app)
    with patch("backend.ai.config_store.update_model", new_callable=AsyncMock) as mock_upd, \
         patch("backend.ai.config_store.get_config", new_callable=AsyncMock) as mock_get, \
         patch("backend.web.app._apply_runtime_selection") as mock_apply:
        mock_upd.return_value = True
        mock_get.return_value = {"provider": "openai", "model": "old"}
        resp = client.post("/api/ai/model", json={"model": "gpt-4o"})
        assert resp.status_code == 200
        assert resp.json()["model"] == "gpt-4o"
        mock_apply.assert_called_once_with("openai", "gpt-4o")


def test_api_ai_set_provider_endpoint_applies_runtime():
    from backend.web.app import app

    client = TestClient(app)
    with patch("backend.ai.config_store.update_provider", new_callable=AsyncMock) as mock_upd, \
         patch("backend.web.app._apply_runtime_selection") as mock_apply:
        mock_upd.return_value = True
        resp = client.post("/api/ai/provider", json={"provider": "openai"})
        assert resp.status_code == 200
        assert mock_apply.called
        assert mock_apply.call_args[0][0] == "openai"
        assert len(mock_apply.call_args[0]) == 2  # (provider, model)
