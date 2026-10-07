"""
Reaction service — Phase 6 (Emoji & Reaction, ROADMAP §27).

The deterministic business layer for ONE explicit operation: apply ONE
reaction — a Unicode emoji or a custom-emoji document — to ONE explicit
message the owner selects.

    Glass UI / handler   (explicit chat id + message id + reaction value)
      → this service     (validate → resolve the target → react ONCE)
        → telegram_api.reactions (typed SendReactionRequest wrapper)

Contracts:

* **Explicit, validated target — never inferred.** The caller must supply a
  chat id and message id; the service resolves that exact pair through the
  existing Telegram facade BEFORE reacting, so a stale, deleted or foreign
  target (one that resolves to a different chat) is refused with an honest
  error. No "last message", no conversational context, no sender heuristics,
  no keyword/regex routing, no AI.
* **Separate subsystem (§27).** This module never transforms, sends, deletes
  or reconstructs a message, never reads mappings, never touches the Phase 3
  replacement state/`emoji_state` tables and never imports the transformer or
  the replacement service. A reaction is a reaction — not a message edit.
* **Owner boundary.** Every call receives the owner id the UI resolved and
  refuses to act without a valid one; the operation runs on the self client
  only (the owner's own account), and a target the owner's client cannot see
  resolves to nothing and is refused.
* **One attempt, honest failure (§28).** Exactly ONE reaction request is made
  per call — no retry, and no substitution of a different reaction
  representation when Telegram rejects the chosen one.
* **No persistence.** A reaction is an action, not configuration (§27), so
  this phase adds no table, no column, no migration and no SQL.
* **No second infrastructure.** The client is passed in by the caller; there
  is no client/loop/scheduler/executor/AI of this module's own.
"""
from __future__ import annotations

import logging
from typing import Any

from backend.telegram_api.exceptions import TelegramAPIError
from backend.telegram_api.messages import get_message
from backend.telegram_api.reactions import (
    KIND_CUSTOM_EMOJI,
    KIND_EMOJI,
    normalize_reaction,
    send_reaction,
    valid_chat_id,
    valid_message_id,
)

logger = logging.getLogger(__name__)

STATUS_REACTED = "reacted"
STATUS_FAILED = "failed"

ERROR_OWNER = "E_OWNER"
ERROR_TARGET = "E_TARGET"
ERROR_TARGET_FOREIGN = "E_TARGET_FOREIGN"
ERROR_TARGET_STALE = "E_TARGET_STALE"
ERROR_REACTION = "E_REACTION"
ERROR_NO_CLIENT = "E_NO_CLIENT"
ERROR_TELEGRAM = "E_TELEGRAM"


def owner_is_valid(owner_id: Any) -> bool:
    """The single owner-boundary predicate: a positive integer, never a bool."""
    return isinstance(owner_id, int) and not isinstance(owner_id, bool) and owner_id > 0


def reaction_label(reaction: dict[str, Any] | None) -> str:
    """Owner-facing label for a NORMALIZED reaction — never a fabricated glyph.

    A custom emoji is shown by its document id, because the service has no
    business resolving or inventing a visual for it.
    """
    if not isinstance(reaction, dict):
        return "?"
    if reaction.get("kind") == KIND_EMOJI:
        return str(reaction.get("emoji") or "?")
    if reaction.get("kind") == KIND_CUSTOM_EMOJI:
        return f"custom #{reaction.get('document_id', '?')}"
    return "?"


def _failure(
    chat_id: Any,
    msg_id: Any,
    error: str,
    detail: str,
    reaction: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "ok": False,
        "status": STATUS_FAILED,
        "error": error,
        "detail": detail,
        "chat_id": chat_id,
        "message_id": msg_id,
        "reaction": reaction,
    }


async def react_to_message(
    client: Any,
    owner_id: int,
    chat_id: int,
    msg_id: int,
    reaction: dict[str, Any],
) -> dict[str, Any]:
    """Apply ``reaction`` to the exact ``(chat_id, msg_id)`` message.

    Returns a plain, honest result dict — success echoes the applied reaction,
    every failure carries a stable ``error`` code and a human-readable
    ``detail``. It never raises for an operational failure (the Glass UI
    renders the result) and never fabricates success.
    """
    if not owner_is_valid(owner_id):
        return _failure(chat_id, msg_id, ERROR_OWNER, "no valid owner for this operation")
    if not valid_chat_id(chat_id) or not valid_message_id(msg_id):
        return _failure(
            chat_id, msg_id, ERROR_TARGET,
            "the reaction target is not an explicit chat/message pair",
        )
    normalized = normalize_reaction(reaction)
    if normalized is None:
        return _failure(
            chat_id, msg_id, ERROR_REACTION,
            "the reaction value is not a usable emoji or custom emoji",
        )
    if client is None:
        return _failure(
            chat_id, msg_id, ERROR_NO_CLIENT,
            "the self client is not connected", normalized,
        )

    try:
        target = await get_message(client, chat_id, msg_id)
    except TelegramAPIError as exc:
        logger.warning("[REACTION] target resolution failed chat=%s msg=%s: %s", chat_id, msg_id, exc)
        return _failure(chat_id, msg_id, ERROR_TARGET_STALE, str(exc), normalized)

    if not isinstance(target, dict) or not target.get("id"):
        return _failure(
            chat_id, msg_id, ERROR_TARGET,
            "the target message no longer exists", normalized,
        )
    target_chat = target.get("chat_id")
    if isinstance(target_chat, int) and target_chat != chat_id:
        return _failure(
            chat_id, msg_id, ERROR_TARGET_FOREIGN,
            "the target resolves to a different chat", normalized,
        )

    try:
        ack = await send_reaction(client, chat_id, msg_id, normalized)
    except TelegramAPIError as exc:
        logger.warning("[REACTION] telegram rejected chat=%s msg=%s: %s", chat_id, msg_id, exc)
        return _failure(chat_id, msg_id, ERROR_TELEGRAM, str(exc), normalized)

    return {
        "ok": True,
        "status": STATUS_REACTED,
        "error": None,
        "detail": None,
        "chat_id": chat_id,
        "message_id": msg_id,
        "reaction": ack.get("reaction", normalized),
    }
