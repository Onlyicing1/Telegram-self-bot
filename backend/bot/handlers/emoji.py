"""
Emoji Library & Categories — Glass UI (Phases 1–2, ROADMAP §9–§12).

Exposes the bounded Saved Messages emoji-library import, the imported
library, and the Phase 2 categories-and-mappings management layer through
the repository's standard panel machinery — the panel registry,
``InlinePanelBuilder``, ``panel:``/``action:`` callback conventions and the
callback router's owner check. No new UI framework, no second
client/loop/scheduler: storage reads/writes go through the existing
``backend.db.client`` emoji helpers, and every category/mapping decision
runs through ``backend.services.emoji_category_service`` (uniqueness,
conflict detection, deletion semantics live THERE, not in the UI).

Honesty contract: the import report is rendered straight from the service
result — success, degraded and failed states are all shown, with the
failure reason when there is one. Browsers show only what the rows actually
contain (alt text, document id, origin, created date); the mapping conflict
panel renders the actual current and requested premium emoji side by side
from their resolved library entries and never fabricates a visual for an
entry that cannot be resolved.

Flow state (mapping editor / destructive confirmations) is a per-owner
draft buffer keyed by ``owner_id`` — the same server-side-buffer convention
the Bio template builder uses — with a bounded staleness so a forgotten
flow can never drive a later panel. One flow at a time per owner, matching
the single pending-input contract of ``backend.helper.input_state``.
"""
from __future__ import annotations

import logging
import time
from typing import Any

from backend.helper.inline_engine import get_owner_id, get_self_client
from backend.helper.panels import (
    InlinePanelBuilder,
    register_action,
    register_panel,
)
from backend.helper.input_state import set_pending
from backend.services import emoji_category_service as cat_service
from backend.services import emoji_state_service as state_service
from backend.services import premium_emoji_probe_service as probe_service
from backend.services import reaction_service
from backend.services.emoji_library_service import import_from_saved_messages

logger = logging.getLogger(__name__)

#: 2×5 grid: ten library entries per page, two buttons per row.
_PAGE_SIZE = 10

_LABEL_MAX = 24


def _entry_label(row: dict[str, Any]) -> str:
    alt = str(row.get("alt_text") or "").strip()
    label = " ".join(alt.split()) if alt else "·"
    return label[:_LABEL_MAX] + ("…" if len(label) > _LABEL_MAX else "")


# ── main panel ────────────────────────────────────────────────────────────────

_MAIN_PANEL = "emoji"
_LIBRARY_PANEL = "emoji_library"
_ENTRY_PANEL = "emoji_entry"
_CATEGORIES_PANEL = "emoji_categories"
_CATEGORY_PANEL = "emoji_cat"
_MAPPINGS_PANEL = "emoji_mappings"
_MAPPING_PANEL = "emoji_map"
_PICKER_PANEL = "emoji_pick"
_REPLACEMENT_PANEL = "emoji_replacement"
_STATE_SCOPE_PICKER_PANEL = "emoji_state_scope"
_COMPOSE_PANEL = "emoji_compose"
_SOURCE_PICKER_PANEL = "emoji_sources"


async def _emoji_panel_handler(event, extra: str) -> tuple[str, str, list] | None:
    from backend.db import client as db_client

    owner = get_owner_id()
    total = None
    if owner:
        _rows, total = await db_client.list_emoji_entries(owner, limit=1, offset=0)

    lines = ["😀 **Emoji Library**", ""]
    if total is None:
        lines.append("_Library size unknown._")
    elif total == 0:
        lines.append("_Empty — run Import to scan Saved Messages._")
    else:
        lines.append(f"{total} custom emoji")
    if owner:
        enabled = await state_service.replacement_enabled(owner)
        lines.append("")
        lines.append(f"Replacement: {'✅ ON' if enabled else '❌ OFF'}")
    builder = InlinePanelBuilder()
    builder.add_row("⬇ Import from Saved Messages", "action:emoji_import")
    builder.add_row("💬 React to a message", "action:emoji_react")
    builder.add_row("💬 Set Reaction Emoji", f"action:{_PROBE_ACTION}")
    builder.add_row("📚 Library", f"panel:{_LIBRARY_PANEL}")
    builder.add_row("🗂 Categories", f"panel:{_CATEGORIES_PANEL}")
    builder.add_row("🔁 Replacement", f"panel:{_REPLACEMENT_PANEL}")
    return "Emoji", "\n".join(lines), builder.build()


# ── per-owner flow draft (server-side buffer, Bio-builder convention) ─────────

_DRAFT_TTL_S = 600  # a forgotten flow expires; it can never drive a later panel

_drafts: dict[int, dict] = {}


def _set_draft(owner: int, **fields: Any) -> dict:
    draft = {"created_at": time.monotonic()}
    draft.update(fields)
    _drafts[owner] = draft
    return draft


def _get_draft(owner: int) -> dict | None:
    draft = _drafts.get(owner)
    if draft is None:
        return None
    if time.monotonic() - draft.get("created_at", 0) > _DRAFT_TTL_S:
        _drafts.pop(owner, None)
        return None
    return draft


def _clear_draft(owner: int) -> None:
    _drafts.pop(owner, None)


# ── shared render helpers ─────────────────────────────────────────────────────


def _truncate(text: str, limit: int = _LABEL_MAX) -> str:
    return text[:limit] + ("…" if len(text) > limit else "")


def _count_label(counts: dict[int, int] | None, category_id: Any) -> str:
    if counts is None or not isinstance(category_id, int) or isinstance(category_id, bool):
        return "?"
    return str(counts.get(category_id, 0))


def _entry_visual(row: dict[str, Any] | None) -> str:
    """Honest premium-emoji representation for panel text: the library row's
    alt glyph + document id, or an explicit unavailable marker — never a
    fabricated visual (ROADMAP §12)."""
    if row is None:
        return "· unavailable ·"
    alt = " ".join(str(row.get("alt_text") or "").split()) or "·"
    return f"{alt} (#{row.get('document_id', '?')})"


def _error_panel(title: str, message: str, back_data: str | None = None) -> tuple[str, str, list]:
    builder = InlinePanelBuilder()
    if back_data:
        builder.add_row("↩ Retry", back_data)
    return title, f"! {message}", builder.build()


def _report_lines(report: dict[str, Any]) -> list[str]:
    """Concise, honest report body — failure first, counters second."""
    lines: list[str] = []
    if report.get("error"):
        lines.append(f"✗ {report['error']}")
    elif not report.get("ok"):
        lines.append(f"✗ {report.get('failed', 0)} record(s) failed to save")
    elif report.get("degraded"):
        if report.get("set_error"):
            lines.append(f"◌ {report['set_error']}")
        else:
            degraded = []
            if report.get("unresolved_documents"):
                degraded.append(f"{report['unresolved_documents']} doc(s) unresolved")
            if report.get("documents_without_set"):
                degraded.append(f"{report['documents_without_set']} without set info")
            if report.get("hit_set_limit") or report.get("hit_set_member_limit"):
                degraded.append("set scan budget reached")
            lines.append("◌ Partial set enrichment — " + ", ".join(degraded))
    else:
        lines.append("✓ Import complete")

    lines.append("")
    lines.append(
        f"Scanned {report.get('scanned_messages', 0)} messages"
        f" · seen {report.get('custom_emoji_seen', 0)}"
        f" · bad {report.get('malformed_entities', 0)}"
    )
    lines.append(
        f"Imported {report.get('imported', 0)}"
        f" · duplicates {report.get('duplicates', 0)}"
        + (f" · failed {report.get('failed', 0)}" if report.get("failed") else "")
    )
    sets_resolved = report.get("sets_resolved", 0)
    if sets_resolved or report.get("set_imported"):
        lines.append(
            f"Sets {sets_resolved} · members +{report.get('set_imported', 0)}"
            f" · known {report.get('set_duplicates', 0)}"
            + (f" · failed {report.get('set_failed', 0)}" if report.get("set_failed") else "")
        )
    total = report.get("library_total")
    if total is not None:
        lines.append(f"Library: {total}")
    if report.get("end_reached"):
        lines.append("_End of Saved Messages reached._")
    if report.get("hit_scan_limit"):
        lines.append("_Scan budget reached — run Import again to continue._")
    if report.get("hit_record_limit"):
        lines.append("_Record budget reached — run Import again to continue._")
    return lines


def _render_report(report: dict[str, Any]) -> tuple[str, str, list]:
    body = "\n".join(_report_lines(report))
    builder = InlinePanelBuilder()
    builder.add_row("↻ Import again", "action:emoji_import")
    builder.add_row("📚 Library", f"panel:{_LIBRARY_PANEL}")
    return "Emoji Import", body, builder.build()


async def _import_action(event, extra: str, chat_id: int) -> tuple[str, str, list] | None:
    self_client = get_self_client()
    if self_client is None:
        return "Emoji Import", "! Self client is not connected.", InlinePanelBuilder().build()
    owner = get_owner_id()
    if not owner:
        return "Emoji Import", "! Owner is not set.", InlinePanelBuilder().build()
    try:
        report = await import_from_saved_messages(self_client, owner)
    except Exception as exc:  # the service reports its own failures; this is a last resort
        logger.error("[EMOJI_UI] import crashed: %s", exc)
        return "Emoji Import", "! Import failed unexpectedly.", InlinePanelBuilder().build()
    return _render_report(report)


# ── library browser (2×5 grid) ────────────────────────────────────────────────


def _page_count(total: int) -> int:
    return max(1, (total + _PAGE_SIZE - 1) // _PAGE_SIZE)


def _page_of(extra: str, total: int) -> int:
    try:
        page = int(extra.split(":")[0])
    except (TypeError, ValueError, IndexError):
        page = 0
    return max(0, min(page, _page_count(total) - 1))


async def _library_page_handler(event, extra: str) -> tuple[str, str, list] | None:
    from backend.db import client as db_client

    owner = get_owner_id()
    rows, total = await db_client.list_emoji_entries(owner, limit=_PAGE_SIZE, offset=0)
    page = _page_of(extra, total)
    if page:
        rows, _total = await db_client.list_emoji_entries(
            owner, limit=_PAGE_SIZE, offset=page * _PAGE_SIZE
        )

    builder = InlinePanelBuilder()
    lines = [f"📚 **Emoji Library** — {total}", ""]
    if not rows:
        lines = [f"📚 **Emoji Library** — {total}", "", "_Empty._"]
    else:
        lines = [f"📚 **Emoji Library** — {total} · page {page + 1}/{_page_count(total)}"]
        for i in range(0, len(rows), 2):
            pair = []
            for j in (i, i + 1):
                if j >= len(rows):
                    break
                pair.append(
                    (_entry_label(rows[j]), f"panel:{_ENTRY_PANEL}:{page}:{j}")
                )
            builder.add_buttons(*pair)
        page_count = _page_count(total)
        if page_count > 1:
            builder.add_buttons(
                ("◀", f"panel:{_LIBRARY_PANEL}:{page - 1}" if page else f"panel:{_LIBRARY_PANEL}"),
                (f"{page + 1}/{page_count}", f"panel:{_LIBRARY_PANEL}:{page}"),
                ("▶", f"panel:{_LIBRARY_PANEL}:{page + 1}"),
            )
    return "Library", "\n".join(lines), builder.build()


async def _entry_page_handler(event, extra: str) -> tuple[str, str, list] | None:
    from backend.db import client as db_client

    owner = get_owner_id()
    try:
        page_s, idx_s = extra.split(":", 1)
        page, idx = int(page_s), int(idx_s)
    except (TypeError, ValueError):
        page, idx = 0, -1
    rows, _total = await db_client.list_emoji_entries(
        owner, limit=_PAGE_SIZE, offset=max(0, page) * _PAGE_SIZE
    )
    row = rows[idx] if 0 <= idx < len(rows) else None
    if row is None:
        return "Entry", "! Entry not found — it may have been removed.", InlinePanelBuilder().build()

    alt = " ".join(str(row.get("alt_text") or "").split()) or "·"
    created = str(row.get("created_at") or "")[:10]
    source_msg = row.get("source_msg_id")
    origin = (
        f"Saved Messages #{source_msg}"
        if isinstance(source_msg, int) and source_msg > 0
        else "Set scan"
    )
    lines = [
        f"{alt}",
        "",
        f"ID: `{row.get('document_id', '?')}`",
        f"Origin: {origin}",
        f"Added: {created or '?'}",
    ]
    return "Entry", "\n".join(lines), InlinePanelBuilder().build()


# ── categories (Phase 2, ROADMAP §10) ───────────────────────────────────────


async def _categories_page_handler(event, extra: str) -> tuple[str, str, list] | None:
    return await _render_categories(get_owner_id(), extra)


async def _render_categories(
    owner: int, extra: str, notice: str | None = None,
) -> tuple[str, str, list]:
    from backend.db import client as db_client

    rows, total, counts = await cat_service.list_categories(
        owner, limit=_PAGE_SIZE, offset=0,
    )
    page = _page_of(extra, total)
    if page:
        rows, _total, counts = await cat_service.list_categories(
            owner, limit=_PAGE_SIZE, offset=page * _PAGE_SIZE,
        )

    builder = InlinePanelBuilder()
    lines: list[str] = []
    if notice:
        lines.append(notice)
        lines.append("")
    if not rows:
        lines.append(f"🗂 **Categories** — {total}")
        lines.append("")
        lines.append("_No categories yet. Create one to start mapping._")
    else:
        page_count = _page_count(total)
        lines.append(
            f"🗂 **Categories** — {total} · page {page + 1}/{page_count}"
        )
        for i in range(0, len(rows), 2):
            pair = []
            for j in (i, i + 1):
                if j >= len(rows):
                    break
                row = rows[j]
                name = _truncate(str(row.get("name") or "?"))
                if cat_service.is_custom_category(row):
                    # the 🧩 marker is rendered from the explicit row TYPE,
                    # never inferred from the name (ROADMAP §26)
                    name = f"🧩 {name}"
                label = f"{name} · {_count_label(counts, row.get('id'))}"
                pair.append((label, f"panel:{_CATEGORY_PANEL}:{page}:{j}"))
            builder.add_buttons(*pair)
        if page_count > 1:
            builder.add_buttons(
                ("◀", f"panel:{_CATEGORIES_PANEL}:{page - 1}" if page else f"panel:{_CATEGORIES_PANEL}"),
                (f"{page + 1}/{page_count}", f"panel:{_CATEGORIES_PANEL}:{page}"),
                ("▶", f"panel:{_CATEGORIES_PANEL}:{page + 1}"),
            )
    builder.add_row("＋ New category", "action:emoji_cat_new")
    builder.add_row("🧩 New custom category", "action:emoji_cat_new_custom")
    return "Categories", "\n".join(lines), builder.build()


async def _category_page_handler(event, extra: str) -> tuple[str, str, list] | None:
    owner = get_owner_id()
    try:
        page_s, idx_s = extra.split(":", 1)
        page, idx = int(page_s), int(idx_s)
    except (TypeError, ValueError):
        page, idx = 0, -1
    rows, _total, counts = await cat_service.list_categories(
        owner, limit=_PAGE_SIZE, offset=max(0, page) * _PAGE_SIZE,
    )
    row = rows[idx] if 0 <= idx < len(rows) else None
    if row is None:
        return _error_panel(
            "Category", "Category not found — it may have been deleted.",
            f"panel:{_CATEGORIES_PANEL}",
        )
    cid = row.get("id")
    count_label = _count_label(counts, cid)
    custom = cat_service.is_custom_category(row)
    sources = cat_service.category_sources(row) if custom else []
    lines = [
        f"{'🧩' if custom else '🗂'} **{row.get('name') or '?'}**",
        "",
        (
            f"{count_label} mapping(s)"
            if counts is not None
            else "_Mapping count unknown — storage read failed._"
        ),
    ]
    if custom:
        lines.append(
            f"Composed snapshot — {len(sources)} source(s), in precedence order."
        )
        lines.append(
            "_Add sources below, then Compose. The snapshot never follows "
            "source edits until you Refresh it._"
            if not sources
            else "_Source edits do not move the snapshot until you Refresh it._"
        )
    builder = InlinePanelBuilder()
    builder.add_row("🗺 Mappings", f"panel:{_MAPPINGS_PANEL}:{cid}:0")
    if custom:
        builder.add_row("🧩 Compose / sources", f"panel:{_COMPOSE_PANEL}:{cid}")
        builder.add_row("🔄 Refresh snapshot", f"action:emoji_refresh:{cid}")
    builder.add_row("✏️ Rename", f"action:emoji_cat_rename:{cid}")
    builder.add_row("🗑 Delete", f"action:emoji_cat_del:{cid}")
    return "Category", "\n".join(lines), builder.build()


async def _cat_new_action(event, extra: str, chat_id: int) -> tuple[str, str, list] | None:
    owner = get_owner_id()
    prompt = (
        "**New Category**\n\nSend the category name as your next message."
    )
    set_pending(
        owner, _MAIN_PANEL, _category_create_input_handler,
        chat_id, prompt,
        inline_chat_id=chat_id or 0, inline_msg_id=0,
    )
    builder = InlinePanelBuilder()
    builder.add_row("← Back", f"panel:{_CATEGORIES_PANEL}")
    return "New Category", "Send the category name as your next message.", builder.build()


async def _category_create_input_handler(
    text, chat_id, msg_id, inline_chat_id, inline_msg_id,
):
    from backend.helper.inline_engine import _owner_id

    owner = _owner_id
    result = await cat_service.create_category(owner, text)
    if result.get("ok"):
        title, body, buttons = await _render_categories(owner, "0")
    else:
        title, body, buttons = _error_panel(
            "New Category",
            _category_error_text(result.get("error"), result.get("category")),
            "action:emoji_cat_new",
        )
    await _edit_inline(inline_chat_id, inline_msg_id, title, body, buttons)
    await _delete_owner_message(chat_id, msg_id)


async def _cat_new_custom_action(event, extra: str, chat_id: int) -> tuple[str, str, list] | None:
    """Create a Custom Category (§26): explicit type metadata, empty until
    the owner composes it from source categories."""
    owner = get_owner_id()
    prompt = (
        "**New Custom Category**\n\nSend the name as your next message.\n\n"
        "The category starts empty — you pick its source categories next."
    )
    set_pending(
        owner, _MAIN_PANEL, _custom_category_create_input_handler,
        chat_id, prompt,
        inline_chat_id=chat_id or 0, inline_msg_id=0,
    )
    builder = InlinePanelBuilder()
    builder.add_row("← Back", f"panel:{_CATEGORIES_PANEL}")
    return (
        "New Custom Category",
        "Send the category name as your next message.",
        builder.build(),
    )


async def _custom_category_create_input_handler(
    text, chat_id, msg_id, inline_chat_id, inline_msg_id,
):
    from backend.helper.inline_engine import _owner_id

    owner = _owner_id
    result = await cat_service.create_category(owner, text, is_custom=True)
    category = result.get("category") or {}
    if result.get("ok") and isinstance(category.get("id"), int):
        _set_draft(owner, kind="compose", category_id=category["id"], source_ids=[])
        title, body, buttons = await _render_compose(
            owner, category["id"],
            notice="✓ Custom category created — add its source categories.",
        )
    else:
        title, body, buttons = _error_panel(
            "New Custom Category",
            _category_error_text(result.get("error"), category),
            "action:emoji_cat_new_custom",
        )
    await _edit_inline(inline_chat_id, inline_msg_id, title, body, buttons)
    await _delete_owner_message(chat_id, msg_id)


def _category_error_text(error: str | None, existing: dict | None = None) -> str:
    if error == cat_service.E_NAME_EXISTS:
        name = str((existing or {}).get("name") or "")
        return (
            f"A category named “{name}” already exists."
            if name else "A category with this name already exists."
        )
    if error == cat_service.E_NAME_TOO_LONG:
        return f"Name too long — up to {cat_service.MAX_CATEGORY_NAME_LEN} characters."
    if error == cat_service.E_INVALID_NAME:
        return "Invalid name — send a non-empty single-line name."
    return "Category could not be saved — storage failed. Nothing was changed."


async def _cat_rename_action(event, extra: str, chat_id: int) -> tuple[str, str, list] | None:
    owner = get_owner_id()
    try:
        cid = int(extra)
    except (TypeError, ValueError):
        return _error_panel(
            "Rename", "Category not found — it may have been deleted.",
            f"panel:{_CATEGORIES_PANEL}",
        )
    category = await cat_service.get_category(owner, cid)
    if category is None:
        return _error_panel(
            "Rename", "Category not found — it may have been deleted.",
            f"panel:{_CATEGORIES_PANEL}",
        )
    _set_draft(owner, kind="rename", category_id=cid, name=category.get("name"))
    prompt = (
        f"**Rename Category**\n\nCurrent name: **{category.get('name')}**\n\n"
        "Send the new name as your next message."
    )
    set_pending(
        owner, _MAIN_PANEL, _category_rename_input_handler,
        chat_id, prompt,
        inline_chat_id=chat_id or 0, inline_msg_id=0,
    )
    builder = InlinePanelBuilder()
    builder.add_row("← Back", f"panel:{_CATEGORIES_PANEL}")
    return "Rename Category", "Send the new name as your next message.", builder.build()


async def _category_rename_input_handler(
    text, chat_id, msg_id, inline_chat_id, inline_msg_id,
):
    from backend.helper.inline_engine import _owner_id

    owner = _owner_id
    draft = _get_draft(owner)
    if not draft or draft.get("kind") != "rename":
        title, body, buttons = _error_panel(
            "Rename", "No category selected — start again from the list.",
            f"panel:{_CATEGORIES_PANEL}",
        )
    else:
        result = await cat_service.rename_category(
            owner, draft.get("category_id"), text,
        )
        _clear_draft(owner)
        if result.get("ok"):
            title, body, buttons = await _render_categories(owner, "0")
        else:
            title, body, buttons = _error_panel(
                "Rename",
                _category_error_text(result.get("error"), result.get("category")),
                f"action:emoji_cat_rename:{draft.get('category_id')}",
            )
    await _edit_inline(inline_chat_id, inline_msg_id, title, body, buttons)
    await _delete_owner_message(chat_id, msg_id)


async def _cat_del_action(event, extra: str, chat_id: int) -> tuple[str, str, list] | None:
    owner = get_owner_id()
    try:
        cid = int(extra)
    except (TypeError, ValueError):
        return _error_panel(
            "Delete Category", "Category not found — it may have been deleted.",
            f"panel:{_CATEGORIES_PANEL}",
        )
    category = await cat_service.get_category(owner, cid)
    if category is None:
        return _error_panel(
            "Delete Category", "Category not found — it may have been deleted.",
            f"panel:{_CATEGORIES_PANEL}",
        )
    count = await cat_service.category_mapping_count(owner, cid)
    count_text = str(count) if count is not None else "unknown number of"
    _set_draft(owner, kind="delete", category_id=cid, name=category.get("name"))
    body = (
        f"⚠️ **Delete category “{category.get('name')}”?**\n\n"
        f"{count_text} mapping(s) will be removed with it.\n"
        "Library entries are NOT deleted."
    )
    if cat_service.is_custom_category(category):
        body += (
            "\n\n_🧩 Composed category: its snapshot mappings are removed; "
            "the source categories themselves are untouched._"
        )
    builder = InlinePanelBuilder()
    builder.add_row("🗑 Delete", "action:emoji_cat_delgo")
    builder.add_row("✗ Cancel", f"panel:{_CATEGORIES_PANEL}:0")
    return "Delete Category", body, builder.build()


async def _cat_delgo_action(event, extra: str, chat_id: int) -> tuple[str, str, list] | None:
    owner = get_owner_id()
    draft = _get_draft(owner)
    if not draft or draft.get("kind") != "delete":
        return _error_panel(
            "Delete Category", "No category selected — start again from the list.",
            f"panel:{_CATEGORIES_PANEL}",
        )
    result = await cat_service.delete_category(owner, draft.get("category_id"))
    _clear_draft(owner)
    if result.get("ok"):
        notice = (
            f"✓ Category deleted — {result.get('removed_mappings', 0)} mapping(s) "
            "removed. Library entries kept."
        )
        return await _render_categories(owner, "0", notice=notice)
    error = result.get("error")
    if error == cat_service.E_NOT_FOUND:
        return _error_panel(
            "Delete Category", "Category not found — it may have been deleted.",
            f"panel:{_CATEGORIES_PANEL}",
        )
    if result.get("category_deleted"):
        notice = (
            "✗ Category row could not be deleted — storage failed. "
            f"{result.get('removed_mappings', 0)} mapping(s) were already removed."
        )
    else:
        notice = "✗ Delete failed — storage error. Nothing was changed."
    return _error_panel("Delete Category", notice, f"panel:{_CATEGORIES_PANEL}")


# ── mappings (Phase 2, ROADMAP §11) ────────────────────────────────────────


async def _mappings_page_handler(event, extra: str) -> tuple[str, str, list] | None:
    """extra: ``:<category_id>:<page>`` — the exact remainder callbacks carry."""
    rest, page_s = extra.rsplit(":", 1)
    cid = int(rest.split(":")[-1])
    try:
        page = int(page_s)
    except (TypeError, ValueError):
        page = 0
    return await _render_mappings(get_owner_id(), cid, page)


async def _render_mappings(
    owner: int, cid: int, page: int, notice: str | None = None,
) -> tuple[str, str, list]:
    category = await cat_service.get_category(owner, cid)
    if category is None:
        return _error_panel(
            "Mappings", "Category not found — it may have been deleted.",
            f"panel:{_CATEGORIES_PANEL}",
        )
    rows, total = await cat_service.list_mappings(
        owner, cid, limit=_PAGE_SIZE, offset=max(0, page) * _PAGE_SIZE,
    )
    page = max(0, min(page, _page_count(total) - 1))
    page_count = _page_count(total)
    wid = intw(cid)
    builder = InlinePanelBuilder()
    custom = cat_service.is_custom_category(category)
    if notice:
        lines: list[str] = [notice, "", f"🗺 **{category.get('name')}** — {total}"]
    elif not rows:
        lines = [
            f"🗺 **{category.get('name')}** — {total}", "",
            "_No mappings yet — compose it from sources._" if custom
            else "_No mappings yet. Add one below._",
        ]
    else:
        lines = [
            f"🗺 **{category.get('name')}** — {total} · page {page + 1}/{page_count}",
            "",
            "_simple → premium:_",
        ]
    if custom:
        lines.extend([
            "",
            "_🧩 Composed category — this set is a snapshot of its sources; "
            "Refresh rebuilds it._",
        ])
    for j, row in enumerate(rows):
        entry = await cat_service._resolve_library_entry(
            owner, row.get("document_id")
        )
        entry_row = entry if isinstance(entry, dict) else None
        simple = str(row.get("simple_emoji") or "·")
        lines.append(
            f"{simple} → {_entry_visual(entry_row)}"
            + ("" if entry_row is not None else " _[entry missing]_")
        )
        builder.add_row(
            f"{simple} → {_truncate(_entry_visual(entry_row))}",
            f"panel:{_MAPPING_PANEL}:{page}:{j}:{wid}",
        )
    if page_count > 1:
        prev_data = (
            f"panel:{_MAPPINGS_PANEL}::{page - 1}:{wid}"
            if page
            else f"panel:{_MAPPINGS_PANEL}::0:{wid}"
        )
        builder.add_buttons(
            ("◀", prev_data),
            (f"{page + 1}/{page_count}", f"panel:{_MAPPINGS_PANEL}::{page}:{wid}"),
            ("▶", f"panel:{_MAPPINGS_PANEL}::{page + 1}:{wid}"),
        )
    if custom:
        builder.add_row("🧩 Compose / sources", f"panel:{_COMPOSE_PANEL}:{cid}")
        builder.add_row("🔄 Refresh snapshot", f"action:emoji_refresh:{cid}")
    else:
        builder.add_row("＋ Add mapping", f"action:emoji_map_new:{wid}")
    return "Mappings", "\n".join(lines), builder.build()


async def _mapping_page_handler(event, extra: str) -> tuple[str, str, list] | None:
    """extra: ``<page>:<idx>:<wid>`` — the exact remainder callbacks carry."""
    page_s, idx_s, wid = extra.split(":", 2)
    page, idx = int(page_s), int(idx_s)
    cid = from_wid(wid)
    rows, _total = await cat_service.list_mappings(
        get_owner_id(), cid, limit=_PAGE_SIZE, offset=max(0, page) * _PAGE_SIZE,
    )
    row = rows[idx] if 0 <= idx < len(rows) else None
    if row is None:
        return _error_panel(
            "Mapping", "Mapping not found — it may have been deleted.",
            f"panel:{_MAPPINGS_PANEL}::0:{wid}",
        )
    entry = await cat_service._resolve_library_entry(
        get_owner_id(), row.get("document_id")
    )
    entry_row = entry if isinstance(entry, dict) else None
    lines = [
        f"{row.get('simple_emoji')} → {_entry_visual(entry_row)}",
        "",
        f"Library entry: #{row.get('document_id', '?')}",
    ]
    if entry_row is None:
        lines.append("_Library entry no longer exists._")
    builder = InlinePanelBuilder()
    category = await cat_service.get_category(get_owner_id(), cid)
    if cat_service.is_custom_category(category):
        lines.append(
            "_🧩 Composed category — manual mapping edits are disabled; "
            "compose its sources and Refresh instead._"
        )
        builder.add_row("🧩 Compose / sources", f"panel:{_COMPOSE_PANEL}:{cid}")
        return "Mapping", "\n".join(lines), builder.build()
    builder.add_row("✏️ Change", f"action:emoji_map_edit:{row.get('simple_emoji')}:{wid}")
    builder.add_row("🗑 Remove", f"action:emoji_map_del:{row.get('simple_emoji')}:{wid}")
    return "Mapping", "\n".join(lines), builder.build()


async def _map_new_action(event, extra: str, chat_id: int) -> tuple[str, str, list] | None:
    owner = get_owner_id()
    try:
        # callbacks carry ``:<page>:<wid>``; the action slot itself is the
        # last segment either way ("123" plain or "" from ":0:123")
        cid = from_wid(extra.rsplit(":", 1)[-1])
    except (TypeError, ValueError):
        return _error_panel(
            "Add Mapping", "Category not found — it may have been deleted.",
            f"panel:{_CATEGORIES_PANEL}",
        )
    category = await cat_service.get_category(owner, cid)
    if category is None:
        return _error_panel(
            "Add Mapping", "Category not found — it may have been deleted.",
            f"panel:{_CATEGORIES_PANEL}",
        )
    if cat_service.is_custom_category(category):
        # Snapshot coherence (§26): a composed category is edited by
        # composing its sources, never by hand-written mappings that a
        # Refresh would silently discard.
        return _error_panel(
            "Add Mapping",
            "This is a composed category — add sources and Compose/Refresh "
            "instead. Manual mapping edits are disabled.",
            f"panel:{_COMPOSE_PANEL}:{cid}",
        )
    _set_draft(owner, kind="map_emoji", category_id=cid)
    prompt = (
        "**Add Mapping — Step 1/2**\n\n"
        f"Category: **{category.get('name')}**\n\n"
        "Send the SIMPLE emoji to be mapped (e.g. 🗑)."
    )
    set_pending(
        owner, _MAIN_PANEL, _map_emoji_input_handler,
        chat_id, prompt,
        inline_chat_id=chat_id or 0, inline_msg_id=0,
    )
    builder = InlinePanelBuilder()
    builder.add_row("← Back", f"panel:{_MAPPINGS_PANEL}::0:{intw(cid)}")
    return "Add Mapping", "Send the simple emoji as your next message.", builder.build()


async def _map_emoji_input_handler(
    text, chat_id, msg_id, inline_chat_id, inline_msg_id,
):
    from backend.helper.inline_engine import _owner_id

    owner = _owner_id
    emoji = cat_service.clean_simple_emoji(text)
    draft = _get_draft(owner) if emoji else None
    if emoji is None or not draft or draft.get("kind") != "map_emoji":
        if draft is not None:
            draft = None
        cid_txt = intw(draft["category_id"]) if draft else ""
        title, body, buttons = _error_panel(
            "Add Mapping", "Invalid emoji — send a single-line emoji/value.",
            (f"panel:{_MAPPINGS_PANEL}::0:{cid_txt}" if cid_txt else f"panel:{_CATEGORIES_PANEL}"),
        )
    else:
        _set_draft(
            owner, kind="map_library", category_id=draft.get("category_id"),
            simple_emoji=emoji,
        )
        key = f"{intw(draft.get('category_id'))}:{emoji}"
        title, body, buttons = await _render_picker(owner, f"{key}:0")
    if not emoji:
        return title, body, buttons
    await _edit_inline(inline_chat_id, inline_msg_id, title, body, buttons)
    await _delete_owner_message(chat_id, msg_id)
    return title, body, buttons


async def _picker_page_handler(event, extra: str) -> tuple[str, str, list] | None:
    return await _render_picker(get_owner_id(), extra)


async def _render_picker(owner: int, extra: str) -> tuple[str, str, list]:
    """Library browser for the mapping editor.
    extra: ``<wid>:<emoji>:<page>`` — the exact remainder callbacks carry."""
    wid, emoji, page_s = extra.split(":", 2)
    try:
        cid = from_wid(wid)
        page = int(page_s)
    except (TypeError, ValueError):
        return _error_panel(
            "Pick Emoji", "Flow out of date — start again.",
            f"panel:{_CATEGORIES_PANEL}",
        )
    from backend.db import client as db_client
    rows, total = await db_client.list_emoji_entries(
        owner, limit=_PAGE_SIZE, offset=max(0, page) * _PAGE_SIZE,
    )
    page = max(0, min(page, _page_count(total) - 1))
    if total == 0:
        return _error_panel(
            "Pick Emoji", "The library is empty — run Import first.",
            f"panel:{_CATEGORIES_PANEL}",
        )
    page_count = _page_count(total)
    builder = InlinePanelBuilder()
    lines = [f"📚 **Pick premium emoji** for {emoji} · page {page + 1}/{page_count}"]
    for i in range(0, len(rows), 2):
        pair = []
        for j in (i, i + 1):
            if j >= len(rows):
                break
            row = rows[j]
            label = f"{_truncate(_entry_label(row), 12)} #{row.get('document_id')}"
            data = f"action:emoji_map_pick:{wid}:{emoji}:{page}:{j}"
            pair.append((label, data))
        builder.add_buttons(*pair)
    if page_count > 1:
        seed = f"{wid}:{emoji}"
        builder.add_buttons(
            ("◀", f"panel:{_PICKER_PANEL}:{seed}:{page - 1}" if page else f"panel:{_PICKER_PANEL}:{seed}:0"),
            (f"{page + 1}/{page_count}", f"panel:{_PICKER_PANEL}:{seed}:{page}"),
            ("▶", f"panel:{_PICKER_PANEL}:{seed}:{page + 1}"),
        )
    return "Pick Emoji", "\n".join(lines), builder.build()


async def _map_pick_action(event, extra: str, chat_id: int) -> tuple[str, str, list] | None:
    """`extra: <wid>:<emoji>:<page>:<idx>`` — the exact remainder callbacks carry."""
    owner = get_owner_id()
    wid, emoji_raw, page_s, idx_s = extra.split(":", 3)
    cid = from_wid(wid)
    page, idx = int(page_s), int(idx_s)
    emoji = cat_service.clean_simple_emoji(emoji_raw)
    draft = _get_draft(owner)
    if (
        emoji is None
        or not draft
        or draft.get("kind") != "map_library"
        or draft.get("category_id") != cid
        or draft.get("simple_emoji") != emoji
    ):
        return _error_panel(
            "Pick Emoji", "Flow out of date — start again.",
            f"panel:{_CATEGORIES_PANEL}",
        )
    from backend.db import client as db_client
    rows, _total = await db_client.list_emoji_entries(
        owner, limit=_PAGE_SIZE, offset=max(0, page) * _PAGE_SIZE,
    )
    picked = rows[idx] if 0 <= idx < len(rows) else None
    if picked is None:
        return _error_panel(
            "Pick Emoji", "Entry not found — it may have been removed.",
            f"panel:{_PICKER_PANEL}:{wid}:{emoji}:{page}",
        )
    result = await cat_service.create_mapping(
        owner, cid, emoji, picked.get("document_id"),
    )
    if result.get("ok"):
        _clear_draft(owner)
        return await _render_mappings(
            owner, cid, 0, notice=f"✓ {emoji} mapped to {_entry_visual(result.get('new_entry'))}",
        )
    if result.get("conflict"):
        _set_draft(
            owner, kind="map_conflict", category_id=cid, simple_emoji=emoji,
            document_id=picked.get("document_id"),
        )
        return _render_conflict(owner, result, wid, emoji)
    if result.get("error") == cat_service.E_LIBRARY_MISSING:
        return _error_panel(
            "Pick Emoji", "Library entry not found — pick again.",
            f"panel:{_PICKER_PANEL}:{wid}:{emoji}:{page}",
        )
    return _error_panel(
        "Add Mapping", "Mapping could not be saved — storage failed.",
        f"panel:{_MAPPINGS_PANEL}::0:{wid}",
    )


def _render_conflict(
    owner: int, result: dict[str, Any], wid: str, emoji: str,
) -> tuple[str, str, list]:
    """Dedicated conflict panel (ROADMAP §12): the ACTUAL current mapping and
    the requested new one, visual side by side, never a silent overwrite."""
    current = result.get("current") or {}
    current_entry = result.get("current_entry")
    new_entry = result.get("new_entry")
    body = (
        "⚠️ **Mapping conflict**\n\n"
        f"{emoji} is already defined in this category:\n"
        f"**Current:** {_entry_visual(current_entry)}\n"
        f"**New:**     {_entry_visual(new_entry)}\n\n"
        "_Nothing has been overwritten._"
    )
    builder = InlinePanelBuilder()
    builder.add_row("🔁 Replace", f"action:emoji_map_repl:{wid}:{emoji}")
    builder.add_row("✗ Cancel", f"panel:{_MAPPINGS_PANEL}::0:{wid}")
    return "Mapping Conflict", body, builder.build()


async def _map_repl_action(event, extra: str, chat_id: int) -> tuple[str, str, list] | None:
    """The explicit confirmation only — the service's replace_mapping is the
    single overwrite path and re-proves the mapping + library entry."""
    owner = get_owner_id()
    wid, emoji_raw = extra.split(":", 1)
    emoji = cat_service.clean_simple_emoji(emoji_raw)
    draft = _get_draft(owner)
    if (
        emoji is None
        or not draft
        or draft.get("kind") != "map_conflict"
        or draft.get("simple_emoji") != emoji
    ):
        return _error_panel(
            "Replace Mapping", "Flow out of date — the mapping is unchanged.",
            f"panel:{_MAPPINGS_PANEL}::0:{wid}",
        )
    result = await cat_service.replace_mapping(
        owner, draft.get("category_id"), emoji, draft.get("document_id"),
    )
    _clear_draft(owner)
    if result.get("ok"):
        return await _render_mappings(
            owner, draft.get("category_id"), 0,
            notice=f"✓ {emoji} now maps to #{draft.get('document_id')}",
        )
    if result.get("error") == cat_service.E_LIBRARY_MISSING:
        return _error_panel(
            "Replace Mapping",
            "Library entry no longer exists — the mapping is unchanged.",
            f"panel:{_MAPPINGS_PANEL}::0:{wid}",
        )
    if result.get("error") == cat_service.E_MAPPING_MISSING:
        return _error_panel(
            "Replace Mapping", "Mapping no longer exists.",
            f"panel:{_MAPPINGS_PANEL}::0:{wid}",
        )
    return _error_panel(
        "Replace Mapping", "Replace failed — storage error. Nothing was changed.",
        f"panel:{_MAPPINGS_PANEL}::0:{wid}",
    )


async def _map_edit_action(event, extra: str, chat_id: int) -> tuple[str, str, list] | None:
    """Change a mapping: same editor flow, seeded from an existing mapping.
    ``extra: <emoji>:<wid>`` — the exact remainder callbacks carry."""
    owner = get_owner_id()
    emoji_raw, wid = extra.rsplit(":", 1)
    emoji = cat_service.clean_simple_emoji(emoji_raw)
    try:
        cid = from_wid(wid)
    except (TypeError, ValueError):
        return _error_panel(
            "Change Mapping", "Category not found — it may have been deleted.",
            f"panel:{_CATEGORIES_PANEL}",
        )
    if emoji is None:
        return _error_panel(
            "Change Mapping", "Invalid emoji key.",
            f"panel:{_MAPPINGS_PANEL}::0:{wid}",
        )
    mapping = await cat_service.get_mapping(owner, cid, emoji)
    if mapping is None:
        return _error_panel(
            "Change Mapping", "Mapping not found — it may have been deleted.",
            f"panel:{_MAPPINGS_PANEL}::0:{wid}",
        )
    category = await cat_service.get_category(owner, cid)
    if cat_service.is_custom_category(category):
        return _error_panel(
            "Change Mapping",
            "This is a composed category — change its sources and Refresh "
            "instead. Manual mapping edits are disabled.",
            f"panel:{_COMPOSE_PANEL}:{cid}",
        )
    _set_draft(
        owner, kind="map_library", category_id=cid, simple_emoji=emoji,
    )
    key = f"{wid}:{emoji}"
    return await _render_picker(owner, f"{key}:0")


async def _map_del_action(event, extra: str, chat_id: int) -> tuple[str, str, list] | None:
    """`extra: <emoji>:<wid>`` — the exact remainder callbacks carry."""
    owner = get_owner_id()
    emoji_raw, wid = extra.rsplit(":", 1)
    emoji = cat_service.clean_simple_emoji(emoji_raw)
    try:
        cid = from_wid(wid)
    except (TypeError, ValueError):
        return _error_panel(
            "Remove Mapping", "Category not found — it may have been deleted.",
            f"panel:{_CATEGORIES_PANEL}",
        )
    category = await cat_service.get_category(owner, cid)
    if cat_service.is_custom_category(category):
        return _error_panel(
            "Remove Mapping",
            "This is a composed category — change its sources and Refresh "
            "instead. Manual mapping edits are disabled.",
            f"panel:{_COMPOSE_PANEL}:{cid}",
        )
    result = await cat_service.delete_mapping(owner, cid, emoji)
    if result.get("ok"):
        return await _render_mappings(
            owner, cid, 0, notice=f"✓ {emoji} removed — the library entry was kept.",
        )
    if result.get("error") == cat_service.E_NOT_FOUND:
        return _error_panel(
            "Remove Mapping", "Mapping not found — it may have been deleted.",
            f"panel:{_MAPPINGS_PANEL}::0:{wid}",
        )
    return _error_panel(
        "Remove Mapping", "Invalid emoji key.", f"panel:{_MAPPINGS_PANEL}::0:{wid}",
    )


# ── composition UI (Phase 5, ROADMAP §26) ─────────────────────────────────


def _compose_draft_sources(owner: int, cid: int) -> list[int] | None:
    """The in-progress source selection for ONE custom category, or None when
    no compose flow is armed (the panel then shows the PERSISTED sources)."""
    draft = _get_draft(owner)
    if not draft or draft.get("kind") != "compose" or draft.get("category_id") != cid:
        return None
    return [sid for sid in (draft.get("source_ids") or []) if isinstance(sid, int)]


async def _source_candidates(
    owner: int, cid: int, page: int,
) -> tuple[list[dict[str, Any]], int, int]:
    """Owner-scoped candidate sources for one page (2×5 grid), newest-first —
    the same ordering the categories panel uses. The composed category itself
    is excluded so it can never be its own source; already-selected sources
    stay VISIBLE (marked) so grid positions never shift under the owner."""
    rows, total, _counts = await cat_service.list_categories(
        owner, limit=_PAGE_SIZE, offset=0,
    )
    page_count = max(1, (total + _PAGE_SIZE - 1) // _PAGE_SIZE)
    page = max(0, min(page, page_count - 1))
    if page:
        rows, _total, _counts = await cat_service.list_categories(
            owner, limit=_PAGE_SIZE, offset=page * _PAGE_SIZE,
        )
    return [row for row in rows if row.get("id") != cid], page, total


async def _render_compose(
    owner: int, cid: int, notice: str | None = None,
) -> tuple[str, str, list]:
    """The composition panel for ONE Custom Category.

    Sources are rendered in PRECEDENCE ORDER — the first source wins every
    shared simple emoji — from the in-progress draft when a flow is armed,
    otherwise from the persisted composition. Nothing here decides a
    composition: the service does, and the snapshot is never live.
    """
    category = await cat_service.get_category(owner, cid) if isinstance(cid, int) and cid > 0 else None
    if category is None or not cat_service.is_custom_category(category):
        return _error_panel(
            "Compose", "Custom category not found — it may have been deleted.",
            f"panel:{_CATEGORIES_PANEL}",
        )
    source_ids = _compose_draft_sources(owner, cid)
    if source_ids is None:
        source_ids = cat_service.category_sources(category)

    names: dict[int, str] = {}
    rows, _total, _counts = await cat_service.list_categories(
        owner, limit=_PAGE_SIZE, offset=0,
    )
    for row in rows:
        names[row.get("id")] = str(row.get("name") or "?")
    for sid in source_ids:
        if sid not in names:
            row = await cat_service.get_category(owner, sid)
            names[sid] = str((row or {}).get("name") or f"#{sid}") if row else f"#{sid} (missing)"

    count = await cat_service.category_mapping_count(owner, cid)
    lines = [
        f"🧩 **{category.get('name') or '?'}**",
        "",
        f"Snapshot: {count if count is not None else '?'} mapping(s)",
        "",
        "_Sources (order = precedence — the first source wins a shared emoji):_",
    ]
    if not source_ids:
        lines.append("_No sources yet._")
    else:
        for i, sid in enumerate(source_ids, start=1):
            lines.append(f"{i}. {names[sid]}")
    if notice:
        lines.append("")
        lines.append(notice)

    builder = InlinePanelBuilder()
    builder.add_row("➕ Add source", f"action:emoji_compose_add:{cid}")
    for sid in source_ids:
        builder.add_row(
            _truncate(f"✖ Remove {names[sid]}"),
            f"action:emoji_compose_rm:{cid}:{sid}",
        )
    builder.add_row("✅ Compose snapshot", f"action:emoji_compose_apply:{cid}")
    builder.add_row("🔄 Refresh from sources", f"action:emoji_refresh:{cid}")
    builder.add_row("🗺 Mappings", f"panel:{_MAPPINGS_PANEL}:{cid}:0")
    builder.add_row("← Back", f"panel:{_CATEGORIES_PANEL}")
    return "Compose", "\n".join(lines), builder.build()


async def _render_source_picker(
    owner: int, cid: int, page: int, notice: str | None = None,
) -> tuple[str, str, list]:
    category = await cat_service.get_category(owner, cid) if isinstance(cid, int) and cid > 0 else None
    if category is None or not cat_service.is_custom_category(category):
        return _error_panel(
            "Add Source", "Custom category not found — it may have been deleted.",
            f"panel:{_CATEGORIES_PANEL}",
        )
    candidates, page, total = await _source_candidates(owner, cid, page)
    selected = _compose_draft_sources(owner, cid)
    if selected is None:
        selected = cat_service.category_sources(category)
    if total <= 1 or not candidates:
        return _error_panel(
            "Add Source", "No other categories exist to use as sources.",
            f"panel:{_COMPOSE_PANEL}:{cid}",
        )

    builder = InlinePanelBuilder()
    lines = [
        f"➕ **Add source** — page {page + 1}/{_page_count(total)}",
        "",
        "_Tap a category to append it as the next source._",
    ]
    if notice:
        lines.extend(["", notice])
    for i in range(0, len(candidates), 2):
        pair = []
        for j in (i, i + 1):
            if j >= len(candidates):
                break
            row = candidates[j]
            name = _truncate(str(row.get("name") or "?"), 18)
            if cat_service.is_custom_category(row):
                name = f"🧩 {name}"
            if row.get("id") in selected:
                name = f"✓ {name}"
            pair.append((name, f"action:emoji_compose_add:{cid}:{page}:{j}"))
        builder.add_buttons(*pair)
    page_count = _page_count(total)
    if page_count > 1:
        builder.add_buttons(
            ("◀", f"panel:{_SOURCE_PICKER_PANEL}:{cid}:{max(0, page - 1)}"),
            (f"{page + 1}/{page_count}", f"panel:{_SOURCE_PICKER_PANEL}:{cid}:{page}"),
            ("▶", f"panel:{_SOURCE_PICKER_PANEL}:{cid}:{page + 1}"),
        )
    builder.add_row("✅ Compose now", f"action:emoji_compose_apply:{cid}")
    builder.add_row("← Back", f"panel:{_COMPOSE_PANEL}:{cid}")
    return "Add Source", "\n".join(lines), builder.build()


def _render_compose_conflicts(
    cid: int, result: dict[str, Any], op: str,
) -> tuple[str, str, list]:
    """Explicit conflict resolution (§26): the reported precedence is the
    owner's source order, and nothing is written until it is confirmed."""
    conflicts = result.get("conflicts") or []
    lines = [
        "⚠️ **Composition conflicts**",
        "",
        "More than one source defines the same simple emoji. Precedence is the "
        "source order — the FIRST source wins:",
        "",
    ]
    for record in conflicts[:8]:
        kept = record.get("kept") or {}
        dropped = record.get("dropped") or []
        lines.append(
            f"{record.get('simple_emoji')} → source `#{kept.get('source_id')}` "
            f"(doc #{kept.get('document_id')}) · dropped "
            + ", ".join(f"`#{d.get('source_id')}`" for d in dropped)
        )
    if len(conflicts) > 8:
        lines.append(f"… and {len(conflicts) - 8} more")
    lines.extend(["", "_Nothing has been written yet._"])

    builder = InlinePanelBuilder()
    builder.add_row(
        "✅ Confirm (first source wins)",
        f"action:emoji_compose_confirm:{op}:{cid}",
    )
    builder.add_row("✗ Cancel", f"panel:{_COMPOSE_PANEL}:{cid}")
    return "Composition Conflicts", "\n".join(lines), builder.build()


async def _render_composition_result(
    owner: int, cid: int, result: dict[str, Any], *, op: str,
) -> tuple[str, str, list]:
    """Render one composition/refresh outcome honestly — success, conflicts,
    or the exact reason nothing changed."""
    if result.get("ok"):
        _clear_draft(owner)
        parts = [
            f"✓ {'Composed' if op == 'compose' else 'Refreshed'} — "
            f"{len(result.get('mappings') or [])} mapping(s) from "
            f"{len(result.get('sources') or [])} source(s)."
        ]
        if result.get("conflicts"):
            parts.append(
                f"{len(result['conflicts'])} conflict(s) resolved by source order."
            )
        if result.get("removed_sources"):
            parts.append(
                "Dropped missing source(s): "
                + ", ".join(f"#{sid}" for sid in result["removed_sources"])
                + "."
            )
        if result.get("empty_sources"):
            parts.append(f"{len(result['empty_sources'])} source(s) had no mappings.")
        if result.get("unresolvable"):
            parts.append(
                f"{result['unresolvable']} mapping(s) reference a missing item "
                "— kept and shown as unavailable."
            )
        return await _render_compose(owner, cid, notice=" ".join(parts))

    error = result.get("error")
    if error == cat_service.E_CONFLICT:
        return _render_compose_conflicts(cid, result, op)
    if error == cat_service.E_MAPPING_LIST_INCOMPLETE:
        return _error_panel(
            "Compose",
            "A source's mappings could not be read completely — nothing was changed.",
            f"panel:{_COMPOSE_PANEL}:{cid}",
        )
    if error == cat_service.E_SNAPSHOT_INCOMPLETE:
        return _error_panel(
            "Compose",
            f"Snapshot updated but {len(result.get('stale') or [])} old mapping(s) "
            "could not be removed — Refresh again before relying on it.",
            f"panel:{_COMPOSE_PANEL}:{cid}",
        )
    if error == cat_service.E_STORAGE:
        return _error_panel(
            "Compose", "Storage failed — the previous snapshot is unchanged.",
            f"panel:{_COMPOSE_PANEL}:{cid}",
        )
    if error == cat_service.E_SOURCE_MISSING:
        missing = ", ".join(f"#{sid}" for sid in (result.get("missing_sources") or []))
        return _error_panel(
            "Compose",
            f"Source category not found ({missing or '?'}) — nothing was changed.",
            f"panel:{_COMPOSE_PANEL}:{cid}",
        )
    if error == cat_service.E_SOURCE_CYCLE:
        return _error_panel(
            "Compose",
            "That would create a circular composition — rejected. Nothing was changed.",
            f"panel:{_COMPOSE_PANEL}:{cid}",
        )
    if error == cat_service.E_SOURCE_SELF:
        return _error_panel(
            "Compose", "A category cannot compose itself — nothing was changed.",
            f"panel:{_COMPOSE_PANEL}:{cid}",
        )
    if error == cat_service.E_SOURCE_DUPLICATE:
        return _error_panel(
            "Compose",
            "The same source category is selected twice — nothing was changed.",
            f"panel:{_COMPOSE_PANEL}:{cid}",
        )
    if error == cat_service.E_TOO_MANY_SOURCES:
        return _error_panel(
            "Compose",
            f"At most {cat_service.MAX_SOURCE_CATEGORIES} source categories.",
            f"panel:{_COMPOSE_PANEL}:{cid}",
        )
    if error == cat_service.E_NO_SOURCES:
        return _error_panel(
            "Compose", "No sources selected — add at least one source first.",
            f"panel:{_COMPOSE_PANEL}:{cid}",
        )
    if error == cat_service.E_NOT_CUSTOM:
        return _error_panel(
            "Compose", "This category is not a composed (custom) category.",
            f"panel:{_CATEGORIES_PANEL}",
        )
    if error == cat_service.E_NOT_FOUND:
        return _error_panel(
            "Compose", "Custom category not found — it may have been deleted.",
            f"panel:{_CATEGORIES_PANEL}",
        )
    return _error_panel(
        "Compose", "Composition failed — nothing was changed.",
        f"panel:{_COMPOSE_PANEL}:{cid}",
    )


async def _compose_panel_handler(event, extra: str) -> tuple[str, str, list] | None:
    owner = get_owner_id()
    if not owner:
        return _error_panel("Compose", "Owner is not set.", f"panel:{_MAIN_PANEL}")
    try:
        cid = int(extra.split(":")[0])
    except (TypeError, ValueError, IndexError):
        cid = 0
    return await _render_compose(owner, cid)


async def _source_picker_handler(event, extra: str) -> tuple[str, str, list] | None:
    owner = get_owner_id()
    if not owner:
        return _error_panel("Add Source", "Owner is not set.", f"panel:{_MAIN_PANEL}")
    cid_s, _, page_s = extra.partition(":")
    try:
        cid = int(cid_s)
        page = max(0, int(page_s or 0))
    except (TypeError, ValueError):
        return _error_panel(
            "Add Source", "Flow out of date — start again.", f"panel:{_CATEGORIES_PANEL}"
        )
    return await _render_source_picker(owner, cid, page)


async def _compose_add_action(event, extra: str, chat_id: int):
    """`extra: <wid>` opens the picker; ``<wid>:<page>:<idx>`` appends ONE
    owner-scoped source. Every id is re-validated against the owner — the
    callback payload is never authorization."""
    owner = get_owner_id()
    parts = str(extra).split(":")
    try:
        cid = int(parts[0])
    except (TypeError, ValueError, IndexError):
        return _error_panel(
            "Add Source", "Flow out of date — start again.", f"panel:{_CATEGORIES_PANEL}"
        )
    category = await cat_service.get_category(owner, cid)
    if category is None or not cat_service.is_custom_category(category):
        return _error_panel(
            "Add Source", "Custom category not found — it may have been deleted.",
            f"panel:{_CATEGORIES_PANEL}",
        )
    if len(parts) == 1:
        if _compose_draft_sources(owner, cid) is None:
            _set_draft(owner, kind="compose", category_id=cid, source_ids=[])
        return await _render_source_picker(owner, cid, 0)
    try:
        page, idx = int(parts[1]), int(parts[2])
    except (TypeError, ValueError, IndexError):
        return _error_panel(
            "Add Source", "Flow out of date — start again.",
            f"panel:{_COMPOSE_PANEL}:{cid}",
        )
    candidates, page, _total = await _source_candidates(owner, cid, page)
    picked = candidates[idx] if 0 <= idx < len(candidates) else None
    if picked is None:
        return _error_panel(
            "Add Source", "List changed — pick again.",
            f"panel:{_SOURCE_PICKER_PANEL}:{cid}:{page}",
        )
    selected = _compose_draft_sources(owner, cid)
    if selected is None:
        # no flow armed yet: start from the persisted composition
        selected = cat_service.category_sources(category)
    source_id = picked.get("id")
    name = _truncate(str(picked.get("name") or "?"))
    if source_id in selected:
        return await _render_source_picker(
            owner, cid, page,
            notice=f"“{name}” is already a source (it keeps its precedence position).",
        )
    if len(selected) >= cat_service.MAX_SOURCE_CATEGORIES:
        return await _render_source_picker(
            owner, cid, page,
            notice=f"At most {cat_service.MAX_SOURCE_CATEGORIES} sources.",
        )
    selected.append(source_id)
    _set_draft(owner, kind="compose", category_id=cid, source_ids=selected)
    return await _render_compose(
        owner, cid, notice=f"✓ Added “{name}” as source #{len(selected)}.",
    )


async def _compose_rm_action(event, extra: str, chat_id: int):
    """`extra: <wid>:<source_id>`` — remove one source from the draft."""
    owner = get_owner_id()
    wid, _, sid_s = str(extra).partition(":")
    try:
        cid, source_id = int(wid), int(sid_s)
    except (TypeError, ValueError):
        return _error_panel(
            "Compose", "Flow out of date — start again.", f"panel:{_CATEGORIES_PANEL}"
        )
    category = await cat_service.get_category(owner, cid)
    if category is None or not cat_service.is_custom_category(category):
        return _error_panel(
            "Compose", "Custom category not found — it may have been deleted.",
            f"panel:{_CATEGORIES_PANEL}",
        )
    selected = _compose_draft_sources(owner, cid)
    if selected is None:
        selected = cat_service.category_sources(category)
    if source_id not in selected:
        return await _render_compose(
            owner, cid, notice="Source not in this composition — nothing changed.",
        )
    selected = [sid for sid in selected if sid != source_id]
    _set_draft(owner, kind="compose", category_id=cid, source_ids=selected)
    return await _render_compose(
        owner, cid, notice=f"✖ Source `#{source_id}` removed from the selection.",
    )


async def _compose_apply_action(event, extra: str, chat_id: int):
    """Run the composition from the armed draft — the service decides; the
    UI only renders its report."""
    owner = get_owner_id()
    try:
        cid = int(str(extra).split(":")[0])
    except (TypeError, ValueError, IndexError):
        return _error_panel(
            "Compose", "Flow out of date — start again.", f"panel:{_CATEGORIES_PANEL}"
        )
    selected = _compose_draft_sources(owner, cid)
    if selected is None:
        return _error_panel(
            "Compose", "Flow out of date — open the category again.",
            f"panel:{_COMPOSE_PANEL}:{cid}",
        )
    result = await cat_service.compose_category(owner, cid, selected)
    return await _render_composition_result(owner, cid, result, op="compose")


async def _refresh_action(event, extra: str, chat_id: int):
    """Explicit Refresh: rebuild the snapshot from the PERSISTED sources."""
    owner = get_owner_id()
    try:
        cid = int(str(extra).split(":")[0])
    except (TypeError, ValueError, IndexError):
        return _error_panel(
            "Refresh", "Flow out of date — start again.", f"panel:{_CATEGORIES_PANEL}"
        )
    result = await cat_service.refresh_category(owner, cid)
    return await _render_composition_result(owner, cid, result, op="refresh")


async def _compose_confirm_action(event, extra: str, chat_id: int):
    """The ONLY path that turns reported conflicts into stored precedence."""
    owner = get_owner_id()
    op, _, wid = str(extra).partition(":")
    try:
        cid = int(wid)
    except (TypeError, ValueError):
        return _error_panel(
            "Compose", "Flow out of date — start again.", f"panel:{_CATEGORIES_PANEL}"
        )
    if op == "refresh":
        result = await cat_service.refresh_category(owner, cid, confirm_conflicts=True)
    elif op == "compose":
        selected = _compose_draft_sources(owner, cid)
        if selected is None:
            return _error_panel(
                "Compose", "Flow out of date — open the category again.",
                f"panel:{_COMPOSE_PANEL}:{cid}",
            )
        result = await cat_service.compose_category(
            owner, cid, selected, confirm_conflicts=True,
        )
    else:
        return _error_panel(
            "Compose", "Flow out of date — start again.", f"panel:{_CATEGORIES_PANEL}"
        )
    return await _render_composition_result(owner, cid, result, op=op)


# ── registration ──────────────────────────────────────────────────────────


# ── replacement state (Phase 3, ROADMAP §13/§14) ───────────────────────────


def _category_name_of(rows: list[dict[str, Any]], category_id: int | None) -> str | None:
    if category_id is None:
        return None
    for row in rows:
        if row.get("id") == category_id:
            return str(row.get("name") or f"#{category_id}")
    return None


async def _render_replacement(
    owner: int, extra: str = "", notice: str | None = None,
) -> tuple[str, str, list]:
    """The Replacement panel: toggle state, global default, the per-chat
    override for the CURRENT target chat (when one is armed), the effective
    resolved category for that chat, and the owner actions.

    ``extra`` carries ``<pick_scope>:<chat_id>`` while a category-picker flow
    is armed (pick_scope: ``global`` | ``override``); ``chat_id`` is 0 when
    no target chat is armed.
    """
    from backend.helper.target_context import get_target

    pick_scope, _, chat_s = extra.partition(":")
    try:
        target_chat = int(chat_s) if chat_s else 0
    except (TypeError, ValueError):
        target_chat = 0

    enabled = await state_service.replacement_enabled(owner)
    default_id = await state_service.get_global_default_category(owner)

    rows, _total, _counts = await cat_service.list_categories(
        owner, limit=_PAGE_SIZE, offset=0,
    )

    # Target chat: only a REAL armed target-context chat — never fabricated.
    if not target_chat:
        ctx = get_target(owner)
        if ctx is not None and ctx.kind == "reply" and ctx.reply_chat_id:
            target_chat = ctx.reply_chat_id

    lines = [f"Replacement: {'✅ ON' if enabled else '❌ OFF'}", ""]

    default_name = _category_name_of(rows, default_id)
    lines.append(
        f"Global default: {default_name if default_name else '_none_'}"
    )

    if target_chat:
        override_id = await state_service.get_chat_override(owner, target_chat)
        override_name = _category_name_of(rows, override_id)
        lines.append(
            f"Override for chat `{target_chat}`: "
            f"{override_name if override_name else '_none_'}"
        )
        effective_id = await state_service.resolve_effective_category(
            owner, target_chat,
        )
        effective_name = _category_name_of(rows, effective_id)
        if not enabled:
            effective_text = "_none — replacement is OFF_"
        elif effective_name:
            effective_text = effective_name
        else:
            effective_text = "_none — no category resolves_"
        lines.append(f"Effective here: {effective_text}")
    else:
        lines.append("Override: _no target chat — reply to a message in the "
                     "chat you want to target, then open this panel._")

    if notice:
        lines.append("")
        lines.append(notice)

    builder = InlinePanelBuilder()
    builder.add_row(
        "🔘 Turn OFF" if enabled else "🔘 Turn ON",
        "action:emoji_state_toggle",
    )
    builder.add_row("🌐 Set global default", "action:emoji_state_global_pick")
    if target_chat:
        seed = f"override:{target_chat}"
        builder.add_row(
            "📍 Set chat override", f"action:emoji_state_override_pick:{target_chat}"
        )
        builder.add_row("✖ Clear chat override", f"action:emoji_state_clear:{target_chat}")
    builder.add_row("🗂 Manage categories", f"panel:{_CATEGORIES_PANEL}")
    builder.add_row("← Back", f"panel:{_MAIN_PANEL}")
    return "Emoji Replacement", "\n".join(lines), builder.build()


async def _replacement_panel_handler(event, extra: str) -> tuple[str, str, list] | None:
    owner = get_owner_id()
    if not owner:
        return _error_panel("Emoji Replacement", "Owner is not set.", f"panel:{_MAIN_PANEL}")
    return await _render_replacement(owner, extra or "")


async def _state_global_pick_action(event, extra: str, chat_id: int):
    owner = get_owner_id()
    _set_draft(owner, kind="state_pick", scope="global", target_chat=0)
    return await _render_state_picker(owner, "global:0:0")


async def _state_override_pick_action(event, extra: str, chat_id: int):
    owner = get_owner_id()
    try:
        target_chat = int(extra)
    except (TypeError, ValueError):
        return _error_panel(
            "Set Override", "No target chat — reply to a message in the chat "
            "you want to target first.",
            f"panel:{_REPLACEMENT_PANEL}",
        )
    _set_draft(owner, kind="state_pick", scope="override", target_chat=target_chat)
    return await _render_state_picker(owner, f"override:{target_chat}:0")


async def _render_state_picker(owner: int, extra: str) -> tuple[str, str, list]:
    """2×5 category picker for the replacement state. extra: ``<scope>:<chat>:<page>``."""
    scope, _, rest = extra.partition(":")
    chat_s, _, page_s = rest.partition(":")
    try:
        target_chat = int(chat_s)
        page = max(0, int(page_s or 0))
    except (TypeError, ValueError):
        return _error_panel(
            "Pick Category", "Flow out of date — start again.",
            f"panel:{_REPLACEMENT_PANEL}",
        )
    if scope not in ("global", "override"):
        return _error_panel(
            "Pick Category", "Flow out of date — start again.",
            f"panel:{_REPLACEMENT_PANEL}",
        )
    rows, total, _counts = await cat_service.list_categories(
        owner, limit=_PAGE_SIZE, offset=max(0, page) * _PAGE_SIZE,
    )
    if total == 0:
        return _error_panel(
            "Pick Category", "No categories exist yet — create one first.",
            f"panel:{_REPLACEMENT_PANEL}",
        )
    page = min(page, _page_count(total) - 1)
    if page:
        rows, _total, _counts = await cat_service.list_categories(
            owner, limit=_PAGE_SIZE, offset=page * _PAGE_SIZE,
        )

    title = "Set Global Default" if scope == "global" else "Set Chat Override"
    builder = InlinePanelBuilder()
    for i in range(0, len(rows), 2):
        pair = []
        for j in (i, i + 1):
            if j >= len(rows):
                break
            row = rows[j]
            pair.append(
                (
                    _truncate(str(row.get("name") or "?")),
                    f"action:emoji_state_choose:{scope}:{target_chat}:{row.get('id')}",
                )
            )
        builder.add_buttons(*pair)
    page_count = _page_count(total)
    if page_count > 1:
        seed = f"{scope}:{target_chat}"
        builder.add_buttons(
            ("◀", f"panel:{_STATE_SCOPE_PICKER_PANEL}:{seed}:{page - 1}" if page else f"panel:{_STATE_SCOPE_PICKER_PANEL}:{seed}:0"),
            (f"{page + 1}/{page_count}", f"panel:{_STATE_SCOPE_PICKER_PANEL}:{seed}:{page}"),
            ("▶", f"panel:{_STATE_SCOPE_PICKER_PANEL}:{seed}:{page + 1}"),
        )
    builder.add_row("← Back", f"panel:{_REPLACEMENT_PANEL}")
    return title, "Pick the category to activate:", builder.build()


async def _state_scope_picker_handler(event, extra: str) -> tuple[str, str, list] | None:
    owner = get_owner_id()
    if not owner:
        return _error_panel("Pick Category", "Owner is not set.", f"panel:{_MAIN_PANEL}")
    return await _render_state_picker(owner, extra or "")


async def _state_choose_action(event, extra: str, chat_id: int):
    """extra: ``<scope>:<target_chat>:<category_id>`` — validate through the
    service (owner-scoped, fail closed) and re-render the state panel."""
    owner = get_owner_id()
    scope, _, rest = extra.partition(":")
    chat_s, _, cid_s = rest.partition(":")
    try:
        target_chat = int(chat_s)
        cid = int(cid_s)
    except (TypeError, ValueError):
        return _error_panel(
            "Pick Category", "Flow out of date — start again.",
            f"panel:{_REPLACEMENT_PANEL}",
        )
    if scope == "global":
        ok = await state_service.set_global_default_category(owner, cid)
        notice = (
            "✓ Global default set."
            if ok
            else "✗ Category not found (or storage failed) — nothing changed."
        )
    elif scope == "override":
        if not target_chat:
            return _error_panel(
                "Set Override", "No target chat — start again.",
                f"panel:{_REPLACEMENT_PANEL}",
            )
        ok = await state_service.set_chat_override(owner, target_chat, cid)
        notice = (
            f"✓ Override set for chat `{target_chat}`."
            if ok
            else "✗ Category not found (or storage failed) — nothing changed."
        )
    else:
        return _error_panel(
            "Pick Category", "Flow out of date — start again.",
            f"panel:{_REPLACEMENT_PANEL}",
        )
    _clear_draft(owner)
    return await _render_replacement(owner, "", notice)


async def _state_toggle_action(event, extra: str, chat_id: int):
    owner = get_owner_id()
    new_value = await state_service.toggle_replacement(owner)
    if new_value is False and await state_service.replacement_enabled(owner) is False:
        # distinguish a genuine OFF result from a degraded write
        enabled_now = await state_service.replacement_enabled(owner)
        if enabled_now:
            notice = "✗ Could not save the toggle — storage failed. Still OFF."
        else:
            notice = "🔘 Replacement is OFF."
    else:
        notice = f"✅ Replacement is ON." if new_value else "🔘 Replacement is OFF."
    return await _render_replacement(owner, "", notice)


async def _state_clear_action(event, extra: str, chat_id: int):
    owner = get_owner_id()
    try:
        target_chat = int(extra)
    except (TypeError, ValueError):
        return _error_panel(
            "Clear Override", "No target chat — start again.",
            f"panel:{_REPLACEMENT_PANEL}",
        )
    ok = await state_service.clear_chat_override(owner, target_chat)
    notice = (
        f"✓ Override cleared for chat `{target_chat}` — the global default "
        "applies again."
        if ok
        else "✗ Could not clear the override — storage failed. Nothing changed."
    )
    return await _render_replacement(owner, "", notice)


# ── reaction (Phase 6, ROADMAP §27) ───────────────────────────────────────
#
# ONE deterministic flow, built only from the existing machinery: the panel
# arms the SAME pending-input reply mode Deep Save uses, the owner replies to
# the exact message they want to react to, and the reply's own content is the
# reaction value — a plain emoji (``ReactionEmoji``) or a premium emoji, which
# Telegram sends as a custom-emoji entity (``ReactionCustomEmoji``). Nothing is
# inferred: no "last message", no chat history, no keyword routing. The
# reaction is applied by the reaction SERVICE on the self client; this UI only
# arms the reply flow and renders the service's honest result — it owns no
# message rewriting, no delivery and no deletion.

#: Two bounded RPCs (target resolution + the reaction itself) run inside this
#: handler; the wrapper bounds each at 30s, so this is a backstop, not the
#: primary bound.
_REACT_HANDLER_TIMEOUT_S = 90.0

_REACT_PROMPT = (
    "Reply to the message you want to react to,\n"
    "sending ONLY the emoji to react with.\n"
    "A premium emoji from your library works too."
)


def _reaction_from_reply(reply_msg, text: str) -> tuple[dict | None, str]:
    """The reaction the owner's reply carries, or the reason it carries none.

    A custom-emoji entity (the premium emoji the owner actually sent) wins
    over the fallback glyph — the same one representation Telegram delivered,
    never a substitution. Otherwise the reply's own (stripped) text IS the
    reaction value; a missing value is reported, never guessed.
    """
    from telethon.tl.types import MessageEntityCustomEmoji

    for entity in getattr(reply_msg, "entities", None) or []:
        if isinstance(entity, MessageEntityCustomEmoji):
            document_id = getattr(entity, "document_id", None)
            if isinstance(document_id, int) and not isinstance(document_id, bool) and document_id > 0:
                return {"kind": "custom_emoji", "document_id": document_id}, ""
    value = (text or getattr(reply_msg, "message", None) or "").strip()
    if not value:
        return None, "Reply with the emoji you want to react with."
    return {"kind": "emoji", "emoji": value}, ""


def _render_reaction_result(result: dict, target_id: int) -> tuple[str, str, list]:
    """Honest outcome panel: the applied reaction, or the exact failure."""
    builder = InlinePanelBuilder()
    builder.add_row("💬 React to another message", "action:emoji_react")
    if result.get("ok"):
        label = reaction_service.reaction_label(result.get("reaction"))
        return "React", f"✓ Reacted with {label}\n\nto message `#{target_id}`", builder.build()
    detail = result.get("detail") or "the reaction was not applied"
    code = result.get("error") or "?"
    return "React", f"✗ {detail}\n\n`{code}` · message `#{target_id}`", builder.build()


async def _react_action(event, extra: str, chat_id: int) -> tuple[str, str, list] | None:
    """Arm reply mode: the owner's next reply in this chat is the reaction."""
    if get_self_client() is None:
        return "React", "! Self client is not connected.", InlinePanelBuilder().build()
    if not get_owner_id():
        return "React", "! Owner is not set.", InlinePanelBuilder().build()
    if not chat_id:
        return "React", "! Could not determine the current chat. Please try again.", InlinePanelBuilder().build()

    set_pending(
        get_owner_id(), "emoji_react", _react_reply_wait_handler,
        chat_id, _REACT_PROMPT,
        inline_chat_id=chat_id,
        inline_msg_id=getattr(event, "message_id", 0) or 0,
        extra="",
        timeout=_REACT_HANDLER_TIMEOUT_S,
    )
    return "React", _REACT_PROMPT, []


async def _react_reply_wait_handler(
    text, chat_id, msg_id, inline_chat_id, inline_msg_id,
) -> None:
    """Apply the reply's reaction to the reply's own reply-target.

    Deterministic target resolution, mirroring Deep Save's reply mode: the
    owner's reply message is read back, its ``reply_to_msg_id`` is the target
    — never the owner's reply itself — and a cross-chat reply header is
    refused rather than guessed at.
    """
    client = get_self_client()
    owner = get_owner_id()

    async def _finish(title: str, body: str, buttons: list) -> None:
        await _edit_inline(inline_chat_id, inline_msg_id, title, body, buttons)

    if client is None:
        await _finish("React", "! Self client is not connected.", [])
        return

    try:
        reply_msg = await client.get_messages(chat_id, ids=msg_id)
    except Exception as exc:
        logger.warning("[EMOJI_UI] react: cannot read the reply: %s", exc)
        reply_msg = None
    if reply_msg is None:
        await _finish("React", "! Could not read your reply message — please try again.", [])
        return

    header = getattr(reply_msg, "reply_to", None)
    if getattr(header, "reply_to_peer_id", None) is not None:
        await _finish(
            "React",
            "! That reply targets another chat.\nReply in the same chat as the message.",
            [],
        )
        return
    target_id = getattr(reply_msg, "reply_to_msg_id", None) or getattr(header, "reply_to_msg_id", None)
    if not isinstance(target_id, int) or isinstance(target_id, bool) or target_id <= 0:
        await _finish(
            "React",
            "! Your message was not a reply.\nReply TO the message you want to react to.",
            [],
        )
        return

    reaction, reason = _reaction_from_reply(reply_msg, text)
    if reaction is None:
        await _finish("React", f"! {reason}", [])
        return

    result = await reaction_service.react_to_message(
        client, owner, chat_id, target_id, reaction,
    )
    title, body, buttons = _render_reaction_result(result, target_id)
    await _finish(title, body, buttons)


# ── shared edit helpers (save.py input-handler convention) ─────────────────


async def _edit_inline(
    inline_chat_id, inline_msg_id, title: str, body: str, buttons: list,
) -> None:
    from backend.helper.client import get_client
    from backend.helper.panel_render import render_edit

    helper = get_client()
    if helper and inline_chat_id and inline_msg_id:
        try:
            text, built = render_edit("", f"**{title}**\n\n{body}", buttons)
            await helper.edit_message(
                inline_chat_id, inline_msg_id, text, buttons=built,
            )
        except Exception as exc:
            logger.warning("[EMOJI_UI] inline edit failed: %s", exc)


async def _delete_owner_message(chat_id, msg_id) -> None:
    from backend.helper.inline_engine import get_self_client as _gsc

    client = _gsc()
    if client:
        try:
            await client.delete_messages(chat_id, [msg_id])
        except Exception:
            pass


def intw(value: Any) -> str:
    """Category-id callback width token."""
    try:
        return str(int(value))
    except (TypeError, ValueError):
        return "0"


def from_wid(wid: str) -> int:
    return int(wid)


# ── premium-emoji probe (POC: the helper bot renders the custom emoji) ────
#
# ONE bounded experiment proving the capability the Emoji & Reaction design
# rests on and that has never been live-verified: a REAL Telegram Premium
# custom emoji, picked by the owner as a REPLY to a deterministic Saved
# Messages selection message, is read from its ENTITY and handed to the
# EXISTING helper bot, which sends its OWN message carrying that entity. The
# self client never renders the premium emoji itself, and the visible Unicode
# glyph is never treated as the emoji.
#
# Nothing else changes here: no library scan, no pagination change, no
# category/mapping change, no state, no schema, no new listener, no second
# client and no AI. The reply is accepted only when it targets the EXACT
# selection message recorded for this interaction.

#: The ONE action that launches the probe.
_PROBE_ACTION = "emoji_react_premium"

#: Plain words only — the selection message is sent by the owner's own
#: account, so it carries no glyph a category mapping could ever key on.
_PROBE_PROMPT = (
    "Reply to this message with the Premium Emoji you want to use.\n"
    "Only a reply to THIS message is accepted."
)

#: Two bounded calls (reading the reply back + the helper bot's send) run
#: inside this handler; the wrappers bound each, so this is a backstop.
_PROBE_TIMEOUT_S = 90.0


def _probe_buttons() -> list:
    builder = InlinePanelBuilder()
    builder.add_row("💬 Set Reaction Emoji", f"action:{_PROBE_ACTION}")
    builder.add_row("💬 React to a message", "action:emoji_react")
    return builder.build()


async def _react_premium_action(event, extra: str, chat_id: int) -> tuple[str, str, list]:
    """Send the selection message to Saved Messages and arm reply mode."""
    client = get_self_client()
    owner = get_owner_id()
    if client is None:
        return _error_panel("Set Reaction Emoji", "Self client is not connected.")
    if not owner:
        return _error_panel("Set Reaction Emoji", "Owner is not set.")

    try:
        selection = await client.send_message("me", _PROBE_PROMPT)
    except Exception as exc:
        logger.warning("[EMOJI_UI] probe: selection message not created: %s", exc)
        return _error_panel(
            "Set Reaction Emoji",
            f"Could not create the Saved Messages selection message: {exc}",
        )

    selection_id = getattr(selection, "id", 0) or 0
    selection_chat = getattr(selection, "chat_id", 0) or 0
    if (
        not isinstance(selection_id, int)
        or isinstance(selection_id, bool)
        or selection_id <= 0
        or not selection_chat
    ):
        return _error_panel(
            "Set Reaction Emoji",
            "The selection message could not be created — nothing was armed.",
        )

    set_pending(
        owner, _PROBE_ACTION, _react_premium_reply_handler, selection_chat,
        _PROBE_PROMPT,
        inline_chat_id=chat_id,
        inline_msg_id=getattr(event, "message_id", 0) or 0,
        extra=str(selection_id),
        timeout=_PROBE_TIMEOUT_S,
    )
    logger.info(
        "[EMOJI_UI] probe: selection message #%s sent to Saved Messages",
        selection_id,
    )
    body = (
        "A selection message was sent to **Saved Messages**.\n\n"
        "Reply to THAT message with the Premium Emoji you want to use.\n"
        f"Only a reply to message `#{selection_id}` is accepted.\n\n"
        "The helper bot then displays the emoji it received."
    )
    return "Set Reaction Emoji", body, _probe_buttons()


async def _react_premium_reply_handler(
    text,
    chat_id,
    msg_id,
    inline_chat_id,
    inline_msg_id,
    extra: str = "",
) -> None:
    """Resolve the EXACT selection-message reply and prove the render boundary."""
    client = get_self_client()
    owner = get_owner_id()

    async def _finish(title: str, body: str, buttons: list) -> None:
        await _edit_inline(inline_chat_id, inline_msg_id, title, body, buttons)

    selection_id = int(extra) if isinstance(extra, str) and extra.isdigit() else 0
    if client is None:
        await _finish("Set Reaction Emoji", "! Self client is not connected.", [])
        return
    if not selection_id:
        await _finish(
            "Set Reaction Emoji",
            "! The selection message is no longer known — start the action again.",
            _probe_buttons(),
        )
        return

    try:
        reply = await client.get_messages(chat_id, ids=msg_id)
    except Exception as exc:
        logger.warning("[EMOJI_UI] probe: cannot read the reply: %s", exc)
        reply = None
    if reply is None:
        await _finish(
            "Set Reaction Emoji",
            "! Could not read your reply message — please try again.",
            _probe_buttons(),
        )
        return

    header = getattr(reply, "reply_to", None)
    if getattr(header, "reply_to_peer_id", None) is not None:
        await _finish(
            "Set Reaction Emoji",
            "! That reply targets another chat.\nReply in Saved Messages.",
            _probe_buttons(),
        )
        return

    target_id = getattr(reply, "reply_to_msg_id", None) or getattr(
        header, "reply_to_msg_id", None
    )
    if not isinstance(target_id, int) or isinstance(target_id, bool) or target_id <= 0:
        await _finish(
            "Set Reaction Emoji",
            f"! Your message was not a reply.\nReply TO selection message `#{selection_id}`.",
            _probe_buttons(),
        )
        return
    if target_id != selection_id:
        await _finish(
            "Set Reaction Emoji",
            f"! That reply targets message `#{target_id}`, not the selection "
            f"message `#{selection_id}`.",
            _probe_buttons(),
        )
        return

    found = probe_service.inspect_message(reply)
    if found["kind"] != probe_service.KIND_CUSTOM_EMOJI:
        await _finish(
            "Set Reaction Emoji",
            f"! {found['detail']}.\n\n"
            f"Diagnosis `{probe_service.SOURCE_ENTITY_MISSING}` — nothing was "
            "sent to the helper bot.",
            _probe_buttons(),
        )
        return

    outcome = await probe_service.deliver_proof(
        client, owner, found["document_id"], found["alt_text"]
    )
    entity = outcome.get("entity") or {}
    if outcome.get("ok"):
        body = (
            f"✓ Telegram accepted the helper bot's message "
            f"`#{outcome.get('message_id')}` carrying a REAL custom-emoji "
            f"entity for document `#{entity.get('document_id')}` "
            f"(offset {entity.get('offset')}, length {entity.get('length')}).\n\n"
            "Read-back (the helper bot's own session): "
            f"{probe_service.readback_summary(outcome.get('readback'))}\n\n"
            f"Diagnosis `{outcome.get('diagnosis')}`.\n\n"
            "That message shows the Premium emoji only if the helper bot may "
            "use custom-emoji entities (Fragment-purchased username). A plain "
            "glyph there is a failure, not a success."
        )
        await _finish("Set Reaction Emoji", body, _probe_buttons())
        return

    body = (
        f"✗ {outcome.get('detail')}\n\n"
        f"`{outcome.get('error')}` · the custom-emoji entity "
        f"(document `#{found['document_id']}`) was NOT rendered by the helper bot.\n\n"
        f"Diagnosis `{outcome.get('diagnosis')}`."
    )
    await _finish("Set Reaction Emoji", body, _probe_buttons())


# ── registration ──────────────────────────────────────────────────────────


def register(client, owner_id: int) -> None:
    register_panel(_MAIN_PANEL, _emoji_panel_handler, parent="menu", title="Emoji")
    register_panel(
        _LIBRARY_PANEL, _library_page_handler, parent=_MAIN_PANEL, title="Library"
    )
    register_panel(
        _ENTRY_PANEL, _entry_page_handler, parent=_LIBRARY_PANEL, title="Entry"
    )
    register_panel(
        _CATEGORIES_PANEL, _categories_page_handler, parent=_MAIN_PANEL, title="Categories"
    )
    register_panel(
        _CATEGORY_PANEL, _category_page_handler, parent=_CATEGORIES_PANEL, title="Category"
    )
    register_panel(
        _MAPPINGS_PANEL, _mappings_page_handler, parent=_CATEGORY_PANEL, title="Mappings"
    )
    register_panel(
        _MAPPING_PANEL, _mapping_page_handler, parent=_MAPPINGS_PANEL, title="Mapping"
    )
    register_panel(
        _PICKER_PANEL, _picker_page_handler, parent=_MAPPINGS_PANEL, title="Pick Emoji"
    )
    register_panel(
        _REPLACEMENT_PANEL, _replacement_panel_handler,
        parent=_MAIN_PANEL, title="Replacement",
    )
    register_panel(
        _STATE_SCOPE_PICKER_PANEL, _state_scope_picker_handler,
        parent=_REPLACEMENT_PANEL, title="Pick Category",
    )
    register_panel(
        _COMPOSE_PANEL, _compose_panel_handler,
        parent=_CATEGORY_PANEL, title="Compose",
    )
    register_panel(
        _SOURCE_PICKER_PANEL, _source_picker_handler,
        parent=_COMPOSE_PANEL, title="Add Source",
    )
    register_action("emoji_import", _import_action)
    register_action("emoji_react", _react_action)
    register_action(_PROBE_ACTION, _react_premium_action)
    register_action("emoji_state_toggle", _state_toggle_action)
    register_action("emoji_state_global_pick", _state_global_pick_action)
    register_action("emoji_state_override_pick", _state_override_pick_action)
    register_action("emoji_state_choose", _state_choose_action)
    register_action("emoji_state_clear", _state_clear_action)
    register_action("emoji_cat_new", _cat_new_action)
    register_action("emoji_cat_new_custom", _cat_new_custom_action)
    register_action("emoji_compose", _compose_panel_handler)
    register_action("emoji_compose_add", _compose_add_action)
    register_action("emoji_compose_rm", _compose_rm_action)
    register_action("emoji_compose_apply", _compose_apply_action)
    register_action("emoji_compose_confirm", _compose_confirm_action)
    register_action("emoji_refresh", _refresh_action)
    register_action("emoji_cat_rename", _cat_rename_action)
    register_action("emoji_cat_del", _cat_del_action)
    register_action("emoji_cat_delgo", _cat_delgo_action)
    register_action("emoji_map_new", _map_new_action)
    register_action("emoji_map_pick", _map_pick_action)
    register_action("emoji_map_edit", _map_edit_action)
    register_action("emoji_map_del", _map_del_action)
    register_action("emoji_map_repl", _map_repl_action)
    logger.info("[EMOJI_UI] panels registered")
