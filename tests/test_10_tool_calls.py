"""
Focused tests for AI tool-call parsing, continuation, and token preservation.
"""
from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.ai.providers.base.contract import ProviderResponse
from backend.ai.tools.executor import ToolExecutionResult


@pytest.mark.asyncio
async def test_openai_compat_parses_tool_calls():
    from backend.ai.providers.base.config import ProviderConfig
    from backend.ai.providers.openai_compat import OpenAICompatProvider

    config = ProviderConfig(api_key="k", enabled=True, base_url="https://api.openai.com/v1", default_model="gpt-4")
    provider = OpenAICompatProvider(config)

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "choices": [{
            "message": {
                "content": None,
                "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "save", "arguments": json.dumps({"type": "forward"})}}],
            },
            "finish_reason": "tool_calls",
        }],
        "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
    }
    provider._http_client = MagicMock()
    provider._http_client.post = AsyncMock(return_value=mock_resp)

    result = await provider.chat([{"role": "user", "content": "Save this"}])

    assert result.success is True
    assert len(result.tool_calls) == 1
    assert result.tool_calls[0]["name"] == "save"
    assert result.tool_calls[0]["id"] == "call_1"
    assert result.tool_calls[0]["arguments"] == {"type": "forward"}
    assert result.usage["total_tokens"] == 120


@pytest.mark.asyncio
async def test_gemini_parses_function_calls():
    from backend.ai.providers.base.config import ProviderConfig
    from backend.ai.providers.gemini import GeminiProvider

    config = ProviderConfig(api_key="k", enabled=True, default_model="gemini-pro")
    provider = GeminiProvider(config)

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "candidates": [{
            "content": {"parts": [{"functionCall": {"name": "search", "args": {"query": "test"}}}]},
            "finishReason": "STOP",
        }],
        "usageMetadata": {"promptTokenCount": 50, "candidatesTokenCount": 10, "totalTokenCount": 60},
    }
    provider._http_client = MagicMock()
    provider._http_client.post = AsyncMock(return_value=mock_resp)

    result = await provider.chat([{"role": "user", "content": "Search for test"}])

    assert result.success is True
    assert len(result.tool_calls) == 1
    assert result.tool_calls[0]["name"] == "search"
    assert result.tool_calls[0]["arguments"] == {"query": "test"}
    assert result.usage["total_tokens"] == 60


@pytest.mark.asyncio
async def test_gemini_handles_tool_result_messages():
    from backend.ai.providers.base.config import ProviderConfig
    from backend.ai.providers.gemini import GeminiProvider

    config = ProviderConfig(api_key="k", enabled=True, default_model="gemini-pro")
    provider = GeminiProvider(config)

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "candidates": [{"content": {"parts": [{"text": "Done"}]}, "finishReason": "STOP"}],
        "usageMetadata": {"promptTokenCount": 80, "candidatesTokenCount": 30, "totalTokenCount": 110},
    }
    provider._http_client = MagicMock()
    provider._http_client.post = AsyncMock(return_value=mock_resp)

    messages = [
        {"role": "user", "content": "Search"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "search", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "name": "search", "content": json.dumps({"success": True})},
    ]
    result = await provider.chat(messages)

    assert result.success is True
    payload = provider._http_client.post.call_args.kwargs["json"]
    contents = payload["contents"]
    assert len(contents) == 3
    assert "functionCall" in contents[1]["parts"][0]
    assert "functionResponse" in contents[2]["parts"][0]


@pytest.mark.asyncio
async def test_dispatcher_tool_continuation():
    from backend.ai.engine.dispatcher import Dispatcher
    from backend.ai.engine.hooks import NOOP_HOOKS
    from backend.ai.engine.metrics import EngineMetrics
    from backend.ai.session.request import AIRequest

    mock_pm = MagicMock()
    mock_pm.get_active_name.return_value = "test"
    mock_pm.get_active.return_value.config.model = "m"
    mock_pm.get_active.return_value.health.return_value = {"healthy": True}
    mock_pm.get_active.return_value.chat = AsyncMock()
    first = ProviderResponse(text="", provider_name="test", success=True,
        tool_calls=[{"id": "c1", "name": "search", "arguments": {"q": "t"}}],
        usage={"prompt_tokens": 50, "completion_tokens": 10, "total_tokens": 60},
        metadata={"finish_reason": "tool_calls"})
    final = ProviderResponse(text="Found 3 items.", provider_name="test", success=True,
        usage={"prompt_tokens": 70, "completion_tokens": 25, "total_tokens": 95},
        metadata={"finish_reason": "stop"})
    mock_pm.get_active().chat = AsyncMock(side_effect=[first, final])

    mock_conv = MagicMock()
    mock_sess = MagicMock()
    mock_sess.session_id = "s"
    mock_sess.active_provider = "test"
    mock_conv.get_session.return_value = mock_sess
    mock_conv.restore_history = AsyncMock()
    mock_conv.get_history.return_value = []

    mock_pb = MagicMock()
    pp = MagicMock()
    pp.system_prompt = "sys"
    pp.runtime_context = ""
    pp.conversation_context = ""
    pp.tool_context = ""
    pp.user_input = "Search"
    pp.estimated_tokens.estimated_input_tokens = 50
    pp.estimated_tokens.prompt_size_chars = 100
    mock_pb.build.return_value = pp

    mock_te = MagicMock()
    mock_te.execute_calls = AsyncMock(return_value=[ToolExecutionResult(tool_name="search", success=True, message="Found 3")])
    mock_te._context = MagicMock()
    mock_te._context.extra = {}

    d = Dispatcher(mock_conv, mock_pb, mock_pm, NOOP_HOOKS, EngineMetrics(), tool_executor=mock_te)
    result = await d.dispatch(AIRequest(session_id="s1", message_id=1, owner_id=123, user_message="Search", chat_id=456))

    assert result.success is True
    assert result.response == "Found 3 items."
    assert mock_pm.get_active().chat.call_count == 2
    assert result.metadata.get("tool_rounds") == 1


@pytest.mark.asyncio
async def test_tool_error_distinguishable():
    from backend.ai.engine.dispatcher import Dispatcher
    from backend.ai.engine.hooks import NOOP_HOOKS
    from backend.ai.engine.metrics import EngineMetrics
    from backend.ai.session.request import AIRequest

    mock_pm = MagicMock()
    mock_pm.get_active_name.return_value = "test"
    mock_pm.get_active.return_value.config.model = "m"
    mock_pm.get_active.return_value.health.return_value = {"healthy": True}
    mock_pm.get_active.return_value.chat = AsyncMock()
    first = ProviderResponse(text="", provider_name="test", success=True,
        tool_calls=[{"id": "c1", "name": "bad_tool", "arguments": {}}],
        usage={"prompt_tokens": 50, "completion_tokens": 10, "total_tokens": 60})
    final = ProviderResponse(text="Can't find that tool.", provider_name="test", success=True,
        usage={"prompt_tokens": 60, "completion_tokens": 15, "total_tokens": 75})
    mock_pm.get_active().chat = AsyncMock(side_effect=[first, final])

    mock_conv = MagicMock()
    mock_sess = MagicMock()
    mock_sess.session_id = "s"
    mock_sess.active_provider = "test"
    mock_conv.get_session.return_value = mock_sess
    mock_conv.restore_history = AsyncMock()
    mock_conv.get_history.return_value = []

    mock_pb = MagicMock()
    pp = MagicMock()
    pp.system_prompt = "sys"
    pp.runtime_context = ""
    pp.conversation_context = ""
    pp.tool_context = ""
    pp.user_input = "Run"
    pp.estimated_tokens.estimated_input_tokens = 50
    pp.estimated_tokens.prompt_size_chars = 100
    mock_pb.build.return_value = pp

    mock_te = MagicMock()
    mock_te.execute_calls = AsyncMock(return_value=[ToolExecutionResult(tool_name="bad_tool", success=False, message="not registered", error="not_found")])
    mock_te._context = MagicMock()
    mock_te._context.extra = {}

    d = Dispatcher(mock_conv, mock_pb, mock_pm, NOOP_HOOKS, EngineMetrics(), tool_executor=mock_te)
    result = await d.dispatch(AIRequest(session_id="s1", message_id=1, owner_id=123, user_message="Run", chat_id=456))

    assert result.success is True
    tr = result.metadata.get("tool_results", [])
    assert len(tr) == 1
    assert tr[0]["success"] is False
    assert tr[0]["error"] == "not_found"


@pytest.mark.asyncio
async def test_token_usage_preserved():
    from backend.ai.engine.dispatcher import Dispatcher
    from backend.ai.engine.hooks import NOOP_HOOKS
    from backend.ai.engine.metrics import EngineMetrics
    from backend.ai.session.request import AIRequest

    mock_pm = MagicMock()
    mock_pm.get_active_name.return_value = "test"
    mock_pm.get_active.return_value.config.model = "m"
    mock_pm.get_active.return_value.health.return_value = {"healthy": True}
    mock_pm.get_active.return_value.chat = AsyncMock()
    resp = ProviderResponse(text="Hi", provider_name="test", success=True,
        usage={"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
        metadata={"finish_reason": "stop"})
    mock_pm.get_active().chat = AsyncMock(return_value=resp)

    mock_conv = MagicMock()
    mock_sess = MagicMock()
    mock_sess.session_id = "s"
    mock_sess.active_provider = "test"
    mock_conv.get_session.return_value = mock_sess
    mock_conv.restore_history = AsyncMock()
    mock_conv.get_history.return_value = []

    mock_pb = MagicMock()
    pp = MagicMock()
    pp.system_prompt = "sys"
    pp.runtime_context = ""
    pp.conversation_context = ""
    pp.tool_context = ""
    pp.user_input = "Hi"
    pp.estimated_tokens.estimated_input_tokens = 80
    pp.estimated_tokens.prompt_size_chars = 100
    mock_pb.build.return_value = pp

    d = Dispatcher(mock_conv, mock_pb, mock_pm, NOOP_HOOKS, EngineMetrics())
    result = await d.dispatch(AIRequest(session_id="s1", message_id=1, owner_id=123, user_message="Hi", chat_id=456))

    assert result.total_tokens == 150
    assert result.prompt_tokens == 100
    assert result.completion_tokens == 50


@pytest.mark.asyncio
async def test_provider_failure_error():
    """A provider returning success=False (e.g. HTTP 429) must become a
    structured failure — the emergency fallback must NOT report fake success
    and the original provider error must survive."""
    from backend.ai.engine.dispatcher import Dispatcher
    from backend.ai.engine.hooks import NOOP_HOOKS
    from backend.ai.engine.metrics import EngineMetrics
    from backend.ai.providers.manager.manager import ProviderManager
    from backend.ai.providers.base.contract import BaseProvider
    from backend.ai.session.request import AIRequest

    class RateLimitedProvider(BaseProvider):
        PROVIDER_NAME = "rate_limited"
        PROVIDER_VERSION = "1.0.0"

        def initialize(self) -> None:
            pass

        def shutdown(self) -> None:
            pass

        async def chat(self, messages, **kwargs):
            return ProviderResponse(
                text="Rate limited.",
                provider_name=self.name,
                success=False,
                metadata={"http_status": 429, "retry_after": 5},
            )

        def count_tokens(self, text: str) -> int:
            return len(text) // 4

        def health(self) -> dict:
            return {"healthy": True, "provider": self.name}

    pm = ProviderManager()
    prov = RateLimitedProvider()
    pm.register_provider(prov)
    pm.switch_provider("rate_limited")

    mock_conv = MagicMock()
    mock_sess = MagicMock()
    mock_sess.session_id = "s"
    mock_sess.active_provider = "rate_limited"
    mock_conv.get_session.return_value = mock_sess
    mock_conv.restore_history = AsyncMock()
    mock_conv.get_history.return_value = []

    mock_pb = MagicMock()
    pp = MagicMock()
    pp.system_prompt = "sys"
    pp.runtime_context = ""
    pp.conversation_context = ""
    pp.tool_context = ""
    pp.user_input = "Hi"
    pp.estimated_tokens.estimated_input_tokens = 20
    pp.estimated_tokens.prompt_size_chars = 50
    mock_pb.build.return_value = pp

    d = Dispatcher(mock_conv, mock_pb, pm, NOOP_HOOKS, EngineMetrics())
    result = await d.dispatch(AIRequest(session_id="s1", message_id=1, owner_id=123, user_message="Hi", chat_id=456))

    assert result.success is False
    assert "Rate limited" in result.errors[0]
    assert result.metadata.get("finish_state") == "provider_failure"
    assert result.response == ""


@pytest.mark.asyncio
async def test_openai_compat_finish_reason_diagnostics():
    from backend.ai.providers.base.config import ProviderConfig
    from backend.ai.providers.openai_compat import OpenAICompatProvider

    config = ProviderConfig(api_key="k", enabled=True, base_url="https://api.openai.com/v1", default_model="gpt-4")
    provider = OpenAICompatProvider(config)

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"choices": [{"message": {"content": ""}, "finish_reason": "length"}], "usage": {}}
    provider._http_client = MagicMock()
    provider._http_client.post = AsyncMock(return_value=mock_resp)

    result = await provider.chat([{"role": "user", "content": "test"}])
    assert "truncated" in result.text.lower()


@pytest.mark.asyncio
async def test_gemini_finish_reason_diagnostics():
    from backend.ai.providers.base.config import ProviderConfig
    from backend.ai.providers.gemini import GeminiProvider

    config = ProviderConfig(api_key="k", enabled=True, default_model="gemini-pro")
    provider = GeminiProvider(config)

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"candidates": [{"content": {"parts": []}, "finishReason": "SAFETY"}], "usageMetadata": {}}
    provider._http_client = MagicMock()
    provider._http_client.post = AsyncMock(return_value=mock_resp)

    result = await provider.chat([{"role": "user", "content": "test"}])
    assert "blocked" in result.text.lower()


# ── Native tool definitions (real execution wiring) ──


def test_build_tool_definitions_uses_openai_format():
    from backend.ai.engine.dispatcher import Dispatcher
    from backend.ai.tools.base import PermissionLevel, ToolResult
    from backend.ai.tools.registry import ToolRegistry

    class PingTool:
        name = "ping"
        description = "Reply pong"
        parameters = {}
        permission_level = PermissionLevel.READ_ONLY
        safe = True
        return_type = "text"
        long_running = False

        async def execute(self, ctx, args):
            return ToolResult(success=True, message="pong")

    registry = ToolRegistry()
    registry.register(PingTool())

    d = Dispatcher.__new__(Dispatcher)
    d._tool_registry = registry
    definitions = d._build_tool_definitions()

    assert len(definitions) == 1
    fn = definitions[0]["function"]
    assert definitions[0]["type"] == "function"
    assert fn["name"] == "ping"
    assert fn["parameters"]["type"] == "object"


def test_build_tool_definitions_marks_required_params():
    from backend.ai.engine.dispatcher import Dispatcher
    from backend.ai.tools.base import PermissionLevel, ToolResult
    from backend.ai.tools.registry import ToolRegistry

    class DeleteTool:
        name = "delete"
        description = "Delete last N"
        parameters = {"count": {"type": "integer", "minimum": 1, "maximum": 500}}
        permission_level = PermissionLevel.DANGEROUS
        safe = False
        return_type = "text"
        long_running = False

        async def execute(self, ctx, args):
            return ToolResult(success=True, message="ok")

    registry = ToolRegistry()
    registry.register(DeleteTool())

    d = Dispatcher.__new__(Dispatcher)
    d._tool_registry = registry
    definitions = d._build_tool_definitions()

    assert definitions[0]["function"]["parameters"]["required"] == ["count"]


@pytest.mark.asyncio
async def test_dispatcher_passes_tools_to_provider():
    from backend.ai.engine.dispatcher import Dispatcher
    from backend.ai.engine.hooks import NOOP_HOOKS
    from backend.ai.engine.metrics import EngineMetrics
    from backend.ai.session.request import AIRequest
    from backend.ai.tools.base import PermissionLevel, ToolResult
    from backend.ai.tools.context import ToolContext
    from backend.ai.tools.executor import ToolExecutor
    from backend.ai.tools.registry import ToolRegistry

    class PingTool:
        name = "ping"
        description = "Reply pong"
        parameters = {}
        permission_level = PermissionLevel.READ_ONLY
        safe = True
        return_type = "text"
        long_running = False

        async def execute(self, ctx, args):
            return ToolResult(success=True, message="pong")

    registry = ToolRegistry()
    registry.register(PingTool())

    mock_pm = MagicMock()
    mock_pm.get_active_name.return_value = "test"
    mock_pm.get_active.return_value.config.model = "m"
    mock_pm.get_active.return_value.health.return_value = {"healthy": True}
    mock_pm.get_active.return_value.chat = AsyncMock(
        return_value=ProviderResponse(text="pong", provider_name="test", success=True, usage={})
    )

    mock_conv = MagicMock()
    mock_sess = MagicMock()
    mock_sess.session_id = "s"
    mock_sess.active_provider = "test"
    mock_conv.get_session.return_value = mock_sess
    mock_conv.restore_history = AsyncMock()
    mock_conv.get_history.return_value = []

    from backend.ai.prompt.builder import PromptPackage
    from backend.ai.prompt.budget import TokenBudget

    mock_pb = MagicMock()
    pp = PromptPackage(
        system_prompt="sys",
        runtime_context="",
        conversation_context="",
        tool_context="",
        user_input="ping",
        metadata={},
        estimated_tokens=TokenBudget(
            estimated_input_tokens=20,
            estimated_output_budget=0,
            estimated_total=20,
            prompt_size_chars=40,
            max_total_tokens=8500,
            max_output_tokens=1000,
            within_budget=True,
        ),
        sections={},
    )
    mock_pb.build.return_value = pp

    te = ToolExecutor(registry, ToolContext(telegram=None, owner_id=1, tz_str="UTC"))
    d = Dispatcher(mock_conv, mock_pb, mock_pm, NOOP_HOOKS, EngineMetrics(),
                   tool_registry=registry, tool_executor=te)
    await d.dispatch(AIRequest(session_id="s1", message_id=1, owner_id=1, user_message="ping", chat_id=10))

    call_kwargs = mock_pm.get_active.return_value.chat.call_args.kwargs
    tools = call_kwargs.get("tools")
    assert tools, "provider must receive native tool definitions"
    assert any(t["function"]["name"] == "ping" for t in tools)


@pytest.mark.asyncio
async def test_openai_compat_sends_tools_in_payload():
    from backend.ai.providers.base.config import ProviderConfig
    from backend.ai.providers.openai_compat import OpenAICompatProvider

    provider = OpenAICompatProvider(ProviderConfig(
        api_key="k", enabled=True, base_url="https://api.openai.com/v1", default_model="gpt-4"
    ))
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}], "usage": {}}
    provider._http_client = MagicMock()
    provider._http_client.post = AsyncMock(return_value=mock_resp)

    tools = [{"type": "function", "function": {"name": "save", "description": "d", "parameters": {"type": "object", "properties": {}}}}]
    await provider.chat([{"role": "user", "content": "hi"}], tools=tools)

    payload = provider._http_client.post.call_args.kwargs["json"]
    assert payload["tools"] == tools


@pytest.mark.asyncio
async def test_gemini_sends_function_declarations():
    from backend.ai.providers.base.config import ProviderConfig
    from backend.ai.providers.gemini import GeminiProvider

    provider = GeminiProvider(ProviderConfig(api_key="k", enabled=True, default_model="gemini-pro"))
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"candidates": [{"content": {"parts": [{"text": "ok"}]}, "finishReason": "STOP"}], "usageMetadata": {}}
    provider._http_client = MagicMock()
    provider._http_client.post = AsyncMock(return_value=mock_resp)

    tools = [{"type": "function", "function": {"name": "save", "description": "d", "parameters": {"type": "object", "properties": {}}}}]
    await provider.chat([{"role": "user", "content": "hi"}], tools=tools)

    payload = provider._http_client.post.call_args.kwargs["json"]
    declarations = payload["tools"][0]["functionDeclarations"]
    assert declarations[0]["name"] == "save"


# ── Destructive execution is deterministic ──


@pytest.mark.asyncio
async def test_delete_tool_executes_with_explicit_count():
    from backend.ai.tools.context import ToolContext
    from backend.ai.tools.delete import DeleteTool
    from backend.ai.tools.executor import ToolExecutor
    from backend.ai.tools.registry import ToolRegistry

    class FakeTelegram:
        client = object()

    ctx = ToolContext(telegram=FakeTelegram(), owner_id=1, tz_str="UTC", extra={"chat_id": -100})
    registry = ToolRegistry()
    registry.register(DeleteTool(ctx))
    executor = ToolExecutor(registry, ctx)

    with patch("backend.services.delete_service.do_del_last_n_real", AsyncMock(return_value=(3, 3, None))):
        results = await executor.execute_calls([{"name": "delete", "arguments": {"count": 3}}], owner_id=1)

    assert results[0].success is True
    assert results[0].needs_confirmation is False
    assert results[0].data["count"] == 3


@pytest.mark.asyncio
async def test_delete_tool_fails_safely_without_explicit_count():
    from backend.ai.tools.context import ToolContext
    from backend.ai.tools.delete import DeleteTool

    class FakeTelegram:
        client = None

    ctx = ToolContext(telegram=FakeTelegram(), owner_id=1, tz_str="UTC", extra={"chat_id": -100})
    result = await DeleteTool(ctx).execute(ctx, {})
    assert result.success is False
    assert "explicit count" in result.message


@pytest.mark.asyncio
async def test_delete_tool_fails_safely_on_out_of_range_count():
    from backend.ai.tools.context import ToolContext
    from backend.ai.tools.delete import DeleteTool

    class FakeTelegram:
        client = None

    ctx = ToolContext(telegram=FakeTelegram(), owner_id=1, tz_str="UTC", extra={"chat_id": -100})
    result = await DeleteTool(ctx).execute(ctx, {"count": 9999})
    assert result.success is False
    assert "1 and 500" in result.message


def test_default_trigger_word_is_nova():
    from backend.ai.config_store import _DEFAULTS, match_trigger

    assert _DEFAULTS["trigger_en"] == "Nova"
    assert match_trigger("Nova", "Nova", "") is True
    assert match_trigger("nova", "Nova", "") is True
