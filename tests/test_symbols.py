"""Central functional Unicode symbol registry + label-convention tests.

Verifies the single source of truth in ``backend.helper.symbols`` and that the
production panel paths actually consume it:

* the registry exposes the intended semantic keys;
* category-specific glyphs never collide across categories (only the
  documented common-operation reuses are permitted);
* task-status and schedule mappings are exact;
* labels follow the ``"<glyph> <text>"`` convention and describe their action;
* shared navigation and dynamic (pagination) buttons use the registry;
* the Taskloom list/detail/wizard keep their callbacks and use the glyphs;
* the display-font pipeline leaves every registry glyph byte-identical;
* no registry glyph carries a variation selector (U+FE0F).
"""
from __future__ import annotations

import asyncio
from collections import defaultdict

import pytest

from backend.helper import symbols as sym


OWNER = 4242


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _texts(buttons) -> list[str]:
    return [str(getattr(b, "text", b)) for row in buttons for b in row]


def _datas(buttons) -> list[str]:
    out = []
    for row in buttons:
        for b in row:
            raw = getattr(b, "data", b)
            out.append(raw.decode("utf-8") if isinstance(raw, bytes) else str(raw))
    return out


# ── 1. registry contains the intended semantic keys ─────────────────────────

def test_registry_contains_all_required_semantic_keys():
    expected = {
        # A. task states
        "active", "paused", "completed", "failed", "expired", "deleted",
        # B. schedule types
        "once", "interval", "daily", "weekly", "event",
        # C. destination + content
        "destination", "static", "ai",
        # D. common operations
        "create", "save", "edit", "delete", "refresh", "pause", "resume",
        "back", "next", "home", "close", "previous",
        # E. structure + wizard navigation
        "heading", "separator", "step_current", "step_incomplete",
        "step_completed", "page_current",
    }
    assert expected <= set(sym.ALL_SYMBOLS)
    for key, spec in sym.ALL_SYMBOLS.items():
        assert spec.key and spec.meaning, key
        assert spec.glyph and len(spec.glyph) >= 1


# ── 2/3/4. category uniqueness + documented reuse only ──────────────────────

def test_category_specific_glyphs_are_unique_within_each_category():
    groups = {
        "task_state": sym.TASK_STATE_SYMBOLS,
        "schedule": sym.SCHEDULE_SYMBOLS,
        "content": sym.CONTENT_SYMBOLS,
        "structure": sym.STRUCTURE_SYMBOLS,
    }
    for name, members in groups.items():
        glyphs = [spec.glyph for spec in members.values()]
        assert len(glyphs) == len(set(glyphs)), f"{name} has a duplicate glyph"


def test_no_undocumented_duplicate_assignment_remains():
    by_glyph: dict[str, set[str]] = defaultdict(set)
    for key, spec in sym.ALL_SYMBOLS.items():
        by_glyph[spec.glyph].add(key)
    for glyph, keys in by_glyph.items():
        if len(keys) > 1:
            assert glyph in sym.DOCUMENTED_REUSE, f"{glyph!r} reused by {sorted(keys)}"
            assert keys == set(sym.DOCUMENTED_REUSE[glyph]), (
                f"{glyph!r} shared by {sorted(keys)}, documented as "
                f"{sorted(sym.DOCUMENTED_REUSE[glyph])}"
            )


def test_common_operations_reuse_keeps_one_meaning_per_glyph():
    # Save reuses the completion check mark because saving IS completing.
    assert sym.OP_SAVE == sym.STATE_COMPLETED == sym.STEP_COMPLETED
    # Back and Previous are the same operation.
    assert sym.OP_BACK == sym.OP_PREV


# ── 5. task status + schedule mappings ──────────────────────────────────────

def test_task_status_glyph_mapping_is_exact():
    assert sym.status_glyph("active") == sym.STATE_ACTIVE
    assert sym.status_glyph("paused") == sym.STATE_PAUSED
    assert sym.status_glyph("completed") == sym.STATE_COMPLETED
    assert sym.status_glyph("failed") == sym.STATE_FAILED
    assert sym.status_glyph("expired") == sym.STATE_EXPIRED
    assert sym.status_glyph("deleted") == sym.STATE_DELETED
    # unknown status fails safe to the failure glyph
    assert sym.status_glyph("nonsense") == sym.STATE_FAILED


def test_schedule_glyph_mapping_is_exact():
    assert sym.schedule_glyph("once") == sym.SCHEDULE_ONCE
    assert sym.schedule_glyph("interval") == sym.SCHEDULE_INTERVAL
    assert sym.schedule_glyph("daily") == sym.SCHEDULE_DAILY
    assert sym.schedule_glyph("weekly") == sym.SCHEDULE_WEEKLY
    assert sym.schedule_glyph("event") == sym.SCHEDULE_EVENT
    assert sym.schedule_glyph("nonsense") == sym.SEPARATOR


def test_content_glyph_mapping_is_exact():
    assert sym.content_glyph("ai") == sym.CONTENT_AI
    assert sym.content_glyph("static") == sym.CONTENT_STATIC


def test_distinct_states_have_distinct_glyphs():
    glyphs = {
        sym.STATE_ACTIVE, sym.STATE_PAUSED, sym.STATE_COMPLETED,
        sym.STATE_FAILED, sym.STATE_EXPIRED, sym.STATE_DELETED,
    }
    assert len(glyphs) == 6


# ── 6. label convention ─────────────────────────────────────────────────────

def test_label_composition_is_glyph_space_text():
    assert sym.label(sym.OP_CREATE, "New task") == f"{sym.OP_CREATE} New task"
    assert sym.label(sym.OP_BACK, "Back").startswith(sym.OP_BACK + " ")


# ── 7. shared navigation + dynamic buttons use the registry ─────────────────

def test_shared_navigation_uses_registry_glyphs():
    from backend.helper.panels import (
        InlinePanelBuilder, _add_nav_buttons, _add_close_button, _progress_buttons,
    )
    builder = InlinePanelBuilder()
    _add_nav_buttons(builder)
    labels = _texts(builder.build())
    assert sym.label(sym.OP_BACK, "Back") in labels
    assert sym.label(sym.OP_HOME, "Home") in labels
    assert sym.label(sym.OP_CLOSE, "Close") in labels

    close_builder = InlinePanelBuilder()
    _add_close_button(close_builder)
    assert sym.label(sym.OP_CLOSE, "Close") in _texts(close_builder.build())

    assert sym.label(sym.OP_HOME, "Home") in _texts(_progress_buttons())


def test_input_prompt_navigation_uses_registry_glyphs():
    from backend.helper.panels import _handle_input, register_input
    from backend.helper import panels

    captured: list = []

    async def _capture(event, text, buttons, chat_id, msg_id):
        captured.append((text, buttons))
        return True

    register_input("_sym_probe", "field", {
        "handler": lambda *a, **k: None, "prompt": "Enter", "back": "panel:_sym_probe",
    })
    original = panels._safe_edit
    panels._safe_edit = _capture
    try:
        _run(_handle_input(object(), "_sym_probe:field", OWNER, 11, 22))
    finally:
        panels._safe_edit = original
    _text, buttons = captured[-1]
    labels = _texts(buttons)
    assert sym.label(sym.OP_BACK, "Back") in labels
    assert sym.label(sym.OP_HOME, "Home") in labels
    assert sym.label(sym.OP_CLOSE, "Close") in labels


# ── 8. Taskloom list/detail/wizard glyphs, labels and callbacks ─────────────

@pytest.fixture()
def taskloom_repo(monkeypatch):
    from backend.ai.database.task_repository import InMemoryTaskRepository
    import backend.ai.task_management as tm
    from backend.bot.handlers import taskloom
    from backend.helper import inline_engine

    repo = InMemoryTaskRepository()
    real = tm.TaskManagementService
    monkeypatch.setattr(tm, "TaskManagementService", lambda r, o: real(repo, o))
    inline_engine.set_owner_id(OWNER)
    taskloom.register(client=None, owner_id=OWNER, tz_str="UTC")
    return taskloom, repo


def _make_task(repo, label="write hello", status="active"):
    return _run(repo.create_task(OWNER, {
        "label": label,
        "schedule_type": "interval",
        "schedule": {"interval_seconds": 60},
        "timezone": "UTC",
        "actions": [{"name": "send_message", "arguments": {"text": "hello"}}],
        "notification_destination": {"kind": "saved_messages"},
        "status": status,
        "next_run_at": None,
    }))


def test_taskloom_icons_come_from_the_registry(taskloom_repo):
    taskloom, _repo = taskloom_repo
    assert taskloom._STATUS_ICON == {k: v.glyph for k, v in sym.TASK_STATE_SYMBOLS.items()}
    assert taskloom._SCHEDULE_ICON == {k: v.glyph for k, v in sym.SCHEDULE_SYMBOLS.items()}


def test_taskloom_list_uses_registry_heading_and_counts(taskloom_repo):
    taskloom, repo = taskloom_repo
    _make_task(repo, "write hello")
    _title, body, buttons = _run(taskloom._taskloom_panel(None, ""))
    assert sym.PANEL_HEADING in body
    assert f"{sym.STATE_ACTIVE} 1 active" in body
    assert f"{sym.STATE_PAUSED} 0 paused" in body
    assert sym.label(sym.OP_CREATE, "New task") in _texts(buttons)
    labels = _texts(buttons)
    assert sym.label(sym.OP_BACK, "Back") in labels
    assert sym.label(sym.OP_HOME, "Home") in labels


def test_taskloom_detail_labels_and_callbacks_are_preserved(taskloom_repo):
    taskloom, repo = taskloom_repo
    task = _make_task(repo, "write hello")
    _title, body, buttons = _run(taskloom._task_detail_panel(None, str(task.id)))
    labels = _texts(buttons)
    datas = _datas(buttons)
    assert sym.label(sym.OP_PAUSE, "Pause task") in labels
    assert sym.label(sym.OP_EDIT, "Edit task") in labels
    assert sym.label(sym.OP_DELETE, "Delete task") in labels
    assert sym.label(sym.OP_REFRESH, "Refresh") in labels
    assert sym.label(sym.OP_SAVE, "Complete task") in labels
    assert sym.DESTINATION in body
    # Callback payloads (id:version CAS) are unchanged by the label redesign.
    assert f"action:taskloom_pause:{task.id}:{task.version}" in datas
    assert f"action:taskloom_complete:{task.id}:{task.version}" in datas
    assert f"action:taskloom_delete:{task.id}:{task.version}" in datas


def test_taskloom_wizard_uses_registry_labels(taskloom_repo):
    taskloom, _repo = taskloom_repo
    taskloom.reset_wizard_draft(OWNER)
    title, body, buttons = taskloom._wizard_render(taskloom._draft(OWNER))
    assert title == sym.label(sym.OP_CREATE, "New task")
    assert sym.STEP_CURRENT in body
    labels = _texts(buttons)
    assert sym.label(sym.OP_CLOSE, "Cancel") in labels
    assert sym.label(sym.OP_CLOSE, "Close") in labels


def test_taskloom_wizard_pagination_and_nav_glyphs(taskloom_repo):
    taskloom, repo = taskloom_repo
    for index in range(6):
        _make_task(repo, f"job {index}")
    _title, _body, buttons = _run(taskloom._taskloom_panel(None, "0"))
    labels = _texts(buttons)
    assert sym.OP_NEXT in labels
    assert any(label.startswith(sym.PAGE_CURRENT) for label in labels)


# ── 9. display-font pipeline preserves glyphs and callback data ─────────────

def test_font_pipeline_leaves_registry_glyphs_untouched():
    from backend.helper.font_style import apply_font, FONT_KEYS

    for key in FONT_KEYS:
        for spec in sym.ALL_SYMBOLS.values():
            assert apply_font(spec.glyph, key) == spec.glyph, (key, spec.key)


def test_font_pipeline_does_not_corrupt_a_composed_button_label():
    from backend.helper.font_style import apply_font, FONT_KEYS

    label = sym.label(sym.OP_DELETE, "Delete task")
    for key in FONT_KEYS:
        styled = apply_font(label, key)
        assert styled.startswith(sym.OP_DELETE), key


def test_registry_glyphs_have_no_variation_selector():
    for spec in sym.ALL_SYMBOLS.values():
        assert "\ufe0f" not in spec.glyph, spec.key


def test_callback_data_is_never_touched_by_font_styling():
    from backend.helper.panel_render import render_edit
    from backend.services import settings_service

    data = "action:taskloom_pause:7:3"
    previous = settings_service.dashboard_font()
    try:
        settings_service.set_dashboard_font("script")
        _text, built = render_edit("T", "B", [[(sym.label(sym.OP_PAUSE, "Pause task"), data)]])
    finally:
        settings_service.set_dashboard_font(previous)
    # The button TEXT is styled, but the callback DATA is byte-identical.
    assert built[0][0].data.decode("utf-8") == data
    assert built[0][0].text.startswith(sym.OP_PAUSE)
