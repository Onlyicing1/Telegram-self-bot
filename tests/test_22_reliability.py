"""
Focused tests for the AI execution reliability gateway:

- ``list_recent_messages`` is expressible as a JSON action.
- A prose provider response with no resolvable action is recovered by exactly
  ONE bounded format-enforcement retry (no tool double-execution).
- If the recovery still returns prose, the ORIGINAL prose is kept (no nudge
  artifacts) and no tool runs.
- Model-not-found is surfaced with the provider detail instead of being
  collapsed into a generic message.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.ai.actions import (
    KIND_EXECUTABLE,
    KIND_INVALID,
    parse_action_text,
)


# ── list_recent_messages JSON action ──


def test_json_list_recent_messages_with_count():
    r = parse_action_text('{"action": "list_recent_messages", "count": 10}')
    assert r.kind == KIND_EXECUTABLE
    assert r.tool_calls == [{"name": "list_recent_messages", "arguments": {"limit": 10}}]


def test_json_list_recent_messages_default_limit():
    r = parse_action_text('{"action": "list_recent_messages"}')
    assert r.kind == KIND_EXECUTABLE
    assert r.tool_calls == [{"name": "list_recent_messages", "arguments": {}}]


def test_json_list_recent_messages_invalid_count():
    r = parse_action_text('{"action": "list_recent_messages", "count": -1}')
    assert r.kind == KIND_INVALID


# ── Bounded recovery retry ──


def _make_dispatcher(mock_te, provider_responses):
    from backend.ai.engine.dispatcher import Dispatcher
    from backend.ai.engine.hooks import NOOP_HOOKS
    from backend.ai.engine.metrics import EngineMetrics

    mock_pm = MagicMock()
    mock_pm.get_active_name.return_value = "test"
    mock_pm.get_active.return_value.config.model = "m"
    mock_pm.get_active.return_value.health.return_value = {"healthy": True}
    mock_pm.get_active.return_value.chat = AsyncMock(side_effect=provider_responses)

    mock_conv = MagicMock()
    mock_sess = MagicMock()
    mock_sess.session_id = "s"
    mock_sess.owner_id = 123
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
    pp.user_input = "do it"
    pp.estimated_tokens.estimated_input_tokens = 50
    pp.estimated_tokens.prompt_size_chars = 100
    mock_pb.build.return_value = pp

    return Dispatcher(
        mock_conv, mock_pb, mock_pm, NOOP_HOOKS, EngineMetrics(), tool_executor=mock_te,
    ), mock_pm


@pytest.mark.asyncio
async def test_prose_recovers_to_native_tool_via_bounded_retry():
    from backend.ai.providers.base.contract import ProviderResponse
    from backend.ai.session.request import AIRequest
    from backend.ai.tools.executor import ToolExecutionResult

    prose = ProviderResponse(
        text="I can look that up for you.",
        provider_name="test", success=True, usage={},
        metadata={"finish_reason": "stop"},
    )
    tool_response = ProviderResponse(
        text="", provider_name="test", success=True, usage={},
        tool_calls=[{"id": "t1", "name": "username_show", "arguments": {}}],
        metadata={"finish_reason": "tool_calls"},
    )

    mock_te = MagicMock()
    mock_te.execute_calls = AsyncMock(return_value=[
        ToolExecutionResult(tool_name="username_show", success=True, message="Username: @nova", data={}),
    ])
    mock_te._context = MagicMock()
    mock_te._context.extra = {}
    mock_te._context.telegram = None
    mock_te._context.tz_str = "UTC"
    mock_te._context.client = None

    d, mock_pm = _make_dispatcher(mock_te, [prose, tool_response])

    result = await d.dispatch(AIRequest(
        session_id="s1", message_id=1, owner_id=123,
        user_message="وضعیت first name اکانتم رو بگو", chat_id=456,
    ))

    assert result.success is True
    # initial prose + one recovery retry (native tool call) = 2 provider calls.
    assert mock_pm.get_active.return_value.chat.await_count == 2
    # The tool executed exactly once.
    assert mock_te.execute_calls.await_count == 1
    assert "Username: @nova" in result.response


@pytest.mark.asyncio
async def test_prose_recovery_falls_back_to_original_prose():
    from backend.ai.providers.base.contract import ProviderResponse
    from backend.ai.session.request import AIRequest

    original = ProviderResponse(
        text="I can help with that.",
        provider_name="test", success=True, usage={},
        metadata={"finish_reason": "stop"},
    )
    still_prose = ProviderResponse(
        text="Here is some helpful information about your account.",
        provider_name="test", success=True, usage={},
        metadata={"finish_reason": "stop"},
    )

    mock_te = MagicMock()
    mock_te.execute_calls = AsyncMock()
    mock_te._context = MagicMock()
    mock_te._context.extra = {}

    d, mock_pm = _make_dispatcher(mock_te, [original, still_prose])

    result = await d.dispatch(AIRequest(
        session_id="s1", message_id=1, owner_id=123,
        user_message="وضعیت first name اکانتم رو بگو", chat_id=456,
    ))

    assert result.success is True
    assert mock_pm.get_active.return_value.chat.await_count == 2
    # No tool executed — the request stayed conversational.
    assert mock_te.execute_calls.await_count == 0
    # The ORIGINAL prose is kept; the nudge artifact is never surfaced.
    assert result.response == "I can help with that."


def test_action_nudge_is_appended_as_user_turn():
    from backend.ai.engine.dispatcher import Dispatcher

    messages = [{"role": "system", "content": "sys"}]
    out = Dispatcher._append_action_nudge(messages)
    assert len(out) == 2
    assert out[0] == messages[0]
    assert out[1]["role"] == "user"
    assert "tool call" in out[1]["content"]


# ── Model-not-found surfacing ──


def test_humanize_model_not_found_includes_detail():
    from backend.bot.handlers.ai_unified import _humanize_error

    msg = _humanize_error(
        "All AI providers failed. Last error: groq: success=False "
        "(API error (404): Model 'openai/gpt-oss-120b' not found)"
    )
    assert "Model not found" in msg
    assert "not found" in msg


def test_humanize_rate_limit_not_misclassified_as_model():
    from backend.bot.handlers.ai_unified import _humanize_error

    msg = _humanize_error("Rate limited. Try again in 5s.")
    assert "Rate limited" in msg
    assert "Model not found" not in msg
