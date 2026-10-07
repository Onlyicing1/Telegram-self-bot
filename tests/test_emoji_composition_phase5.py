"""Custom Category / Composition — Phase 5 (Emoji & Reaction, ROADMAP §26).

Pins the Phase 5 contracts on top of the Phase 0–4 feature:

  1. Model: a Custom Category is explicit row TYPE metadata (``is_custom`` —
     never inferred from the name) and its mappings stay ordinary
     ``emoji_mappings`` rows, so listing, resolution, the active-category
     state and the Phase 4 transformer need no custom-category knowledge.
  2. Composition: sources are owner-scoped, distinct, real categories in a
     deterministic owner-chosen order (the owner's order IS the precedence);
     self-composition and cycles are rejected; a nested Custom Category is
     flattened to concrete mappings; nothing is fabricated.
  3. Conflicts: the same simple emoji in several sources is never silently
     overwritten — the composition is REFUSED until the owner confirms, and
     the stored snapshot then contains at most one mapping per simple emoji.
  4. Snapshot semantics (§34-F default, owner confirmation still OPEN): a
     snapshot is never a live reference — source edits/additions/deletions do
     not move it, only an explicit Refresh rebuilds it, missing sources are
     reported honestly, and a failed Refresh keeps the previous valid
     snapshot.
  5. UI: custom creation, the compose panel, the 2×5 source picker, refresh,
     disabled manual edits on a composed category, bounded callback data,
     owner isolation, stale-callback honesty, and no regression for ordinary
     categories.
  6. Integration: a Custom Category drives the replacement as global default
     and as per-chat override through the existing Phase 3 resolution and the
     existing Phase 4 pipeline, with no AI import, no second update loop, no
     scheduler/executor, and no regex/keyword routing.

Everything runs offline against the in-memory fallback store and a fake
helper-bot/self-client surface. No live Telegram/Supabase behavior is claimed.
"""
from __future__ import annotations

import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import backend.helper.client as helper_client
from backend.bot.handlers import emoji
from backend.db import client as db_client
from backend.helper import inline_engine
from backend.helper.input_state import clear_pending
from backend.services import emoji_category_service as cat_service
from backend.services import emoji_replacement_service as repl
from backend.services import emoji_state_service as state_service
from backend.telegram_api._helpers import serialize_message

OWNER = 424242
OTHER = 991199
CHAT = -100500
CHAT2 = -100501
BOT_ID = 7001
KEY = "🗑"
DOC = 42001

_FALLBACKS = (
    "emoji_library", "emoji_categories", "emoji_mappings",
    "emoji_state", "emoji_chat_overrides",
)


@pytest.fixture(autouse=True)
def _env():
    for key in _FALLBACKS:
        db_client._fallback[key] = {} if key == "emoji_state" else []
    inline_engine.set_owner_id(OWNER)
    emoji._drafts.clear()
    clear_pending(OWNER)
    repl.reset_loop_guard()
    yield
    for key in _FALLBACKS:
        db_client._fallback[key] = {} if key == "emoji_state" else []
    emoji._drafts.clear()
    clear_pending(OWNER)
    repl.reset_loop_guard()


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ── seeding helpers ───────────────────────────────────────────────────────────


def _lib(document_id: int, alt: str = "😀", owner: int = OWNER) -> dict:
    row = {
        "owner_id": owner,
        "document_id": document_id,
        "alt_text": alt,
        "source": "imported",
        "source_msg_id": 5,
        "created_at": "2026-10-06T10:00:00+00:00",
    }
    db_client._fallback["emoji_library"].append(row)
    return row


def _create(name: str, *, custom: bool = False, owner: int = OWNER) -> dict:
    result = _run(cat_service.create_category(owner, name, is_custom=custom))
    assert result["ok"], result
    return result["category"]


def _cat(name: str, owner: int = OWNER) -> dict:
    return _create(name, owner=owner)


def _custom(name: str, owner: int = OWNER) -> dict:
    return _create(name, custom=True, owner=owner)


def _map(category_id: int, simple: str, document_id: int, owner: int = OWNER) -> None:
    result = _run(cat_service.create_mapping(owner, category_id, simple, document_id))
    assert result["ok"], result


def _snapshot(category_id: int, owner: int = OWNER) -> dict[str, int]:
    rows, _total = _run(cat_service.list_mappings(owner, category_id, limit=1000, offset=0))
    return {row["simple_emoji"]: row["document_id"] for row in rows}


def _compose(category_id: int, sources: list, *, confirm: bool = False, owner: int = OWNER):
    return _run(
        cat_service.compose_category(owner, category_id, sources, confirm_conflicts=confirm)
    )


def _refresh(category_id: int, *, confirm: bool = False, owner: int = OWNER):
    return _run(cat_service.refresh_category(owner, category_id, confirm_conflicts=confirm))


def _stored(category_id: int, owner: int = OWNER) -> dict | None:
    return _run(cat_service.get_category(owner, category_id))


# ── telegram-level fakes (the surface the bridge already consumes) ────────────


class _FakeBot:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self._next_id = 9001

    def is_connected(self) -> bool:
        return True

    async def send_message(self, peer, text, *, formatting_entities=None, reply_to=None):
        self.calls.append({
            "peer": peer, "text": text,
            "formatting_entities": formatting_entities, "reply_to": reply_to,
        })
        return SimpleNamespace(
            id=self._next_id, chat_id=peer[1] if isinstance(peer, tuple) else 0,
            sender_id=BOT_ID, text=text, message=None, date=None, media=None,
            reply_to=None, out=False, entities=list(formatting_entities or []),
            inline_message_id=None, peer_id=None,
        )


class _FakeSelfClient:
    def __init__(self) -> None:
        self.resolved: list[Any] = []
        self.deleted: list[tuple[Any, tuple[int, ...]]] = []

    async def get_input_entity(self, chat_id):
        self.resolved.append(chat_id)
        return ("peer", chat_id)

    async def delete_messages(self, chat_id, msg_ids):
        self.deleted.append((chat_id, tuple(msg_ids)))
        return len(msg_ids)


@pytest.fixture
def bot(monkeypatch):
    fake = _FakeBot()
    monkeypatch.setattr(helper_client, "_client", fake)
    monkeypatch.setattr(helper_client, "_bot_id", BOT_ID)
    return fake


@pytest.fixture
def self_client():
    return _FakeSelfClient()


def _message(msg_id: int = 11, text: str = "", *, chat_id: int = CHAT):
    return SimpleNamespace(
        id=msg_id, chat_id=chat_id, sender_id=OWNER, text=text, message=None,
        date=None, media=None, reply_to=None, out=True, entities=[],
        via_bot_id=None,
    )


def _process(self_client, message, *, owner_id: int = OWNER):
    return _run(repl.process_outgoing_message(
        owner_id=owner_id,
        client=self_client,
        message=serialize_message(message),
        via_bot_id=getattr(message, "via_bot_id", None),
    ))


# ── UI helpers ────────────────────────────────────────────────────────────────


def _datas(buttons) -> list[str]:
    out = []
    for row in buttons:
        for button in row:
            raw = getattr(button, "data", button)
            out.append(raw.decode("utf-8") if isinstance(raw, bytes) else str(raw))
    return out


def _texts(buttons) -> list[str]:
    return [str(getattr(button, "text", button)) for row in buttons for button in row]


# ── A. custom category model ──────────────────────────────────────────────────


def test_create_custom_category_records_explicit_type_metadata():
    row = _custom("Kit")
    assert row["is_custom"] is True
    assert cat_service.is_custom_category(row) is True
    assert cat_service.category_sources(row) == []
    assert _stored(row["id"])["is_custom"] is True


def test_ordinary_category_stays_ordinary():
    row = _cat("Plain")
    assert cat_service.is_custom_category(row) is False
    assert cat_service.category_sources(row) == []
    assert "is_custom" not in {k: v for k, v in row.items() if v is True}


def test_type_is_never_inferred_from_the_name():
    named_custom = _cat("Custom")
    assert cat_service.is_custom_category(named_custom) is False
    plan = _run(cat_service.plan_composition(OWNER, named_custom["id"], [1]))
    assert plan["ok"] is False
    assert plan["error"] == cat_service.E_NOT_CUSTOM


def test_custom_name_uniqueness_is_shared_with_ordinary_categories():
    _custom("Shared")
    result = _run(cat_service.create_category(OWNER, "Shared"))
    assert result["ok"] is False
    assert result["error"] == cat_service.E_NAME_EXISTS
    rows, total, _counts = _run(cat_service.list_categories(OWNER))
    assert total == 1 and len(rows) == 1


def test_custom_category_listing_carries_the_type_flag():
    _cat("Ordinary")
    custom = _custom("Composed")
    rows, total, _counts = _run(cat_service.list_categories(OWNER))
    assert total == 2
    flags = {row["id"]: cat_service.is_custom_category(row) for row in rows}
    assert flags[custom["id"]] is True
    assert [flag for cid, flag in flags.items() if cid != custom["id"]] == [False]


def test_rename_custom_category_keeps_type_and_snapshot():
    source = _cat("Source")
    _lib(DOC, "😀")
    _map(source["id"], KEY, DOC)
    custom = _custom("One")
    assert _compose(custom["id"], [source["id"]])["ok"] is True
    result = _run(cat_service.rename_category(OWNER, custom["id"], "Two"))
    assert result["ok"] is True
    assert result["category"]["is_custom"] is True
    assert cat_service.category_sources(result["category"]) == [source["id"]]
    assert _snapshot(custom["id"]) == {KEY: DOC}


def test_delete_custom_category_removes_snapshot_and_keeps_library():
    source = _cat("Source")
    _lib(DOC, "😀")
    _map(source["id"], KEY, DOC)
    custom = _custom("Composed")
    _compose(custom["id"], [source["id"]])
    result = _run(cat_service.delete_category(OWNER, custom["id"]))
    assert result["ok"] is True
    assert result["removed_mappings"] == 1
    assert _stored(custom["id"]) is None
    assert _run(db_client.get_emoji_entry(OWNER, DOC)) is not None
    assert _snapshot(source["id"]) == {KEY: DOC}


# ── B. composition basics ─────────────────────────────────────────────────────


def test_compose_from_one_source_copies_its_mappings():
    source = _cat("Source")
    _lib(1, "fire")
    _lib(2, "wastebasket")
    _map(source["id"], "🔥", 1)
    _map(source["id"], KEY, 2)
    custom = _custom("Composed")

    result = _compose(custom["id"], [source["id"]])
    assert result["ok"] is True
    assert result["sources"] == [source["id"]]
    assert result["added"] == 2 and result["updated"] == 0 and result["removed"] == 0
    assert result["conflicts"] == []
    assert _snapshot(custom["id"]) == {"🔥": 1, KEY: 2}
    assert cat_service.category_sources(_stored(custom["id"])) == [source["id"]]


def test_mappings_keep_referencing_the_existing_library_entries():
    source = _cat("Source")
    _lib(DOC, "😀")
    _map(source["id"], KEY, DOC)
    custom = _custom("Composed")
    _compose(custom["id"], [source["id"]])
    rows, _total = _run(cat_service.list_mappings(OWNER, custom["id"]))
    row = rows[0]
    assert row["category_id"] == custom["id"]
    assert row["document_id"] == DOC
    assert "alt_text" not in row and "source" not in row
    assert len(db_client._fallback["emoji_library"]) == 1  # never duplicated


def test_compose_from_multiple_sources_merges_distinct_emojis():
    first, second = _cat("First"), _cat("Second")
    _lib(1, "fire")
    _lib(2, "wastebasket")
    _map(first["id"], "🔥", 1)
    _map(second["id"], KEY, 2)
    custom = _custom("Composed")

    result = _compose(custom["id"], [first["id"], second["id"]])
    assert result["ok"] is True
    assert result["sources"] == [first["id"], second["id"]]
    assert _snapshot(custom["id"]) == {"🔥": 1, KEY: 2}
    assert cat_service.category_sources(_stored(custom["id"])) == [first["id"], second["id"]]


def test_owner_source_order_is_the_precedence():
    first, second = _cat("First"), _cat("Second")
    _lib(1, "wastebasket")
    _lib(2, "wastebasket")
    _map(first["id"], KEY, 1)
    _map(second["id"], KEY, 2)

    left = _custom("Left")
    right = _custom("Right")
    assert _compose(left["id"], [first["id"], second["id"]], confirm=True)["ok"] is True
    assert _compose(right["id"], [second["id"], first["id"]], confirm=True)["ok"] is True
    assert _snapshot(left["id"]) == {KEY: 1}
    assert _snapshot(right["id"]) == {KEY: 2}


def test_recomposing_the_same_sources_is_idempotent():
    source = _cat("Source")
    _lib(DOC, "😀")
    _map(source["id"], KEY, DOC)
    custom = _custom("Composed")
    _compose(custom["id"], [source["id"]])

    again = _compose(custom["id"], [source["id"]])
    assert again["ok"] is True
    assert again["added"] == 0 and again["updated"] == 0 and again["removed"] == 0
    assert _snapshot(custom["id"]) == {KEY: DOC}


def test_duplicate_source_is_rejected_and_nothing_is_written():
    source = _cat("Source")
    custom = _custom("Composed")
    result = _compose(custom["id"], [source["id"], source["id"]])
    assert result["ok"] is False
    assert result["error"] == cat_service.E_SOURCE_DUPLICATE
    assert _snapshot(custom["id"]) == {}
    assert cat_service.category_sources(_stored(custom["id"])) == []


def test_self_composition_is_rejected():
    custom = _custom("Composed")
    result = _compose(custom["id"], [custom["id"]])
    assert result["ok"] is False
    assert result["error"] == cat_service.E_SOURCE_SELF
    assert cat_service.category_sources(_stored(custom["id"])) == []


def test_missing_source_is_rejected_honestly():
    _lib(DOC, "😀")
    custom = _custom("Composed")
    result = _compose(custom["id"], [424243])
    assert result["ok"] is False
    assert result["error"] == cat_service.E_SOURCE_MISSING
    assert result["missing_sources"] == [424243]
    assert _snapshot(custom["id"]) == {}


def test_foreign_owner_source_is_rejected():
    foreign = _cat("Foreign", owner=OTHER)
    _lib(DOC, "😀", owner=OTHER)
    _map(foreign["id"], KEY, DOC, owner=OTHER)
    custom = _custom("Composed")

    result = _compose(custom["id"], [foreign["id"]])
    assert result["ok"] is False
    assert result["error"] == cat_service.E_SOURCE_MISSING
    assert _snapshot(custom["id"]) == {}
    assert _snapshot(foreign["id"], owner=OTHER) == {KEY: DOC}


def test_ordinary_category_cannot_be_composed():
    ordinary = _cat("Ordinary")
    source = _cat("Source")
    result = _compose(ordinary["id"], [source["id"]])
    assert result["ok"] is False
    assert result["error"] == cat_service.E_NOT_CUSTOM
    assert cat_service.category_sources(_stored(ordinary["id"])) == []


@pytest.mark.parametrize("raw", [None, [], "x", [0], [True], ["1"], [1.5]])
def test_invalid_source_lists_fail_closed(raw):
    custom = _custom("Composed")
    result = _compose(custom["id"], raw)
    assert result["ok"] is False
    assert result["error"] in (cat_service.E_NO_SOURCES, cat_service.E_SOURCE_MISSING)
    assert _snapshot(custom["id"]) == {}


def test_source_list_is_bounded():
    custom = _custom("Composed")
    result = _compose(custom["id"], list(range(1, cat_service.MAX_SOURCE_CATEGORIES + 2)))
    assert result["ok"] is False
    assert result["error"] == cat_service.E_TOO_MANY_SOURCES


def test_empty_source_category_composes_an_empty_snapshot_honestly():
    empty = _cat("Empty")
    custom = _custom("Composed")
    result = _compose(custom["id"], [empty["id"]])
    assert result["ok"] is True
    assert result["mappings"] == []
    assert result["empty_sources"] == [empty["id"]]
    assert _snapshot(custom["id"]) == {}
    assert cat_service.category_sources(_stored(custom["id"])) == [empty["id"]]


def test_unresolvable_source_mapping_is_counted_and_copied_honestly():
    source = _cat("Source")
    _lib(DOC, "😀")
    _map(source["id"], KEY, DOC)
    db_client._fallback["emoji_library"] = [
        entry for entry in db_client._fallback["emoji_library"]
        if entry["document_id"] != DOC
    ]
    custom = _custom("Composed")

    result = _compose(custom["id"], [source["id"]])
    assert result["ok"] is True
    assert result["unresolvable"] == 1
    assert result["scanned"] == 1
    assert _snapshot(custom["id"]) == {KEY: DOC}


def test_nested_custom_source_is_flattened_to_concrete_mappings():
    base = _cat("Base")
    _lib(1, "fire")
    _map(base["id"], "🔥", 1)
    middle = _custom("Middle")
    assert _compose(middle["id"], [base["id"]])["ok"] is True

    outer = _custom("Outer")
    result = _compose(outer["id"], [middle["id"]])
    assert result["ok"] is True
    assert result["sources"] == [middle["id"]]
    assert _snapshot(outer["id"]) == {"🔥": 1}
    rows, _total = _run(cat_service.list_mappings(OWNER, outer["id"]))
    assert rows[0]["document_id"] == 1  # a concrete entry, never a live reference


def test_two_level_cycle_is_rejected():
    base = _cat("Base")
    _lib(1, "fire")
    _map(base["id"], "🔥", 1)
    middle = _custom("Middle")
    _compose(middle["id"], [base["id"]])
    outer = _custom("Outer")
    _compose(outer["id"], [middle["id"]])

    result = _compose(middle["id"], [outer["id"]])
    assert result["ok"] is False
    assert result["error"] == cat_service.E_SOURCE_CYCLE
    # middle keeps its valid snapshot and its persisted sources
    assert _snapshot(middle["id"]) == {"🔥": 1}
    assert cat_service.category_sources(_stored(middle["id"])) == [base["id"]]


def test_three_level_cycle_is_rejected():
    leaf = _cat("Leaf")
    one = _custom("One")
    two = _custom("Two")
    _compose(one["id"], [leaf["id"]])
    _compose(two["id"], [one["id"]])

    result = _compose(leaf["id"], [two["id"]])  # leaf is ordinary → refused
    assert result["ok"] is False
    assert result["error"] == cat_service.E_NOT_CUSTOM

    third = _custom("Third")  # third → one → two → third
    _compose(third["id"], [two["id"]])
    result = _compose(one["id"], [third["id"]])
    assert result["ok"] is False
    assert result["error"] == cat_service.E_SOURCE_CYCLE


# ── C. conflicts ──────────────────────────────────────────────────────────────


def test_shared_simple_emoji_is_refused_until_confirmed():
    first, second = _cat("First"), _cat("Second")
    _lib(1, "wastebasket")
    _lib(2, "wastebasket")
    _map(first["id"], KEY, 1)
    _map(second["id"], KEY, 2)
    custom = _custom("Composed")

    result = _compose(custom["id"], [first["id"], second["id"]])
    assert result["ok"] is False
    assert result["error"] == cat_service.E_CONFLICT
    assert len(result["conflicts"]) == 1
    record = result["conflicts"][0]
    assert record["simple_emoji"] == KEY
    assert record["kept"]["source_id"] == first["id"]
    assert record["kept"]["document_id"] == 1
    assert record["dropped"] == [{"source_id": second["id"], "document_id": 2}]
    # nothing was written: no snapshot, no persisted source list
    assert _snapshot(custom["id"]) == {}
    assert cat_service.category_sources(_stored(custom["id"])) == []


def test_confirmed_conflict_stores_the_first_source_and_reports_it():
    first, second = _cat("First"), _cat("Second")
    _lib(1, "wastebasket")
    _lib(2, "wastebasket")
    _map(first["id"], KEY, 1)
    _map(second["id"], KEY, 2)
    custom = _custom("Composed")

    result = _compose(custom["id"], [first["id"], second["id"]], confirm=True)
    assert result["ok"] is True
    assert len(result["conflicts"]) == 1
    assert result["conflicts"][0]["dropped"][0]["source_id"] == second["id"]
    assert _snapshot(custom["id"]) == {KEY: 1}


def test_snapshot_holds_at_most_one_mapping_per_simple_emoji():
    first, second, third = _cat("First"), _cat("Second"), _cat("Third")
    for doc in (1, 2, 3):
        _lib(doc)
        _map(first["id"] if doc == 1 else second["id"] if doc == 2 else third["id"], KEY, doc)
    _lib(4, "fire")
    _map(third["id"], "🔥", 4)
    custom = _custom("Composed")

    result = _compose(custom["id"], [first["id"], second["id"], third["id"]], confirm=True)
    assert result["ok"] is True
    rows, total = _run(cat_service.list_mappings(OWNER, custom["id"], limit=100, offset=0))
    simples = [row["simple_emoji"] for row in rows]
    assert total == 2 and len(simples) == len(set(simples))
    assert _snapshot(custom["id"]) == {KEY: 1, "🔥": 4}


def test_conflict_panel_writes_nothing_until_confirmed():
    first, second = _cat("First"), _cat("Second")
    _lib(1, "wastebasket")
    _lib(2, "wastebasket")
    _map(first["id"], KEY, 1)
    _map(second["id"], KEY, 2)
    custom = _custom("Composed")
    inline_engine.set_owner_id(OWNER)
    emoji._set_draft(OWNER, kind="compose", category_id=custom["id"],
                     source_ids=[first["id"], second["id"]])

    title, body, buttons = _run(emoji._compose_apply_action(None, str(custom["id"]), CHAT))
    assert title == "Composition Conflicts"
    assert "Nothing has been written yet" in body
    assert f"action:emoji_compose_confirm:compose:{custom['id']}" in _datas(buttons)
    assert _snapshot(custom["id"]) == {}

    title, body, buttons = _run(
        emoji._compose_confirm_action(None, f"compose:{custom['id']}", CHAT)
    )
    assert title == "Compose"
    assert "conflict(s) resolved by source order" in body
    assert _snapshot(custom["id"]) == {KEY: 1}


# ── D. snapshot semantics (§34-F default) ─────────────────────────────────────


def _sourced(emoji_key: str = KEY, document_id: int = DOC):
    """One ordinary source with one mapping plus a composed Custom Category."""
    source = _cat("Source")
    _lib(document_id, "😀")
    _map(source["id"], emoji_key, document_id)
    custom = _custom("Composed")
    result = _compose(custom["id"], [source["id"]])
    assert result["ok"] is True, result
    return source, custom


def test_snapshot_rows_are_not_live_references():
    source, custom = _sourced()
    for row in db_client._fallback["emoji_mappings"]:
        if row.get("category_id") == source["id"]:
            row["document_id"] = 999999
    assert _snapshot(custom["id"]) == {KEY: DOC}


def test_source_edit_does_not_move_the_snapshot_until_refresh():
    source, custom = _sourced()
    _lib(43003, "🗿")
    replaced = _run(cat_service.replace_mapping(OWNER, source["id"], KEY, 43003))
    assert replaced["ok"] is True

    assert _snapshot(custom["id"]) == {KEY: DOC}  # snapshot untouched
    result = _refresh(custom["id"])
    assert result["ok"] is True
    assert result["updated"] == 1 and result["added"] == 0 and result["removed"] == 0
    assert _snapshot(custom["id"]) == {KEY: 43003}


def test_source_addition_does_not_move_the_snapshot_until_refresh():
    source, custom = _sourced()
    _lib(43004, "fire")
    _map(source["id"], "🔥", 43004)

    assert _snapshot(custom["id"]) == {KEY: DOC}
    result = _refresh(custom["id"])
    assert result["ok"] is True
    assert result["added"] == 1
    assert _snapshot(custom["id"]) == {KEY: DOC, "🔥": 43004}


def test_source_mapping_removal_does_not_move_the_snapshot_until_refresh():
    source, custom = _sourced()
    removed = _run(cat_service.delete_mapping(OWNER, source["id"], KEY))
    assert removed["ok"] is True

    assert _snapshot(custom["id"]) == {KEY: DOC}
    result = _refresh(custom["id"])
    assert result["ok"] is True
    assert result["removed"] == 1
    assert _snapshot(custom["id"]) == {}


def test_source_deletion_does_not_delete_the_snapshot():
    source, custom = _sourced()
    _run(cat_service.delete_category(OWNER, source["id"]))

    assert _snapshot(custom["id"]) == {KEY: DOC}
    stored = _stored(custom["id"])
    assert stored is not None and cat_service.is_custom_category(stored) is True
    rows, total, _counts = _run(cat_service.list_categories(OWNER))
    assert total == 1 and rows[0]["id"] == custom["id"]

    result = _refresh(custom["id"])
    assert result["ok"] is False
    assert result["error"] == cat_service.E_SOURCE_MISSING
    assert result["missing_sources"] == [source["id"]]
    assert _snapshot(custom["id"]) == {KEY: DOC}  # previous snapshot preserved
    assert cat_service.category_sources(_stored(custom["id"])) == [source["id"]]


def test_refresh_drops_a_missing_source_and_reports_it():
    first = _cat("First")
    second = _cat("Second")
    _lib(1, "fire")
    _lib(2, "wastebasket")
    _map(first["id"], "🔥", 1)
    _map(second["id"], KEY, 2)
    custom = _custom("Composed")
    _compose(custom["id"], [first["id"], second["id"]])
    _run(cat_service.delete_category(OWNER, second["id"]))

    result = _refresh(custom["id"])
    assert result["ok"] is True
    assert result["missing_sources"] == [second["id"]]
    assert result["removed_sources"] == [second["id"]]
    assert _snapshot(custom["id"]) == {"🔥": 1}
    assert cat_service.category_sources(_stored(custom["id"])) == [first["id"]]


def test_refresh_refuses_a_new_conflict_until_confirmed():
    first = _cat("First")
    second = _cat("Second")
    _lib(1, "wastebasket")
    _lib(2, "wastebasket")
    _map(first["id"], KEY, 1)
    custom = _custom("Composed")
    _compose(custom["id"], [first["id"], second["id"]])
    _map(second["id"], KEY, 2)  # a conflict appears only later

    result = _refresh(custom["id"])
    assert result["ok"] is False
    assert result["error"] == cat_service.E_CONFLICT
    assert _snapshot(custom["id"]) == {KEY: 1}

    confirmed = _refresh(custom["id"], confirm=True)
    assert confirmed["ok"] is True
    assert _snapshot(custom["id"]) == {KEY: 1}  # first source still wins


def test_failed_refresh_keeps_the_previous_valid_snapshot(monkeypatch):
    source, custom = _sourced()
    _lib(43005, "fire")
    _map(source["id"], "🔥", 43005)

    async def _fail(_data):
        return None

    monkeypatch.setattr(db_client, "insert_emoji_mapping", _fail)
    result = _refresh(custom["id"])
    assert result["ok"] is False
    assert result["error"] == cat_service.E_STORAGE
    assert result["rolled_back"] is True
    assert _snapshot(custom["id"]) == {KEY: DOC}


def test_incomplete_source_mapping_read_fails_the_whole_plan_closed(monkeypatch):
    source, custom = _sourced()
    _lib(43006, "fire")
    _map(source["id"], "🔥", 43006)
    before = _snapshot(custom["id"])
    real = db_client.list_emoji_mappings

    async def _truncated(owner_id, category_id, limit=1000, offset=0):
        rows, total = await real(owner_id, category_id, limit=limit, offset=offset)
        return rows[: max(0, len(rows) - 1)], total

    monkeypatch.setattr(db_client, "list_emoji_mappings", _truncated)
    result = _refresh(custom["id"])
    assert result["ok"] is False
    assert result["error"] == cat_service.E_MAPPING_LIST_INCOMPLETE
    monkeypatch.undo()
    assert before == {KEY: DOC}
    assert _snapshot(custom["id"]) == before


def test_refresh_without_persisted_sources_is_refused():
    custom = _custom("Composed")
    result = _refresh(custom["id"])
    assert result["ok"] is False
    assert result["error"] == cat_service.E_NO_SOURCES


def test_refresh_on_an_ordinary_category_is_refused():
    ordinary = _cat("Ordinary")
    result = _refresh(ordinary["id"])
    assert result["ok"] is False
    assert result["error"] == cat_service.E_NOT_CUSTOM


def test_refresh_of_a_deleted_category_fails_closed():
    _source, custom = _sourced()
    _run(cat_service.delete_category(OWNER, custom["id"]))
    result = _refresh(custom["id"])
    assert result["ok"] is False
    assert result["error"] == cat_service.E_NOT_FOUND


def test_refresh_leaves_the_active_category_state_alone():
    _source, custom = _sourced()
    _run(state_service.set_replacement_enabled(OWNER, True))
    _run(state_service.set_global_default_category(OWNER, custom["id"]))
    _refresh(custom["id"])
    assert _run(state_service.replacement_enabled(OWNER)) is True
    assert _run(state_service.get_global_default_category(OWNER)) == custom["id"]
    assert _run(state_service.resolve_effective_category(OWNER, CHAT)) == custom["id"]


# ── E. edit posture on a composed category ───────────────────────────────────


def test_manual_mapping_edit_is_refused_on_a_composed_category():
    _source, custom = _sourced()
    inline_engine.set_owner_id(OWNER)
    title, body, buttons = _run(
        emoji._map_edit_action(None, f"{KEY}:{custom['id']}", CHAT)
    )
    assert title == "Change Mapping"
    assert "disabled" in body
    assert f"panel:emoji_compose:{custom['id']}" in _datas(buttons)
    assert _snapshot(custom["id"]) == {KEY: DOC}


def test_manual_mapping_delete_is_refused_on_a_composed_category():
    _source, custom = _sourced()
    inline_engine.set_owner_id(OWNER)
    title, body, _buttons = _run(
        emoji._map_del_action(None, f"{KEY}:{custom['id']}", CHAT)
    )
    assert title == "Remove Mapping"
    assert "disabled" in body
    assert _snapshot(custom["id"]) == {KEY: DOC}


def test_mappings_panel_offers_compose_instead_of_add_for_a_composed_category():
    _source, custom = _sourced()
    inline_engine.set_owner_id(OWNER)
    title, body, buttons = _run(emoji._mappings_page_handler(None, f":{custom['id']}:0"))
    datas = _datas(buttons)
    assert "snapshot of its sources" in body
    assert f"panel:emoji_compose:{custom['id']}" in datas
    assert f"action:emoji_refresh:{custom['id']}" in datas
    assert f"action:emoji_map_new:{custom['id']}" not in datas

    ordinary = _cat("Ordinary")
    title, body, buttons = _run(emoji._mappings_page_handler(None, f":{ordinary['id']}:0"))
    datas = _datas(buttons)
    assert f"action:emoji_map_new:{ordinary['id']}" in datas
    assert not [d for d in datas if "compose" in d or "refresh" in d]


def test_mapping_detail_on_a_composed_category_offers_compose_not_edit():
    _source, custom = _sourced()
    inline_engine.set_owner_id(OWNER)
    title, body, buttons = _run(emoji._mapping_page_handler(None, f"0:0:{custom['id']}"))
    datas = _datas(buttons)
    assert "composed category" in body.lower()
    assert f"panel:emoji_compose:{custom['id']}" in datas
    assert f"action:emoji_map_edit:{KEY}:{custom['id']}" not in datas
    assert f"action:emoji_map_del:{KEY}:{custom['id']}" not in datas

    ordinary = _cat("Ordinary")
    _lib(7, "seven")
    _map(ordinary["id"], KEY, 7)
    title, body, buttons = _run(emoji._mapping_page_handler(None, f"0:0:{ordinary['id']}"))
    datas = _datas(buttons)
    assert f"action:emoji_map_edit:{KEY}:{ordinary['id']}" in datas
    assert f"action:emoji_map_del:{KEY}:{ordinary['id']}" in datas


# ── F. UI: creation, compose panel, picker, isolation ────────────────────────


async def _noop(*_args, **_kwargs):
    return None


def test_registration_covers_the_phase5_surface():
    emoji.register(client=None, owner_id=OWNER)
    from backend.helper.panel_registry import registry as get_registry
    from backend.helper.panels import get_action

    for panel_id in ("emoji_compose", "emoji_sources"):
        assert get_registry().get_handler(panel_id) is not None, panel_id
    for action in (
        "emoji_cat_new_custom", "emoji_compose_add", "emoji_compose_rm",
        "emoji_compose_apply", "emoji_compose_confirm", "emoji_refresh",
    ):
        assert get_action(action) is not None, action


def test_categories_panel_offers_custom_creation_and_marks_custom_rows():
    _cat("Ordinary")
    custom = _custom("Composed")
    inline_engine.set_owner_id(OWNER)
    title, body, buttons = _run(emoji._categories_page_handler(None, "0"))
    datas, texts = _datas(buttons), _texts(buttons)
    assert "action:emoji_cat_new_custom" in datas
    assert any(text.startswith("🧩 Composed") for text in texts)
    assert not any(text.startswith("🧩 Ordinary") for text in texts)


def test_new_custom_category_input_flow_arms_the_composer(monkeypatch):
    edits: list[tuple] = []

    async def _record(*args):
        edits.append(args)

    monkeypatch.setattr(emoji, "_edit_inline", _record)
    monkeypatch.setattr(emoji, "_delete_owner_message", _noop)
    inline_engine.set_owner_id(OWNER)

    _run(emoji._custom_category_create_input_handler("Kit", CHAT, 5, 0, 0))
    rows, total, _counts = _run(cat_service.list_categories(OWNER))
    assert total == 1 and rows[0]["name"] == "Kit"
    assert cat_service.is_custom_category(rows[0]) is True
    assert edits and edits[0][2] == "Compose"
    assert emoji._compose_draft_sources(OWNER, rows[0]["id"]) == []


def test_new_custom_category_rejects_a_duplicate_name_honestly(monkeypatch):
    edits: list[tuple] = []

    async def _record(*args):
        edits.append(args)

    monkeypatch.setattr(emoji, "_edit_inline", _record)
    monkeypatch.setattr(emoji, "_delete_owner_message", _noop)
    inline_engine.set_owner_id(OWNER)
    _cat("Kit")

    _run(emoji._custom_category_create_input_handler("Kit", CHAT, 5, 0, 0))
    rows, total, _counts = _run(cat_service.list_categories(OWNER))
    assert total == 1
    assert edits and "already exists" in edits[0][3]


def test_compose_panel_lists_sources_in_precedence_order():
    first, second = _cat("First"), _cat("Second")
    _lib(1, "fire")
    _lib(2, "wastebasket")
    _map(first["id"], "🔥", 1)
    _map(second["id"], KEY, 2)
    custom = _custom("Composed")
    _compose(custom["id"], [first["id"], second["id"]])
    inline_engine.set_owner_id(OWNER)

    title, body, buttons = _run(emoji._compose_panel_handler(None, str(custom["id"])))
    datas = _datas(buttons)
    assert title == "Compose"
    assert "1. First" in body and "2. Second" in body
    assert "Snapshot: 2 mapping(s)" in body
    assert f"action:emoji_compose_rm:{custom['id']}:{first['id']}" in datas
    assert f"action:emoji_compose_rm:{custom['id']}:{second['id']}" in datas
    assert f"action:emoji_refresh:{custom['id']}" in datas
    assert f"action:emoji_compose_apply:{custom['id']}" in datas
    assert emoji._compose_draft_sources(OWNER, custom["id"]) is None


def test_compose_panel_fails_honestly_for_missing_and_ordinary_categories():
    inline_engine.set_owner_id(OWNER)
    title, body, buttons = _run(emoji._compose_panel_handler(None, "424243"))
    assert title == "Compose" and "not found" in body.lower()
    assert "panel:emoji_categories" in _datas(buttons)

    ordinary = _cat("Ordinary")
    title, body, _buttons = _run(emoji._compose_panel_handler(None, str(ordinary["id"])))
    assert "not found" in body.lower()


def test_composition_result_panel_reports_the_not_custom_error():
    ordinary = _cat("Ordinary")
    source = _cat("Source")
    plan = _run(cat_service.plan_composition(OWNER, ordinary["id"], [source["id"]]))
    assert plan["error"] == cat_service.E_NOT_CUSTOM
    title, body, buttons = _run(
        emoji._render_composition_result(OWNER, ordinary["id"], plan, op="compose")
    )
    assert title == "Compose"
    assert "not a composed (custom) category" in body
    assert "panel:emoji_categories" in _datas(buttons)


def test_source_picker_is_2x5_and_clamps_pages():
    custom = _custom("Composed")
    for i in range(12):
        _cat(f"c{i:02d}")
    inline_engine.set_owner_id(OWNER)

    title, body, buttons = _run(emoji._source_picker_handler(None, f"{custom['id']}:0"))
    picks = [d for d in _datas(buttons) if d.startswith("action:emoji_compose_add:")]
    assert title == "Add Source"
    assert len(picks) == 10
    assert "page 1/2" in body
    assert f"panel:emoji_sources:{custom['id']}:1" in _datas(buttons)

    _title, _body, buttons2 = _run(emoji._source_picker_handler(None, f"{custom['id']}:99"))
    picks2 = [d for d in _datas(buttons2) if d.startswith("action:emoji_compose_add:")]
    assert len(picks2) == 2  # clamped onto the last page, self excluded


def test_source_picker_marks_selected_sources_and_never_offers_itself():
    source = _cat("Source")
    custom = _custom("Composed")
    inline_engine.set_owner_id(OWNER)
    emoji._set_draft(OWNER, kind="compose", category_id=custom["id"],
                     source_ids=[source["id"]])

    _title, body, buttons = _run(
        emoji._source_picker_handler(None, f"{custom['id']}:0")
    )
    texts = _texts(buttons)
    assert any(text.startswith("✓ Source") for text in texts)
    assert "Composed" not in texts
    assert not any("🧩" in text for text in texts)


def test_source_picker_needs_another_category_to_be_useful():
    custom = _custom("Composed")
    inline_engine.set_owner_id(OWNER)
    title, body, buttons = _run(emoji._source_picker_handler(None, f"{custom['id']}:0"))
    assert title == "Add Source"
    assert "No other categories" in body
    assert f"panel:emoji_compose:{custom['id']}" in _datas(buttons)


def test_add_source_appends_in_tap_order():
    first, second = _cat("First"), _cat("Second")
    custom = _custom("Composed")
    inline_engine.set_owner_id(OWNER)
    candidates, _page, _total = _run(emoji._source_candidates(OWNER, custom["id"], 0))
    index = {row["id"]: j for j, row in enumerate(candidates)}

    _run(emoji._compose_add_action(None, f"{custom['id']}:0:{index[first['id']]}", CHAT))
    _title, body, _buttons = _run(
        emoji._compose_add_action(None, f"{custom['id']}:0:{index[second['id']]}", CHAT)
    )
    assert emoji._compose_draft_sources(OWNER, custom["id"]) == [first["id"], second["id"]]
    assert "1. First" in body and "2. Second" in body
    assert _snapshot(custom["id"]) == {}  # a draft is not a composition yet


def test_add_source_refuses_a_duplicate_tap():
    source = _cat("Source")
    custom = _custom("Composed")
    inline_engine.set_owner_id(OWNER)
    _run(emoji._compose_add_action(None, str(custom["id"]), CHAT))  # open the picker
    candidates, _page, _total = _run(emoji._source_candidates(OWNER, custom["id"], 0))
    index = {row["id"]: j for j, row in enumerate(candidates)}
    target = f"{custom['id']}:0:{index[source['id']]}"

    _run(emoji._compose_add_action(None, target, CHAT))
    _title, body, _buttons = _run(emoji._compose_add_action(None, target, CHAT))
    assert "already a source" in body
    assert emoji._compose_draft_sources(OWNER, custom["id"]) == [source["id"]]


def test_add_source_stale_index_fails_honestly():
    _cat("Source")
    custom = _custom("Composed")
    inline_engine.set_owner_id(OWNER)
    title, body, _buttons = _run(
        emoji._compose_add_action(None, f"{custom['id']}:0:99", CHAT)
    )
    assert title == "Add Source"
    assert "List changed" in body
    assert emoji._compose_draft_sources(OWNER, custom["id"]) is None


def test_remove_source_updates_the_draft():
    first, second = _cat("First"), _cat("Second")
    _lib(1, "fire")
    _map(first["id"], "🔥", 1)
    custom = _custom("Composed")
    _compose(custom["id"], [first["id"]], confirm=True)
    with_second = _compose(custom["id"], [first["id"], second["id"]], confirm=True)
    assert with_second["ok"] is True
    inline_engine.set_owner_id(OWNER)

    _title, body, _buttons = _run(
        emoji._compose_rm_action(None, f"{custom['id']}:{second['id']}", CHAT)
    )
    assert emoji._compose_draft_sources(OWNER, custom["id"]) == [first["id"]]
    assert "1. First" in body and "Second" not in body

    _title, body, _buttons = _run(
        emoji._compose_rm_action(None, f"{custom['id']}:{second['id']}", CHAT)
    )
    assert "not in this composition" in body


def test_owner_isolation_for_compose_surface():
    foreign_source = _cat("Foreign", owner=OTHER)
    foreign = _custom("ForeignComposed", owner=OTHER)
    _lib(DOC, "😀", owner=OTHER)
    _map(foreign_source["id"], KEY, DOC, owner=OTHER)
    assert _compose(foreign["id"], [foreign_source["id"]], owner=OTHER)["ok"] is True
    inline_engine.set_owner_id(OWNER)

    _title, body, _buttons = _run(emoji._compose_panel_handler(None, str(foreign["id"])))
    assert "not found" in body.lower()
    _title, body, _buttons = _run(
        emoji._source_picker_handler(None, f"{foreign['id']}:0")
    )
    assert "not found" in body.lower()
    _title, body, _buttons = _run(
        emoji._compose_add_action(None, str(foreign["id"]), CHAT)
    )
    assert "not found" in body.lower()
    _title, body, _buttons = _run(
        emoji._refresh_action(None, str(foreign["id"]), CHAT)
    )
    assert "not found" in body.lower()
    _title, body, _buttons = _run(
        emoji._compose_confirm_action(None, f"compose:{foreign['id']}", CHAT)
    )
    assert "not found" in body.lower() or "out of date" in body.lower()

    stored = _run(cat_service.get_category(OTHER, foreign["id"]))
    assert cat_service.category_sources(stored) == [foreign_source["id"]]
    assert _snapshot(foreign["id"], owner=OTHER) == {KEY: DOC}
    assert _run(state_service.get_global_default_category(OWNER)) is None


def test_stale_callbacks_after_deletion_fail_safely():
    _source, custom = _sourced()
    _run(cat_service.delete_category(OWNER, custom["id"]))
    inline_engine.set_owner_id(OWNER)

    for outcome in (
        _run(emoji._compose_panel_handler(None, str(custom["id"]))),
        _run(emoji._source_picker_handler(None, f"{custom['id']}:0")),
        _run(emoji._compose_add_action(None, str(custom["id"]), CHAT)),
        _run(emoji._refresh_action(None, str(custom["id"]), CHAT)),
        _run(emoji._compose_apply_action(None, str(custom["id"]), CHAT)),
    ):
        title, body, _buttons = outcome
        assert "not found" in body.lower() or "out of date" in body.lower(), body


def test_compose_callback_ids_are_real_owner_scoped_ids():
    first, second = _cat("First"), _cat("Second")
    _lib(1, "fire")
    _map(first["id"], "🔥", 1)
    custom = _custom("Composed")
    _compose(custom["id"], [first["id"]], confirm=True)
    inline_engine.set_owner_id(OWNER)

    panels = [
        _run(emoji._categories_page_handler(None, "0")),
        _run(emoji._category_page_handler(None, "0:0")),
        _run(emoji._compose_panel_handler(None, str(custom["id"]))),
        _run(emoji._source_picker_handler(None, f"{custom['id']}:0")),
    ]
    checked = 0
    for _title, _body, buttons in panels:
        for data in _datas(buttons):
            payload = data.split(":")
            if payload[0] == "action" and payload[1] in (
                "emoji_compose_add", "emoji_compose_rm", "emoji_compose_apply",
                "emoji_refresh", "emoji_compose_confirm",
            ):
                cid = int(payload[2])
                assert _run(cat_service.get_category(OWNER, cid)) is not None, data
                checked += 1
                if payload[1] == "emoji_compose_rm":
                    sid = int(payload[3])
                    assert _run(cat_service.get_category(OWNER, sid)) is not None, data
            elif payload[0] == "panel" and payload[1].startswith("emoji_compose"):
                assert int(payload[2]) == custom["id"], data
                checked += 1
    assert checked > 0


def test_compose_callback_data_stays_within_the_telegram_bound():
    first, second = _cat("First"), _cat("Second")
    _lib(1, "wastebasket")
    _lib(2, "wastebasket")
    _map(first["id"], KEY, 1)
    _map(second["id"], KEY, 2)
    custom = _custom("A rather long composed category name")
    inline_engine.set_owner_id(OWNER)
    emoji._set_draft(OWNER, kind="compose", category_id=custom["id"],
                     source_ids=[first["id"], second["id"]])

    panels = [
        _run(emoji._categories_page_handler(None, "0")),
        _run(emoji._category_page_handler(None, "0:0")),
        _run(emoji._compose_panel_handler(None, str(custom["id"]))),
        _run(emoji._source_picker_handler(None, f"{custom['id']}:0")),
        _run(emoji._mappings_page_handler(None, f":{custom['id']}:0")),
        _run(emoji._compose_apply_action(None, str(custom["id"]), CHAT)),
    ]
    for _title, _body, buttons in panels:
        for data in _datas(buttons):
            assert len(data.encode("utf-8")) <= 64, data


def test_ordinary_categories_keep_their_phase2_surface():
    ordinary = _cat("Ordinary")
    inline_engine.set_owner_id(OWNER)
    _title, body, buttons = _run(emoji._category_page_handler(None, "0:0"))
    datas, texts = _datas(buttons), _texts(buttons)
    assert "mapping(s)" in body
    assert "Composed snapshot" not in body
    assert "🧩" not in texts[0]
    assert f"panel:emoji_mappings:{ordinary['id']}:0" in datas
    assert f"action:emoji_cat_rename:{ordinary['id']}" in datas
    assert f"action:emoji_cat_del:{ordinary['id']}" in datas
    assert f"action:emoji_refresh:{ordinary['id']}" not in datas
    assert f"panel:emoji_compose:{ordinary['id']}" not in datas


# ── G. integration: state, replacement pipeline, architecture ───────────────


def test_custom_category_works_as_the_global_default(bot, self_client):
    source = _cat("Source")
    _lib(DOC, "😀")
    _map(source["id"], KEY, DOC)
    custom = _custom("Composed")
    _compose(custom["id"], [source["id"]])
    _run(state_service.set_replacement_enabled(OWNER, True))
    assert _run(state_service.set_global_default_category(OWNER, custom["id"])) is True

    outcome = _process(self_client, _message(text=f"hi {KEY}"))
    assert outcome["status"] == repl.STATUS_REPLACED
    assert outcome["replaced"] == 1
    assert bot.calls[0]["text"] == "hi 😀"
    assert self_client.deleted == [(CHAT, (11,))]


def test_custom_category_works_as_a_per_chat_override(bot, self_client):
    _lib(DOC, "😀")
    _lib(43003, "🗿")
    global_cat = _cat("Global")
    _map(global_cat["id"], KEY, DOC)
    source = _cat("Source")
    _map(source["id"], KEY, 43003)
    custom = _custom("Composed")
    _compose(custom["id"], [source["id"]])

    _run(state_service.set_replacement_enabled(OWNER, True))
    _run(state_service.set_global_default_category(OWNER, global_cat["id"]))
    assert _run(state_service.set_chat_override(OWNER, CHAT, custom["id"])) is True

    _process(self_client, _message(text=KEY))
    assert bot.calls[0]["formatting_entities"][0].document_id == 43003

    _process(self_client, _message(msg_id=12, text=KEY, chat_id=CHAT2))
    assert bot.calls[1]["formatting_entities"][0].document_id == DOC


def test_custom_category_respects_the_replacement_toggle(bot, self_client):
    _source, custom = _sourced()
    _run(state_service.set_global_default_category(OWNER, custom["id"]))
    outcome = _process(self_client, _message(text=f"hi {KEY}"))
    assert outcome["status"] == repl.STATUS_NO_CATEGORY
    assert bot.calls == [] and self_client.deleted == []


def test_deleted_custom_category_fails_closed_in_the_pipeline(bot, self_client):
    _source, custom = _sourced()
    _run(state_service.set_replacement_enabled(OWNER, True))
    _run(state_service.set_global_default_category(OWNER, custom["id"]))
    _run(cat_service.delete_category(OWNER, custom["id"]))
    outcome = _process(self_client, _message(text=f"hi {KEY}"))
    assert outcome["status"] == repl.STATUS_NO_CATEGORY
    assert bot.calls == []


def test_snapshot_mapping_drives_replacement_without_any_special_path(bot, self_client):
    source = _cat("Source")
    _lib(DOC, "😀")
    _map(source["id"], KEY, DOC)
    custom = _custom("Composed")
    _compose(custom["id"], [source["id"]])
    _run(state_service.set_replacement_enabled(OWNER, True))
    _run(state_service.set_global_default_category(OWNER, custom["id"]))

    # the source changes afterwards: the snapshot (and the pipeline) keep the
    # composed state until an explicit Refresh rebuilds it
    _lib(43007, "🗿")
    _run(cat_service.replace_mapping(OWNER, source["id"], KEY, 43007))
    _process(self_client, _message(msg_id=21, text=KEY))
    assert bot.calls[0]["formatting_entities"][0].document_id == DOC

    _refresh(custom["id"])
    _process(self_client, _message(msg_id=22, text=KEY))
    assert bot.calls[1]["formatting_entities"][0].document_id == 43007


def test_phase4_modules_need_no_custom_category_logic():
    from backend.services import emoji_transformer

    for path in (Path(emoji_transformer.__file__), Path(repl.__file__)):
        source = path.read_text()
        for forbidden in ("is_custom", "category_sources", "plan_composition"):
            assert forbidden not in source, (path.name, forbidden)


def test_phase5_modules_import_no_ai_or_runtime_modules():
    for module in (cat_service, emoji):
        for node in ast.walk(ast.parse(Path(module.__file__).read_text())):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            for name in names:
                assert not name.startswith(("backend.ai", "backend.runtime")), name


def test_phase5_modules_add_no_loop_executor_or_task():
    banned = {"create_task", "new_event_loop", "run_until_complete", "call_later"}
    for module in (cat_service, emoji):
        for node in ast.walk(ast.parse(Path(module.__file__).read_text())):
            if isinstance(node, ast.Attribute):
                assert node.attr not in banned, (module.__name__, node.attr)


def test_composition_uses_no_regex_or_keyword_routing():
    for module in (cat_service, emoji):
        tree = ast.parse(Path(module.__file__).read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                assert "re" not in [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                assert node.module != "re"
            elif isinstance(node, ast.Attribute):
                assert not (isinstance(node.value, ast.Name) and node.value.id == "re")


def test_only_the_phase4_handler_owns_a_telegram_listener():
    for module in (cat_service, emoji):
        source = Path(module.__file__).read_text()
        assert "events.NewMessage" not in source, module.__name__
        assert "backend.bot.handlers.emoji_replacement" not in source, module.__name__
