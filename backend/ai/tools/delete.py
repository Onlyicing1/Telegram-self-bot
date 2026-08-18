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
                "description": "Number of messages to delete (required, 1-500).",
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

        count = arguments.get("count")
        if not isinstance(count, int) or isinstance(count, bool) or count < 1 or count > 500:
            return ToolResult(
                success=False,
                message=(
                    "Delete requires an explicit count between 1 and 500 "
                    "(e.g. 'delete the last 5 messages'). No messages were deleted."
                ),
            )
        chat_id = context.extra.get("chat_id") if context.extra else None
        if chat_id is None:
            return ToolResult(success=False, message="No chat context for deletion.")

        try:
            deleted, error = await delete_service.do_del_n_counts(
                context.telegram.client, chat_id, count
            )
        except Exception as exc:
            return ToolResult(
                success=False,
                message=f"Delete failed: {exc}",
                data={"count": 0},
            )
        if error is not None:
            return ToolResult(
                success=False,
                message=f"Delete failed: {error}",
                data={"count": deleted},
            )
        if deleted == 0:
            return ToolResult(
                success=True,
                message="No outgoing messages were deleted (none matched in this chat).",
                data={"count": 0},
            )
        return ToolResult(
            success=True,
            message=f"Deleted {deleted} outgoing message(s) in this chat.",
            data={"count": deleted},
        )


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
        if not isinstance(message_id, int) or isinstance(message_id, bool):
            return ToolResult(
                success=False,
                message=("Delete by ID requires an explicit message ID. No messages were deleted."),
            )
        chat_id = context.extra.get("chat_id") if context.extra else None
        if chat_id is None:
            return ToolResult(success=False, message="No chat context for deletion.")

        try:
            deleted, error = await delete_service.do_del_id_counts(
                context.telegram.client, chat_id, message_id
            )
        except Exception as exc:
            return ToolResult(
                success=False,
                message=f"Delete by ID failed: {exc}",
                data={"message_id": message_id, "count": 0},
            )
        if error is not None:
            return ToolResult(
                success=False,
                message=f"Delete by ID failed: {error}",
                data={"message_id": message_id, "count": deleted},
            )
        if deleted == 0:
            return ToolResult(
                success=True,
                message="No outgoing messages were deleted (none matched from that ID forward).",
                data={"message_id": message_id, "count": 0},
            )
        return ToolResult(
            success=True,
            message=f"Deleted {deleted} outgoing message(s) starting from ID {message_id}.",
            data={"message_id": message_id, "count": deleted},
        )
