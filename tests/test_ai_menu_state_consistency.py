"""
Focused tests for the live menu-vs-runtime provider/model divergence.

Live bug (user-observed): the Self Bot AI menu displayed
``cohere / command-a-plus-05-2026`` while the AI's own runtime context
and actually-served requests used ``groq / gpt-oss-20b``.

Source-proven root cause (before this fix):

  1. ``ProviderFactory.create_registry`` registers ONLY providers whose
     API key exists in this process's ENV (``factory.py``).
  2. Every provider-selection writer validates by NAME only via
     ``discovery.get_provider_info`` — which returns metadata for every
     supported provider regardless of a key — so an unregistered provider
     (e.g. cohere with no ``AI_COHERE_API_KEY`` on the server) was
     persisted into ``config_store``.
  3. ``ProviderManager.apply_selection`` returns ``False`` WITHOUT
     switching when the provider is not registered, and
     ``engine.apply_persisted_config`` ignored that return value — so the
     phantom persisted pair survived boot AND every request restore.
  4. The AI menu main/health panels displayed the persisted pair first
     (``config.get("provider") or engine_info["provider"]``), while the AI
     runtime context and the actual requests read the ProviderManager.
     Result: menu said cohere, AI said groq, both "right" about their own
     authority, never reconciled.

These tests pin the fix:
  - ``apply_persisted_config`` HEALS a phantom persisted pair to the ACTIVE
    runtime pair (ProviderManager is the single authoritative runtime
    state) — at boot and before every request;
  - the AI menu/health panels display the effective runtime pair
    (ProviderManager first), falling back to persisted config only when
    the engine has no info;
  - ``settings_set`` and the web API refuse to persist a provider that is
    not registered at runtime.

Every test that persists state uses its own owner id (``config_store``
keeps a per-owner in-memory fallback when no Supabase is available).
"""
from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from backend.ai.engine.engine import Engine
from backend.ai.providers.base.capabilities import ProviderCapabilities
from backend.ai.providers.base.config import ProviderConfig
from backend.ai.providers.base.contract import BaseProvider, ProviderResponse
from backend.ai.providers.manager.manager import ProviderManager
from backend.ai.providers.registry.registry import ProviderRegistry
from backend.ai.session.request import AIRequest
from backend.ai.tools.context import ToolContext
from backend.ai.tools.executor import ToolExecutor
from backend.ai.tools.registry import create_default_registry

CHAT = -100903
SESS = "owner-menu-consistency"

# One owner per isolation group (see module docstring).
OWNER_HEAL = 903001          # phantom-config heal at restore
OWNER_MENU = 903002          # main panel + next request served by runtime
OWNER_HEALTH = 903003        # health panel
OWNER_FALLBACK = 903004      # menu falls back to persisted config w/o engine
OWNER_SETTINGS = 903005      # settings_set refuses unregistered provider
OWNER_WEB_SWITCH = 903006    # web provider switch persists provider + default model


class _RecordingProvider(BaseProvider):
    """Scripted chat provider that records the (provider, model) it serves."""

    def __init__(self, name: str, recorded: list[dict[str, str]], *, default_model: str = "model-a") -> None:
        super().__init__(ProviderConfig(provider_name=name, enabled=True, default_model=default_model))
        self._name = name
        self._recorded = recorded

    @property
    def name(self) -> str:
        return self._name

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(supports_tools=True)

    async def chat(self, messages: list[dict[str, Any]], **kwargs: Any) -> ProviderResponse:
        self._recorded.append({"provider": self._name, "model": self.config.default_model})
        return ProviderResponse(text="stub reply", provider_name=self._name, success=True)

    def initialize(self) -> None:
        return None

    def shutdown(self) -> None:
        return None

    def count_tokens(self, text: str) -> int:
        return max(1, len(text) // 4)

    def health(self) -> dict[str, Any]:
        return {"healthy": True, "ready": True}


def make_runtime_engine(recorded: list[dict[str, str]], *,
                        active: str = "groq", active_model: str = "gpt-oss-20b",
                        extra_provider: str | None = None) -> tuple[Engine, ProviderManager]:
    """Build a REAL Engine over scripted providers, wired like production."""
    registry = ProviderRegistry()
    if extra_provider:
        registry.register(_RecordingProvider(extra_provider, recorded, default_model="other-model"))
    registry.register(_RecordingProvider(active, recorded, default_model=active_model))
    manager = ProviderManager(registry)
    manager.apply_selection(active, active_model)
    engine = Engine(providers=manager)
    return engine, manager


async def set_phantom_config(owner_id: int) -> None:
    """Persist a pair whose provider is NOT registered in the test engine.

    Mirrors the live bug: cohere is a discovery-known provider name, but no
    ``AI_COHERE_API_KEY`` exists in the runtime ENV, so it is never
    registered — yet the selection writers accept it by name.
    """
    from backend.ai import config_store
    await config_store.update_provider(owner_id, "cohere", "command-a-plus-05-2026")


# ── 1. apply_persisted_config heals the phantom pair to the runtime ──


@pytest.mark.asyncio
async def test_apply_persisted_config_heals_phantom_provider_to_active_runtime():
    recorded: list[dict[str, str]] = []
    engine, manager = make_runtime_engine(recorded)  # groq registered + active
    assert manager.get_active_name() == "groq"

    await set_phantom_config(OWNER_HEAL)  # cohere is NOT registered here
    assert (await get_config_async(OWNER_HEAL))["provider"] == "cohere"

    import backend.ai.engine.engine as engine_module
    with patch.object(engine_module, "get_engine", return_value=engine):
        ok = await engine_module.apply_persisted_config(OWNER_HEAL)

    assert ok is True
    # The persisted config no longer lies: it matches the ACTIVE runtime pair.
    healed = await get_config_async(OWNER_HEAL)
    assert healed["provider"] == "groq"
    assert healed["model"] == "gpt-oss-20b"
    # The runtime was never corrupted by the phantom.
    assert manager.get_active_name() == "groq"
    assert manager.get_provider_config("groq").default_model == "gpt-oss-20b"

    # Idempotent: a second restore keeps the healed pair (no re-divergence).
    with patch.object(engine_module, "get_engine", return_value=engine):
        await engine_module.apply_persisted_config(OWNER_HEAL)
    assert (await get_config_async(OWNER_HEAL))["provider"] == "groq"
    assert manager.get_active_name() == "groq"


async def get_config_async(owner_id: int) -> dict[str, Any]:
    from backend.ai import config_store
    return await config_store.get_config(owner_id)


# ── 2. The AI menu main panel shows the EFFECTIVE runtime pair ──


@pytest.mark.asyncio
async def test_menu_main_panel_shows_effective_runtime_pair_and_request_matches():
    from backend.bot.handlers import ai as ai_module

    recorded: list[dict[str, str]] = []
    engine, manager = make_runtime_engine(recorded)  # groq / gpt-oss-20b active
    await set_phantom_config(OWNER_MENU)             # persisted cohere phantom

    with patch.object(ai_module, "_get_engine", return_value=engine), \
         patch.object(ai_module, "_get_owner_id", AsyncMock(return_value=OWNER_MENU)):
        title, body, buttons = await ai_module._ai_main_panel_handler(None, "")

    # The menu displays what the AI actually serves — the ProviderManager
    # pair — never the unappliable persisted pair.
    assert title == "AI"
    assert "gpt-oss-20b" in body
    assert "Groq" in body
    assert "command-a-plus-05-2026" not in body
    assert "Cohere" not in body

    # And the NEXT real request through the same engine is served by the
    # exact pair the menu just showed.
    req = AIRequest(session_id=SESS, user_message="hello", owner_id=OWNER_MENU,
                    chat_id=CHAT, message_id=1)
    res = await engine.execute(req)
    assert res.success is True
    assert recorded[-1] == {"provider": "groq", "model": "gpt-oss-20b"}


# ── 3. The health panel shows the EFFECTIVE runtime pair ──


@pytest.mark.asyncio
async def test_menu_health_panel_shows_effective_runtime_pair():
    from backend.bot.handlers import ai as ai_module

    recorded: list[dict[str, str]] = []
    engine, _manager = make_runtime_engine(recorded)
    await set_phantom_config(OWNER_HEALTH)

    with patch.object(ai_module, "_get_engine", return_value=engine), \
         patch.object(ai_module, "_get_owner_id", AsyncMock(return_value=OWNER_HEALTH)):
        title, body, buttons = await ai_module._ai_health_panel_handler(None, "")

    assert title == "AI · Health"
    assert "gpt-oss-20b" in body
    assert "Groq" in body
    assert "command-a-plus-05-2026" not in body
    assert "Cohere" not in body


# ── 4. Without an engine, the menu falls back to the persisted pair ──


@pytest.mark.asyncio
async def test_menu_main_panel_falls_back_to_persisted_pair_without_engine():
    from backend.bot.handlers import ai as ai_module

    await set_phantom_config(OWNER_FALLBACK)

    with patch.object(ai_module, "_get_engine", return_value=None), \
         patch.object(ai_module, "_get_owner_id", AsyncMock(return_value=OWNER_FALLBACK)):
        title, body, buttons = await ai_module._ai_main_panel_handler(None, "")

    # No engine to ask: the persisted pair is the only identity available.
    assert "command-a-plus-05-2026" in body


# ── 5. settings_set refuses to persist an unregistered provider ──


def make_executor(*, owner_id: int) -> tuple[ToolExecutor, ToolContext]:
    ctx = ToolContext(
        telegram=object(),
        owner_id=owner_id,
        tz_str="UTC",
        extra={"chat_id": CHAT, "request_id": "menu-consistency-test"},
    )
    registry = create_default_registry(ctx)
    return ToolExecutor(registry, ctx), ctx


@pytest.mark.asyncio
async def test_settings_set_rejects_unregistered_provider_and_persists_nothing():
    from contextlib import ExitStack

    recorded: list[dict[str, str]] = []
    engine, manager = make_runtime_engine(recorded, extra_provider="gemini")
    executor, ctx = make_executor(owner_id=OWNER_SETTINGS)

    import backend.ai.engine.engine as engine_module
    with ExitStack() as stack:
        stack.enter_context(patch.object(engine_module, "get_engine", return_value=engine))
        stack.enter_context(patch.object(
            engine_module, "apply_runtime_selection",
            side_effect=lambda provider, model: manager.apply_selection(provider, model),
        ))
        result = await executor.execute_confirmed(
            {"name": "settings_set", "arguments": {"key": "provider", "value": "cohere"}},
            owner_id=OWNER_SETTINGS, session_id=SESS, context_override=ctx,
        )

        assert result.success is False
        assert "not available" in result.message
        assert (await get_config_async(OWNER_SETTINGS))["provider"] == ""
        assert manager.get_active_name() == "groq"

        # Positive control: a REGISTERED provider still persists and applies.
        ok_result = await executor.execute_confirmed(
            {"name": "settings_set", "arguments": {"key": "provider", "value": "gemini"}},
            owner_id=OWNER_SETTINGS, session_id=SESS, context_override=ctx,
        )
    assert ok_result.success is True
    assert (await get_config_async(OWNER_SETTINGS))["provider"] == "gemini"
    assert manager.get_active_name() == "gemini"


# ── 6. The web API refuses to persist an unregistered provider ──


def test_web_api_rejects_unregistered_provider_before_persisting():
    from fastapi.testclient import TestClient

    from backend.web.app import app

    client = TestClient(app)
    with patch("backend.ai.config_store.update_provider", new_callable=AsyncMock) as mock_upd:
        resp = client.post("/api/ai/provider", json={"provider": "cohere"})
    assert resp.status_code == 400
    mock_upd.assert_not_called()
    assert "not available" in resp.json()["detail"]


# ── 7. Web provider switch persists the provider's DEFAULT model too ──


@pytest.mark.asyncio
async def test_web_api_provider_switch_persists_default_model_and_survives_restore():
    """A REGISTERED provider switch through the web API must persist the
    new provider's DEFAULT model atomically with the provider.

    Regression: ``POST /api/ai/provider`` called ``update_provider(owner,
    provider)`` with no model, so ``ai_config.model`` kept the OLD
    provider's model while the runtime applied (and the response claimed)
    the new provider's default. The next per-request restore then re-applied
    the stale persisted model onto the runtime — the same
    persisted-vs-runtime divergence class this suite pins. All other
    provider-change writers (``settings_set``, the glass provider action)
    persist provider + default model together; the web writer must match.
    """
    from fastapi.testclient import TestClient

    import backend.ai.engine.engine as engine_module
    from backend.ai import config_store
    from backend.ai.discovery import get_provider_info as real_get_provider_info
    from backend.web import app as web_app

    recorded: list[dict[str, str]] = []
    engine, manager = make_runtime_engine(
        recorded, extra_provider="gemini",  # gemini registered, default "other-model"
    )

    # Persisted state BEFORE the switch: groq / gpt-oss-20b (active runtime).
    await config_store.update_provider(OWNER_WEB_SWITCH, "groq", "gpt-oss-20b")
    assert manager.get_active_name() == "groq"

    def _fake_provider_info(name: str):
        real = real_get_provider_info(name)
        if name == "gemini":
            return {"capability_kind": "chat", "default_model": "gemini-default"}
        return real

    prev_owner = web_app._owner_id
    web_app.set_owner_id(OWNER_WEB_SWITCH)
    client = TestClient(web_app.app)
    try:
        with patch.object(engine_module, "get_engine", return_value=engine), \
             patch("backend.ai.discovery.get_provider_info", side_effect=_fake_provider_info):
            resp = client.post("/api/ai/provider", json={"provider": "gemini"})
    finally:
        web_app.set_owner_id(prev_owner)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["provider"] == "gemini"
    assert body["model"] == "gemini-default"

    # Persisted config and runtime AGREE on gemini / gemini-default.
    cfg = await config_store.get_config(OWNER_WEB_SWITCH)
    assert cfg["provider"] == "gemini"
    assert cfg["model"] == "gemini-default"
    assert manager.get_active_name() == "gemini"
    assert manager.get_provider_config("gemini").default_model == "gemini-default"

    # The next per-request restore must NOT flip the runtime back to the
    # old provider's model (gpt-oss-20b) — persisted == runtime already.
    with patch.object(engine_module, "get_engine", return_value=engine):
        await engine_module.apply_persisted_config(OWNER_WEB_SWITCH)
    cfg = await config_store.get_config(OWNER_WEB_SWITCH)
    assert cfg["provider"] == "gemini"
    assert cfg["model"] == "gemini-default"
    assert manager.get_active_name() == "gemini"
    assert manager.get_provider_config("gemini").default_model == "gemini-default"