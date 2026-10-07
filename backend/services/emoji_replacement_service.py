"""
Emoji replacement reconstruction — Phase 4 (Emoji & Reaction, ROADMAP §15–§24).

The single deterministic pipeline that turns ONE owner-authored outgoing
message into its reconstructed (premium-emoji) form:

    effective category (Phase 3 state boundary — never re-derived here)
      → mapping table (reference-only rows resolved against the library)
      → entity-safe transformation (``backend.services.emoji_transformer``)
      → bridge delivery (the existing send-only helper-bot bridge)
      → delete the original — only AFTER the new message exists

Contracts:

* Ownership boundary (§19): only owner-authored messages are processed. The
  handler already gates on ``is_owner``; this service re-proves the
  serialized message's ``out`` flag and sender identity before any work, so
  no other actor's message can ever reach the pipeline.
* Fail closed everywhere (§28): replacement OFF, no effective category, a
  deleted category, an unusable/absent mapping, no mapped emoji, an unsafe
  entity combination, a media message, a missing bridge, or a failed
  delivery all end with the ORIGINAL message untouched and an honest status —
  never a fabricated success, never a partial destructive step.
* Send-first ordering (§15/§34-B default proposal): the reconstructed message
  is delivered BEFORE the original is deleted, so a delivery failure can
  never destroy the owner's message. If the deletion then fails, the outcome
  is reported honestly as a duplicate — never as a clean success.
* Same destination (§18) and reply threading (§23) are preserved by passing
  the original message's chat id and reply target straight to the bridge.
* Media (§23): NOT reconstructed in this phase. A message with media is left
  untouched — the cross-account media re-send mechanism is an open
  investigation and is never guessed at here.
* The effective category comes ONLY from
  ``backend.services.emoji_state_service.resolve_effective_category`` — this
  module never re-implements the §13/§14 resolution order.
* Loop prevention (§24) is structural and deterministic: the handler is
  outgoing-only; the bridge bot's own author id is skipped; messages
  originated by an inline bot (``via_bot_id`` — the Glass UI panel
  machinery) are skipped; a message the owner is typing into a panel input
  flow is left to the existing pending-input machinery; and a bounded
  in-memory registry of the messages this pipeline produced / already
  reconstructed makes re-entry impossible. No visible text marker, no
  heuristic, no AI, no second listener.
* No second client, loop, scheduler, executor, or AI: state comes from the
  Phase 3 service, storage from the existing db layer, delivery from the
  existing bridge, deletion from the existing Telegram facade. Nothing from
  ``backend.ai`` is imported.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict
from typing import Any

from backend.db import client as db_client
from backend.helper.input_state import get_pending
from backend.services import emoji_state_service as state_service
from backend.services.emoji_transformer import transform_message
from backend.telegram_api.bridge import (
    bridge_available,
    bridge_bot_id,
    send_reconstructed,
)
from backend.telegram_api.messages import delete_messages

logger = logging.getLogger(__name__)

#: One message can never be resolved against more mappings than the db layer
#: itself bounds one listing to; an incomplete read is reported, never
#: silently applied to part of a message.
MAX_MAPPINGS = 5000

#: Loop-prevention registries: bounded in-memory maps of
#: ``(chat_id, message_id) -> monotonic timestamp``. Deterministic and
#: invisible to chat members (§24).
GUARD_MAX = 512
GUARD_TTL_S = 900.0

STATUS_SKIPPED_INVALID = "skipped_invalid"
STATUS_OWNER_BOUNDARY = "skipped_not_owner_authored"
STATUS_BRIDGE_ORIGIN = "skipped_bridge_origin"
STATUS_INLINE_ORIGIN = "skipped_inline_origin"
STATUS_DUPLICATE = "skipped_duplicate"
STATUS_PENDING_INPUT = "skipped_pending_input"
STATUS_MEDIA = "skipped_media"
STATUS_NO_TEXT = "skipped_no_text"
STATUS_NO_CATEGORY = "skipped_no_effective_category"
STATUS_NO_MAPPINGS = "skipped_no_usable_mappings"
STATUS_NO_EMOJI = "skipped_no_mapped_emoji"
STATUS_UNSAFE = "skipped_unsafe_reconstruction"
STATUS_BRIDGE_UNAVAILABLE = "skipped_bridge_unavailable"
STATUS_FAILED = "failed"
STATUS_REPLACED = "replaced"
STATUS_REPLACED_UNDELETED = "replaced_undeleted"

_bridge_origins: "OrderedDict[tuple[int, int], float]" = OrderedDict()
_handled_originals: "OrderedDict[tuple[int, int], float]" = OrderedDict()


def _guard_prune(store: "OrderedDict[tuple[int, int], float]", now: float) -> None:
    for key in [k for k, stamp in store.items() if now - stamp > GUARD_TTL_S]:
        store.pop(key, None)
    while len(store) > GUARD_MAX:
        store.popitem(last=False)


def _guard_has(store: "OrderedDict[tuple[int, int], float]", key: tuple[int, int], now: float) -> bool:
    _guard_prune(store, now)
    return key in store


def _guard_add(store: "OrderedDict[tuple[int, int], float]", key: tuple[int, int], now: float) -> None:
    store[key] = now
    _guard_prune(store, now)


def reset_loop_guard() -> None:
    """Clear both loop-prevention registries (bounded, in-memory only)."""
    _bridge_origins.clear()
    _handled_originals.clear()


def _valid_owner(owner_id: Any) -> bool:
    return isinstance(owner_id, int) and not isinstance(owner_id, bool) and owner_id > 0


def _valid_chat_id(chat_id: Any) -> bool:
    return isinstance(chat_id, int) and not isinstance(chat_id, bool) and chat_id != 0


def _valid_message_id(msg_id: Any) -> bool:
    return isinstance(msg_id, int) and not isinstance(msg_id, bool) and msg_id > 0


def _outcome(
    status: str,
    *,
    replaced: int = 0,
    sent_message_id: int | None = None,
    deleted: bool = False,
    error: str | None = None,
) -> dict[str, Any]:
    return {
        "status": status,
        "replaced": replaced,
        "sent_message_id": sent_message_id,
        "deleted": deleted,
        "error": error,
    }


async def _load_mapping_table(
    owner_id: int, category_id: int,
) -> tuple[dict[str, dict[str, Any]], str | None]:
    """Resolve one category's mappings into ``{simple_text: entry}``.

    Every row is a REFERENCE to the library (Phase 2): the premium emoji is
    resolved through ``get_emoji_entry`` and a row whose entry cannot be
    resolved (deleted library entry, missing/blank alt text) is DROPPED — its
    simple emoji then stays untouched instead of a replacement being
    fabricated. Returns ``(table, error)``; ``error`` means the listing
    itself could not be trusted (incomplete read), so the caller must not
    transform a part of the message as if it were the whole mapping set.
    """
    rows, total = await db_client.list_emoji_mappings(
        owner_id, category_id, limit=MAX_MAPPINGS, offset=0,
    )
    if not isinstance(rows, list):
        return {}, "mapping listing failed"
    if isinstance(total, int) and total > len(rows):
        return {}, "mapping listing incomplete"
    table: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        simple = row.get("simple_emoji")
        document_id = row.get("document_id")
        if not isinstance(simple, str) or not simple:
            continue
        if not isinstance(document_id, int) or isinstance(document_id, bool) or document_id <= 0:
            continue
        entry = await db_client.get_emoji_entry(owner_id, document_id)
        if not isinstance(entry, dict):
            continue
        alt = entry.get("alt_text")
        if not isinstance(alt, str) or not alt:
            continue
        table[simple] = {"document_id": document_id, "alt_text": alt}
    return table, None


async def process_outgoing_message(
    *,
    owner_id: int,
    client: Any,
    message: dict[str, Any],
    via_bot_id: int | None = None,
) -> dict[str, Any]:
    """Reconstruct ONE owner-authored outgoing message, if everything lines up.

    ``client`` is the self client (peer resolution for the bridge + deletion
    of the original). ``message`` is the ``serialize_message`` representation
    of the owner's outgoing message. ``via_bot_id`` marks a message that
    Telegram attributes to an inline bot (the Glass UI panel machinery) —
    those are never processed.

    Returns an honest outcome dict (``status`` / ``replaced`` /
    ``sent_message_id`` / ``deleted`` / ``error``); it never raises for a
    condition it can describe, and it never reports a replacement that did
    not happen.
    """
    if not _valid_owner(owner_id) or not isinstance(message, dict):
        return _outcome(STATUS_SKIPPED_INVALID, error="invalid owner or message")

    chat_id = message.get("chat_id")
    msg_id = message.get("id")
    if not _valid_chat_id(chat_id) or not _valid_message_id(msg_id):
        return _outcome(STATUS_SKIPPED_INVALID, error="message has no usable chat/message id")

    # §24 sender-id suppression, checked before anything else: a message the
    # bridge bot authored is never touched, whatever the event claims.
    bridge_id = bridge_bot_id()
    if _valid_message_id(bridge_id) and message.get("sender_id") == bridge_id:
        return _outcome(STATUS_BRIDGE_ORIGIN, error="message was authored by the bridge bot")

    if message.get("out") is not True or message.get("sender_id") != owner_id:
        return _outcome(STATUS_OWNER_BOUNDARY, error="message is not owner-authored")

    if via_bot_id is None:
        via_bot_id = message.get("via_bot_id")
    if _valid_message_id(via_bot_id):
        return _outcome(STATUS_INLINE_ORIGIN, error="message was sent through an inline bot")

    now = time.monotonic()

    key = (chat_id, msg_id)
    if _guard_has(_bridge_origins, key, now):
        return _outcome(STATUS_BRIDGE_ORIGIN, error="message was produced by this pipeline")
    if _guard_has(_handled_originals, key, now):
        return _outcome(STATUS_DUPLICATE, error="message was already reconstructed")

    # A message the owner is typing into a panel's input flow is UI input,
    # not content: the existing pending-input machinery owns it.
    pending = get_pending(owner_id)
    if isinstance(pending, dict) and pending.get("chat_id") == chat_id:
        return _outcome(STATUS_PENDING_INPUT, error="message is input for a pending panel flow")

    if message.get("has_media"):
        return _outcome(STATUS_MEDIA, error="media messages are not reconstructed in this phase")

    text = message.get("text")
    if not isinstance(text, str) or not text:
        return _outcome(STATUS_NO_TEXT)

    category_id = await state_service.resolve_effective_category(owner_id, chat_id)
    if not _valid_message_id(category_id):
        return _outcome(STATUS_NO_CATEGORY, error="no effective category for this chat")

    table, mapping_error = await _load_mapping_table(owner_id, category_id)
    if mapping_error is not None:
        logger.warning("[EMOJI_REPL] mapping table unusable: %s", mapping_error)
        return _outcome(STATUS_NO_MAPPINGS, error=mapping_error)
    if not table:
        return _outcome(STATUS_NO_MAPPINGS, error="category has no resolvable mappings")

    transformed = transform_message(text, message.get("entities"), table)
    if not transformed["ok"]:
        logger.warning(
            "[EMOJI_REPL] unsafe reconstruction chat=%s msg=%s: %s",
            chat_id, msg_id, transformed["error"],
        )
        return _outcome(STATUS_UNSAFE, error=transformed["error"])
    if not transformed["changed"]:
        return _outcome(STATUS_NO_EMOJI)

    if not bridge_available():
        return _outcome(STATUS_BRIDGE_UNAVAILABLE, error="helper bot is not connected")

    reply_to = message.get("reply_to_msg_id")
    if not _valid_message_id(reply_to):
        reply_to = None

    try:
        sent = await send_reconstructed(
            client,
            chat_id,
            transformed["text"],
            entities=transformed["entities"],
            reply_to_msg_id=reply_to,
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning("[EMOJI_REPL] delivery failed chat=%s msg=%s: %s", chat_id, msg_id, exc)
        return _outcome(STATUS_FAILED, error=f"bridge delivery failed: {exc}")

    sent_id = sent.get("id") if isinstance(sent, dict) else None
    if _valid_message_id(sent_id):
        _guard_add(_bridge_origins, (chat_id, sent_id), now)

    # Send-first (§15): the original is deleted ONLY now that a replacement
    # exists. A failure here leaves an honest duplicate, never a loss.
    deleted = False
    delete_error: str | None = None
    try:
        removed = await delete_messages(client, chat_id, [msg_id])
        deleted = bool(removed)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        delete_error = f"original message not deleted: {exc}"
    _guard_add(_handled_originals, key, now)

    if deleted:
        logger.info(
            "[EMOJI_REPL] replaced chat=%s msg=%s -> %s (%s span(s))",
            chat_id, msg_id, sent_id, transformed["changed"],
        )
        return _outcome(
            STATUS_REPLACED,
            replaced=transformed["changed"],
            sent_message_id=sent_id,
            deleted=True,
        )
    logger.warning("[EMOJI_REPL] replaced but original survived chat=%s msg=%s", chat_id, msg_id)
    return _outcome(
        STATUS_REPLACED_UNDELETED,
        replaced=transformed["changed"],
        sent_message_id=sent_id,
        deleted=False,
        error=delete_error or "delete reported no removed message",
    )
