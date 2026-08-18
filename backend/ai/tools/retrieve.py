"""
Retrieve tools — wrap ``discover_service`` functions.

These tools let the AI search saved items and list recent saves.
"""
from __future__ import annotations

from typing import Any

from backend.ai.tools.base import PermissionLevel, Tool, ToolResult, result_from_service
from backend.ai.tools.context import ToolContext


class SearchTool(Tool):
    """Search saved items by keyword."""

    def __init__(self, context: ToolContext) -> None:
        self._context = context

    @property
    def name(self) -> str:
        return "search"

    @property
    def description(self) -> str:
        return "Search saved items by keyword query."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "query": {
                "type": "string",
                "description": "Search query to find in saved items.",
            },
        }

    @property
    def permission_level(self) -> PermissionLevel:
        return PermissionLevel.READ_ONLY

    @property
    def safe(self) -> bool:
        return True

    @property
    def return_type(self) -> str:
        return "ToolResult with search results text in message"

    async def execute(self, context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
        from backend.services import discover_service

        query = arguments.get("query")
        if not query:
            return ToolResult(success=False, message="Missing query argument.")
        try:
            result = await discover_service.do_find(context.owner_id, query, context.tz_str)
            return result_from_service(result, data={"query": query})
        except Exception as exc:
            return ToolResult(success=False, message=f"Search failed: {exc}")


class ListSavesTool(Tool):
    """List recent saved items."""

    def __init__(self, context: ToolContext) -> None:
        self._context = context

    @property
    def name(self) -> str:
        return "list_saves"

    @property
    def description(self) -> str:
        return "List recent saved items."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": 50,
                "default": 10,
                "description": "Maximum number of items to list.",
            },
        }

    @property
    def permission_level(self) -> PermissionLevel:
        return PermissionLevel.READ_ONLY

    @property
    def safe(self) -> bool:
        return True

    @property
    def return_type(self) -> str:
        return "ToolResult with saved items list text in message"

    async def execute(self, context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
        from backend.services import discover_service

        limit = arguments.get("limit", 10)
        try:
            result = await discover_service.do_list(context.owner_id, limit, context.tz_str)
            return result_from_service(result, data={"limit": limit})
        except Exception as exc:
            return ToolResult(success=False, message=f"List saves failed: {exc}")
