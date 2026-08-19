"""
Focused tests for the structured AI action layer: parse → validate → resolve
→ existing executor, with strict local rejection and no false success.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.ai.actions import (
    KIND_CLARIFY,
    KIND_CONVERSATIONAL,
    KIND_EXECUTABLE,
    KIND_INVALID,
    KIND_UNSUPPORTED,
    parse_action_text,
)


# ── Parse + validate + resolve (the deterministic layer) ──


def test_persian_direct_save():
    # Model output for "نوا این پیام رو سیو کن"
    r = parse_action_text('{"action": "save", "target": "replied_message"}')
    assert r.kind == KIND_EXECUTABLE
    assert r.tool_calls == [{"name": "save", "arguments": {}}]


def test_persian_deep_save_with_caption():
    # "نوا اینو عمیق ذخیره کن و کپشنش رو هم نگه دار"
    r = parse_action_text('{"action": "deep_save", "target": "replied_message", "caption": true}')
    assert r.kind == KIND_EXECUTABLE
    assert r.caption is True
    # Deep save is the only save mode; it always preserves captions via
    # the existing execute_save() pipeline.
    assert r.tool_calls == [{"name": "save", "arguments": {}}]


def test_delete_last_message():
    # "نوا این پیام آخر رو پاک کن"
    r = parse_action_text('{"action": "delete_messages", "target": "last_message"}')
    assert r.kind == KIND_EXECUTABLE
    assert r.tool_calls == [{"name": "delete", "arguments": {"count": 1}}]


def test_delete_n_persian_digits():
    # "نوا ۱۰ پیام آخر رو پاک کن" — Persian digits are normalized.
    r = parse_action_text('{"action": "delete_messages", "target": "recent_messages", "count": "۱۰"}')
    assert r.kind == KIND_EXECUTABLE
    assert r.count == 10
    assert r.tool_calls == [{"name": "delete", "arguments": {"count": 10}}]


def test_delete_n_english_digits_mixed():
    # "Nava delete last 10 messages"
    r = parse_action_text('{"action": "delete_messages", "target": "recent_messages", "count": 10}')
    assert r.kind == KIND_EXECUTABLE
    assert r.tool_calls == [{"name": "delete", "arguments": {"count": 10}}]


def test_delete_replied_message():
    r = parse_action_text('{"action": "delete_messages", "target": "replied_message"}')
    assert r.kind == KIND_EXECUTABLE
    assert r.tool_calls == [{"name": "delete_replied", "arguments": {}}]


def test_ambiguous_delete_returns_clarify():
    r = parse_action_text('{"action": "delete_messages"}')
    assert r.kind == KIND_CLARIFY


def test_explicit_clarify_action():
    r = parse_action_text('{"action": "clarify", "reason": "which message?"}')
    assert r.kind == KIND_CLARIFY
    assert r.reason == "which message?"


def test_unknown_action_rejected():
    r = parse_action_text('{"action": "hack_telegram"}')
    assert r.kind == KIND_INVALID
    assert "Unknown action" in r.error


def test_unknown_field_rejected():
    r = parse_action_text('{"action": "save", "evil": true}')
    assert r.kind == KIND_INVALID
    assert "Unknown field" in r.error


def test_invalid_count_rejected():
    import json
    for bad in (-5, 0, 501, "abc"):
        text = json.dumps({"action": "delete_messages", "target": "recent_messages", "count": bad})
        r = parse_action_text(text)
        assert r.kind == KIND_INVALID, bad


def test_unsupported_action():
    r = parse_action_text('{"action": "remember", "content": "x"}')
    assert r.kind == KIND_UNSUPPORTED
    assert r.action == "remember"


def test_conversational_prose_is_not_an_action():
    r = parse_action_text("The weather looks fine today.")
    assert r.kind == KIND_CONVERSATIONAL
    assert r.tool_calls == []


def test_markdown_json_fence_is_parsed():
    text = '```json\n{"action": "save", "target": "replied_message"}\n```'
    r = parse_action_text(text)
    assert r.kind == KIND_EXECUTABLE
    assert r.tool_calls == [{"name": "save", "arguments": {}}]


def test_embedded_json_in_prose_is_parsed():
    text = 'Sure — here it is: {"action": "delete_messages", "target": "recent_messages", "count": 3}'
    r = parse_action_text(text)
    assert r.kind == KIND_EXECUTABLE
    assert r.tool_calls == [{"name": "delete", "arguments": {"count": 3}}]


# ── Dispatcher: executable intent actually executes (no false success) ──


def _make_dispatcher(mock_te, provider_response):
    from backend.ai.engine.dispatcher import Dispatcher
    from backend.ai.engine.hooks import NOOP_HOOKS
    from backend.ai.engine.metrics import EngineMetrics
    from backend.ai.providers.base.contract import ProviderResponse

    mock_pm = MagicMock()
    mock_pm.get_active_name.return_value = "test"
    mock_pm.get_active.return_value.config.model = "m"
    mock_pm.get_active.return_value.health.return_value = {"healthy": True}
    mock_pm.get_active.return_value.chat = AsyncMock(return_value=provider_response)

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

    return Dispatcher(mock_conv, mock_pb, mock_pm, NOOP_HOOKS, EngineMetrics(), tool_executor=mock_te), mock_pm


@pytest.mark.asyncio
async def test_dispatcher_executes_structured_action():
    from backend.ai.engine.result import EngineResult
    from backend.ai.providers.base.contract import ProviderResponse
    from backend.ai.session.request import AIRequest
    from backend.ai.tools.executor import ToolExecutionResult

    mock_te = MagicMock()
    mock_te.execute_calls = AsyncMock(return_value=[
        ToolExecutionResult(tool_name="delete", success=True, message="Deleted 3 outgoing message(s).", data={"count": 3}),
    ])
    mock_te._context = MagicMock()
    mock_te._context.extra = {}
    mock_te._context.telegram = None
    mock_te._context.tz_str = "UTC"
    mock_te._context.client = None

    provider_response = ProviderResponse(
        text='{"action": "delete_messages", "target": "recent_messages", "count": 3}',
        provider_name="test",
        success=True,
        usage={},
    )
    d, mock_pm = _make_dispatcher(mock_te, provider_response)

    result = await d.dispatch(AIRequest(
        session_id="s1", message_id=1, owner_id=123, user_message="delete 3", chat_id=456,
    ))

    assert result.success is True
    # The resolved action reached the executor as the delete tool.
    called = mock_te.execute_calls.call_args.args[0]
    assert called == [{"name": "delete", "arguments": {"count": 3}}]
    # The response reflects the REAL tool result.
    assert "Deleted 3" in result.response
    assert result.metadata.get("ai_action", {}).get("action") == "delete_messages"


@pytest.mark.asyncio
async def test_dispatcher_reports_executor_failure_no_false_success():
    from backend.ai.providers.base.contract import ProviderResponse
    from backend.ai.session.request import AIRequest
    from backend.ai.tools.executor import ToolExecutionResult

    mock_te = MagicMock()
    mock_te.execute_calls = AsyncMock(return_value=[
        ToolExecutionResult(tool_name="save", success=False, message="Save failed: timeout", data={}),
    ])
    mock_te._context = MagicMock()
    mock_te._context.extra = {}
    mock_te._context.telegram = None
    mock_te._context.tz_str = "UTC"
    mock_te._context.client = None

    provider_response = ProviderResponse(
        text='{"action": "save", "target": "replied_message"}',
        provider_name="test",
        success=True,
        usage={},
    )
    d, _ = _make_dispatcher(mock_te, provider_response)

    result = await d.dispatch(AIRequest(
        session_id="s1", message_id=1, owner_id=123, user_message="save this", chat_id=456,
    ))

    # The pipeline did not crash, and it did NOT claim success.
    assert "Save failed: timeout" in result.response
    assert "Saved" not in result.response


@pytest.mark.asyncio
async def test_dispatcher_rejects_unsupported_action():
    from backend.ai.providers.base.contract import ProviderResponse
    from backend.ai.session.request import AIRequest

    mock_te = MagicMock()
    mock_te._context = MagicMock()
    mock_te._context.extra = {}

    provider_response = ProviderResponse(
        text='{"action": "remember", "content": "x"}',
        provider_name="test",
        success=True,
        usage={},
    )
    d, _ = _make_dispatcher(mock_te, provider_response)

    result = await d.dispatch(AIRequest(
        session_id="s1", message_id=1, owner_id=123, user_message="remember x", chat_id=456,
    ))

    assert "Unsupported action" in result.response
    mock_te.execute_calls.assert_not_called()


@pytest.mark.asyncio
async def test_dispatcher_conversational_is_unchanged():
    from backend.ai.providers.base.contract import ProviderResponse
    from backend.ai.session.request import AIRequest

    mock_te = MagicMock()
    mock_te._context = MagicMock()
    mock_te._context.extra = {}

    provider_response = ProviderResponse(
        text="What's the weather like?",
        provider_name="test",
        success=True,
        usage={},
    )
    d, _ = _make_dispatcher(mock_te, provider_response)

    result = await d.dispatch(AIRequest(
        session_id="s1", message_id=1, owner_id=123, user_message="hi", chat_id=456,
    ))

    assert result.response == "What's the weather like?"
    mock_te.execute_calls.assert_not_called()
