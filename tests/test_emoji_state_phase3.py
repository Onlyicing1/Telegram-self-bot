"""Emoji Replacement state — Phase 3 (Emoji & Reaction, ROADMAP §13/§14/§29).

Pins the Phase 3 contracts:

  1. Toggle: first-boot default OFF; ON/OFF persists and loads through the
     db-layer abstraction; OFF means resolution yields None regardless of
     any category state.
  2. Global default category: can be set to a REAL owner-scoped category;
     nonexistent and foreign-owner categories are rejected.
  3. Per-chat override: set/clear through the same validation; clearing
     restores the global default at resolution time.
  4. Resolution order: override > global default > none; the winning
     category is validated against the LIVE category table so a deleted
     category can never remain effective (fail closed to None).
  5. Owner isolation: every state mutation and read is owner-scoped.
  6. Degraded storage: a durable write failure reports False and the state
     stays honestly unchanged (never a phantom success).
  7. Glass UI: the Replacement panel exposes toggle/default/override/
     effective state and actions; actions mutate only the intended state;
     the panel requires a real target chat for per-chat state and never
     fabricates one; callbacks stay owner-scoped and bounded. Phase 4's
     transformer/reconstruction now exist in their own modules (see
     ``tests/test_emoji_replacement_phase4.py``); the Phase 3 state service
     and this UI surface must stay free of execution logic.
  8. Architecture: no second client/loop/scheduler/executor, no
     ``backend.ai`` import, no keyword routing in the new surface.

Everything runs offline against the in-memory fallback store.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from backend.bot.handlers import emoji
from backend.db import client as db_client
from backend.helper import inline_engine
from backend.helper.target_context import TargetContext, set_target, clear_target
from backend.services import emoji_category_service as cat_service
from backend.services import emoji_state_service as state_service

OWNER = 424242
OTHER = 991199
CHAT = -1001234567890
CHAT2 = -1009998887771


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    db_client._fallback["emoji_library"] = []
    db_client._fallback["emoji_categories"] = []
    db_client._fallback["emoji_mappings"] = []
    db_client._fallback["emoji_state"] = {}
    db_client._fallback["emoji_chat_overrides"] = []
    inline_engine.set_owner_id(OWNER)
    clear_target(OWNER)
    yield
    db_client._fallback["emoji_library"] = []
    db_client._fallback["emoji_categories"] = []
    db_client._fallback["emoji_mappings"] = []
    db_client._fallback["emoji_state"] = {}
    db_client._fallback["emoji_chat_overrides"] = []
    clear_target(OWNER)


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _cat(name: str, owner: int = OWNER) -> dict:
    result = _run(cat_service.create_category(owner, name))
    assert result["ok"], result
    return result["category"]


def _datas(buttons) -> list[str]:
    out = []
    for row in buttons:
        for button in row:
            raw = getattr(button, "data", button)
            out.append(raw.decode("utf-8") if isinstance(raw, bytes) else str(raw))
    return out


# ── 1. toggle: default OFF + persistence ─────────────────────────────────────


def test_replacement_defaults_off():
    assert _run(state_service.replacement_enabled(OWNER)) is False


def test_resolve_defaults_to_none_on_first_boot():
    assert _run(state_service.resolve_effective_category(OWNER, CHAT)) is None


def test_toggle_on_persists_and_loads():
    assert _run(state_service.set_replacement_enabled(OWNER, True)) is True
    assert _run(state_service.replacement_enabled(OWNER)) is True


def test_toggle_off_persists_and_loads():
    _run(state_service.set_replacement_enabled(OWNER, True))
    assert _run(state_service.set_replacement_enabled(OWNER, False)) is True
    assert _run(state_service.replacement_enabled(OWNER)) is False


def test_toggle_flips_the_value():
    assert _run(state_service.toggle_replacement(OWNER)) is True
    assert _run(state_service.replacement_enabled(OWNER)) is True
    assert _run(state_service.toggle_replacement(OWNER)) is False
    assert _run(state_service.replacement_enabled(OWNER)) is False


def test_toggle_rejects_invalid_owner():
    assert _run(state_service.set_replacement_enabled(0, True)) is False
    assert _run(state_service.toggle_replacement(0)) is False


# ── 2/3. global default + override validation ────────────────────────────────


def test_global_default_can_be_set():
    cat = _cat("002")
    assert _run(state_service.set_global_default_category(OWNER, cat["id"])) is True
    assert _run(state_service.get_global_default_category(OWNER)) == cat["id"]


def test_nonexistent_global_category_is_rejected():
    assert _run(state_service.set_global_default_category(OWNER, 999999)) is False
    assert _run(state_service.get_global_default_category(OWNER)) is None


def test_foreign_owner_global_category_is_rejected():
    foreign = _cat("Foreign", owner=OTHER)
    assert _run(state_service.set_global_default_category(OWNER, foreign["id"])) is False
    assert _run(state_service.get_global_default_category(OWNER)) is None


def test_global_default_can_be_cleared():
    cat = _cat("002")
    _run(state_service.set_global_default_category(OWNER, cat["id"]))
    assert _run(state_service.clear_global_default_category(OWNER)) is True
    assert _run(state_service.get_global_default_category(OWNER)) is None


def test_chat_override_can_be_set():
    cat = _cat("Aya")
    assert _run(state_service.set_chat_override(OWNER, CHAT, cat["id"])) is True
    assert _run(state_service.get_chat_override(OWNER, CHAT)) == cat["id"]


def test_chat_override_can_be_cleared():
    cat = _cat("Aya")
    _run(state_service.set_chat_override(OWNER, CHAT, cat["id"]))
    assert _run(state_service.clear_chat_override(OWNER, CHAT)) is True
    assert _run(state_service.get_chat_override(OWNER, CHAT)) is None


def test_nonexistent_override_category_is_rejected():
    assert _run(state_service.set_chat_override(OWNER, CHAT, 123456)) is False
    assert _run(state_service.get_chat_override(OWNER, CHAT)) is None


def test_foreign_owner_override_category_is_rejected():
    foreign = _cat("Foreign", owner=OTHER)
    assert _run(state_service.set_chat_override(OWNER, CHAT, foreign["id"])) is False
    assert _run(state_service.get_chat_override(OWNER, CHAT)) is None


def test_override_is_per_chat():
    cat_a, cat_b = _cat("A"), _cat("B")
    _run(state_service.set_chat_override(OWNER, CHAT, cat_a["id"]))
    _run(state_service.set_chat_override(OWNER, CHAT2, cat_b["id"]))
    assert _run(state_service.get_chat_override(OWNER, CHAT)) == cat_a["id"]
    assert _run(state_service.get_chat_override(OWNER, CHAT2)) == cat_b["id"]


# ── 4. resolution order ──────────────────────────────────────────────────────


def test_resolution_override_wins_over_global_default():
    cat_g, cat_o = _cat("Global"), _cat("Override")
    _run(state_service.set_replacement_enabled(OWNER, True))
    _run(state_service.set_global_default_category(OWNER, cat_g["id"]))
    _run(state_service.set_chat_override(OWNER, CHAT, cat_o["id"]))
    assert _run(state_service.resolve_effective_category(OWNER, CHAT)) == cat_o["id"]


def test_resolution_falls_back_to_global_default_after_clear():
    cat_g, cat_o = _cat("Global"), _cat("Override")
    _run(state_service.set_replacement_enabled(OWNER, True))
    _run(state_service.set_global_default_category(OWNER, cat_g["id"]))
    _run(state_service.set_chat_override(OWNER, CHAT, cat_o["id"]))
    _run(state_service.clear_chat_override(OWNER, CHAT))
    assert _run(state_service.resolve_effective_category(OWNER, CHAT)) == cat_g["id"]


def test_resolution_none_without_override_and_default():
    _run(state_service.set_replacement_enabled(OWNER, True))
    assert _run(state_service.resolve_effective_category(OWNER, CHAT)) is None


def test_replacement_off_means_no_resolution_despite_categories():
    cat_g, cat_o = _cat("Global"), _cat("Override")
    _run(state_service.set_global_default_category(OWNER, cat_g["id"]))
    _run(state_service.set_chat_override(OWNER, CHAT, cat_o["id"]))
    # toggle is OFF (default) — resolution must be None regardless.
    assert _run(state_service.resolve_effective_category(OWNER, CHAT)) is None
    _run(state_service.set_replacement_enabled(OWNER, True))
    assert _run(state_service.resolve_effective_category(OWNER, CHAT)) == cat_o["id"]


def test_resolution_uses_global_default_in_other_chats():
    cat_g, cat_o = _cat("Global"), _cat("Override")
    _run(state_service.set_replacement_enabled(OWNER, True))
    _run(state_service.set_global_default_category(OWNER, cat_g["id"]))
    _run(state_service.set_chat_override(OWNER, CHAT, cat_o["id"]))
    assert _run(state_service.resolve_effective_category(OWNER, CHAT2)) == cat_g["id"]


def test_deleted_global_category_cannot_stay_effective():
    cat = _cat("Doomed")
    _run(state_service.set_replacement_enabled(OWNER, True))
    _run(state_service.set_global_default_category(OWNER, cat["id"]))
    _run(cat_service.delete_category(OWNER, cat["id"]))
    # The stored id survives, but resolution validates against the LIVE table
    # and fails closed to None instead of fabricating another category.
    assert _run(state_service.get_global_default_category(OWNER)) == cat["id"]
    assert _run(state_service.resolve_effective_category(OWNER, CHAT)) is None


def test_deleted_override_category_cannot_stay_effective():
    cat_g, cat_o = _cat("Global"), _cat("Doomed")
    _run(state_service.set_replacement_enabled(OWNER, True))
    _run(state_service.set_global_default_category(OWNER, cat_g["id"]))
    _run(state_service.set_chat_override(OWNER, CHAT, cat_o["id"]))
    _run(cat_service.delete_category(OWNER, cat_o["id"]))
    # A deleted override fails closed — it does NOT silently fall back.
    assert _run(state_service.resolve_effective_category(OWNER, CHAT)) is None


def test_resolution_with_unknown_chat_uses_global_default():
    cat_g = _cat("Global")
    _run(state_service.set_replacement_enabled(OWNER, True))
    _run(state_service.set_global_default_category(OWNER, cat_g["id"]))
    assert _run(state_service.resolve_effective_category(OWNER, None)) == cat_g["id"]


# ── 5. owner isolation ───────────────────────────────────────────────────────


def test_owner_isolation_on_state_reads_and_writes():
    cat = _cat("Mine")
    _run(state_service.set_replacement_enabled(OWNER, True))
    _run(state_service.set_global_default_category(OWNER, cat["id"]))
    _run(state_service.set_chat_override(OWNER, CHAT, cat["id"]))

    assert _run(state_service.replacement_enabled(OTHER)) is False
    assert _run(state_service.get_global_default_category(OTHER)) is None
    assert _run(state_service.get_chat_override(OTHER, CHAT)) is None
    assert _run(state_service.resolve_effective_category(OTHER, CHAT)) is None
    assert _run(state_service.toggle_replacement(OTHER)) is True
    assert _run(state_service.replacement_enabled(OWNER)) is True  # untouched


# ── 6. degraded storage honesty ──────────────────────────────────────────────


def test_degraded_write_reports_false_and_state_unchanged(monkeypatch):
    cat = _cat("002")
    _run(state_service.set_replacement_enabled(OWNER, True))
    _run(state_service.set_global_default_category(OWNER, cat["id"]))

    async def _fail(*args, **kwargs):
        return False

    monkeypatch.setattr(db_client, "upsert_emoji_state", _fail)
    assert _run(state_service.set_replacement_enabled(OWNER, False)) is False
    monkeypatch.undo()

    # The state was NOT mutated by the failed write.
    assert _run(state_service.replacement_enabled(OWNER)) is True
    assert _run(state_service.get_global_default_category(OWNER)) == cat["id"]


def test_degraded_override_write_reports_false(monkeypatch):
    cat = _cat("Aya")

    async def _fail(*args, **kwargs):
        return False

    monkeypatch.setattr(db_client, "upsert_emoji_chat_override", _fail)
    assert _run(state_service.set_chat_override(OWNER, CHAT, cat["id"])) is False
    assert _run(state_service.get_chat_override(OWNER, CHAT)) is None


def test_degraded_read_reports_unset_not_error(monkeypatch):
    async def _fail(*args, **kwargs):
        return None

    monkeypatch.setattr(db_client, "get_emoji_state", _fail)
    # A failed read looks like "not set": OFF, no categories, no replacement.
    assert _run(state_service.replacement_enabled(OWNER)) is False
    assert _run(state_service.resolve_effective_category(OWNER, CHAT)) is None


# ── 7. Glass UI ──────────────────────────────────────────────────────────────


def test_ui_registers_replacement_panels_and_actions():
    emoji.register(client=None, owner_id=OWNER)
    from backend.helper.panel_registry import registry as get_registry
    from backend.helper.panels import get_action

    assert get_registry().get_handler("emoji_replacement") is not None
    assert get_registry().get_handler("emoji_state_scope") is not None
    for action_id in (
        "emoji_state_toggle",
        "emoji_state_global_pick",
        "emoji_state_override_pick",
        "emoji_state_choose",
        "emoji_state_clear",
    ):
        assert get_action(action_id) is not None, action_id


def test_main_panel_shows_replacement_state_and_row():
    _run(state_service.set_replacement_enabled(OWNER, True))
    title, body, buttons = _run(emoji._emoji_panel_handler(None, ""))
    assert "Replacement: ✅ ON" in body
    datas = _datas(buttons)
    assert "panel:emoji_replacement" in datas


def test_main_panel_shows_replacement_off_by_default():
    title, body, _buttons = _run(emoji._emoji_panel_handler(None, ""))
    assert "Replacement: ❌ OFF" in body


def test_replacement_panel_shows_full_state():
    cat_g, cat_o = _cat("Global"), _cat("PerChat")
    set_target(
        OWNER, TargetContext(owner_id=OWNER, kind="reply", reply_chat_id=CHAT, reply_msg_id=1)
    )
    _run(state_service.set_replacement_enabled(OWNER, True))
    _run(state_service.set_global_default_category(OWNER, cat_g["id"]))
    _run(state_service.set_chat_override(OWNER, CHAT, cat_o["id"]))

    title, body, _buttons = _run(emoji._replacement_panel_handler(None, ""))
    assert "Replacement: ✅ ON" in body
    assert "Global default: Global" in body
    assert f"Override for chat `{CHAT}`: PerChat" in body
    assert "Effective here: PerChat" in body


def test_replacement_panel_shows_effective_global_when_no_override():
    cat_g = _cat("Global")
    set_target(
        OWNER, TargetContext(owner_id=OWNER, kind="reply", reply_chat_id=CHAT, reply_msg_id=1)
    )
    _run(state_service.set_replacement_enabled(OWNER, True))
    _run(state_service.set_global_default_category(OWNER, cat_g["id"]))
    _title, body, _buttons = _run(emoji._replacement_panel_handler(None, ""))
    assert "Override for chat" in body
    assert "_none_" in body
    assert "Effective here: Global" in body


def test_replacement_panel_without_target_chat_does_not_fabricate_one():
    _run(state_service.set_replacement_enabled(OWNER, True))
    _cat("Global")
    _title, body, buttons = _run(emoji._replacement_panel_handler(None, ""))
    assert "no target chat" in body
    datas = _datas(buttons)
    assert "action:emoji_state_override_pick" not in datas
    assert "action:emoji_state_clear" not in datas
    # Global actions remain available.
    assert "action:emoji_state_global_pick" in datas


def test_replacement_panel_off_shows_honest_effective_none():
    cat_g = _cat("Global")
    set_target(
        OWNER, TargetContext(owner_id=OWNER, kind="reply", reply_chat_id=CHAT, reply_msg_id=1)
    )
    _run(state_service.set_global_default_category(OWNER, cat_g["id"]))
    _title, body, _buttons = _run(emoji._replacement_panel_handler(None, ""))
    assert "Replacement: ❌ OFF" in body
    assert "none — replacement is OFF" in body


def test_toggle_action_mutates_only_the_toggle():
    cat_g = _cat("Global")
    _run(state_service.set_global_default_category(OWNER, cat_g["id"]))
    set_target(
        OWNER, TargetContext(owner_id=OWNER, kind="reply", reply_chat_id=CHAT, reply_msg_id=1)
    )
    _title, _body, _buttons = _run(emoji._state_toggle_action(None, "", CHAT))
    assert _run(state_service.replacement_enabled(OWNER)) is True
    assert _run(state_service.get_global_default_category(OWNER)) == cat_g["id"]
    _title, body, _buttons = _run(emoji._state_toggle_action(None, "", CHAT))
    assert _run(state_service.replacement_enabled(OWNER)) is False


def test_choose_action_sets_only_the_global_default():
    cat_a, cat_b = _cat("A"), _cat("B")
    _title, _body, _buttons = _run(
        emoji._state_choose_action(None, f"global:0:{cat_a['id']}", CHAT)
    )
    assert _run(state_service.get_global_default_category(OWNER)) == cat_a["id"]
    assert _run(state_service.get_chat_override(OWNER, CHAT)) is None
    # A second choose replaces the global default — and nothing else.
    _run(emoji._state_choose_action(None, f"global:0:{cat_b['id']}", CHAT))
    assert _run(state_service.get_global_default_category(OWNER)) == cat_b["id"]


def test_choose_action_sets_only_the_override():
    cat_a, cat_b = _cat("A"), _cat("B")
    _run(emoji._state_choose_action(None, f"override:{CHAT}:{cat_a['id']}", CHAT))
    assert _run(state_service.get_chat_override(OWNER, CHAT)) == cat_a["id"]
    assert _run(state_service.get_global_default_category(OWNER)) is None
    assert _run(state_service.get_chat_override(OWNER, CHAT2)) is None


def test_choose_action_rejects_foreign_category():
    foreign = _cat("Foreign", owner=OTHER)
    _title, body, _buttons = _run(
        emoji._state_choose_action(None, f"global:0:{foreign['id']}", CHAT)
    )
    assert "not found" in body or "nothing changed" in body
    assert _run(state_service.get_global_default_category(OWNER)) is None


def test_clear_action_clears_only_the_override():
    cat_g, cat_o = _cat("Global"), _cat("PerChat")
    _run(state_service.set_replacement_enabled(OWNER, True))
    _run(state_service.set_global_default_category(OWNER, cat_g["id"]))
    _run(state_service.set_chat_override(OWNER, CHAT, cat_o["id"]))

    _title, body, _buttons = _run(emoji._state_clear_action(None, str(CHAT), CHAT))
    assert "cleared" in body
    assert _run(state_service.get_chat_override(OWNER, CHAT)) is None
    # Global default untouched; resolution now falls back to it.
    assert _run(state_service.get_global_default_category(OWNER)) == cat_g["id"]
    assert _run(state_service.resolve_effective_category(OWNER, CHAT)) == cat_g["id"]


def test_state_picker_lists_owner_categories_only():
    cat = _cat("Mine")
    _cat("Theirs", owner=OTHER)
    _title, _body, buttons = _run(emoji._render_state_picker(OWNER, "global:0:0"))
    datas = _datas(buttons)
    assert f"action:emoji_state_choose:global:0:{cat['id']}" in datas
    assert not any(":" in d and d.rstrip(":").endswith("Theirs") for d in datas)


def test_state_picker_empty_library_prompts_creation():
    _title, body, _buttons = _run(emoji._render_state_picker(OWNER, "global:0:0"))
    assert "No categories" in body


def test_ui_callback_data_stays_bounded():
    _cat("A-very-long-category-name-for-bounds-checking")
    set_target(
        OWNER, TargetContext(owner_id=OWNER, kind="reply", reply_chat_id=CHAT, reply_msg_id=1)
    )
    for render in (
        emoji._replacement_panel_handler(None, ""),
        emoji._render_state_picker(OWNER, "global:0:0"),
    ):
        _title, _body, buttons = _run(render) if asyncio.iscoroutine(render) else render
        for data in _datas(buttons):
            assert len(data.encode("utf-8")) <= 64


def test_ui_owner_scoping_via_engine_owner():
    # The engine's owner is OWNER; OTHER's categories must not appear.
    foreign = _cat("Foreign", owner=OTHER)
    _title, _body, buttons = _run(emoji._render_state_picker(OWNER, "global:0:0"))
    datas = _datas(buttons)
    assert all(str(foreign["id"]) not in d for d in datas)


# ── 8. architecture / phase boundary ─────────────────────────────────────────


def test_no_second_infrastructure_and_no_ai_import():
    import ast
    from pathlib import Path

    for module_path in (
        Path(emoji.__file__),
        Path(state_service.__file__),
    ):
        source = module_path.read_text()
        tree = ast.parse(source)
        imported = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.extend(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.append(node.module)
        assert not any(
            n == "backend.ai" or n.startswith("backend.ai.") for n in imported
        ), module_path
        for forbidden in (
            "TelegramClient",
            "run_until_disconnected",
            "create_task",
            "immortal_create_task",
            "forward_messages",
            "SendMessagesRequest",
            "events.NewMessage",
        ):
            assert forbidden not in source, (module_path, forbidden)


def test_phase3_surface_keeps_no_execution_logic():
    """Phase 4 (transformer + reconstruction + loop prevention) lives in its
    own modules; the Phase 3 state service and the emoji UI must never grow
    transformation, delivery, deletion, or loop-prevention logic."""
    import re
    from pathlib import Path

    emoji_source = Path(emoji.__file__).read_text()
    state_source = Path(state_service.__file__).read_text()
    for source in (emoji_source, state_source):
        assert "transform" not in source.lower()
        assert not re.search(r"\breconstruct", source, re.IGNORECASE)
        assert not re.search(r"\bdelete_original\b", source, re.IGNORECASE)
        assert not re.search(r"\bbridge_send\b", source, re.IGNORECASE)
        assert "emoji_transformer" not in source
        assert "emoji_replacement_service" not in source
        assert "send_reconstructed" not in source


def test_state_service_resolves_without_telegram():
    # The resolution boundary is pure state + db — a resolved category is a
    # plain int, no Telegram objects, no sends.
    cat = _cat("002")
    _run(state_service.set_replacement_enabled(OWNER, True))
    _run(state_service.set_global_default_category(OWNER, cat["id"]))
    result = _run(state_service.resolve_effective_category(OWNER, CHAT))
    assert isinstance(result, int)
    assert result == cat["id"]
