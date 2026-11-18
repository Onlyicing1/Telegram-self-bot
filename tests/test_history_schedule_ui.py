"""Owner History schedule wizard: registration and the task/state creation path."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

import backend.ai.database.manager as manager_module
from backend.ai.database.task_repository import InMemoryTaskRepository
from backend.bot.handlers import history
from backend.helper.panels import get_action, get_panel
from backend.helper.panel_registry import get_panel_parent, get_panel_title
from backend.services import history_service
from backend.telegram_api.peers import PeerType

OWNER = 88001
PEER = -10088001


def _reset() -> None:
    history._STATE = history._State(
        chat_id=PEER, chat_name="Selected", peer_type=PeerType.PRIVATE_USER, focus="",
    )
    history._SCHEDULE = history._ScheduleState(
        peer_id=PEER,
        peer_name="Selected",
        peer_type=PeerType.PRIVATE_USER,
    )
    history._OWNER_ID = OWNER
    history._DEFAULT_TZ = "UTC"


def test_the_schedule_panel_and_action_are_registered_once():
    history.register(None, OWNER, "Asia/Tehran")
    assert get_panel("history_schedule") is history._schedule_panel
    assert get_panel_parent("history_schedule") == "history"
    assert get_panel_title("history_schedule") == "Scheduled History"
    assert get_action("history_schedule") is history._schedule_action
    assert b"panel:history_schedule" in [
        button.data for row in history._panel_buttons() for button in row
    ]


@pytest.mark.asyncio
async def test_the_wizard_refuses_to_create_without_a_selected_chat():
    _reset()
    history._STATE = history._State()
    history._SCHEDULE = history._ScheduleState()
    _title, body, _buttons = await history._create_scheduled_history()
    assert "Select a chat" in body


@pytest.mark.asyncio
async def test_the_wizard_creates_through_task_creation_service_and_state(monkeypatch):
    _reset()
    repo = InMemoryTaskRepository()
    repo.supports_durable_history = True

    async def _classify(*args, **kwargs):
        return PeerType.PRIVATE_USER

    async def _latest(*args, **kwargs):
        return 55

    monkeypatch.setattr(history, "get_self_client", lambda: object())
    monkeypatch.setattr(history_service, "classify_chat", _classify)
    monkeypatch.setattr(history_service, "latest_message_id", _latest)
    monkeypatch.setattr(
        manager_module, "get_repository_manager",
        lambda: SimpleNamespace(task=repo, supabase_available=True),
    )

    _title, body, _buttons = await history._create_scheduled_history()

    assert "Task #" in body
    tasks = await repo.list_tasks(OWNER)
    assert len(tasks) == 1
    task = tasks[0]
    assert task.actions[0]["name"] == "scheduled_history"
    # A Scheduled History checkpoint must never opt into result delivery: the
    # cumulative summary is read in the Summary Archive, not sent to the
    # owner's own chat (Saved Messages).
    assert task.notification_destination == {}
    assert "deliver_result" not in task.notification_destination
    state = await repo.get_history_schedule(OWNER, task.id)
    assert state is not None
    assert state.peer_id == PEER
    assert state.baseline_cursor == 55
    assert history._SCHEDULE.created_task_id == task.id


@pytest.mark.asyncio
async def test_the_wizard_refuses_when_durable_storage_is_unavailable(monkeypatch):
    _reset()
    repo = InMemoryTaskRepository()

    def _must_not_use_storage(*args, **kwargs):
        raise AssertionError("the wizard must refuse before creating a task")

    monkeypatch.setattr(history, "get_self_client", lambda: object())
    monkeypatch.setattr(history_service, "classify_chat", _must_not_use_storage)
    monkeypatch.setattr(
        manager_module, "get_repository_manager",
        lambda: SimpleNamespace(task=repo, supabase_available=False),
    )

    _title, body, _buttons = await history._create_scheduled_history()

    assert "Durable History storage is unavailable" in body
    assert await repo.list_tasks(OWNER) == []


@pytest.mark.asyncio
async def test_the_wizard_explicitly_chooses_the_backlog_baseline(monkeypatch):
    _reset()
    history._SCHEDULE.baseline_mode = "backlog"
    repo = InMemoryTaskRepository()
    repo.supports_durable_history = True
    called = False

    async def _classify(*args, **kwargs):
        return PeerType.PRIVATE_USER

    async def _latest(*args, **kwargs):
        nonlocal called
        called = True
        return 99

    monkeypatch.setattr(history, "get_self_client", lambda: object())
    monkeypatch.setattr(history_service, "classify_chat", _classify)
    monkeypatch.setattr(history_service, "latest_message_id", _latest)
    monkeypatch.setattr(
        manager_module, "get_repository_manager",
        lambda: SimpleNamespace(task=repo, supabase_available=True),
    )

    await history._create_scheduled_history()

    task = (await repo.list_tasks(OWNER))[0]
    state = await repo.get_history_schedule(OWNER, task.id)
    assert state.baseline_cursor == 0
    assert called is False
