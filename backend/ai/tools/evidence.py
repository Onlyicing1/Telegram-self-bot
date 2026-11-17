"""Owner-facing Evidence AI tools (G4-A/F1).

``cited_summary`` is the explicit AI entry point for an opt-in Cited Summary.
Like ``archive_evidence`` it is CONFIRMATION_REQUIRED, so the executor never
auto-runs it: a citation request is opted into by the owner for that one
request and is never remembered.  Both tools are thin shims over
``backend.evidence.service``; neither retrieves Telegram history itself, builds
links, or decides authorization.

The generic ToolResult boundary is intentionally narrow: it may carry the
generated summary (not the Evidence Envelope) and bounded run metadata.  It
never carries Telegram/message/owner/Workspace ids, source/revision ids,
content hashes, excerpts, or Telegram links.  The owner-facing Explorer is the
only surface that resolves local links.
"""
from __future__ import annotations

import logging
from typing import Any

from backend.ai import language as language_ai
from backend.ai.tools.base import PermissionLevel, Tool, ToolResult
from backend.ai.tools.context import ToolContext
from backend.evidence import constants
from backend.evidence.models import EvidenceError
from backend.evidence.service import get_evidence_service
from backend.helper import symbols as sym

logger = logging.getLogger(__name__)


def _chat_id(context: ToolContext) -> int | None:
    extra = context.extra if context is not None else None
    value = extra.get("chat_id") if extra else None
    try:
        chat = int(value)
    except (TypeError, ValueError):
        return None
    return chat or None


def _source(context: ToolContext) -> Any:
    return getattr(context, "telegram", None) or getattr(context, "client", None)


def _provider_manager(context: ToolContext) -> Any:
    extra = context.extra if context is not None else None
    return extra.get("provider_manager") if extra else None


def _request_id(context: ToolContext) -> str:
    extra = context.extra if context is not None else None
    return str(extra.get("request_id") or "") if extra else ""


def _request_timeout(context: ToolContext) -> Any:
    extra = context.extra if context is not None else None
    return extra.get("request_timeout_s") if extra else None


def _current_message_id(context: ToolContext) -> int | None:
    extra = context.extra if context is not None else None
    if not extra:
        return None
    for key in ("request_message_id", "current_message_id", "message_id"):
        try:
            value = int(extra.get(key))
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    return None


def _peer_name(context: ToolContext) -> str:
    extra = context.extra if context is not None else None
    value = extra.get("chat_name") if extra else ""
    return " ".join(str(value or "").split())[: constants.MAX_PEER_NAME_CHARS]


def _global_language(context: ToolContext) -> str:
    extra = context.extra if context is not None else None
    value = extra.get("global_language") if extra else None
    if isinstance(value, str) and value:
        return value
    return language_ai.global_preference(int(getattr(context, "owner_id", 0) or 0))


class CitedSummaryTool(Tool):
    """Create an opt-in, cited summary and durable Active Evidence Run."""

    def __init__(self, context: ToolContext) -> None:
        self._context = context

    @property
    def name(self) -> str:
        return "cited_summary"

    @property
    def description(self) -> str:
        return (
            "Create an explicit Cited Summary of the most recent messages in this "
            "chat. This is opt-in and requires the owner's explicit confirmation "
            "for this request; ordinary summarization must use summarize_history. "
            "The result is backed by a temporary active evidence run (default 7 "
            "days, 1–30 selectable), stores bounded excerpts, and returns a summary "
            "plus opaque run metadata. Citations are resolved locally in History → "
            "Evidence Explorer; this tool never returns source ids, message ids, "
            "links or hashes."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "count": {
                "type": "integer",
                "minimum": 1,
                "maximum": constants.MAX_CITED_COUNT,
                "default": constants.DEFAULT_CITED_COUNT,
                "description": "Most recent messages to cite (at most 50).",
            },
            "ttl_days": {
                "type": "integer",
                "minimum": constants.MIN_TTL_DAYS,
                "maximum": constants.MAX_TTL_DAYS,
                "default": constants.DEFAULT_TTL_DAYS,
                "description": "Active-run lifetime in days (1–30).",
            },
            "language": {
                "type": "string",
                "enum": [*language_ai.TASK_CODES, language_ai.AUTO],
                "description": "Output language; omit for the owner's global language.",
            },
            "instruction": {
                "type": "string",
                "description": "Optional focus instruction, at most 500 characters.",
            },
        }

    @property
    def permission_level(self) -> PermissionLevel:
        return PermissionLevel.CONFIRMATION_REQUIRED

    @property
    def safe(self) -> bool:
        return False

    @property
    def long_running(self) -> bool:
        return True

    @property
    def return_type(self) -> str:
        return (
            "ToolResult whose message is the generated cited-summary text; data "
            "carries only the opaque run_id plus requested/processed/truncated/"
            "refs/citations/ttl_days. Citations are available only in the local "
            "owner-facing Evidence Explorer."
        )

    async def execute(self, context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
        chat_id = _chat_id(context)
        manager = _provider_manager(context)
        if not chat_id:
            return ToolResult(success=False, message="No chat context available.")
        if manager is None:
            return ToolResult(success=False, message="The AI engine is not running.")
        try:
            try:
                count = int(arguments.get("count") or constants.DEFAULT_CITED_COUNT)
                ttl_days = int(arguments.get("ttl_days") or constants.DEFAULT_TTL_DAYS)
            except (TypeError, ValueError):
                return ToolResult(
                    success=False,
                    message="Cited Summary needs whole-number count and ttl_days values.",
                    data={"operation": "cited_summary", "error": "validation"},
                )
            result = await get_evidence_service().run_cited_summary(
                _source(context),
                owner_id=int(getattr(context, "owner_id", 0) or 0),
                peer_id=chat_id,
                peer_name=_peer_name(context),
                count=count,
                ttl_days=ttl_days,
                instruction=str(arguments.get("instruction") or ""),
                language=str(arguments.get("language") or _global_language(context)),
                provider_manager=manager,
                current_message_id=_current_message_id(context),
                timeout_s=_request_timeout(context),
                request_id=_request_id(context),
                # The executor only reaches this method after the owner
                # confirmed THIS call; consent is never carried forward.
                opt_in=True,
            )
        except EvidenceError as exc:
            logger.info(
                "AI_EXEC_TRACE request_id=%s stage=evidence_tool_failed tool=%s code=%s",
                _request_id(context) or "-", self.name, exc.code,
            )
            return ToolResult(
                success=False,
                message=f"{sym.STATE_FAILED} Cited Summary failed: {exc}",
                data={"operation": "cited_summary", "error": exc.code},
            )
        except Exception as exc:  # noqa: BLE001 — tool boundary
            logger.warning(
                "AI_EXEC_TRACE request_id=%s stage=evidence_tool_exception tool=%s error=%s",
                _request_id(context) or "-", self.name, type(exc).__name__,
            )
            return ToolResult(
                success=False,
                message=f"{sym.STATE_FAILED} Cited Summary failed ({type(exc).__name__}).",
                data={"operation": "cited_summary", "error": "tool_exception"},
            )
        data = result.data
        # Deliberately no message ids, links, hashes, excerpt text or citation
        # keys cross the generic ToolResult boundary.
        safe_data = {
            "operation": "cited_summary",
            "run_id": result.run_id,
            "requested": int(data.get("requested") or 0),
            "processed": int(data.get("processed") or 0),
            "truncated": bool(data.get("truncated")),
            "refs": int(data.get("refs") or 0),
            "citations": len(result.citations),
            "ttl_days": int(data.get("ttl_days") or 0),
        }
        message = result.summary
        return ToolResult(success=True, message=message, data=safe_data)


class ArchiveEvidenceTool(Tool):
    """Archive one reviewed evidence reference after explicit confirmation."""

    def __init__(self, context: ToolContext) -> None:
        self._context = context

    @property
    def name(self) -> str:
        return "archive_evidence"

    @property
    def description(self) -> str:
        return (
            "Archive one owner-reviewed reference from an existing active evidence "
            "run. The archive is append-only and creates an immutable revision; "
            "the same content is a no-op, and changed content requires this same "
            "explicit owner confirmation. Requires the opaque run_id and the "
            "reference number (1–50) shown in Evidence Explorer. This operation "
            "requires owner confirmation before it runs."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "run_id": {
                "type": "string",
                "description": "Opaque active evidence run id (never a Telegram id).",
            },
            "ref": {
                "type": "integer",
                "minimum": 1,
                "maximum": constants.MAX_REFS_PER_RUN,
                "description": "Reference number shown as E1…E50 in Evidence Explorer.",
            },
        }

    @property
    def permission_level(self) -> PermissionLevel:
        return PermissionLevel.CONFIRMATION_REQUIRED

    @property
    def safe(self) -> bool:
        return False

    @property
    def required_arguments(self) -> tuple[str, ...]:
        return ("run_id", "ref")

    @property
    def return_type(self) -> str:
        return (
            "ToolResult with status created/unchanged and revision_no; no source "
            "or revision ids, hashes, links or message ids are returned."
        )

    async def execute(self, context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
        run_id = str(arguments.get("run_id") or "").strip()
        try:
            ref = int(arguments.get("ref"))
        except (TypeError, ValueError):
            return ToolResult(success=False, message="A reference number is required.")
        if not run_id:
            return ToolResult(success=False, message="An active evidence run id is required.")
        try:
            result = await get_evidence_service().archive_ref(
                int(getattr(context, "owner_id", 0) or 0),
                run_id,
                f"E{ref}",
                approved=True,
            )
        except EvidenceError as exc:
            return ToolResult(
                success=False,
                message=f"{sym.STATE_FAILED} Archive failed: {exc}",
                data={"operation": "archive_evidence", "error": exc.code},
            )
        except Exception as exc:  # noqa: BLE001 — tool boundary
            logger.warning(
                "EVIDENCE_ARCHIVE_TOOL_FAILED error=%s", type(exc).__name__,
            )
            return ToolResult(
                success=False,
                message=f"{sym.STATE_FAILED} Archive failed ({type(exc).__name__}).",
                data={"operation": "archive_evidence", "error": "tool_exception"},
            )
        message = (
            "Archived as an immutable revision."
            if result.status == "created"
            else "That exact excerpt is already archived; nothing was overwritten."
        )
        return ToolResult(
            success=True,
            message=message,
            data={
                "operation": "archive_evidence",
                "run_id": run_id,
                "status": result.status,
                "revision_no": result.revision_no,
                "ref": ref,
            },
        )


__all__ = ["ArchiveEvidenceTool", "CitedSummaryTool"]
