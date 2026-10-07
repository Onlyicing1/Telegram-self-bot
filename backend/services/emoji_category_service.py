"""
Emoji categories & mappings service — Phase 2 (Emoji & Reaction, ROADMAP §10–§12).

Business logic for the management layer the Glass UI drives. Persistence goes
through the existing ``backend/db/client.py`` Supabase-or-in-memory-fallback
pattern (dedicated ``emoji_categories`` / ``emoji_mappings`` tables; the
physical schema is MANUAL-ONLY and documented in ``IMPLEMENTATION_REPORT.md``).

Contracts (ROADMAP §7/§10/§11/§12):

* Categories are owner-scoped rows with a unique name per owner. Deleting a
  category removes its mappings and never touches the library: mappings die
  with the category, library entries survive.
* A mapping is ``category_id + simple_emoji → library document_id`` — a
  REFERENCE, never a copy: no premium-emoji definition (alt text, source,
  origin) is stored in the mapping row. The library stays the single source
  of emoji definitions; a mapping is only valid while its
  ``(owner_id, document_id)`` exists in the library.
* ``(owner_id, category_id, simple_emoji)`` is unique. The service checks for
  an existing mapping BEFORE any write and reports a conflict result carrying
  the actual current mapping and both resolved library entries — it never
  overwrites. The ONLY overwrite path is ``replace_mapping``, called on the
  owner's explicit confirmation from the conflict panel.
* The same simple emoji may map differently in different categories, and the
  same library document may be referenced by several mappings — both are
  plain inserts and both are covered by the uniqueness rule above.
* Deterministic and fail closed: every input is validated against explicit
  bounds before any storage call, every result carries an honest error
  string, and nothing is fabricated when a library entry cannot be resolved.

Phase 5 (§26) adds ONE concept on top of the same model — the **Custom
Category**: a category row whose ``is_custom`` flag is true and whose
mappings are a concrete SNAPSHOT composed from other categories in a
deterministic, owner-chosen order. The snapshot semantics are isolated in
the ``plan_composition`` / ``compose_category`` / ``refresh_category``
functions below, so changing the §34-F decision (snapshot vs live
reference) touches these functions only — the mapping model, the resolution
boundary and the replacement pipeline never learn what a Custom Category is:
they keep reading concrete ``emoji_mappings`` rows. A snapshot is never a
live reference: source edits do not move it, and only an explicit Refresh
rebuilds it (atomically, or not at all — the previous valid snapshot stays).

Context isolation: nothing from ``backend.ai`` is imported here — categories
and mappings are deterministic owner-managed state, decided by no model.

No second client, loop, scheduler, or executor: every function takes the
owner id explicitly and all Telegram-free storage goes through the db layer.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from backend.db import client as db_client

logger = logging.getLogger(__name__)

MAX_CATEGORY_NAME_LEN = 64
MAX_SIMPLE_EMOJI_LEN = 32

#: error codes returned in results — deterministic, UI-rendered verbatim
E_INVALID_NAME = "invalid_name"
E_NAME_TOO_LONG = "name_too_long"
E_NAME_EXISTS = "name_exists"
E_NOT_FOUND = "not_found"
E_STORAGE = "storage_failed"
E_INVALID_EMOJI = "invalid_emoji"
E_CATEGORY_MISSING = "category_not_found"
E_LIBRARY_MISSING = "library_entry_not_found"
E_MAPPING_MISSING = "mapping_missing"

#: Composition (§26) error codes and bounds.
E_NOT_CUSTOM = "not_custom_category"
E_SOURCE_MISSING = "source_category_not_found"
E_SOURCE_DUPLICATE = "duplicate_source_category"
E_SOURCE_SELF = "self_composition"
E_SOURCE_CYCLE = "source_cycle"
E_TOO_MANY_SOURCES = "too_many_sources"
E_NO_SOURCES = "no_sources"
E_MAPPING_LIST_INCOMPLETE = "mapping_listing_incomplete"
E_SNAPSHOT_TOO_LARGE = "snapshot_too_large"
E_CONFLICT = "conflict_needs_confirmation"
E_SNAPSHOT_INCOMPLETE = "snapshot_incomplete"

MAX_SOURCE_CATEGORIES = 20
MAX_SNAPSHOT_MAPPINGS = 2000
#: A source graph walk is bounded: beyond this many visited categories the
#: acyclicity of the graph cannot be proven and composition fails closed.
MAX_SOURCE_GRAPH_VISITS = 200


def clean_category_name(raw: Any) -> str | None:
    """Validate a category name deterministically: a single-line, bounded,
    non-empty string after stripping surrounding whitespace. Anything else
    is rejected (fail closed) — no normalization beyond the strip."""
    if not isinstance(raw, str):
        return None
    name = raw.strip()
    if not name or "\n" in name or "\r" in name:
        return None
    if len(name) > MAX_CATEGORY_NAME_LEN:
        return None
    return name


def clean_simple_emoji(raw: Any) -> str | None:
    """Validate a simple-emoji mapping key deterministically: a single-line,
    bounded, non-empty string after stripping whitespace. The key's emoji
    semantics are Telegram's business at replacement time (Phase 4) — this
    layer only enforces structure, so the owner's exact input is preserved."""
    if not isinstance(raw, str):
        return None
    emoji = raw.strip()
    if not emoji or "\n" in emoji or "\r" in emoji:
        return None
    if len(emoji) > MAX_SIMPLE_EMOJI_LEN:
        return None
    return emoji


def _valid_owner(owner_id: Any) -> bool:
    return isinstance(owner_id, int) and not isinstance(owner_id, bool) and owner_id > 0


def _valid_id(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


# ── categories ────────────────────────────────────────────────────────────────


async def create_category(
    owner_id: int, raw_name: Any, *, is_custom: bool = False,
) -> dict[str, Any]:
    """Create one category. Never overwrites: an existing name is reported,
    not replaced. Returns ``{ok, error, category}``.

    ``is_custom=True`` creates a Custom Category (§26): the type is explicit
    row metadata, never inferred from the name, and it is only sent for a
    custom row so ordinary category writes stay byte-identical to Phase 2
    (``is_custom``/``source_category_ids`` are MANUAL-ONLY columns — a
    configured Supabase without them reports an honest storage failure for
    custom creation and keeps working untouched for ordinary creation).
    """
    result: dict[str, Any] = {"ok": False, "error": None, "category": None}
    if not _valid_owner(owner_id):
        result["error"] = E_INVALID_NAME
        return result
    name = clean_category_name(raw_name)
    if name is None:
        result["error"] = (
            E_NAME_TOO_LONG if isinstance(raw_name, str) and len(raw_name.strip()) > MAX_CATEGORY_NAME_LEN
            else E_INVALID_NAME
        )
        return result
    data: dict[str, Any] = {"owner_id": owner_id, "name": name}
    if is_custom:
        data["is_custom"] = True
        data["source_category_ids"] = []
    row = await db_client.insert_emoji_category(data)
    if row is None:
        existing = await db_client.get_emoji_category_by_name(owner_id, name)
        if existing is not None:
            result["error"] = E_NAME_EXISTS
        else:
            result["error"] = E_STORAGE
        return result
    result["ok"] = True
    result["category"] = row
    return result


async def list_categories(
    owner_id: int, limit: int = 50, offset: int = 0,
) -> tuple[list[dict[str, Any]], int, dict[int, int] | None]:
    """Owner's categories newest-first as ``(rows, total, mapping_counts)``.

    ``mapping_counts`` maps category_id → live mapping count; None means the
    durable count read failed and the UI must show an unknown-count state
    instead of a fabricated 0 (ROADMAP §29 degradation labeling)."""
    rows, total = await db_client.list_emoji_categories(owner_id, limit=limit, offset=offset)
    counts = await db_client.count_emoji_mappings_by_category(owner_id)
    return rows, total, counts


async def get_category(owner_id: int, category_id: int) -> dict[str, Any] | None:
    if not _valid_id(category_id):
        return None
    return await db_client.get_emoji_category(owner_id, category_id)


async def category_mapping_count(owner_id: int, category_id: int) -> int | None:
    """Live mapping count for one category; None when the count read fails."""
    counts = await db_client.count_emoji_mappings_by_category(owner_id)
    if counts is None:
        return None
    return counts.get(category_id, 0)


async def rename_category(
    owner_id: int, category_id: int, raw_name: Any,
) -> dict[str, Any]:
    """Rename one category. Deterministic: the new name must be valid, the
    category must exist, and no sibling category may already hold the name.
    Returns ``{ok, error, category}``."""
    result: dict[str, Any] = {"ok": False, "error": None, "category": None}
    if not _valid_id(category_id) or not _valid_owner(owner_id):
        result["error"] = E_NOT_FOUND
        return result
    name = clean_category_name(raw_name)
    if name is None:
        result["error"] = (
            E_NAME_TOO_LONG if isinstance(raw_name, str) and len(raw_name.strip()) > MAX_CATEGORY_NAME_LEN
            else E_INVALID_NAME
        )
        return result
    current = await db_client.get_emoji_category(owner_id, category_id)
    if current is None:
        result["error"] = E_NOT_FOUND
        return result
    if current.get("name") == name:
        result["ok"] = True
        result["category"] = current
        return result
    row = await db_client.update_emoji_category(owner_id, category_id, name)
    if row is None:
        existing = await db_client.get_emoji_category_by_name(owner_id, name)
        result["error"] = E_NAME_EXISTS if existing is not None else E_STORAGE
        return result
    result["ok"] = True
    result["category"] = row
    return result


async def delete_category(owner_id: int, category_id: int) -> dict[str, Any]:
    """Delete one category and ALL of its mappings; library entries are
    never touched. Fail closed ordering: the mappings go first — if that
    write fails the category is left intact; if the category row itself
    cannot then be deleted the result says so honestly.
    Returns ``{ok, error, removed_mappings, category_deleted}``."""
    result: dict[str, Any] = {
        "ok": False, "error": None, "removed_mappings": 0, "category_deleted": False,
    }
    if not _valid_id(category_id) or not _valid_owner(owner_id):
        result["error"] = E_NOT_FOUND
        return result
    category = await db_client.get_emoji_category(owner_id, category_id)
    if category is None:
        result["error"] = E_NOT_FOUND
        return result
    removed = await db_client.delete_emoji_mappings_for_category(owner_id, category_id)
    if removed < 0:
        result["error"] = E_STORAGE
        return result
    result["removed_mappings"] = removed
    deleted = await db_client.delete_emoji_category(owner_id, category_id)
    result["category_deleted"] = deleted
    if not deleted:
        result["error"] = E_STORAGE
        return result
    result["ok"] = True
    return result


# ── mappings ──────────────────────────────────────────────────────────────────


async def _resolve_library_entry(owner_id: int, document_id: Any) -> dict[str, Any] | None | str:
    """Owner-scoped library lookup. Returns the row, None (not found), or
    the string "invalid" for a structurally invalid document id."""
    if isinstance(document_id, bool) or not isinstance(document_id, int):
        return "invalid"
    if document_id <= 0:
        return "invalid"
    return await db_client.get_emoji_entry(owner_id, document_id)


async def create_mapping(
    owner_id: int, category_id: int, raw_emoji: Any, document_id: Any,
) -> dict[str, Any]:
    """Define ``simple_emoji → library document`` inside one category.

    Never overwrites: when the category already defines the simple emoji the
    result carries ``conflict=True`` with the ACTUAL current mapping row and
    both library entries resolved (an unresolvable entry is reported as
    ``None`` — never fabricated). Storage happens only on the conflict-free
    path; the overwrite path is ``replace_mapping``.
    Returns ``{ok, error, conflict, mapping, current, current_entry, new_entry}``.
    """
    result: dict[str, Any] = {
        "ok": False, "error": None, "conflict": False, "mapping": None,
        "current": None, "current_entry": None, "new_entry": None,
    }
    if not _valid_owner(owner_id) or not _valid_id(category_id):
        result["error"] = E_CATEGORY_MISSING
        return result
    emoji = clean_simple_emoji(raw_emoji)
    if emoji is None:
        result["error"] = E_INVALID_EMOJI
        return result
    category = await db_client.get_emoji_category(owner_id, category_id)
    if category is None:
        result["error"] = E_CATEGORY_MISSING
        return result
    new_entry = await _resolve_library_entry(owner_id, document_id)
    if new_entry == "invalid":
        result["error"] = E_LIBRARY_MISSING
        return result
    if new_entry is None:
        result["error"] = E_LIBRARY_MISSING
        return result
    result["new_entry"] = new_entry

    current = await db_client.get_emoji_mapping(owner_id, category_id, emoji)
    if current is not None:
        current_entry = await _resolve_library_entry(owner_id, current.get("document_id"))
        result["conflict"] = True
        result["current"] = current
        result["current_entry"] = None if current_entry == "invalid" else current_entry
        return result

    row = await db_client.insert_emoji_mapping(
        {
            "owner_id": owner_id,
            "category_id": category_id,
            "simple_emoji": emoji,
            "document_id": document_id,
        }
    )
    if row is None:
        result["error"] = E_STORAGE
        return result
    result["ok"] = True
    result["mapping"] = row
    return result


async def replace_mapping(
    owner_id: int, category_id: int, raw_emoji: Any, document_id: Any,
) -> dict[str, Any]:
    """The ONLY overwrite path — the explicit owner confirmation from the
    conflict panel. Requires the mapping to still exist (a stale panel never
    resurrects a deleted mapping) and the new library entry to resolve.
    Returns ``{ok, error, mapping}``."""
    result: dict[str, Any] = {"ok": False, "error": None, "mapping": None}
    if not _valid_owner(owner_id) or not _valid_id(category_id):
        result["error"] = E_MAPPING_MISSING
        return result
    emoji = clean_simple_emoji(raw_emoji)
    if emoji is None:
        result["error"] = E_INVALID_EMOJI
        return result
    new_entry = await _resolve_library_entry(owner_id, document_id)
    if new_entry == "invalid" or new_entry is None:
        result["error"] = E_LIBRARY_MISSING
        return result
    current = await db_client.get_emoji_mapping(owner_id, category_id, emoji)
    if current is None:
        result["error"] = E_MAPPING_MISSING
        return result
    row = await db_client.update_emoji_mapping(owner_id, category_id, emoji, document_id)
    if row is None:
        result["error"] = E_STORAGE
        return result
    result["ok"] = True
    result["mapping"] = row
    return result


async def list_mappings(
    owner_id: int, category_id: int, limit: int = 50, offset: int = 0,
) -> tuple[list[dict[str, Any]], int]:
    return await db_client.list_emoji_mappings(owner_id, category_id, limit=limit, offset=offset)


async def get_mapping(
    owner_id: int, category_id: int, raw_emoji: Any,
) -> dict[str, Any] | None:
    emoji = clean_simple_emoji(raw_emoji)
    if emoji is None or not _valid_id(category_id):
        return None
    return await db_client.get_emoji_mapping(owner_id, category_id, emoji)


async def delete_mapping(
    owner_id: int, category_id: int, raw_emoji: Any,
) -> dict[str, Any]:
    """Remove one mapping. The library entry it referenced is untouched."""
    result: dict[str, Any] = {"ok": False, "error": None, "removed": False}
    emoji = clean_simple_emoji(raw_emoji)
    if emoji is None or not _valid_id(category_id) or not _valid_owner(owner_id):
        result["error"] = E_INVALID_EMOJI
        return result
    removed = await db_client.delete_emoji_mapping(owner_id, category_id, emoji)
    if not removed:
        result["error"] = E_NOT_FOUND
        return result
    result["ok"] = True
    result["removed"] = True
    return result


# ── composition (Phase 5, ROADMAP §26) ──────────────────────────────────────


def is_custom_category(category: Any) -> bool:
    """Explicit category TYPE metadata — never inferred from the name."""
    return bool(isinstance(category, dict) and category.get("is_custom") is True)


def _validate_source_ids(raw: Any) -> tuple[list[int] | None, str | None]:
    """Deterministic validation of a composition source list.

    Returns ``(ids, error)``: a bounded, ordered list of distinct positive
    owner-scoped ids, or the reason it cannot be used. A duplicate is an
    explicit error — never silently collapsed; an empty selection is an
    error (there is nothing to compose).
    """
    if raw is None:
        return None, E_NO_SOURCES
    if not isinstance(raw, (list, tuple)):
        return None, E_SOURCE_MISSING
    if len(raw) > MAX_SOURCE_CATEGORIES:
        return None, E_TOO_MANY_SOURCES
    out: list[int] = []
    seen: set[int] = set()
    for value in raw:
        if not _valid_id(value):
            return None, E_SOURCE_MISSING
        if value in seen:
            return None, E_SOURCE_DUPLICATE
        seen.add(value)
        out.append(value)
    if not out:
        return None, E_NO_SOURCES
    return out, None


def category_sources(category: Any) -> list[int]:
    """The persisted, normalized source list of a category row.

    ``[]`` for an ordinary category and for a row written before the
    MANUAL-ONLY columns existed — an absent source list is honestly empty,
    never fabricated.
    """
    if not isinstance(category, dict):
        return []
    raw = category.get("source_category_ids")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return []
    cleaned, error = _validate_source_ids(raw)
    if error in (E_NO_SOURCES,) and isinstance(raw, (list, tuple)) and not raw:
        return []
    return cleaned or []


async def _reachable_category(owner_id: int, start_ids: list[int], target_id: int) -> str | None:
    """Bounded walk over the persisted source graph.

    Returns ``"cycle"`` when ``target_id`` is reachable from ``start_ids``
    (self-composition included — a category sourcing itself is a cycle), and
    ``"unresolved"`` when the graph is too large to verify within
    ``MAX_SOURCE_GRAPH_VISITS``. ``None`` means the graph was proven acyclic
    for this target. Only owner-scoped rows are ever read.
    """
    visited: set[int] = set()
    stack: list[int] = list(start_ids)
    while stack:
        current = stack.pop()
        if current == target_id:
            return "cycle"
        if current in visited:
            continue
        visited.add(current)
        if len(visited) > MAX_SOURCE_GRAPH_VISITS:
            return "unresolved"
        row = await db_client.get_emoji_category(owner_id, current)
        if row is None or not is_custom_category(row):
            continue
        stack.extend(category_sources(row))
    return None


async def _snapshot_plan(
    owner_id: int, source_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    """Read each source's mappings IN ORDER and merge them with
    first-source-wins.

    The result is concrete mapping entries (``simple_emoji`` + library
    ``document_id``) — never a live reference to the source. Mappings whose
    library entry can no longer be resolved are counted honestly
    (``unresolvable``) but still copied: the snapshot faithfully equals the
    sources, and the resolver keeps failing closed on them at replacement
    time. An incomplete mapping listing fails the whole plan closed.
    """
    merged: dict[str, dict[str, Any]] = {}
    conflicts: dict[str, dict[str, Any]] = {}
    per_source: dict[int, int] = {}
    unresolvable = 0
    scanned = 0
    for row in source_rows:
        sid = row.get("id")
        rows, total = await db_client.list_emoji_mappings(
            owner_id, sid, limit=MAX_SNAPSHOT_MAPPINGS, offset=0,
        )
        if not isinstance(rows, list):
            return {"error": E_MAPPING_LIST_INCOMPLETE}
        if isinstance(total, int) and total > len(rows):
            return {"error": E_MAPPING_LIST_INCOMPLETE}
        per_source[sid] = len(rows)
        for mapping in rows:
            simple = mapping.get("simple_emoji")
            document_id = mapping.get("document_id")
            if not isinstance(simple, str) or not simple:
                continue
            if not _valid_id(document_id):
                continue
            scanned += 1
            entry = await db_client.get_emoji_entry(owner_id, document_id)
            if not isinstance(entry, dict) or not str(entry.get("alt_text") or ""):
                unresolvable += 1
            kept = merged.get(simple)
            if kept is None:
                merged[simple] = {
                    "simple_emoji": simple, "document_id": document_id, "source_id": sid,
                }
                continue
            record = conflicts.setdefault(simple, {
                "simple_emoji": simple, "kept": dict(kept), "dropped": [],
            })
            record["dropped"].append({"source_id": sid, "document_id": document_id})
    if len(merged) > MAX_SNAPSHOT_MAPPINGS:
        return {"error": E_SNAPSHOT_TOO_LARGE}
    mappings: list[dict[str, Any]] = []
    for simple, entry in merged.items():
        mappings.append({
            "simple_emoji": simple,
            "document_id": entry["document_id"],
            "source_id": entry["source_id"],
        })
    return {
        "mappings": mappings,
        "conflicts": list(conflicts.values()),
        "unresolvable": unresolvable,
        "scanned": scanned,
        "per_source": per_source,
    }


def _plan_base(category: Any) -> dict[str, Any]:
    return {
        "ok": False, "error": None, "category": category, "sources": [],
        "mappings": [], "conflicts": [], "unresolvable": 0, "scanned": 0,
        "empty_sources": [], "missing_sources": [],
    }


async def plan_composition(
    owner_id: int, category_id: int, source_ids: Any,
) -> dict[str, Any]:
    """Compute the snapshot a composition WOULD produce — no writes at all.

    Validates the target (a real owner-scoped Custom Category), the source
    list (bounded, distinct, real owner-scoped categories, no self, no
    cycle) and every source's mappings (bounded, complete listing), then
    merges them deterministically: **the owner's source order is the
    precedence** — the first source that defines a simple emoji wins, every
    later definition is reported as a conflict and is never silently
    applied. Returns a report the UI can render and the persist step can
    consume.
    """
    if not _valid_owner(owner_id) or not _valid_id(category_id):
        return _plan_base(None) | {"error": E_NOT_FOUND}
    category = await db_client.get_emoji_category(owner_id, category_id)
    if category is None:
        return _plan_base(None) | {"error": E_NOT_FOUND}
    result = _plan_base(category)
    if not is_custom_category(category):
        result["error"] = E_NOT_CUSTOM
        return result

    cleaned, error = _validate_source_ids(source_ids)
    if error is not None:
        result["error"] = error
        return result
    if category_id in cleaned:
        result["error"] = E_SOURCE_SELF
        return result

    reached = await _reachable_category(owner_id, cleaned, category_id)
    if reached is not None:
        result["error"] = E_SOURCE_CYCLE
        return result

    source_rows: list[dict[str, Any]] = []
    missing: list[int] = []
    for sid in cleaned:
        row = await db_client.get_emoji_category(owner_id, sid)
        if row is None:
            missing.append(sid)
            continue
        source_rows.append(row)
    if missing:
        result["error"] = E_SOURCE_MISSING
        result["missing_sources"] = missing
        return result

    plan = await _snapshot_plan(owner_id, source_rows)
    if plan.get("error"):
        result["error"] = plan["error"]
        return result
    per_source = plan.get("per_source") or {}
    result.update(
        ok=True,
        sources=source_rows,
        mappings=plan["mappings"],
        conflicts=plan["conflicts"],
        unresolvable=plan["unresolvable"],
        scanned=plan["scanned"],
        empty_sources=[row.get("id") for row in source_rows if not per_source.get(row.get("id"))],
    )
    return result


async def _replace_category_snapshot(
    owner_id: int, category_id: int, target_mappings: list[dict[str, Any]],
) -> dict[str, Any]:
    """Replace one category's mapping set with ``target_mappings``.

    The target is validated first (bounded, exactly one entry per simple
    emoji, every library reference structurally valid). Then the rows are
    applied NEW-FIRST: additions and repoints happen before any deletion, so
    a failure leaves the previous rows intact; on a failure during that
    phase the partial change is undone with a compensating rollback
    (best effort — there is no multi-statement transaction across the
    db layer). Deletions run last: a failed deletion leaves a SUPERSET, which
    is reported honestly as ``E_SNAPSHOT_INCOMPLETE`` instead of pretending
    the snapshot equals the sources.
    """
    result: dict[str, Any] = {
        "ok": False, "error": None, "added": 0, "updated": 0,
        "removed": 0, "rolled_back": True, "stale": [],
    }
    target: dict[str, int] = {}
    for mapping in target_mappings:
        simple = mapping.get("simple_emoji")
        document_id = mapping.get("document_id")
        if not isinstance(simple, str) or not simple or not _valid_id(document_id):
            result["error"] = E_INVALID_EMOJI
            return result
        if simple in target:
            result["error"] = E_CONFLICT
            return result
        target[simple] = document_id
    if len(target) > MAX_SNAPSHOT_MAPPINGS:
        result["error"] = E_SNAPSHOT_TOO_LARGE
        return result

    current_rows, total = await db_client.list_emoji_mappings(
        owner_id, category_id, limit=MAX_SNAPSHOT_MAPPINGS, offset=0,
    )
    if not isinstance(current_rows, list):
        result["error"] = E_MAPPING_LIST_INCOMPLETE
        return result
    if isinstance(total, int) and total > len(current_rows):
        result["error"] = E_MAPPING_LIST_INCOMPLETE
        return result
    current: dict[str, dict[str, Any]] = {}
    for row in current_rows:
        simple = row.get("simple_emoji")
        if isinstance(simple, str) and simple:
            current[simple] = row

    inserted: list[str] = []
    repointed: list[tuple[str, Any]] = []
    for simple, document_id in target.items():
        existing = current.get(simple)
        if existing is None:
            row = await db_client.insert_emoji_mapping({
                "owner_id": owner_id,
                "category_id": category_id,
                "simple_emoji": simple,
                "document_id": document_id,
            })
            if row is None:
                result["error"] = E_STORAGE
                result["rolled_back"] = await _rollback_partial(
                    owner_id, category_id, inserted, repointed,
                )
                return result
            inserted.append(simple)
            result["added"] += 1
        elif existing.get("document_id") != document_id:
            row = await db_client.update_emoji_mapping(
                owner_id, category_id, simple, document_id,
            )
            if row is None:
                result["error"] = E_STORAGE
                result["rolled_back"] = await _rollback_partial(
                    owner_id, category_id, inserted, repointed,
                )
                return result
            repointed.append((simple, existing.get("document_id")))
            result["updated"] += 1

    stale = [simple for simple in current if simple not in target]
    failed_deletions: list[str] = []
    for simple in stale:
        if not await db_client.delete_emoji_mapping(owner_id, category_id, simple):
            failed_deletions.append(simple)
    if failed_deletions:
        result["error"] = E_SNAPSHOT_INCOMPLETE
        result["stale"] = failed_deletions
        return result
    result["removed"] = len(stale)
    result["ok"] = True
    return result


async def _rollback_partial(
    owner_id: int,
    category_id: int,
    inserted: list[str],
    repointed: list[tuple[str, Any]],
) -> bool:
    """Undo the additions/repoints of a failed snapshot write (best effort,
    reported: False means the caller must not claim the old snapshot is
    intact)."""
    ok = True
    for simple in inserted:
        if not await db_client.delete_emoji_mapping(owner_id, category_id, simple):
            ok = False
    for simple, old_document_id in repointed:
        row = await db_client.update_emoji_mapping(
            owner_id, category_id, simple, old_document_id,
        )
        if row is None:
            ok = False
    return ok


async def _persist_composition(
    owner_id: int,
    category_id: int,
    plan: dict[str, Any],
    *,
    sources_to_persist: list[int],
    removed_sources: list[int],
) -> dict[str, Any]:
    """Persist a ready plan: source list first, snapshot second, with the
    previous state restored when the snapshot write fails."""
    result = dict(plan)
    result["ok"] = False
    result["removed_sources"] = list(removed_sources)
    previous_sources = category_sources(plan.get("category"))
    if previous_sources != list(sources_to_persist):
        if not await db_client.set_emoji_category_sources(
            owner_id, category_id, list(sources_to_persist),
        ):
            result["error"] = E_STORAGE
            return result
    replaced = await _replace_category_snapshot(
        owner_id, category_id, plan.get("mappings") or [],
    )
    if not replaced["ok"]:
        result["error"] = replaced["error"]
        result["stale"] = replaced.get("stale") or []
        result["rolled_back"] = replaced.get("rolled_back", True)
        if previous_sources != list(sources_to_persist):
            restored = await db_client.set_emoji_category_sources(
                owner_id, category_id, previous_sources,
            )
            result["sources_restored"] = bool(restored)
        return result
    result.update(
        ok=True,
        error=None,
        sources=[row.get("id") for row in plan.get("sources") or []],
        added=replaced["added"],
        updated=replaced["updated"],
        removed=replaced["removed"],
    )
    return result


async def compose_category(
    owner_id: int,
    category_id: int,
    source_ids: Any,
    *,
    confirm_conflicts: bool = False,
) -> dict[str, Any]:
    """Compose (or recompose) one Custom Category from ``source_ids``.

    The plan is recomputed against live state at this moment — a stale UI
    flow can never apply a stale plan. When the sources disagree on a simple
    emoji the composition is REFUSED unless ``confirm_conflicts`` is set: the
    owner's explicit confirmation is what turns the reported precedence
    (first source wins) into the stored snapshot. Nothing is written before
    that, and the snapshot replacement itself is all-or-nothing (or reported
    honestly).
    """
    plan = await plan_composition(owner_id, category_id, source_ids)
    if not plan["ok"]:
        return plan
    if plan["conflicts"] and not confirm_conflicts:
        plan["error"] = E_CONFLICT
        plan["ok"] = False
        return plan
    return await _persist_composition(
        owner_id,
        category_id,
        plan,
        sources_to_persist=[row.get("id") for row in plan["sources"]],
        removed_sources=[],
    )


async def refresh_category(
    owner_id: int,
    category_id: int,
    *,
    confirm_conflicts: bool = False,
) -> dict[str, Any]:
    """Rebuild one Custom Category's snapshot from its PERSISTED sources.

    A source that no longer exists (deleted, or belonging to another owner)
    is dropped from the composition and reported — never fabricated — and
    the snapshot is rebuilt from the remaining sources; when nothing is left
    to compose the previous snapshot is kept and the result says why. New
    conflicts revealed by changed sources also require explicit confirmation
    (``confirm_conflicts``): without it the previous valid snapshot stays
    exactly as it is.
    """
    if not _valid_owner(owner_id) or not _valid_id(category_id):
        return _plan_base(None) | {"error": E_NOT_FOUND}
    category = await db_client.get_emoji_category(owner_id, category_id)
    if category is None:
        return _plan_base(None) | {"error": E_NOT_FOUND}
    result = _plan_base(category)
    if not is_custom_category(category):
        result["error"] = E_NOT_CUSTOM
        return result
    persisted = category_sources(category)
    if not persisted:
        result["error"] = E_NO_SOURCES
        return result

    present: list[dict[str, Any]] = []
    missing: list[int] = []
    for sid in persisted:
        row = await db_client.get_emoji_category(owner_id, sid)
        if row is None:
            missing.append(sid)
        else:
            present.append(row)
    result["missing_sources"] = missing
    if not present:
        result["error"] = E_SOURCE_MISSING
        return result

    reached = await _reachable_category(
        owner_id, [row.get("id") for row in present], category_id,
    )
    if reached is not None:
        result["error"] = E_SOURCE_CYCLE
        return result

    plan = await _snapshot_plan(owner_id, present)
    if plan.get("error"):
        result["error"] = plan["error"]
        return result
    per_source = plan.get("per_source") or {}
    result.update(
        sources=present,
        mappings=plan["mappings"],
        conflicts=plan["conflicts"],
        unresolvable=plan["unresolvable"],
        scanned=plan["scanned"],
        empty_sources=[
            row.get("id") for row in present if not per_source.get(row.get("id"))
        ],
    )
    if result["conflicts"] and not confirm_conflicts:
        result["error"] = E_CONFLICT
        return result
    result["ok"] = True
    return await _persist_composition(
        owner_id,
        category_id,
        result,
        sources_to_persist=[row.get("id") for row in present],
        removed_sources=missing,
    )
