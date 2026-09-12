"""Taskloom editor (Edit) UX tests — part A of the editor/list reliability repair.

Contract under test:

- "✎ Edit" opens the SAME wizard draft in EDIT mode (``editing_task_id`` set),
  on an explicit EDIT HUB, prefilled from the STORED definition only.
- The editor owns its navigation: Back is the editor's own previous step (each
  field step returns to the hub), "Cancel edit" DISCARDS the draft and returns
  to the edited task's detail view, "Close" closes the panel. No edit step ever
  emits the shared ``panel:_nav:back`` stack pop.
- An input submission keeps the current step, the draft, and every unrelated
  field, and never replaces the editor with a root panel.
- Saving goes through the SAME CAS update (``update_definition``), bumps the
  version exactly once, and returns to the edited task's detail view. A stale
  save keeps the whole draft in an actionable state.
- Back and Cancel never modify durable state, and a definition edit is refused
  when this editor cannot faithfully reproduce it.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from backend.ai.database.task_repository import InMemoryTaskRepository

OWNER = 7311
OTHER = 99
BASE = datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc)
SOURCE = "Ayanami Rei"


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _rows(buttons):
    rows = []
    for row in buttons:
        rendered = row if isinstance(row, list) else [row]
        pairs = []
        for button in rendered:
            raw = getattr(button, "data", button)
            data = raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)
            pairs.append((str(getattr(button, "text", "")), data))
        rows.append(pairs)
    return rows


def _data(buttons) -> list[str]:
    return [value for row in _rows(buttons) for _, value in row]


def _labels(buttons) -> list[str]:
    return [text for row in _rows(buttons) for text, _ in row]


def _message_task_data(**overrides):
    value = {
        "label": "Say hello",
        "schedule_type": "interval",
        "schedule": {"seconds": 300},
        "timezone": "Asia/Tehran",
        "next_run_at": BASE,
        "actions": [{"name": "send_message", "arguments": {"text": "hello", "font": "script"}}],
        "notification_destination": {"chat_id": 5},
        "ai_instruction": None,
    }
    value.update(overrides)
    return value


@pytest.fixture()
def editor(monkeypatch):
    """Taskloom + a real in-memory repository behind the monkeypatched manager."""
    import backend.ai.database.manager as manager
    import backend.bot.handlers.taskloom as handler
    from backend.helper import inline_engine

    repo = InMemoryTaskRepository()

    class _Manager:
        task = repo

    monkeypatch.setattr(manager, "get_repository_manager", lambda: _Manager())
    inline_engine.set_owner_id(OWNER)
    handler._drafts.clear()
    handler.register(client=None, owner_id=OWNER, tz_str="Asia/Tehran")
    yield handler, repo
    handler._drafts.clear()


def _create_bio(wizard, *, source=SOURCE, show_source=False, interval="2", max_length="59"):
    """Create a Bio task through the REAL wizard path (no direct DB seeding)."""
    _run(wizard._wizard_action(None, "set:action:bio", 1))
    _run(wizard._wizard_action(None, "set:mode:ai", 1))
    _run(wizard._wizard_input_handler("source")(source, 1, 2, 0, 0))
    _run(wizard._wizard_action(None, "set:lang:en", 1))
    if max_length:
        _run(wizard._wizard_input_handler("maxlen")(max_length, 1, 2, 0, 0))
    if show_source:
        _run(wizard._wizard_action(None, "set:show_source:1", 1))
    _run(wizard._wizard_input_handler("interval")(interval, 1, 2, 0, 0))
    return _run(wizard._wizard_action(None, "create", 1))


def _open_edit(wizard, task_id):
    return _run(wizard._wizard_panel(None, f"edit:{task_id}"))


# ── the editor opens in EDIT mode on the hub ────────────────────────────────

def test_edit_opens_on_the_hub_in_editor_mode(editor):
    taskloom, repo = editor
    task = _run(repo.create_task(OWNER, _message_task_data()))

    title, body, _buttons_ = _open_edit(taskloom, task.id)
    draft = taskloom._draft(OWNER)

    assert title == f"✎ Edit task #{task.id}"
    assert draft.step == taskloom.STEP_EDIT
    assert draft.editing_task_id == task.id
    assert draft.editing_version == task.version
    assert f"✎ Editing task #{task.id}" in body
    assert "What do you want to edit?" in body


def test_edit_hub_rows_jump_straight_to_the_named_field(editor):
    taskloom, repo = editor
    task = _run(repo.create_task(OWNER, _message_task_data()))

    _title, _body, buttons = _open_edit(taskloom, task.id)
    data = _data(buttons)

    assert f"action:taskloom_wizard:step:{taskloom.STEP_DETAILS}" in data
    assert f"action:taskloom_wizard:step:{taskloom.STEP_SCHEDULE}" in data
    assert f"action:taskloom_wizard:step:{taskloom.STEP_REVIEW}" in data
    assert "action:taskloom_wizard:reload" in data
    # The hub never offers the creation-only first/wizard steps.
    assert f"action:taskloom_wizard:step:{taskloom.STEP_ACTION}" not in data
    assert f"action:taskloom_wizard:step:{taskloom.STEP_CONTENT}" not in data


def test_edit_prefills_every_field_from_the_stored_definition(editor):
    taskloom, repo = editor
    _create_bio(taskloom, source=SOURCE, interval="2")
    task = _run(repo.list_tasks(OWNER))[0]

    _open_edit(taskloom, task.id)
    draft = taskloom._draft(OWNER)

    assert draft.action == "bio"
    assert draft.content_mode == taskloom.AI_MODE
    assert draft.source == SOURCE
    assert draft.language == "en"
    assert draft.max_length == 59
    assert draft.show_source is False
    assert draft.schedule_type == "interval"
    assert draft.interval_minutes == 2
    assert draft.timezone == "Asia/Tehran"
    assert draft.label == task.label
    assert draft.text == ""


def test_edit_hub_summarises_the_definition_being_edited(editor):
    taskloom, repo = editor
    _create_bio(taskloom, source=SOURCE, show_source=True)
    task = _run(repo.list_tasks(OWNER))[0]

    _title, body, _buttons_ = _open_edit(taskloom, task.id)

    assert "**Action:** Update Bio" in body
    assert f"source {SOURCE}" in body
    assert "show source Yes" in body
    assert "Every 2 minutes" in body


def test_edit_hub_shows_the_font_of_a_static_message_task(editor):
    taskloom, repo = editor
    task = _run(repo.create_task(OWNER, _message_task_data()))

    _title, body, _buttons_ = _open_edit(taskloom, task.id)

    assert "✍ `hello`" in body
    assert "**Font:** script" in body


# ── Back is the editor's own previous step ─────────────────────────────────

def test_back_from_every_editor_step_returns_to_the_hub(editor):
    taskloom, repo = editor
    task = _run(repo.create_task(OWNER, _message_task_data()))
    _open_edit(taskloom, task.id)

    for step in (taskloom.STEP_DETAILS, taskloom.STEP_SCHEDULE, taskloom.STEP_REVIEW):
        _run(taskloom._wizard_action(None, f"step:{step}", 1))
        assert taskloom._draft(OWNER).step == step
        _run(taskloom._wizard_action(None, "step:edit", 1))
        assert taskloom._draft(OWNER).step == taskloom.STEP_EDIT


def test_back_button_of_each_editor_step_targets_the_hub(editor):
    taskloom, repo = editor
    task = _run(repo.create_task(OWNER, _message_task_data()))
    _open_edit(taskloom, task.id)

    for step in (taskloom.STEP_DETAILS, taskloom.STEP_SCHEDULE, taskloom.STEP_REVIEW):
        _run(taskloom._wizard_action(None, f"step:{step}", 1))
        _title, _body, buttons = taskloom._wizard_render(taskloom._draft(OWNER))
        rows = _rows(buttons)
        back = [pairs for pairs in rows if pairs and pairs[0][0].startswith("← Back")]
        assert len(back) == 1, f"{step} must expose exactly one editor Back"
        assert back[0][0][1] == f"action:taskloom_wizard:step:{taskloom.STEP_EDIT}"


def test_editor_never_emits_the_shared_panel_stack_back(editor):
    taskloom, repo = editor
    task = _run(repo.create_task(OWNER, _message_task_data()))
    _open_edit(taskloom, task.id)

    for step in (
        taskloom.STEP_EDIT, taskloom.STEP_DETAILS, taskloom.STEP_SCHEDULE, taskloom.STEP_REVIEW,
    ):
        _run(taskloom._wizard_action(None, f"step:{step}", 1))
        _title, _body, buttons = taskloom._wizard_render(taskloom._draft(OWNER))
        assert "panel:_nav:back" not in _data(buttons)


def test_editor_footer_offers_cancel_edit_and_close(editor):
    taskloom, repo = editor
    task = _run(repo.create_task(OWNER, _message_task_data()))
    _title, _body, buttons = _open_edit(taskloom, task.id)
    data = _data(buttons)

    assert "action:taskloom_wizard:cancel" in data
    assert "panel:_nav:close" in data
    # Cancel is an EDITOR action, not the creation footer's panel jump.
    assert "panel:taskloom" not in data


# ── input submission stays inside the editor ───────────────────────────────

@pytest.mark.parametrize(
    "field,text,step,check",
    [
        ("source", SOURCE, "details", lambda d: d.source == SOURCE),
        ("maxlen", "59", "details", lambda d: d.max_length == 59),
        ("interval", "7", "schedule", lambda d: d.interval_minutes == 7),
        ("daily", "09:30", "schedule", lambda d: d.clock == "09:30"),
        ("weekly", "Monday 09:30", "schedule", lambda d: d.weekday == 0),
        ("tz", "Asia/Tehran", "schedule", lambda d: d.timezone == "Asia/Tehran"),
    ],
)
def test_input_submission_stays_in_the_editor_and_keeps_the_draft(
    editor, monkeypatch, field, text, step, check
):
    taskloom, repo = editor
    _create_bio(taskloom, source=SOURCE)
    task = _run(repo.list_tasks(OWNER))[0]
    _open_edit(taskloom, task.id)

    step_name = taskloom.STEP_DETAILS if step == "details" else taskloom.STEP_SCHEDULE
    _run(taskloom._wizard_action(None, f"step:{step_name}", 1))

    rendered = {}

    async def _capture(*args, **kwargs):
        rendered["result"] = taskloom._wizard_render(taskloom._draft(OWNER))

    monkeypatch.setattr(taskloom, "_wizard_finish", _capture)
    from backend.helper import panels

    handler = panels.get_input(taskloom.WIZARD_PANEL_QUERY, field)["handler"]
    _run(handler(text, 1, 2, 0, 0))

    stored = taskloom._draft(OWNER)
    assert stored.step == step_name, "an input must not navigate the editor"
    assert check(stored)
    assert stored.editing_task_id == task.id
    # The re-rendered step is the editor, never a root panel.
    title, body, _buttons_ = rendered["result"]
    assert title == f"✎ Edit task #{task.id}"
    assert "What do you want to edit?" not in body  # still the field step
    assert "Edit task" in title


def test_edit_loads_and_preserves_the_stored_display_choice(editor, monkeypatch):
    """The editor loads the STORED display choice and a later save keeps it."""
    taskloom, repo = editor
    _create_bio(taskloom, source=SOURCE, show_source=True)
    task = _run(repo.list_tasks(OWNER))[0]
    _open_edit(taskloom, task.id)
    assert taskloom._draft(OWNER).show_source is True

    _run(taskloom._wizard_input_handler("interval")("4", 1, 2, 0, 0))
    _run(taskloom._wizard_action(None, "step:review", 1))
    _run(taskloom._wizard_action(None, "create", 1))

    from backend.ai.preparation_policy import derive_policy

    current = _run(repo.get_task(OWNER, task.id))
    policy = derive_policy(current.ai_instruction)
    assert policy.source == SOURCE and policy.show_source is True
    assert current.schedule == {"seconds": 240}


def test_the_source_input_keeps_the_deliberate_display_default(editor, monkeypatch):
    """Naming a source is a NEW generation constraint: the display choice
    returns to its deliberate default (No) for the source actually named."""
    taskloom, repo = editor
    _create_bio(taskloom, source=SOURCE, show_source=True)
    task = _run(repo.list_tasks(OWNER))[0]
    _open_edit(taskloom, task.id)
    _run(taskloom._wizard_action(None, f"step:{taskloom.STEP_DETAILS}", 1))

    async def _no_edit(*args, **kwargs):
        return None

    monkeypatch.setattr(taskloom, "_wizard_finish", _no_edit)
    from backend.helper import panels

    source_handler = panels.get_input(taskloom.WIZARD_PANEL_QUERY, "source")["handler"]
    _run(source_handler("Rei Ayanami", 1, 2, 0, 0))
    assert taskloom._draft(OWNER).source == "Rei Ayanami"
    assert taskloom._draft(OWNER).show_source is False
    # The editor is still the editor: same step, same task, draft intact.
    assert taskloom._draft(OWNER).step == taskloom.STEP_DETAILS
    assert taskloom._draft(OWNER).editing_task_id == task.id

    # Opting back in is one explicit click, and it persists.
    _run(source_handler(SOURCE, 1, 2, 0, 0))
    _run(taskloom._wizard_action(None, "set:show_source:1", 1))
    _run(taskloom._wizard_action(None, "step:review", 1))
    _run(taskloom._wizard_action(None, "create", 1))

    from backend.ai.preparation_policy import derive_policy

    current = _run(repo.get_task(OWNER, task.id))
    assert derive_policy(current.ai_instruction).show_source is True


# ── Cancel / Close / Back and durable state ────────────────────────────────

def test_cancel_edit_discards_the_draft_and_returns_to_the_detail_view(editor):
    taskloom, repo = editor
    task = _run(repo.create_task(OWNER, _message_task_data()))
    _open_edit(taskloom, task.id)
    _run(taskloom._wizard_input_handler("interval")("9", 1, 2, 0, 0))

    title, body, _buttons_ = _run(taskloom._wizard_action(None, "cancel", 1))
    current = _run(repo.get_task(OWNER, task.id))

    assert title == f"Task #{task.id}"
    assert "**Actions:**" in body
    # The draft is REALLY gone: nothing can resume as a later "＋ New task".
    assert OWNER not in taskloom._drafts
    assert current.version == task.version
    assert current.schedule == {"seconds": 300}


def test_back_and_cancel_never_modify_the_durable_task(editor):
    taskloom, repo = editor
    task = _run(repo.create_task(OWNER, _message_task_data()))
    _open_edit(taskloom, task.id)
    _run(taskloom._wizard_input_handler("interval")("47", 1, 2, 0, 0))
    _run(taskloom._wizard_action(None, "step:review", 1))
    _run(taskloom._wizard_action(None, "step:edit", 1))

    current = _run(repo.get_task(OWNER, task.id))
    assert current.version == task.version
    assert current.schedule == task.schedule
    assert current.actions == task.actions
    assert current.next_run_at == task.next_run_at


def test_back_from_review_keeps_every_edited_value(editor):
    taskloom, repo = editor
    _create_bio(taskloom, source=SOURCE)
    task = _run(repo.list_tasks(OWNER))[0]
    _open_edit(taskloom, task.id)
    _run(taskloom._wizard_input_handler("interval")("30", 1, 2, 0, 0))
    _run(taskloom._wizard_action(None, "step:review", 1))

    _run(taskloom._wizard_action(None, "step:edit", 1))
    draft = taskloom._draft(OWNER)

    assert draft.step == taskloom.STEP_EDIT
    assert draft.interval_minutes == 30
    assert draft.source == SOURCE
    assert draft.language == "en"
    assert draft.max_length == 59
    assert draft.editing_task_id == task.id


# ── saving ─────────────────────────────────────────────────────────────────

def test_save_returns_to_the_task_detail_and_bumps_the_version_once(editor):
    taskloom, repo = editor
    _create_bio(taskloom, source=SOURCE)
    task = _run(repo.list_tasks(OWNER))[0]
    _open_edit(taskloom, task.id)
    _run(taskloom._wizard_input_handler("interval")("10", 1, 2, 0, 0))
    _run(taskloom._wizard_action(None, "step:review", 1))

    title, body, _buttons_ = _run(taskloom._wizard_action(None, "create", 1))
    tasks = _run(repo.list_tasks(OWNER))
    current = tasks[0]

    assert title == f"Task #{task.id}"
    assert f"✓ Task #{task.id} updated · v{task.version + 1}" in body
    assert "**Actions:**" in body  # the detail view, not a menu
    assert len(tasks) == 1, "an edit must never create a second task"
    assert current.version == task.version + 1
    assert current.schedule == {"seconds": 600}


def test_a_stale_save_keeps_the_whole_draft_and_stays_in_the_editor(editor):
    taskloom, repo = editor
    _create_bio(taskloom, source=SOURCE)
    task = _run(repo.list_tasks(OWNER))[0]
    _open_edit(taskloom, task.id)
    _run(taskloom._wizard_input_handler("interval")("11", 1, 2, 0, 0))
    _run(taskloom._wizard_action(None, "step:review", 1))
    # A concurrent writer moves the task on after the form was opened.
    _run(repo.update_task(OWNER, task.id, task.version, {"label": "changed elsewhere"}))

    title, body, buttons = _run(taskloom._wizard_action(None, "create", 1))
    draft = taskloom._draft(OWNER)
    current = _run(repo.get_task(OWNER, task.id))

    assert "stale version" in body
    assert "nothing was saved" in body
    # Still an editor with a recoverable draft, not a dead end.
    assert title == f"✎ Edit task #{task.id}"
    assert draft.step == taskloom.STEP_REVIEW
    assert draft.interval_minutes == 11
    assert draft.editing_task_id == task.id
    # The stale draft is recoverable: Back returns to the hub, where
    # "⟳ Reload from task" adopts the current version.
    assert f"action:taskloom_wizard:step:{taskloom.STEP_EDIT}" in _data(buttons)
    assert "Reload from task" in body
    assert current.label == "changed elsewhere"
    assert current.schedule == {"seconds": 120}


def test_reload_adopts_the_current_version_after_a_stale_save(editor):
    taskloom, repo = editor
    _create_bio(taskloom, source=SOURCE)
    task = _run(repo.list_tasks(OWNER))[0]
    _open_edit(taskloom, task.id)
    _run(taskloom._wizard_input_handler("interval")("11", 1, 2, 0, 0))
    _run(repo.update_task(OWNER, task.id, task.version, {"label": "changed elsewhere"}))

    _run(taskloom._wizard_action(None, "reload", 1))
    draft = taskloom._draft(OWNER)
    current = _run(repo.get_task(OWNER, task.id))

    assert draft.editing_version == current.version
    assert draft.step == taskloom.STEP_EDIT
    assert draft.label == "changed elsewhere"
    # The reloaded draft can now actually save.
    _run(taskloom._wizard_input_handler("interval")("3", 1, 2, 0, 0))
    _title, body, _buttons_ = _run(taskloom._wizard_action(None, "create", 1))
    assert "stale version" not in body
    assert _run(repo.get_task(OWNER, task.id)).version == current.version + 1


# ── one field at a time: nothing else is rewritten ─────────────────────────

def test_editing_only_the_interval_leaves_every_other_field_unchanged(editor):
    taskloom, repo = editor
    _create_bio(taskloom, source=SOURCE, show_source=True)
    task = _run(repo.list_tasks(OWNER))[0]
    _open_edit(taskloom, task.id)
    _run(taskloom._wizard_input_handler("interval")("10", 1, 2, 0, 0))
    _run(taskloom._wizard_action(None, "step:review", 1))
    _run(taskloom._wizard_action(None, "create", 1))

    current = _run(repo.get_task(OWNER, task.id))
    from backend.ai.preparation_policy import derive_policy

    policy = derive_policy(current.ai_instruction)
    assert current.schedule == {"seconds": 600}
    assert policy.source == SOURCE
    assert policy.language == "english"
    assert policy.max_length == 59
    assert policy.show_source is True
    assert current.label == task.label


def test_a_non_schedule_edit_does_not_move_the_next_run_boundary(editor):
    taskloom, repo = editor
    _create_bio(taskloom, source=SOURCE)
    task = _run(repo.list_tasks(OWNER))[0]
    before = task.next_run_at
    assert before is not None

    _open_edit(taskloom, task.id)
    _run(taskloom._wizard_input_handler("maxlen")("30", 1, 2, 0, 0))
    _run(taskloom._wizard_action(None, "step:review", 1))
    _run(taskloom._wizard_action(None, "create", 1))

    current = _run(repo.get_task(OWNER, task.id))
    assert current.next_run_at == before, "a content edit must not push the boundary out"


def test_a_schedule_edit_does_recompute_the_boundary(editor):
    taskloom, repo = editor
    _create_bio(taskloom, source=SOURCE, interval="2")
    task = _run(repo.list_tasks(OWNER))[0]

    _open_edit(taskloom, task.id)
    _run(taskloom._wizard_input_handler("interval")("30", 1, 2, 0, 0))
    _run(taskloom._wizard_action(None, "step:review", 1))
    _run(taskloom._wizard_action(None, "create", 1))

    current = _run(repo.get_task(OWNER, task.id))
    assert current.schedule == {"seconds": 1800}
    assert current.next_run_at != task.next_run_at


def test_editing_a_send_message_task_keeps_its_font_and_updates_only_the_text(editor):
    taskloom, repo = editor
    task = _run(repo.create_task(OWNER, _message_task_data()))

    _open_edit(taskloom, task.id)
    draft = taskloom._draft(OWNER)
    assert draft.text == "hello" and draft.font == "script"

    _run(taskloom._wizard_action(None, f"step:{taskloom.STEP_DETAILS}", 1))
    _run(taskloom._wizard_input_handler("text")("good evening", 1, 2, 0, 0))
    _run(taskloom._wizard_action(None, "step:review", 1))
    _title, _body, _buttons_ = _run(taskloom._wizard_action(None, "create", 1))

    current = _run(repo.get_task(OWNER, task.id))
    assert current.actions == [
        {"name": "send_message", "arguments": {"text": "good evening", "font": "script"}}
    ]
    assert current.version == task.version + 1


def test_editing_a_username_task_preserves_its_action_and_schedule(editor):
    taskloom, repo = editor
    _run(taskloom._wizard_action(None, "set:action:username", 1))
    _run(taskloom._wizard_action(None, "set:mode:ai", 1))
    _run(taskloom._wizard_input_handler("interval")("5", 1, 2, 0, 0))
    _run(taskloom._wizard_action(None, "create", 1))
    task = _run(repo.list_tasks(OWNER))[0]

    _open_edit(taskloom, task.id)
    draft = taskloom._draft(OWNER)
    assert draft.action == "username" and draft.interval_minutes == 5

    _run(taskloom._wizard_input_handler("interval")("15", 1, 2, 0, 0))
    _run(taskloom._wizard_action(None, "step:review", 1))
    _run(taskloom._wizard_action(None, "create", 1))

    current = _run(repo.get_task(OWNER, task.id))
    assert current.actions[0]["name"] == "username_set_text"
    assert current.schedule == {"seconds": 900}
    assert current.version == task.version + 1


def test_edit_of_another_owners_task_is_not_reachable(editor):
    taskloom, repo = editor
    task = _run(repo.create_task(OTHER, _message_task_data()))

    title, body, _buttons_ = _open_edit(taskloom, task.id)

    assert body == "× Task not found."
    assert title == "Taskloom"
    assert OWNER not in taskloom._drafts  # no editor draft was ever created


def test_an_unrepresentable_definition_is_refused_instead_of_rewritten(editor):
    taskloom, repo = editor
    task = _run(repo.create_task(OWNER, _message_task_data(
        actions=[{"name": "list_saves", "arguments": {}}],
    )))

    title, body, _buttons_ = _open_edit(taskloom, task.id)

    assert "cannot be edited here" in body
    assert OWNER not in taskloom._drafts
    assert _run(repo.get_task(OWNER, task.id)).version == task.version
