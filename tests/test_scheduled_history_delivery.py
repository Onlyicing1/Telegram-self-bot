"""Scheduled History commits its checkpoint without messaging anyone.

Every run here drives the REAL delivery chain — TaskScheduler -> occurrence
claim -> TaskExecutionCoordinator -> ToolExecutor -> ScheduledHistoryTool —
and replaces only the History service, which needs a live Telegram client.
The assertion is always on the production ``_deliver_result`` boundary, never
on a copied helper.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.ai.database import manager as dbm
from backend.ai.task_contract import SCHEDULED_HISTORY_ACTION
from backend.ai.task_execution import TaskExecutionCoordinator
from backend.ai.task_scheduler import TaskScheduler
from backend.ai.tools.context import ToolContext
from backend.ai.tools.executor import ToolExecutor
from backend.ai.tools.registry import create_default_registry
from backend.services import history_schedule_service

OWNER = 99001
PEER = -10099001
CHAT = -100123

SUMMARY = "Cumulative checkpoint: the chat discussed release planning."


async def _history_task(repo, *, destination):
    task = await repo.create_task(OWNER, {
        "label": "History · Selected",
        "schedule_type": "interval",
        "schedule": {"seconds": 3600},
        "timezone": "UTC",
        "actions": [{"name": SCHEDULED_HISTORY_ACTION, "arguments": {}}],
        "notification_destination": destination,
        "next_run_at": datetime.now(timezone.utc) - timedelta(seconds=5),
    })
    await repo.create_history_schedule(OWNER, task.id, PEER, baseline_cursor=0)
    return task


async def _run_once(repo, telegram):
    context = ToolContext(telegram=telegram, owner_id=OWNER, tz_str="UTC", extra={})
    executor = ToolExecutor(create_default_registry(context), context)
    coordinator = TaskExecutionCoordinator(repo, executor, OWNER, context)
    manager = SimpleNamespace(task=repo, supabase_available=False)
    with patch.object(dbm, "get_repository_manager", return_value=manager):
        scheduler = TaskScheduler(repo, OWNER, coordinator, outcome_notifier=None)
        return await scheduler.run_once(datetime.now(timezone.utc))


def _service_result(ok=True, message=SUMMARY, **data):
    payload = {"operation": SCHEDULED_HISTORY_ACTION, "processed": 3,
               "scanned": 10, "summarized": 10, "has_more": False,
               "revision": 1, "version": 2}
    payload.update(data)
    return ok, message, payload


@pytest.mark.asyncio
async def test_scheduled_history_occurrence_commits_and_never_messages(monkeypatch):
    from backend.ai.database.task_repository import InMemoryTaskRepository

    repo = InMemoryTaskRepository()
    telegram = MagicMock()
    telegram.send_message = AsyncMock(return_value={"id": 1})
    task = await _history_task(repo, destination={})
    calls: list[dict] = []

    async def _fake_run(*args, **kwargs):
        # Commit exactly as the real service does, so the test proves the
        # summary IS durable while nothing is delivered.
        state = await repo.get_history_schedule(OWNER, kwargs["task_id"])
        await repo.commit_history_success(
            OWNER, state.peer_id, expected_version=state.version,
            summary=SUMMARY, scanned_cursor=10, summarized_cursor=10,
        )
        calls.append(kwargs)
        return _service_result()

    monkeypatch.setattr(history_schedule_service, "run_scheduled_history", _fake_run)

    assert await _run_once(repo, telegram) == 1

    occurrence = (await repo.list_occurrences(OWNER, task.id))[0]
    assert occurrence.status == "succeeded"
    stored = await repo.get_history_schedule(OWNER, task.id)
    assert stored.summary == SUMMARY and stored.revision == 1
    assert calls and calls[0]["owner_id"] == OWNER and calls[0]["task_id"] == task.id
    telegram.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_legacy_definition_with_deliver_result_still_never_messages(monkeypatch):
    """Rows persisted before the wizard changed must also stay silent."""
    from backend.ai.database.task_repository import InMemoryTaskRepository

    repo = InMemoryTaskRepository()
    telegram = MagicMock()
    telegram.send_message = AsyncMock(return_value={"id": 1})
    task = await _history_task(
        repo, destination={"chat_id": CHAT, "deliver_result": True},
    )

    async def _fake_run(*args, **kwargs):
        return _service_result()

    monkeypatch.setattr(history_schedule_service, "run_scheduled_history", _fake_run)

    assert await _run_once(repo, telegram) == 1
    assert (await repo.list_occurrences(OWNER, task.id))[0].status == "succeeded"
    telegram.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_noop_checkpoint_sends_no_message(monkeypatch):
    from backend.ai.database.task_repository import InMemoryTaskRepository

    repo = InMemoryTaskRepository()
    telegram = MagicMock()
    telegram.send_message = AsyncMock(return_value={"id": 1})
    task = await _history_task(repo, destination={})

    async def _fake_run(*args, **kwargs):
        return _service_result(message="", processed=0)

    monkeypatch.setattr(history_schedule_service, "run_scheduled_history", _fake_run)

    assert await _run_once(repo, telegram) == 1
    assert (await repo.list_occurrences(OWNER, task.id))[0].status == "succeeded"
    telegram.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_checkpoint_sends_no_summary(monkeypatch):
    from backend.ai.database.task_repository import InMemoryTaskRepository

    repo = InMemoryTaskRepository()
    telegram = MagicMock()
    telegram.send_message = AsyncMock(return_value={"id": 1})
    task = await _history_task(repo, destination={})

    async def _fake_run(*args, **kwargs):
        return _service_result(
            False, "Scheduled History could not read the chat.",
            error="scheduled_history_retrieval_failed",
        )

    monkeypatch.setattr(history_schedule_service, "run_scheduled_history", _fake_run)

    assert await _run_once(repo, telegram) == 1
    assert (await repo.list_occurrences(OWNER, task.id))[0].status != "succeeded"
    telegram.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_delivery_still_works_for_an_ordinary_opted_in_task():
    """The History guard must not change any other task's delivery."""
    from backend.ai.database.task_repository import InMemoryTaskRepository

    repo = InMemoryTaskRepository()
    telegram = MagicMock()
    telegram.send_message = AsyncMock(return_value={"id": 1})
    task = await repo.create_task(OWNER, {
        "label": "daily report",
        "schedule_type": "interval",
        "schedule": {"seconds": 3600},
        "timezone": "UTC",
        "actions": [{"name": "task_list", "arguments": {}}],
        "notification_destination": {"chat_id": CHAT, "deliver_result": True},
    })
    context = ToolContext(telegram=telegram, owner_id=OWNER, tz_str="UTC", extra={})
    coordinator = TaskExecutionCoordinator(
        repo, ToolExecutor(create_default_registry(context), context), OWNER, context,
    )

    await coordinator._deliver_result(
        task, [SimpleNamespace(message="daily report ready")], context, "occurrence",
    )

    telegram.send_message.assert_awaited_once()
    chat, text = telegram.send_message.await_args.args
    assert chat == CHAT and "daily report ready" in text
