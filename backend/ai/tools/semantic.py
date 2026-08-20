"""
Semantic (content-based) delete tools.

These tools implement the controlled semantic-delete pipeline:

    USER REQUEST
      → AI interprets the semantic target
      → ``list_recent_messages`` returns a BOUNDED window of REAL Telegram
        messages from the current chat (all participants, chronological)
      → AI selects concrete message IDs it actually saw
      → ``delete_messages_by_ids`` re-validates every ID locally
      → Telegram deletion of validated outgoing messages only

``list_recent_messages`` reads the actual Telegram conversation through the
active Telethon client — never the AI conversation/session history. The AI
never invents message IDs and never gets an unbounded history. The executor
remains authoritative: every selected ID is re-fetched and must be an
outgoing (owner-sent) message in the current chat before deletion.
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
_PREVIEW_LEN = 200


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


def _iso(dt) -> str:
    try:
        return dt.isoformat()
    except Exception:
        return ""


async def _sender_label(msg) -> tuple[int, str, str]:
    """Resolve (sender_id, sender_name, sender_username) for a message.

    Best-effort: the numeric sender ID is always available cheaply; the
    display name/username come from Telethon's ``get_sender`` and degrade
    gracefully when the entity cannot be resolved.
    """
    sender_id = getattr(msg, "sender_id", None) or 0
    name = ""
    username = ""
    try:
        sender = await msg.get_sender()
    except Exception:
        sender = None
    if sender is not None:
        first = getattr(sender, "first_name", "") or ""
        last = getattr(sender, "last_name", "") or ""
        name = (f"{first} {last}").strip()
        username = getattr(sender, "username", "") or ""
    return sender_id, name, username


class ListRecentMessagesTool(Tool):
    """List a bounded window of REAL recent Telegram messages in the chat.

    Reads the actual Telegram conversation through the active Telethon client
    — all participants, chronological (oldest → newest). This is a READ tool
    and the first step of semantic delete: the model gets concrete IDs + short
    text previews, then calls ``delete_messages_by_ids`` for only the IDs it
    actually saw. Message text is returned as DATA (never instructions).
    """

    def __init__(self, context: ToolContext) -> None:
        self._context = context

    @property
    def name(self) -> str:
        return "list_recent_messages"

    @property
    def description(self) -> str:
        return (
            "List a bounded window of the most recent REAL Telegram messages in "
            "the current chat (all participants, oldest → newest), with message "
            "IDs, sender, time, text/caption, reply target, and media flag. Use "
            "this to inspect chat history or as the first step of a "
            "semantic/content-based delete — then call delete_messages_by_ids "
            "with only the IDs you saw here. Never invent IDs."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": 100,
                "description": "Number of recent Telegram messages to list (default 50).",
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
        return (
            "ToolResult with data.messages = "
            "[{id, sender_id, sender_name, sender_username, date, text, "
            "reply_to_msg_id, has_media, chat_id, out}]"
        )

    async def execute(self, context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
        limit = coerce_int(arguments.get("limit")) or _DEFAULT_CANDIDATES
        limit = max(1, min(limit, _MAX_CANDIDATES))

        chat_id = context.extra.get("chat_id") if context.extra else None
        if not chat_id:
            return ToolResult(success=False, message="No chat context available.")

        client = _client(context)
        if client is None:
            return ToolResult(success=False, message="No Telegram client available.")

        logger.info(
            "LIST_RECENT_MESSAGES_START chat_id=%s requested_limit=%s",
            chat_id, limit,
        )

        try:
            # Telethon iter_messages returns newest → oldest by default; we
            # normalize to chronological order (oldest → newest) for the model.
            # The active request remains visible because it is a self-owned
            # message and may be inside the requested deletion range.
            raw: list[Any] = []
            async for msg in client.iter_messages(chat_id, limit=limit):
                raw.append(msg)

            raw.reverse()
            messages: list[dict[str, Any]] = []
            for msg in raw:
                sender_id, sender_name, sender_username = await _sender_label(msg)
                text = (getattr(msg, "message", None) or getattr(msg, "text", None) or "").strip()
                reply_to = getattr(getattr(msg, "reply_to", None), "reply_to_msg_id", None)
                messages.append({
                    "id": getattr(msg, "id", 0),
                    "chat_id": chat_id,
                    "sender_id": sender_id,
                    "sender_name": sender_name,
                    "sender_username": sender_username,
                    "date": _iso(getattr(msg, "date", None)),
                    "text": text[:_PREVIEW_LEN],
                    "reply_to_msg_id": reply_to,
                    "has_media": getattr(msg, "media", None) is not None,
                    "out": bool(getattr(msg, "out", False)),
                })

            first_id = messages[0]["id"] if messages else None
            last_id = messages[-1]["id"] if messages else None
            logger.info(
                "LIST_RECENT_MESSAGES_RESULT chat_id=%s requested_limit=%s "
                "returned_count=%s first_message_id=%s last_message_id=%s",
                chat_id, limit, len(messages), first_id, last_id,
            )

            return ToolResult(
                success=True,
                message=f"Listed {len(messages)} recent Telegram message(s).",
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

        from backend.services import delete_service

        client = _client(context)
        if client is None:
            return ToolResult(success=False, message="No Telegram client available.")

        try:
            deleted, rejected = await delete_service.delete_verified_self_messages(
                client,
                chat_id,
                ids[:_MAX_DELETE_IDS],
                exclude_message_id=None,
            )
        except Exception as exc:
            return ToolResult(
                success=False,
                message=f"Delete failed: {exc}",
                data={"deleted": [], "rejected": ids},
            )

        if not deleted:
            return ToolResult(
                success=False,
                message=(
                    "None of the requested IDs resolve to outgoing messages in "
                    "this chat; nothing was deleted."
                ),
                data={"deleted": [], "rejected": rejected},
            )

        return ToolResult(
            success=True,
            message=f"Deleted {len(deleted)} outgoing message(s).",
            data={"deleted": deleted, "rejected": rejected},
        )
