"""
Focused tests for end-to-end AI provider/model state consistency.

Live bug (user-observed): at the same moment the Self Bot AI menu showed
one model/provider, ``settings_get`` returned another, and the AI's own
runtime context claimed ``provider=dummy`` even while a real provider was
serving requests.

Source-proven causes (before this fix):

  1. ``RuntimeSession.active_provider`` defaults to ``"dummy"`` and
     ``ConversationManager.set_provider`` had ZERO production callers, so
     ``Dispatcher._build_context`` rendered a never-updated session value
     into the prompt's [Runtime Context] block.
  2. The persisted ``ai_config`` was applied to the runtime only on the
     first chat request (``ai_unified._restore_config``). At boot the
     engine's active provider stayed at the ENV default (``AI_PROVIDER``
     or ``dummy``), so engine-read surfaces (AI menu state, health)
     disagreed with config-read surfaces (menu identity, settings_get).
  3. ``temperature`` / ``max_tokens`` persisted in ``config_store`` never
     reached the active provider's runtime config — the object the
     provider reads at request time.

These tests pin the fix: ONE shared restore
(``engine.apply_persisted_config``) applied at boot AND before every chat
request, and a runtime context built from the ProviderManager — the
single authoritative runtime state.

Every test that persists state uses its own owner id (``config_store``
keeps a per-owner in-memory fallback when no Supabase is available).
"""
from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from backend.ai.engine.engine import Engine
from backend.ai.providers.base.capabilities import ProviderCapabilities
from backend.ai.providers.base.config import ProviderConfig
from backend.ai.providers.base.contract import BaseProvider, ProviderResponse
from backend.ai.providers.manager.manager import ProviderManager
from backend.ai.providers.registry.registry import ProviderRegistry
from backend.ai.session.request import AIRequest

CHAT = -100902
SESS = "owner-state-consistency"

# One owner per isolation group (see module docstring).
OWNER_RESTORE = 902001       # apply_persisted_config full restore
OWNER_SESSION = 902002       # session sync
OWNER_TEMP = 902003          # temperature/max_tokens sync
OWNER_CONTEXT = 902004       # dispatcher runtime context (full dispatch)


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


def make_runtime_engine(recorded: list[dict[str, str]]) -> tuple[Engine, ProviderManager]:
    """Build a REAL Engine over scripted providers, wired like production."""
    registry = ProviderRegistry()
    registry.register(_RecordingProvider("prov-a", recorded, default_model="model-a"))
    registry.register(_RecordingProvider("prov-b", recorded, default_model="model-b"))
    manager = ProviderManager(registry)
    engine = Engine(providers=manager)
    return engine, manager


async def set_ai_config(owner_id: int, *, provider: str = "", model: str = "",
                        temperature: float | None = None, max_tokens: int | None = None) -> None:
    from backend.ai import config_store
    if provider:
        await config_store.update_provider(owner_id, provider, model or None)
    elif model:
        await config_store.update_model(owner_id, model)
    if temperature is not None:
        await config_store.update_setting(owner_id, "temperature", temperature)
    if max_tokens is not None:
        await config_store.update_setting(owner_id, "max_tokens", max_tokens)


# ── apply_persisted_config: the single shared restore ──


@pytest.mark.asyncio
async def test_apply_persisted_config_switches_provider_and_model():
    recorded: list[dict[str, str]] = []
    engine, manager = make_runtime_engine(recorded)
    manager.apply_selection("prov-a", "model-a")
    assert manager.get_active_name() == "prov-a"

    await set_ai_config(OWNER_RESTORE, provider="prov-b", model="persisted-model")

    import backend.ai.engine.engine as engine_module
    with patch.object(engine_module, "get_engine", return_value=engine):
        ok = await engine_module.apply_persisted_config(OWNER_RESTORE)

    assert ok is True
    # The runtime provider instance — the object the next request uses —
    # is switched to the persisted pair.
    assert manager.get_active_name() == "prov-b"
    assert manager.get_provider_config("prov-b").default_model == "persisted-model"

    # A new AI request through the real Engine is served by the restored pair.
    req = AIRequest(session_id=SESS, user_message="hello", owner_id=OWNER_RESTORE,
                    chat_id=CHAT, message_id=1)
    res = await engine.execute(req)
    assert res.success is True
    assert recorded[-1] == {"provider": "prov-b", "model": "persisted-model"}


@pytest.mark.asyncio
async def test_apply_persisted_config_syncs_conversation_session():
    recorded: list[dict[str, str]] = []
    engine, _manager = make_runtime_engine(recorded)

    await set_ai_config(OWNER_SESSION, provider="prov-b", model="session-model")

    import backend.ai.engine.engine as engine_module
    with patch.object(engine_module, "get_engine", return_value=engine):
        await engine_module.apply_persisted_config(OWNER_SESSION)

    # The session no longer reports the never-updated "dummy" default.
    session = engine.conversation_manager.get_session(OWNER_SESSION)
    assert session is not None
    assert session.active_provider == "prov-b"
    assert session.active_model == "session-model"
    assert session.system_prompt == "You are LifeOS Assistant."


@pytest.mark.asyncio
async def test_apply_persisted_config_syncs_temperature_and_max_tokens():
    recorded: list[dict[str, str]] = []
    engine, manager = make_runtime_engine(recorded)
    manager.apply_selection("prov-a", "model-a")

    await set_ai_config(OWNER_TEMP, provider="prov-b", model="model-b",
                        temperature=0.2, max_tokens=1234)

    import backend.ai.engine.engine as engine_module
    with patch.object(engine_module, "get_engine", return_value=engine):
        await engine_module.apply_persisted_config(OWNER_TEMP)

    pconfig = manager.get_provider_config("prov-b")
    assert pconfig.temperature == 0.2
    assert pconfig.max_tokens == 1234


# ── dispatcher runtime context: the provider/model the AI actually sees ──


class _CapturePromptBuilder:
    """Prompt builder that captures the ConversationContext it receives."""

    def __init__(self) -> None:
        self.context = None

    def build(self, context: Any) -> Any:
        self.context = context
        pp = MagicMock()
        pp.system_prompt = "sys"
        pp.runtime_context = ""
        pp.conversation_context = ""
        pp.tool_context = ""
        pp.user_input = "hello"
        pp.estimated_tokens.estimated_input_tokens = 10
        pp.estimated_tokens.prompt_size_chars = 100
        return pp


@pytest.mark.asyncio
async def test_runtime_context_reports_real_active_provider_and_model():
    recorded: list[dict[str, str]] = []
    registry = ProviderRegistry()
    registry.register(_RecordingProvider("prov-b", recorded, default_model="active-model"))
    manager = ProviderManager(registry)
    capture = _CapturePromptBuilder()
    engine = Engine(providers=manager, prompt_builder=capture)
    manager.apply_selection("prov-b", "active-model")

    req = AIRequest(session_id=SESS, user_message="hello", owner_id=OWNER_CONTEXT,
                    chat_id=CHAT, message_id=1)
    res = await engine.execute(req)
    assert res.success is True
    assert capture.context is not None
    # The AI's runtime context is built from the ProviderManager — the
    # single authoritative runtime state — never the session "dummy" default.
    assert capture.context.runtime.active_provider == "prov-b"
    assert capture.context.runtime.active_model == "active-model"


# ── prompt rendering: the model is visible to the model ──


def test_runtime_context_renders_provider_and_model_line():
    from backend.ai.conversation.context_builder import ContextBuilder, RuntimeContext
    from backend.ai.prompt.builder import PromptBuilder
    from backend.ai.conversation.state import ConversationState

    class _FakeSession:
        session_id = "s"
        owner_id = 1
        chat_id = 1
        state = ConversationState.IDLE
        current_panel = ""
        current_category = ""
        current_flow = ""
        pending_action = ""
        language = "English"
        timezone = "UTC"
        current_tool = ""
        last_tool = ""

    ctx = ContextBuilder().build(
        session=_FakeSession(), user_text="hi", message_id=1,
        runtime=RuntimeContext(ai_enabled=True, active_provider="groq", active_model="llama-x"),
    )
    package = PromptBuilder().build(ctx)
    assert "provider=groq" in package.runtime_context
    assert "model=llama-x" in package.runtime_context
    assert "AI: enabled" in package.runtime_context