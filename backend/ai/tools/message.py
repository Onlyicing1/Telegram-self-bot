"""
Message-send tool — the deterministic Telegram side effect for scheduled tasks.

A scheduled task such as "every 1 minute write hello" persists a bounded
``send_message`` action (see ``task_candidate._canonicalize_action``) and is
executed by the single TaskScheduler through this registered tool. The tool:

  - accepts ONLY a bounded ``text`` argument — never a destination, chat id,
    recipient, method name, or any Telegram/RPC instruction;
  - resolves the destination from TRUSTED runtime context: the owner's own
    chat (Saved Messages), the same destination the existing task-outcome
    notification sender uses — never from model output;
  - performs the actual Telegram operation through the existing
    ``TelegramAPI`` facade (``backend/telegram_api``), which owns bounded
    timeouts, flood-wait handling, and structured errors.

A Telegram failure surfaces as a failed ``ToolResult`` and flows through the
existing ``TaskExecutionCoordinator`` retry boundary — it is never reported
as a success and never silently swallowed.
"""
from __future__ import annotations

from typing import Any

from backend.ai.tools.base import PermissionLevel, Tool, ToolResult
from backend.ai.tools.context import ToolContext

MAX_SEND_TEXT_CHARS = 4096
SEND_TIMEOUT_SECONDS = 30.0


class SendMessageTool(Tool):
    """Send a bounded text message to the owner's own chat (Saved Messages)."""

    def __init__(self, context: ToolContext) -> None:
        self._context = context

    @property
    def name(self) -> str:
        return "send_message"

    @property
    def description(self) -> str:
        return (
            "Send a text message to the owner's own Saved Messages chat. "
            "Accepts a single 'text' argument only; the destination is always "
            "the owner's own chat, never a user-supplied chat."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "text": {
                "type": "string",
                "minLength": 1,
                "maxLength": MAX_SEND_TEXT_CHARS,
                "description": "The exact message text to send.",
            },
        }

    @property
    def permission_level(self) -> PermissionLevel:
        # Sending to the owner's own Saved Messages chat is a benign,
        # owner-authorized side effect in this single-owner self-bot.
        return PermissionLevel.READ_WRITE

    @property
    def safe(self) -> bool:
        return True

    @property
    def return_type(self) -> str:
        return "ToolResult with the sent text and the fixed destination chat"

    @property
    def timeout_seconds(self) -> int:
        return int(SEND_TIMEOUT_SECONDS)

    async def execute(self, context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
        text = arguments.get("text")
        if not isinstance(text, str) or not text.strip():
            return ToolResult(success=False, message="Missing 'text' argument; nothing was sent.")
        text = text.strip()
        if len(text) > MAX_SEND_TEXT_CHARS:
            return ToolResult(success=False, message="Message text is too long; nothing was sent.")
        # Destination comes from trusted runtime context, never arguments.
        # For immediate sends the chat_id is the current request chat; for
        # scheduled tasks it is the chat where the task was created (stored
        # in notification_destination). Falls back to the owner's own chat
        # (Saved Messages) only when no trusted chat_id is available.
        extra = getattr(context, "extra", None) or {}
        chat_id = extra.get("chat_id")
        if not isinstance(chat_id, int) or chat_id == 0:
            chat_id = getattr(context, "owner_id", 0)
        if not isinstance(chat_id, int) or chat_id == 0:
            return ToolResult(success=False, message="Trusted destination is unavailable; nothing was sent.")
        telegram = getattr(context, "telegram", None)
        if telegram is None:
            client = getattr(context, "client", None)
            if client is None:
                return ToolResult(success=False, message="Telegram transport is unavailable; nothing was sent.")
            from backend.telegram_api import TelegramAPI
            telegram = TelegramAPI(client)
        try:
            await telegram.send_message(chat_id, text)
        except Exception as exc:  # noqa: BLE001 — surfaced to the retry boundary
            return ToolResult(
                success=False,
                message=f"Telegram send failed: {type(exc).__name__}: {exc}",
                data={"error_class": type(exc).__name__},
            )
        return ToolResult(
            success=True,
            message=f"📨 Sent to chat {chat_id}.",
            data={"text": text, "chat_id": int(chat_id)},
        )
