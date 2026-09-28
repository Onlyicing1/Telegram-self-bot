"""
Question tool — the deterministic ask-the-owner side effect for durable chains.

A durable task may need ONE specific answer from the owner before it can
continue (Todo Part 3D). This registered tool is the ONLY way a scheduled
occurrence asks:

  - it accepts ONLY a bounded ``question`` argument — never a destination,
    chat id, parse mode, or any Telegram instruction;
  - it resolves the destination from TRUSTED runtime context exactly like
    ``SendMessageTool`` (the occurrence's stored chat, else the owner's own
    chat);
  - it performs the send through the existing ``TelegramAPI`` facade and
    returns the SENT message's chat + message id as its structured result —
    the durable correlation identity the answer resolver pairs with the
    owner's reply.

The result's ``answer`` field is NOT produced here: the question action's
run record only becomes ``succeeded`` (carrying ``{"answer": ...}``) when
the correlated reply is durably consumed. Until then the chain stays parked
on ``waiting_answer`` and this run record stays ``pending``.
"""
from __future__ import annotations

from typing import Any

from backend.ai.tools.base import PermissionLevel, Tool, ToolResult
from backend.ai.tools.context import ToolContext
from backend.ai.task_contract import (
    MAX_QUESTION_CHARS,
    QUESTION_CHAT_ID_KEY,
    QUESTION_MESSAGE_ID_KEY,
    validate_question_text,
)

QUESTION_TIMEOUT_SECONDS = 30.0


class AskOwnerTool(Tool):
    """Send ONE bounded question to the owner's own chat and identify it."""

    def __init__(self, context: ToolContext) -> None:
        self._context = context

    @property
    def name(self) -> str:
        return "ask_owner"

    @property
    def required_arguments(self) -> tuple[str, ...]:
        return ("question",)

    @property
    def description(self) -> str:
        return (
            "Ask the owner one bounded plain-text question and stop the "
            "workflow until they answer it. Accepts a single 'question' "
            "argument only; the destination is always the owner's own chat."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "question": {
                "type": "string",
                "minLength": 1,
                "maxLength": MAX_QUESTION_CHARS,
                "description": "The exact question text to send.",
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
        return (
            "ToolResult with the question text and the sent message's "
            "chat_id + message_id (the answer's correlation identity)"
        )

    @property
    def timeout_seconds(self) -> int:
        return int(QUESTION_TIMEOUT_SECONDS)

    @property
    def consumable_output_fields(self) -> tuple[str, ...]:
        # The answer is the declared chainable field; a later action consumes
        # it with the existing reference contract, a condition reads it with
        # its existing source/operator/value contract. The correlation ids are
        # context for the answer resolver, never chainable data.
        return ("answer",)

    async def execute(self, context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
        try:
            text = validate_question_text(arguments.get("question"))
        except Exception as exc:  # noqa: BLE001 — structured refusal, never a raise
            return ToolResult(success=False, message=f"Invalid question; nothing was sent: {exc}")
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
            sent = await telegram.send_message(chat_id, text)
        except Exception as exc:  # noqa: BLE001 — surfaced to the retry boundary
            return ToolResult(
                success=False,
                message=f"Telegram send failed: {type(exc).__name__}: {exc}",
                data={"error_class": type(exc).__name__},
            )
        message_id = sent.get("id") if isinstance(sent, dict) else None
        sent_chat = sent.get("chat_id") if isinstance(sent, dict) else None
        if not isinstance(message_id, int) or message_id <= 0:
            return ToolResult(
                success=False,
                message="The sent question could not be identified; nothing to correlate.",
                data={"error_class": "unidentifiable_question_message"},
            )
        return ToolResult(
            success=True,
            message="❓ Asked and waiting for the answer.",
            data={
                "question": text,
                QUESTION_CHAT_ID_KEY: int(sent_chat) if isinstance(sent_chat, int) else int(chat_id),
                QUESTION_MESSAGE_ID_KEY: int(message_id),
            },
        )
