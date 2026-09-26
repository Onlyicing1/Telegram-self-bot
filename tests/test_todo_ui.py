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
from backend.ai.task_management import TaskManagementService
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


# ── the ordered steps (multi-step todos) ─────────────────────────────────────

STEPS = ("جمع‌آوری منابع", "نوشتن گزارش", "آماده‌سازی ارائه")


def _steps_todo(repo, label="پروژه دانشگاه", steps=STEPS, owner=OWNER):
    return _run(
        TaskCreationService(repo, owner).create_todo(
            label,
            "Asia/Tehran",
            datetime(2026, 9, 27, 9, 0, tzinfo=timezone.utc),
            steps=steps,
        )
    )


def _steps_of(repo, task_id):
    return _run(TaskManagementService(repo, OWNER).list_steps(task_id))


def test_registers_the_step_panel_actions_and_inputs(registered):
    from backend.helper.panel_registry import registry as get_registry
    from backend.helper.panels import get_action, get_input

    assert get_registry().get_handler("todo_steps") is not None
    for action_id in (
        "todo_step_complete",
        "todo_step_reopen",
        "todo_step_delete",
        "todo_complete_all",
    ):
        assert get_action(action_id) is not None, action_id
    assert get_input("todo_steps", "add") is not None
    assert get_input("todo_steps", "edit") is not None


def test_detail_panel_shows_progress_and_opens_the_steps_panel(registered, repo):
    task = _steps_todo(repo)
    _title, body, buttons = _run(registered._todo_detail_panel(None, str(task.id)))
    assert "Progress: 0 / 3 steps completed" in body
    assert f"Next: {STEPS[0]}" in body
    datas = _datas(buttons)
    assert f"panel:todo_steps:{task.id}" in datas
    # A todo with remaining steps is completed through the labelled button.
    assert f"action:todo_complete_all:{task.id}:{task.version}" in datas
    assert f"action:todo_complete:{task.id}:{task.version}" not in datas
    assert any("Steps (0/3)" in text for text in _texts(buttons))

    # Without steps the plain completion button stays the Part 1 one.
    plain = _run(_todo(repo, "simple"))
    _title, _body, buttons = _run(registered._todo_detail_panel(None, str(plain.id)))
    datas = _datas(buttons)
    assert f"action:todo_complete:{plain.id}:{plain.version}" in datas
    assert f"input:todo_steps:add:{plain.id}" in datas


def test_steps_panel_renders_the_list_progress_and_per_step_actions(registered, repo):
    task = _steps_todo(repo)
    steps = _steps_of(repo, task.id)
    _run(registered._step_complete_action(None, f"{steps[0].id}:{steps[0].version}", 0))

    title, body, buttons = _run(registered._todo_steps_panel(None, str(task.id)))
    assert title == f"Steps of Todo #{task.id}"
    assert "Progress: 1 / 3 steps completed" in body
    assert f"✓ 1. {STEPS[0]}" in body and f"○ 2. {STEPS[1]}" in body
    assert f"Next: 2. {STEPS[1]}" in body
    datas = _datas(buttons)
    assert f"input:todo_steps:add:{task.id}" in datas
    assert f"panel:todo_task:{task.id}" in datas
    # The completed step offers Reopen, the remaining ones offer Complete.
    current = _steps_of(repo, task.id)
    assert f"action:todo_step_reopen:{current[0].id}:{current[0].version}" in datas
    assert f"action:todo_step_complete:{current[1].id}:{current[1].version}" in datas
    assert f"input:todo_steps:edit:{current[1].id}:{current[1].version}" in datas
    assert f"action:todo_step_delete:{current[1].id}:{current[1].version}" in datas


def test_the_step_panel_refuses_a_foreign_or_unknown_todo(registered, repo):
    foreign = _steps_todo(repo, "other owner", owner=OTHER)
    _title, body, _buttons = _run(registered._todo_steps_panel(None, str(foreign.id)))
    assert "not found" in body
    _title, body, _buttons = _run(registered._todo_steps_panel(None, "not-an-id"))
    assert "Invalid todo id" in body


def test_step_complete_reopen_and_delete_actions_mutate_and_refresh(registered, repo):
    task = _steps_todo(repo)
    steps = _steps_of(repo, task.id)
    first = steps[0]

    _title, body, _buttons = _run(
        registered._step_complete_action(None, f"{first.id}:{first.version}", 0)
    )
    assert "completed" in body.lower()
    assert "1 / 3 steps completed" in body
    assert _steps_of(repo, task.id)[0].status == "completed"

    updated = _steps_of(repo, task.id)[0]
    _title, body, _buttons = _run(
        registered._step_reopen_action(None, f"{updated.id}:{updated.version}", 0)
    )
    assert "reopened" in body.lower()
    assert _steps_of(repo, task.id)[0].status == "active"

    reopened = _steps_of(repo, task.id)[0]
    _title, body, _buttons = _run(
        registered._step_delete_action(None, f"{reopened.id}:{reopened.version}", 0)
    )
    assert "Step removed" in body
    assert [s.title for s in _steps_of(repo, task.id)] == [STEPS[1], STEPS[2]]
    assert _run(repo.get_task(OWNER, task.id)) is not None


def test_the_step_actions_fail_closed_on_a_stale_version(registered, repo):
    task = _steps_todo(repo)
    step = _steps_of(repo, task.id)[0]
    _run(registered._step_complete_action(None, f"{step.id}:{step.version}", 0))
    assert _steps_of(repo, task.id)[0].status == "completed"

    # The SAME (now stale) version can neither complete nor reopen nor delete.
    for handler in (
        registered._step_complete_action,
        registered._step_reopen_action,
        registered._step_delete_action,
    ):
        _title, body, _buttons = _run(handler(None, f"{step.id}:{step.version}", 0))
        assert "stale" in body.lower() or "changed" in body.lower(), body
    assert len(_steps_of(repo, task.id)) == 3
    assert _steps_of(repo, task.id)[0].status == "completed"

    for extra in ("", "not-an-id", "0:1", "1:0", "1"):
        _title, body, _buttons = _run(
            registered._step_complete_action(None, extra, 0)
        )
        assert "Invalid action arguments" in body, extra


def test_complete_all_finishes_the_todo_and_its_remaining_steps(registered, repo):
    task = _steps_todo(repo)
    steps = _steps_of(repo, task.id)
    _run(registered._step_complete_action(None, f"{steps[0].id}:{steps[0].version}", 0))

    title, body, buttons = _run(
        registered._complete_all_action(None, f"{task.id}:{task.version}", 0)
    )
    assert title == f"Todo #{task.id}"
    assert "completed with its remaining steps" in body
    assert "Progress: 3 / 3 steps completed" in body
    stored = _run(repo.get_task(OWNER, task.id))
    assert stored.status == "completed"
    assert all(s.status == "completed" for s in _steps_of(repo, task.id))
    # A completed todo offers Reopen and no step-add affordance.
    datas = _datas(buttons)
    assert any(data.startswith("action:todo_reopen") for data in datas)
    _title, _body, step_buttons = _run(
        registered._todo_steps_panel(None, str(task.id))
    )
    assert not any(data.startswith("input:todo_steps:add") for data in _datas(step_buttons))

    stale = _run(registered._complete_all_action(None, f"{task.id}:{task.version}", 0))
    assert "stale" in stale[1].lower()


def test_the_add_step_input_appends_one_or_several_steps_at_once(registered, repo):
    from backend.helper import input_state

    task = _steps_todo(repo, steps=())
    input_state.set_pending(
        OWNER,
        panel_id="todo_steps",
        handler=registered._steps_add_input_handler,
        chat_id=10,
        prompt="",
        extra=f"add:{task.id}",
    )
    _run(
        registered._steps_add_input_handler(
            "  جمع‌آوری منابع \n نوشتن گزارش \n\n آماده‌سازی ارائه  ", 10, 20, 0, 0
        )
    )
    steps = _steps_of(repo, task.id)
    assert [s.title for s in steps] == list(STEPS)
    assert [s.position for s in steps] == [1, 2, 3]

    # A blank input changes nothing, and the todo keeps what it has.
    input_state.set_pending(
        OWNER,
        panel_id="todo_steps",
        handler=registered._steps_add_input_handler,
        chat_id=10,
        prompt="",
        extra=f"add:{task.id}",
    )
    _run(registered._steps_add_input_handler("   \n  ", 10, 20, 0, 0))
    assert len(_steps_of(repo, task.id)) == 3

    # An expired input (no pending state) is refused instead of guessing a todo.
    input_state.clear_pending(OWNER)
    _run(registered._steps_add_input_handler("late", 10, 20, 0, 0))
    assert len(_steps_of(repo, task.id)) == 3


def test_the_step_edit_input_renames_under_the_version_it_opened_with(registered, repo):
    from backend.helper import input_state

    task = _steps_todo(repo)
    step = _steps_of(repo, task.id)[0]
    input_state.set_pending(
        OWNER,
        panel_id="todo_steps",
        handler=registered._steps_edit_input_handler,
        chat_id=10,
        prompt="",
        extra=f"edit:{step.id}:{step.version}",
    )
    _run(registered._steps_edit_input_handler("  جمع‌آوری منابع ترم  ", 10, 20, 0, 0))
    steps = _steps_of(repo, task.id)
    assert steps[0].title == "جمع‌آوری منابع ترم"
    assert steps[0].version == step.version + 1
    # Neither the todo nor the other steps moved.
    assert _run(repo.get_task(OWNER, task.id)).label == "پروژه دانشگاه"
    assert [s.title for s in steps[1:]] == [STEPS[1], STEPS[2]]

    # The SAME (now stale) version is refused: nothing is overwritten.
    input_state.set_pending(
        OWNER,
        panel_id="todo_steps",
        handler=registered._steps_edit_input_handler,
        chat_id=10,
        prompt="",
        extra=f"edit:{step.id}:{step.version}",
    )
    _run(registered._steps_edit_input_handler("overwritten", 10, 20, 0, 0))
    assert _steps_of(repo, task.id)[0].title == "جمع‌آوری منابع ترم"

    # A blank title is refused too.
    input_state.set_pending(
        OWNER,
        panel_id="todo_steps",
        handler=registered._steps_edit_input_handler,
        chat_id=10,
        prompt="",
        extra=f"edit:{step.id}:{step.version}",
    )
    _run(registered._steps_edit_input_handler("   ", 10, 20, 0, 0))
    assert _steps_of(repo, task.id)[0].title == "جمع‌آوری منابع ترم"


def test_the_todo_detail_never_shows_a_foreign_progress_line(registered, repo):
    foreign = _steps_todo(repo, "other owner", owner=OTHER)
    _title, body, _buttons = _run(registered._todo_detail_panel(None, str(foreign.id)))
    assert "not found" in body
    assert "Progress" not in body
