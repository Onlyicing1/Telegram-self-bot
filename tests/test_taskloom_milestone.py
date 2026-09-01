"""Regression coverage for the current Taskloom milestone."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.ai.actions import KIND_EXECUTABLE, parse_command_intent
from backend.ai.chat_resolution import format_clarification_options, resolve_chat_name
from backend.ai.database.task_repository import InMemoryTaskRepository
from backend.ai.task_execution import TaskExecutionCoordinator
from backend.ai.tools.context import ToolContext
from backend.ai.tools.executor import ToolExecutor
from backend.ai.tools.message import SendMessageTool


OWNER = 777


def test_persian_interval_variants_are_scheduled():
    variants = [
        "هر 1 دقیقه یک بار برای من بنویس سلام",
        "هر یک دقیقه برای من بنویس سلام",
        "هر 5 دقیقه بنویس hello",
        "هر ده دقیقه یک پیام بفرست",
    ]
    for text in variants:
        result = parse_command_intent(text, has_reply=False)
        assert result.kind == KIND_EXECUTABLE
        assert result.action == "create_task"


def test_english_interval_variants_are_scheduled():
    for text in ("every minute write hello", "every 5 minutes write hello", "every hour remind me"):
        result = parse_command_intent(text, has_reply=False)
        assert result.kind == KIND_EXECUTABLE
        assert result.action == "create_task"


@pytest.mark.asyncio
async def test_immediate_send_uses_current_chat_context():
    telegram = MagicMock()
    telegram.send_message = AsyncMock(return_value={"id": 1})
    context = ToolContext(
        telegram=telegram,
        owner_id=OWNER,
        tz_str="UTC",
        extra={"chat_id": -100123},
    )
    result = await SendMessageTool(context).execute(context, {"text": "سلام"})
    assert result.success is True
    telegram.send_message.assert_awaited_once_with(-100123, "سلام")


@pytest.mark.asyncio
async def test_missing_destination_fails_closed():
    telegram = MagicMock()
    telegram.send_message = AsyncMock()
    context = ToolContext(telegram=telegram, owner_id=0, tz_str="UTC")
    result = await SendMessageTool(context).execute(context, {"text": "hello"})
    assert result.success is False
    telegram.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_scheduled_send_uses_task_creation_chat():
    repo = InMemoryTaskRepository()
    task = await repo.create_task(OWNER, {
        "label": "Write hello",
        "schedule_type": "once",
        "schedule": {"at": "2026-01-01T12:00:00", "timezone": "UTC"},
        "timezone": "UTC",
        "actions": [{"name": "send_message", "arguments": {"text": "hello"}}],
        "notification_destination": {"chat_id": -100123},
        "next_run_at": datetime.now(timezone.utc) - timedelta(seconds=1),
    })
    occurrence = await repo.create_occurrence(OWNER, {
        "task_id": task.id,
        "occurrence_key": "once",
        "definition_version": task.version,
        "action_snapshot": task.actions,
        "scheduled_for": datetime.now(timezone.utc),
    })
    await repo.claim_occurrence(OWNER, task.id, occurrence.occurrence_key)
    telegram = MagicMock()
    telegram.send_message = AsyncMock(return_value={"id": 1})
    context = ToolContext(telegram=telegram, owner_id=OWNER, tz_str="UTC")
    registry = __import__("backend.ai.tools.registry", fromlist=["ToolRegistry"]).ToolRegistry()
    registry.register(SendMessageTool(context))
    executor = ToolExecutor(registry, context)
    coordinator = TaskExecutionCoordinator(repo, executor, OWNER, context)
    result = await coordinator.execute(await repo.get_occurrence(OWNER, task.id, "once"))
    assert result.success is True
    telegram.send_message.assert_awaited_once_with(-100123, "hello")


def test_fuzzy_chat_name_resolves_unambiguous_partial():
    result = resolve_chat_name(
        "oskar",
        [{"id": -1, "title": "OskarBeam"}, {"id": -2, "title": "Research"}],
    )
    assert result["resolved"] is True
    assert result["chat_id"] == -1


def test_ambiguous_chat_name_returns_numbered_choices():
    result = resolve_chat_name(
        "oskar",
        [
            {"id": -1, "title": "OskarBeam"},
            {"id": -2, "title": "Oskar"},
            {"id": -3, "title": "Oskar Beam"},
        ],
    )
    assert result["resolved"] is False
    text = format_clarification_options(result)
    assert "1." in text and "2." in text
    assert "OskarBeam" in text


def test_numeric_model_destination_is_not_resolved_as_chat_name():
    result = resolve_chat_name("-100123", [{"id": -100123, "title": "OskarBeam"}])
    assert result["resolved"] is False
