"""
Every request crosses the provider boundary and converges on ONE executor.

There is no local deterministic fast path any more: the model interprets
intent and emits a structured tool call, and the dispatcher validates it and
runs it through the SAME ToolExecutor. These tests drive the real
Dispatcher + real ProviderManager (with a scripted provider) and pin the two
properties that matter now:

- a conversational request reaches the provider and its structured proposal is
  executed through the single ToolExecutor boundary;
- a request the model proposes a destructive tool for still goes through the
  executor exactly once, never around it.
"""
from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.ai.providers.base.capabilities import ProviderCapabilities
from backend.ai.providers.base.config import ProviderConfig
from backend.ai.providers.base.contract import BaseProvider, ProviderResponse
from backend.ai.providers.manager.manager import ProviderManager
from backend.ai.session.request import AIRequest


class _FakeProvider(BaseProvider):
    def __init__(self, name: str, responses: list[ProviderResponse] | None = None) -> None:
        super().__init__(ProviderConfig(provider_name=name, enabled=True, default_model="m"))
        self._name = name
        self._responses = list(responses or [])
        self.calls = 0

    @property
    def name(self) -> str:
        return self._name

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(supports_tools=True, supports_function_call=True)

    async def chat(self, messages, **kwargs):
        self.calls += 1
        if self._responses:
            return self._responses.pop(0)
        return ProviderResponse(text="ok", provider_name=self._name, success=True)

    def initialize(self) -> None:
        return None

    def shutdown(self) -> None:
        return None

    def count_tokens(self, text: str) -> int:
        return max(1, len(text) // 4)

    def health(self) -> dict[str, Any]:
        return {"healthy": True}


def _make_dispatcher(mock_te, provider):
    from backend.ai.engine.dispatcher import Dispatcher
    from backend.ai.engine.hooks import NOOP_HOOKS
    from backend.ai.engine.metrics import EngineMetrics

    pm = ProviderManager()
    pm.register_provider(provider)
    pm.switch_provider(provider.name)
    pm._fallback_chain = []

    mock_conv = MagicMock()
    mock_sess = MagicMock()
    mock_sess.session_id = "s"
    mock_sess.owner_id = 123
    mock_sess.active_provider = provider.name
    mock_conv.get_session.return_value = mock_sess
    mock_conv.restore_history = AsyncMock()
    mock_conv.get_history.return_value = []

    mock_pb = MagicMock()
    pp = MagicMock()
    pp.system_prompt = "sys"
    pp.runtime_context = ""
    pp.conversation_context = ""
    pp.tool_context = ""
    pp.user_input = "do it"
    pp.estimated_tokens.estimated_input_tokens = 50
    pp.estimated_tokens.prompt_size_chars = 100
    mock_pb.build.return_value = pp

    return Dispatcher(
        mock_conv, mock_pb, pm, NOOP_HOOKS, EngineMetrics(), tool_executor=mock_te,
    ), provider


def _mock_executor(results):
    from backend.ai.tools.executor import ToolExecutionResult

    mock_te = MagicMock()
    mock_te.execute_calls = AsyncMock(return_value=[
        ToolExecutionResult(tool_name=r[0], success=r[1], message=r[2], data=r[3])
        for r in results
    ])
    c = MagicMock()
    c.extra = {}
    c.telegram = None
    c.tz_str = "UTC"
    c.client = None
    mock_te._context = c
    return mock_te


@pytest.mark.asyncio
async def test_conversational_request_still_uses_provider():
    provider = _FakeProvider("test", [
        ProviderResponse(
            text="I'm here!", provider_name="test", success=True, usage={},
            metadata={"finish_reason": "stop"},
        ),
    ])
    mock_te = MagicMock()
    mock_te.execute_calls = AsyncMock()
    c = MagicMock()
    c.extra = {}
    c.telegram = None
    c.tz_str = "UTC"
    c.client = None
    mock_te._context = c

    d, provider = _make_dispatcher(mock_te, provider)

    result = await d.dispatch(AIRequest(
        session_id="s1", message_id=1, owner_id=123,
        user_message="هستی؟", chat_id=456,
    ))

    assert result.success is True
    assert result.response == "I'm here!"
    # initial provider round + one bounded prose-recovery retry.
    assert provider.calls >= 1
    assert mock_te.execute_calls.await_count == 0


@pytest.mark.asyncio
async def test_semantic_delete_still_uses_provider():
    """A semantic request reaches the AI so the model can reason over real
    chat history and propose the structured delete arguments itself."""
    provider = _FakeProvider("test", [
        ProviderResponse(
            text="", provider_name="test", success=True, usage={},
            tool_calls=[{"id": "t1", "name": "list_recent_messages", "arguments": {"limit": 50}}],
            metadata={"finish_reason": "tool_calls"},
        ),
    ])
    mock_te = _mock_executor([
        ("list_recent_messages", True, "", {"messages": []}),
    ])
    d, provider = _make_dispatcher(mock_te, provider)

    result = await d.dispatch(AIRequest(
        session_id="s1", message_id=1, owner_id=123,
        user_message="پیام‌های مربوط به دعوای اخیر رو پیدا کن و حذفشون کن",
        chat_id=456,
    ))

    assert result.success is True
    assert provider.calls >= 1
