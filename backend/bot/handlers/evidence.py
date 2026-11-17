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


def _owner_ok(event: Any) -> bool:
    if not _OWNER_ID:
        return True
    try:
        return int(getattr(event, "sender_id", 0) or 0) == _OWNER_ID
    except (TypeError, ValueError):
        return False


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


_runs_sync_cache: list = []


def _runs_view(notice: str = "") -> tuple[str, str, list]:
    try:
        # The panel handler is async; this function is only called from an
        # async wrapper below.  Kept separate for testability.
        runs = _runs_sync_cache
    except Exception as exc:  # pragma: no cover - defensive
        return TITLE, _failure(exc), _back_buttons()
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
    run_id: str, page: int = 0, notice: str = "",
) -> tuple[str, str, list]:
    try:
        bundle = _run_bundle_cache
    except NameError:
        bundle = None
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


_run_bundle_cache: Any = None


def _suggest_view(run_id: str, notice: str = "") -> tuple[str, str, list]:
    try:
        suggestions = _suggestions_cache
    except NameError:
        suggestions = []
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


def _confirm_view(run_id: str, key: str, notice: str = "") -> tuple[str, str, list]:
    try:
        candidate = _candidate_cache
    except NameError:
        candidate = None
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
    builder.add_row("Confirm immutable archive", f"action:evidence_commit:{run_id}:{key}")
    builder.add_row("Cancel", f"action:evidence_open:{run_id}")
    if notice:
        lines.extend(["", notice])
    return TITLE, "\n".join(lines), builder.build()


def _archive_view(notice: str = "") -> tuple[str, str, list]:
    try:
        sources = _sources_cache
        usage = _usage_cache
    except NameError:
        sources, usage = [], None
    builder = InlinePanelBuilder()
    lines = ["Archive is owner-approved, immutable and never expires automatically."]
    if usage is not None:
        lines.extend([
            f"Records: {usage.records}/{constants.MAX_ARCHIVE_RECORDS_PER_OWNER}",
            f"Bytes: {_bytes(usage.bytes)}/{_bytes(constants.MAX_ARCHIVE_BYTES_PER_OWNER)}",
        ])
    if not sources:
        lines.extend(["", "No archived evidence sources."])
    for index, source in enumerate(sources, start=1):
        link = telegram_message_link(source.peer_id, source.message_id)
        lines.extend([
            "",
            f"{index}. {link}",
            f"   Observed: {source.observed_state}  ·  Last seen {_dt(source.last_observed_at)}",
        ])
        builder.add_row(f"Inspect source {index}", f"action:evidence_source:{source.id}")
    builder.add_row("Back to Evidence", "panel:evidence")
    if notice:
        lines.extend(["", notice])
    return TITLE, "\n".join(lines), builder.build()


def _source_view(source_id: str, notice: str = "") -> tuple[str, str, list]:
    try:
        source = _source_cache
        revisions = _revisions_cache
    except NameError:
        source, revisions = None, []
    if source is None:
        return TITLE, f"{sym.STATE_FAILED} That archived source is unavailable.", _back_buttons()
    link = telegram_message_link(source.peer_id, source.message_id)
    lines = [
        f"Source: {link}",
        f"Observed status: {source.observed_state}",
        f"First seen: {_dt(source.first_seen_at)}",
        f"Last observed: {_dt(source.last_observed_at)}",
        "",
        f"Immutable revisions ({len(revisions)}):",
    ]
    builder = InlinePanelBuilder()
    for revision in revisions[:10]:
        lines.extend([
            "",
            f"Revision {revision.revision_no} · {_dt(revision.captured_at)}",
            _truncate(revision.text or "(media only)", 300),
            f"Approval: {revision.approval_reference}",
        ])
        builder.add_row(
            f"Delete revision {revision.revision_no}",
            f"action:evidence_delete_revision:{source.id}:{revision.id}",
        )
    if len(revisions) > 10:
        lines.extend(["", f"Showing the first 10 of {len(revisions)} revisions."])
    builder.add_row("Delete entire source", f"action:evidence_delete_source:{source.id}")
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
        f"action:evidence_delete_source_commit:{source_id}",
    )
    builder.add_row(
        "Confirm cascade deletion",
        f"action:evidence_delete_source_cascade:{source_id}",
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
        f"action:evidence_delete_revision_commit:{source_id}:{revision_id}",
    )
    builder.add_row(
        "Confirm cascade deletion",
        f"action:evidence_delete_revision_cascade:{source_id}:{revision_id}",
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


async def _load_archive():
    service = get_evidence_service()
    sources = await service.list_archive_sources(_OWNER_ID)
    usage = await service.archive_usage(_OWNER_ID)
    return sources, usage


async def _load_source(source_id: str):
    service = get_evidence_service()
    source = await service.get_source(_OWNER_ID, source_id)
    revisions = await service.list_revisions(_OWNER_ID, source_id)
    return source, revisions


# ── panel/action registration ──────────────────────────────────────────────


async def _panel_handler(event, extra: str) -> tuple[str, str, list] | None:
    global _run_bundle_cache
    global _sources_cache, _usage_cache, _source_cache, _revisions_cache
    if not _owner_ok(event):
        return TITLE, "This panel is owner-only.", []
    try:
        if extra.startswith("run:"):
            parts = extra.split(":")
            run_id = parts[1] if len(parts) > 1 else ""
            page = int(parts[2]) if len(parts) > 2 else 0
            _run_bundle_cache = await _load_run(run_id)
            return _run_view(run_id, page)
        if extra == "archive":
            _sources_cache, _usage_cache = await _load_archive()
            return _archive_view()
        if extra.startswith("source:"):
            source_id = extra.split(":", 1)[1]
            _source_cache, _revisions_cache = await _load_source(source_id)
            return _source_view(source_id)
        return await _overview()
    except Exception as exc:  # noqa: BLE001 — panel boundary
        return TITLE, _failure(exc), _back_buttons()


async def _runs_action(event, extra: str, chat_id: int):
    global _runs_sync_cache
    if not _owner_ok(event):
        return TITLE, "This panel is owner-only.", []
    try:
        _runs_sync_cache = await _load_runs()
        return _runs_view()
    except Exception as exc:  # noqa: BLE001
        return TITLE, _failure(exc), _back_buttons()


async def _open_action(event, extra: str, chat_id: int):
    global _run_bundle_cache
    if not _owner_ok(event):
        return TITLE, "This panel is owner-only.", []
    try:
        _run_bundle_cache = await _load_run(extra)
        return _run_view(extra)
    except Exception as exc:  # noqa: BLE001
        return TITLE, _failure(exc), _back_buttons()


async def _page_action(event, extra: str, chat_id: int):
    global _run_bundle_cache
    if not _owner_ok(event):
        return TITLE, "This panel is owner-only.", []
    try:
        run_id, raw_page = extra.rsplit(":", 1)
        _run_bundle_cache = await _load_run(run_id)
        return _run_view(run_id, int(raw_page))
    except Exception as exc:  # noqa: BLE001
        return TITLE, _failure(exc), _back_buttons()


async def _suggest_action(event, extra: str, chat_id: int):
    global _suggestions_cache
    if not _owner_ok(event):
        return TITLE, "This panel is owner-only.", []
    try:
        _suggestions_cache = await _load_suggestions(extra)
        return _suggest_view(extra)
    except Exception as exc:  # noqa: BLE001
        return TITLE, _failure(exc), _back_buttons()


async def _confirm_action(event, extra: str, chat_id: int):
    global _candidate_cache
    if not _owner_ok(event):
        return TITLE, "This panel is owner-only.", []
    try:
        run_id, key = extra.rsplit(":", 1)
        suggestions = await _load_suggestions(run_id)
        _candidate_cache = next((item for item in suggestions if item.key == key), None)
        return _confirm_view(run_id, key)
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
        globals()["_run_bundle_cache"] = bundle
        notice = (
            f"{sym.STATE_SUCCESS} Archived {key} as immutable revision "
            f"{result.revision_no}."
            if result.status == "created"
            else f"{sym.STATE_SUCCESS} {key} was already archived (no new revision)."
        )
        return _run_view(run_id, 0, notice)
    except Exception as exc:  # noqa: BLE001
        return TITLE, _failure(exc), _back_buttons()


async def _archive_action(event, extra: str, chat_id: int):
    global _sources_cache, _usage_cache
    if not _owner_ok(event):
        return TITLE, "This panel is owner-only.", []
    try:
        _sources_cache, _usage_cache = await _load_archive()
        return _archive_view()
    except Exception as exc:  # noqa: BLE001
        return TITLE, _failure(exc), _back_buttons()


async def _source_action(event, extra: str, chat_id: int):
    global _source_cache, _revisions_cache
    if not _owner_ok(event):
        return TITLE, "This panel is owner-only.", []
    try:
        _source_cache, _revisions_cache = await _load_source(extra)
        return _source_view(extra)
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
        source_id, revision_id = extra.split(":", 1)
    except ValueError:
        return TITLE, f"{sym.STATE_FAILED} That deletion link is not valid.", _back_buttons()
    return _delete_revision_confirm(source_id, revision_id)


async def _delete_source_commit(event, extra: str, chat_id: int, *, cascade: bool = False):
    if not _owner_ok(event):
        return TITLE, "This panel is owner-only.", []
    try:
        await get_evidence_service().delete_source(
            _OWNER_ID, extra, confirm=True, cascade_confirmed=cascade,
        )
        sources, usage = await _load_archive()
        globals()["_sources_cache"] = sources
        globals()["_usage_cache"] = usage
        return _archive_view(f"{sym.STATE_SUCCESS} The archived source was deleted.")
    except Exception as exc:  # noqa: BLE001
        return TITLE, _failure(exc), _back_buttons()


async def _delete_revision_commit(event, extra: str, chat_id: int, *, cascade: bool = False):
    if not _owner_ok(event):
        return TITLE, "This panel is owner-only.", []
    try:
        source_id, revision_id = extra.split(":", 1)
        await get_evidence_service().delete_revision(
            _OWNER_ID, revision_id, confirm=True, cascade_confirmed=cascade,
        )
        source, revisions = await _load_source(source_id)
        globals()["_source_cache"] = source
        globals()["_revisions_cache"] = revisions
        return _source_view(source_id, f"{sym.STATE_SUCCESS} The archive revision was deleted.")
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
    register_action("evidence_delete_source", _delete_source_action)
    register_action("evidence_delete_source_commit", _delete_source_commit_action)
    register_action("evidence_delete_source_cascade", _delete_source_cascade_action)
    register_action("evidence_delete_revision", _delete_revision_action)
    register_action("evidence_delete_revision_commit", _delete_revision_commit_action)
    register_action("evidence_delete_revision_cascade", _delete_revision_cascade_action)
