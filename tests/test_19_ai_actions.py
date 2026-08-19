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
    parse_command_intent,
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
    from backend.ai.conversation.context_builder import ReplyContext
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
        reply_context=ReplyContext(exists=True, message_id=789, chat_id=456, sender_id=123),
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


# ── Deterministic command intent (model-prose safety net) ──


def test_deterministic_persian_save():
    r = parse_command_intent("این پیام رو سیو کن", has_reply=True)
    assert r.kind == KIND_EXECUTABLE
    assert r.action == "save"
    assert r.tool_calls == [{"name": "save", "arguments": {}}]


def test_deterministic_persian_deep_save():
    r = parse_command_intent("اینو عمیق ذخیره کن", has_reply=True)
    assert r.kind == KIND_EXECUTABLE
    assert r.action == "deep_save"
    assert r.tool_calls == [{"name": "save", "arguments": {}}]


def test_deterministic_delete_replied():
    r = parse_command_intent("این پیام رو پاک کن", has_reply=True)
    assert r.kind == KIND_EXECUTABLE
    assert r.action == "delete_messages"
    assert r.tool_calls == [{"name": "delete_replied", "arguments": {}}]


def test_deterministic_delete_last_message():
    r = parse_command_intent("پیام آخر رو پاک کن", has_reply=False)
    assert r.kind == KIND_EXECUTABLE
    assert r.tool_calls == [{"name": "delete", "arguments": {"count": 1}}]


def test_deterministic_delete_n_persian_digits():
    r = parse_command_intent("۱۰ پیام آخر رو پاک کن", has_reply=False)
    assert r.kind == KIND_EXECUTABLE
    assert r.count == 10
    assert r.tool_calls == [{"name": "delete", "arguments": {"count": 10}}]


def test_deterministic_delete_n_no_last():
    r = parse_command_intent("۳ پیام رو پاک کن", has_reply=False)
    assert r.kind == KIND_EXECUTABLE
    assert r.tool_calls == [{"name": "delete", "arguments": {"count": 3}}]


def test_deterministic_negation_save_not_delete():
    r = parse_command_intent("اینو پاک نکن فقط سیوش کن", has_reply=True)
    assert r.kind == KIND_EXECUTABLE
    assert r.action == "save"
    assert r.tool_calls == [{"name": "save", "arguments": {}}]


def test_deterministic_mixed_english_delete():
    r = parse_command_intent("delete this message", has_reply=True)
    assert r.kind == KIND_EXECUTABLE
    assert r.tool_calls == [{"name": "delete_replied", "arguments": {}}]


def test_deterministic_mixed_english_deep_save():
    r = parse_command_intent("save this message with deep mode", has_reply=True)
    assert r.kind == KIND_EXECUTABLE
    assert r.action == "deep_save"
    assert r.tool_calls == [{"name": "save", "arguments": {}}]


def test_deterministic_english_delete_last_n():
    r = parse_command_intent("delete last 10 messages", has_reply=False)
    assert r.kind == KIND_EXECUTABLE
    assert r.tool_calls == [{"name": "delete", "arguments": {"count": 10}}]


def test_deterministic_ambiguous_delete_clarifies():
    r = parse_command_intent("پاک کن", has_reply=False)
    assert r.kind == KIND_CLARIFY


def test_deterministic_save_without_reply_clarifies():
    r = parse_command_intent("اینو سیو کن", has_reply=False)
    assert r.kind == KIND_CLARIFY


def test_deterministic_conversational_is_not_action():
    r = parse_command_intent("امروز چه خبر؟", has_reply=False)
    assert r.kind == KIND_CONVERSATIONAL


def test_deterministic_question_not_action():
    r = parse_command_intent("what does save mean?", has_reply=False)
    assert r.kind == KIND_CONVERSATIONAL


def test_deterministic_send_unsupported():
    r = parse_command_intent("اینو برای علی بفرست", has_reply=True)
    assert r.kind == KIND_UNSUPPORTED
    assert r.action == "send"


def test_deterministic_negation_only_is_conversational():
    r = parse_command_intent("اینو پاک نکن", has_reply=True)
    assert r.kind == KIND_CONVERSATIONAL


# ── Dispatcher: deterministic bridge turns model prose into execution ──


@pytest.mark.asyncio
async def test_dispatcher_deterministic_bridge_over_model_prose():
    from backend.ai.conversation.context_builder import ReplyContext
    from backend.ai.providers.base.contract import ProviderResponse
    from backend.ai.session.request import AIRequest
    from backend.ai.tools.executor import ToolExecutionResult

    mock_te = MagicMock()
    mock_te.execute_calls = AsyncMock(return_value=[
        ToolExecutionResult(
            tool_name="delete_replied",
            success=True,
            message="Deleted the replied message.",
            data={"count": 1},
        ),
    ])
    mock_te._context = MagicMock()
    mock_te._context.extra = {}
    mock_te._context.telegram = None
    mock_te._context.tz_str = "UTC"
    mock_te._context.client = None

    # The production failure: the model returns PROSE (no JSON, no tool call).
    provider_response = ProviderResponse(
        text="Sure! Which message would you like me to delete?",
        provider_name="test",
        success=True,
        usage={},
    )
    d, _ = _make_dispatcher(mock_te, provider_response)

    result = await d.dispatch(AIRequest(
        session_id="s1",
        message_id=1,
        owner_id=123,
        user_message="اینو پاک کن",
        chat_id=456,
        reply_context=ReplyContext(exists=True, message_id=789, chat_id=456, sender_id=123),
    ))

    called = mock_te.execute_calls.call_args.args[0]
    assert called == [{"name": "delete_replied", "arguments": {}}]
    assert "Deleted the replied message" in result.response
    assert result.metadata.get("ai_action", {}).get("action") == "delete_messages"


@pytest.mark.asyncio
async def test_dispatcher_deterministic_bridge_delete_last_n():
    from backend.ai.providers.base.contract import ProviderResponse
    from backend.ai.session.request import AIRequest
    from backend.ai.tools.executor import ToolExecutionResult

    mock_te = MagicMock()
    mock_te.execute_calls = AsyncMock(return_value=[
        ToolExecutionResult(
            tool_name="delete",
            success=True,
            message="Deleted 3 outgoing message(s).",
            data={"count": 3},
        ),
    ])
    mock_te._context = MagicMock()
    mock_te._context.extra = {}
    mock_te._context.telegram = None
    mock_te._context.tz_str = "UTC"
    mock_te._context.client = None

    provider_response = ProviderResponse(
        text="Please provide the message ID.",
        provider_name="test",
        success=True,
        usage={},
    )
    d, _ = _make_dispatcher(mock_te, provider_response)

    result = await d.dispatch(AIRequest(
        session_id="s1",
        message_id=1,
        owner_id=123,
        user_message="۳ پیام آخر رو پاک کن",
        chat_id=456,
    ))

    called = mock_te.execute_calls.call_args.args[0]
    assert called == [{"name": "delete", "arguments": {"count": 3}}]
    assert "Deleted 3" in result.response


@pytest.mark.asyncio
async def test_dispatcher_deterministic_bridge_save():
    from backend.ai.conversation.context_builder import ReplyContext
    from backend.ai.providers.base.contract import ProviderResponse
    from backend.ai.session.request import AIRequest
    from backend.ai.tools.executor import ToolExecutionResult

    mock_te = MagicMock()
    mock_te.execute_calls = AsyncMock(return_value=[
        ToolExecutionResult(
            tool_name="save",
            success=True,
            message="Saved as S0001.",
            data={"save_code": "S0001"},
        ),
    ])
    mock_te._context = MagicMock()
    mock_te._context.extra = {}
    mock_te._context.telegram = None
    mock_te._context.tz_str = "UTC"
    mock_te._context.client = None

    provider_response = ProviderResponse(
        text="Sure, I can help you save that message.",
        provider_name="test",
        success=True,
        usage={},
    )
    d, _ = _make_dispatcher(mock_te, provider_response)

    result = await d.dispatch(AIRequest(
        session_id="s1",
        message_id=1,
        owner_id=123,
        user_message="اینو سیو کن",
        chat_id=456,
        reply_context=ReplyContext(exists=True, message_id=789, chat_id=456, sender_id=123),
    ))

    called = mock_te.execute_calls.call_args.args[0]
    assert called == [{"name": "save", "arguments": {}}]
    assert "S0001" in result.response
