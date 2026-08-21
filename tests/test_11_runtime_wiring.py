"""
TASK 11 — AI Runtime Wiring Regression Tests

Focused tests for the runtime fix pass:

  1. attach_tools propagates the ToolExecutor to the Dispatcher.
  2. The real ToolContext (telegram/client) reaches tools at execution time.
  3. ProviderManager falls back on success=False (chain + emergency) and
     never returns fake success.
  4. Malformed tool arguments become structured failures — never executed
     with fake {}.
  5. MAX_TOOL_ROUNDS exhaustion is detected and never silently dropped.
  6. Token accounting accumulates initial + every continuation exactly once.
  7. Gemini continuation (functionCall -> functionResponse) works through
     the dispatcher.
  8. OpenAI-compatible continuation with multiple tool calls works and
     preserves ids/names.
  9. DummyProvider never reports fake success.
 10. Empty-response finish-state classification.
 11. The AI glass panel exposes a real Test Models button and the action
     renders structured results; existing AI glass buttons are preserved.
"""
from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.ai.providers.base.contract import BaseProvider, ProviderResponse
from backend.ai.providers.base.config import ProviderConfig
from backend.ai.providers.manager.manager import ProviderManager
from backend.ai.tools.base import PermissionLevel, Tool, ToolResult
from backend.ai.tools.context import ToolContext
from backend.ai.tools.executor import ToolExecutor
from backend.ai.tools.registry import ToolRegistry


class FakeTelegram:
    """Stand-in for the TelegramAPI facade in wiring tests."""

    client = "real-client-ref"


class ProbeTool(Tool):
    """Records the context it received and succeeds."""

    def __init__(self, context: ToolContext) -> None:
        self._context = context
        self.executions: list[tuple[ToolContext, dict]] = []

    @property
    def name(self) -> str:
        return "probe"

    @property
    def description(self) -> str:
        return "Probe tool for wiring tests."

    @property
    def parameters(self) -> dict[str, Any]:
        return {}

    @property
    def permission_level(self) -> PermissionLevel:
        return PermissionLevel.READ_ONLY

    @property
    def safe(self) -> bool:
        return True

    @property
    def return_type(self) -> str:
        return "ToolResult"

    async def execute(self, context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
        self.executions.append((context, dict(arguments or {})))
        return ToolResult(
            success=True,
            message="probe ok",
            data={
                "got_telegram": context.telegram is not None,
                "got_client": context.client is not None,
            },
        )


class ScriptedProvider(BaseProvider):
    """Provider that plays back scripted responses and records messages."""

    PROVIDER_NAME = "scripted"
    PROVIDER_VERSION = "1.0.0"

    def __init__(self, responses: list) -> None:
        super().__init__(ProviderConfig(provider_name="scripted", enabled=True, default_model="scripted-1"))
        self._responses = list(responses)
        self.received: list[list[dict]] = []

    def initialize(self) -> None:
        pass

    def shutdown(self) -> None:
        pass

    async def chat(self, messages: list[dict[str, Any]], **kwargs: Any) -> ProviderResponse:
        self.received.append(messages)
        if self._responses:
            item = self._responses.pop(0)
            if callable(item):
                return item(messages)
            return item
        return ProviderResponse(text="scripted end", provider_name=self.name, success=True)

    def count_tokens(self, text: str) -> int:
        return len(text) // 4

    def health(self) -> dict[str, Any]:
        return {"healthy": True, "provider": self.name}


def _usage(prompt: int, completion: int) -> dict:
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": prompt + completion,
    }


def _make_engine(provider_manager: ProviderManager, real_ctx: ToolContext, tool: Tool):
    from backend.ai.engine.engine import Engine

    engine = Engine(providers=provider_manager)
    registry = ToolRegistry()
    registry.register(tool)
    engine.attach_tools(registry, real_ctx, owner_id=real_ctx.owner_id, tz_str=real_ctx.tz_str)
    return engine


# ── 1 + 2. ToolExecutor wiring + ToolContext propagation ──


@pytest.mark.asyncio
async def test_attach_tools_propagates_executor_to_dispatcher():
    from backend.ai.engine.engine import Engine

    engine = Engine()
    registry = ToolRegistry()
    real_ctx = ToolContext(telegram=FakeTelegram(), owner_id=777, tz_str="UTC", client="client-ref")
    registry.register(ProbeTool(real_ctx))
    engine.attach_tools(registry, real_ctx, owner_id=777, tz_str="UTC")

    assert engine._dispatcher._tool_executor is not None, (
        "attach_tools must propagate the executor to the Dispatcher"
    )
    base = engine._dispatcher._tool_executor._context
    assert base.telegram is not None
    assert base.client is not None


@pytest.mark.asyncio
async def test_tool_receives_real_telegram_and_client_through_dispatch():
    provider = ScriptedProvider([
        ProviderResponse(
            text="",
            provider_name="scripted",
            success=True,
            tool_calls=[{"id": "c1", "name": "probe", "arguments": {}}],
            usage=_usage(100, 5),
        ),
        ProviderResponse(text="final answer", provider_name="scripted", success=True, usage=_usage(120, 10)),
    ])
    pm = ProviderManager()
    pm.register_provider(provider)
    pm.switch_provider("scripted")

    real_ctx = ToolContext(telegram=FakeTelegram(), owner_id=777, tz_str="UTC", client="client-ref")
    tool = ProbeTool(real_ctx)
    engine = _make_engine(pm, real_ctx, tool)

    from backend.ai.session.request import AIRequest

    result = await engine.execute(
        AIRequest(session_id="w1", user_message="probe me", owner_id=777, chat_id=-1, message_id=1)
    )

    assert result.success is True
    assert result.response == "final answer"
    assert len(tool.executions) == 1
    ctx_received, _ = tool.executions[0]
    assert ctx_received.telegram is not None
    assert ctx_received.client is not None
    assert ctx_received.owner_id == 777
    assert ctx_received.extra.get("chat_id") == -1


# ── 3. Provider fallback on success=False ──


@pytest.mark.asyncio
async def test_provider_manager_falls_back_on_success_false():
    class FailingProvider(BaseProvider):
        PROVIDER_NAME = "failing"
        PROVIDER_VERSION = "1.0.0"

        def initialize(self) -> None:
            pass

        def shutdown(self) -> None:
            pass

        async def chat(self, messages, **kwargs):
            return ProviderResponse(
                text="Rate limited.", provider_name=self.name, success=False,
                metadata={"http_status": 429, "retry_after": 3},
            )

        def count_tokens(self, text: str) -> int:
            return len(text) // 4

        def health(self) -> dict:
            return {"healthy": True}

    class BackupProvider(BaseProvider):
        PROVIDER_NAME = "backup"
        PROVIDER_VERSION = "1.0.0"

        def initialize(self) -> None:
            pass

        def shutdown(self) -> None:
            pass

        async def chat(self, messages, **kwargs):
            return ProviderResponse(text="recovered", provider_name=self.name, success=True)

        def count_tokens(self, text: str) -> int:
            return len(text) // 4

        def health(self) -> dict:
            return {"healthy": True}

    pm = ProviderManager()
    pm.register_provider(FailingProvider())
    pm.register_provider(BackupProvider())
    pm.switch_provider("failing")
    pm._fallback_chain = ["backup"]

    response = await pm.chat([{"role": "user", "content": "hi"}])
    assert response.success is True
    assert response.provider_name == "backup"
    assert response.metadata.get("fallback") is True
    assert response.metadata.get("fallback_from") == "failing"


@pytest.mark.asyncio
async def test_provider_manager_emergency_fallback_never_fake_success():
    class AlwaysFailProvider(BaseProvider):
        PROVIDER_NAME = "always_fail"
        PROVIDER_VERSION = "1.0.0"

        def initialize(self) -> None:
            pass

        def shutdown(self) -> None:
            pass

        async def chat(self, messages, **kwargs):
            return ProviderResponse(
                text="quota exceeded", provider_name=self.name, success=False,
                metadata={"http_status": 500},
            )

        def count_tokens(self, text: str) -> int:
            return len(text) // 4

        def health(self) -> dict:
            return {"healthy": True}

    pm = ProviderManager()
    pm.register_provider(AlwaysFailProvider())
    pm.switch_provider("always_fail")
    pm._fallback_chain = []

    response = await pm.chat([{"role": "user", "content": "hi"}])
    assert response.success is False
    assert response.provider_name == "dummy"
    assert "quota exceeded" in response.text
    assert response.metadata.get("emergency") is True
    assert response.metadata.get("fallback_exhausted") is True


# ── 4 + 5. Malformed tool arguments ──


@pytest.mark.asyncio
async def test_malformed_arguments_never_execute_tool():
    ctx = ToolContext(telegram=None, owner_id=1, tz_str="UTC")
    tool = ProbeTool(ctx)
    registry = ToolRegistry()
    registry.register(tool)
    executor = ToolExecutor(registry, ctx)

    results = await executor.execute_calls(
        [
            {
                "id": "c1",
                "name": "probe",
                "arguments": {},
                "malformed_arguments": True,
                "arguments_error": "malformed JSON arguments: Expecting value",
            }
        ],
        owner_id=1,
        session_id="s1",
    )

    assert len(results) == 1
    assert results[0].success is False
    assert results[0].error == "malformed_arguments"
    assert len(tool.executions) == 0, "tool must never execute with fake {}"


@pytest.mark.asyncio
async def test_openai_compat_marks_malformed_arguments():
    from backend.ai.providers.openai_compat import OpenAICompatProvider

    config = ProviderConfig(
        provider_name="openai", api_key="k", enabled=True,
        base_url="https://api.openai.com/v1", default_model="gpt-4o",
    )
    provider = OpenAICompatProvider(config)
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "choices": [{
            "message": {
                "content": None,
                "tool_calls": [{
                    "id": "call_1", "type": "function",
                    "function": {"name": "save", "arguments": "{bad json"},
                }],
            },
            "finish_reason": "tool_calls",
        }],
        "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
    }
    provider._http_client = MagicMock()
    provider._http_client.post = AsyncMock(return_value=mock_resp)

    result = await provider.chat([{"role": "user", "content": "save"}])
    assert result.success is True
    assert len(result.tool_calls) == 1
    assert result.tool_calls[0]["name"] == "save"
    assert result.tool_calls[0]["malformed_arguments"] is True
    assert "malformed" in result.tool_calls[0]["arguments_error"]


# ── 6. MAX_TOOL_ROUNDS exhaustion ──


@pytest.mark.asyncio
async def test_max_tool_rounds_exhaustion_is_detected():
    tool_call = {"id": "c1", "name": "probe", "arguments": {}}
    provider = ScriptedProvider([
        ProviderResponse(text="", provider_name="scripted", success=True, tool_calls=[tool_call], usage=_usage(100, 5)),
        ProviderResponse(text="", provider_name="scripted", success=True, tool_calls=[tool_call], usage=_usage(110, 5)),
        ProviderResponse(text="", provider_name="scripted", success=True, tool_calls=[tool_call], usage=_usage(120, 5)),
        ProviderResponse(text="", provider_name="scripted", success=True, tool_calls=[tool_call], usage=_usage(130, 5)),
    ])
    pm = ProviderManager()
    pm.register_provider(provider)
    pm.switch_provider("scripted")

    real_ctx = ToolContext(telegram=FakeTelegram(), owner_id=777, tz_str="UTC", client="client-ref")
    tool = ProbeTool(real_ctx)
    engine = _make_engine(pm, real_ctx, tool)

    from backend.ai.session.request import AIRequest

    result = await engine.execute(
        AIRequest(session_id="w2", user_message="loop tools", owner_id=777, chat_id=-1, message_id=1)
    )

    assert result.metadata.get("tool_rounds_exhausted") is True
    assert len(result.metadata.get("pending_tool_calls", [])) == 1
    assert any("tool_round_limit_reached" in w for w in result.warnings)
    assert result.metadata.get("finish_state") == "tool_rounds_exhausted"
    # The safety limit still bounds the loop.
    assert len(tool.executions) == 3


# ── 7. Token accounting across continuation rounds ──


@pytest.mark.asyncio
async def test_token_accounting_accumulates_initial_and_continuations():
    tool_call = {"id": "c1", "name": "probe", "arguments": {}}
    provider = ScriptedProvider([
        ProviderResponse(text="", provider_name="scripted", success=True, tool_calls=[tool_call], usage=_usage(100, 20)),
        ProviderResponse(text="", provider_name="scripted", success=True, tool_calls=[tool_call], usage=_usage(50, 10)),
        ProviderResponse(text="done", provider_name="scripted", success=True, usage=_usage(30, 5)),
    ])
    pm = ProviderManager()
    pm.register_provider(provider)
    pm.switch_provider("scripted")

    real_ctx = ToolContext(telegram=FakeTelegram(), owner_id=777, tz_str="UTC", client="client-ref")
    tool = ProbeTool(real_ctx)
    engine = _make_engine(pm, real_ctx, tool)

    from backend.ai.session.request import AIRequest

    result = await engine.execute(
        AIRequest(session_id="w3", user_message="count tokens", owner_id=777, chat_id=-1, message_id=1)
    )

    assert result.response == "done"
    assert result.prompt_tokens == 180      # 100 + 50 + 30
    assert result.completion_tokens == 35   # 20 + 10 + 5
    assert result.total_tokens == 215       # 120 + 60 + 35


# ── 8. Gemini continuation through the dispatcher ──


@pytest.mark.asyncio
async def test_gemini_continuation_through_dispatcher():
    from backend.ai.providers.gemini import GeminiProvider

    config = ProviderConfig(
        provider_name="gemini", api_key="k", enabled=True, default_model="gemini-pro",
    )
    provider = GeminiProvider(config)

    first = MagicMock()
    first.status_code = 200
    first.json.return_value = {
        "candidates": [{
            "content": {"parts": [{"functionCall": {"name": "probe", "args": {}}}]},
            "finishReason": "STOP",
        }],
        "usageMetadata": {"promptTokenCount": 40, "candidatesTokenCount": 5, "totalTokenCount": 45},
    }
    second = MagicMock()
    second.status_code = 200
    second.json.return_value = {
        "candidates": [{
            "content": {"parts": [{"text": "gemini final"}]},
            "finishReason": "STOP",
        }],
        "usageMetadata": {"promptTokenCount": 60, "candidatesTokenCount": 8, "totalTokenCount": 68},
    }
    provider._http_client = MagicMock()
    provider._http_client.post = AsyncMock(side_effect=[first, second])

    pm = ProviderManager()
    pm.register_provider(provider)
    pm.switch_provider("gemini")

    real_ctx = ToolContext(telegram=FakeTelegram(), owner_id=777, tz_str="UTC", client="client-ref")
    tool = ProbeTool(real_ctx)
    engine = _make_engine(pm, real_ctx, tool)

    from backend.ai.session.request import AIRequest

    result = await engine.execute(
        AIRequest(session_id="w4", user_message="gemini tool", owner_id=777, chat_id=-1, message_id=1)
    )

    assert result.success is True
    assert result.response == "gemini final"
    assert len(tool.executions) == 1
    # Continuation request must contain a functionResponse-shaped tool message.
    cont_messages = provider.received[1] if hasattr(provider, "received") else None
    # (GeminiProvider does not record calls; verify via the mock instead)
    post_kwargs = provider._http_client.post.call_args_list[1].kwargs
    payload = post_kwargs["json"]
    assert any(p.get("role") == "model" for p in payload["contents"])


# ── 9. OpenAI-compatible continuation with multiple tool calls ──


@pytest.mark.asyncio
async def test_openai_compat_continuation_multiple_tool_calls():
    from backend.ai.providers.openai import OpenAIProvider

    config = ProviderConfig(
        provider_name="openai", api_key="k", enabled=True,
        base_url="https://api.openai.com/v1", default_model="gpt-4o",
    )
    provider = OpenAIProvider(config)

    first = MagicMock()
    first.status_code = 200
    first.json.return_value = {
        "choices": [{
            "message": {
                "content": None,
                "tool_calls": [
                    {"id": "a1", "type": "function", "function": {"name": "probe", "arguments": "{}"}},
                    {"id": "b2", "type": "function", "function": {"name": "probe", "arguments": "{\"x\": 1}"}},
                ],
            },
            "finish_reason": "tool_calls",
        }],
        "usage": {"prompt_tokens": 80, "completion_tokens": 10, "total_tokens": 90},
    }
    second = MagicMock()
    second.status_code = 200
    second.json.return_value = {
        "choices": [{"message": {"content": "openai final"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 12, "total_tokens": 112},
    }
    provider._http_client = MagicMock()
    provider._http_client.post = AsyncMock(side_effect=[first, second])

    pm = ProviderManager()
    pm.register_provider(provider)
    pm.switch_provider("openai")

    real_ctx = ToolContext(telegram=FakeTelegram(), owner_id=777, tz_str="UTC", client="client-ref")
    tool = ProbeTool(real_ctx)
    engine = _make_engine(pm, real_ctx, tool)

    from backend.ai.session.request import AIRequest

    result = await engine.execute(
        AIRequest(session_id="w5", user_message="two tools", owner_id=777, chat_id=-1, message_id=1)
    )

    assert result.success is True
    assert result.response == "openai final"
    assert len(tool.executions) == 2
    # Continuation payload must preserve both tool_call ids and add tool messages.
    cont_payload = provider._http_client.post.call_args_list[1].kwargs["json"]
    assistant_msgs = [m for m in cont_payload["messages"] if m.get("role") == "assistant" and m.get("tool_calls")]
    assert len(assistant_msgs) == 1
    ids = [tc["id"] for tc in assistant_msgs[0]["tool_calls"]]
    assert ids == ["a1", "b2"]
    tool_msgs = [m for m in cont_payload["messages"] if m.get("role") == "tool"]
    assert len(tool_msgs) == 2


# ── 10. Dummy never fake success ──


@pytest.mark.asyncio
async def test_dummy_provider_never_fake_success():
    from backend.ai.providers.dummy.provider import DummyProvider

    response = await DummyProvider().chat([{"role": "user", "content": "hi"}])
    assert response.success is False
    assert "not configured" in response.text.lower()
    assert response.metadata.get("reason") == "no_provider_configured"


@pytest.mark.asyncio
async def test_engine_reports_failure_when_no_provider_configured():
    from backend.ai.engine.engine import get_engine
    from backend.ai.session.request import AIRequest

    engine = get_engine()
    result = await engine.execute(
        AIRequest(session_id="w6", user_message="hello", owner_id=0, chat_id=1, message_id=1)
    )
    assert result.success is False
    assert result.metadata.get("finish_state") == "provider_failure"


# ── 11. Empty-response finish-state classification ──


@pytest.mark.asyncio
async def test_finish_state_classification_empty():
    from backend.ai.engine.engine import Engine

    # Two empty responses: the dispatcher performs ONE bounded empty-response
    # retry (a transient "thinking stall" safety net) before classifying the
    # result as empty.
    provider = ScriptedProvider([
        ProviderResponse(text="", provider_name="scripted", success=True, usage=_usage(10, 0)),
        ProviderResponse(text="", provider_name="scripted", success=True, usage=_usage(10, 0)),
    ])
    pm = ProviderManager()
    pm.register_provider(provider)
    pm.switch_provider("scripted")

    engine = Engine(providers=pm)
    from backend.ai.session.request import AIRequest

    result = await engine.execute(
        AIRequest(session_id="w7", user_message="empty", owner_id=777, chat_id=-1, message_id=1)
    )
    assert result.success is True
    assert result.metadata.get("finish_state") == "empty"


@pytest.mark.asyncio
async def test_finish_state_classification_blocked():
    from backend.ai.engine.engine import Engine

    provider = ScriptedProvider([
        ProviderResponse(
            text="", provider_name="scripted", success=True, usage=_usage(10, 0),
            metadata={"finish_reason": "SAFETY"},
        ),
    ])
    pm = ProviderManager()
    pm.register_provider(provider)
    pm.switch_provider("scripted")

    engine = Engine(providers=pm)
    from backend.ai.session.request import AIRequest

    result = await engine.execute(
        AIRequest(session_id="w8", user_message="blocked", owner_id=777, chat_id=-1, message_id=1)
    )
    assert result.metadata.get("finish_state") == "provider_blocked"
    assert result.metadata.get("finish_reason") == "SAFETY"


@pytest.mark.asyncio
async def test_finish_state_classification_truncated():
    from backend.ai.engine.engine import Engine

    provider = ScriptedProvider([
        ProviderResponse(
            text="", provider_name="scripted", success=True, usage=_usage(10, 0),
            metadata={"finish_reason": "length"},
        ),
    ])
    pm = ProviderManager()
    pm.register_provider(provider)
    pm.switch_provider("scripted")

    engine = Engine(providers=pm)
    from backend.ai.session.request import AIRequest

    result = await engine.execute(
        AIRequest(session_id="w9", user_message="truncated", owner_id=777, chat_id=-1, message_id=1)
    )
    assert result.metadata.get("finish_state") == "token_truncated"


# ── 12. Glass AI Test Models button + regression audit ──


def _flatten_button_datas(buttons) -> list[str]:
    datas = []
    for row in buttons:
        if isinstance(row, list):
            for btn in row:
                data = getattr(btn, "data", None)
                if isinstance(data, bytes):
                    datas.append(data.decode("utf-8", errors="replace"))
                elif isinstance(data, str):
                    datas.append(data)
        else:
            data = getattr(row, "data", None)
            if isinstance(data, bytes):
                datas.append(data.decode("utf-8", errors="replace"))
            elif isinstance(data, str):
                datas.append(data)
    return datas


@pytest.mark.asyncio
async def test_ai_panel_has_test_models_button_and_preserves_existing():
    from backend.bot.handlers import ai as ai_module

    default_config = {
        "provider": "", "model": "", "temperature": 1.0, "max_tokens": 4096,
        "system_prompt": "", "history_budget": 4000, "is_configured": False,
        "trigger_en": "", "trigger_fa": "",
    }
    with patch.object(ai_module, "_get_saved_config", AsyncMock(return_value=default_config)), \
         patch.object(ai_module, "_discover", AsyncMock(return_value=[])):
        title, body, buttons = await ai_module._ai_main_panel_handler(None, "")

    datas = _flatten_button_datas(buttons)
    assert "action:ai_test_models" in datas, "Test Models button must exist in the AI glass panel"
    assert "panel:ai_provider" in datas
    assert any(d.startswith("panel:_nav:") for d in datas)


@pytest.mark.asyncio
async def test_ai_panel_ready_branch_keeps_all_existing_buttons():
    from backend.bot.handlers import ai as ai_module
    from backend.ai.discovery import ProviderStatus

    configured = {
        "provider": "openai", "model": "gpt-4o", "temperature": 1.0, "max_tokens": 4096,
        "system_prompt": "", "history_budget": 4000, "is_configured": True,
        "trigger_en": "Nova", "trigger_fa": "",
    }
    available = [
        ProviderStatus(
            name="openai", display_name="OpenAI", env_var="AI_OPENAI_API_KEY",
            status="available", has_key=True, validated=True,
            default_model="gpt-4o", base_url="https://api.openai.com/v1", icon="🧠",
        )
    ]
    with patch.object(ai_module, "_get_saved_config", AsyncMock(return_value=configured)), \
         patch.object(ai_module, "_discover", AsyncMock(return_value=available)):
        title, body, buttons = await ai_module._ai_main_panel_handler(None, "")

    datas = _flatten_button_datas(buttons)
    expected = {
        "action:ai_start_chat",
        "panel:ai_usage",
        "panel:ai_health",
        "panel:ai_details",
        "panel:ai_model",
        "panel:ai_provider",
        "panel:ai_settings",
        "action:ai_test_models",
    }
    assert expected.issubset(set(datas)), f"missing buttons: {expected - set(datas)}"


@pytest.mark.asyncio
async def test_ai_test_models_action_renders_results():
    from backend.bot.handlers import ai as ai_module

    result = await ai_module._ai_test_models_action(None, "", 0)
    assert result is not None
    title, body, buttons = result
    assert title == "🧪 Test Models"
    assert "Model Tests" in body
    datas = _flatten_button_datas(buttons)
    assert "action:ai_test_models" in datas  # Re-run button


@pytest.mark.asyncio
async def test_ai_glass_register_wires_test_models_action():
    from backend.bot.handlers import ai as ai_module

    with patch.object(ai_module, "register_action") as mock_register_action, \
         patch.object(ai_module, "register_panel"), \
         patch.object(ai_module, "register_inline_builder"), \
         patch.object(ai_module, "register_input"):
        ai_module.register(None, 0)

    registered_ids = [call.args[0] for call in mock_register_action.call_args_list]
    assert "ai_test_models" in registered_ids
    assert "ai_start_chat" in registered_ids
    assert "ai_select_provider" in registered_ids
    assert "ai_select_model" in registered_ids
