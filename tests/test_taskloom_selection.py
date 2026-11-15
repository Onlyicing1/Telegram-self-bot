"""G1-A reusable interaction grammar — Taskloom selection and bulk actions.

The contract under test:

* the existing Stash selection store is owner- and feature-scoped without
  changing its default (Stash) behavior, cap or expiry;
* Taskloom has a multi-select view whose rows toggle exact task ids and whose
  callbacks carry the task version rendered on that page;
* selection survives pagination, has an explicit clear path, and is never
  addressed by row position or display label;
* bulk actions are offered only when the selected set can accept them, every
  target is re-read through TaskManagementService, and each result is reported
  honestly — including stale, missing and foreign targets;
* destructive bulk delete has a dedicated confirmation with the exact scope,
  a cancel action, red confirmation styling, and CAS revalidation.

Everything here is offline application behavior against the in-memory task
repository and fake callback context. No live Telegram, Supabase or Render
operation is performed.
"""
from __future__ import annotations

import pytest

from backend.ai.database.task_repository import InMemoryTaskRepository
from backend.ai.task_management import TaskManagementService
from backend.bot.handlers import taskloom
from backend.helper import inline_engine, selection_state, symbols as sym

OWNER = 4242
OTHER = 9999
SCOPE = "taskloom"


async def _make_task(repo, label="write hello", *, owner=OWNER, status="active",
               schedule_type="interval"):
    schedule = {"interval_seconds": 60} if schedule_type == "interval" else {}
    return await repo.create_task(
            owner,
            {
                "label": label,
                "schedule_type": schedule_type,
                "schedule": schedule,
                "timezone": "UTC",
                "actions": [{"name": "send_message", "arguments": {"text": "hello"}}],
                "notification_destination": {"kind": "saved_messages"},
                "status": status,
                "next_run_at": None,
            },
        )


def _datas(buttons) -> list[str]:
    result = []
    for row in buttons or []:
        for button in row:
            data = getattr(button, "data", None)
            if isinstance(data, bytes):
                data = data.decode("utf-8")
            result.append(str(data))
    return result


def _texts(buttons) -> list[str]:
    return [str(getattr(button, "text", button)) for row in buttons or [] for button in row]


def _bg(button, name: str):
    return getattr(getattr(button, "style", None), f"bg_{name}", None)


def _button(buttons, data: str):
    for row in buttons or []:
        for button in row:
            raw = getattr(button, "data", None)
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8")
            if raw == data:
                return button
    return None


@pytest.fixture()
def repo():
    return InMemoryTaskRepository()


@pytest.fixture()
def registered(repo, monkeypatch):
    import backend.ai.task_management as tm

    real = tm.TaskManagementService

    def factory(task_repo, owner_id):
        return real(repo, owner_id)

    monkeypatch.setattr(tm, "TaskManagementService", factory)
    inline_engine.set_owner_id(OWNER)
    selection_state.clear_all()
    taskloom.register(client=None, owner_id=OWNER, tz_str="UTC")
    yield taskloom
    selection_state.clear_all()


# ── 1. The existing store is generalized, not replaced ─────────────────────

def test_selection_store_isolates_owner_and_feature_scope():
    selection_state.toggle(OWNER, "1", "Task one", SCOPE, version=3)
    selection_state.toggle(OWNER, "S0001", "Stash item")

    assert selection_state.codes(OWNER, SCOPE) == ("1",)
    assert selection_state.entries(OWNER, SCOPE) == (("1", "Task one", 3),)
    assert selection_state.codes(OWNER) == ("S0001",)
    assert selection_state.entries(OWNER)[0][2] is None
    assert selection_state.codes(OTHER, SCOPE) == ()

    assert selection_state.clear(OWNER, SCOPE) == ("1",)
    assert selection_state.codes(OWNER) == ("S0001",)
    assert selection_state.codes(OWNER, SCOPE) == ()


def test_selection_store_capacity_expiry_and_clear_are_per_scope(monkeypatch):
    now = 1000.0
    monkeypatch.setattr(selection_state, "_now", lambda: now)
    for index in range(1, selection_state.MAX_SELECTION + 1):
        assert selection_state.toggle(OWNER, str(index), "task", SCOPE)[0] is True

    selected, reason = selection_state.toggle(
        OWNER, str(selection_state.MAX_SELECTION + 1), "overflow", SCOPE
    )
    assert selected is False
    assert "at most" in reason
    assert selection_state.count(OWNER, SCOPE) == selection_state.MAX_SELECTION

    selection_state.toggle(OWNER, "S0001", "stash")
    now += selection_state.SELECTION_TTL_SECONDS + 1
    assert selection_state.codes(OWNER, SCOPE) == ()
    assert selection_state.codes(OWNER) == ()
    assert selection_state.current_page(OWNER, SCOPE) == 1


# ── 2. Taskloom list → select mode, exact toggles and visible state ────────

@pytest.mark.asyncio
async def test_list_keeps_detail_navigation_and_offers_select(registered, repo):
    task = await _make_task(repo)
    _title, _body, buttons = await registered._taskloom_panel(None, "")
    datas = _datas(buttons)
    assert f"panel:taskloom_task:{task.id}" in datas
    assert "panel:taskloom:select" in datas


@pytest.mark.asyncio
async def test_toggle_selects_and_deselects_exact_task_with_visible_check(registered, repo):
    task = await _make_task(repo, "write hello")
    _title, body, buttons = await registered._taskloom_panel(None, "select")
    assert "0 selected" in body
    assert any(text.startswith(sym.SELECT_EMPTY) for text in _texts(buttons))

    _title, body, buttons = await registered._taskloom_toggle_action(
        None, f"{task.id}:{task.version}", 1
    )
    assert selection_state.entries(OWNER, SCOPE) == (
        (str(task.id), f"Task {task.id}: write hello", task.version),
    )
    assert "1 selected" in body
    assert any(text.startswith(sym.SELECT_MARKED) for text in _texts(buttons))

    _title, body, buttons = await registered._taskloom_toggle_action(
        None, f"{task.id}:{task.version}", 1
    )
    assert selection_state.codes(OWNER, SCOPE) == ()
    assert "0 selected" in body
    assert not any(text.startswith(sym.SELECT_MARKED) for text in _texts(buttons))


@pytest.mark.asyncio
async def test_selection_survives_pagination_and_marks_the_original_task(registered, repo):
    tasks = [await _make_task(repo, f"task {index}") for index in range(6)]
    await registered._taskloom_panel(None, "select")
    await registered._taskloom_toggle_action(
        None, f"{tasks[0].id}:{tasks[0].version}", 1
    )

    _title, page2_body, page2_buttons = await registered._taskloom_panel(
        None, "select:page:2"
    )
    assert "1 selected" in page2_body
    assert f"action:taskloom_toggle:{tasks[4].id}:{tasks[4].version}" in _datas(page2_buttons)
    assert f"action:taskloom_toggle:{tasks[0].id}:{tasks[0].version}" not in _datas(page2_buttons)

    await registered._taskloom_toggle_action(
        None, f"{tasks[4].id}:{tasks[4].version}", 1
    )
    assert selection_state.codes(OWNER, SCOPE) == (str(tasks[0].id), str(tasks[4].id))

    _title, _page1_body, page1_buttons = await registered._taskloom_panel(
        None, "select:page:1"
    )
    marked = [text for text in _texts(page1_buttons) if text.startswith(sym.SELECT_MARKED)]
    assert any(f"Task {tasks[0].id}:" in text for text in marked)


@pytest.mark.asyncio
async def test_clear_selection_returns_to_the_browse_view(registered, repo):
    task = await _make_task(repo)
    await registered._taskloom_panel(None, "select")
    await registered._taskloom_toggle_action(None, f"{task.id}:{task.version}", 1)
    assert selection_state.count(OWNER, SCOPE) == 1

    _title, _body, buttons = await registered._taskloom_panel(None, "select:cancel")
    assert selection_state.codes(OWNER, SCOPE) == ()
    datas = _datas(buttons)
    assert f"panel:taskloom_task:{task.id}" in datas
    assert "panel:taskloom:select" in datas


def test_registers_selection_actions(registered):
    from backend.helper.panels import get_action

    assert get_action("taskloom_toggle") is taskloom._taskloom_toggle_action
    assert get_action("taskloom_bulk") is taskloom._taskloom_bulk_action
    assert get_action("taskloom_bulk_delete") is taskloom._taskloom_bulk_delete_confirm


@pytest.mark.asyncio
async def test_exact_task_id_is_the_authority_not_the_duplicate_label(registered, repo):
    first = await _make_task(repo, "same label")
    second = await _make_task(repo, "same label")
    await registered._taskloom_panel(None, "select")
    await registered._taskloom_toggle_action(None, f"{first.id}:{first.version}", 1)

    assert selection_state.codes(OWNER, SCOPE) == (str(first.id),)
    await registered._taskloom_bulk_action(None, "pause", 1)

    assert (await repo.get_task(OWNER, first.id)).status == "paused"
    assert (await repo.get_task(OWNER, second.id)).status == "active"


@pytest.mark.asyncio
async def test_selection_callbacks_stay_within_telegram_budget(registered, repo):
    tasks = [await _make_task(repo, f"task {index}") for index in range(6)]
    await registered._taskloom_panel(None, "select")
    await registered._taskloom_toggle_action(
        None, f"{tasks[0].id}:{tasks[0].version}", 1
    )
    _title, _body, buttons = await registered._taskloom_panel(None, "select:back")
    for data in _datas(buttons):
        assert len(data.encode("utf-8")) <= 64, data
        assert data.startswith(("panel:", "action:")), data


# ── 3. Stale, missing and foreign exact targets fail closed ────────────────

@pytest.mark.asyncio
async def test_toggle_rejects_missing_foreign_and_stale_targets(registered, repo):
    mine = await _make_task(repo, "mine")
    foreign = await _make_task(repo, "theirs", owner=OTHER)
    await registered._taskloom_panel(None, "select")

    _title, body, _buttons = await registered._taskloom_toggle_action(
        None, f"{foreign.id}:{foreign.version}", 1
    )
    assert "not one of your tasks" in body
    assert selection_state.codes(OWNER, SCOPE) == ()

    _title, body, _buttons = await registered._taskloom_toggle_action(
        None, f"{mine.id}:{mine.version + 1}", 1
    )
    assert "changed since this page was shown" in body
    assert selection_state.codes(OWNER, SCOPE) == ()

    _title, body, _buttons = await registered._taskloom_toggle_action(
        None, "31337:1", 1
    )
    assert "not one of your tasks" in body
    assert selection_state.codes(OWNER, SCOPE) == ()


@pytest.mark.asyncio
async def test_stale_callback_cannot_act_on_a_replacement_row(registered, repo):
    first = await _make_task(repo, "first")
    await registered._taskloom_panel(None, "select")
    old_callback = f"{first.id}:{first.version}"
    # The task changes after the page was rendered. The exact id still exists,
    # but the callback's version no longer identifies the rendered state.
    await TaskManagementService(repo, OWNER).pause(first.id, first.version)
    _title, body, _buttons = await registered._taskloom_toggle_action(None, old_callback, 1)
    assert "changed since this page was shown" in body
    assert selection_state.codes(OWNER, SCOPE) == ()

    # And the stale old-page delete callback is refused rather than deleting
    # whatever now occupies the row.
    selection_state.toggle(OWNER, str(first.id), "old", SCOPE, version=first.version)
    _title, body, _buttons = await registered._taskloom_apply_bulk(OWNER, "delete")
    assert "changed since selection" in body
    assert await repo.get_task(OWNER, first.id) is not None


# ── 4. Contextual bulk actions and honest per-item results ─────────────────

@pytest.mark.asyncio
async def test_no_selection_means_no_bulk_actions(registered, repo):
    await _make_task(repo)
    _title, body, buttons = await registered._taskloom_panel(None, "select")
    assert "0 selected" in body
    assert not any(data.startswith("action:taskloom_bulk") for data in _datas(buttons))

    _title, body, _buttons = await registered._taskloom_bulk_action(None, "pause", 1)
    assert "No tasks are selected" in body


@pytest.mark.asyncio
async def test_only_actions_valid_for_the_selected_set_are_shown(registered, repo):
    active = await _make_task(repo, "active", status="active")
    paused = await _make_task(repo, "paused", status="paused")
    completed = await _make_task(repo, "completed", status="completed")

    async def select(task):
        await registered._taskloom_panel(None, "select")
        await registered._taskloom_toggle_action(None, f"{task.id}:{task.version}", 1)
        _title, _body, buttons = await registered._taskloom_panel(None, "select:back")
        return _datas(buttons)

    active_datas = await select(active)
    assert "action:taskloom_bulk:pause" in active_datas
    assert "action:taskloom_bulk:complete" in active_datas
    assert "action:taskloom_bulk:delete" in active_datas
    assert "action:taskloom_bulk:resume" not in active_datas

    paused_datas = await select(paused)
    assert "action:taskloom_bulk:resume" in paused_datas
    assert "action:taskloom_bulk:pause" not in paused_datas

    completed_datas = await select(completed)
    assert "action:taskloom_bulk:delete" in completed_datas
    assert "action:taskloom_bulk:pause" not in completed_datas
    assert "action:taskloom_bulk:resume" not in completed_datas
    assert "action:taskloom_bulk:complete" not in completed_datas


@pytest.mark.asyncio
async def test_bulk_pause_reports_each_item_and_clears_selection(registered, repo):
    active = await _make_task(repo, "active")
    completed = await _make_task(repo, "completed", status="completed")
    await registered._taskloom_panel(None, "select")
    await registered._taskloom_toggle_action(None, f"{active.id}:{active.version}", 1)
    await registered._taskloom_toggle_action(None, f"{completed.id}:{completed.version}", 1)

    _title, body, _buttons = await registered._taskloom_bulk_action(None, "pause", 1)

    assert "Paused 1 of 2 selected tasks" in body
    assert f"Task {completed.id}:" in body
    assert "status is completed; not paused" in body
    assert (await repo.get_task(OWNER, active.id)).status == "paused"
    assert (await repo.get_task(OWNER, completed.id)).status == "completed"
    assert selection_state.codes(OWNER, SCOPE) == ()


@pytest.mark.asyncio
async def test_bulk_never_reports_success_without_service_confirmation(registered, repo, monkeypatch):
    task = await _make_task(repo, "active")
    await registered._taskloom_panel(None, "select")
    await registered._taskloom_toggle_action(None, f"{task.id}:{task.version}", 1)

    async def refused(_self, _task_id, _version):
        return None

    monkeypatch.setattr(TaskManagementService, "complete", refused)
    _title, body, _buttons = await registered._taskloom_bulk_action(None, "complete", 1)

    assert "Completed 0 of 1 selected tasks" in body
    assert "service did not confirm" in body
    assert (await repo.get_task(OWNER, task.id)).status == "active"


# ── 5. Destructive confirmation ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_delete_has_a_dedicated_confirmation_with_exact_scope(registered, repo):
    first = await _make_task(repo, "first")
    second = await _make_task(repo, "second")
    await registered._taskloom_panel(None, "select")
    await registered._taskloom_toggle_action(None, f"{first.id}:{first.version}", 1)
    await registered._taskloom_toggle_action(None, f"{second.id}:{second.version}", 1)

    title, body, buttons = await registered._taskloom_bulk_action(None, "delete", 1)

    assert title == "Delete Tasks"
    assert "2 selected tasks" in body
    assert f"Task {first.id}:" in body and f"Task {second.id}:" in body
    assert "This cannot be undone" in body
    assert "action:taskloom_bulk_delete:confirm" in _datas(buttons)
    confirm = _button(buttons, "action:taskloom_bulk_delete:confirm")
    assert _bg(confirm, "danger") is True
    assert "panel:taskloom:select:back" in _datas(buttons)


@pytest.mark.asyncio
async def test_delete_cancel_performs_no_mutation(registered, repo):
    first = await _make_task(repo, "first")
    second = await _make_task(repo, "second")
    await registered._taskloom_panel(None, "select")
    await registered._taskloom_toggle_action(None, f"{first.id}:{first.version}", 1)
    await registered._taskloom_toggle_action(None, f"{second.id}:{second.version}", 1)
    await registered._taskloom_bulk_action(None, "delete", 1)
    await registered._taskloom_panel(None, "select:back")

    assert await repo.get_task(OWNER, first.id) is not None
    assert await repo.get_task(OWNER, second.id) is not None
    assert selection_state.count(OWNER, SCOPE) == 2


@pytest.mark.asyncio
async def test_delete_revalidates_and_reports_partial_success(registered, repo):
    first = await _make_task(repo, "first")
    stale = await _make_task(repo, "stale")
    foreign = await _make_task(repo, "foreign", owner=OTHER)
    await registered._taskloom_panel(None, "select")
    await registered._taskloom_toggle_action(None, f"{first.id}:{first.version}", 1)
    await registered._taskloom_toggle_action(None, f"{stale.id}:{stale.version}", 1)
    # A crafted/foreign selection entry must still be rejected by the service.
    selection_state.toggle(OWNER, str(foreign.id), "foreign", SCOPE, version=foreign.version)
    # Make one target stale after it was selected.
    await TaskManagementService(repo, OWNER).pause(stale.id, stale.version)

    _title, body, _buttons = await registered._taskloom_bulk_delete_confirm(
        None, "confirm", 1
    )

    assert "Deleted 1 of 3 selected tasks" in body
    assert "changed since selection" in body
    assert "not found for this owner" in body
    assert await repo.get_task(OWNER, first.id) is None
    assert await repo.get_task(OWNER, stale.id) is not None
    assert await repo.get_task(OTHER, foreign.id) is not None
    assert selection_state.codes(OWNER, SCOPE) == ()


@pytest.mark.asyncio
async def test_delete_confirmation_token_is_required(registered, repo):
    task = await _make_task(repo)
    await registered._taskloom_panel(None, "select")
    await registered._taskloom_toggle_action(None, f"{task.id}:{task.version}", 1)

    _title, body, _buttons = await registered._taskloom_bulk_delete_confirm(
        None, "", 1
    )
    assert "Confirmation is required" in body
    assert await repo.get_task(OWNER, task.id) is not None


# ── 6. Stash's destructive confirm uses the reserved red style ─────────────

def test_stash_bulk_delete_confirm_is_danger():
    from backend.bot.handlers import retrieve

    selection_state.toggle(OWNER, "S0001", "Stash item")
    _title, _body, buttons = retrieve._bulk_review(OWNER, retrieve._BULK_DELETE)
    confirm = _button(buttons, "action:retrieve_bulk_delete:confirm")
    assert confirm is not None
    assert _bg(confirm, "danger") is True
