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


# ── 5. registry conformance: no full-color emoji in migrated modules ────────

# The registry owns abstract, monochrome marks only. Full-color emoji and the
# U+FE0F variation selector must never appear as literals in the migrated
# production modules — those are the glyphs the font pipeline cannot style and
# the registry deliberately replaced.
_EMOJI_RANGES = (
    (0x1F000, 0x1FAFF),   # emoji / pictographs / transport / symbols
    (0x2600, 0x26FF),     # misc symbols (☀ ☺ ⚡ …)
    (0x2700, 0x27BF),     # dingbats (✅ ✂ ✈ …)
    (0x2B00, 0x2BFF),     # misc symbols and arrows (⬅ ⬡ …)
    (0x1F1E6, 0x1F1FF),   # regional indicators
)


def _is_full_color_emoji(ch: str) -> bool:
    o = ord(ch)
    return any(lo <= o <= hi for lo, hi in _EMOJI_RANGES)


# base.py keeps the *documented* legacy service-failure prefix set on purpose;
# reactions.py and the emoji library expose owner-authored emoji as data.
_CONFORMANCE_EXEMPT = {"symbols.py", "base.py", "reactions.py", "font_style.py"}


def _string_literals(path):
    """Yield (lineno, value) for every non-docstring string literal."""
    import ast

    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                docstrings.add(id(body[0].value))
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
            yield node.lineno, node.value
        elif isinstance(node, ast.JoinedStr):
            for part in node.values:
                if isinstance(part, ast.Constant) and isinstance(part.value, str):
                    yield node.lineno, part.value


def test_migrated_production_modules_have_no_full_color_emoji():
    from pathlib import Path

    backend = Path(__file__).resolve().parent.parent / "backend"
    offenders: list[str] = []
    for path in sorted(backend.rglob("*.py")):
        if path.name in _CONFORMANCE_EXEMPT:
            continue
        for lineno, value in _string_literals(path):
            bad = sorted({c for c in value if _is_full_color_emoji(c)})
            if bad:
                offenders.append(f"{path.relative_to(backend)}:{lineno} {bad}")
    assert not offenders, "full-color emoji remain in production code:\n" + "\n".join(offenders)


def test_registry_exposes_model_credential_bullet_and_empty_page_glyphs():
    assert sym.ICON_MODEL and sym.ICON_MODEL == sym.CONTENT_AI
    assert sym.ICON_CREDENTIAL
    assert sym.BULLET == "\u2022"
    assert sym.PAGE_EMPTY == "\u25b1"
    for key in ("model", "credential", "bullet", "page_empty"):
        assert key in sym.ALL_SYMBOLS
    assert sym.ALL_SYMBOLS["model"].glyph == sym.ICON_MODEL


def test_ai_test_status_icons_come_from_the_registry():
    from backend.bot.handlers import ai as ai_module

    icons = ai_module._TEST_STATUS_ICONS
    assert icons["AVAILABLE"] == sym.OP_SAVE
    assert icons["AUTH_ERROR"] == sym.STATE_FAILED
    assert set(icons.values()) <= {sym.OP_SAVE, sym.STATE_FAILED, "\u2026", "\u00b7"}


def test_ai_test_results_use_registry_glyphs_not_literals():
    from backend.bot.handlers import ai as ai_module

    payload = {
        "results": [
            {"provider": "groq", "model": "m1", "status": "AVAILABLE", "display_name": "Groq"},
            {"provider": "openai", "model": "gpt-4o", "status": "AUTH_ERROR", "display_name": "OpenAI"},
        ],
        "summary": {"available": 1, "failed": 1},
    }
    body, _buttons = ai_module._render_test_results(payload, current=("groq", "m1"))
    assert f"**{sym.OP_SAVE} Usable Models**" in body
    assert f"{sym.ICON_MODEL} **Groq**" in body
    assert sym.MEDIA_VOICE in body          # the active model's in-place mark
    assert sym.STATE_FAILED in body         # the failure prefix
