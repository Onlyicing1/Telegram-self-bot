from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from backend.ai.task_candidate import TaskCandidate
from backend.ai.database.task_repository import TaskRecord
from backend.bot.handlers import tasks


class Event:
    def __init__(self, text: str, sender_id: int = 7):
        self.raw_text = text
        self.sender_id = sender_id
        self.edits: list[str] = []

    async def edit(self, text: str):
        self.edits.append(text)


class Client:
    def __init__(self):
        self.handler = None

    def on(self, event):
        def decorator(fn):
            self.handler = fn
            return fn
        return decorator


def candidate():
    return TaskCandidate.from_untrusted({
        "label": "standup",
        "schedule_type": "once",
        "schedule": {"at": "2030-01-01T10:00:00", "timezone": "UTC"},
        "timezone": "UTC",
        "actions": [{"name": "safe", "arguments": {}}],
        "notification_destination": {"kind": "owner"},
    })


@pytest.mark.asyncio
async def test_authorized_request_interprets_then_persists_and_edits_in_place():
    client = Client()
    tasks.register(client, 7, "UTC")
    event = Event(".task remind me")
    record = SimpleNamespace(id=12, label="standup")
    with patch.object(tasks, "_provider_manager", return_value=object()), \
         patch.object(tasks.TaskInterpreter, "interpret", new=AsyncMock(return_value=candidate())), \
         patch.object(tasks, "get_repository_manager") as manager:
        manager.return_value.task = object()
        with patch.object(tasks.TaskCreationService, "create", new=AsyncMock(return_value=record)) as create:
            await client.handler(event)
    assert create.await_count == 1
    assert "✅ Created" in event.edits[-1]
    assert len(event.edits) == 2


@pytest.mark.asyncio
async def test_management_commands_are_available():
    source = Path("backend/bot/handlers/tasks.py").read_text()
    assert "_MANAGEMENT_COMMANDS" in source
    assert "TaskManagementService" in source
    assert "list_text" in source
    assert "inspect_text" in source
    assert "expected_version" not in source
    assert "getattr(service, command)" in source


@pytest.mark.asyncio
async def test_non_owner_is_silent():
    client = Client()
    tasks.register(client, 7, "UTC")
    event = Event(".task remind me", sender_id=8)
    with patch.object(tasks, "_provider_manager") as provider:
        await client.handler(event)
    provider.assert_not_called()
    assert event.edits == []


@pytest.mark.asyncio
async def test_interpretation_failure_never_reports_success():
    client = Client()
    tasks.register(client, 7, "UTC")
    event = Event(".task ambiguous")
    with patch.object(tasks, "_provider_manager", return_value=object()), \
         patch.object(tasks.TaskInterpreter, "interpret", new=AsyncMock(side_effect=ValueError("bad"))), \
         patch.object(tasks.TaskCreationService, "create", new=AsyncMock()) as create, \
         patch.object(tasks, "get_repository_manager"):
        await client.handler(event)
    create.assert_not_called()
    assert "✅" not in event.edits[-1]
    assert "not created" in event.edits[-1]


@pytest.mark.asyncio
async def test_persistence_failure_never_reports_success():
    client = Client()
    tasks.register(client, 7, "UTC")
    event = Event(".task valid")
    with patch.object(tasks, "_provider_manager", return_value=object()), \
         patch.object(tasks.TaskInterpreter, "interpret", new=AsyncMock(return_value=candidate())), \
         patch.object(tasks, "get_repository_manager") as manager, \
         patch.object(tasks.TaskCreationService, "create", new=AsyncMock(side_effect=RuntimeError("db"))):
        manager.return_value.task = object()
        await client.handler(event)
    assert "✅" not in event.edits[-1]
    assert "persistence failed" in event.edits[-1]
