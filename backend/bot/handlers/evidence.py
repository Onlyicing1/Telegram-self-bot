"""Owner-only Evidence Explorer panel.

This panel is read-only for evidence content: it renders active run snapshots
and archive revisions that already exist, constructs Telegram links locally, and
collects explicit owner confirmations for archival and destructive archive
deletion.  It runs no provider call, scheduler, Telegram listener or background
cleanup task.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from backend.evidence import constants
from backend.evidence.models import (
    EvidenceConfirmationRequired,
    EvidenceDependencyBlocked,
    EvidenceError,
    EvidenceExpired,
    EvidenceNotFound,
    EvidenceQuotaError,
)
from backend.evidence.service import get_evidence_service
from backend.evidence.links import telegram_message_link
from backend.bot.handlers.guard import is_owner
from backend.helper.inline_engine import get_self_client
from backend.helper.panels import (
    InlinePanelBuilder,
    register_action,
    register_panel,
)
from backend.helper import symbols as sym

logger = logging.getLogger(__name__)

PANEL_ID = "evidence"
TITLE = "Evidence Explorer"

_OWNER_ID = 0
_PAGE_SIZE_REFS = 5
_PAGE_SIZE_ARCHIVE = 10
_PAGE_SIZE_REVISIONS = 10


def _owner_ok(event: Any) -> bool:
    """The canonical owner gate — never an allow-all default."""
    return is_owner(event, _OWNER_ID)


def _failure(exc: Exception) -> str:
    if isinstance(exc, EvidenceExpired):
        return f"{sym.STATE_FAILED} That evidence run has expired. It cannot be reconstructed from Telegram, the archive or summaries."
    if isinstance(exc, EvidenceNotFound):
        return f"{sym.STATE_FAILED} That evidence record no longer exists for this owner."
    if isinstance(exc, EvidenceQuotaError):
        return f"{sym.STATE_WARNING} {exc}"
    if isinstance(exc, EvidenceDependencyBlocked):
        return f"{sym.STATE_WARNING} {exc}"
    if isinstance(exc, EvidenceConfirmationRequired):
        return f"{sym.STATE_WARNING} {exc}"
    if isinstance(exc, EvidenceError):
        return f"{sym.STATE_FAILED} {exc}"
    logger.warning("EVIDENCE_PANEL_FAILED error=%s", type(exc).__name__)
    return f"{sym.STATE_FAILED} The evidence store could not complete that operation ({type(exc).__name__})."


def _dt(value: datetime) -> str:
    value = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: max(1, limit - 1)] + "…"


def _split_page(value: str) -> tuple[str, int]:
    """Split ``<opaque id>[:<page>]``; an opaque id never contains a colon."""
    head, _, raw = str(value or "").partition(":")
    return head, int(raw) if raw.isdigit() else 0


def _bytes(value: int) -> str:
    if value < 1024:
        return f"{value} B"
    if value < 1024 * 1024:
        return f"{value / 1024:.1f} KiB"
    return f"{value / (1024 * 1024):.2f} MiB"


async def _overview() -> tuple[str, str, list]:
    """The panel root: bounded status only, never an account-wide scan."""
    service = get_evidence_service()
    runs = await _load_runs()
    usage = await service.archive_usage(_OWNER_ID)
    builder = InlinePanelBuilder()
    builder.add_row(f"Active Runs ({len(runs)})", "action:evidence_runs")
    builder.add_row(
        f"Archive ({usage.records} revisions)", "action:evidence_archive",
    )
    builder.add_row("History & Intelligence", "panel:history")
    lines = [
        "Owner-only Evidence foundation. Active runs are temporary snapshots; "
        "the archive is owner-approved and append-only.",
        "",
        f"Active runs: {len(runs)}/{constants.MAX_ACTIVE_RUNS_PER_OWNER}",
        f"Archive: {usage.records}/{constants.MAX_ARCHIVE_RECORDS_PER_OWNER} "
        f"revisions · {_bytes(usage.bytes)}",
        "",
        "Active runs expire on database time and are never reconstructed. "
        "Expired snapshots may be physically removed only inside an existing "
        "authorized operation; there is no cleanup scheduler.",
        "Archive deletion never deletes an active run's excerpt snapshots.",
    ]
    return TITLE, "\n".join(lines), builder.build()


def _runs_view(runs: list, notice: str = "") -> tuple[str, str, list]:
    builder = InlinePanelBuilder()
    lines = [
        f"Active Evidence Runs ({len(runs)}/{constants.MAX_ACTIVE_RUNS_PER_OWNER})",
        "Each run carries bounded excerpt snapshots and expires on database time.",
    ]
    if not runs:
        lines.extend(["", "No active evidence runs. Create one from History → Cited Summary."])
    for index, run in enumerate(runs, start=1):
        lines.extend([
            "",
            f"{index}. {run.peer_name or 'Unnamed chat'} — {run.ref_count} refs",
            f"   {_dt(run.created_at)} → {_dt(run.expires_at)}",
        ])
        builder.add_row(
            f"Open run {index} ({run.ref_count} refs)",
            f"action:evidence_open:{run.id}",
        )
    builder.add_row("Back", "panel:evidence")
    if notice:
        lines.extend(["", notice])
    return TITLE, "\n".join(lines), builder.build()


def _back_buttons() -> list:
    builder = InlinePanelBuilder()
    builder.add_row("Back to Evidence", "panel:evidence")
    return builder.build()


def _run_view(
    bundle: Any, page: int = 0, notice: str = "",
) -> tuple[str, str, list]:
    if bundle is None:
        return TITLE, f"{sym.STATE_FAILED} That evidence run is unavailable.", _back_buttons()
    run = bundle.run
    refs = list(bundle.refs)
    pages = max(1, (len(refs) + _PAGE_SIZE_REFS - 1) // _PAGE_SIZE_REFS)
    page = max(0, min(int(page), pages - 1))
    start = page * _PAGE_SIZE_REFS
    shown = refs[start:start + _PAGE_SIZE_REFS]
    lines = [
        f"Chat: {run.peer_name or 'Unnamed chat'}",
        f"Run: {run.id}",
        f"TTL: {run.ttl_days} days",
        f"Created: {_dt(run.created_at)}",
        f"Expires: {_dt(run.expires_at)} (database time)",
        f"References: {len(refs)}  ·  Page {page + 1}/{pages}",
        f"Bounded text: {_bytes(run.text_bytes)}",
    ]
    if run.summary:
        lines.extend(["", "Summary:", _truncate(run.summary, 900)])
    if not shown:
        lines.extend(["", "No references in this run."])
    for ref in shown:
        link = telegram_message_link(run.peer_id, ref.message_id)
        lines.extend([
            "",
            f"{ref.key}  ·  {link}",
            _truncate(ref.excerpt or "(media only)", 300)
            + ("  [excerpt truncated]" if ref.excerpt_truncated else ""),
        ])
    builder = InlinePanelBuilder()
    if page > 0:
        builder.add_row("Previous", f"action:evidence_page:{run.id}:{page - 1}")
    if page + 1 < pages:
        builder.add_row("Next", f"action:evidence_page:{run.id}:{page + 1}")
    builder.add_row("Archive Suggestions", f"action:evidence_suggest:{run.id}")
    builder.add_row("Back to Active Runs", "action:evidence_runs")
    if notice:
        lines.extend(["", notice])
    return TITLE, "\n".join(lines), builder.build()


def _suggest_view(
    run_id: str, suggestions: list, notice: str = "",
) -> tuple[str, str, list]:
    lines = [
        "Archive suggestions are limited to evidence explicitly reviewed in this "
        "run. Nothing is archived automatically; every revision needs confirmation.",
        "",
    ]
    builder = InlinePanelBuilder()
    if not suggestions:
        lines.append("No unarchived reviewed references remain in this run.")
    for suggestion in suggestions:
        lines.extend([
            f"{suggestion.key}: {suggestion.reason}",
            _truncate(suggestion.excerpt or "(media only)", 220),
            "",
        ])
        builder.add_row(
            f"Review {suggestion.key} for archive",
            f"action:evidence_confirm:{run_id}:{suggestion.key}",
        )
    builder.add_row("Back to Run", f"action:evidence_open:{run_id}")
    if notice:
        lines.extend(["", notice])
    return TITLE, "\n".join(lines), builder.build()


def _confirm_view(
    run_id: str, candidate: Any, notice: str = "",
) -> tuple[str, str, list]:
    if candidate is None:
        return TITLE, f"{sym.STATE_FAILED} That suggestion is no longer available.", _back_buttons()
    lines = [
        f"Archive {candidate.key} as an immutable revision?",
        "",
        _truncate(candidate.excerpt or "(media only)", 500),
        "",
        f"Why: {candidate.reason}",
        "",
        "Confirming writes a new owner-approved revision. The same content is a "
        "no-op; changed content never overwrites an old revision. The source may "
        "still be edited or deleted in Telegram; only its observed status changes.",
    ]
    builder = InlinePanelBuilder()
    builder.add_row(
        "Confirm immutable archive",
        f"action:evidence_commit:{run_id}:{candidate.key}",
    )
    builder.add_row("Cancel", f"action:evidence_open:{run_id}")
    if notice:
        lines.extend(["", notice])
    return TITLE, "\n".join(lines), builder.build()


def _archive_view(
    sources: list, usage: Any, offset: int = 0, has_more: bool = False,
    notice: str = "",
) -> tuple[str, str, list]:
    builder = InlinePanelBuilder()
    page_no = offset // _PAGE_SIZE_ARCHIVE + 1
    lines = ["Archive is owner-approved, immutable and never expires automatically."]
    if usage is not None:
        lines.extend([
            f"Records: {usage.records}/{constants.MAX_ARCHIVE_RECORDS_PER_OWNER}",
            f"Bytes: {_bytes(usage.bytes)}/{_bytes(constants.MAX_ARCHIVE_BYTES_PER_OWNER)}",
        ])
    lines.append(
        f"Page {page_no}" + (" (more archived sources available)" if has_more else "")
    )
    if not sources:
        lines.extend(["", "No archived evidence sources."])
    for index, source in enumerate(sources, start=offset + 1):
        link = telegram_message_link(source.peer_id, source.message_id)
        lines.extend([
            "",
            f"{index}. {link}",
            f"   Observed: {source.observed_state}  ·  Last seen {_dt(source.last_observed_at)}",
        ])
        builder.add_row(f"Inspect source {index}", f"action:evidence_source:{source.id}")
    if offset > 0:
        builder.add_row(
            "Previous",
            f"action:evidence_archive:{max(0, offset - _PAGE_SIZE_ARCHIVE)}",
        )
    if has_more:
        builder.add_row(
            "Next", f"action:evidence_archive:{offset + _PAGE_SIZE_ARCHIVE}",
        )
    builder.add_row("Back to Evidence", "panel:evidence")
    if notice:
        lines.extend(["", notice])
    return TITLE, "\n".join(lines), builder.build()


def _source_view(
    source: Any, revisions: list, page: int = 0, notice: str = "",
) -> tuple[str, str, list]:
    if source is None:
        return TITLE, f"{sym.STATE_FAILED} That archived source is unavailable.", _back_buttons()
    pages = max(1, (len(revisions) + _PAGE_SIZE_REVISIONS - 1) // _PAGE_SIZE_REVISIONS)
    page = max(0, min(int(page), pages - 1))
    start = page * _PAGE_SIZE_REVISIONS
    shown = revisions[start:start + _PAGE_SIZE_REVISIONS]
    link = telegram_message_link(source.peer_id, source.message_id)
    lines = [
        f"Source: {link}",
        f"Observed status: {source.observed_state}",
        f"First seen: {_dt(source.first_seen_at)}",
        f"Last observed: {_dt(source.last_observed_at)}",
        "",
        f"Immutable revisions ({len(revisions)})  ·  Page {page + 1}/{pages}",
    ]
    builder = InlinePanelBuilder()
    builder.add_row("Re-check source", f"action:evidence_recheck:{source.id}")
    for revision in shown:
        lines.extend([
            "",
            f"Revision {revision.revision_no} · {_dt(revision.captured_at)}",
            _truncate(revision.text or "(media only)", 300),
            f"Approval: {revision.approval_reference}",
        ])
        builder.add_row(
            f"Delete revision {revision.revision_no}",
            f"action:evidence_delrev:{revision.id}",
        )
    if page > 0:
        builder.add_row(
            "Previous revisions", f"action:evidence_source:{source.id}:{page - 1}",
        )
    if page + 1 < pages:
        builder.add_row(
            "Next revisions", f"action:evidence_source:{source.id}:{page + 1}",
        )
    builder.add_row("Delete entire source", f"action:evidence_delsrc:{source.id}")
    builder.add_row("Back to Archive", "action:evidence_archive")
    if notice:
        lines.extend(["", notice])
    return TITLE, "\n".join(lines), builder.build()


def _delete_source_confirm(source_id: str) -> tuple[str, str, list]:
    lines = [
        "Delete this archived source and every revision?",
        "",
        "This is destructive and requires explicit confirmation. Dependents block "
        "deletion by default. Active evidence-run excerpt snapshots are not part "
        "of this archive and remain until their own TTL; this action is not a "
        "purge of those snapshots.",
    ]
    builder = InlinePanelBuilder()
    builder.add_row(
        "Confirm source deletion",
        f"action:evidence_delsrc_ok:{source_id}",
    )
    builder.add_row(
        "Confirm cascade deletion",
        f"action:evidence_delsrc_all:{source_id}",
    )
    builder.add_row("Cancel", f"action:evidence_source:{source_id}")
    return TITLE, "\n".join(lines), builder.build()


def _delete_revision_confirm(source_id: str, revision_id: str) -> tuple[str, str, list]:
    lines = [
        "Delete this immutable archive revision?",
        "",
        "This is destructive and requires explicit confirmation. Dependents block "
        "deletion by default. Active evidence-run excerpt snapshots are unaffected "
        "and remain until their own TTL.",
    ]
    builder = InlinePanelBuilder()
    builder.add_row(
        "Confirm revision deletion",
        f"action:evidence_delrev_ok:{revision_id}",
    )
    builder.add_row(
        "Confirm cascade deletion",
        f"action:evidence_delrev_all:{revision_id}",
    )
    builder.add_row("Cancel", f"action:evidence_source:{source_id}")
    return TITLE, "\n".join(lines), builder.build()


# ── async data loaders used by the panel/action wrappers ────────────────────


async def _load_runs() -> list:
    return await get_evidence_service().list_active_runs(_OWNER_ID)


async def _load_run(run_id: str):
    return await get_evidence_service().get_run(_OWNER_ID, run_id)


async def _load_suggestions(run_id: str):
    return await get_evidence_service().archive_candidates(_OWNER_ID, run_id)


async def _load_archive(offset: int = 0):
    """One bounded page plus a look-ahead row; no page state is cached."""
    service = get_evidence_service()
    start = max(0, int(offset))
    rows = await service.list_archive_sources(
        _OWNER_ID, limit=_PAGE_SIZE_ARCHIVE + 1, offset=start,
    )
    usage = await service.archive_usage(_OWNER_ID)
    return rows[:_PAGE_SIZE_ARCHIVE], usage, len(rows) > _PAGE_SIZE_ARCHIVE


async def _load_source(source_id: str):
    service = get_evidence_service()
    source = await service.get_source(_OWNER_ID, source_id)
    revisions = await service.list_revisions(_OWNER_ID, source_id)
    return source, revisions


# ── panel/action registration ──────────────────────────────────────────────


async def _panel_handler(event, extra: str) -> tuple[str, str, list] | None:
    if not _owner_ok(event):
        return TITLE, "This panel is owner-only.", []
    try:
        if extra.startswith("run:"):
            parts = extra.split(":")
            run_id = parts[1] if len(parts) > 1 else ""
            page = int(parts[2]) if len(parts) > 2 else 0
            return _run_view(await _load_run(run_id), page)
        if extra == "archive":
            sources, usage, has_more = await _load_archive(0)
            return _archive_view(sources, usage, 0, has_more)
        if extra.startswith("source:"):
            source_id, page = _split_page(extra.split(":", 1)[1])
            source, revisions = await _load_source(source_id)
            return _source_view(source, revisions, page)
        return await _overview()
    except Exception as exc:  # noqa: BLE001 — panel boundary
        return TITLE, _failure(exc), _back_buttons()


async def _runs_action(event, extra: str, chat_id: int):
    if not _owner_ok(event):
        return TITLE, "This panel is owner-only.", []
    try:
        return _runs_view(await _load_runs())
    except Exception as exc:  # noqa: BLE001
        return TITLE, _failure(exc), _back_buttons()


async def _open_action(event, extra: str, chat_id: int):
    if not _owner_ok(event):
        return TITLE, "This panel is owner-only.", []
    try:
        return _run_view(await _load_run(extra))
    except Exception as exc:  # noqa: BLE001
        return TITLE, _failure(exc), _back_buttons()


async def _page_action(event, extra: str, chat_id: int):
    if not _owner_ok(event):
        return TITLE, "This panel is owner-only.", []
    try:
        run_id, raw_page = extra.rsplit(":", 1)
        return _run_view(await _load_run(run_id), int(raw_page))
    except Exception as exc:  # noqa: BLE001
        return TITLE, _failure(exc), _back_buttons()


async def _suggest_action(event, extra: str, chat_id: int):
    if not _owner_ok(event):
        return TITLE, "This panel is owner-only.", []
    try:
        return _suggest_view(extra, await _load_suggestions(extra))
    except Exception as exc:  # noqa: BLE001
        return TITLE, _failure(exc), _back_buttons()


async def _confirm_action(event, extra: str, chat_id: int):
    if not _owner_ok(event):
        return TITLE, "This panel is owner-only.", []
    try:
        run_id, key = extra.rsplit(":", 1)
        suggestions = await _load_suggestions(run_id)
        candidate = next((item for item in suggestions if item.key == key), None)
        return _confirm_view(run_id, candidate)
    except Exception as exc:  # noqa: BLE001
        return TITLE, _failure(exc), _back_buttons()


async def _commit_action(event, extra: str, chat_id: int):
    if not _owner_ok(event):
        return TITLE, "This panel is owner-only.", []
    try:
        run_id, key = extra.rsplit(":", 1)
        result = await get_evidence_service().archive_ref(
            _OWNER_ID, run_id, key, approved=True,
        )
        bundle = await _load_run(run_id)
        notice = (
            f"{sym.STATE_COMPLETED} Archived {key} as immutable revision "
            f"{result.revision_no}."
            if result.status == "created"
            else f"{sym.STATE_COMPLETED} {key} was already archived (no new revision)."
        )
        return _run_view(bundle, 0, notice)
    except Exception as exc:  # noqa: BLE001
        return TITLE, _failure(exc), _back_buttons()


async def _archive_action(event, extra: str, chat_id: int):
    if not _owner_ok(event):
        return TITLE, "This panel is owner-only.", []
    try:
        # The callback only carries an offset; the page contents are re-read
        # through the owner-scoped service every time.
        offset = max(0, int(extra or 0))
    except (TypeError, ValueError):
        offset = 0
    try:
        sources, usage, has_more = await _load_archive(offset)
        return _archive_view(sources, usage, offset, has_more)
    except Exception as exc:  # noqa: BLE001
        return TITLE, _failure(exc), _back_buttons()


async def _source_action(event, extra: str, chat_id: int):
    if not _owner_ok(event):
        return TITLE, "This panel is owner-only.", []
    try:
        source_id, page = _split_page(extra)
        source, revisions = await _load_source(source_id)
        return _source_view(source, revisions, page)
    except Exception as exc:  # noqa: BLE001
        return TITLE, _failure(exc), _back_buttons()


async def _recheck_action(event, extra: str, chat_id: int):
    """Re-read the exact Telegram source; archived text is never rewritten."""
    if not _owner_ok(event):
        return TITLE, "This panel is owner-only.", []
    try:
        source = await get_evidence_service().revalidate_source(
            _OWNER_ID, extra, source=get_self_client(),
        )
        revisions = await get_evidence_service().list_revisions(
            _OWNER_ID, source.id,
        )
        return _source_view(
            source, revisions, 0,
            f"{sym.STATE_COMPLETED} Source re-checked: {source.observed_state}. "
            "The archived text and hash are unchanged.",
        )
    except Exception as exc:  # noqa: BLE001
        return TITLE, _failure(exc), _back_buttons()


async def _delete_source_action(event, extra: str, chat_id: int):
    if not _owner_ok(event):
        return TITLE, "This panel is owner-only.", []
    return _delete_source_confirm(extra)


async def _delete_revision_action(event, extra: str, chat_id: int):
    if not _owner_ok(event):
        return TITLE, "This panel is owner-only.", []
    try:
        # The callback carries only the opaque revision id; its source is
        # re-resolved through the owner-scoped service, never from the click.
        revision = await get_evidence_service().get_revision(_OWNER_ID, extra)
    except Exception as exc:  # noqa: BLE001
        return TITLE, _failure(exc), _back_buttons()
    return _delete_revision_confirm(revision.source_id, revision.id)


async def _delete_source_commit(event, extra: str, chat_id: int, *, cascade: bool = False):
    if not _owner_ok(event):
        return TITLE, "This panel is owner-only.", []
    try:
        await get_evidence_service().delete_source(
            _OWNER_ID, extra, confirm=True, cascade_confirmed=cascade,
        )
        sources, usage, has_more = await _load_archive(0)
        return _archive_view(
            sources, usage, 0, has_more,
            f"{sym.STATE_COMPLETED} The archived source was deleted.",
        )
    except Exception as exc:  # noqa: BLE001
        return TITLE, _failure(exc), _back_buttons()


async def _delete_revision_commit(event, extra: str, chat_id: int, *, cascade: bool = False):
    if not _owner_ok(event):
        return TITLE, "This panel is owner-only.", []
    try:
        service = get_evidence_service()
        revision = await service.get_revision(_OWNER_ID, extra)
        source_id = revision.source_id
        await service.delete_revision(
            _OWNER_ID, revision.id, confirm=True, cascade_confirmed=cascade,
        )
        source, revisions = await _load_source(source_id)
        return _source_view(
            source, revisions, 0,
            f"{sym.STATE_COMPLETED} The archive revision was deleted.",
        )
    except Exception as exc:  # noqa: BLE001
        return TITLE, _failure(exc), _back_buttons()


async def _delete_source_commit_action(event, extra: str, chat_id: int):
    return await _delete_source_commit(event, extra, chat_id)


async def _delete_source_cascade_action(event, extra: str, chat_id: int):
    return await _delete_source_commit(event, extra, chat_id, cascade=True)


async def _delete_revision_commit_action(event, extra: str, chat_id: int):
    return await _delete_revision_commit(event, extra, chat_id)


async def _delete_revision_cascade_action(event, extra: str, chat_id: int):
    return await _delete_revision_commit(event, extra, chat_id, cascade=True)


def register(client=None, owner_id: int = 0) -> None:
    """Register the Evidence Explorer panel and its actions. No listener."""
    global _OWNER_ID
    _OWNER_ID = int(owner_id or 0)
    register_panel(PANEL_ID, _panel_handler, parent="history", title=TITLE)
    register_action("evidence_runs", _runs_action)
    register_action("evidence_open", _open_action)
    register_action("evidence_page", _page_action)
    register_action("evidence_suggest", _suggest_action)
    register_action("evidence_confirm", _confirm_action)
    register_action("evidence_commit", _commit_action)
    register_action("evidence_archive", _archive_action)
    register_action("evidence_source", _source_action)
    register_action("evidence_recheck", _recheck_action)
    register_action("evidence_delsrc", _delete_source_action)
    register_action("evidence_delsrc_ok", _delete_source_commit_action)
    register_action("evidence_delsrc_all", _delete_source_cascade_action)
    register_action("evidence_delrev", _delete_revision_action)
    register_action("evidence_delrev_ok", _delete_revision_commit_action)
    register_action("evidence_delrev_all", _delete_revision_cascade_action)
