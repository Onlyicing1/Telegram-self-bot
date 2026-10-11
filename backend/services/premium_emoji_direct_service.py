"""
Premium custom emoji — DIRECT owner-authored delivery (controlled live test).

A genuine ``MessageEntityCustomEmoji`` can leave this project through three
mechanisms, and they are NOT interchangeable:

1. **the helper bot sends it itself** (``telegram_api/bridge.py``) — the closed
   POC route, where Telegram dropped the entity server-side;
2. **the owner's account sends a helper-bot-supplied inline result**
   (``messages.sendInlineBotResult``) — ``premium_emoji_inline_service``, the
   production route, where Telegram stamps ``via_bot_id`` and where three live
   attempts ended at ``INLINE_RESULT_ENTITY_MISSING`` (the stored inline result
   carried no entity, so the fail-closed gate refused the send);
3. **the owner's own account sends it directly** (``messages.sendMessage``
   carrying a custom-emoji entity) — **the mechanism this module owns**, and
   the only one whose entity preservation has never been established.

Whether (3) keeps the entity is a question only Telegram can answer, so this
module exists to run ONE bounded, honest test of it inside the live runtime:

    the owner's real custom-emoji entity (document id + UTF-16 geometry)
      → the document's own ``alt`` text (Telegram's documented rule)
        → ONE plainly labelled message to the owner's own Saved Messages,
          sent by the owner's own client
          → the EXACT stored message fetched back by id
            → entity preservation and bot attribution judged SEPARATELY

Contracts:

* **Entity, never glyph.** The document id comes from the entity; the glyph is
  only the text the entity rides on. There is no Unicode fallback here: a
  stripped entity is reported as stripped and never re-sent as a glyph.
* **Submission ≠ preservation.** ``ok`` is the RPC fact (Telegram accepted the
  send); ``entity_preserved`` is the read-back fact (the stored message's own
  entity matches the expected document id and UTF-16 geometry). Neither is
  inferred from the outbound request.
* **Preservation ≠ attribution.** A direct owner-authored message carries no
  ``via_bot_id``. The stored message's own attribution is recorded separately
  (``attribution``) and this route NEVER claims to satisfy an attribution
  requirement — the two verdicts live in different fields.
* **Saved Messages only.** A destination that is not the owner's own account is
  refused up front (:data:`UNSUPPORTED_DESTINATION`) rather than claimed, and
  the read-back additionally records the stored message's own chat.
* **Diagnostic entry point, not a production route.** Nothing else in the
  project sends through this module; the production inline route and its
  fail-closed inline-result inspection are untouched, and no caller falls back
  to this path when that one fails.
* **No new infrastructure.** No second client, loop, scheduler, executor or
  listener: the caller supplies the existing self client, and every Telegram
  call is bounded by the existing wrappers.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from telethon.tl.types import MessageEntityCustomEmoji

from backend.services.premium_emoji_inline_service import (
    ERROR_DESTINATION,
    ERROR_OWNER,
    ERROR_PAYLOAD,
    ERROR_READBACK,
    ERROR_SEND,
    ERROR_SOURCE,
    OUTBOUND_PAYLOAD_INVALID,
    READBACK_FAILED,
    SOURCE_ENTITY_MISSING,
    STORED_ENTITY_MISMATCH,
    STORED_ENTITY_STRIPPED,
    STORED_ENTITY_VERIFIED_RENDER_UNVERIFIED,
    UNSUPPORTED_DESTINATION,
    _bounded,
    _document_facts,
    _empty_eligibility,
    _empty_readback,
    _fetch_stored_message,
    _is_positive_int,
    _message_text,
    _owner_premium,
    _scan_custom_emoji,
    _span_text,
    build_inline_payload,
    validate_inline_payload,
)
from backend.telegram_api._helpers import dict_entities_to_tl, utf16_length
from backend.telegram_api.messages import send_message as send_typed_message

logger = logging.getLogger(__name__)

#: The MTProto operation a direct send performs — recorded, never inferred.
SEND_PATH_DIRECT_SELF = "messages.sendMessage (self account)"

#: The plain-text label the live-test message carries, so the artifact is
#: unmistakable in Saved Messages and cannot be confused with the inline
#: route's own artifact (``premium_emoji_inline_service.PREFIX``). The entity
#: offset is computed from it in UTF-16 code units — never a character count.
DIRECT_PREFIX = "Direct self-chat test: "

#: Diagnoses this route adds. The shared outcome constants are reused as-is.
DIRECT_SEND_FAILED = "DIRECT_SEND_FAILED"
STORED_UNEXPECTED_ATTRIBUTION = "STORED_UNEXPECTED_ATTRIBUTION"
STORED_DESTINATION_MISMATCH = "STORED_DESTINATION_MISMATCH"


def _trace(stage: str, **fields: Any) -> None:
    """ONE structured line per stage, grep-friendly under its own prefix.

    Bounded by construction: ids, offsets, lengths, booleans and honest
    reasons — never session data, tokens, access hashes or whole messages.
    """
    parts = [stage]
    for key, value in fields.items():
        if value is None or value == "":
            continue
        parts.append(f"{key}={value}")
    logger.info("[PREMIUM_DIRECT] %s", " ".join(parts))


def _attribution(via_bot_id: Any) -> dict[str, Any]:
    """The stored message's attribution — recorded, never demanded.

    The direct route is owner-authored, so ``via_bot_id`` is not *expected*;
    this block states that explicitly instead of quietly treating an absent
    attribution as a satisfied one.
    """
    return {
        "applicable": False,
        "via_bot_id": via_bot_id,
        "via_bot_match": None,
        "note": (
            "a direct owner-authored message carries no bot attribution, so "
            "this route does not satisfy a via_bot_id requirement"
        ),
    }


def _failed(
    error: str,
    detail: str,
    diagnosis: str,
    payload: dict[str, Any] | None = None,
    eligibility: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """An honest not-verified result: no message id, no read-back verdict."""
    return {
        "ok": False,
        "entity_preserved": None,
        "verified": False,
        "error": error,
        "detail": detail,
        "diagnosis": diagnosis,
        "send_path": None,
        "destination_chat_id": None,
        "message_id": None,
        "payload": payload,
        "readback": _empty_readback(),
        "eligibility": eligibility if eligibility is not None else _empty_eligibility(),
        "attribution": _attribution(None),
    }


async def _tl_custom_emoji_entities(
    self_client: Any, payload: dict[str, Any]
) -> tuple[list[Any] | None, str]:
    """Rebuild the validated payload into the REAL TL entity the send carries.

    The payload is the last thing this module controls before Telegram, so the
    rebuilt objects are checked against it: exactly ONE entity, a real
    ``MessageEntityCustomEmoji``, with the payload's own document id, offset
    and length. Any deviation fails closed and nothing is sent.
    """
    try:
        entities = await dict_entities_to_tl(self_client, payload["entities"])
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        return None, f"the outbound entity could not be rebuilt: {exc}"
    expected = payload["entity"]
    if len(entities) != 1 or not isinstance(entities[0], MessageEntityCustomEmoji):
        return None, "the outbound entity list is not exactly one custom-emoji entity"
    entity = entities[0]
    if (
        getattr(entity, "document_id", None) != expected["document_id"]
        or getattr(entity, "offset", None) != expected["offset"]
        or getattr(entity, "length", None) != expected["length"]
    ):
        return None, "the rebuilt entity does not match the validated payload"
    return entities, ""


def _read_back_direct(
    stored: Any, payload: dict[str, Any], expected_chat_id: Any
) -> dict[str, Any]:
    """Classify ONLY what the stored message shows — entity, chat, attribution.

    ``entity_preserved`` is this route's decisive field: it is True only when
    the stored message carries a real ``MessageEntityCustomEmoji`` whose
    document id AND UTF-16 offset/length/span match what was sent.
    ``destination_match`` compares the stored message's own chat with the
    destination this route was allowed to write to, and ``via_bot_id`` is
    recorded separately and never required here.
    """
    expected = payload["entity"]
    evidence = _empty_readback()
    evidence["attempted"] = True
    evidence["ok"] = True
    text = _message_text(stored)
    evidence["stored_text"] = text
    evidence["stored_text_utf16_len"] = utf16_length(text)
    evidence["text_match"] = text == payload["text"]
    stored_chat_id = getattr(stored, "chat_id", None)
    # A stored chat id is any int (groups/channels are negative); only an
    # unreadable one keeps the destination verdict unknown.
    readable_chat = isinstance(stored_chat_id, int) and not isinstance(
        stored_chat_id, bool
    )
    evidence["stored_chat_id"] = stored_chat_id if readable_chat else None
    evidence["destination_match"] = (
        stored_chat_id == expected_chat_id if readable_chat else None
    )
    via_bot_id = getattr(stored, "via_bot_id", None)
    evidence["via_bot_id"] = via_bot_id
    evidence["via_bot_match"] = None
    scan = _scan_custom_emoji(text, getattr(stored, "entities", None))
    if scan is None:
        evidence["entity_present"] = False
        evidence["entity_preserved"] = False
        evidence["span_text"] = _span_text(text, expected["offset"], expected["length"])
        evidence["span_match"] = evidence["span_text"] == payload["glyph"]
        _trace(
            "READBACK_RESULT",
            entity_present=False,
            entity_preserved=False,
            destination_match=evidence["destination_match"],
            via_bot_id=via_bot_id,
            text_utf16_len=evidence["stored_text_utf16_len"],
            stored_span=_bounded(evidence["span_text"]),
        )
        return evidence
    evidence["entity_present"] = True
    evidence["document_id"] = scan["document_id"]
    evidence["offset"] = scan["offset"]
    evidence["length"] = scan["length"]
    evidence["span_text"] = scan["span_text"]
    evidence["document_id_match"] = scan["document_id"] == expected["document_id"]
    evidence["span_match"] = (
        scan["offset"] == expected["offset"]
        and scan["length"] == expected["length"]
        and scan["span_text"] == payload["glyph"]
    )
    evidence["entity_preserved"] = bool(
        evidence["document_id_match"] and evidence["span_match"]
    )
    _trace(
        "READBACK_RESULT",
        entity_present=True,
        entity_preserved=evidence["entity_preserved"],
        document_id=scan["document_id"],
        document_id_match=evidence["document_id_match"],
        offset=scan["offset"],
        length=scan["length"],
        span_match=evidence["span_match"],
        destination_match=evidence["destination_match"],
        via_bot_id=via_bot_id,
    )
    return evidence


def classify_direct_diagnosis(readback: dict[str, Any]) -> str:
    """The ONE outcome the read-back evidence establishes — never more.

    Order matters: a read-back that did not complete proves nothing about the
    entity; a message fetched from a chat this route never wrote to is not the
    artifact at all; a missing entity is stripping; an entity with the wrong
    document id or span is a mismatch; an entity whose message is
    bot-attributed did not come from this route; only a genuine, matching,
    unattributed entity is verified — and even then only as *stored*, never as
    *rendered*.
    """
    if not readback.get("ok"):
        return READBACK_FAILED
    if readback.get("destination_match") is False:
        return STORED_DESTINATION_MISMATCH
    if not readback.get("entity_present"):
        return STORED_ENTITY_STRIPPED
    if not (readback.get("document_id_match") and readback.get("span_match")):
        return STORED_ENTITY_MISMATCH
    if readback.get("via_bot_id") is not None:
        return STORED_UNEXPECTED_ATTRIBUTION
    return STORED_ENTITY_VERIFIED_RENDER_UNVERIFIED


async def send_premium_emoji_direct(
    self_client: Any,
    chat_id: Any,
    document_id: Any,
    source_glyph: Any,
    owner_id: Any,
) -> dict[str, Any]:
    """Send ONE genuine custom emoji as the OWNER, directly, to Saved Messages.

    The destination is the owner's Saved Messages only (``chat_id ==
    owner_id``). ``source_glyph`` is the text the SOURCE entity's span covered;
    the glyph actually wrapped on the way out is Telegram's own ``alt`` for the
    document when it resolves, so the documented "the entity must wrap exactly
    the emoji in ``documentAttributeCustomEmoji.alt``" rule holds by
    construction (the fallback is recorded as ``glyph_source``).

    Returns the full record: ``ok`` (the RPC was accepted — a SEND fact),
    ``entity_preserved`` (the stored message's own entity matches — the
    decisive fact, ``None`` when it could not be judged), ``verified``
    (preserved AND unattributed AND in Saved Messages), ``attribution``,
    ``diagnosis``, ``send_path``, ``message_id``, ``payload`` and the
    ``readback`` / ``eligibility`` evidence blocks.
    """
    if not _is_positive_int(owner_id):
        _trace("DIRECT_SEND_STARTED", started=False, reason="owner")
        return _failed(
            ERROR_OWNER,
            "no valid owner for this operation",
            SOURCE_ENTITY_MISSING,
        )
    if not _is_positive_int(chat_id) or chat_id != owner_id:
        _trace("DIRECT_SEND_STARTED", started=False, reason="destination")
        return _failed(
            ERROR_DESTINATION,
            "only your Saved Messages is a supported destination for this test "
            "— nothing was sent",
            UNSUPPORTED_DESTINATION,
        )
    if not _is_positive_int(document_id):
        _trace("DIRECT_SEND_STARTED", started=False, reason="document_id")
        return _failed(
            ERROR_SOURCE,
            "the source message carries no usable custom-emoji document id",
            SOURCE_ENTITY_MISSING,
        )

    eligibility = _empty_eligibility()
    premium, premium_error = await _owner_premium(self_client)
    eligibility["owner_premium"] = premium
    eligibility["owner_premium_error"] = premium_error or None

    facts, facts_error = await _document_facts(self_client, int(document_id))
    eligibility["document"] = facts
    eligibility["document_error"] = facts_error or None
    glyph, glyph_source = "", ""
    if facts and isinstance(facts.get("alt"), str) and facts["alt"]:
        glyph, glyph_source = facts["alt"], "document_alt"
    elif isinstance(source_glyph, str) and source_glyph:
        glyph, glyph_source = source_glyph, "source_span"
    eligibility["glyph_source"] = glyph_source
    if not glyph:
        _trace("DIRECT_SEND_STARTED", started=False, reason="glyph")
        return _failed(
            ERROR_SOURCE,
            "the emoji's own fallback text could not be resolved — Telegram's "
            "documented rule requires the entity to wrap exactly the emoji in "
            "the document's alt text, so nothing was sent",
            SOURCE_ENTITY_MISSING,
            None,
            eligibility,
        )

    # The outbound payload has the same shape as the inline route's (one
    # custom-emoji entity over plain text) — only the transport differs.
    payload = build_inline_payload(int(document_id), glyph, DIRECT_PREFIX)
    issue = validate_inline_payload(payload)
    if issue:
        _trace("DIRECT_PAYLOAD_BUILT", valid=False, issue=_bounded(issue))
        return _failed(
            ERROR_PAYLOAD,
            f"the outbound payload failed its own validation: {issue}",
            OUTBOUND_PAYLOAD_INVALID,
            payload,
            eligibility,
        )
    _trace(
        "SOURCE_ENTITY_ACCEPTED",
        document_id=payload["entity"]["document_id"],
        offset=payload["entity"]["offset"],
        length=payload["entity"]["length"],
        text_utf16_len=utf16_length(payload["text"]),
    )

    entities, entity_error = await _tl_custom_emoji_entities(self_client, payload)
    if entities is None:
        _trace("DIRECT_PAYLOAD_BUILT", valid=False, issue=_bounded(entity_error))
        return _failed(
            ERROR_PAYLOAD,
            f"the outbound entity failed its own validation: {entity_error}",
            OUTBOUND_PAYLOAD_INVALID,
            payload,
            eligibility,
        )

    _trace(
        "DIRECT_SEND_STARTED",
        started=True,
        path=SEND_PATH_DIRECT_SELF,
        destination="saved_messages",
        document_id=payload["entity"]["document_id"],
        offset=payload["entity"]["offset"],
        length=payload["entity"]["length"],
        entity_count=len(payload["entities"]),
        text_utf16_len=utf16_length(payload["text"]),
    )
    try:
        sent = await send_typed_message(
            self_client, chat_id, payload["text"], formatting_entities=entities
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning("[PREMIUM_DIRECT] the direct send failed: %s", exc)
        _trace(
            "DIRECT_SEND_ACCEPTED",
            accepted=False,
            error=_bounded(f"{type(exc).__name__}: {exc}"),
        )
        _trace("DIAGNOSIS", diagnosis=DIRECT_SEND_FAILED)
        return _failed(
            ERROR_SEND,
            f"the direct send failed: {exc}",
            DIRECT_SEND_FAILED,
            payload,
            eligibility,
        )

    raw_id = sent.get("id") if isinstance(sent, dict) else getattr(sent, "id", None)
    message_id = raw_id if _is_positive_int(raw_id) else None
    _trace("DIRECT_SEND_ACCEPTED", accepted=True, message_id=message_id)

    record: dict[str, Any] = {
        "ok": True,
        "entity_preserved": None,
        "verified": False,
        "error": None,
        "detail": None,
        "diagnosis": READBACK_FAILED,
        "send_path": SEND_PATH_DIRECT_SELF,
        "destination_chat_id": chat_id,
        "message_id": message_id,
        "payload": payload,
        "readback": _empty_readback(message_id),
        "eligibility": eligibility,
        "attribution": _attribution(None),
    }
    if message_id is None:
        record["readback"]["error"] = (
            "the send returned no message id — the stored message cannot be read back"
        )
        record["error"] = ERROR_READBACK
        record["detail"] = (
            "the direct send returned no message id — the stored message cannot "
            "be read back, so nothing about the entity is proven"
        )
        _trace("DIAGNOSIS", diagnosis=READBACK_FAILED)
        return record

    stored, readback_error = await _fetch_stored_message(
        self_client, chat_id, message_id
    )
    if stored is None:
        readback = _empty_readback(message_id)
        readback["attempted"] = True
        readback["error"] = readback_error
        record["readback"] = readback
        record["error"] = ERROR_READBACK
        record["detail"] = (
            "the message was accepted but could not be read back, so nothing "
            f"about the stored entity is proven: {readback_error}"
        )
        _trace("READBACK_RESULT", fetched=False, error=_bounded(readback_error))
        _trace("DIAGNOSIS", diagnosis=READBACK_FAILED)
        return record

    readback = _read_back_direct(stored, payload, chat_id)
    record["readback"] = readback
    record["entity_preserved"] = readback["entity_preserved"]
    record["attribution"] = _attribution(readback.get("via_bot_id"))
    record["diagnosis"] = classify_direct_diagnosis(readback)
    record["verified"] = record["diagnosis"] == STORED_ENTITY_VERIFIED_RENDER_UNVERIFIED
    if record["diagnosis"] != STORED_ENTITY_VERIFIED_RENDER_UNVERIFIED:
        record["error"] = ERROR_READBACK
        record["detail"] = outcome_summary(record)
    _trace("DIAGNOSIS", diagnosis=record["diagnosis"])
    return record


def outcome_summary(result: Any) -> str:
    """ONE honest sentence about the outcome — never a render claim."""
    if not isinstance(result, dict):
        return "the send produced no result."
    if result.get("error") and result.get("diagnosis") == DIRECT_SEND_FAILED:
        return str(result.get("detail") or "the direct send did not happen.")
    diagnosis = result.get("diagnosis")
    expected = (result.get("payload") or {}).get("entity") or {}
    if diagnosis == STORED_ENTITY_VERIFIED_RENDER_UNVERIFIED:
        return (
            "Telegram stored the genuine custom-emoji entity on your own "
            f"message (document `#{expected.get('document_id')}`, offset "
            f"{expected.get('offset')}, length {expected.get('length')}) and the "
            "message is NOT bot-attributed — so this proves entity preservation "
            "only, never a `via_bot_id` attribution, and whether your client "
            "renders it is still for you to confirm visually."
        )
    if diagnosis == STORED_DESTINATION_MISMATCH:
        return (
            "the stored message was fetched from a chat that is not your Saved "
            "Messages, so it is not the artifact this route created — nothing "
            "about its entity is claimed."
        )
    if diagnosis == STORED_UNEXPECTED_ATTRIBUTION:
        return (
            "the stored message carries the custom-emoji entity but ALSO a "
            f"`via_bot_id` ({result.get('attribution', {}).get('via_bot_id')!r}) — "
            "a direct owner-authored send cannot produce that, so the message "
            "did not come from this route."
        )
    if diagnosis == STORED_ENTITY_STRIPPED:
        return (
            "Telegram accepted your own send but stored the message WITHOUT the "
            "custom-emoji entity — the direct route does not preserve it."
        )
    if diagnosis == STORED_ENTITY_MISMATCH:
        return (
            "Telegram stored a custom-emoji entity that does not match the one "
            "that was sent (document id or UTF-16 span differs)."
        )
    if diagnosis == READBACK_FAILED:
        return (
            "the message was accepted but could not be read back, so nothing "
            f"about the stored entity is proven ({result.get('readback', {}).get('error')})."
        )
    return str(result.get("detail") or f"outcome {diagnosis!r}.")
