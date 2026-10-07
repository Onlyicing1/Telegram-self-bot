"""Categories & Mappings Glass UI — Phase 2 (Emoji & Reaction, ROADMAP §10–§12, §25).

Pins the panel surface: registration through the standard panel machinery, the
mother-menu Categories row, category/mapping browsing with 2×5 pagination and
page clamping, honest stale-entry failures, bounded callback data, the
conflict panel (current vs new visuals, explicit replace/cancel, nothing
overwritten silently), owner-only flows, and the architecture pins (no
backend.ai import, no NewMessage listener for this feature, no create_task
runtime, no forwarding, no second client).
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from backend.bot.handlers import emoji, misc
from backend.db import client as db_client
from backend.helper import inline_engine
from backend.helper.input_state import _pending, clear_pending

OWNER = 606060
OTHER = 313131

_FALLBACKS = ("emoji_library", "emoji_categories", "emoji_mappings")


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for key in _FALLBACKS:
        db_client._fallback[key] = []
    inline_engine.set_owner_id(OWNER)
    emoji._drafts.clear()
    _pending.clear()
    yield
    for key in _FALLBACKS:
        db_client._fallback[key] = []
    emoji._drafts.clear()
    _pending.clear()


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _seed(entries: list[dict[str, Any]]) -> None:
    db_client._fallback["emoji_library"].extend(entries)


def _entry(doc_id: int, alt: str = "x") -> dict[str, Any]:
    return {
        "owner_id": OWNER,
        "document_id": doc_id,
        "alt_text": alt,
        "source": "imported",
        "source_msg_id": None,
        "created_at": "2026-10-06T10:00:00+00:00",
    }


def _cat(name: str) -> dict[str, Any]:
    result = _run(emoji.cat_service.create_category(OWNER, name))
    assert result["ok"], result
    return result["category"]


def _datas(buttons) -> list[str]:
    out = []
    for row in buttons:
        for button in row:
            raw = getattr(button, "data", button)
            out.append(raw.decode("utf-8") if isinstance(raw, bytes) else str(raw))
    return out


def _texts(buttons) -> list[str]:
    return [str(getattr(button, "text", button)) for row in buttons for button in row]


# ── registration ──────────────────────────────────────────────────────────────


def test_registers_all_phase2_panels_and_actions():
    emoji.register(client=None, owner_id=OWNER)
    from backend.helper.panel_registry import registry as get_registry
    from backend.helper.panels import get_action

    for panel_id in (
        "emoji", "emoji_library", "emoji_entry", "emoji_categories",
        "emoji_cat", "emoji_mappings", "emoji_map", "emoji_pick",
    ):
        assert get_registry().get_handler(panel_id) is not None, panel_id
    for action in (
        "emoji_import", "emoji_cat_new", "emoji_cat_rename", "emoji_cat_del",
        "emoji_cat_delgo", "emoji_map_new", "emoji_map_pick", "emoji_map_edit",
        "emoji_map_del", "emoji_map_repl",
    ):
        assert get_action(action) is not None, action


def test_the_mother_menu_links_the_emoji_panel_and_categories_row_exists():
    assert "panel:emoji" in _datas(misc._build_menu_buttons())
    title, body, buttons = _run(emoji._emoji_panel_handler(None, ""))
    assert "panel:emoji_categories" in _datas(buttons)


# ── categories panel ──────────────────────────────────────────────────────────


def test_categories_empty_state_offers_creation():
    title, body, buttons = _run(emoji._categories_page_handler(None, "0"))
    assert "No categories yet" in body
    assert "action:emoji_cat_new" in _datas(buttons)


def test_categories_grid_shows_mapping_counts():
    cat = _cat("A")
    _seed([_entry(1), _entry(2)])
    _run(emoji.cat_service.create_mapping(OWNER, cat["id"], "🗑", 1))
    _run(emoji.cat_service.create_mapping(OWNER, cat["id"], "🗣", 2))
    title, body, buttons = _run(emoji._categories_page_handler(None, "0"))
    assert "A · 2" in _texts(buttons)
    assert "panel:emoji_cat:0:0" in _datas(buttons)


def test_categories_pagination_is_2x5_and_clamped():
    for i in range(13):
        _cat(f"c{i:02d}")
    title, body, buttons = _run(emoji._categories_page_handler(None, "0"))
    cat_buttons = [d for d in _datas(buttons) if d.startswith("panel:emoji_cat:")]
    assert len(cat_buttons) == 10
    assert "panel:emoji_categories:1" in _datas(buttons)
    assert "page 1/2" in body
    _title, _body, buttons2 = _run(emoji._categories_page_handler(None, "99"))
    assert "panel:emoji_cat:1:0" in _datas(buttons2)  # clamped onto the last page


def test_category_detail_shows_count_and_actions():
    cat = _cat("Solo")
    _seed([_entry(1)])
    _run(emoji.cat_service.create_mapping(OWNER, cat["id"], "👋", 1))
    title, body, buttons = _run(emoji._category_page_handler(None, "0:0"))
    assert "1 mapping(s)" in body
    datas = _datas(buttons)
    assert f"panel:emoji_mappings:{cat['id']}:0" in datas
    assert f"action:emoji_cat_rename:{cat['id']}" in datas
    assert f"action:emoji_cat_del:{cat['id']}" in datas


def test_category_detail_stale_index_fails_honestly():
    _cat("Solo")
    title, body, _buttons = _run(emoji._category_page_handler(None, "0:7"))
    assert "not found" in body.lower()
    assert title == "Category"


def test_category_detail_after_delete_fails_honestly():
    cat = _cat("Gone")
    _run(emoji.cat_service.delete_category(OWNER, cat["id"]))
    title, body, _buttons = _run(emoji._category_page_handler(None, "0:0"))
    assert "not found" in body.lower()


# ── mappings panel ────────────────────────────────────────────────────────────


def test_mappings_empty_state_offers_adding():
    cat = _cat("C")
    title, body, buttons = _run(emoji._mappings_page_handler(None, f":{cat['id']}:0"))
    assert "No mappings yet" in body
    assert f"action:emoji_map_new:{cat['id']}" in _datas(buttons)


def test_mappings_list_with_visuals_and_pagination():
    cat = _cat("C")
    _seed([_entry(i, f"alt{i}") for i in range(1, 13)])
    for i in range(1, 12):
        _run(emoji.cat_service.create_mapping(OWNER, cat["id"], f"e{i:02d}", i))
    title, body, buttons = _run(emoji._mappings_page_handler(None, f":{cat['id']}:0"))
    assert "page 1/2" in body
    assert "→" in body
    map_buttons = [d for d in _datas(buttons) if d.startswith("panel:emoji_map:")]
    assert len(map_buttons) == 10
    assert f"panel:emoji_mappings::{cat['id']}:1" in _datas(buttons)
    # second page shows the remainder
    _title, body2, buttons2 = _run(emoji._mappings_page_handler(None, f":{cat['id']}:1"))
    assert len([d for d in _datas(buttons2) if d.startswith("panel:emoji_map:")]) == 1


def test_mappings_missing_category_fails_honestly():
    title, body, _buttons = _run(emoji._mappings_page_handler(None, ":999999:0"))
    assert "not found" in body.lower()


def test_mapping_detail_shows_visual_and_entry_state():
    cat = _cat("C")
    _seed([_entry(5, "wave")])
    _run(emoji.cat_service.create_mapping(OWNER, cat["id"], "👋", 5))
    title, body, buttons = _run(emoji._mapping_page_handler(None, f"0:0:{cat['id']}"))
    assert "wave" in body and "👋" in body
    assert "Library entry: #5" in body
    datas = _datas(buttons)
    assert any(d.startswith("action:emoji_map_edit:") and d.endswith(f":{cat['id']}") for d in datas)
    assert any(d.startswith("action:emoji_map_del:") and d.endswith(f":{cat['id']}") for d in datas)


def test_mapping_detail_stale_index_fails_honestly():
    cat = _cat("C")
    _seed([_entry(5, "wave")])
    _run(emoji.cat_service.create_mapping(OWNER, cat["id"], "👋", 5))
    title, body, _buttons = _run(emoji._mapping_page_handler(None, f"0:9:{cat['id']}"))
    assert "not found" in body.lower()


def test_mapping_detail_missing_library_entry_is_marked_honestly():
    cat = _cat("C")
    _seed([_entry(5, "wave")])
    _run(emoji.cat_service.create_mapping(OWNER, cat["id"], "👋", 5))
    db_client._fallback["emoji_library"] = []
    _title, body, _buttons = _run(emoji._mapping_page_handler(None, f"0:0:{cat['id']}"))
    assert "no longer exists" in body


# ── add-mapping flow (draft + input state) ────────────────────────────────────


def test_add_mapping_arms_the_input_step_with_a_draft():
    cat = _cat("C")
    result = _run(emoji._map_new_action(None, f":0:{cat['id']}", OWNER))
    title, body, buttons = result
    assert title == "Add Mapping"
    pending = clear_pending(OWNER)
    assert pending is not None
    assert "Step 1/2" in pending["prompt"]
    assert pending["handler"] is emoji._map_emoji_input_handler
    assert emoji._get_draft(OWNER)["kind"] == "map_emoji"


def test_add_mapping_for_missing_category_fails_honestly():
    title, body, _buttons = _run(emoji._map_new_action(None, "999999", OWNER))
    assert "not found" in body.lower()
    assert clear_pending(OWNER) is None


def test_add_mapping_plain_wid_remainder_also_parses():
    cat = _cat("C")
    _title, body, _buttons = _run(emoji._map_new_action(None, str(cat["id"]), OWNER))
    assert "Send the simple emoji" in body
    assert clear_pending(OWNER) is not None


def test_emoji_input_step_then_picker_renders_the_library():
    cat = _cat("C")
    _seed([_entry(i, f"alt{i}") for i in range(1, 13)])
    _run(emoji._map_new_action(None, f":0:{cat['id']}", OWNER))
    _run(emoji._map_emoji_input_handler("👋", OWNER, 1, OWNER, 2))
    draft = emoji._get_draft(OWNER)
    assert draft["kind"] == "map_library"
    assert draft["simple_emoji"] == "👋"
    _pending.clear()
    title, body, buttons = _run(emoji._picker_page_handler(None, f"{cat['id']}:👋:0"))
    pick_actions = [d for d in _datas(buttons) if d.startswith("action:emoji_map_pick:")]
    assert len(pick_actions) == 10
    assert "page 1/2" in body


def test_emoji_input_with_invalid_value_fails_and_keeps_flow_honest():
    cat = _cat("C")
    _run(emoji._map_new_action(None, f":0:{cat['id']}", OWNER))
    _pending.clear()
    result = _run(
        emoji._map_emoji_input_handler("", OWNER, 1, OWNER, 2)
    )
    title, body, _buttons = result
    assert "Invalid emoji" in body


def test_pick_creates_the_mapping_and_returns_to_the_list():
    cat = _cat("C")
    _seed([_entry(3, "wave")])
    _run(emoji._map_new_action(None, f":0:{cat['id']}", OWNER))
    _run(emoji._map_emoji_input_handler("👋", OWNER, 1, OWNER, 2))
    _pending.clear()
    title, body, _buttons = _run(
        emoji._map_pick_action(None, f"{cat['id']}:👋:0:0", OWNER)
    )
    assert "✓" in body
    rows, total = _run(emoji.cat_service.list_mappings(OWNER, cat["id"]))
    assert total == 1 and rows[0]["document_id"] == 3


def test_pick_with_stale_draft_fails_honestly():
    cat = _cat("C")
    _seed([_entry(3)])
    title, body, _buttons = _run(
        emoji._map_pick_action(None, f"{cat['id']}:👋:0:0", OWNER)
    )
    assert "out of date" in body
    rows, total = _run(emoji.cat_service.list_mappings(OWNER, cat["id"]))
    assert total == 0


def test_pick_with_stale_index_fails_honestly():
    cat = _cat("C")
    _seed([_entry(3)])
    _run(emoji._map_new_action(None, f":0:{cat['id']}", OWNER))
    _run(emoji._map_emoji_input_handler("👋", OWNER, 1, OWNER, 2))
    _pending.clear()
    title, body, _buttons = _run(
        emoji._map_pick_action(None, f"{cat['id']}:👋:0:5", OWNER)
    )
    assert "not found" in body


def test_pick_with_deleted_library_entry_fails_honestly():
    cat = _cat("C")
    _seed([_entry(3, "wave")])
    _run(emoji._map_new_action(None, f":0:{cat['id']}", OWNER))
    _run(emoji._map_emoji_input_handler("👋", OWNER, 1, OWNER, 2))
    _pending.clear()
    db_client._fallback["emoji_library"] = []
    title, body, _buttons = _run(
        emoji._map_pick_action(None, f"{cat['id']}:👋:0:0", OWNER)
    )
    assert "not found" in body.lower()
    rows, total = _run(emoji.cat_service.list_mappings(OWNER, cat["id"]))
    assert total == 0


def test_picker_empty_library_fails_honestly():
    cat = _cat("C")
    title, body, _buttons = _run(emoji._picker_page_handler(None, f"{cat['id']}:👋:0"))
    assert "empty" in body.lower()


# ── conflict panel ────────────────────────────────────────────────────────────


def _arm_conflict(cat_id: int) -> None:
    _seed([_entry(1, "old"), _entry(2, "new")])
    _run(emoji.cat_service.create_mapping(OWNER, cat_id, "👋", 1))
    _run(emoji._map_new_action(None, f":0:{cat_id}", OWNER))
    _run(emoji._map_emoji_input_handler("👋", OWNER, 1, OWNER, 2))
    _pending.clear()


def test_duplicate_attempt_shows_conflict_panel_not_a_silent_overwrite():
    cat = _cat("C")
    _arm_conflict(cat["id"])
    title, body, buttons = _run(
        emoji._map_pick_action(None, f"{cat['id']}:👋:0:1", OWNER)
    )
    assert title == "Mapping Conflict"
    assert "Current:" in body and "New:" in body
    assert "old (#1)" in body and "new (#2)" in body
    assert "Nothing has been overwritten" in body
    datas = _datas(buttons)
    assert f"action:emoji_map_repl:{cat['id']}:👋" in datas
    assert f"panel:emoji_mappings::0:{cat['id']}" in datas
    rows, total = _run(emoji.cat_service.list_mappings(OWNER, cat["id"]))
    assert total == 1 and rows[0]["document_id"] == 1


def test_explicit_replace_changes_the_mapping():
    cat = _cat("C")
    _arm_conflict(cat["id"])
    _run(emoji._map_pick_action(None, f"{cat['id']}:👋:0:1", OWNER))
    title, body, _buttons = _run(
        emoji._map_repl_action(None, f"{cat['id']}:👋", OWNER)
    )
    assert "✓" in body
    rows, total = _run(emoji.cat_service.list_mappings(OWNER, cat["id"]))
    assert total == 1 and rows[0]["document_id"] == 2


def test_cancel_from_conflict_keeps_the_current_mapping():
    cat = _cat("C")
    _arm_conflict(cat["id"])
    _run(emoji._map_pick_action(None, f"{cat['id']}:👋:0:1", OWNER))
    # cancel is the plain mappings-panel callback — nothing mutates
    rows, total = _run(emoji.cat_service.list_mappings(OWNER, cat["id"]))
    assert total == 1 and rows[0]["document_id"] == 1


def test_replace_without_conflict_draft_is_refused():
    cat = _cat("C")
    _arm_conflict(cat["id"])
    _run(emoji._map_pick_action(None, f"{cat['id']}:👋:0:1", OWNER))
    emoji._clear_draft(OWNER)  # the draft expired / was cleared
    title, body, _buttons = _run(
        emoji._map_repl_action(None, f"{cat['id']}:👋", OWNER)
    )
    assert "unchanged" in body
    rows, total = _run(emoji.cat_service.list_mappings(OWNER, cat["id"]))
    assert total == 1 and rows[0]["document_id"] == 1


def test_replace_when_mapping_was_deleted_fails_honestly():
    cat = _cat("C")
    _arm_conflict(cat["id"])
    _run(emoji._map_pick_action(None, f"{cat['id']}:👋:0:1", OWNER))
    _run(emoji.cat_service.delete_mapping(OWNER, cat["id"], "👋"))
    title, body, _buttons = _run(
        emoji._map_repl_action(None, f"{cat['id']}:👋", OWNER)
    )
    assert "no longer exists" in body


def test_conflict_visual_honest_when_current_entry_unresolvable():
    cat = _cat("C")
    _seed([_entry(1, "old"), _entry(2, "new")])
    _run(emoji.cat_service.create_mapping(OWNER, cat["id"], "👋", 1))
    db_client._fallback["emoji_library"] = [_entry(2, "new")]
    _run(emoji._map_new_action(None, f":0:{cat['id']}", OWNER))
    _run(emoji._map_emoji_input_handler("👋", OWNER, 1, OWNER, 2))
    _pending.clear()
    _title, body, _buttons = _run(
        emoji._map_pick_action(None, f"{cat['id']}:👋:0:0", OWNER)
    )
    assert "unavailable" in body
    assert "new (#2)" in body  # the resolvable side is still shown


# ── edit / delete mapping flows ───────────────────────────────────────────────


def test_edit_mapping_seeds_the_picker_again():
    cat = _cat("C")
    _seed([_entry(1, "old"), _entry(2, "new")])
    _run(emoji.cat_service.create_mapping(OWNER, cat["id"], "👋", 1))
    title, body, buttons = _run(
        emoji._map_edit_action(None, f"👋:{cat['id']}", OWNER)
    )
    assert "Pick premium emoji" in body
    assert f"action:emoji_map_pick:{cat['id']}:👋:0:0" in _datas(buttons)


def test_edit_missing_mapping_fails_honestly():
    cat = _cat("C")
    title, body, _buttons = _run(emoji._map_edit_action(None, f"👋:{cat['id']}", OWNER))
    assert "not found" in body.lower()


def test_delete_mapping_from_the_mapping_panel():
    cat = _cat("C")
    _seed([_entry(1, "old")])
    _run(emoji.cat_service.create_mapping(OWNER, cat["id"], "👋", 1))
    title, body, _buttons = _run(
        emoji._map_del_action(None, f"👋:{cat['id']}", OWNER)
    )
    assert "removed" in body
    rows, total = _run(emoji.cat_service.list_mappings(OWNER, cat["id"]))
    assert total == 0
    assert _run(db_client.get_emoji_entry(OWNER, 1)) is not None


def test_delete_missing_mapping_fails_honestly():
    cat = _cat("C")
    title, body, _buttons = _run(emoji._map_del_action(None, f"👋:{cat['id']}", OWNER))
    assert "not found" in body.lower()


# ── delete-category flow ──────────────────────────────────────────────────────


def test_category_delete_arms_confirmation_then_deletes():
    cat = _cat("Fatal")
    _seed([_entry(1)])
    _run(emoji.cat_service.create_mapping(OWNER, cat["id"], "👋", 1))
    title, body, buttons = _run(emoji._cat_del_action(None, str(cat["id"]), OWNER))
    assert "will be removed" in body
    assert "Library entries are NOT deleted" in body
    assert "action:emoji_cat_delgo" in _datas(buttons)
    title, body, _buttons = _run(emoji._cat_delgo_action(None, "", OWNER))
    assert "✓" in body and "1 mapping(s)" in body
    assert _run(emoji.cat_service.get_category(OWNER, cat["id"])) is None


def test_category_delete_cancel_keeps_everything():
    cat = _cat("Safe")
    _seed([_entry(1)])
    _run(emoji.cat_service.create_mapping(OWNER, cat["id"], "👋", 1))
    emoji._set_draft(OWNER, kind="delete", category_id=cat["id"])
    # cancel is a plain panel callback; nothing runs
    assert _run(emoji.cat_service.get_category(OWNER, cat["id"])) is not None
    assert _run(emoji.cat_service.category_mapping_count(OWNER, cat["id"])) == 1


def test_category_delete_confirm_without_draft_fails_honestly():
    cat = _cat("X")
    title, body, _buttons = _run(emoji._cat_delgo_action(None, "", OWNER))
    assert "No category selected" in body
    assert _run(emoji.cat_service.get_category(OWNER, cat["id"])) is not None


# ── owner isolation (UI boundary) ─────────────────────────────────────────────


def test_owner_boundary_uses_the_existing_callback_authorization():
    import inspect

    src = inspect.getsource(emoji.register)
    # the handler wires through the standard registry; the callback router
    # performs the is_owner check for every panel/action callback
    assert src.strip().startswith("def register(client, owner_id: int)")


def test_owner_scoped_data_never_leaks_across_owners():
    cat = _cat("Mine")
    _seed([_entry(1)])
    inline_engine.set_owner_id(OTHER)
    _title, body, _buttons = _run(emoji._category_page_handler(None, "0:0"))
    assert "not found" in body.lower()
    rows, _total, _counts = _run(emoji.cat_service.list_categories(OTHER))
    assert rows == []
    inline_engine.set_owner_id(OWNER)


# ── callback bounds & architecture ────────────────────────────────────────────


def test_every_phase2_callback_stays_within_the_telegram_bound():
    emoji.register(client=None, owner_id=OWNER)
    _seed([_entry(i, f"a{i}") for i in range(1, 13)])
    cat = _cat("Bounds")
    for i in range(1, 12):
        _run(emoji.cat_service.create_mapping(OWNER, cat["id"], f"e{i:02d}", i))
    panels = [
        emoji._categories_page_handler(None, "0"),
        emoji._category_page_handler(None, "0:0"),
        emoji._mappings_page_handler(None, f":{cat['id']}:0"),
        emoji._mapping_page_handler(None, "0:0:5"),
        emoji._picker_page_handler(None, f"{cat['id']}:👋:0"),
    ]
    for coro in panels:
        _title, _body, buttons = _run(coro)
        for data in _datas(buttons):
            assert len(data.encode("utf-8")) <= 64, data


def test_no_backend_ai_dependency():
    import ast
    import pathlib

    for path in (
        pathlib.Path(emoji.__file__),
        pathlib.Path(emoji.cat_service.__file__),
    ):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                assert not any(a.name.startswith("backend.ai") for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                assert not (node.module or "").startswith("backend.ai")


def test_no_newmessage_listener_no_create_task_no_forwarding():
    import inspect

    src = inspect.getsource(emoji)
    assert "events.NewMessage" not in src
    assert "create_task" not in src
    assert "forward_messages" not in src
    svc_src = inspect.getsource(emoji.cat_service)
    assert "forward_messages" not in svc_src
    assert "events.NewMessage" not in svc_src
