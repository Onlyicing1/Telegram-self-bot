"""Registered action for a scheduled History checkpoint.

The action is intentionally argument-thin: the peer, baseline and durable state
come from the task's server-created ``history_schedules`` row, addressed by the
coordinator-injected task id AND scheduled-occurrence key. Both trusted markers
are required; the model cannot create this action through the natural-language
task path and cannot supply an identity through tool arguments.
"""
from __future__ import annotations

import logging
from typing import Any

from backend.ai.task_contract import (
    SCHEDULED_HISTORY_ACTION,
    scheduled_occurrence_provenance,
)
from backend.ai.task_trace import occurrence_trace
from backend.ai.tools.base import PermissionLevel, Tool, ToolResult
from backend.ai.tools.context import ToolContext
from backend.services import history_schedule_service

logger = logging.getLogger(__name__)


def _source(context: ToolContext) -> Any:
    return getattr(context, "telegram", None) or getattr(context, "client", None)


def _provider_manager(context: ToolContext) -> Any:
    extra = context.extra if context is not None else None
    return extra.get("provider_manager") if extra else None


def _request_id(context: ToolContext) -> str:
    extra = context.extra if context is not None else None
    return str(extra.get("request_id") or "") if extra else ""


class ScheduledHistoryTool(Tool):
    """Advance the durable History checkpoint owned by the current task."""

    def __init__(self, context: ToolContext) -> None:
        self._context = context

    @property
    def name(self) -> str:
        return SCHEDULED_HISTORY_ACTION

    @property
    def description(self) -> str:
        return (
            "Internal scheduled History action. It advances the owner-created "
            "History checkpoint for the current task; it takes no identity, peer "
            "or cursor argument and is unavailable outside its scheduled "
            "occurrence."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "instruction": {
                "type": "string",
                "description": (
                    "Optional owner-authored focus instruction stored with the "
                    "owner-created schedule. Never a peer, task or message id."
                ),
            },
        }

    @property
    def permission_level(self) -> PermissionLevel:
        return PermissionLevel.READ_WRITE

    @property
    def safe(self) -> bool:
        return True

    @property
    def long_running(self) -> bool:
        """One bounded batch can legitimately outlive the generic 10s timeout."""
        return True

    @property
    def return_type(self) -> str:
        return (
            "ToolResult whose message is the cumulative summary when new text "
            "was committed; data carries processed/scanned/summarized/has_more "
            "and the committed revision/version."
        )

    async def execute(self, context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
        occurrence_trace("tool_invocation", action=self.name)
        extra = context.extra if context is not None else None
        # Both markers are coordinator-injected from the claimed occurrence.
        # A task id alone, a bare boolean/old marker, a marker belonging to
        # another task, or any model-supplied identity fails closed HERE —
        # before repository lookup or Telegram/history retrieval.
        task_key, _occurrence_key, provenance_error = (
            scheduled_occurrence_provenance(extra)
        )
        if provenance_error:
            occurrence_trace(
                "provenance_result", outcome="rejected",
                category=provenance_error,
            )
            return ToolResult(
                success=False,
                message=(
                    "This action is only available inside its scheduled task "
                    "occurrence."
                ),
                data={"error": f"scheduled_history_provenance_{provenance_error}"},
            )
        occurrence_trace(
            "provenance_result", outcome="passed", task_marker=True,
            occurrence_marker=True,
        )

        if not isinstance(arguments, dict) or set(arguments) - {"instruction"}:
            occurrence_trace(
                "arguments_result", outcome="rejected",
                category="arguments_invalid",
            )
            return ToolResult(
                success=False,
                message=(
                    "The History action accepts only the owner-authored "
                    "instruction argument."
                ),
                data={"error": "scheduled_history_arguments_invalid"},
            )
        instruction = arguments.get("instruction")
        if instruction is not None and not isinstance(instruction, str):
            occurrence_trace(
                "arguments_result", outcome="rejected",
                category="instruction_invalid",
            )
            return ToolResult(
                success=False,
                message="The History schedule instruction must be text.",
                data={"error": "scheduled_history_instruction_invalid"},
            )
        occurrence_trace(
            "arguments_result", outcome="passed",
            instruction_present=bool(instruction),
        )

        occurrence_trace("service_invocation_started")
        try:
            ok, message, data = await history_schedule_service.run_scheduled_history(
                _source(context),
                owner_id=int(getattr(context, "owner_id", 0) or 0),
                task_id=task_key,
                instruction=instruction or "",
                provider_manager=_provider_manager(context),
                request_id=_request_id(context),
            )
        except Exception as exc:  # noqa: BLE001 — tool boundaries never crash the chain
            occurrence_trace(
                "service_result", outcome="exception",
                exception=type(exc).__name__, category="service_exception",
            )
            logger.warning(
                "HISTORY_SCHEDULE_ACTION_FAILED task_id=%s error=%s",
                task_key, type(exc).__name__,
            )
            return ToolResult(
                success=False,
                message=f"Scheduled History failed: {type(exc).__name__}: {exc}",
                data={"error": "scheduled_history_failed"},
            )
        occurrence_trace(
            "service_result", outcome="success" if ok else "failed",
            error_category=(data or {}).get("error", "-"),
            processed=(data or {}).get("processed", "-"),
        )
        return ToolResult(success=ok, message=message, data=data)
