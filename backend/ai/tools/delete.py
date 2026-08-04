"""
Delete tools — wrap ``delete_service.do_del_n`` and ``do_del_id``.

These tools are classified as DANGEROUS. The AI must ask the owner for
confirmation before calling them.
"""
from __future__ import annotations

from typing import Any

from backend.ai.tools.base import PermissionLevel, Tool, ToolResult
from backend.ai.tools.context import ToolContext


class DeleteTool(Tool):
    """Delete the last N outgoing messages in the current chat.

    Arguments:
        count:  Number of messages to delete (1-500).
    """

    def __init__(self, context: ToolContext) -> None:
        self._context = context

    @property
    def name(self) -> str:
        return "delete"

    @property
    def description(self) -> str:
        return "Delete the last N outgoing messages in the current chat."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "count": {
                "type": "integer",
                "minimum": 1,
                "maximum": 500,
                "default": 1,
                "description": "Number of messages to delete.",
            },
        }

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
        from backend.services import delete_service

        count = arguments.get("count", 1)
        chat_id = context.extra.get("chat_id") if context.extra else None
        if chat_id is None:
            return ToolResult(success=False, message="No chat context for deletion.")

        try:
            result = await delete_service.do_del_n(context.telegram.client, chat_id, count)
            return ToolResult(success=True, message=result, data={"count": count})
        except Exception as exc:
            return ToolResult(success=False, message=f"Delete failed: {exc}")


class DeleteByIdTool(Tool):
    """Delete all messages from a given message ID onward.

    Arguments:
        message_id:  The starting message ID to delete from.
    """

    def __init__(self, context: ToolContext) -> None:
        self._context = context

    @property
    def name(self) -> str:
        return "delete_by_id"

    @property
    def description(self) -> str:
        return "Delete all outgoing messages from a given message ID onward in the current chat."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "message_id": {
                "type": "integer",
                "description": "Starting message ID to delete from.",
            },
        }

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
        from backend.services import delete_service

        message_id = arguments.get("message_id")
        if message_id is None:
            return ToolResult(success=False, message="Missing message_id argument.")
        chat_id = context.extra.get("chat_id") if context.extra else None
        if chat_id is None:
            return ToolResult(success=False, message="No chat context for deletion.")

        try:
            result = await delete_service.do_del_id(context.telegram.client, chat_id, message_id)
            return ToolResult(success=True, message=result, data={"message_id": message_id})
        except Exception as exc:
            return ToolResult(success=False, message=f"Delete by ID failed: {exc}")
