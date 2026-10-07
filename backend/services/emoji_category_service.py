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

Context isolation: nothing from ``backend.ai`` is imported here — categories
and mappings are deterministic owner-managed state, decided by no model.

No second client, loop, scheduler, or executor: every function takes the
owner id explicitly and all Telegram-free storage goes through the db layer.
"""
from __future__ import annotations

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


async def create_category(owner_id: int, raw_name: Any) -> dict[str, Any]:
    """Create one category. Never overwrites: an existing name is reported,
    not replaced. Returns ``{ok, error, category}``."""
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
    row = await db_client.insert_emoji_category({"owner_id": owner_id, "name": name})
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
