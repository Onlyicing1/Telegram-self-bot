"""Saved-item management tools — wrap the existing ``retrieve_service``.

Three thin wrappers over the SAME service operations the retrieve panels
already use, so the AI-facing management surface is the panel surface:

  - ``retrieve_save`` — re-send the item (media + metadata caption)
  - ``preview_save``  — read the item's stored metadata by save code
  - ``delete_save``   — remove the item (its saved copy + its DB row)

Destinations are resolved from TRUSTED runtime context — the chat the AI
request came from — never from model output, mirroring ``SendMessageTool``.
The save code is owner-scoped through the existing service/DB contract:
the authenticated owner identity always comes from ``context.owner_id``,
never from tool arguments.
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
    def required_arguments(self) -> tuple[str, ...]:
        return ("save_code",)

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

        # Model/provider output is normalized at the tool boundary: the
        # service looks codes up verbatim and DB codes are stored upper-case,
        # so a lower-cased echo ("s0012") must be canonicalized here. Codes
        # are `S` + alphanumerics (see db.client._SHORT_CODE_ALPHABET).
        save_code = str(arguments.get("save_code") or "").strip().upper()
        if not save_code or not all(ch.isalnum() for ch in save_code):
            return ToolResult(
                success=False,
                message="A valid save code is required (e.g. S0001).",
            )

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


class PreviewSaveTool(Tool):
    """Read ONE saved item's stored metadata by its save code.

    Delegates to ``retrieve_service.do_preview`` — the exact function the
    retrieve panel's manual code entry already uses. Read-only: no Telegram
    side effect, no re-send, no DB write.
    """

    def __init__(self, context: ToolContext) -> None:
        self._context = context

    @property
    def name(self) -> str:
        return "preview_save"

    @property
    def required_arguments(self) -> tuple[str, ...]:
        return ("save_code",)

    @property
    def description(self) -> str:
        return (
            "Show the stored metadata of ONE saved item by its save code "
            "(e.g. S0001): type, format, size, sender and save date. Use this "
            "when the owner asks for the details of a specific saved item. It "
            "never re-sends the file — use retrieve_save for that."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "save_code": {
                "type": "string",
                "description": "The save code of the item to preview (from search/list_saves).",
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
        return "ToolResult with the item's metadata text in message"

    async def execute(self, context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
        from backend.services import retrieve_service

        save_code = str(arguments.get("save_code") or "").strip().upper()
        if not save_code or not all(ch.isalnum() for ch in save_code):
            return ToolResult(
                success=False,
                message="A valid save code is required (e.g. S0001).",
            )

        client = None
        if context.telegram is not None:
            client = getattr(context.telegram, "client", None)
        if client is None:
            client = context.client

        try:
            # ``do_preview`` reads only the owner's saved-items row; the
            # signature keeps the client for symmetry with the other item
            # operations and never uses it.
            result = await retrieve_service.do_preview(client, context.owner_id, save_code)
        except Exception as exc:  # noqa: BLE001
            return ToolResult(success=False, message=f"Preview failed: {exc}")
        return result_from_service(result, data={"save_code": save_code})


class DeleteSaveTool(Tool):
    """Delete a saved item: its Saved Messages copy and its DB row.

    Delegates to ``retrieve_service.do_delete`` — the same owner-scoped
    operation the retrieve panel's Delete action uses. This is Saved-Items
    management, NOT message deletion: the original message in the origin
    chat is never touched.
    """

    def __init__(self, context: ToolContext) -> None:
        self._context = context

    @property
    def name(self) -> str:
        return "delete_save"

    @property
    def required_arguments(self) -> tuple[str, ...]:
        return ("save_code",)

    @property
    def description(self) -> str:
        return (
            "Delete a saved item by its save code (e.g. S0001): removes the "
            "item from Saved Items and deletes its Saved Messages copy. Use "
            "this for 'delete saved item S0001' — the original message in the "
            "source chat is never touched."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "save_code": {
                "type": "string",
                "description": "The save code of the item to delete (from search/list_saves).",
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
        return "ToolResult with the deletion confirmation or honest failure"

    async def execute(self, context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
        from backend.services import retrieve_service

        save_code = str(arguments.get("save_code") or "").strip().upper()
        if not save_code or not all(ch.isalnum() for ch in save_code):
            return ToolResult(
                success=False,
                message="A valid save code is required (e.g. S0001). Nothing was deleted.",
            )

        client = None
        if context.telegram is not None:
            client = getattr(context.telegram, "client", None)
        if client is None:
            client = context.client
        if client is None:
            return ToolResult(
                success=False,
                message="No Telegram client available; the saved item was not deleted.",
            )

        try:
            result = await retrieve_service.do_delete(client, context.owner_id, save_code)
        except Exception as exc:  # noqa: BLE001
            return ToolResult(success=False, message=f"Delete failed: {exc}")
        return result_from_service(result, data={"save_code": save_code})
