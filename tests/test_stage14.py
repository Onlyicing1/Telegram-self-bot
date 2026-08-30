from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

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


@pytest.mark.asyncio
async def test_list_delegates_to_management_service_and_edits_once():
    client = Client()
    tasks.register(client, 7, "UTC")
    event = Event(".task list")
    service = SimpleNamespace()
    with patch.object(tasks, "_management", return_value=service), \
         patch.object(tasks, "list_text", new=AsyncMock(return_value="Tasks\n\n#1 demo")):
        await client.handler(event)
    assert len(event.edits) == 1
    assert "#1 demo" in event.edits[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["inspect", "pause", "resume", "complete", "fail", "expire", "delete"])
async def test_management_commands_delegate_with_id_and_version(command):
    client = Client()
    tasks.register(client, 7, "UTC")
    event = Event(f".task {command} 12" if command == "inspect" else f".task {command} 12 4")
    method = AsyncMock(return_value=SimpleNamespace(id=12, version=5))
    service = SimpleNamespace(**{command: method})
    if command == "inspect":
        with patch.object(tasks, "_management", return_value=service), \
             patch.object(tasks, "inspect_text", new=AsyncMock(return_value="details")):
            await client.handler(event)
        assert "details" in event.edits[-1]
    else:
        with patch.object(tasks, "_management", return_value=service):
            await client.handler(event)
        method.assert_awaited_once_with(12, 4)
        assert "✅" in event.edits[-1]


@pytest.mark.asyncio
async def test_invalid_management_input_does_not_call_service():
    client = Client()
    tasks.register(client, 7, "UTC")
    event = Event(".task pause nope nope")
    with patch.object(tasks, "_management") as management:
        await client.handler(event)
    management.assert_called_once_with(7)
    assert "Invalid task command" in event.edits[-1]


@pytest.mark.asyncio
async def test_stale_or_missing_mutation_is_not_reported_as_success():
    client = Client()
    tasks.register(client, 7, "UTC")
    event = Event(".task pause 12 4")
    service = SimpleNamespace(pause=AsyncMock(return_value=None))
    with patch.object(tasks, "_management", return_value=service):
        await client.handler(event)
    assert "✅" not in event.edits[-1]
    assert "stale" in event.edits[-1]


@pytest.mark.asyncio
async def test_non_owner_management_request_is_silent():
    client = Client()
    tasks.register(client, 7, "UTC")
    event = Event(".task delete 12 4", sender_id=8)
    with patch.object(tasks, "_management") as management:
        await client.handler(event)
    management.assert_not_called()
    assert event.edits == []


def test_handler_has_no_direct_persistence_or_telegram_execution():
    source = open("backend/bot/handlers/tasks.py", encoding="utf-8").read()
    assert "TaskRepository" not in source
    assert ".execute(" not in source
    assert "forward_messages" not in source
    assert "table(" not in source
