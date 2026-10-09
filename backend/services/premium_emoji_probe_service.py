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
from backend.runtime.operation_watchdog import guarded_await
from backend.telegram_api._helpers import utf16_index_at, utf16_length
from backend.telegram_api.bridge import bridge_available, send_reconstructed
from backend.telegram_api.custom_emoji import get_custom_emoji_documents
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

#: Diagnostic outcomes. Each states ONLY what its evidence proves: a retained
#: entity is never a visual render (the viewing client decides that) and a
#: missing id/span is a mismatch, never a guess.
SOURCE_ENTITY_MISSING = "SOURCE_ENTITY_MISSING"
OUTBOUND_ENTITY_INVALID = "OUTBOUND_ENTITY_INVALID"
SEND_FAILED = "SEND_FAILED"
READBACK_FAILED = "READBACK_FAILED"
ENTITY_STRIPPED_OR_MISSING = "ENTITY_STRIPPED_OR_MISSING"
ENTITY_MISMATCH = "ENTITY_MISMATCH"
ENTITY_RETAINED_RENDER_UNVERIFIED = "ENTITY_RETAINED_RENDER_UNVERIFIED"

#: Bound on the read-back fetch, following the existing operation-watchdog
#: convention (the bridge's own send bound is separate and unchanged).
_READBACK_TIMEOUT_S = 30.0

#: The label the helper bot's message carries in front of the emoji.
PROOF_PREFIX = "Selected reaction emoji: "

#: The custom-emoji entity must cover at least one UTF-16 unit of text. When
#: Telegram reports no alt text for the document, this plainly non-emoji
#: placeholder becomes that underlying text; the rendered emoji still comes
#: from ``document_id`` alone.
PLACEHOLDER_GLYPH = "\u25aa"

#: Bound on the helper bot's dialog scan used to resolve its chat with the owner.
BOT_DIALOG_LIMIT = 50


def _trace(stage: str, **fields: Any) -> None:
    """ONE structured line per pipeline stage, grep-friendly by stage name.

    Bounded by construction: never session data, tokens, access hashes, whole
    messages or unrelated history — only ids, offsets, lengths, short quoted
    spans and honest reason strings.
    """
    parts = [stage]
    for key, value in fields.items():
        if value is None or value == "":
            continue
        parts.append(f"{key}={value}")
    logger.info("[PREMIUM_PROBE] %s", " ".join(parts))


def _bounded(value: Any, limit: int = 32) -> str:
    """A short, quoted form of a span/reason for one trace line."""
    text = str(value)
    return repr(text[:limit] + ("…" if len(text) > limit else ""))


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


def _scan_custom_emoji(message: Any) -> dict[str, Any] | None:
    """The FIRST real custom-emoji entity of a message — entity-only.

    Returns ``{document_id, offset, length, span_text, text}`` for the first
    ``MessageEntityCustomEmoji`` (``document_id`` is ``None`` when Telegram's
    value is unusable). ``None`` means the message carries no custom-emoji
    entity at all — the visible glyph is never a substitute.
    """
    text = _message_text(message)
    for entity in getattr(message, "entities", None) or []:
        if not isinstance(entity, MessageEntityCustomEmoji):
            continue
        document_id = getattr(entity, "document_id", None)
        offset = getattr(entity, "offset", None)
        length = getattr(entity, "length", None)
        return {
            "document_id": document_id if _is_positive_int(document_id) else None,
            "offset": (
                offset
                if isinstance(offset, int) and not isinstance(offset, bool)
                else None
            ),
            "length": (
                length
                if isinstance(length, int) and not isinstance(length, bool)
                else None
            ),
            "span_text": _span_text(text, offset, length),
            "text": text,
        }
    return None


def inspect_message(message: Any) -> dict[str, Any]:
    """The Premium/Unicode/none verdict for ONE message's REAL entity data.

    Returns ``{kind, document_id, alt_text, detail}``. ``kind`` is
    :data:`KIND_CUSTOM_EMOJI` only when a real ``MessageEntityCustomEmoji``
    with a usable ``document_id`` exists — the source of truth is the entity,
    never the visible text. Otherwise ``kind`` is :data:`KIND_UNICODE` (the
    reply's own visible text, which is NOT a Premium emoji) or
    :data:`KIND_NONE`, each with the honest reason in ``detail``.

    Trace: emits ``SOURCE_ENTITY_FOUND`` and (for a usable entity)
    ``SOURCE_ENTITY_VALIDATED`` with the entity's own span facts.
    """
    text = _message_text(message)
    scan = _scan_custom_emoji(message)
    if scan is None:
        _trace("SOURCE_ENTITY_FOUND", found=False)
    elif scan["document_id"] is None:
        _trace("SOURCE_ENTITY_FOUND", found=True, usable=False)
        return {
            "kind": KIND_NONE,
            "document_id": None,
            "alt_text": "",
            "detail": (
                "the reply carries a custom-emoji entity with an unusable "
                "document id — it cannot be sent as a Premium emoji"
            ),
        }
    else:
        _trace(
            "SOURCE_ENTITY_FOUND",
            found=True,
            document_id=scan["document_id"],
        )
        _trace(
            "SOURCE_ENTITY_VALIDATED",
            document_id=scan["document_id"],
            offset=scan["offset"],
            length=scan["length"],
            span=_bounded(scan["span_text"]),
        )
        return {
            "kind": KIND_CUSTOM_EMOJI,
            "document_id": scan["document_id"],
            "alt_text": scan["span_text"],
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
    text = PROOF_PREFIX + glyph
    _trace(
        "OUTBOUND_ENTITY_BUILT",
        entity_type=entity["type"],
        document_id=entity["document_id"],
        offset=entity["offset"],
        length=entity["length"],
        text_utf16_len=utf16_length(text),
        used_placeholder=not has_alt,
    )
    return {
        "text": text,
        "entities": [entity],
        "entity": entity,
        "fallback_text": glyph,
        "used_placeholder": not has_alt,
    }


def validate_proof_payload(payload: Any) -> str:
    """The outbound payload's own validation — ``""`` means valid.

    The payload is the LAST thing this module controls before Telegram, so it
    is checked against itself: the entity must be a custom-emoji entity with a
    real document id, offsets/lengths must be usable UTF-16 units, and the
    span must cover exactly the fallback text the entity rides on. Any issue
    is returned as a reason string — the caller fails closed, it never sends a
    payload it could not validate.
    """
    if not isinstance(payload, dict):
        return "the payload is not a dict"
    text = payload.get("text")
    entity = payload.get("entity")
    if not isinstance(text, str) or not text:
        return "the payload carries no text"
    if not isinstance(entity, dict):
        return "the payload carries no entity"
    if entity.get("type") != "MessageEntityCustomEmoji":
        return f"unexpected entity type: {entity.get('type')!r}"
    if not _is_positive_int(entity.get("document_id")):
        return "the entity carries no usable document id"
    offset = entity.get("offset")
    length = entity.get("length")
    if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
        return "the entity offset is unusable"
    if not isinstance(length, int) or isinstance(length, bool) or length <= 0:
        return "the entity length is unusable"
    if not _span_text(text, offset, length):
        return "the entity span does not cover any text"
    if _span_text(text, offset, length) != payload.get("fallback_text"):
        return "the entity span does not cover the fallback text"
    if payload.get("entities") != [entity]:
        return "the payload must carry exactly the one entity"
    return ""


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


def _empty_readback(message_id: int | None = None) -> dict[str, Any]:
    """The read-back evidence record — every key present, an honest shape."""
    return {
        "attempted": False,
        "ok": False,
        "error": None,
        "message_id": message_id,
        "entity_present": None,
        "document_id": None,
        "offset": None,
        "length": None,
        "span_text": "",
        "document_id_match": None,
        "span_match": None,
        "stored_text_utf16_len": None,
        "document_alt": None,
        "document_alt_match": None,
        "document_alt_error": None,
    }


def _failed(
    error: str,
    detail: str,
    diagnosis: str,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """An honest not-sent result: no message id, no read-back, full payload."""
    result: dict[str, Any] = {
        "ok": False,
        "error": error,
        "detail": detail,
        "message_id": None,
        "diagnosis": diagnosis,
        "readback": _empty_readback(),
        "text": "",
        "entities": [],
        "entity": None,
        "fallback_text": "",
        "used_placeholder": False,
    }
    if payload is not None:
        result.update(payload)
    return result


def classify_diagnosis(readback: dict[str, Any]) -> str:
    """The ONE outcome the read-back evidence establishes — never more.

    A retained entity with the expected id and span is still only
    ``ENTITY_RETAINED_RENDER_UNVERIFIED``: whether the owner's client DISPLAYS
    a custom emoji is decided by Telegram and the viewing client, not by this
    read-back, so it is never reported as a successful visual render.
    """
    if not readback.get("ok"):
        return READBACK_FAILED
    if not readback.get("entity_present"):
        return ENTITY_STRIPPED_OR_MISSING
    if readback.get("document_id_match") and readback.get("span_match"):
        return ENTITY_RETAINED_RENDER_UNVERIFIED
    return ENTITY_MISMATCH


async def _fetch_sent_message(bot: Any, peer: Any, message_id: int) -> tuple[Any, str]:
    """Fetch the EXACT sent message through the HELPER BOT's own session.

    Targeted by id only — never a recent-messages scan, never an inferred
    "latest", never unrelated history. Returns ``(message, "")`` or
    ``(None, honest_reason)``; a fetch failure is reported separately from a
    send failure and never as one.
    """
    if bot is None:
        return None, "the helper bot is no longer connected for the read-back"
    try:
        fetched = await guarded_await(
            bot.get_messages(peer, ids=message_id),
            name="telegram:premium_probe:readback",
            timeout=_READBACK_TIMEOUT_S,
        )
    except asyncio.CancelledError:
        raise
    except asyncio.TimeoutError:
        return None, f"the read-back timed out after {_READBACK_TIMEOUT_S:g}s"
    except Exception as exc:
        return None, f"the read-back failed: {exc}"
    if fetched is None:
        return None, f"Telegram returned no message for id {message_id}"
    return fetched, ""


async def _resolve_document_alt(
    self_client: Any, document_id: int
) -> tuple[str | None, str]:
    """The custom-emoji document's real ``alt`` through the existing wrapper.

    Only requested when the read-back did NOT retain a matching entity: the
    comparison separates "the span did not match the document's alt text"
    from "the span matched but the entity was still dropped". A lookup failure
    returns ``(None, reason)`` and never fails the diagnosis.
    """
    try:
        documents = await get_custom_emoji_documents(self_client, [document_id])
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        return None, f"the document alt lookup failed: {exc}"
    for document in documents or []:
        if document.get("document_id") != document_id:
            continue
        alt = document.get("alt")
        if isinstance(alt, str) and alt:
            return alt, ""
        return None, "Telegram reported no alt text for the document"
    return None, "Telegram did not return the document for this id"


async def _read_back(
    self_client: Any,
    bot: Any,
    peer: Any,
    payload: dict[str, Any],
    message_id: int | None,
) -> tuple[dict[str, Any], str]:
    """Read the exact sent message back and classify ONLY what it shows."""
    expected = payload["entity"]
    expected_document_id = expected["document_id"]
    expected_offset = expected["offset"]
    expected_length = expected["length"]
    expected_span = payload["fallback_text"]
    readback = _empty_readback(message_id)
    if message_id is None:
        readback["error"] = "the send returned no message id to read back"
        _trace("READBACK_STARTED", started=False, reason="no_message_id")
        _trace("DIAGNOSIS", diagnosis=READBACK_FAILED)
        return readback, READBACK_FAILED
    readback["attempted"] = True
    _trace(
        "READBACK_STARTED",
        started=True,
        via="helper_bot_client",
        message_id=message_id,
        exact=True,
    )
    fetched, error = await _fetch_sent_message(bot, peer, message_id)
    if error:
        readback["error"] = error
        _trace("READBACK_RESULT", fetched=False, error=_bounded(error))
        _trace("DIAGNOSIS", diagnosis=READBACK_FAILED)
        return readback, READBACK_FAILED
    readback["ok"] = True
    stored_text = _message_text(fetched)
    readback["stored_text_utf16_len"] = utf16_length(stored_text)
    scan = _scan_custom_emoji(fetched)
    if scan is None:
        readback["entity_present"] = False
        readback["span_text"] = _span_text(stored_text, expected_offset, expected_length)
        readback["span_match"] = readback["span_text"] == expected_span
        _trace(
            "READBACK_RESULT",
            entity_present=False,
            text_utf16_len=readback["stored_text_utf16_len"],
            expected_span=_bounded(expected_span),
            stored_span=_bounded(readback["span_text"]),
            span_match=readback["span_match"],
        )
    else:
        readback["entity_present"] = True
        readback["document_id"] = scan["document_id"]
        readback["offset"] = scan["offset"]
        readback["length"] = scan["length"]
        readback["span_text"] = scan["span_text"]
        readback["document_id_match"] = scan["document_id"] == expected_document_id
        readback["span_match"] = (
            scan["offset"] == expected_offset
            and scan["length"] == expected_length
            and scan["span_text"] == expected_span
        )
        _trace(
            "READBACK_RESULT",
            entity_present=True,
            document_id=scan["document_id"],
            document_id_match=readback["document_id_match"],
            offset=scan["offset"],
            length=scan["length"],
            span_match=readback["span_match"],
            stored_span=_bounded(scan["span_text"]),
        )
    diagnosis = classify_diagnosis(readback)
    if diagnosis != ENTITY_RETAINED_RENDER_UNVERIFIED:
        alt, alt_error = await _resolve_document_alt(self_client, expected_document_id)
        readback["document_alt"] = alt
        readback["document_alt_error"] = alt_error or None
        if alt is not None:
            readback["document_alt_match"] = alt == expected_span
        _trace(
            "DOCUMENT_ALT",
            document_id=expected_document_id,
            alt=_bounded(alt) if alt is not None else None,
            match=readback["document_alt_match"],
            error=_bounded(alt_error) if alt_error else None,
        )
    _trace("DIAGNOSIS", diagnosis=diagnosis)
    return readback, diagnosis


def readback_summary(readback: Any) -> str:
    """ONE honest sentence about the read-back — never a render claim."""
    if not isinstance(readback, dict) or not readback.get("attempted"):
        return "no read-back was attempted."
    if not readback.get("ok"):
        reason = readback.get("error") or "unknown reason"
        return f"the read-back did not complete ({reason})."
    if not readback.get("entity_present"):
        alt = readback.get("document_alt")
        if isinstance(alt, str) and alt:
            alt_note = (
                "the sent span matches the document's alt text"
                if readback.get("document_alt_match")
                else "the sent span does NOT match the document's alt text"
            )
        elif readback.get("document_alt_error"):
            alt_note = (
                "the document's alt text could not be resolved "
                f"({readback['document_alt_error']})"
            )
        else:
            alt_note = "the document's alt text is unknown"
        return (
            "Telegram stored the message WITHOUT the custom-emoji entity — it "
            f"was stripped or ignored; {alt_note}."
        )
    return (
        f"Telegram stored the entity (document `#{readback.get('document_id')}`, "
        f"offset {readback.get('offset')}, length {readback.get('length')}); "
        f"document id matches: {'yes' if readback.get('document_id_match') else 'no'}, "
        f"expected span matches: {'yes' if readback.get('span_match') else 'no'} — a "
        "retained entity is not a verified render."
    )


async def deliver_proof(
    self_client: Any,
    owner_id: Any,
    document_id: Any,
    alt_text: Any,
) -> dict[str, Any]:
    """Have the HELPER BOT display ``document_id`` as a real custom emoji.

    Builds the payload, resolves the bot's own chat with the owner, sends
    exactly ONE message through the existing bridge, then READS THAT EXACT
    message back through the helper bot's own session and classifies what
    Telegram actually stored:

    * ``ok`` is True only when Telegram accepted the bot's send — a SEND fact,
      never a render claim;
    * ``diagnosis`` is one of this module's outcome constants, derived from the
      read-back evidence alone;
    * ``readback`` carries the read-back evidence, or the honest reason it
      could not be obtained (a read-back failure is reported separately from a
      send failure).

    Every failure carries a stable ``error`` code, a human-readable ``detail``
    and the payload that was (or would have been) sent, so a caller can never
    mistake a Unicode glyph for the Premium render.
    """
    if not _is_positive_int(owner_id):
        _trace("SEND_STARTED", started=False, reason="owner")
        return _failed(
            ERROR_OWNER, "no valid owner for this operation", SEND_FAILED
        )
    if not _is_positive_int(document_id):
        _trace("SEND_STARTED", started=False, reason="document_id")
        return _failed(
            ERROR_NO_EMOJI,
            "the reply carries no real custom-emoji document id",
            SOURCE_ENTITY_MISSING,
        )
    payload = build_proof_payload(document_id, alt_text)
    issue = validate_proof_payload(payload)
    if issue:
        _trace("OUTBOUND_ENTITY_BUILT", valid=False, issue=_bounded(issue))
        return _failed(
            ERROR_SEND,
            f"the outbound payload failed its own validation: {issue}",
            OUTBOUND_ENTITY_INVALID,
            payload,
        )
    if not bridge_available():
        _trace("SEND_STARTED", started=False, reason="helper_bot")
        return _failed(
            ERROR_NO_HELPER,
            "the helper bot is not connected — the premium emoji cannot be "
            "rendered by the bot",
            SEND_FAILED,
            payload,
        )
    peer, reason = await _resolve_bot_peer(owner_id)
    if peer is None:
        _trace("SEND_STARTED", started=False, reason="bot_chat")
        return _failed(ERROR_NO_BOT_CHAT, reason, SEND_FAILED, payload)
    _trace(
        "SEND_STARTED",
        started=True,
        path="helper_bot_bridge",
        via="helper_bot_client",
        document_id=payload["entity"]["document_id"],
        offset=payload["entity"]["offset"],
        length=payload["entity"]["length"],
        entity_count=len(payload["entities"]),
        text_utf16_len=utf16_length(payload["text"]),
    )
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
        _trace(
            "SEND_ACCEPTED",
            accepted=False,
            error=_bounded(f"{type(exc).__name__}: {exc}"),
        )
        _trace("DIAGNOSIS", diagnosis=SEND_FAILED)
        return _failed(
            ERROR_SEND, f"the helper bot's send was refused: {exc}", SEND_FAILED, payload
        )
    raw_id = sent.get("id") if isinstance(sent, dict) else None
    message_id = raw_id if _is_positive_int(raw_id) else None
    _trace("SEND_ACCEPTED", accepted=True, message_id=message_id)
    readback, diagnosis = await _read_back(
        self_client, helper_client.get_client(), peer, payload, message_id
    )
    return {
        "ok": True,
        "error": None,
        "detail": None,
        "message_id": message_id,
        "diagnosis": diagnosis,
        "readback": readback,
        **payload,
    }
