"""
Organize tools — wrap ``organize_service`` functions.

``OrganizeListTool`` is READ_ONLY. ``OrganizeCleanTool`` is DANGEROUS —
the AI must ask the owner for confirmation before purging old logs.
"""
from __future__ import annotations

from typing import Any

from backend.ai.tools.base import PermissionLevel, Tool, ToolResult, result_from_service
from backend.ai.tools.context import ToolContext


class OrganizeListTool(Tool):
    """Show an overview of saves, logs, and bio engine status."""

    def __init__(self, context: ToolContext) -> None:
        self._context = context

    @property
    def name(self) -> str:
        return "organize_list"

    @property
    def description(self) -> str:
        return "Show an overview of saved items, logs, and bio engine status."

    @property
    def parameters(self) -> dict[str, Any]:
        return {}

    @property
    def permission_level(self) -> PermissionLevel:
        return PermissionLevel.READ_ONLY

    @property
    def safe(self) -> bool:
        return True

    @property
    def return_type(self) -> str:
        return "ToolResult with overview text in message"

    async def execute(self, context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
        from backend.services import organize_service

        try:
            result = await organize_service.do_list(context.owner_id)
            return result_from_service(result)
        except Exception as exc:
            return ToolResult(success=False, message=f"Organize list failed: {exc}")


class OrganizeCleanTool(Tool):
    """Purge bot logs older than 7 days. Dangerous — requires confirmation."""

    def __init__(self, context: ToolContext) -> None:
        self._context = context

    @property
    def name(self) -> str:
        return "organize_clean"

    @property
    def description(self) -> str:
        return "Purge bot log entries older than 7 days."

    @property
    def parameters(self) -> dict[str, Any]:
        return {}

    @property
    def permission_level(self) -> PermissionLevel:
        return PermissionLevel.DANGEROUS

    @property
    def safe(self) -> bool:
        return False

    @property
    def return_type(self) -> str:
        return "ToolResult with deletion count in message"

    async def execute(self, context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
        from backend.services import organize_service

        try:
            result = await organize_service.do_clean(context.owner_id)
            return result_from_service(result)
        except Exception as exc:
            return ToolResult(success=False, message=f"Organize clean failed: {exc}")
