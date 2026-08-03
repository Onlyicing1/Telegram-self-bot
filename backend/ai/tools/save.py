"""
Save tool — wraps ``save_service.execute_save``.

The AI calls this tool to save a message to Saved Messages. The tool
delegates entirely to the existing save service. No logic is duplicated.
"""
from __future__ import annotations

from typing import Any

from backend.ai.tools.base import PermissionLevel, Tool, ToolResult
from backend.ai.tools.context import ToolContext


class SaveTool(Tool):
    """Save a replied message to Saved Messages.

    Arguments:
        mode:  ``"forward"`` or ``"deep"`` (default: ``"forward"``).
    """

    def __init__(self, context: ToolContext) -> None:
        self._context = context

    @property
    def name(self) -> str:
        return "save"

    @property
    def description(self) -> str:
        return "Save a message to Saved Messages. Requires a replied message."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "mode": {
                "type": "string",
                "enum": ["forward", "deep"],
                "default": "forward",
                "description": "Save mode: 'forward' (instant) or 'deep' (download + re-upload).",
            },
        }

    @property
    def permission_level(self) -> PermissionLevel:
        return PermissionLevel.READ_WRITE

    @property
    def safe(self) -> bool:
        return True

    @property
    def return_type(self) -> str:
        return "ToolResult with save_code and confirmation message in data"

    async def execute(self, context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
        from backend.services import save_service

        mode = arguments.get("mode", "forward")
        reply_msg = context.extra.get("reply_msg") if context.extra else None
        if reply_msg is None:
            return ToolResult(success=False, message="No replied message to save.")

        try:
            result = await save_service.execute_save(
                context.client, context.owner_id, reply_msg, mode, context.tz_str
            )
            return ToolResult(success=True, message=result, data={"mode": mode})
        except Exception as exc:
            return ToolResult(success=False, message=f"Save failed: {exc}")
