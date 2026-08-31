"""
Focused regression tests: immediate (non-scheduled) text-write execution.

The scheduled path already executes through the registered ``send_message``
tool. This suite proves the IMMEDIATE path reuses that SAME registered
execution boundary instead of remaining recognized-but-unwired:

    "بنویس سلام" / "write hello"
      -> deterministic intent detection (parse_command_intent)
      -> executable send action -> [{"name": "send_message", "arguments": {"text": ...}}]
      -> Dispatcher fast path -> ToolExecutor -> SendMessageTool
      -> TelegramAPI.send_message(owner_id, text)  (trusted owner destination)

Recipient/reference/forward sends ("اینو برای علی بفرست") stay unsupported:
the architecture never lets the model choose a destination. Scheduling
detection and historical deletes are unchanged.
"""
from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.ai.actions import (
    KIND_CONVERSATIONAL,
    KIND_EXECUTABLE,
    KIND_INVALID,
    KIND_UNSUPPORTED,
    parse_command_intent,
    validate_action,
)


# ── Deterministic intent recognition ──


def test_persian_write_hello_is_immediate_send():
    r = parse_command_intent("بنویس سلام", has_reply=False)
    assert r.kind == KIND_EXECUTABLE
    assert r.action == "send"
    assert r.tool_calls == [{"name": "send_message", "arguments": {"text": "سلام"}}]


def test_english_write_hello_is_immediate_send():
    r = parse_command_intent("write hello", has_reply=False)
    assert r.kind == KIND_EXECUTABLE
    assert r.action == "send"
    assert r.tool_calls == [{"name": "send_message", "arguments": {"text": "hello"}}]


def test_recipient_send_stays_unsupported():
    r = parse_command_intent("اینو برای علی بفرست", has_reply=True)
    assert r.kind == KIND_UNSUPPORTED
    assert r.action == "send"


def test_write_with_recipient_is_not_an_immediate_write():
    r = parse_command_intent("بنویس برای علی سلام", has_reply=False)
    assert r.kind == KIND_UNSUPPORTED
    assert r.action == "send"


def test_write_with_reference_is_not_an_immediate_write():
    r = parse_command_intent("اینو بنویس", has_reply=True)
    assert r.kind == KIND_UNSUPPORTED
    assert r.action == "send"


def test_desire_marker_is_not_a_deterministic_write():
    r = parse_command_intent("میخوام بنویسم یه مطلب", has_reply=False)
    assert r.kind == KIND_CONVERSATIONAL


def test_future_time_write_is_not_an_immediate_write():
    r = parse_command_intent("write hello tomorrow", has_reply=False)
    assert r.kind != KIND_EXECUTABLE


def test_bare_write_alone_is_ambiguous():
    r = parse_command_intent("بنویس", has_reply=False)
    assert r.kind != KIND_EXECUTABLE


# ── Immediate vs scheduled vs historical separation ──


def test_scheduled_write_still_creates_task():
    r = parse_command_intent("write hello every minute", has_reply=False)
    assert r.kind == KIND_EXECUTABLE
    assert r.action == "create_task"
    assert [tc["name"] for tc in r.tool_calls] == ["create_task"]


def test_historical_delete_is_not_scheduled():
    r = parse_command_intent("پیام های ساعت ۹ دیروز رو پاک کن", has_reply=False)
    assert r.action == "delete_messages"
    assert r.kind == KIND_EXECUTABLE


def test_ordinary_conversation_unchanged():
    r = parse_command_intent("درباره آب و هوا بگو", has_reply=True)
    assert r.kind == KIND_CONVERSATIONAL


# ── Structured action contract (provider JSON path) ──


def test_validate_send_requires_text():
    ok = validate_action({"action": "send", "text": "hello"})
    assert ok.kind == KIND_EXECUTABLE
    assert ok.text == "hello"
    missing = validate_action({"action": "send"})
    assert missing.kind == KIND_INVALID
    empty = validate_action({"action": "send", "content": "   "})
    assert empty.kind == KIND_INVALID


def test_validate_send_accepts_content_alias():
    ok = validate_action({"action": "send", "content": "سلام"})
    assert ok.kind == KIND_EXECUTABLE
    assert ok.text == "سلام"


def test_validate_send_rejects_recipient():
    r = validate_action({"action": "send", "text": "hi", "recipient": "ali"})
    assert r.kind == KIND_INVALID
    assert "recipient" in r.error


# ── Dispatcher fast path reaches SendMessageTool (real registry) ──


class _FakeProvider:
    """Provider that must NOT be called for the deterministic write path."""

    def __init__(self) -> None:
        self.calls = 0

    @property
    def name(self) -> str:
        return "fake"

    @property
    def capabilities(self):
        from backend.ai.providers.base.capabilities import ProviderCapabilities
        return ProviderCapabilities(supports_tools=True, supports_function_call=True)

    async def chat(self, messages, **kwargs):
        self.calls += 1
        from backend.ai.providers.base.contract import ProviderResponse
        return ProviderResponse(text="unexpected", provider_name="fake", success=True)

    def initialize(self) -> None:
        return None

    def shutdown(self) -> None:
        return None

    def count_tokens(self, text: str) -> int:
        return max(1, len(text) // 4)

    def health(self) -> dict[str, Any]:
        return {"healthy": True}


def _make_dispatcher(telegram, provider):
    from backend.ai.engine.dispatcher import Dispatcher
    from backend.ai.engine.hooks import NOOP_HOOKS
    from backend.ai.engine.metrics import EngineMetrics
    from backend.ai.providers.manager.manager import ProviderManager
    from backend.ai.tools.context import ToolContext
    from backend.ai.tools.executor import ToolExecutor
    from backend.ai.tools.registry import create_default_registry

    pm = ProviderManager()
    pm.register_provider(provider)
    pm.switch_provider(provider.name)
    pm._fallback_chain = []
    provider._provider_pm = pm

    mock_conv = MagicMock()
    mock_sess = MagicMock()
    mock_sess.session_id = "s"
    mock_sess.owner_id = 777
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

    ctx = ToolContext(telegram=telegram, owner_id=777, tz_str="UTC", extra={})
    registry = create_default_registry(ctx)
    executor = ToolExecutor(registry, ctx)

    return Dispatcher(
        mock_conv, mock_pb, pm, NOOP_HOOKS, EngineMetrics(),
        tool_registry=registry,
        tool_executor=executor,
    ), registry, executor


@pytest.mark.asyncio
async def test_dispatcher_fast_path_sends_write_hello_to_owner():
    from backend.ai.session.request import AIRequest

    telegram = MagicMock()
    telegram.send_message = AsyncMock(return_value={"id": 1, "text": "hello"})
    provider = _FakeProvider()
    d, registry, executor = _make_dispatcher(telegram, provider)

    result = await d.dispatch(AIRequest(
        session_id="s1", message_id=1, owner_id=777,
        user_message="write hello", chat_id=-1001, timezone="UTC",
    ))

    assert result.success is True
    assert result.metadata["finish_state"] == "local_fast_path"
    assert result.metadata["ai_action"]["action"] == "send"
    # the deterministic write path never burns a provider round
    assert provider.calls == 0
    # the SAME registered tool sends to the TRUSTED owner destination
    telegram.send_message.assert_awaited_once_with(777, "hello")


@pytest.mark.asyncio
async def test_dispatcher_fast_path_persian_write_sends_to_owner():
    from backend.ai.session.request import AIRequest

    telegram = MagicMock()
    telegram.send_message = AsyncMock(return_value={"id": 1, "text": "سلام"})
    provider = _FakeProvider()
    d, registry, executor = _make_dispatcher(telegram, provider)

    result = await d.dispatch(AIRequest(
        session_id="s2", message_id=2, owner_id=777,
        user_message="بنویس سلام", chat_id=-1001, timezone="UTC",
    ))

    assert result.success is True
    assert result.metadata["finish_state"] == "local_fast_path"
    assert provider.calls == 0
    telegram.send_message.assert_awaited_once_with(777, "سلام")


@pytest.mark.asyncio
async def test_dispatcher_fast_path_recipient_send_does_not_send():
    from backend.ai.session.request import AIRequest

    telegram = MagicMock()
    telegram.send_message = AsyncMock(return_value={"id": 1, "text": "x"})
    provider = _FakeProvider()
    d, registry, executor = _make_dispatcher(telegram, provider)

    result = await d.dispatch(AIRequest(
        session_id="s3", message_id=3, owner_id=777,
        user_message="اینو برای علی بفرست", chat_id=-1001, timezone="UTC",
    ))

    assert result.success is True
    assert result.metadata["ai_action"]["action"] == "send"
    telegram.send_message.assert_not_awaited()
