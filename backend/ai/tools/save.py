"""
Save tool — wraps ``save_service.execute_save``.

The AI calls this tool to Deep-Save a message to Saved Messages. The tool
delegates entirely to the existing save service. No logic is duplicated.
"""
from __future__ import annotations

import logging
from typing import Any

from backend.ai.tools.base import PermissionLevel, Tool, ToolResult, result_from_service
from backend.ai.tools.context import ToolContext

logger = logging.getLogger(__name__)


class SaveTool(Tool):
    """Deep-save a replied message to Saved Messages.

    Downloads the source content and re-uploads it as a NEW message. Deep
    Save is the only save method — there is no Forward Save.
    """

    def __init__(self, context: ToolContext) -> None:
        self._context = context

    @property
    def name(self) -> str:
        return "save"

    @property
    def description(self) -> str:
        return (
            "Deep-save a message to Saved Messages by downloading and "
            "re-uploading it as a new message. Requires a replied message."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {}

    @property
    def permission_level(self) -> PermissionLevel:
        return PermissionLevel.READ_WRITE

    @property
    def safe(self) -> bool:
        return True

    @property
    def return_type(self) -> str:
        return "ToolResult with save_code and confirmation message in data"

    @property
    def long_running(self) -> bool:
        return True

    async def execute(self, context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
        from backend.services import save_service

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
                context.telegram.client, context.owner_id, reply_msg, context.tz_str
            )
            # Services report failures as "❌ ..."/"⚠️ ..." strings — only a
            # success string means the save actually happened.
            return result_from_service(result, data={"mode": "deep"})
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


class SaveByLinkTool(Tool):
    """Deep-save a Telegram message resolved from a t.me / telegram.me link.

    Reuses ``save_service.execute_link_save`` — the SAME Deep Save pipeline
    as ``SaveTool`` (download → re-upload as a NEW Saved Messages message).
    The link is resolved deterministically by the service; the model never
    rewrites the URL.
    """

    def __init__(self, context: ToolContext) -> None:
        self._context = context

    @property
    def name(self) -> str:
        return "save_by_link"

    @property
    def description(self) -> str:
        return (
            "Deep-save a Telegram message given its t.me / telegram.me message "
            "link. Resolves the linked message and runs the existing Deep Save "
            "pipeline (download → re-upload as a NEW Saved Messages message)."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "link": {
                "type": "string",
                "description": (
                    "Exact Telegram message link, e.g. https://t.me/channel/123 "
                    "or https://t.me/c/123456789/42. Preserve it verbatim."
                ),
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

    @property
    def long_running(self) -> bool:
        return True

    async def execute(self, context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
        from backend.services import save_service

        link = str(arguments.get("link", "") or "").strip()
        if not link:
            return ToolResult(success=False, message="No link provided.")
        if not link.lower().startswith("http"):
            link = "https://" + link

        channel, chat_id, _msg_id = save_service.parse_telegram_link(link)
        if not channel and not chat_id:
            return ToolResult(
                success=False,
                message="That does not look like a valid Telegram message link.",
            )

        client = None
        if context.telegram is not None:
            client = getattr(context.telegram, "client", None)
        if client is None:
            client = context.client
        if client is None:
            return ToolResult(success=False, message="No Telegram client available.")

        try:
            result = await save_service.execute_link_save(
                client, context.owner_id, link, context.tz_str
            )
            return result_from_service(
                result, data={"mode": "deep", "source": "telegram_link"}
            )
        except Exception as exc:
            return ToolResult(success=False, message=f"Link save failed: {exc}")
