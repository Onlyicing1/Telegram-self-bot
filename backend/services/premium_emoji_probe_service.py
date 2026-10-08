"""
Premium-emoji probe service — POC (helper-bot rendering boundary).

Proves ONE capability, offline-testable, without touching the emoji library,
the category/mapping model, the replacement pipeline, the library pagination
or the existing reaction behaviour:

    the owner's real Telegram custom-emoji entity
      → the REAL document id (read from the entity, never from the glyph)
        → the EXISTING helper-bot bridge as a ``MessageEntityCustomEmoji``
          → a message SENT BY THE HELPER BOT carrying that entity

Boundaries this module is built on:

* **Entity, never glyph.** Only a real ``MessageEntityCustomEmoji`` with a
  usable ``document_id`` counts as a Premium emoji here. A plain Unicode
  reply, a media-only reply, empty text, or a custom-emoji entity whose id is
  unusable all fail closed with an honest reason: the visible glyph is never
  promoted to "the emoji" and a Unicode render is never reported as success.
* **The helper bot renders.** Nothing here sends a premium emoji with the
  self client. Delivery goes through the existing bridge
  (``backend/telegram_api/bridge.py``) — the optional helper bot's own client,
  the same single delivery path the replacement pipeline uses. The self
  client is used only for the destinations the bridge resolves by contract.
* **Capability is Telegram's verdict.** A plain bot may use custom-emoji
  entities only if it purchased additional usernames on Fragment
  (ROADMAP §17). The send is therefore attempted exactly ONCE and Telegram's
  answer is reported as-is — no alt-text fallback, no entitlement probing, no
  second attempt with a different representation.
* **No new infrastructure.** No persistence, no schema, no AI, no second
  client/loop/scheduler and no event listener; the caller supplies the
  clients through the existing modules.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from telethon.tl.types import MessageEntityCustomEmoji

from backend.helper import client as helper_client
from backend.telegram_api._helpers import utf16_index_at, utf16_length
from backend.telegram_api.bridge import bridge_available, send_reconstructed
from backend.telegram_api.entities import get_input_entity

logger = logging.getLogger(__name__)

#: The three verdicts of :func:`inspect_message`.
KIND_CUSTOM_EMOJI = "custom_emoji"
KIND_UNICODE = "unicode"
KIND_NONE = "none"

ERROR_OWNER = "E_OWNER"
ERROR_NO_EMOJI = "E_NO_CUSTOM_EMOJI"
ERROR_NO_HELPER = "E_NO_HELPER"
ERROR_NO_BOT_CHAT = "E_NO_BOT_CHAT"
ERROR_SEND = "E_SEND"

#: The label the helper bot's message carries in front of the emoji.
PROOF_PREFIX = "Selected reaction emoji: "

#: The custom-emoji entity must cover at least one UTF-16 unit of text. When
#: Telegram reports no alt text for the document, this plainly non-emoji
#: placeholder becomes that underlying text; the rendered emoji still comes
#: from ``document_id`` alone.
PLACEHOLDER_GLYPH = "\u25aa"

#: Bound on the helper bot's dialog scan used to resolve its chat with the owner.
BOT_DIALOG_LIMIT = 50


def _is_positive_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _message_text(message: Any) -> str:
    """The message's own text — raw, before any per-client rendering."""
    raw = getattr(message, "message", None)
    if not isinstance(raw, str) or not raw:
        raw = getattr(message, "text", None)
    return raw if isinstance(raw, str) else ""


def _span_text(text: str, offset: Any, length: Any) -> str:
    """The text an entity's UTF-16 span covers, or ``""`` when it is unusable.

    A corrupt span (out of range, mid-surrogate) yields no text rather than a
    clamped guess.
    """
    if not isinstance(offset, int) or not isinstance(length, int):
        return ""
    if offset < 0 or length <= 0:
        return ""
    try:
        start = utf16_index_at(text, offset)
        end = utf16_index_at(text, offset + length)
    except ValueError:
        return ""
    return text[start:end]


def inspect_message(message: Any) -> dict[str, Any]:
    """The Premium/Unicode/none verdict for ONE message's REAL entity data.

    Returns ``{kind, document_id, alt_text, detail}``. ``kind`` is
    :data:`KIND_CUSTOM_EMOJI` only when a real ``MessageEntityCustomEmoji``
    with a usable ``document_id`` exists — the source of truth is the entity,
    never the visible text. Otherwise ``kind`` is :data:`KIND_UNICODE` (the
    reply's own visible text, which is NOT a Premium emoji) or
    :data:`KIND_NONE`, each with the honest reason in ``detail``.
    """
    text = _message_text(message)
    for entity in getattr(message, "entities", None) or []:
        if not isinstance(entity, MessageEntityCustomEmoji):
            continue
        document_id = getattr(entity, "document_id", None)
        if not _is_positive_int(document_id):
            return {
                "kind": KIND_NONE,
                "document_id": None,
                "alt_text": "",
                "detail": (
                    "the reply carries a custom-emoji entity with an unusable "
                    "document id — it cannot be sent as a Premium emoji"
                ),
            }
        return {
            "kind": KIND_CUSTOM_EMOJI,
            "document_id": document_id,
            "alt_text": _span_text(
                text,
                getattr(entity, "offset", None),
                getattr(entity, "length", None),
            ),
            "detail": "",
        }
    visible = text.strip()
    if visible:
        return {
            "kind": KIND_UNICODE,
            "document_id": None,
            "alt_text": visible,
            "detail": (
                "the reply carries no Telegram custom-emoji entity — a plain "
                "Unicode emoji is not a Premium emoji"
            ),
        }
    return {
        "kind": KIND_NONE,
        "document_id": None,
        "alt_text": "",
        "detail": "the reply carries no Telegram custom-emoji entity",
    }


def build_proof_payload(document_id: Any, alt_text: Any) -> dict[str, Any]:
    """The EXACT helper-bot message payload for ONE real custom emoji.

    ``{text, entities, entity, fallback_text, used_placeholder}`` — the label,
    the real ``MessageEntityCustomEmoji`` (offsets in UTF-16 units, carrying
    the REAL ``document_id``) and the underlying text the emoji rides on. The
    entity is the payload; the glyph is only its fallback text.
    """
    if not _is_positive_int(document_id):
        raise ValueError("build_proof_payload requires a real custom-emoji document id")
    has_alt = isinstance(alt_text, str) and alt_text != ""
    glyph = alt_text if has_alt else PLACEHOLDER_GLYPH
    entity = {
        "type": "MessageEntityCustomEmoji",
        "offset": utf16_length(PROOF_PREFIX),
        "length": utf16_length(glyph),
        "document_id": int(document_id),
    }
    return {
        "text": PROOF_PREFIX + glyph,
        "entities": [entity],
        "entity": entity,
        "fallback_text": glyph,
        "used_placeholder": not has_alt,
    }


def helper_bot_available() -> bool:
    """True when the existing helper bot bridge can send at all."""
    return bridge_available()


async def _resolve_bot_peer(owner_id: int) -> tuple[Any, str]:
    """Resolve the OWNER's peer through the HELPER BOT's own session.

    The destination is the helper bot's own private chat with the owner, so
    the peer (and its access hash) must come from the BOT's session — the
    self client cannot address its own account as a bot peer. Resolution is
    bounded and deterministic: the bot's entity cache first, then a bounded
    scan of the bot's own dialogs. Returns ``(peer, "")`` or
    ``(None, honest_reason)``.
    """
    bot = helper_client.get_client()
    if bot is None:
        return None, "the helper bot is not connected"
    try:
        peer = await get_input_entity(bot, owner_id)
    except asyncio.CancelledError:
        raise
    except Exception:
        peer = None
    if peer is not None:
        return peer, ""
    scanned = 0
    try:
        async for dialog in bot.iter_dialogs(limit=BOT_DIALOG_LIMIT):
            scanned += 1
            if getattr(dialog, "id", None) == owner_id:
                return getattr(dialog, "input_entity", None), ""
            if scanned >= BOT_DIALOG_LIMIT:
                break
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning("[PREMIUM_PROBE] helper dialog scan failed: %s", exc)
    return None, (
        "the helper bot has no private chat with the owner yet — open the "
        "helper bot and press Start, then retry"
    )


async def deliver_proof(
    self_client: Any,
    owner_id: Any,
    document_id: Any,
    alt_text: Any,
) -> dict[str, Any]:
    """Have the HELPER BOT display ``document_id`` as a real custom emoji.

    Builds the payload, resolves the bot's own chat with the owner and sends
    exactly ONE message through the existing bridge. Returns an honest result
    dict — ``ok`` is True only when Telegram accepted the bot's send. Every
    failure carries a stable ``error`` code, a human-readable ``detail`` and
    the payload that was (or would have been) sent, so a caller can never
    mistake a Unicode glyph for the Premium render.
    """
    if not _is_positive_int(owner_id):
        return {
            "ok": False,
            "error": ERROR_OWNER,
            "detail": "no valid owner for this operation",
            "text": "",
            "entities": [],
            "entity": None,
            "fallback_text": "",
            "used_placeholder": False,
            "message_id": None,
        }
    if not _is_positive_int(document_id):
        return {
            "ok": False,
            "error": ERROR_NO_EMOJI,
            "detail": "the reply carries no real custom-emoji document id",
            "text": "",
            "entities": [],
            "entity": None,
            "fallback_text": "",
            "used_placeholder": False,
            "message_id": None,
        }
    payload = build_proof_payload(document_id, alt_text)
    if not bridge_available():
        return {
            "ok": False,
            "error": ERROR_NO_HELPER,
            "detail": (
                "the helper bot is not connected — the premium emoji cannot be "
                "rendered by the bot"
            ),
            "message_id": None,
            **payload,
        }
    peer, reason = await _resolve_bot_peer(owner_id)
    if peer is None:
        return {
            "ok": False,
            "error": ERROR_NO_BOT_CHAT,
            "detail": reason,
            "message_id": None,
            **payload,
        }
    try:
        sent = await send_reconstructed(
            self_client,
            owner_id,
            payload["text"],
            entities=payload["entities"],
            resolved_peer=peer,
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning("[PREMIUM_PROBE] helper bot refused the send: %s", exc)
        return {
            "ok": False,
            "error": ERROR_SEND,
            "detail": f"the helper bot's send was refused: {exc}",
            "message_id": None,
            **payload,
        }
    message_id = sent.get("id") if isinstance(sent, dict) else None
    return {
        "ok": True,
        "error": None,
        "detail": None,
        "message_id": message_id if _is_positive_int(message_id) else None,
        **payload,
    }
