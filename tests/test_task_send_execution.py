"""
Focused regression tests: durable scheduled message EXECUTION end-to-end.

The task-creation fix delivered routing + persistence, but the persisted
action for "هر 1 دقیقه یک بار برای من بنویس سلام" (a message-writing action)
had no registered execution tool: the tool registry exposed no generic send
tool, so a claimed occurrence failed with ``unregistered_action`` at
execution time. These tests prove the full lifecycle now works:

    candidate action (model alias 'send' / 'content')
      -> canonicalized to the registered 'send_message' tool (text only)
      -> persisted in ai_tasks
      -> scheduler picks the task up
      -> ToolExecutor dispatches 'send_message'
      -> SendMessageTool sends the exact text to the OWNER's own chat
         (Saved Messages) through the existing TelegramAPI facade
      -> occurrence persists the real outcome (succeeded / retry_pending / failed)

Destination is derived ONLY from trusted runtime context: injected
chat/recipient/method arguments are stripped at candidate time and ignored
by the tool. Telegram failures flow through the existing retry boundary and
stay bounded by MAX_ATTEMPTS.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.ai.actions import KIND_CONVERSATIONAL, KIND_UNSUPPORTED, parse_command_intent
from backend.ai.database.task_repository import InMemoryTaskRepository
from backend.ai.providers.base.capabilities import ProviderCapabilities
from backend.ai.providers.base.config import ProviderConfig
from backend.ai.providers.base.contract import BaseProvider, ProviderResponse
from backend.ai.task_candidate import TaskCandidate, TaskCandidateError
from backend.ai.tools.base import ToolResult
from backend.ai.tools.context import ToolContext

OWNER = 777
TEXT = "سلام"


def _candidate(actions: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "label": "Write Hello",
        "schedule_type": "interval",
        "schedule": {"seconds": 60},
        "timezone": "UTC",
        "actions": actions,
        "notification_destination": {},
    }


def _fake_telegram(send_side_effect=None):
    telegram = MagicMock()
    if send_side_effect is not None:
        telegram.send_message = AsyncMock(side_effect=send_side_effect)
    else:
        telegram.send_message = AsyncMock(return_value={"id": 1, "text": TEXT})
    return telegram


# ── Candidate canonicalization ──


def test_send_alias_canonicalized_to_registered_tool():
    candidate = TaskCandidate.from_untrusted(
        _candidate([{"name": "send", "arguments": {"content": TEXT}}])
    )
    assert candidate.actions == [{"name": "send_message", "arguments": {"text": TEXT}}]


def test_send_message_alias_with_text_preserved():
    candidate = TaskCandidate.from_untrusted(
        _candidate([{"name": "send_message", "arguments": {"text": TEXT}}])
    )
    assert candidate.actions == [{"name": "send_message", "arguments": {"text": TEXT}}]


def test_injected_destination_is_stripped_not_persisted():
    # The model can never smuggle a destination/chat/method into the action.
    candidate = TaskCandidate.from_untrusted(
        _candidate([
            {"name": "send", "arguments": {
                "text": TEXT, "chat_id": 999, "recipient": "someone", "method": "delete_all",
            }}
        ])
    )
    assert candidate.actions == [{"name": "send_message", "arguments": {"text": TEXT}}]


def test_empty_send_text_rejects_candidate():
    with pytest.raises(TaskCandidateError):
        TaskCandidate.from_untrusted(_candidate([{"name": "send", "arguments": {}}]))


def test_non_send_actions_unchanged():
    candidate = TaskCandidate.from_untrusted(
        _candidate([{"name": "delete", "arguments": {"mode": "all"}}])
    )
    assert candidate.actions == [{"name": "delete", "arguments": {"mode": "all"}}]


# ── Registry / executor consistency ──


def test_send_message_is_registered_in_default_registry():
    from backend.ai.tools.registry import create_default_registry

    registry = create_default_registry(ToolContext(telegram=None, owner_id=OWNER, tz_str="UTC"))
    names = registry.list_names()
    assert "send_message" in names
    assert "create_task" in names


@pytest.mark.asyncio
async def test_executor_dispatches_send_message_to_owner():
    from backend.ai.tools.executor import ToolExecutor
    from backend.ai.tools.registry import create_default_registry

    telegram = _fake_telegram()
    ctx = ToolContext(telegram=telegram, owner_id=OWNER, tz_str="UTC")
    registry = create_default_registry(ctx)
    executor = ToolExecutor(registry, ctx)

    results = await executor.execute_calls(
        [{"name": "send_message", "arguments": {"text": TEXT}}], owner_id=OWNER
    )
    assert len(results) == 1
    assert results[0].success is True
    telegram.send_message.assert_awaited_once_with(OWNER, TEXT)


@pytest.mark.asyncio
async def test_send_message_tool_ignores_argument_destination():
    from backend.ai.tools.message import SendMessageTool

    telegram = _fake_telegram()
    ctx = ToolContext(telegram=telegram, owner_id=OWNER, tz_str="UTC")
    result = await SendMessageTool(ctx).execute(
        ctx, {"text": TEXT, "chat_id": 999, "recipient": "someone"}
    )
    assert result.success is True
    telegram.send_message.assert_awaited_once_with(OWNER, TEXT)


@pytest.mark.asyncio
async def test_send_message_tool_rejects_missing_text():
    from backend.ai.tools.message import SendMessageTool

    telegram = _fake_telegram()
    ctx = ToolContext(telegram=telegram, owner_id=OWNER, tz_str="UTC")
    result = await SendMessageTool(ctx).execute(ctx, {})
    assert result.success is False
    telegram.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_send_message_tool_fails_without_owner_context():
    from backend.ai.tools.message import SendMessageTool

    telegram = _fake_telegram()
    ctx = ToolContext(telegram=telegram, owner_id=0, tz_str="UTC")
    result = await SendMessageTool(ctx).execute(ctx, {"text": TEXT})
    assert result.success is False
    telegram.send_message.assert_not_awaited()


# ── Creation persists the canonical action ──


class _FakeProvider(BaseProvider):
    def __init__(self, name: str, response_text: str) -> None:
        super().__init__(ProviderConfig(provider_name=name, enabled=True, default_model="m"))
        self._name = name
        self._response_text = response_text
        self.calls = 0

    @property
    def name(self) -> str:
        return self._name

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(supports_tools=True, supports_function_call=True)

    async def chat(self, messages, **kwargs):
        self.calls += 1
        return ProviderResponse(text=self._response_text, provider_name=self._name, success=True)

    def initialize(self) -> None:
        return None

    def shutdown(self) -> None:
        return None

    def count_tokens(self, text: str) -> int:
        return max(1, len(text) // 4)

    def health(self) -> dict[str, Any]:
        return {"healthy": True}


@pytest.mark.asyncio
async def test_create_task_tool_persists_canonical_send_message_action():
    from backend.ai.database import manager as dbm
    from backend.ai.providers.manager.manager import ProviderManager
    from backend.ai.tools.task import CreateTaskTool
    from unittest.mock import patch

    pm = ProviderManager()
    pm.register_provider(_FakeProvider(
        "fake",
        '{"label":"Write Hello","schedule_type":"interval","schedule":{"seconds":60},'
        '"timezone":"UTC","actions":[{"name":"send","arguments":{"content":"سلام"}}],'
        '"notification_destination":{}}',
    ))
    pm.switch_provider("fake")
    pm._fallback_chain = []

    manager = dbm.RepositoryManager(supabase_available=False)
    ctx = ToolContext(
        telegram=None, owner_id=OWNER, tz_str="UTC", client=None,
        extra={"provider_manager": pm, "chat_id": -1001},
    )
    with patch.object(dbm, "get_repository_manager", return_value=manager):
        result = await CreateTaskTool(ctx).execute(ctx, {"request": "هر 1 دقیقه یک بار برای من بنویس سلام"})

    assert result.success is True
    tasks = await manager.task.list_tasks(OWNER)
    assert len(tasks) == 1
    assert tasks[0].actions == [{"name": "send_message", "arguments": {"text": "سلام"}}]


# ── Scheduler execution end-to-end ──


def _coordinator(repo, telegram):
    from backend.ai.task_execution import TaskExecutionCoordinator
    from backend.ai.tools.executor import ToolExecutor
    from backend.ai.tools.registry import create_default_registry

    ctx = ToolContext(telegram=telegram, owner_id=OWNER, tz_str="UTC")
    registry = create_default_registry(ctx)
    executor = ToolExecutor(registry, ctx)
    return TaskExecutionCoordinator(repo, executor, OWNER, ctx)


@pytest.mark.asyncio
async def test_scheduler_executes_scheduled_message_and_persists_success():
    from backend.ai.task_scheduler import TaskScheduler

    repo = InMemoryTaskRepository()
    telegram = _fake_telegram()
    task = await repo.create_task(OWNER, {
        "label": "Write Hello",
        "schedule_type": "interval",
        "schedule": {"seconds": 60},
        "timezone": "UTC",
        "actions": [{"name": "send_message", "arguments": {"text": TEXT}}],
        "notification_destination": {},
        "next_run_at": datetime.now(timezone.utc) - timedelta(seconds=10),
    })

    scheduler = TaskScheduler(repo, OWNER, _coordinator(repo, telegram), outcome_notifier=None)
    now = datetime.now(timezone.utc)
    processed = await scheduler.run_once(now)

    assert processed == 1
    telegram.send_message.assert_awaited_once_with(OWNER, TEXT)
    occurrences = await repo.list_occurrences(OWNER, task.id)
    assert len(occurrences) == 1
    assert occurrences[0].status == "succeeded"
    assert occurrences[0].result_metadata.get("terminal_status") == "succeeded"


@pytest.mark.asyncio
async def test_send_failure_reaches_retry_boundary_then_terminal_failure():
    from backend.ai.task_execution import TaskExecutionCoordinator
    from backend.ai.tools.executor import ToolExecutor
    from backend.ai.tools.registry import ToolRegistry
    from backend.ai.tools.message import SendMessageTool
    from backend.telegram_api.exceptions import TelegramTimeoutError

    repo = InMemoryTaskRepository()
    task = await repo.create_task(OWNER, {
        "label": "Write Hello",
        "schedule_type": "once",
        "schedule": {"at": "2026-01-01T12:00:00", "timezone": "UTC"},
        "timezone": "UTC",
        "actions": [{"name": "send_message", "arguments": {"text": TEXT}}],
        "notification_destination": {},
        "next_run_at": datetime.now(timezone.utc) - timedelta(seconds=1),
    })

    telegram = _fake_telegram(send_side_effect=TelegramTimeoutError("send timed out"))
    ctx = ToolContext(telegram=telegram, owner_id=OWNER, tz_str="UTC")
    registry = ToolRegistry()
    registry.register(SendMessageTool(ctx))
    executor = ToolExecutor(registry, ctx)
    coordinator = TaskExecutionCoordinator(repo, executor, OWNER, ctx)

    # occurrence 1: claimed -> running -> retry_pending (attempt 2)
    occ = await repo.create_occurrence(OWNER, {
        "task_id": task.id, "occurrence_key": "k1", "definition_version": task.version,
        "action_snapshot": [{"name": "send_message", "arguments": {"text": TEXT}}],
        "scheduled_for": datetime.now(timezone.utc),
    })
    claimed = await repo.claim_occurrence(OWNER, task.id, "k1")
    result = await coordinator.execute(claimed)
    assert result.status == "retry_pending"
    occ1 = await repo.get_occurrence(OWNER, task.id, "k1")
    assert occ1.status == "retry_pending"
    assert occ1.attempt == 2

    # occurrence 2 (retry): still failing -> retry_pending (attempt 3)
    claimed2 = await repo.claim_occurrence(OWNER, task.id, "k1")
    result2 = await coordinator.execute(claimed2)
    assert result2.status == "retry_pending"
    assert (await repo.get_occurrence(OWNER, task.id, "k1")).attempt == 3

    # occurrence 3: MAX_ATTEMPTS reached -> terminal failed, no fourth attempt
    claimed3 = await repo.claim_occurrence(OWNER, task.id, "k1")
    result3 = await coordinator.execute(claimed3)
    assert result3.status == "failed"
    final = await repo.get_occurrence(OWNER, task.id, "k1")
    assert final.status == "failed"
    assert final.attempt == 3


# ── Ordinary AI behavior is NOT diverted into the task system ──


def test_ordinary_conversation_is_not_scheduled():
    r = parse_command_intent("درباره آب و هوا برایم توضیح بده", has_reply=True)
    assert r.kind == KIND_CONVERSATIONAL


def test_immediate_send_is_not_a_scheduled_task():
    r = parse_command_intent("اینو برای علی بفرست", has_reply=True)
    assert r.kind == KIND_UNSUPPORTED
    assert r.action == "send"
