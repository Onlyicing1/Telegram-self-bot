"""Categories & mappings service — Phase 2 (Emoji & Reaction, ROADMAP §10–§12).

Pins the service-layer contracts the UI depends on: deterministic category
validation, unique names per owner, mapping uniqueness inside one category,
reference-only mapping storage, deletion semantics (mappings die with the
category, library survives), and the never-silent-overwrite conflict result.
Everything runs against the in-memory fallback store.
"""
from __future__ import annotations

import asyncio

import pytest

from backend.db import client as db_client
from backend.services import emoji_category_service as svc

OWNER = 424242
OTHER = 991199


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    db_client._fallback["emoji_library"] = []
    db_client._fallback["emoji_categories"] = []
    db_client._fallback["emoji_mappings"] = []
    yield
    db_client._fallback["emoji_library"] = []
    db_client._fallback["emoji_categories"] = []
    db_client._fallback["emoji_mappings"] = []


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _lib(doc_id: int, alt: str = "x", owner: int = OWNER) -> dict:
    return {
        "owner_id": owner,
        "document_id": doc_id,
        "alt_text": alt,
        "source": "imported",
        "source_msg_id": 5,
        "created_at": "2026-10-06T10:00:00+00:00",
    }


def _cat(name: str, owner: int = OWNER) -> dict | None:
    return _run(svc.create_category(owner, name)).get("category")


def _mapping(cid: int, emoji: str, doc: int, owner: int = OWNER) -> dict:
    result = _run(svc.create_mapping(owner, cid, emoji, doc))
    assert result["ok"], result
    return result["mapping"]


# ── category validation ───────────────────────────────────────────────────────


def test_create_valid_category():
    result = _run(svc.create_category(OWNER, "002"))
    assert result["ok"] is True
    assert result["category"]["name"] == "002"
    assert result["category"]["owner_id"] == OWNER


def test_category_name_is_stripped():
    result = _run(svc.create_category(OWNER, "  Aya  "))
    assert result["ok"] is True
    assert result["category"]["name"] == "Aya"


@pytest.mark.parametrize("raw", ["", "   ", None, 7, "a\nb", "a\rb", "x" * 65])
def test_invalid_category_input_fails_closed(raw):
    result = _run(svc.create_category(OWNER, raw))
    assert result["ok"] is False
    assert result["error"] in (svc.E_INVALID_NAME, svc.E_NAME_TOO_LONG)
    assert result["category"] is None


def test_name_too_long_is_its_own_error():
    result = _run(svc.create_category(OWNER, "x" * 65))
    assert result["error"] == svc.E_NAME_TOO_LONG


def test_maximum_length_name_is_accepted():
    result = _run(svc.create_category(OWNER, "x" * 64))
    assert result["ok"] is True


# ── category list / rename / delete ───────────────────────────────────────────


def test_list_categories_reports_counts():
    cat_a, cat_b = _cat("A"), _cat("B")
    db_client._fallback["emoji_library"].extend([_lib(1), _lib(2)])
    _mapping(cat_a["id"], "🗑", 1)
    _mapping(cat_a["id"], "🗣", 2)
    rows, total, counts = _run(svc.list_categories(OWNER))
    assert total == 2
    assert [r["name"] for r in rows] == ["B", "A"]  # newest first
    assert counts == {cat_a["id"]: 2}


def test_rename_category():
    cat = _cat("Old")
    result = _run(svc.rename_category(OWNER, cat["id"], "New"))
    assert result["ok"] is True
    assert result["category"]["name"] == "New"


def test_rename_to_same_name_is_a_noop_success():
    cat = _cat("Same")
    result = _run(svc.rename_category(OWNER, cat["id"], "Same"))
    assert result["ok"] is True


def test_rename_onto_existing_name_is_refused():
    _cat("One")
    cat2 = _cat("Two")
    result = _run(svc.rename_category(OWNER, cat2["id"], "One"))
    assert result["ok"] is False
    assert result["error"] == svc.E_NAME_EXISTS


def test_rename_missing_category_fails():
    result = _run(svc.rename_category(OWNER, 123456, "New"))
    assert result["ok"] is False
    assert result["error"] == svc.E_NOT_FOUND


@pytest.mark.parametrize("raw", ["", None, "x" * 65])
def test_rename_rejects_invalid_names(raw):
    cat = _cat("Keep")
    result = _run(svc.rename_category(OWNER, cat["id"], raw))
    assert result["ok"] is False
    assert result["error"] in (svc.E_INVALID_NAME, svc.E_NAME_TOO_LONG)
    assert _run(svc.get_category(OWNER, cat["id"]))["name"] == "Keep"


def test_delete_category_removes_mappings():
    cat = _cat("Doomed")
    db_client._fallback["emoji_library"].extend([_lib(1), _lib(2)])
    _mapping(cat["id"], "🗑", 1)
    _mapping(cat["id"], "🗣", 2)
    result = _run(svc.delete_category(OWNER, cat["id"]))
    assert result["ok"] is True
    assert result["removed_mappings"] == 2
    assert result["category_deleted"] is True
    assert _run(svc.get_category(OWNER, cat["id"])) is None
    rows, _total = _run(svc.list_mappings(OWNER, cat["id"]))
    assert rows == []


def test_delete_category_preserves_library_entries():
    cat = _cat("Doomed")
    db_client._fallback["emoji_library"].append(_lib(1))
    _mapping(cat["id"], "🗑", 1)
    result = _run(svc.delete_category(OWNER, cat["id"]))
    assert result["ok"] is True
    entry = _run(db_client.get_emoji_entry(OWNER, 1))
    assert entry is not None and entry["document_id"] == 1


def test_delete_missing_category_fails():
    result = _run(svc.delete_category(OWNER, 424243))
    assert result["ok"] is False
    assert result["error"] == svc.E_NOT_FOUND


def test_create_duplicate_name_is_refused_not_overwritten():
    _cat("Alpha")
    result = _run(svc.create_category(OWNER, "Alpha"))
    assert result["ok"] is False
    assert result["error"] == svc.E_NAME_EXISTS
    rows, total, _counts = _run(svc.list_categories(OWNER))
    assert total == 1


# ── owner isolation ───────────────────────────────────────────────────────────


def test_same_name_allowed_for_different_owners():
    cat_a = _cat("Shared", owner=OWNER)
    cat_b = _cat("Shared", owner=OTHER)
    assert cat_a["id"] != cat_b["id"]


def test_owner_cannot_see_or_touch_other_owners_category():
    cat = _cat("Mine", owner=OTHER)
    assert _run(svc.get_category(OWNER, cat["id"])) is None
    result = _run(svc.rename_category(OWNER, cat["id"], "Hijacked"))
    assert result["ok"] is False
    result = _run(svc.delete_category(OWNER, cat["id"]))
    assert result["ok"] is False
    assert _run(svc.get_category(OTHER, cat["id"]))["name"] == "Mine"


# ── mapping creation ──────────────────────────────────────────────────────────


def test_create_valid_mapping():
    cat = _cat("C")
    db_client._fallback["emoji_library"].append(_lib(10, "wave"))
    result = _run(svc.create_mapping(OWNER, cat["id"], "👋", 10))
    assert result["ok"] is True
    row = result["mapping"]
    assert row["simple_emoji"] == "👋"
    assert row["document_id"] == 10
    assert row["category_id"] == cat["id"]


def test_mapping_stores_only_a_reference():
    cat = _cat("C")
    db_client._fallback["emoji_library"].append(_lib(10, "wave"))
    result = _run(svc.create_mapping(OWNER, cat["id"], "👋", 10))
    row = result["mapping"]
    assert "alt_text" not in row and "source" not in row


def test_missing_library_entry_is_rejected():
    cat = _cat("C")
    result = _run(svc.create_mapping(OWNER, cat["id"], "👋", 999999))
    assert result["ok"] is False
    assert result["error"] == svc.E_LIBRARY_MISSING
    rows, total = _run(svc.list_mappings(OWNER, cat["id"]))
    assert total == 0


@pytest.mark.parametrize("doc", [0, -5, None, "10", True, 3.5])
def test_invalid_document_ids_are_rejected(doc):
    cat = _cat("C")
    result = _run(svc.create_mapping(OWNER, cat["id"], "👋", doc))
    assert result["ok"] is False
    assert result["error"] == svc.E_LIBRARY_MISSING


def test_library_entry_of_another_owner_is_rejected():
    cat = _cat("C")
    db_client._fallback["emoji_library"].append(_lib(10, owner=OTHER))
    result = _run(svc.create_mapping(OWNER, cat["id"], "👋", 10))
    assert result["ok"] is False
    assert result["error"] == svc.E_LIBRARY_MISSING


def test_missing_category_is_rejected():
    result = _run(svc.create_mapping(OWNER, 555555, "👋", 1))
    assert result["ok"] is False
    assert result["error"] == svc.E_CATEGORY_MISSING


@pytest.mark.parametrize("raw", ["", None, "x" * 33, "a\nb"])
def test_invalid_simple_emoji_is_rejected(raw):
    cat = _cat("C")
    db_client._fallback["emoji_library"].append(_lib(10))
    result = _run(svc.create_mapping(OWNER, cat["id"], raw, 10))
    assert result["ok"] is False
    assert result["error"] == svc.E_INVALID_EMOJI


def test_maximum_length_emoji_key_is_accepted():
    cat = _cat("C")
    db_client._fallback["emoji_library"].append(_lib(10))
    result = _run(svc.create_mapping(OWNER, cat["id"], "x" * 32, 10))
    assert result["ok"] is True


# ── list / edit / delete mappings ─────────────────────────────────────────────


def test_list_mappings_newest_first():
    cat = _cat("C")
    db_client._fallback["emoji_library"].extend([_lib(1), _lib(2), _lib(3)])
    _mapping(cat["id"], "a", 1)
    _mapping(cat["id"], "b", 2)
    _mapping(cat["id"], "c", 3)
    rows, total = _run(svc.list_mappings(OWNER, cat["id"]))
    assert total == 3
    assert [r["simple_emoji"] for r in rows] == ["c", "b", "a"]


def test_edit_mapping_points_at_new_library_entry():
    cat = _cat("C")
    db_client._fallback["emoji_library"].extend([_lib(1), _lib(2)])
    _mapping(cat["id"], "👋", 1)
    result = _run(svc.replace_mapping(OWNER, cat["id"], "👋", 2))
    assert result["ok"] is True
    row = _run(svc.get_mapping(OWNER, cat["id"], "👋"))
    assert row["document_id"] == 2
    rows, total = _run(svc.list_mappings(OWNER, cat["id"]))
    assert total == 1  # edited in place, not duplicated


def test_replace_missing_library_entry_fails_and_keeps_old():
    cat = _cat("C")
    db_client._fallback["emoji_library"].append(_lib(1))
    _mapping(cat["id"], "👋", 1)
    result = _run(svc.replace_mapping(OWNER, cat["id"], "👋", 777777))
    assert result["ok"] is False
    assert result["error"] == svc.E_LIBRARY_MISSING
    assert _run(svc.get_mapping(OWNER, cat["id"], "👋"))["document_id"] == 1


def test_replace_missing_mapping_fails_honestly():
    cat = _cat("C")
    db_client._fallback["emoji_library"].append(_lib(1))
    result = _run(svc.replace_mapping(OWNER, cat["id"], "👋", 1))
    assert result["ok"] is False
    assert result["error"] == svc.E_MAPPING_MISSING


def test_delete_mapping_keeps_library_entry():
    cat = _cat("C")
    db_client._fallback["emoji_library"].append(_lib(1))
    _mapping(cat["id"], "👋", 1)
    result = _run(svc.delete_mapping(OWNER, cat["id"], "👋"))
    assert result["ok"] is True
    rows, total = _run(svc.list_mappings(OWNER, cat["id"]))
    assert total == 0
    assert _run(db_client.get_emoji_entry(OWNER, 1)) is not None


def test_delete_missing_mapping_fails():
    cat = _cat("C")
    result = _run(svc.delete_mapping(OWNER, cat["id"], "👋"))
    assert result["ok"] is False
    assert result["error"] == svc.E_NOT_FOUND


# ── uniqueness & conflict ─────────────────────────────────────────────────────


def test_same_simple_emoji_allowed_in_different_categories():
    cat_a, cat_b = _cat("A"), _cat("B")
    db_client._fallback["emoji_library"].extend([_lib(1), _lib(2)])
    _mapping(cat_a["id"], "👋", 1)
    _mapping(cat_b["id"], "👋", 2)
    rows_a, _ = _run(svc.list_mappings(OWNER, cat_a["id"]))
    rows_b, _ = _run(svc.list_mappings(OWNER, cat_b["id"]))
    assert rows_a[0]["document_id"] == 1
    assert rows_b[0]["document_id"] == 2


def test_duplicate_simple_emoji_in_one_category_reports_conflict():
    cat = _cat("C")
    db_client._fallback["emoji_library"].extend([_lib(1), _lib(2)])
    _mapping(cat["id"], "👋", 1)
    result = _run(svc.create_mapping(OWNER, cat["id"], "👋", 2))
    assert result["ok"] is False
    assert result["conflict"] is True
    assert result["error"] is None
    assert result["current"]["document_id"] == 1
    assert result["new_entry"]["document_id"] == 2
    assert result["current_entry"]["document_id"] == 1
    rows, total = _run(svc.list_mappings(OWNER, cat["id"]))
    assert total == 1  # nothing overwritten, nothing added


def test_conflict_current_entry_unresolvable_is_reported_not_fabricated():
    cat = _cat("C")
    db_client._fallback["emoji_library"].extend([_lib(1), _lib(2)])
    _mapping(cat["id"], "👋", 1)  # library entry later disappears
    db_client._fallback["emoji_library"] = [e for e in db_client._fallback["emoji_library"] if e["document_id"] != 1]
    result = _run(svc.create_mapping(OWNER, cat["id"], "👋", 2))
    assert result["conflict"] is True
    assert result["current_entry"] is None
    assert result["new_entry"]["document_id"] == 2


def test_same_library_document_in_multiple_mappings():
    cat_a, cat_b = _cat("A"), _cat("B")
    db_client._fallback["emoji_library"].append(_lib(7))
    _mapping(cat_a["id"], "a", 7)
    _mapping(cat_b["id"], "b", 7)
    _mapping(cat_a["id"], "c", 7)
    rows_a, total_a = _run(svc.list_mappings(OWNER, cat_a["id"]))
    rows_b, total_b = _run(svc.list_mappings(OWNER, cat_b["id"]))
    assert total_a == 2 and total_b == 1
    assert all(r["document_id"] == 7 for r in rows_a + rows_b)


def test_uniqueness_backstop_at_db_layer_refuses_duplicate():
    cat = _cat("C")
    db_client._fallback["emoji_library"].append(_lib(1))
    row = {"owner_id": OWNER, "category_id": cat["id"], "simple_emoji": "👋", "document_id": 1}
    first = _run(db_client.insert_emoji_mapping(row))
    assert first is not None
    second = _run(db_client.insert_emoji_mapping(dict(row)))
    assert second is None


def test_category_name_backstop_at_db_layer_refuses_duplicate():
    _run(db_client.insert_emoji_category({"owner_id": OWNER, "name": "Solo"}))
    again = _run(db_client.insert_emoji_category({"owner_id": OWNER, "name": "Solo"}))
    assert again is None


# ── pagination inputs ─────────────────────────────────────────────────────────


def test_mapping_list_pagination_slices_deterministically():
    cat = _cat("C")
    db_client._fallback["emoji_library"].extend(_lib(i) for i in range(1, 13))
    for i in range(1, 13):
        _mapping(cat["id"], f"e{i:02d}", i)
    page1, total = _run(svc.list_mappings(OWNER, cat["id"], limit=10, offset=0))
    page2, _ = _run(svc.list_mappings(OWNER, cat["id"], limit=10, offset=10))
    assert total == 12 and len(page1) == 10 and len(page2) == 2
