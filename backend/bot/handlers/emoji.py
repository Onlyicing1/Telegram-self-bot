"""
Emoji Library — Glass UI (Phase 1 remainder, ROADMAP §9).

Exposes the existing bounded Saved Messages emoji-library import and the
imported library through the repository's standard panel machinery — the
panel registry, ``InlinePanelBuilder``, ``panel:``/``action:`` callback
conventions and the callback router's owner check. No new UI framework, no
second client/loop/scheduler: the import runs through
``backend.services.emoji_library_service`` with the REAL self client from
``backend.helper.inline_engine``, and every read goes through the existing
``backend.db.client`` emoji helpers.

Honesty contract: the import report is rendered straight from the service
result — success, degraded and failed states are all shown, with the
failure reason when there is one. The library browser shows only what the
rows actually contain (alt text, document id, origin, created date); no
set/category metadata is displayed or implied.
"""
from __future__ import annotations

import logging
from typing import Any

from backend.helper.inline_engine import get_owner_id, get_self_client
from backend.helper.panels import (
    InlinePanelBuilder,
    register_action,
    register_panel,
)
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
    return "Emoji", "\n".join(lines), builder.build()


# ── import action ─────────────────────────────────────────────────────────────


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


# ── registration ──────────────────────────────────────────────────────────────


def register(client, owner_id: int) -> None:
    register_panel(_MAIN_PANEL, _emoji_panel_handler, parent="menu", title="Emoji")
    register_panel(
        _LIBRARY_PANEL, _library_page_handler, parent=_MAIN_PANEL, title="Library"
    )
    register_panel(
        _ENTRY_PANEL, _entry_page_handler, parent=_LIBRARY_PANEL, title="Entry"
    )
    register_action("emoji_import", _import_action)
    logger.info("[EMOJI_UI] panels registered")
