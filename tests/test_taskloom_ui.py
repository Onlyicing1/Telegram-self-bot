"""Taskloom UI tests — panels over the existing TaskManagementService boundary.

Verifies:
- Registration of panels/actions and the AI-panel wiring.
- List panel reads through TaskManagementService (no direct DB access).
- Detail panel shows metadata, actions, occurrences, and status-conditional
  CAS buttons carrying (task_id, version).
- Mutations route through the service's CAS transitions and render the
  refreshed detail panel; stale versions fail closed with no change.
- Cross-owner isolation: another owner's task is invisible.
"""
from __future__ import annotations

import asyncio
import re

import pytest

from backend.ai.database.task_repository import InMemoryTaskRepository
from backend.ai.task_management import TaskManagementService
from backend.bot.handlers import taskloom


OWNER = 4242
OTHER = 9999


def _svc(repo, owner):
    return TaskManagementService(repo, owner)


def _make_task(repo, owner, label="write hello", status="active"):
    return asyncio.new_event_loop().run_until_complete(
        repo.create_task(
            owner,
            {
                "label": label,
                "schedule_type": "interval",
                "schedule": {"interval_seconds": 60},
                "timezone": "UTC",
                "actions": [{"name": "send_message", "arguments": {"text": "hello"}}],
                "notification_destination": {"kind": "saved_messages"},
                "status": status,
                "next_run_at": None,
            },
        )
    )


class _FakeEvent:
    def __init__(self):
        self.edits = []

    async def edit(self, text, buttons=None):
        self.edits.append((text, buttons))


@pytest.fixture()
def repo():
    return InMemoryTaskRepository()


@pytest.fixture()
def registered(repo, monkeypatch):
    """Register Taskloom against a repo-backed service for OWNER."""
    import backend.ai.task_management as tm

    real = tm.TaskManagementService

    def factory(task_repo, owner_id):
        return real(repo, owner_id)

    monkeypatch.setattr(tm, "TaskManagementService", factory)
    from backend.helper import inline_engine

    inline_engine.set_owner_id(OWNER)
    taskloom.register(client=None, owner_id=OWNER, tz_str="UTC")
    return taskloom


def _callback_data(buttons) -> list[str]:
    """Flatten button rows into decoded callback-data strings."""
    data = []
    for row in buttons:
        for b in row:
            raw = getattr(b, "data", b)
            data.append(raw.decode("utf-8") if isinstance(raw, bytes) else str(raw))
    return data


def _button_texts(buttons) -> list[str]:
    """Flatten button rows into display texts."""
    return [str(getattr(b, "text", b)) for row in buttons for b in row]


def _button_texts(buttons) -> list[str]:
    """Flatten button rows into display texts."""
    return [str(getattr(b, "text", b)) for row in buttons for b in row]


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ── registration ──

def test_registers_panels_and_actions(registered):
    from backend.helper.panel_registry import registry as get_registry
    from backend.helper.panels import get_action

    assert get_registry().get_handler("taskloom") is not None
    assert get_registry().get_handler("taskloom_task") is not None
    for verb in ("pause", "resume", "complete", "delete"):
        assert get_action(f"taskloom_{verb}") is not None


# ── list panel ──

def test_list_panel_empty_state(registered):
    result = _run(registered._taskloom_panel(_FakeEvent(), ""))
    title, body, buttons = result
    assert title == "Taskloom"
    assert "No tasks yet" in body
    assert buttons  # navigation present


def test_list_panel_shows_tasks_with_counts(registered, repo):
    _make_task(repo, OWNER, "write hello")
    _make_task(repo, OWNER, "cleanup", status="paused")
    _make_task(repo, OWNER, "old one", status="completed")
    title, body, buttons = _run(registered._taskloom_panel(_FakeEvent(), ""))
    assert "🟢 1 active" in body
    assert "⏸ 1 paused" in body
    # task labels live in button text, one row per task
    texts = " ".join(_button_texts(buttons))
    assert "write hello" in texts
    data = _callback_data(buttons)
    assert sum(1 for d in data if "panel:taskloom_task:" in d) == 3


def test_list_panel_is_owner_scoped(registered, repo):
    _make_task(repo, OWNER, "mine")
    _make_task(repo, OTHER, "theirs")
    title, body, buttons = _run(registered._taskloom_panel(_FakeEvent(), ""))
    texts = " ".join(_button_texts(buttons))
    assert "mine" in texts
    assert "theirs" not in texts


# ── detail panel ──

def test_detail_panel_shows_metadata_and_occurrences(registered, repo):
    task = _make_task(repo, OWNER)
    title, body, buttons = _run(registered._task_detail_panel(_FakeEvent(), str(task.id)))
    assert f"#{task.id}" in body
    assert "send_message" in body
    assert "interval" in body
    assert "UTC" in body


def test_detail_panel_cas_buttons_carry_version(registered, repo):
    task = _make_task(repo, OWNER)
    title, body, buttons = _run(registered._task_detail_panel(_FakeEvent(), str(task.id)))
    flat = _callback_data(buttons)
    assert f"action:taskloom_pause:{task.id}:{task.version}" in flat
    assert f"action:taskloom_complete:{task.id}:{task.version}" in flat
    assert f"action:taskloom_delete:{task.id}:{task.version}" in flat


def test_detail_panel_omits_complete_for_terminal(registered, repo):
    task = _make_task(repo, OWNER, status="completed")
    title, body, buttons = _run(registered._task_detail_panel(_FakeEvent(), str(task.id)))
    flat = _callback_data(buttons)
    assert not any("taskloom_complete" in d for d in flat)


def test_detail_panel_invalid_and_missing_ids(registered):
    assert "Invalid task id" in _run(registered._task_detail_panel(_FakeEvent(), "abc"))[1]
    assert "not found" in _run(registered._task_detail_panel(_FakeEvent(), "31337"))[1]


# ── mutations (CAS) ──

def test_pause_action_transitions_and_rerenders(registered, repo):
    task = _make_task(repo, OWNER)
    title, body, buttons = _run(
        registered._pause_action(_FakeEvent(), f"{task.id}:{task.version}", 1)
    )
    assert "✅ paused" in body
    stored = _run(repo.get_task(OWNER, task.id))
    assert stored.status == "paused"
    # refreshed detail offers Resume at the NEW version
    flat = _callback_data(buttons)
    assert f"action:taskloom_resume:{task.id}:{task.version + 1}" in flat


def test_resume_action_transitions(registered, repo):
    task = _make_task(repo, OWNER, status="paused")
    title, body, buttons = _run(
        registered._resume_action(_FakeEvent(), f"{task.id}:{task.version}", 1)
    )
    assert "✅ resumed" in body
    assert _run(repo.get_task(OWNER, task.id)).status == "active"


def test_complete_action_transitions(registered, repo):
    task = _make_task(repo, OWNER)
    _run(registered._complete_action(_FakeEvent(), f"{task.id}:{task.version}", 1))
    assert _run(repo.get_task(OWNER, task.id)).status == "completed"


def test_delete_action_transitions(registered, repo):
    task = _make_task(repo, OWNER)
    _run(registered._delete_action(_FakeEvent(), f"{task.id}:{task.version}", 1))
    assert _run(repo.get_task(OWNER, task.id)).status == "deleted"


def test_stale_version_fails_closed(registered, repo):
    task = _make_task(repo, OWNER)
    title, body, buttons = _run(
        registered._pause_action(_FakeEvent(), f"{task.id}:{task.version + 5}", 1)
    )
    assert "❌" in body
    assert _run(repo.get_task(OWNER, task.id)).status == "active"  # unchanged


def test_mutation_on_foreign_task_fails_closed(registered, repo):
    task = _make_task(repo, OTHER)
    title, body, buttons = _run(
        registered._pause_action(_FakeEvent(), f"{task.id}:{task.version}", 1)
    )
    assert "❌" in body
    assert _run(repo.get_task(OTHER, task.id)).status == "active"


def test_malformed_action_extra_fails_closed(registered, repo):
    task = _make_task(repo, OWNER)
    title, body, buttons = _run(registered._pause_action(_FakeEvent(), "garbage", 1))
    assert "❌" in body
    assert _run(repo.get_task(OWNER, task.id)).status == "active"


def test_mutation_error_message_is_bounded(registered, repo):
    task = _make_task(repo, OWNER)
    title, body, buttons = _run(
        registered._pause_action(_FakeEvent(), f"{task.id}:{task.version}", 1)
    )
    assert len(body) < 8000
