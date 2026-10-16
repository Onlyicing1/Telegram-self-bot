"""
Auto-Reaction engine — Emoji Stage 3 (ROADMAP §27).

The deterministic execution layer behind the Auto-Reaction setting: when the
owner enables it and has chosen a Telegram reaction (Emoji Stage 2), every
eligible outgoing message authored by the account gets that reaction applied
ONCE.

    outgoing handler (existing self-client update path)
      → this service (eligibility → dedup → settings → react ONCE)
        → reaction_service.react_to_message (existing typed target resolution)
          → telegram_api.reactions.send_reaction (existing typed wrapper)

Contracts:

* **Owner-authored outgoing messages only.** The handler is
  ``events.NewMessage(outgoing=True)`` and this service re-proves the
  serialized message's ``out`` flag and sender identity, so an incoming or
  foreign message can never reach the reaction path. ``is_owner`` in the
  handler and the re-proof here are the same boundary the replacement
  pipeline uses.
* **Deterministic eligibility, never inference.** Service messages never
  reach the handler (Telethon's ``NewMessage.build`` drops
  ``MessageService``), and a message with no ``(chat_id, message_id)``, an
  inline-bot origin (``via_bot_id`` — the Glass UI machinery), or a message
  the owner is typing into a pending panel input flow is skipped. There is
  no keyword/regex routing, no "last message" heuristic and no AI.
* **Reads the persisted setting through the existing typed interface.** The
  enabled flag and the selected reaction come from
  ``settings_service.is_auto_reaction_enabled`` /
  ``settings_service.auto_reaction_emoji`` — the SAME values the Glass UI
  writes, so a toggle or selection change is honored on the very next
  message with no restart and no second client.
* **No fabricated fallback.** If the setting is off, or no reaction is
  selected, or the stored value is not a usable reaction, NOTHING is sent —
  never a hardcoded emoji and never a catalog read. The catalog is the
  selector's concern (Stage 2), not the engine's.
* **At most one attempt, bounded and safe (§28).** Exactly ONE reaction is
  attempted per eligible message through the existing service (which itself
  makes one ``SendReactionRequest``); a Telegram error, flood wait or
  permission rejection becomes an honest ``failed`` outcome and a bounded
  log line — no retry, no retry storm. The handler catches everything so one
  failure can never break the general update loop.
* **Bounded deduplication.** A small in-memory registry of the
  ``(chat_id, message_id)`` pairs already handled (TTL + size cap) makes a
  duplicate ``NewMessage`` for the same message id a no-op — re-entry is
  impossible without unbounded state.
* **No second infrastructure.** The client is passed in by the caller; there
  is no client/loop/scheduler/executor/AI of this module's own, and nothing
  from ``backend.ai`` is imported.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict
from typing import Any

from backend.helper.input_state import get_pending
from backend.services import reaction_service
from backend.services import settings_service
from backend.telegram_api.reactions import normalize_reaction

logger = logging.getLogger(__name__)

#: Deduplication registry bound: a small in-memory map of
#: ``(chat_id, message_id) -> monotonic timestamp``. Deterministic and
#: invisible to chat members.
GUARD_MAX = 512
GUARD_TTL_S = 900.0

STATUS_REACTED = "reacted"
STATUS_FAILED = "failed"
STATUS_DISABLED = "skipped_disabled"
STATUS_NO_REACTION = "skipped_no_reaction"
STATUS_INVALID = "skipped_invalid"
STATUS_NOT_OWNER = "skipped_not_owner_authored"
STATUS_INLINE_ORIGIN = "skipped_inline_origin"
STATUS_PENDING_INPUT = "skipped_pending_input"
STATUS_DUPLICATE = "skipped_duplicate"

_handled: "OrderedDict[tuple[int, int], float]" = OrderedDict()


def _guard_prune(now: float) -> None:
    for key in [k for k, stamp in _handled.items() if now - stamp > GUARD_TTL_S]:
        _handled.pop(key, None)
    while len(_handled) > GUARD_MAX:
        _handled.popitem(last=False)


def _guard_has(key: tuple[int, int], now: float) -> bool:
    _guard_prune(now)
    return key in _handled


def _guard_add(key: tuple[int, int], now: float) -> None:
    _handled[key] = now
    _guard_prune(now)


def reset_loop_guard() -> None:
    """Clear the deduplication registry (bounded, in-memory only)."""
    _handled.clear()


def _valid_owner(owner_id: Any) -> bool:
    return isinstance(owner_id, int) and not isinstance(owner_id, bool) and owner_id > 0


def _valid_chat_id(chat_id: Any) -> bool:
    return isinstance(chat_id, int) and not isinstance(chat_id, bool) and chat_id != 0


def _valid_message_id(msg_id: Any) -> bool:
    return isinstance(msg_id, int) and not isinstance(msg_id, bool) and msg_id > 0


def _outcome(status: str, *, error: str | None = None, reaction: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"status": status, "error": error, "reaction": reaction}


async def process_outgoing_message(
    *,
    owner_id: int,
    client: Any,
    message: dict[str, Any],
    via_bot_id: int | None = None,
) -> dict[str, Any]:
    """Apply the configured Auto-Reaction to ONE owner-authored outgoing message.

    ``client`` is the self client (the reaction service resolves the target
    and sends the reaction through it). ``message`` is the
    ``serialize_message`` representation of the owner's outgoing message.
    ``via_bot_id`` marks a message Telegram attributes to an inline bot (the
    Glass UI panel machinery) — those are never reacted to.

    Returns an honest outcome dict (``status`` / ``error`` / ``reaction``); it
    never raises for a condition it can describe and never fabricates a
    reaction that was not sent.
    """
    if not _valid_owner(owner_id) or not isinstance(message, dict):
        return _outcome(STATUS_INVALID, error="invalid owner or message")

    chat_id = message.get("chat_id")
    msg_id = message.get("id")
    if not _valid_chat_id(chat_id) or not _valid_message_id(msg_id):
        return _outcome(STATUS_INVALID, error="message has no usable chat/message id")

    if message.get("out") is not True or message.get("sender_id") != owner_id:
        return _outcome(STATUS_NOT_OWNER, error="message is not owner-authored")

    if via_bot_id is None:
        via_bot_id = message.get("via_bot_id")
    if _valid_message_id(via_bot_id):
        return _outcome(STATUS_INLINE_ORIGIN, error="message was sent through an inline bot")

    now = time.monotonic()
    key = (chat_id, msg_id)
    if _guard_has(key, now):
        return _outcome(STATUS_DUPLICATE, error="message was already handled")

    # A message the owner is typing into a panel's input flow is UI input,
    # not content: the existing pending-input machinery owns it.
    pending = get_pending(owner_id)
    if isinstance(pending, dict) and pending.get("chat_id") == chat_id:
        return _outcome(STATUS_PENDING_INPUT, error="message is input for a pending panel flow")

    # Settings are read through the existing typed accessors, so a toggle or
    # a selection change is honored on the next message. Nothing is sent when
    # the feature is off or no usable reaction is selected — no fallback.
    if not settings_service.is_auto_reaction_enabled():
        return _outcome(STATUS_DISABLED, error="auto-reaction is disabled")

    reaction = normalize_reaction(
        {"kind": "emoji", "emoji": settings_service.auto_reaction_emoji()}
    )
    if reaction is None:
        logger.warning("[AUTO_REACTION] enabled but no usable reaction is selected")
        return _outcome(STATUS_NO_REACTION, error="no usable reaction is selected")

    # Mark handled BEFORE the RPC so a duplicate/edited update for the same
    # message is a no-op even while the first reaction is in flight.
    _guard_add(key, now)

    if client is None:
        return _outcome(STATUS_FAILED, error="the self client is not connected", reaction=reaction)

    try:
        result = await reaction_service.react_to_message(
            client, owner_id, chat_id, msg_id, reaction,
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning("[AUTO_REACTION] reaction failed chat=%s msg=%s: %s", chat_id, msg_id, exc)
        return _outcome(STATUS_FAILED, error=f"reaction failed: {exc}", reaction=reaction)

    if not isinstance(result, dict) or not result.get("ok"):
        detail = result.get("detail") if isinstance(result, dict) else None
        logger.warning(
            "[AUTO_REACTION] telegram rejected chat=%s msg=%s: %s",
            chat_id, msg_id, detail or "unknown error",
        )
        return _outcome(STATUS_FAILED, error=detail or "reaction rejected", reaction=reaction)

    logger.info("[AUTO_REACTION] reacted chat=%s msg=%s", chat_id, msg_id)
    return _outcome(STATUS_REACTED, reaction=result.get("reaction", reaction))
