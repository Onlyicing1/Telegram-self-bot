"""Taskloom input/variable UX — commit, Back and Cancel contracts.

Contracts under test:

- INPUT COMMIT: a valid reply commits the value into the current ``TaskDraft``,
  ends the input interaction (the panel message stops showing the prompt) and
  re-renders the SAME step with the committed value visible.
- FAILED INPUT: the owner stays on the same step, the rest of the draft is
  preserved, the error is shown, another attempt is possible.
- BACK: a field input's Back returns to the panel that opened it — for the
  wizard, the SAME step with the draft intact. Never a stack pop, never
  Taskloom home / the main menu.
- CANCEL: really DISCARDS the draft (edit → the task's detail view, create →
  the Taskloom list). Back and Cancel are not the same control.
- The panel is restored through the helper bot when it exists, and through the
  self client (the documented text-panel fallback) when it does not — an input
  must never leave the panel stuck on the prompt.
"""
from __future__ import annotations

import asyncio

import pytest

from backend.ai.database.task_repository import InMemoryTaskRepository

OWNER = 6401
SOURCE = "Ayanami Rei"


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _rows(buttons):
    rows = []
    for row in buttons or []:
        rendered = row if isinstance(row, list) else [row]
        pairs = []
        for button in rendered:
            raw = getattr(button, "data", button)
            data = raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)
            pairs.append((str(getattr(button, "text", "")), data))
        rows.append(pairs)
    return rows


def _pairs(buttons):
    return [pair for row in _rows(buttons) for pair in row]


@pytest.fixture()
def wizard(monkeypatch):
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


def _bio_details(handler):
    """Drive the creation flow to the Bio content-details step."""
    _run(handler._wizard_action(None, "set:action:bio", 1))
    _run(handler._wizard_action(None, "set:mode:ai", 1))
    _run(handler._wizard_input_handler("source")(SOURCE, 1, 2, 0, 0))
    return handler._draft(OWNER)


# ── the prompt itself: Back and Cancel are distinct controls ────────────────


def test_field_input_prompt_offers_back_and_a_real_cancel(wizard, monkeypatch):
    handler, _repo = wizard
    from backend.helper import panels

    captured: list = []

    async def _capture(event, text, buttons, chat_id, msg_id):
        captured.append((text, buttons))
        return True

    monkeypatch.setattr(panels, "_safe_edit", _capture)
    _run(panels._handle_input(object(), "taskloom_new:interval", OWNER, 111, 222))

    assert captured, "the input prompt was never rendered"
    _text, buttons = captured[-1]
    pairs = _pairs(buttons)
    data = [value for _label, value in pairs]
    labels = [label for label, _value in pairs]

    # Back re-opens the wizard (same step, draft intact) — never a stack pop
    # and never the main menu.
    assert ("← Back", "panel:taskloom_new") in pairs
    assert "panel:_nav:back" not in data
    assert ("✕ Cancel", "action:taskloom_wizard:cancel") in pairs
    assert "Cancel" not in labels


def test_every_wizard_input_registers_the_back_and_cancel_rows(wizard):
    handler, _repo = wizard
    from backend.helper import panels

    for field in ("source", "maxlen", "text", "interval", "daily",
                  "weekly", "once", "tz", "font"):
        config = panels.get_input("taskloom_new", field)
        assert config is not None, field
        assert config["extra_rows"] == (("✕ Cancel", "action:taskloom_wizard:cancel"),)
        assert "back" not in config  # the shared default (the panel itself) applies


# ── input commit: value in the draft, prompt gone, same step ────────────────


def test_daily_input_commits_into_the_draft_and_renders_the_committed_time(wizard):
    handler, _repo = wizard
    _bio_details(handler)
    _run(handler._wizard_action(None, "step:schedule", 1))

    _run(handler._wizard_input_handler("daily")("23:30", 1, 2, 0, 0))

    draft = handler._draft(OWNER)
    assert draft.schedule_type == "daily" and draft.clock == "23:30"
    assert draft.step == handler.STEP_SCHEDULE          # stayed on the same step
    assert draft.source == SOURCE                      # nothing else was touched
    title, body, _buttons = handler._wizard_render(draft)
    assert "Schedule: Daily at 23:30" in body
    assert "Timezone: Asia/Tehran" in body


def test_a_valid_input_never_leaves_the_panel_on_the_prompt(wizard, monkeypatch):
    handler, _repo = wizard
    import backend.bot.handlers.taskloom as taskloom
    from backend.helper import client as helper_client_mod
    from backend.helper import inline_engine

    edits: list = []

    class _Helper:
        async def edit_message(self, chat_id, msg_id, text, buttons=None):
            edits.append((chat_id, msg_id, text))

    monkeypatch.setattr(helper_client_mod, "get_client", lambda: _Helper())
    monkeypatch.setattr(inline_engine, "_self_client", None)

    _bio_details(handler)
    _run(handler._wizard_action(None, "step:schedule", 1))
    _run(taskloom._wizard_input_handler("interval")("5", 111, 222, 111, 222))

    assert edits, "the panel message was not restored after the input"
    assert edits[-1][:2] == (111, 222)
    assert "Every 5 minutes" in edits[-1][2]
    assert "Reply below" not in edits[-1][2]          # the prompt is gone


def test_input_commit_restores_the_panel_when_the_helper_bot_is_disabled(wizard, monkeypatch):
    handler, _repo = wizard
    """The documented text-panel fallback: no helper ⇒ edit in place."""
    import backend.bot.handlers.taskloom as taskloom
    from backend.helper import client as helper_client_mod
    from backend.helper import inline_engine

    edits: list = []
    deleted: list = []

    class _SelfClient:
        async def edit_message(self, chat_id, msg_id, text, buttons=None):
            edits.append((chat_id, msg_id, text))

        async def delete_messages(self, chat_id, ids):
            deleted.append((chat_id, list(ids)))

    monkeypatch.setattr(helper_client_mod, "get_client", lambda: None)
    monkeypatch.setattr(inline_engine, "_self_client", _SelfClient())

    _bio_details(handler)
    _run(handler._wizard_action(None, "step:schedule", 1))
    edits.clear()
    deleted.clear()
    _run(taskloom._wizard_input_handler("daily")("09:30", 111, 222, 111, 222))

    assert handler._draft(OWNER).clock == "09:30"
    assert edits, "the panel stayed on the input prompt (no helper fallback)"
    assert "Daily at 09:30" in edits[-1][2]
    assert deleted == [(111, [222])]                  # the owner's reply is consumed


def test_helper_failure_still_ends_the_input_with_the_notice(wizard, monkeypatch):
    handler, _repo = wizard
    import backend.bot.handlers.taskloom as taskloom
    from backend.helper import client as helper_client_mod
    from backend.helper import inline_engine

    calls: list = []

    class _Helper:
        async def edit_message(self, chat_id, msg_id, text, buttons=None):
            calls.append((text, buttons))
            if buttons is not None:
                raise RuntimeError("panel edit failed")

    monkeypatch.setattr(helper_client_mod, "get_client", lambda: _Helper())
    monkeypatch.setattr(inline_engine, "_self_client", None)

    _bio_details(handler)
    _run(handler._wizard_action(None, "step:schedule", 1))
    _run(taskloom._wizard_input_handler("interval")("7", 111, 222, 111, 222))

    assert calls, "no edit was attempted"
    assert "Every 7 minutes" in calls[-1][0]          # the notice-only fallback
    assert calls[-1][1] is None                       # rebuilt without buttons
    assert handler._draft(OWNER).interval_minutes == 7


# ── validation failure: same step, draft preserved, error shown ─────────────


def test_invalid_input_keeps_the_step_and_the_rest_of_the_draft(wizard):
    handler, _repo = wizard
    _bio_details(handler)
    _run(handler._wizard_action(None, "step:schedule", 1))
    _run(handler._wizard_input_handler("interval")("2", 1, 2, 0, 0))

    _run(handler._wizard_input_handler("daily")("99:99", 1, 2, 0, 0))

    draft = handler._draft(OWNER)
    assert draft.step == handler.STEP_SCHEDULE
    assert draft.schedule_type == "interval" and draft.interval_minutes == 2
    assert draft.source == SOURCE
    assert draft.notice.startswith("×")
    _title, body, _buttons = handler._wizard_render(draft)
    assert draft.notice in body                       # the error is visible


# ── Back vs Cancel from a field input ──────────────────────────────────────


def test_back_from_a_field_input_returns_to_the_same_step_with_the_draft(wizard, monkeypatch):
    handler, _repo = wizard
    from backend.helper import panels

    _bio_details(handler)
    _run(handler._wizard_action(None, "step:schedule", 1))
    _run(handler._wizard_input_handler("interval")("5", 1, 2, 0, 0))
    _run(handler._wizard_input_handler("tz")("Europe/Berlin", 1, 2, 0, 0))

    rendered: list = []

    async def _capture(event, text, buttons, chat_id, msg_id):
        rendered.append(text)
        return True

    monkeypatch.setattr(panels, "_safe_edit", _capture)
    # The prompt's "← Back" is the panel query that owns the input.
    _run(panels._handle_panel(object(), "taskloom_new", 111, 222, OWNER))

    draft = handler._draft(OWNER)
    assert draft.step == handler.STEP_SCHEDULE
    assert draft.interval_minutes == 5 and draft.timezone == "Europe/Berlin"
    assert rendered and "Schedule: Every 5 minutes" in rendered[-1]
    assert "Timezone: Europe/Berlin" in rendered[-1]


def test_cancel_from_a_field_input_discards_the_draft(wizard):
    handler, repo = wizard
    _bio_details(handler)
    _run(handler._wizard_action(None, "step:schedule", 1))
    _run(handler._wizard_input_handler("interval")("5", 1, 2, 0, 0))

    _run(handler._wizard_action(None, "cancel", 1))

    assert OWNER not in handler._drafts
    assert _run(repo.list_tasks(OWNER)) == []


def test_cancel_from_an_edit_field_input_returns_to_the_task_detail(wizard):
    handler, repo = wizard
    # Create a task first, then open its editor and cancel from a field input.
    _bio_details(handler)
    _run(handler._wizard_input_handler("interval")("3", 1, 2, 0, 0))
    _run(handler._wizard_action(None, "create", 1))
    task = _run(repo.list_tasks(OWNER))[0]

    _run(handler._wizard_panel(None, f"edit:{task.id}"))
    _run(handler._wizard_action(None, "step:schedule", 1))
    _run(handler._wizard_input_handler("interval")("9", 1, 2, 0, 0))
    title, body, _buttons = _run(handler._wizard_action(None, "cancel", 1))

    assert title == f"Task #{task.id}"
    assert "**Actions:**" in body
    assert OWNER not in handler._drafts
    assert _run(repo.get_task(OWNER, task.id)).version == task.version


# ── several inputs in sequence: nothing is lost, Review matches the draft ──


def test_multiple_field_inputs_never_lose_earlier_values(wizard):
    handler, repo = wizard
    _bio_details(handler)
    _run(handler._wizard_action(None, "step:details", 1))
    _run(handler._wizard_input_handler("maxlen")("25", 1, 2, 0, 0))
    _run(handler._wizard_action(None, "set:lang:en", 1))
    _run(handler._wizard_action(None, "step:schedule", 1))
    _run(handler._wizard_input_handler("weekly")("Monday 09:30", 1, 2, 0, 0))
    _run(handler._wizard_input_handler("tz")("Europe/Berlin", 1, 2, 0, 0))

    draft = handler._draft(OWNER)
    assert draft.source == SOURCE and draft.max_length == 25 and draft.language == "en"
    assert draft.schedule_type == "weekly" and draft.weekday == 0 and draft.clock == "09:30"
    assert draft.timezone == "Europe/Berlin"

    title, body, _buttons = _run(handler._wizard_action(None, "step:review", 1))
    assert "**Maximum length:** at most 25 characters" in body
    assert "**Schedule:** Weekly on Monday at 09:30" in body
    assert "**Timezone:** Europe/Berlin" in body

    _run(handler._wizard_action(None, "create", 1))
    task = _run(repo.list_tasks(OWNER))[0]
    assert task.schedule_type == "weekly"
    assert task.schedule == {"weekday": 0, "hour": 9, "minute": 30, "timezone": "Europe/Berlin"}
    from backend.ai.preparation_policy import derive_policy

    policy = derive_policy(task.ai_instruction)
    assert policy.source == SOURCE and policy.max_length == 25 and policy.language == "english"


# ── the edit hub's Back: leave to the task detail, draft preserved ──────────
# The edit hub's parent surface is the edited task's DETAIL view, so its Back
# returns there (never Taskloom home, never a stack pop). Back means "previous
# step, keep the draft"; it must stay distinct from Cancel, which discards.

def test_edit_hub_back_returns_to_the_task_detail_and_keeps_the_draft(wizard, monkeypatch):
    handler, repo = wizard
    _bio_details(handler)
    _run(handler._wizard_input_handler("interval")("3", 1, 2, 0, 0))
    _run(handler._wizard_action(None, "create", 1))
    task = _run(repo.list_tasks(OWNER))[0]

    _run(handler._wizard_panel(None, f"edit:{task.id}"))
    _run(handler._wizard_input_handler("interval")("21", 1, 2, 0, 0))
    _run(handler._wizard_action(None, "step:edit", 1))

    title, body, buttons = handler._wizard_render(handler._draft(OWNER))
    rows = _pairs(buttons)
    back = [pair for pair in rows if pair[0].startswith("← Back")]
    assert back == [("← Back", "action:taskloom_wizard:leave")]
    assert "panel:_nav:back" not in [value for _label, value in rows]

    title, body, _buttons = _run(handler._wizard_action(None, "leave", 1))

    assert title == f"Task #{task.id}"
    assert "**Actions:**" in body                       # the detail view
    draft = handler._draft(OWNER)
    assert draft.editing_task_id == task.id             # the edit is still open
    assert draft.interval_minutes == 21                 # and the draft survived
    assert _run(repo.get_task(OWNER, task.id)).schedule == {"seconds": 180}


def test_edit_hub_cancel_still_discards_while_back_preserves(wizard):
    handler, repo = wizard
    _bio_details(handler)
    _run(handler._wizard_input_handler("interval")("3", 1, 2, 0, 0))
    _run(handler._wizard_action(None, "create", 1))
    task = _run(repo.list_tasks(OWNER))[0]

    _run(handler._wizard_panel(None, f"edit:{task.id}"))
    _run(handler._wizard_input_handler("interval")("21", 1, 2, 0, 0))
    _run(handler._wizard_action(None, "leave", 1))
    assert handler._draft(OWNER).interval_minutes == 21

    _run(handler._wizard_panel(None, f"edit:{task.id}"))
    _run(handler._wizard_action(None, "cancel", 1))
    assert OWNER not in handler._drafts
    # Neither Back nor Cancel touched the durable definition.
    assert _run(repo.get_task(OWNER, task.id)).schedule == {"seconds": 180}


def test_edit_hub_back_targets_the_edited_task_detail(wizard):
    handler, repo = wizard
    _bio_details(handler)
    _run(handler._wizard_input_handler("interval")("3", 1, 2, 0, 0))
    _run(handler._wizard_action(None, "create", 1))
    task = _run(repo.list_tasks(OWNER))[0]

    _run(handler._wizard_panel(None, f"edit:{task.id}"))
    title, body, buttons = handler._wizard_render(handler._draft(OWNER))

    assert title == f"✎ Edit task #{task.id}"
    # The hub offers exactly one Back, and it is not the creation footer's
    # panel jump, the wizard's own step Back, or the global stack pop.
    values = [value for _label, value in _pairs(buttons)]
    assert values.count("action:taskloom_wizard:leave") == 1
    assert "panel:taskloom" not in values
    assert f"action:taskloom_wizard:step:{handler.STEP_ACTION}" not in values


def test_field_input_back_inside_an_edit_reopens_the_editor_not_taskloom_home(wizard, monkeypatch):
    """The prompt's Back re-opens the OWNING panel with the draft intact.

    Live defect: a field input's Back jumped to Taskloom home because it used
    the generic stack pop. Inside an edit it must land back on the editor step
    that opened the input, with every pending change preserved.
    """
    handler, repo = wizard
    from backend.helper import panels

    _bio_details(handler)
    _run(handler._wizard_input_handler("interval")("3", 1, 2, 0, 0))
    _run(handler._wizard_action(None, "create", 1))
    task = _run(repo.list_tasks(OWNER))[0]

    _run(handler._wizard_panel(None, f"edit:{task.id}"))
    _run(handler._wizard_action(None, "step:schedule", 1))
    _run(handler._wizard_input_handler("interval")("21", 1, 2, 0, 0))

    # What the prompt actually renders for its first row.
    captured: list = []

    async def _capture(event, text, buttons, chat_id, msg_id):
        captured.append((text, buttons))
        return True

    monkeypatch.setattr(panels, "_safe_edit", _capture)
    _run(panels._handle_input(object(), "taskloom_new:interval", OWNER, 111, 222))
    _text, buttons = captured[-1]
    pairs = _pairs(buttons)
    assert ("← Back", "panel:taskloom_new") in pairs

    # Dispatch exactly that callback target.
    rendered: list = []

    async def _capture_panel(event, text, buttons, chat_id, msg_id):
        rendered.append(text)
        return True

    monkeypatch.setattr(panels, "_safe_edit", _capture_panel)
    _run(panels._handle_panel(object(), "taskloom_new", 111, 222, OWNER))

    draft = handler._draft(OWNER)
    assert draft.editing_task_id == task.id            # still the editor
    assert draft.step == handler.STEP_SCHEDULE         # the step that opened it
    assert draft.interval_minutes == 21                # pending change preserved
    assert rendered and "✎ Edit task" in rendered[-1]
    assert "▦ **Taskloom**" not in rendered[-1]        # never the home list
