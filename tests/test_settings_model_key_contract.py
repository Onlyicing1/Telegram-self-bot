"""
Regression tests — the AI settings tool key contract (live failure).

Live behavior that exposed the bug: the owner asked the AI to change the
model; the AI selected ``settings_set`` with ``key="ai_model"``,
``value="gpt-oss-1200"``, and the backend rejected it with
"Unknown setting key 'ai_model'". The canonical AI runtime key is
``model``.

Root cause (source-proven):
  1. ``backend/ai/prompt/builder.py`` rendered the replied-to AI message
     metadata with the label ``AI Model: <model>`` — the only place the
     token sequence "AI Model" appears in the model-facing prompt (the
     [Runtime Context] block correctly uses ``model=...``). The model
     mapped that label to a non-existent key ``ai_model``.
  2. ``settings_get``/``settings_set`` tool schemas enumerated NO valid
     keys ("The setting key to read/write."), so the model had to guess.
     ``_AI_CONFIG_KEYS`` and ``settings_service.known_keys()`` were never
     exposed to the model.
  3. No alias layer exists anywhere: ``ai_model`` correctly fails the AI
     key set and the panel allowlist. The backend behaved right; the
     model-facing contract was under-specified and misleading.

These tests pin the repaired contract:
  - the tool schema exposes the canonical key list (model, provider, ...)
    and never offers ``ai_model`` as a valid key;
  - ``ai_model`` is rejected as unknown, both directly and through the
    confirmed path, and writes nothing anywhere;
  - a confirmed ``settings_set(model=...)`` persists to ``config_store``,
    applies to the runtime ProviderManager, and the NEXT AI request is
    served by the new model;
  - the prompt renders ``Model:``/``Provider:`` labels (the canonical key
    names), never ``AI Model:``/``AI Provider:``.
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
SESS = "owner-model-key-contract"

# One owner per isolation group (config_store keeps per-owner fallbacks).
# 904xxx is unused by every other suite (901–903 are taken).
OWNER_SCHEMA = 904001
OWNER_REJECT = 904002
OWNER_GATE = 904003
OWNER_RUNTIME = 904004
OWNER_PROMPT = 904005

AI_MODEL_KEY = "ai_model"
CANONICAL_MODEL = "model"
NEW_MODEL = "gpt-oss-1200"


# ────────────────────────────── helpers ──────────────────────────────


def make_executor(*, owner_id: int) -> tuple[ToolExecutor, ToolContext]:
    ctx = ToolContext(
        telegram=object(),
        owner_id=owner_id,
        tz_str="UTC",
        extra={"chat_id": CHAT, "request_id": "model-key-contract-test"},
    )
    registry = create_default_registry(ctx)
    return ToolExecutor(registry, ctx), ctx


async def confirmed_set(executor: ToolExecutor, ctx: ToolContext, key: str, value: Any) -> Any:
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


def make_runtime_engine(recorded: list[dict[str, str]]) -> tuple[Engine, ProviderManager]:
    registry = ProviderRegistry()
    registry.register(_RecordingProvider("prov-a", recorded, default_model="model-a"))
    registry.register(_RecordingProvider("prov-b", recorded, default_model="model-b"))
    manager = ProviderManager(registry)
    engine = Engine(providers=manager)
    ctx = ToolContext(telegram=object(), owner_id=OWNER_RUNTIME, tz_str="UTC", extra={"chat_id": CHAT})
    engine.attach_tools(create_default_registry(ctx), ctx, owner_id=OWNER_RUNTIME, tz_str="UTC")
    return engine, manager


def _apply_patch(engine: Engine, manager: ProviderManager):
    """Route the tool's runtime apply + registration check into the TEST engine."""
    from contextlib import ExitStack

    import backend.ai.engine.engine as engine_module
    stack = ExitStack()
    stack.enter_context(patch.object(
        engine_module, "apply_runtime_selection",
        side_effect=lambda provider, model: manager.apply_selection(provider, model),
    ))
    stack.enter_context(patch.object(engine_module, "get_engine", return_value=engine))
    return stack


async def _next_request(engine: Engine) -> Any:
    req = AIRequest(
        session_id=SESS, user_message="hello", owner_id=OWNER_RUNTIME,
        chat_id=CHAT, message_id=1,
    )
    return await engine.execute(req)


# ── 1. schema contract: canonical keys exposed, ai_model never valid ──


def test_settings_tool_schema_exposes_canonical_keys_and_never_ai_model():
    """The model-facing key vocabulary is the bounded canonical list."""
    from backend.ai.tools.settings import _AI_KEY_LIST, _PANEL_KEY_LIST, _setting_key_contract

    contract = _setting_key_contract()
    # The canonical AI runtime keys are presented to the model.
    assert CANONICAL_MODEL in _AI_KEY_LIST
    assert "provider" in _AI_KEY_LIST
    # The enumerated valid-key lists contain no ai_model-style aliases.
    assert AI_MODEL_KEY not in _AI_KEY_LIST
    assert AI_MODEL_KEY not in _PANEL_KEY_LIST
    # The contract names the canonical key and disambiguates explicitly.
    assert "key 'model'" in contract
    assert AI_MODEL_KEY in contract  # mentioned only as a forbidden alias
    assert contract.index("model") < contract.index(AI_MODEL_KEY)


def test_settings_set_and_get_descriptions_carry_the_contract():
    executor, ctx = make_executor(owner_id=OWNER_SCHEMA)
    registry = create_default_registry(ctx)

    set_tool = registry.get("settings_set")
    get_tool = registry.get("settings_get")
    assert set_tool is not None and get_tool is not None

    for tool in (set_tool, get_tool):
        assert CANONICAL_MODEL in tool.description
        assert "provider" in tool.description
        key_desc = tool.parameters["key"]["description"]
        assert CANONICAL_MODEL in key_desc
        assert "Valid keys" in key_desc


# ── 2. ai_model is rejected as an unknown key, writes nothing ──


@pytest.mark.asyncio
async def test_ai_model_key_rejected_as_unknown():
    from backend.services import settings_service

    executor, ctx = make_executor(owner_id=OWNER_REJECT)
    tool = executor._registry.get("settings_set")
    assert tool is not None

    result = await tool.execute(ctx, {"key": AI_MODEL_KEY, "value": NEW_MODEL})

    assert result.success is False
    assert "Unknown setting key 'ai_model'" in result.message
    # Nothing persisted and nothing cached anywhere.
    assert (await get_ai_config(OWNER_REJECT)).get("model", "") == ""
    assert AI_MODEL_KEY not in settings_service.get_all()


@pytest.mark.asyncio
async def test_ai_model_key_rejected_even_after_owner_confirmation():
    executor, ctx = make_executor(owner_id=OWNER_REJECT)

    result = await confirmed_set(executor, ctx, AI_MODEL_KEY, NEW_MODEL)

    assert result.success is False
    assert "Unknown setting key" in result.message
    assert (await get_ai_config(OWNER_REJECT)).get("model", "") == ""


# ── 3. canonical model key: confirmation gate stays intact ──


@pytest.mark.asyncio
async def test_model_change_still_requires_confirmation():
    executor, ctx = make_executor(owner_id=OWNER_GATE)

    results = await executor.execute_calls(
        [{"name": "settings_set", "arguments": {"key": CANONICAL_MODEL, "value": NEW_MODEL}}],
        owner_id=OWNER_GATE, session_id=SESS,
    )

    assert results[0].needs_confirmation is True
    assert results[0].success is False
    assert (await get_ai_config(OWNER_GATE)).get("model", "") == ""


# ── 4. confirmed model change: persisted → runtime → next request ──


@pytest.mark.asyncio
async def test_confirmed_model_change_persists_applies_and_serves_next_request():
    recorded: list[dict[str, str]] = []
    engine, manager = make_runtime_engine(recorded)
    manager.apply_selection("prov-a", "model-a")
    assert manager.get_active_name() == "prov-a"

    from backend.ai import config_store
    await config_store.update_provider(OWNER_RUNTIME, "prov-a", "model-a")

    executor, ctx = make_executor(owner_id=OWNER_RUNTIME)
    with _apply_patch(engine, manager):
        result = await confirmed_set(executor, ctx, CANONICAL_MODEL, NEW_MODEL)

    assert result.success is True
    assert "Model set to 'gpt-oss-1200'" in result.message
    # Persisted in the authoritative store.
    assert (await get_ai_config(OWNER_RUNTIME))["model"] == NEW_MODEL
    # Runtime provider instance updated (the object the next request uses).
    assert manager.get_provider_config("prov-a").default_model == NEW_MODEL
    assert manager.get_active_name() == "prov-a"

    # A NEW AI request through the real Engine is served by the new model.
    res = await _next_request(engine)
    assert res.success is True
    assert recorded, "provider should have served the request"
    assert recorded[-1] == {"provider": "prov-a", "model": NEW_MODEL}


# ── 5. prompt labels use the canonical key names ──


def test_prompt_builder_renders_canonical_provider_model_labels():
    from backend.ai.conversation.context_builder import (
        ConversationContext,
        ReplyContext,
        RuntimeContext,
        SettingsContext,
        ToolContext,
    )
    from backend.ai.conversation.state import ConversationState
    from backend.ai.prompt.builder import PromptBuilder

    ctx = ConversationContext(
        session_id="s1", owner_id=OWNER_PROMPT, chat_id=CHAT, message_id=1,
        state=ConversationState.IDLE, current_menu="main", current_panel="",
        current_category="", current_flow="", pending_action="",
        language="English", timezone="UTC", current_time="2026-01-01 12:00",
        user_text="change the model",
        reply=ReplyContext(
            exists=True, message_id=7, chat_id=CHAT, sender_id=1,
            is_ai_message=True, ai_session_id="s0", ai_role="assistant",
            ai_provider="prov-a", ai_model="model-a", ai_content="hello",
        ),
        tool=ToolContext(),
        settings=SettingsContext(),
        runtime=RuntimeContext(),
        history=[],
    )
    package = PromptBuilder().build(ctx)
    rendered = package.conversation_context

    # Canonical key names are what the model sees.
    assert "Model: model-a" in rendered
    assert "Provider: prov-a" in rendered
    # The misleading labels that taught the model 'ai_model' are gone.
    assert "AI Model:" not in rendered
    assert "AI Provider:" not in rendered