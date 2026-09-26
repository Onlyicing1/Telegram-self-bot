"""The Telegram Todo surface — panels, actions and inputs over the service.

Every read goes through ``TaskManagementService`` (one snapshot) and every
mutation through its CAS operations; the tests drive the REAL panels and
callbacks against an in-memory repository. No live Telegram, no helper bot.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from backend.ai.database.task_repository import InMemoryTaskRepository
from backend.ai.task_creation import TaskCreationService
from backend.bot.handlers import todo

OWNER = 4242
OTHER = 9999


class _DegradedRepository(InMemoryTaskRepository):
    """A repository that reports its reads degraded (no durable store)."""

    def __init__(self):
        super().__init__()
        self.fallback_active = True
        self.fallback_reason = "unavailable"


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


async def _todo(repo, label, owner=OWNER):
    return await TaskCreationService(repo, owner).create_todo(
        label, "Asia/Tehran", datetime(2026, 9, 26, 9, 0, tzinfo=timezone.utc)
    )


@pytest.fixture()
def repo():
    return InMemoryTaskRepository()


@pytest.fixture()
def registered(repo, monkeypatch):
    """Register the Todo surface against a repo-backed manager for OWNER."""
    from backend.ai.database import manager as dbm

    manager = dbm.RepositoryManager(supabase_available=False)
    manager._task = repo
    monkeypatch.setattr(dbm, "get_repository_manager", lambda: manager)
    from backend.helper import inline_engine

    inline_engine.set_owner_id(OWNER)
    todo.register(client=None, owner_id=OWNER, tz_str="Asia/Tehran")
    return todo


def _datas(buttons) -> list[str]:
    out = []
    for row in buttons:
        for button in row:
            raw = getattr(button, "data", button)
            out.append(raw.decode("utf-8") if isinstance(raw, bytes) else str(raw))
    return out


def _texts(buttons) -> list[str]:
    return [str(getattr(button, "text", button)) for row in buttons for button in row]


# ── registration + entry point ───────────────────────────────────────────────


def test_registers_panels_actions_and_inputs(registered):
    from backend.helper.panel_registry import registry as get_registry
    from backend.helper.panels import get_action, get_input

    for panel_id in ("todo", "todo_done", "todo_task"):
        assert get_registry().get_handler(panel_id) is not None
    for action_id in ("todo_complete", "todo_reopen", "todo_delete"):
        assert get_action(action_id) is not None
    assert get_input("todo", "new") is not None
    assert get_input("todo_task", "edit") is not None


def test_the_mother_menu_opens_the_todo_panel():
    from backend.bot.handlers.misc import _build_menu_buttons

    assert "panel:todo" in _datas(_build_menu_buttons())


# ── list + completed panels ──────────────────────────────────────────────────


def test_list_panel_shows_active_todos_and_the_completed_counter(registered, repo):
    active = _run(_todo(repo, "گزارش دانشگاه"))
    done = _run(_todo(repo, "خرید نان"))
    _run(repo.transition_task(OWNER, done.id, "completed", expected_version=done.version))

    title, body, buttons = _run(registered._todo_list_panel(None, ""))
    assert title == "Todo"
    assert "1 active" in body and "1 completed" in body
    datas = _datas(buttons)
    assert f"panel:todo_task:{active.id}" in datas
    assert f"panel:todo_task:{done.id}" not in datas  # completed lives one tap away
    assert "panel:todo_done" in datas
    assert "input:todo:new" in datas
    assert any("گزارش دانشگاه" in text for text in _texts(buttons))


def test_list_panel_empty_state_offers_the_add_input(registered):
    _title, body, buttons = _run(registered._todo_list_panel(None, ""))
    assert "No active todos" in body
    assert "input:todo:new" in _datas(buttons)


def test_completed_panel_lists_completed_todos_only(registered, repo):
    active = _run(_todo(repo, "still active"))
    done = _run(_todo(repo, "finished"))
    _run(repo.transition_task(OWNER, done.id, "completed", expected_version=done.version))

    _title, body, buttons = _run(registered._todo_done_panel(None, ""))
    assert "1 completed" in body
    datas = _datas(buttons)
    assert f"panel:todo_task:{done.id}" in datas
    assert f"panel:todo_task:{active.id}" not in datas
    assert "panel:todo" in datas


def test_list_is_owner_scoped(registered, repo):
    foreign = _run(_todo(repo, "other owner todo", owner=OTHER))
    _title, body, buttons = _run(registered._todo_list_panel(None, ""))
    assert "other owner todo" not in body
    assert f"panel:todo_task:{foreign.id}" not in _datas(buttons)
    # And the foreign todo's own detail panel is not reachable through it.
    _title, body, _buttons = _run(registered._todo_detail_panel(None, str(foreign.id)))
    assert "not found" in body


def test_pagination_clamps_onto_the_current_list(registered, repo):
    for index in range(9):
        _run(_todo(repo, f"todo {index}"))
    _title, _body, buttons = _run(registered._todo_list_panel(None, "0"))
    assert any("todo 0" in text for text in _texts(buttons))
    assert "panel:todo:1" in _datas(buttons)  # 9 rows over 4-row pages
    # A page that no longer exists clamps onto the last valid one instead of
    # rendering nothing.
    _title, body, buttons = _run(registered._todo_list_panel(None, "9"))
    assert "9 active" in body
    assert any("todo 8" in text for text in _texts(buttons))
    assert "3 / 3" in _texts(buttons)


def test_a_degraded_read_is_never_shown_as_an_authoritative_empty_list(
    registered, repo, monkeypatch
):
    from backend.ai.database import manager as dbm

    degraded = _DegradedRepository()
    manager = dbm.RepositoryManager(supabase_available=False)
    manager._task = degraded
    monkeypatch.setattr(dbm, "get_repository_manager", lambda: manager)
    _title, body, _buttons = _run(registered._todo_list_panel(None, ""))
    assert "Memory fallback" in body


# ── detail panel ─────────────────────────────────────────────────────────────


def test_detail_panel_offers_the_status_appropriate_actions(registered, repo):
    active = _run(_todo(repo, "گزارش دانشگاه"))
    _title, body, buttons = _run(registered._todo_detail_panel(None, str(active.id)))
    assert f"Todo #{active.id}" in body and "گزارش دانشگاه" in body
    datas = _datas(buttons)
    assert f"action:todo_complete:{active.id}:{active.version}" in datas
    assert f"input:todo_task:edit:{active.id}:{active.version}" in datas
    assert f"action:todo_delete:{active.id}:{active.version}" in datas
    assert not any(data.startswith("action:todo_reopen") for data in datas)

    _run(repo.transition_task(OWNER, active.id, "completed", expected_version=active.version))
    _title, _body, buttons = _run(registered._todo_detail_panel(None, str(active.id)))
    datas = _datas(buttons)
    assert any(data.startswith("action:todo_reopen") for data in datas)
    assert not any(data.startswith("action:todo_complete") for data in datas)


def test_detail_panel_rejects_an_unknown_or_non_todo_id(registered, repo):
    _title, body, _buttons = _run(registered._todo_detail_panel(None, "not-a-number"))
    assert "Invalid todo id" in body
    _title, body, _buttons = _run(registered._todo_detail_panel(None, "987654"))
    assert "not found" in body


# ── actions ──────────────────────────────────────────────────────────────────


def test_complete_action_mutates_and_renders_the_refreshed_panel(registered, repo):
    created = _run(_todo(repo, "گزارش دانشگاه"))
    title, body, _buttons = _run(
        registered._complete_action(None, f"{created.id}:{created.version}", 0)
    )
    assert title == f"Todo #{created.id}"
    assert "completed" in body
    assert _run(repo.get_task(OWNER, created.id)).status == "completed"


def test_reopen_action_returns_a_completed_todo_to_active(registered, repo):
    created = _run(_todo(repo, "گزارش دانشگاه"))
    done = _run(repo.transition_task(OWNER, created.id, "completed", expected_version=1))
    _title, body, _buttons = _run(
        registered._reopen_action(None, f"{created.id}:{done.version}", 0)
    )
    assert "reopened" in body
    assert _run(repo.get_task(OWNER, created.id)).status == "active"


def test_a_stale_version_fails_closed_without_changing_anything(registered, repo):
    created = _run(_todo(repo, "گزارش دانشگاه"))
    stale = created.version + 5
    _title, body, _buttons = _run(
        registered._complete_action(None, f"{created.id}:{stale}", 0)
    )
    assert "stale" in body
    assert _run(repo.get_task(OWNER, created.id)).status == "active"
    _title, _body, _buttons = _run(registered._reopen_action(None, f"{created.id}:{stale}", 0))
    assert _run(repo.get_task(OWNER, created.id)).status == "active"


def test_delete_action_removes_the_row_and_shows_the_list(registered, repo):
    created = _run(_todo(repo, "گزارش دانشگاه"))
    title, body, _buttons = _run(
        registered._delete_action(None, f"{created.id}:{created.version}", 0)
    )
    assert title == "Todo"
    assert f"Deleted todo #{created.id}" in body
    assert _run(repo.get_task(OWNER, created.id)) is None


def test_delete_action_refuses_a_stale_version(registered, repo):
    created = _run(_todo(repo, "گزارش دانشگاه"))
    _title, body, _buttons = _run(
        registered._delete_action(None, f"{created.id}:{created.version + 3}", 0)
    )
    assert "stale" in body
    assert _run(repo.get_task(OWNER, created.id)) is not None


def test_actions_reject_malformed_arguments(registered):
    for extra in ("", "not-an-id", "0:1", "1:0", "1"):
        _title, body, _buttons = _run(registered._complete_action(None, extra, 0))
        assert "Invalid action arguments" in body, extra


# ── inputs ───────────────────────────────────────────────────────────────────


def test_the_add_input_creates_a_todo_from_the_typed_title(registered, repo):
    _run(registered._add_input_handler("  گزارش دانشگاه  ", 10, 20, 0, 0))
    stored = _run(repo.list_tasks(OWNER))
    assert [t.label for t in stored] == ["گزارش دانشگاه"]
    assert stored[0].schedule_type == "todo" and stored[0].actions == []


def test_the_add_input_refuses_an_empty_title(registered, repo):
    _run(registered._add_input_handler("   ", 10, 20, 0, 0))
    assert _run(repo.list_tasks(OWNER)) == []


def test_the_edit_input_renames_under_the_version_that_opened_it(registered, repo):
    from backend.helper import input_state

    created = _run(_todo(repo, "old title"))
    input_state.set_pending(
        OWNER,
        panel_id="todo_task",
        handler=registered._edit_input_handler,
        chat_id=10,
        prompt="",
        extra=f"{created.id}:{created.version}",
    )
    _run(registered._edit_input_handler("new title", 10, 20, 0, 0))
    stored = _run(repo.get_task(OWNER, created.id))
    assert stored.label == "new title" and stored.version == created.version + 1

    # The SAME (now stale) version is refused: nothing is overwritten.
    input_state.set_pending(
        OWNER,
        panel_id="todo_task",
        handler=registered._edit_input_handler,
        chat_id=10,
        prompt="",
        extra=f"{created.id}:{created.version}",
    )
    _run(registered._edit_input_handler("overwritten", 10, 20, 0, 0))
    assert _run(repo.get_task(OWNER, created.id)).label == "new title"


def test_the_edit_input_refuses_a_blank_title_and_an_expired_edit(registered, repo):
    from backend.helper import input_state

    created = _run(_todo(repo, "old title"))
    input_state.set_pending(
        OWNER,
        panel_id="todo_task",
        handler=registered._edit_input_handler,
        chat_id=10,
        prompt="",
        extra=f"{created.id}:{created.version}",
    )
    _run(registered._edit_input_handler("   ", 10, 20, 0, 0))
    assert _run(repo.get_task(OWNER, created.id)).label == "old title"

    input_state.clear_pending(OWNER)
    _run(registered._edit_input_handler("new title", 10, 20, 0, 0))
    assert _run(repo.get_task(OWNER, created.id)).label == "old title"
