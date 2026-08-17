"""
Focused tests for AI tool-call parsing, continuation, and token preservation.
"""
from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

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
    first = ProviderResponse(text="", provider_name="test", success=True,
        tool_calls=[{"id": "c1", "name": "search", "arguments": {"q": "t"}}],
        usage={"prompt_tokens": 50, "completion_tokens": 10, "total_tokens": 60},
        metadata={"finish_reason": "tool_calls"})
    final = ProviderResponse(text="Found 3 items.", provider_name="test", success=True,
        usage={"prompt_tokens": 70, "completion_tokens": 25, "total_tokens": 95},
        metadata={"finish_reason": "stop"})
    mock_pm.chat = AsyncMock(side_effect=[first, final])

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
    result = await d.dispatch(AIRequest(owner_id=123, user_message="Search", chat_id=456))

    assert result.success is True
    assert result.response == "Found 3 items."
    assert mock_pm.chat.call_count == 2
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
    first = ProviderResponse(text="", provider_name="test", success=True,
        tool_calls=[{"id": "c1", "name": "bad_tool", "arguments": {}}],
        usage={"prompt_tokens": 50, "completion_tokens": 10, "total_tokens": 60})
    final = ProviderResponse(text="Can't find that tool.", provider_name="test", success=True,
        usage={"prompt_tokens": 60, "completion_tokens": 15, "total_tokens": 75})
    mock_pm.chat = AsyncMock(side_effect=[first, final])

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
    result = await d.dispatch(AIRequest(owner_id=123, user_message="Run", chat_id=456))

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
    resp = ProviderResponse(text="Hi", provider_name="test", success=True,
        usage={"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
        metadata={"finish_reason": "stop"})
    mock_pm.chat = AsyncMock(return_value=resp)

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
    result = await d.dispatch(AIRequest(owner_id=123, user_message="Hi", chat_id=456))

    assert result.total_tokens == 150
    assert result.prompt_tokens == 100
    assert result.completion_tokens == 50


@pytest.mark.asyncio
async def test_provider_failure_error():
    from backend.ai.engine.dispatcher import Dispatcher
    from backend.ai.engine.hooks import NOOP_HOOKS
    from backend.ai.engine.metrics import EngineMetrics
    from backend.ai.session.request import AIRequest

    mock_pm = MagicMock()
    mock_pm.get_active_name.return_value = "test"
    mock_pm.get_active.return_value.config.model = "m"
    resp = ProviderResponse(text="Rate limited.", provider_name="test", success=False,
        metadata={"http_status": 429})
    mock_pm.chat = AsyncMock(return_value=resp)

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
    pp.estimated_tokens.estimated_input_tokens = 20
    pp.estimated_tokens.prompt_size_chars = 50
    mock_pb.build.return_value = pp

    d = Dispatcher(mock_conv, mock_pb, mock_pm, NOOP_HOOKS, EngineMetrics())
    result = await d.dispatch(AIRequest(owner_id=123, user_message="Hi", chat_id=456))

    assert result.success is False
    assert "Rate limited" in result.errors[0]


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
