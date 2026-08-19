"""
Semantic (content-based) delete tools.

These tools implement the controlled semantic-delete pipeline:

    USER REQUEST
      → AI interprets the semantic target
      → ``list_recent_messages`` returns a BOUNDED candidate window
      → AI selects concrete message IDs it actually saw
      → ``delete_messages_by_ids`` re-validates every ID locally
      → Telegram deletion of validated outgoing messages only

The AI never invents message IDs and never gets an unbounded history. The
executor remains authoritative: every selected ID is re-fetched and must be
an outgoing (owner-sent) message in the current chat before deletion.
"""
from __future__ import annotations

import logging
from typing import Any

from backend.ai.persian import coerce_int
from backend.ai.tools.base import PermissionLevel, Tool, ToolResult
from backend.ai.tools.context import ToolContext

logger = logging.getLogger(__name__)

_MAX_CANDIDATES = 100
_DEFAULT_CANDIDATES = 50
_MAX_DELETE_IDS = 100
_PREVIEW_LEN = 160


def _client(context: ToolContext):
    """Return the injected Telethon client from the facade (or fallback)."""
    if context.telegram is not None:
        client = getattr(context.telegram, "client", None)
        if client is not None:
            return client
    return context.client


def _coerce_id_list(ids_raw: Any) -> list[int]:
    """Coerce a list of message IDs, deduplicating and dropping invalid ones."""
    if not isinstance(ids_raw, list):
        return []
    out: list[int] = []
    for value in ids_raw:
        n = coerce_int(value)
        if n is not None and n > 0 and n not in out:
            out.append(n)
    return out


class ListRecentMessagesTool(Tool):
    """List a bounded window of recent outgoing messages for classification.

    This is a READ tool. It is the first step of semantic delete: the model
    gets concrete IDs + short text previews, then asks
    ``delete_messages_by_ids`` for only the IDs it actually saw. Message text
    is returned as DATA (never instructions).
    """

    def __init__(self, context: ToolContext) -> None:
        self._context = context

    @property
    def name(self) -> str:
        return "list_recent_messages"

    @property
    def description(self) -> str:
        return (
            "List a bounded window of the most recent outgoing (owner-sent) "
            "messages in the current chat, with their message IDs and short text "
            "previews. Use this FIRST for a semantic/content-based delete, then "
            "call delete_messages_by_ids with only the IDs you saw here — never "
            "invent IDs."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": 100,
                "description": "Number of recent outgoing messages to list (default 50).",
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
        return "ToolResult with data.messages = [{id, text, has_media}]"

    async def execute(self, context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
        limit = coerce_int(arguments.get("limit")) or _DEFAULT_CANDIDATES
        limit = max(1, min(limit, _MAX_CANDIDATES))

        chat_id = context.extra.get("chat_id") if context.extra else None
        if not chat_id:
            return ToolResult(success=False, message="No chat context available.")

        client = _client(context)
        if client is None:
            return ToolResult(success=False, message="No Telegram client available.")

        try:
            messages: list[dict[str, Any]] = []
            async for msg in client.iter_messages(chat_id, limit=limit, from_user="me"):
                text = (getattr(msg, "text", None) or "").strip()
                messages.append({
                    "id": getattr(msg, "id", 0),
                    "text": text[:_PREVIEW_LEN],
                    "has_media": getattr(msg, "media", None) is not None,
                })
            return ToolResult(
                success=True,
                message=f"Listed {len(messages)} recent outgoing message(s).",
                data={"messages": messages},
            )
        except Exception as exc:
            logger.warning("list_recent_messages failed: %s", exc)
            return ToolResult(success=False, message=f"Could not list messages: {exc}")


class DeleteMessagesByIdsTool(Tool):
    """Delete concrete outgoing messages by their message IDs.

    The executor is authoritative: every ID is re-fetched from the current
    chat and must be outgoing (owner-sent). Invented, stale, or non-outgoing
    IDs are skipped and reported — the model cannot escalate a vague request
    into deletion of arbitrary messages.
    """

    def __init__(self, context: ToolContext) -> None:
        self._context = context

    @property
    def name(self) -> str:
        return "delete_messages_by_ids"

    @property
    def description(self) -> str:
        return (
            "Delete specific outgoing messages by their message IDs. The IDs "
            "MUST have been returned by list_recent_messages in this turn — "
            "never invent IDs. Only outgoing (owner-sent) messages in the "
            "current chat are deleted; every ID is re-validated locally and "
            "invalid/non-outgoing IDs are skipped."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "message_ids": {
                "type": "array",
                "items": {"type": "integer"},
                "description": "Concrete message IDs to delete (from list_recent_messages).",
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
        return "ToolResult with deleted/rejected message IDs in data"

    async def execute(self, context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
        ids = _coerce_id_list(arguments.get("message_ids"))
        if not ids:
            return ToolResult(
                success=False,
                message="No valid message IDs provided. No messages were deleted.",
            )

        chat_id = context.extra.get("chat_id") if context.extra else None
        if not chat_id:
            return ToolResult(success=False, message="No chat context available.")

        client = _client(context)
        if client is None:
            return ToolResult(success=False, message="No Telegram client available.")

        valid: list[int] = []
        rejected: list[int] = []
        for message_id in ids[:_MAX_DELETE_IDS]:
            try:
                msg = await client.get_messages(chat_id, ids=message_id)
            except Exception as exc:
                logger.warning("delete_messages_by_ids: fetch %s failed: %s", message_id, exc)
                rejected.append(message_id)
                continue
            if msg is not None and getattr(msg, "out", False):
                valid.append(message_id)
            else:
                rejected.append(message_id)

        if not valid:
            return ToolResult(
                success=False,
                message=(
                    "None of the requested IDs resolve to outgoing messages in "
                    "this chat; nothing was deleted."
                ),
                data={"deleted": [], "rejected": rejected},
            )

        try:
            await client.delete_messages(chat_id, valid)
        except Exception as exc:
            return ToolResult(
                success=False,
                message=f"Delete failed: {exc}",
                data={"deleted": [], "rejected": rejected},
            )

        return ToolResult(
            success=True,
            message=f"Deleted {len(valid)} outgoing message(s).",
            data={"deleted": valid, "rejected": rejected},
        )
