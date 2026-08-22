"""
Database status tool — wraps ``database_service.do_stats``.

The AI calls this tool to answer "what is my database status?" / "وضعیت
دیتابیس چیه؟". It is READ_ONLY and delegates entirely to the existing
database service. No logic is duplicated.
"""
from __future__ import annotations

from typing import Any

from backend.ai.tools.base import PermissionLevel, Tool, ToolResult, result_from_service
from backend.ai.tools.context import ToolContext


class DatabaseStatsTool(Tool):
    """Return aggregate saved-items database statistics."""

    def __init__(self, context: ToolContext) -> None:
        self._context = context

    @property
    def name(self) -> str:
        return "database_stats"

    @property
    def description(self) -> str:
        return (
            "Show database statistics: saved-item totals, media breakdown, "
            "size estimate, oldest/newest save date, and available AI/Ghost "
            "Room row counts."
        )

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
        return "ToolResult with database statistics text in message"

    async def execute(self, context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
        from backend.services import database_service

        try:
            result = await database_service.do_stats(context.owner_id, context.tz_str)
            return result_from_service(result)
        except Exception as exc:
            return ToolResult(success=False, message=f"Database stats failed: {exc}")
