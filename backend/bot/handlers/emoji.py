"""
Emoji & Reaction — Glass UI panels (Phase 1 remainder).

Owner-only panels reachable from the Menu:

* ``emoji``          — Emoji & Reaction home: import + library browser.
* ``emoji_import``   — Trigger the bounded Saved Messages import; shows the
                        honest import report.
* ``emoji_library``  — 2×5 paginated library browser over the owner's
                        emoji_library entries.

No Phase 2 categories/mappings/toggle/reconstructor/reactions here — those
are later phases. No keyword/regex routing; activation is purely panel-driven.
"""
from __future__ import annotations

import asyncio
import logging

from telethon import events

from backend.bot.handlers.guard import is_owner
from backend.db import client as db_client
from backend.helper import (
    InlinePanelBuilder,
    register_panel,
    register_inline_builder,
    register_action,
    send_inline_panel,
    render,
    render_edit,
)
from backend.helper.client import get_client
from backend.helper.inline_engine import _owner_id as _ie_owner_id
from backend.services import emoji_library_service
from backend.services.emoji_library_service import import_from_saved_messages

logger = logging.getLogger(__name__)

_LIBRARY_PER_PAGE = 10  # 2×5 convention
_EMOJI_ICON = "😶‍🌫️"


def _emoji_icon() -> str:
    return _EMOJI_ICON


def _library_row_label(row: dict) -> str:
    doc_id = row.get("document_id")
    alt = row.get("alt_text") or ""
    set_short = row.get("set_short_name") or ""
    label = f"{doc_id}"
    if alt:
        label += f" {alt}"
    if set_short:
        label += f" · {set_short}"
    if len(label) > 60:
        label = label[:57] + "…"
    return label


# ── Panel: emoji (home) ───────────────────────────────────────────────────────

async def _emoji_panel_handler(event, extra: str):
    builder = InlinePanelBuilder()
    builder.add_row("📥 Import from Saved Messages", "panel:emoji_import")
    builder.add_row("📋 Library Browser", "panel:emoji_library")
    return "Emoji & Reaction", (
        "Phase 1: import premium/custom emoji collections from your Saved "
        "Messages and browse the resulting library.\n\n"
        "Categories, mappings, replacement and reactions are later phases."
    ), builder.build()


async def _emoji_inline_builder(event, extra: str):
    return [render("Emoji & Reaction", (
        "Phase 1: import premium/custom emoji collections from your Saved "
        "Messages and browse the resulting library.\n\n"
        "Categories, mappings, replacement and reactions are later phases."
    ), _emoji_panel_handler(event, extra)[2])]


# ── Panel: emoji_import ───────────────────────────────────────────────────────

async def _emoji_import_panel_handler(event, extra: str):
    if extra.startswith("report:"):
        report = _parse_report(extra[7:])
        return _emoji_import_report(report)

    builder = InlinePanelBuilder()
    builder.add_row("▶ Run Import", "action:emoji_import_run")
    builder.add_row("‹ Back", "panel:emoji")
    return "Emoji Import", (
        "Import premium/custom emoji from your **Saved Messages**.\n\n"
        "Send one emoji from the collection you want to the Saved Messages chat, "
        "then tap **Run Import**. The import is bounded and deterministic — "
        "it scans newest-first, deduplicates, and reports honestly."
    ), builder.build()


async def _emoji_import_inline_builder(event, extra: str):
    return [render("Emoji Import", (
        "Import premium/custom emoji from your **Saved Messages**.\n\n"
        "Send one emoji from the collection you want to the Saved Messages chat, "
        "then tap **Run Import**."
    ), [
        ("▶ Run Import", "action:emoji_import_run"),
        ("‹ Back", "panel:emoji"),
    ])]


def _parse_report(report: dict) -> dict:
    return report


def _emoji_import_report(report: dict) -> tuple[str, str, list]:
    ok = report.get("ok", False)
    status_icon = "✓" if ok else "⚠" if report.get("error") else "✕"
    status_text = (
        "Completed"
        if ok
        else "Failed"
        if report.get("error")
        else "Degraded"
    )

    lines = [
        f"**Import Report**  {status_icon} {status_text}",
        "",
    ]

    if report.get("error"):
        lines.append(f"_{report['error']}_")
        lines.append("")

    # Phase 1 counters.
    lines.append(f"Storage: `{report.get('storage', '—')}`")
    lines.append(f"Pages scanned: `{report.get('pages', 0)}`")
    lines.append(f"Messages scanned: `{report.get('scanned_messages', 0)}`")
    lines.append(f"Custom emojis seen: `{report.get('custom_emoji_seen', 0)}`")
    lines.append(f"Malformed: `{report.get('malformed_entities', 0)}`")
    lines.append("")
    lines.append(f"Imported: `{report.get('imported', 0)}`")
    lines.append(f"Duplicates: `{report.get('duplicates', 0)}`")
    lines.append(f"Failed: `{report.get('failed', 0)}`")
    lines.append(f"Library total: `{report.get('library_total', '—')}`")
    lines.append("")
    lines.append(f"End reached: `{report.get('end_reached', False)}`")
    lines.append(f"Scan limit hit: `{report.get('hit_scan_limit', False)}`")
    lines.append(f"Record limit hit: `{report.get('hit_record_limit', False)}`")
    lines.append(f"Entity limit hit: `{report.get('hit_entity_limit', False)}`")
    lines.append("")

    # Set-resolution counters.
    if report.get("sets_requested", 0) or report.get("sets_resolved", 0):
        lines.append("__Set resolution & enumeration__")
        lines.append(f"Sets requested: `{report.get('sets_requested', 0)}`")
        lines.append(f"Sets resolved: `{report.get('sets_resolved', 0)}`")
        lines.append(f"Sets failed: `{report.get('sets_failed', 0)}`")
        if report.get("set_resolution_error"):
            lines.append(f"_{report['set_resolution_error']}_")
            lines.append("")
        lines.append(f"Set members seen: `{report.get('set_members_seen', 0)}`")
        lines.append(f"Set members imported: `{report.get('set_members_imported', 0)}`")
        lines.append(f"Set members duplicates: `{report.get('set_members_duplicates', 0)}`")
        lines.append(f"Set members failed: `{report.get('set_members_failed', 0)}`")
        lines.append("")

    lines.append("Note: sticker-set metadata is shown in reports and service "
                  "internals only. Persisting set_id / set_short_name per library "
                  "row is a later (manual schema) concern.")

    builder = InlinePanelBuilder()
    builder.add_row("↻ Run Import Again", "action:emoji_import_run")
    builder.add_row("‹ Back to Import", "panel:emoji_import")
    builder.add_row("📋 Library Browser", "panel:emoji_library")
    builder.add_row("🏠 Home", "panel:emoji")
    return "Emoji Import", "\n".join(lines), builder.build()


async def _emoji_import_run_action(event, extra: str, chat_id: int):
    owner_id = _resolve_owner_id(event)
    if not owner_id:
        return ("Emoji Import", "❌ Owner ID unavailable.", [])

    from backend.helper.inline_engine import _self_client as _client
    try:
        report = await import_from_saved_messages(_client, owner_id)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.exception("[EMOJI_UI] import action failed")
        report = {"ok": False, "error": str(exc) or type(exc).__name__}

    return _emoji_import_report(report)


# ── Panel: emoji_library (2×5 paginated browser) ─────────────────────────────

async def _emoji_library_panel_handler(event, extra: str):
    owner_id = _resolve_owner_id(event)
    if not owner_id:
        return "Emoji Library", "❌ Owner ID unavailable.", []

    page = 1
    if extra.startswith("page:"):
        page_str = extra[5:]
        if page_str.isdigit():
            page = max(1, int(page_str))

    per_page = _LIBRARY_PER_PAGE
    offset = (page - 1) * per_page
    rows, total = await db_client.list_emoji_entries(owner_id, limit=per_page, offset=offset)
    total_pages = max(1, (total + per_page - 1) // per_page)
    if page > total_pages:
        page = total_pages
        offset = (page - 1) * per_page
        rows, total = await db_client.list_emoji_entries(owner_id, limit=per_page, offset=offset)

    if not rows:
        builder = InlinePanelBuilder()
        builder.add_row("📥 Import from Saved Messages", "panel:emoji_import")
        builder.add_row("‹ Back", "panel:emoji")
        return "Emoji Library", (
            "_No emoji in the library yet._\n\n"
            "Send a premium/custom emoji to your Saved Messages, then import from "
            "the Emoji & Reaction panel."
        ), builder.build()

    body = f"_{total} emoji · page {page}/{total_pages}_"
    builder = InlinePanelBuilder()

    # 2-column layout: split rows into pairs.
    col1 = []
    col2 = []
    for idx, row in enumerate(rows):
        label = _library_row_label(row)
        callback = f"panel:emoji_library_entry:doc:{row.get('document_id')}"
        if idx % 2 == 0:
            col1.append((label, callback))
        else:
            col2.append((label, callback))

    max_len = max(len(col1), len(col2))
    for i in range(max_len):
        row_buttons = []
        if i < len(col1):
            row_buttons.append(col1[i])
        if i < len(col2):
            row_buttons.append(col2[i])
        if row_buttons:
            builder.add_buttons(*row_buttons)

    nav_row = []
    if page > 1:
        nav_row.append(("◀ Prev", f"panel:emoji_library:page:{page - 1}"))
    nav_row.append((f"{page}/{total_pages}", "panel:_nav:noop"))
    if page < total_pages:
        nav_row.append(("Next ▶", f"panel:emoji_library:page:{page + 1}"))
    builder.add_buttons(*nav_row)
    builder.add_row("📥 Import More", "panel:emoji_import")
    builder.add_row("‹ Back", "panel:emoji")

    return "Emoji Library", body, builder.build()


async def _emoji_library_inline_builder(event, extra: str):
    result = await _emoji_library_panel_handler(event, extra)
    if result is None:
        return [render("Emoji Library", "_No emoji._", [])]
    title, body, buttons = result
    return [render(title, body, buttons)]


async def _emoji_library_entry_panel_handler(event, extra: str):
    if not extra.startswith("doc:"):
        return "Emoji", "❌ Unknown emoji.", []
    try:
        doc_id = int(extra[4:])
    except ValueError:
        return "Emoji", "❌ Invalid emoji id.", []

    owner_id = _resolve_owner_id(event)
    if not owner_id:
        return "Emoji", "❌ Owner ID unavailable.", []

    rows, _total = await db_client.list_emoji_entries(owner_id, limit=10, offset=0)
    row = next((r for r in rows if r.get("document_id") == doc_id), None)
    if not row:
        return "Emoji", f"❌ No emoji found for document_id `{doc_id}`.", []

    set_short = row.get("set_short_name") or "—"
    alt = row.get("alt_text") or ""
    source_msg = row.get("source_msg_id")
    created = row.get("created_at") or "—"

    body = (
        f"**Document ID:** `{doc_id}`\n"
        f"**Alt text:** {alt or '—'}\n"
        f"**Set short name:** {set_short}\n"
        f"**Source msg id:** {source_msg or '—'}\n"
        f"**Source:** `{row.get('source', '—')}`\n"
        f"**Created:** {created}"
    )

    builder = InlinePanelBuilder()
    builder.add_row("📋 Back to Library", "panel:emoji_library")
    builder.add_row("🏠 Home", "panel:emoji")
    return "Emoji Detail", body, builder.build()


async def _emoji_library_entry_inline_builder(event, extra: str):
    result = await _emoji_library_entry_panel_handler(event, extra)
    if result is None:
        return [render("Emoji", "Not found.", [])]
    title, body, buttons = result
    return [render(title, body, buttons)]


# ── Helpers ───────────────────────────────────────────────────────────────────

def _resolve_owner_id(event) -> int | None:
    try:
        from backend.helper.inline_engine import _owner_id as _owner
        if _owner:
            return _owner
    except Exception:
        pass
    try:
        owner_id = getattr(event, "sender_id", None)
        if owner_id:
            return int(owner_id)
    except Exception:
        pass
    return None


# ── Registration ──────────────────────────────────────────────────────────────

def register(client, owner_id: int) -> None:
    try:
        register_panel("emoji", _emoji_panel_handler, parent="menu", title="Emoji & Reaction")
        register_inline_builder("emoji", _emoji_inline_builder)

        register_panel("emoji_import", _emoji_import_panel_handler, parent="emoji", title="Emoji Import")
        register_inline_builder("emoji_import", _emoji_import_inline_builder)
        register_action("emoji_import_run", _emoji_import_run_action)

        register_panel("emoji_library", _emoji_library_panel_handler, parent="emoji", title="Emoji Library")
        register_inline_builder("emoji_library", _emoji_library_inline_builder)

        register_panel("emoji_library_entry", _emoji_library_entry_panel_handler, parent="emoji_library", title="Emoji Detail")
        register_inline_builder("emoji_library_entry", _emoji_library_entry_inline_builder)

        logger.info("Emoji panels registered OK")
    except Exception as exc:
        logger.error("Emoji panel registration FAILED: %s", exc)
