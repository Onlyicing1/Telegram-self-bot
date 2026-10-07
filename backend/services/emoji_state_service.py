"""
Emoji Replacement state — Phase 3 (Emoji & Reaction, ROADMAP §13/§14/§29).

The deterministic state-resolution boundary Phase 4 consumes. It owns the
three distinct concerns that must never be conflated:

  * ``replacement_enabled`` — the global ON/OFF toggle (§14). OFF means no
    processing at all, regardless of any category state; ON with no resolved
    category means no replacement either.
  * the global default active category (§13),
  * an optional per-chat category override (§13) — an override, never a
    second authority: when present it wins, when absent the global default
    applies, when neither exists there is no replacement.

Resolution contract (§13/§14, pinned by tests):

    replacement OFF        -> no replacement (None), whatever the categories
    override set           -> the override category
    else global default    -> the global default category
    else                   -> None (no replacement)

Validation is owner-scoped and fail-closed: a category id is accepted only
while the owner's live ``emoji_categories`` row still exists. A category
that is deleted (or never belonged to the owner) can never remain an
effective active category — resolution validates against the live table and
fails closed to "no replacement" instead of fabricating a category.

Persistence goes through the existing ``backend/db/client.py`` pattern (the
single-row-per-owner ``emoji_state`` table + the per-chat
``emoji_chat_overrides`` table; physical schema MANUAL-ONLY, documented in
``IMPLEMENTATION_REPORT.md``, never executed). Every storage failure degrades
honestly: writes report False, reads report None — the caller decides how to
surface that. Nothing is cached across owners and no second scheduler,
executor, or update loop exists here; this module is pure state.

Context isolation: nothing from ``backend.ai`` is imported.
"""
from __future__ import annotations

import logging

from backend.db import client as db_client

logger = logging.getLogger(__name__)


def _valid_owner(owner_id) -> bool:
    return isinstance(owner_id, int) and not isinstance(owner_id, bool) and owner_id > 0


def _valid_chat_id(chat_id) -> bool:
    """Telegram chat ids are ints (positive or negative for groups/channels)."""
    return isinstance(chat_id, int) and not isinstance(chat_id, bool) and chat_id != 0


async def _category_exists(owner_id: int, category_id) -> bool:
    if not isinstance(category_id, int) or isinstance(category_id, bool) or category_id <= 0:
        return False
    row = await db_client.get_emoji_category(owner_id, category_id)
    return row is not None


# ── raw state access ──────────────────────────────────────────────────────────


async def replacement_enabled(owner_id: int) -> bool:
    """The global Emoji Replacement toggle. First-boot default: OFF (§29)."""
    if not _valid_owner(owner_id):
        return False
    row = await db_client.get_emoji_state(owner_id)
    return bool(row.get("replacement_enabled")) if row else False


async def get_global_default_category(owner_id: int) -> int | None:
    """The stored global default category id, or None when unset. The raw
    stored value is returned even if its category row no longer exists —
    resolution (``resolve_effective_category``) is the validated boundary."""
    if not _valid_owner(owner_id):
        return None
    row = await db_client.get_emoji_state(owner_id)
    value = row.get("global_default_category_id") if row else None
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return None


async def get_chat_override(owner_id: int, chat_id: int) -> int | None:
    """The stored per-chat override category id for one chat, or None."""
    if not _valid_owner(owner_id) or not _valid_chat_id(chat_id):
        return None
    row = await db_client.get_emoji_chat_override(owner_id, chat_id)
    value = row.get("override_category_id") if row else None
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return None


# ── mutations (validated, owner-scoped) ──────────────────────────────────────


async def set_replacement_enabled(owner_id: int, enabled: bool) -> bool:
    """Persist the global toggle. Returns False when the write degraded."""
    if not _valid_owner(owner_id) or not isinstance(enabled, bool):
        return False
    return await db_client.upsert_emoji_state(
        owner_id, {"replacement_enabled": enabled}
    )


async def toggle_replacement(owner_id: int) -> bool:
    """Flip the toggle. Returns the NEW value, or False when the current
    state cannot be read or the write degraded (the toggle stays OFF —
    fail closed, never silently ON)."""
    if not _valid_owner(owner_id):
        return False
    current = await replacement_enabled(owner_id)
    new_value = not current
    if not await set_replacement_enabled(owner_id, new_value):
        return False
    return new_value


async def set_global_default_category(owner_id: int, category_id: int) -> bool:
    """Set the global default to a REAL owner-scoped category. A missing or
    foreign category is rejected (False) — nothing is fabricated."""
    if not _valid_owner(owner_id):
        return False
    if not await _category_exists(owner_id, category_id):
        return False
    return await db_client.upsert_emoji_state(
        owner_id, {"global_default_category_id": int(category_id)}
    )


async def clear_global_default_category(owner_id: int) -> bool:
    if not _valid_owner(owner_id):
        return False
    return await db_client.upsert_emoji_state(
        owner_id, {"global_default_category_id": None}
    )


async def set_chat_override(owner_id: int, chat_id: int, category_id: int) -> bool:
    """Set the per-chat override to a REAL owner-scoped category. The same
    ownership/existence validation as the global default applies."""
    if not _valid_owner(owner_id) or not _valid_chat_id(chat_id):
        return False
    if not await _category_exists(owner_id, category_id):
        return False
    return await db_client.upsert_emoji_chat_override(
        owner_id, chat_id, {"override_category_id": int(category_id)}
    )


async def clear_chat_override(owner_id: int, chat_id: int) -> bool:
    """Clear one chat's override; resolution falls back to the global
    default afterwards. Returns False on invalid input or write failure."""
    if not _valid_owner(owner_id) or not _valid_chat_id(chat_id):
        return False
    return await db_client.upsert_emoji_chat_override(
        owner_id, chat_id, {"override_category_id": None}
    )


# ── resolution (the Phase 4 boundary) ────────────────────────────────────────


async def resolve_effective_category(owner_id: int, chat_id: int | None) -> int | None:
    """The category replacement would use for a message in ``chat_id``.

    Order (§13): per-chat override -> global default -> None — and the
    toggle gates everything (§14): when replacement is OFF the result is
    None regardless of any stored category. The winning category is
    validated against the LIVE owner-scoped category table, so a deleted
    category can never remain effective: the resolution fails closed to
    None instead of substituting another category. ``chat_id`` may be None
    (chat unknown — then only the global default can apply).
    """
    if not _valid_owner(owner_id):
        return None
    if not await replacement_enabled(owner_id):
        return None

    override = await get_chat_override(owner_id, chat_id) if _valid_chat_id(chat_id) else None
    if override is not None:
        if await _category_exists(owner_id, override):
            return override
        return None

    default = await get_global_default_category(owner_id)
    if default is not None:
        if await _category_exists(owner_id, default):
            return default
        return None

    return None
