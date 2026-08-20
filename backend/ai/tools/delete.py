"""
Delete tools — wrap ``delete_service.do_del_n`` and ``do_del_id``.

These tools are classified as DANGEROUS. The AI must ask the owner for
confirmation before calling them.
"""
from __future__ import annotations

from typing import Any

from backend.ai.persian import coerce_int
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
        return (
            "Delete self-owned messages in the current chat. Use count for the "
            "last N self messages, mode=all for all self messages, mode=until_time "
            "for a time range, or mode=until_message for a replied-to boundary. "
            "The active request message is always excluded."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "count": {
                "type": "integer",
                "minimum": 1,
                "maximum": 500,
                "description": "Number of self-owned messages to delete (1-500).",
            },
            "mode": {
                "type": "string",
                "enum": ["last_n", "all", "until_time", "until_message", "filtered"],
                "description": "Optional deletion scope; omit for the last N self messages.",
            },
            "until_time": {
                "type": "string",
                "description": "ISO timestamp or local HH:MM cutoff (inclusive range end).",
            },
            "after_time": {
                "type": "string",
                "description": "Optional ISO timestamp or local HH:MM range start.",
            },
            "boundary_id": {
                "type": "integer",
                "description": "Message ID boundary; the boundary is not permission to delete foreign messages.",
            },
            "query": {
                "type": "string",
                "description": "Optional text filter applied before ownership and deletion.",
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
        from backend.ai.persian import coerce_int
        from backend.services import delete_service

        count = coerce_int(arguments.get("count"))
        mode = str(arguments.get("mode") or "").strip().lower()
        until_time = arguments.get("until_time")
        after_time = arguments.get("after_time")
        query = str(arguments.get("query") or "")
        boundary_id = coerce_int(arguments.get("boundary_id"))
        request_message_id = coerce_int(
            context.extra.get("request_message_id") if context.extra else None
        )
        reply_meta = context.extra.get("reply_msg") if context.extra else None
        if mode == "until_message" and boundary_id is None and reply_meta:
            boundary_id = coerce_int(reply_meta.get("message_id"))
        if mode == "until_message" and boundary_id is None:
            return ToolResult(success=False, message="No message boundary could be resolved; nothing was deleted.")
        if mode == "until_time" and not until_time:
            return ToolResult(success=False, message="No cutoff time could be resolved; nothing was deleted.")
        has_filtered_scope = bool(
            mode in {"all", "until_time", "until_message", "filtered"}
            or until_time is not None or after_time is not None
            or boundary_id is not None or query
        )
        if not has_filtered_scope and count is None:
            return ToolResult(
                success=False,
                message=(
                    "Delete requires an explicit count between 1 and 500 "
                    "(e.g. 'delete the last 5 messages'). No messages were deleted."
                ),
            )
        if count is not None and (count < 1 or count > 500):
            return ToolResult(
                success=False,
                message="Delete count must be between 1 and 500. No messages were deleted.",
            )
        chat_id = context.extra.get("chat_id") if context.extra else None
        if chat_id is None:
            return ToolResult(success=False, message="No chat context for deletion.")

        client = None
        if context.telegram is not None:
            client = getattr(context.telegram, "client", None)
        if client is None:
            client = context.client
        if client is None:
            return ToolResult(success=False, message="No Telegram client available.")

        try:
            if has_filtered_scope or request_message_id is not None:
                considered, deleted, error = await delete_service.do_del_self_filtered(
                    client,
                    chat_id,
                    count=None if mode == "all" else count,
                    until_time=until_time,
                    after_time=after_time,
                    boundary_id=boundary_id,
                    query=query,
                    exclude_message_id=request_message_id,
                    tz_name=context.tz_str,
                )
            else:
                # Preserve the legacy panel/test contract when no AI request
                # ID is present; live AI requests always use the self-owned
                # selector above so the active request cannot be counted.
                considered, deleted, error = await delete_service.do_del_last_n_real(
                    client, chat_id, count
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
                message=(
                    f"Considered the last {considered} message(s) in this chat, "
                    "but none were sent by the owner, so nothing was deleted."
                ),
                data={"count": 0, "considered": considered},
            )
        return ToolResult(
            success=True,
            message=(
                f"Deleted {deleted} outgoing message(s) from the last "
                f"{considered} message(s) in this chat."
            ),
            data={"count": deleted, "considered": considered},
        )


class DeleteRepliedTool(Tool):
    """Delete the specific message the owner replied to (outgoing-only).

    Resolves the target deterministically from the runtime reply context —
    the AI never asks the owner for a message ID when "this message" is the
    replied-to message. Deletion follows the project's outgoing-only rule:
    only the owner's own sent messages can be deleted.
    """

    def __init__(self, context: ToolContext) -> None:
        self._context = context

    @property
    def name(self) -> str:
        return "delete_replied"

    @property
    def description(self) -> str:
        return (
            "Delete the message the owner replied to in the current chat. "
            "Use this when the owner says 'delete this message' / 'اینو پاک کن' "
            "while replying to the target message. Outgoing (owner-sent) messages only."
        )

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
        return "ToolResult with deleted message id in data"

    async def execute(self, context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
        reply_meta = context.extra.get("reply_msg") if context.extra else None
        if not reply_meta:
            return ToolResult(
                success=False,
                message="No replied message to delete. Reply to a message first.",
            )
        chat_id = reply_meta.get("chat_id")
        message_id = reply_meta.get("message_id")
        if not chat_id or not message_id:
            return ToolResult(success=False, message="Could not resolve the replied message target.")

        client = None
        if context.telegram is not None:
            client = getattr(context.telegram, "client", None)
        if client is None:
            client = context.client
        if client is None:
            return ToolResult(success=False, message="No Telegram client available.")

        try:
            msg = await client.get_messages(chat_id, ids=message_id)
        except Exception as exc:
            return ToolResult(
                success=False,
                message=f"Could not fetch the replied message: {exc}",
            )
        if msg is None:
            return ToolResult(success=False, message="Replied message not found.")
        request_message_id = context.extra.get("request_message_id") if context.extra else None
        if request_message_id is not None and message_id == request_message_id:
            return ToolResult(success=False, message="The active Delete request cannot be its own target.")
        if not getattr(msg, "out", False):
            return ToolResult(
                success=False,
                message="That message was not sent by the owner, so it cannot be deleted (outgoing-only).",
            )

        from backend.services import delete_service

        try:
            deleted, _rejected = await delete_service.delete_verified_self_messages(
                client, chat_id, [message_id],
                exclude_message_id=coerce_int(request_message_id),
            )
        except Exception as exc:
            return ToolResult(
                success=False,
                message=f"Delete failed: {exc}",
                data={"message_id": message_id, "count": 0},
            )
        if not deleted:
            return ToolResult(
                success=False,
                message=(
                    "That message could not be verified as outgoing, so it was not deleted."
                ),
                data={"message_id": message_id, "count": 0},
            )
        return ToolResult(
            success=True,
            message="Deleted the replied message.",
            data={"message_id": message_id, "count": 1},
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
        from backend.ai.persian import coerce_int
        from backend.services import delete_service

        message_id = coerce_int(arguments.get("message_id"))
        if message_id is None:
            return ToolResult(
                success=False,
                message=("Delete by ID requires an explicit message ID. No messages were deleted."),
            )
        chat_id = context.extra.get("chat_id") if context.extra else None
        if chat_id is None:
            return ToolResult(success=False, message="No chat context for deletion.")

        request_message_id = coerce_int(
            context.extra.get("request_message_id") if context.extra else None
        )
        if request_message_id is not None and message_id == request_message_id:
            return ToolResult(success=False, message="The active Delete request cannot be its own target.")
        client = context.telegram.client if context.telegram is not None else context.client
        if client is None:
            return ToolResult(success=False, message="No Telegram client available.")
        try:
            deleted, error = await delete_service.do_del_id_counts(
                client, chat_id, message_id, exclude_message_id=request_message_id
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


class DeleteMessageByIdTool(Tool):
    """Delete exactly one outgoing message identified by its message ID.

    Deterministic single-message target: fetch the ID, enforce the
    outgoing-only rule, then delete that one message. Unlike
    ``DeleteByIdTool`` (which deletes from an ID *forward*), this deletes
    a single explicit target and never escalates into a range.
    """

    def __init__(self, context: ToolContext) -> None:
        self._context = context

    @property
    def name(self) -> str:
        return "delete_message_by_id"

    @property
    def description(self) -> str:
        return (
            "Delete one specific outgoing message by its message ID in the "
            "current chat. Outgoing (owner-sent) messages only."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "message_id": {
                "type": "integer",
                "description": "The concrete message ID to delete (from context, not invented).",
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
        return "ToolResult with deleted message id in data"

    async def execute(self, context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
        from backend.ai.persian import coerce_int

        message_id = coerce_int(arguments.get("message_id"))
        if message_id is None or message_id <= 0:
            return ToolResult(success=False, message="A valid message ID is required.")

        chat_id = context.extra.get("chat_id") if context.extra else None
        if chat_id is None:
            return ToolResult(success=False, message="No chat context for deletion.")

        client = None
        if context.telegram is not None:
            client = getattr(context.telegram, "client", None)
        if client is None:
            client = context.client
        if client is None:
            return ToolResult(success=False, message="No Telegram client available.")

        try:
            msg = await client.get_messages(chat_id, ids=message_id)
        except Exception as exc:
            return ToolResult(success=False, message=f"Could not fetch message {message_id}: {exc}")
        if msg is None:
            return ToolResult(success=False, message=f"Message {message_id} not found in this chat.")
        request_message_id = context.extra.get("request_message_id") if context.extra else None
        if request_message_id is not None and message_id == request_message_id:
            return ToolResult(success=False, message="The active Delete request cannot be its own target.")
        if not getattr(msg, "out", False):
            return ToolResult(
                success=False,
                message=(
                    f"Message {message_id} was not sent by the owner, so it cannot "
                    "be deleted (outgoing-only)."
                ),
            )

        from backend.services import delete_service

        try:
            deleted, _rejected = await delete_service.delete_verified_self_messages(
                client, chat_id, [message_id],
                exclude_message_id=coerce_int(request_message_id),
            )
        except Exception as exc:
            return ToolResult(
                success=False,
                message=f"Delete failed: {exc}",
                data={"message_id": message_id, "count": 0},
            )
        if not deleted:
            return ToolResult(
                success=False,
                message=(
                    f"Message {message_id} could not be verified as outgoing, "
                    "so it was not deleted."
                ),
                data={"message_id": message_id, "count": 0},
            )
        return ToolResult(
            success=True,
            message=f"Deleted message {message_id}.",
            data={"message_id": message_id, "count": 1},
        )
