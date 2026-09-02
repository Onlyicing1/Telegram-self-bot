"""Saved-item retrieval tool — wrap ``retrieve_service.do_retrieve``.

The AI can re-send a saved item (media + metadata caption) to the owner.
Destination is resolved from TRUSTED runtime context — the chat the AI
request came from — never from model output, mirroring ``SendMessageTool``.
The save code is owner-scoped through the existing service/DB contract.
"""
from __future__ import annotations

from typing import Any

from backend.ai.tools.base import PermissionLevel, Tool, ToolResult, result_from_service
from backend.ai.tools.context import ToolContext


class RetrieveSaveTool(Tool):
    """Re-send a saved item to the current chat (the panel's Retrieve action)."""

    def __init__(self, context: ToolContext) -> None:
        self._context = context

    @property
    def name(self) -> str:
        return "retrieve_save"

    @property
    def description(self) -> str:
        return (
            "Re-send a saved item (by its save code, e.g. S0001) into the "
            "current chat with its metadata caption. The destination is always "
            "the chat the request came from — never a user-supplied chat."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "save_code": {
                "type": "string",
                "description": "The save code of the item to retrieve (from search/list_saves).",
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
        return "ToolResult with the retrieval confirmation or honest failure"

    async def execute(self, context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
        from backend.services import retrieve_service

        save_code = str(arguments.get("save_code") or "").strip()
        if not save_code:
            return ToolResult(success=False, message="A save code is required (e.g. S0001).")

        chat_id = context.extra.get("chat_id") if context.extra else None
        if not isinstance(chat_id, int) or chat_id == 0:
            return ToolResult(
                success=False,
                message="No trusted destination chat is available; nothing was retrieved.",
            )

        client = None
        if context.telegram is not None:
            client = getattr(context.telegram, "client", None)
        if client is None:
            client = context.client
        if client is None:
            return ToolResult(success=False, message="No Telegram client available.")

        try:
            result = await retrieve_service.do_retrieve(client, context.owner_id, save_code, chat_id)
        except Exception as exc:  # noqa: BLE001
            return ToolResult(success=False, message=f"Retrieve failed: {exc}")
        return result_from_service(result, data={"save_code": save_code, "chat_id": chat_id})
