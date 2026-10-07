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
    builder = InlinePanelBuilder()
    builder.add_row("⬇ Import from Saved Messages", "action:emoji_import")
    builder.add_row("📚 Library", f"panel:{_LIBRARY_PANEL}")
    builder.add_row("🗂 Categories", f"panel:{_CATEGORIES_PANEL}")
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
    lines = [
        f"🗂 **{row.get('name') or '?'}**",
        "",
        (
            f"{count_label} mapping(s)"
            if counts is not None
            else "_Mapping count unknown — storage read failed._"
        ),
    ]
    builder = InlinePanelBuilder()
    builder.add_row("🗺 Mappings", f"panel:{_MAPPINGS_PANEL}:{cid}:0")
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
    if notice:
        lines: list[str] = [notice, "", f"🗺 **{category.get('name')}** — {total}"]
    elif not rows:
        lines = [
            f"🗺 **{category.get('name')}** — {total}", "",
            "_No mappings yet. Add one below._",
        ]
    else:
        lines = [
            f"🗺 **{category.get('name')}** — {total} · page {page + 1}/{page_count}",
            "",
            "_simple → premium:_",
        ]
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


# ── registration ──────────────────────────────────────────────────────────


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
    register_action("emoji_import", _import_action)
    register_action("emoji_cat_new", _cat_new_action)
    register_action("emoji_cat_rename", _cat_rename_action)
    register_action("emoji_cat_del", _cat_del_action)
    register_action("emoji_cat_delgo", _cat_delgo_action)
    register_action("emoji_map_new", _map_new_action)
    register_action("emoji_map_pick", _map_pick_action)
    register_action("emoji_map_edit", _map_edit_action)
    register_action("emoji_map_del", _map_del_action)
    register_action("emoji_map_repl", _map_repl_action)
    logger.info("[EMOJI_UI] panels registered")
