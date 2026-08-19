"""
Focused tests for the dispatcher's LOCAL deterministic fast path.

This is the production reliability guarantee: high-confidence command
intents (status queries, last-N delete, save/delete by reply) execute
through the SAME ToolExecutor WITHOUT a provider round, so they keep
working when every AI provider is rate-limited, misconfigured, or down.

These tests prove the parse → fast-path → executor boundary against a REAL
ProviderManager (with a fake provider), not just the action parser alone:

- a status command executes its tool and returns the REAL result without
  ever calling the provider;
- a conversational request still goes through the provider unchanged;
- a destructive last-N delete executes exactly once via the fast path;
- a semantic request still reaches the provider (it is not a high-
  confidence command).
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
async def test_username_status_runs_fast_path_without_provider():
    mock_te = _mock_executor([
        ("account_show", True, "👤 First Name: Ali", {"first_name": "Ali"}),
    ])
    provider = _FakeProvider("test")
    d, provider = _make_dispatcher(mock_te, provider)

    result = await d.dispatch(AIRequest(
        session_id="s1", message_id=1, owner_id=123,
        user_message="وضعیت یوزرنیمم رو بگو", chat_id=456,
    ))

    assert result.success is True
    assert result.metadata["finish_state"] == "local_fast_path"
    # Casual Persian "یوزرنیم" resolves to the account FIRST NAME.
    assert "First Name: Ali" in result.response
    tool_calls = mock_te.execute_calls.call_args.args[0]
    assert tool_calls == [{"name": "account_show", "arguments": {"fields": ["first_name"]}}]
    assert mock_te.execute_calls.await_count == 1
    # The provider was NEVER called — the fast path is provider-independent.
    assert provider.calls == 0


@pytest.mark.asyncio
async def test_delete_last_n_runs_fast_path_exactly_once():
    mock_te = _mock_executor([
        ("delete", True, "Deleted 3 outgoing message(s).", {"count": 3}),
    ])
    provider = _FakeProvider("test")
    d, provider = _make_dispatcher(mock_te, provider)

    result = await d.dispatch(AIRequest(
        session_id="s1", message_id=1, owner_id=123,
        user_message="سه پیام آخر رو پاک کن", chat_id=456,
    ))

    assert result.success is True
    assert mock_te.execute_calls.await_count == 1
    tool_calls = mock_te.execute_calls.call_args.args[0]
    assert tool_calls == [{"name": "delete", "arguments": {"count": 3}}]
    assert provider.calls == 0


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
    """A semantic request is NOT a high-confidence command — it must still
    reach the AI so the model can reason over real chat history."""
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
