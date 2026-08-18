"""
Save tool — wraps ``save_service.execute_save``.

The AI calls this tool to save a message to Saved Messages. The tool
delegates entirely to the existing save service. No logic is duplicated.
"""
from __future__ import annotations

import logging
from typing import Any

from backend.ai.tools.base import PermissionLevel, Tool, ToolResult, result_from_service
from backend.ai.tools.context import ToolContext

logger = logging.getLogger(__name__)


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

        # ``execute_save`` expects the compact mode codes used by the save
        # panel: "f" = forward, "d" = deep (download + re-upload). The AI
        # tool surface uses friendly names — map them so "forward" never
        # silently becomes a deep save (and vice versa).
        mode_arg = arguments.get("mode", "forward")
        mode = "f" if str(mode_arg).lower().startswith("f") else "d"

        reply_meta = context.extra.get("reply_msg") if context.extra else None
        if reply_meta is None:
            return ToolResult(success=False, message="No replied message to save.")

        reply_msg = await self._resolve_reply_message(context, reply_meta)
        if reply_msg is None:
            return ToolResult(
                success=False,
                message="Could not fetch the replied message from Telegram to save it.",
            )

        try:
            result = await save_service.execute_save(
                context.telegram.client, context.owner_id, reply_msg, mode, context.tz_str
            )
            # Services report failures as "❌ ..."/"⚠️ ..." strings — only a
            # success string means the save actually happened.
            return result_from_service(result, data={"mode": mode})
        except Exception as exc:
            return ToolResult(success=False, message=f"Save failed: {exc}")

    async def _resolve_reply_message(self, context: ToolContext, meta: dict[str, Any]):
        """Resolve the real Telethon Message for the reply metadata.

        The dispatcher carries reply metadata (chat_id + message_id) in
        ``context.extra``; the service layer needs the actual Message
        object. We fetch it through the SAME client the runtime already
        injected — never a second client, never fake values.
        """
        client = None
        if context.telegram is not None:
            client = getattr(context.telegram, "client", None)
        if client is None:
            client = context.client
        if client is None:
            return None

        chat_id = meta.get("chat_id")
        message_id = meta.get("message_id")
        if not chat_id or not message_id:
            return None
        try:
            return await client.get_messages(chat_id, ids=message_id)
        except Exception as exc:
            logger.warning("SaveTool: could not fetch reply message %s/%s: %s", chat_id, message_id, exc)
            return None
