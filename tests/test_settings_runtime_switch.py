"""
Focused tests for the confirmed AI model/provider runtime-switching bug.

Reproduction (source-proven before the fix): ``SettingsSetTool`` routed
EVERY key through ``settings_service`` (panel_settings), while the AI
runtime reads its authoritative provider/model from ``config_store``
(ai_config) and re-applies it to the ``ProviderManager`` before every
request (``ai_unified._restore_config`` → ``apply_runtime_selection``).
Keys like ``provider``/``model`` are not panel_settings columns: the DB
write fails, the value lands only in the panel in-memory cache (phantom
success — ``settings_get`` shows it), and the next AI request is still
served by the OLD provider/model.

These tests pin the fix:
  - AI config keys route to ``config_store`` + the runtime apply path;
  - panel keys keep the ``settings_service`` path unchanged;
  - confirmation is still mandatory before any change;
  - invalid values fail closed and never corrupt the active runtime.

The runtime-propagation tests drive the REAL Engine → Dispatcher →
ProviderManager path with scripted providers so they prove the NEXT AI
REQUEST is served by the requested model/provider — not merely that
``settings_get`` returns the new value.

Every test that asserts "unchanged" uses its own owner id so results do
not depend on execution order (``config_store`` keeps a per-owner
in-memory fallback when no Supabase is available).
"""
from __future__ import annotations

from typing import Any

from unittest.mock import patch

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

CHAT = -100901
SESS = "owner-settings-switch"

# One owner per isolation group (see module docstring).
OWNER_FLOW = 901001          # confirmation gate + persistence + get
OWNER_RUNTIME = 901002       # engine-level model/provider switch tests
OWNER_PANEL = 901003         # panel-key routing
OWNER_INVALID_MODEL = 901004
OWNER_UNKNOWN_PROVIDER = 901005
OWNER_NON_CHAT = 901006
OWNER_NO_PROVIDER = 901007
OWNER_INVALID_TEMP = 901008
OWNER_VALID_TEMP = 901009
OWNER_INVALID_TOKENS = 901010
OWNER_TRIGGERS = 901011


# ────────────────────────────── helpers ──────────────────────────────


def make_executor(*, owner_id: int) -> tuple[ToolExecutor, ToolContext]:
    ctx = ToolContext(
        telegram=object(),
        owner_id=owner_id,
        tz_str="UTC",
        extra={"chat_id": CHAT, "request_id": "settings-switch-test"},
    )
    registry = create_default_registry(ctx)
    return ToolExecutor(registry, ctx), ctx


async def confirmed_set(executor: ToolExecutor, ctx: ToolContext, key: str, value: Any) -> Any:
    """Run settings_set through the real confirmed execution path."""
    return await executor.execute_confirmed(
        {"name": "settings_set", "arguments": {"key": key, "value": value}},
        owner_id=ctx.owner_id, session_id=SESS, context_override=ctx,
    )


async def get_ai_config(owner_id: int) -> dict[str, Any]:
    from backend.ai import config_store
    return await config_store.get_config(owner_id)


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


def make_runtime_engine(recorded: list[dict[str, str]], *, extra_provider: str | None = None) -> tuple[Engine, ProviderManager]:
    """Build a REAL Engine over scripted providers, wired like production."""
    registry = ProviderRegistry()
    registry.register(_RecordingProvider("prov-a", recorded, default_model="model-a"))
    if extra_provider:
        registry.register(_RecordingProvider(extra_provider, recorded, default_model="factory-default"))
    registry.register(_RecordingProvider("prov-b", recorded, default_model="model-b"))
    manager = ProviderManager(registry)
    engine = Engine(providers=manager)
    ctx = ToolContext(telegram=object(), owner_id=OWNER_RUNTIME, tz_str="UTC", extra={"chat_id": CHAT})
    engine.attach_tools(create_default_registry(ctx), ctx, owner_id=OWNER_RUNTIME, tz_str="UTC")
    return engine, manager


def _apply_patch(engine: Engine, manager: ProviderManager):
    """Route the tool's runtime apply into the TEST engine's manager."""
    import backend.ai.engine.engine as engine_module
    return patch.object(
        engine_module, "apply_runtime_selection",
        side_effect=lambda provider, model: manager.apply_selection(provider, model),
    )


async def _next_request(engine: Engine) -> Any:
    req = AIRequest(
        session_id=SESS, user_message="hello", owner_id=OWNER_RUNTIME,
        chat_id=CHAT, message_id=1,
    )
    return await engine.execute(req)


# ── confirmation boundary (AI keys are still ADMIN_ONLY) ──


@pytest.mark.asyncio
async def test_ai_key_requires_confirmation_before_any_change():
    executor, ctx = make_executor(owner_id=OWNER_FLOW)
    results = await executor.execute_calls(
        [{"name": "settings_set", "arguments": {"key": "model", "value": "m2"}}],
        owner_id=OWNER_FLOW, session_id=SESS,
    )
    assert results[0].needs_confirmation is True
    assert results[0].success is False
    assert (await get_ai_config(OWNER_FLOW))["model"] == ""


# ── persistence: confirmed settings_set writes the REAL store ──


@pytest.mark.asyncio
async def test_model_switch_persists_to_config_store():
    from backend.ai import config_store

    executor, ctx = make_executor(owner_id=OWNER_FLOW)
    # An owner who already configured a provider via the setup wizard.
    await config_store.update_provider(OWNER_FLOW, "gemini", "gemini-2.0-flash")
    with patch("backend.ai.engine.engine.apply_runtime_selection", return_value=True):
        result = await confirmed_set(executor, ctx, "model", "gemini-2.5-flash")
    assert result.success is True
    assert (await get_ai_config(OWNER_FLOW))["model"] == "gemini-2.5-flash"


@pytest.mark.asyncio
async def test_settings_get_reads_config_store_for_ai_keys():
    from backend.ai import config_store

    executor, ctx = make_executor(owner_id=OWNER_FLOW)
    await config_store.update_provider(OWNER_FLOW, "gemini", "gemini-2.0-flash")
    with patch("backend.ai.engine.engine.apply_runtime_selection", return_value=True):
        await confirmed_set(executor, ctx, "model", "gemini-2.5-flash")

    registry = create_default_registry(ctx)
    get_tool = registry.get("settings_get")
    assert get_tool is not None

    res = await get_tool.execute(ctx, {"key": "model"})
    assert res.success is True
    assert "gemini-2.5-flash" in res.message

    res_panel = await get_tool.execute(ctx, {"key": "language"})
    assert res_panel.success is True
    assert "en" in res_panel.message


@pytest.mark.asyncio
async def test_panel_keys_still_route_to_settings_service():
    from backend.services import settings_service

    executor, ctx = make_executor(owner_id=OWNER_PANEL)
    with patch.object(settings_service, "set_setting", return_value=True) as setter:
        result = await confirmed_set(executor, ctx, "language", "fa")
    setter.assert_called_once_with("language", "fa")
    assert result.success is True
    assert (await get_ai_config(OWNER_PANEL))["model"] == ""


# ── runtime propagation: the NEXT AI request uses the new model ──


@pytest.mark.asyncio
async def test_model_switch_applies_to_runtime_and_next_request():
    recorded: list[dict[str, str]] = []
    engine, manager = make_runtime_engine(recorded)
    manager.apply_selection("prov-a", "model-a")
    assert manager.get_active_name() == "prov-a"

    from backend.ai import config_store
    await config_store.update_provider(OWNER_RUNTIME, "prov-a", "model-a")

    executor, ctx = make_executor(owner_id=OWNER_RUNTIME)
    with _apply_patch(engine, manager):
        result = await confirmed_set(executor, ctx, "model", "model-b")

    assert result.success is True
    # Persisted in the authoritative store.
    assert (await get_ai_config(OWNER_RUNTIME))["model"] == "model-b"
    # Runtime provider instance updated (the object the next request uses).
    assert manager.get_provider_config("prov-a").default_model == "model-b"
    # Provider unchanged.
    assert manager.get_active_name() == "prov-a"

    # A NEW AI request through the real Engine is served by the new model.
    res = await _next_request(engine)
    assert res.success is True
    assert recorded, "provider should have served the request"
    assert recorded[-1] == {"provider": "prov-a", "model": "model-b"}


@pytest.mark.asyncio
async def test_provider_switch_applies_to_runtime_and_next_request():
    recorded: list[dict[str, str]] = []
    engine, manager = make_runtime_engine(recorded, extra_provider="gemini")
    manager.apply_selection("prov-a", "model-a")

    executor, ctx = make_executor(owner_id=OWNER_RUNTIME)
    with _apply_patch(engine, manager):
        result = await confirmed_set(executor, ctx, "provider", "gemini")

    assert result.success is True
    config = await get_ai_config(OWNER_RUNTIME)
    assert config["provider"] == "gemini"
    assert manager.get_active_name() == "gemini"
    assert manager.get_provider_config("gemini").default_model == config["model"]

    res = await _next_request(engine)
    assert res.success is True
    assert recorded[-1] == {"provider": "gemini", "model": config["model"]}


# ── invalid values fail closed, never corrupting the active runtime ──


@pytest.mark.asyncio
async def test_invalid_model_fails_closed_and_keeps_runtime_unchanged():
    recorded: list[dict[str, str]] = []
    engine, manager = make_runtime_engine(recorded)
    manager.apply_selection("prov-a", "model-a")

    executor, ctx = make_executor(owner_id=OWNER_INVALID_MODEL)
    with _apply_patch(engine, manager):
        result = await confirmed_set(executor, ctx, "model", "")

    assert result.success is False
    assert (await get_ai_config(OWNER_INVALID_MODEL))["model"] == ""
    assert manager.get_provider_config("prov-a").default_model == "model-a"
    assert manager.get_active_name() == "prov-a"


@pytest.mark.asyncio
async def test_unknown_provider_fails_closed():
    executor, ctx = make_executor(owner_id=OWNER_UNKNOWN_PROVIDER)
    result = await confirmed_set(executor, ctx, "provider", "not-a-provider")
    assert result.success is False
    assert (await get_ai_config(OWNER_UNKNOWN_PROVIDER))["provider"] == ""


@pytest.mark.asyncio
async def test_non_chat_capability_provider_rejected():
    executor, ctx = make_executor(owner_id=OWNER_NON_CHAT)
    result = await confirmed_set(executor, ctx, "provider", "you")
    assert result.success is False
    assert (await get_ai_config(OWNER_NON_CHAT))["provider"] == ""


@pytest.mark.asyncio
async def test_model_without_provider_fails_closed():
    executor, ctx = make_executor(owner_id=OWNER_NO_PROVIDER)
    result = await confirmed_set(executor, ctx, "model", "anything")
    assert result.success is False
    assert (await get_ai_config(OWNER_NO_PROVIDER))["model"] == ""


@pytest.mark.asyncio
async def test_invalid_temperature_fails_closed():
    executor, ctx = make_executor(owner_id=OWNER_INVALID_TEMP)
    for bad in ("99", "abc", "-1"):
        result = await confirmed_set(executor, ctx, "temperature", bad)
        assert result.success is False, bad
    assert (await get_ai_config(OWNER_INVALID_TEMP))["temperature"] == 1.0


@pytest.mark.asyncio
async def test_valid_temperature_persists_as_number():
    executor, ctx = make_executor(owner_id=OWNER_VALID_TEMP)
    result = await confirmed_set(executor, ctx, "temperature", "0.7")
    assert result.success is True
    assert (await get_ai_config(OWNER_VALID_TEMP))["temperature"] == 0.7


@pytest.mark.asyncio
async def test_invalid_max_tokens_fails_closed():
    executor, ctx = make_executor(owner_id=OWNER_INVALID_TOKENS)
    for bad in ("0", "-5", "abc"):
        result = await confirmed_set(executor, ctx, "max_tokens", bad)
        assert result.success is False, bad
    assert (await get_ai_config(OWNER_INVALID_TOKENS))["max_tokens"] == 4096


@pytest.mark.asyncio
async def test_trigger_words_validated():
    executor, ctx = make_executor(owner_id=OWNER_TRIGGERS)
    result = await confirmed_set(executor, ctx, "trigger_en", "two words")
    assert result.success is False
    result = await confirmed_set(executor, ctx, "trigger_en", "nova")
    assert result.success is True
    result = await confirmed_set(executor, ctx, "trigger_fa", "NOVA")
    assert result.success is False


def test_ai_config_keys_are_bounded_and_disjoint_from_panel_keys():
    """The routing boundary is explicit and bounded."""
    from backend.ai.tools.settings import _AI_CONFIG_KEYS
    from backend.services.settings_service import _DEFAULTS

    assert "provider" in _AI_CONFIG_KEYS
    assert "model" in _AI_CONFIG_KEYS
    # No overlap: every key belongs to exactly one store.
    assert _AI_CONFIG_KEYS.isdisjoint(_DEFAULTS.keys())