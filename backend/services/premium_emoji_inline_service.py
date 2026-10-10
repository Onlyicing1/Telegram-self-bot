"""
Premium custom emoji through the inline-bot path — production feature.

Telegram has exactly TWO ways a genuine ``MessageEntityCustomEmoji`` can leave
this project, and they are not the same mechanism:

1. **the helper bot sends it itself** (``messages.sendMessage`` as the bot,
   ``backend/telegram_api/bridge.py``) — the path the closed POC tested, where
   Telegram dropped the entity server-side;
2. **the user account sends a bot-supplied inline result**
   (``messages.sendInlineBotResult``, the machinery every Glass UI panel
   already uses) — the sender is the owner's own account, the bot only
   supplies the result, and Telegram stamps ``via_bot_id``.

This module implements (2) and nothing else. It is the ONLY place that puts a
custom-emoji entity into an ``InputBotInlineMessageText`` and reads the sent
message back by exact id.

Pipeline (each stage fails closed with its own honest diagnosis):

    source message (the owner's reply, a REAL Telegram message)
      → entity-only extraction: document_id + UTF-16 offset/length + span text
        → document facts (alt, free, text_color) through the existing wrapper
          → payload: outbound text + the custom-emoji entity covering exactly
            the alt emoji at its real UTF-16 offset
            → the helper bot answers the inline query with that payload
              → the SELF account sends the result (via_bot_id = helper bot)
                → the EXACT stored message is read back and classified

Contracts:

* **Entity, never glyph.** Only a real ``MessageEntityCustomEmoji`` with a
  usable positive ``document_id`` counts. A plain Unicode emoji, an empty or
  media-only message, or an unusable id fails closed with
  :data:`SOURCE_ENTITY_MISSING` — there is no Unicode fallback anywhere in
  this module, and a stripped entity is reported as stripped, never
  re-sent as a glyph.
* **The user sends, the bot supplies.** This module never sends the emoji as
  the bot and never fabricates ``via_bot_id``; attribution is Telegram's own
  verdict and is verified on the stored message.
* **Saved Messages only.** The documented non-Premium allowance for custom
  emoji is scoped to the self-chat, so a destination that is not the owner's
  own account is refused up front (:data:`UNSUPPORTED_DESTINATION`) instead
  of being claimed as supported.
* **Telegram's verdict is reported, not assumed.** The inline result is
  inspected BEFORE it is sent (did Telegram keep the entity in the stored
  result?) and the sent message is read back afterwards, so an
  implementation defect is never confused with a Telegram-side restriction.
  A retained entity is ``…_RENDER_UNVERIFIED`` — display is the viewing
  client's decision, never this module's claim.
* **No new infrastructure.** No second client, loop, scheduler, executor or
  listener; the self client and the existing helper bot are supplied by the
  caller, and the inline query/send go through
  ``backend.helper.inline_engine`` — the same flow the Glass UI uses.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from telethon import types

from backend.helper import client as helper_client
from backend.helper import inline_engine
from backend.runtime.operation_watchdog import guarded_await
from backend.telegram_api._helpers import utf16_index_at, utf16_length
from backend.telegram_api.custom_emoji import get_custom_emoji_documents
from backend.telegram_api.entities import get_me

logger = logging.getLogger(__name__)

#: The inline query key the helper answers on (``<key>:<document_id>:<glyph>``).
INLINE_QUERY_KEY = "premium_emoji_send"

#: Plain-text label the sent message carries before the emoji, so the owner can
#: identify the artifact in Saved Messages. The entity offset is computed from
#: it in UTF-16 code units — never from a character count.
PREFIX = "Premium emoji: "

#: The three verdicts of :func:`inspect_source_message`.
KIND_CUSTOM_EMOJI = "custom_emoji"
KIND_UNICODE = "unicode"
KIND_NONE = "none"

ERROR_OWNER = "E_OWNER"
ERROR_DESTINATION = "E_DESTINATION"
ERROR_SOURCE = "E_SOURCE"
ERROR_PAYLOAD = "E_PAYLOAD"
ERROR_HELPER = "E_HELPER"
ERROR_QUERY = "E_QUERY"
ERROR_SEND = "E_SEND"
ERROR_READBACK = "E_READBACK"

#: The ONE outcome each piece of evidence establishes — never more.
SOURCE_ENTITY_MISSING = "SOURCE_ENTITY_MISSING"
UNSUPPORTED_DESTINATION = "UNSUPPORTED_DESTINATION"
OUTBOUND_PAYLOAD_INVALID = "OUTBOUND_PAYLOAD_INVALID"
INLINE_UNAVAILABLE = "INLINE_UNAVAILABLE"
INLINE_RESULT_EMPTY = "INLINE_RESULT_EMPTY"
INLINE_RESULT_UNSUPPORTED = "INLINE_RESULT_UNSUPPORTED"
INLINE_RESULT_NO_SEND_MESSAGE = "INLINE_RESULT_NO_SEND_MESSAGE"
INLINE_RESULT_REJECTED = "INLINE_RESULT_REJECTED"
INLINE_RESULT_ENTITY_MISSING = "INLINE_RESULT_ENTITY_MISSING"
INLINE_SEND_FAILED = "INLINE_SEND_FAILED"
READBACK_FAILED = "READBACK_FAILED"
STORED_ENTITY_STRIPPED = "STORED_ENTITY_STRIPPED"
STORED_ENTITY_MISMATCH = "STORED_ENTITY_MISMATCH"
STORED_ATTRIBUTION_MISSING = "STORED_ATTRIBUTION_MISSING"
STORED_ENTITY_VERIFIED_RENDER_UNVERIFIED = "STORED_ENTITY_VERIFIED_RENDER_UNVERIFIED"

#: The actual MTProto path a successful send used — recorded, never inferred.
SEND_PATH_INLINE_BOT_RESULT = "messages.sendInlineBotResult"

#: The two real TL results ``getInlineBotResults`` can return. Telethon hands
#: them over inside ``custom.InlineResult`` WRAPPERS (``.result`` carries the raw
#: object; the wrapper has NO ``send_message`` of its own), so both the raw
#: object and the wrapper have to be understood.
_INLINE_RESULT_TYPES = (types.BotInlineResult, types.BotInlineMediaResult)

#: Bounds on the individual Telegram calls this module drives (the handler's
#: own bound is a backstop; the wrappers bound their internals themselves).
_INLINE_CALL_TIMEOUT_S = 45.0
_READBACK_TIMEOUT_S = 30.0


def _trace(stage: str, **fields: Any) -> None:
    """ONE structured line per pipeline stage, grep-friendly by stage name.

    Bounded by construction: ids, offsets, lengths, short quoted spans and
    honest reason strings — never session data, tokens or whole messages.
    """
    parts = [stage]
    for key, value in fields.items():
        if value is None or value == "":
            continue
        parts.append(f"{key}={value}")
    logger.info("[PREMIUM_INLINE] %s", " ".join(parts))


def _bounded(value: Any, limit: int = 32) -> str:
    """A short, quoted form of a span/reason for one trace line."""
    text = str(value)
    return repr(text[:limit] + ("…" if len(text) > limit else ""))


def _is_positive_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _class_path(value: Any) -> str:
    """``module.Class`` of a runtime object — bounded, non-sensitive metadata."""
    cls = type(value)
    return f"{getattr(cls, '__module__', '')}.{getattr(cls, '__qualname__', cls.__name__)}"


def _message_text(message: Any) -> str:
    """The message's own text — raw, before any per-client rendering."""
    raw = getattr(message, "message", None)
    if not isinstance(raw, str) or not raw:
        raw = getattr(message, "text", None)
    return raw if isinstance(raw, str) else ""


def _span_text(text: str, offset: Any, length: Any) -> str:
    """The text an entity's UTF-16 span covers, or ``""`` when unusable.

    A corrupt span (out of range, mid-surrogate) yields no text rather than a
    clamped guess.
    """
    if not isinstance(offset, int) or isinstance(offset, bool):
        return ""
    if not isinstance(length, int) or isinstance(length, bool):
        return ""
    if offset < 0 or length <= 0:
        return ""
    try:
        start = utf16_index_at(text, offset)
        end = utf16_index_at(text, offset + length)
    except ValueError:
        return ""
    return text[start:end]


def _scan_custom_emoji(text: str, entities: Any) -> dict[str, Any] | None:
    """The FIRST real custom-emoji entity of an entity list — entity only.

    Works for both sides of the flow: the source message's entities and the
    entities Telegram returns inside a stored ``BotInlineMessageText``. Returns
    ``{document_id, offset, length, span_text}`` (``document_id`` is ``None``
    when Telegram's value is unusable) or ``None`` when no custom-emoji entity
    exists at all — the visible glyph is never a substitute.
    """
    for entity in entities or []:
        if not isinstance(entity, types.MessageEntityCustomEmoji):
            continue
        document_id = getattr(entity, "document_id", None)
        offset = getattr(entity, "offset", None)
        length = getattr(entity, "length", None)
        return {
            "document_id": document_id if _is_positive_int(document_id) else None,
            "offset": offset if isinstance(offset, int) and not isinstance(offset, bool) else None,
            "length": length if isinstance(length, int) and not isinstance(length, bool) else None,
            "span_text": _span_text(text, offset, length),
        }
    return None


def inspect_source_message(message: Any) -> dict[str, Any]:
    """The custom-emoji / unicode / none verdict for ONE real Telegram message.

    Returns ``{kind, document_id, offset, length, span_text, detail}``.
    ``kind`` is :data:`KIND_CUSTOM_EMOJI` only for a real
    ``MessageEntityCustomEmoji`` with a usable ``document_id`` AND a span that
    resolves inside the message text; everything else is an honest refusal
    (:data:`KIND_UNICODE` for a plain visible reply, :data:`KIND_NONE`
    otherwise) carrying the reason in ``detail``. Nothing is ever promoted to
    "the emoji".
    """
    text = _message_text(message)
    scan = _scan_custom_emoji(text, getattr(message, "entities", None))
    if scan is None:
        _trace("SOURCE_ENTITY_FOUND", found=False)
    elif scan["document_id"] is None:
        _trace("SOURCE_ENTITY_FOUND", found=True, usable=False)
        return {
            "kind": KIND_NONE,
            "document_id": None,
            "offset": None,
            "length": None,
            "span_text": "",
            "detail": (
                "the reply carries a custom-emoji entity with an unusable "
                "document id — it cannot be sent as a custom emoji"
            ),
        }
    elif not scan["span_text"]:
        _trace("SOURCE_ENTITY_FOUND", found=True, usable=False, reason="span")
        return {
            "kind": KIND_NONE,
            "document_id": None,
            "offset": None,
            "length": None,
            "span_text": "",
            "detail": (
                "the reply's custom-emoji entity has a span that does not "
                "resolve inside the message text — nothing can be rebuilt from it"
            ),
        }
    else:
        _trace(
            "SOURCE_ENTITY_VALIDATED",
            document_id=scan["document_id"],
            offset=scan["offset"],
            length=scan["length"],
            span=_bounded(scan["span_text"]),
            text_utf16_len=utf16_length(text),
        )
        return {
            "kind": KIND_CUSTOM_EMOJI,
            "document_id": scan["document_id"],
            "offset": scan["offset"],
            "length": scan["length"],
            "span_text": scan["span_text"],
            "detail": "",
        }
    visible = text.strip()
    if visible:
        return {
            "kind": KIND_UNICODE,
            "document_id": None,
            "offset": None,
            "length": None,
            "span_text": visible,
            "detail": (
                "the reply carries no Telegram custom-emoji entity — a plain "
                "Unicode emoji is not a Premium custom emoji"
            ),
        }
    return {
        "kind": KIND_NONE,
        "document_id": None,
        "offset": None,
        "length": None,
        "span_text": "",
        "detail": "the reply carries no Telegram custom-emoji entity",
    }


def build_inline_payload(document_id: Any, glyph: Any, prefix: str = PREFIX) -> dict[str, Any]:
    """The EXACT ``InputBotInlineMessageText`` payload for ONE custom emoji.

    ``{text, entities, entity, glyph, prefix, span_utf16_len}`` — the label,
    the real ``MessageEntityCustomEmoji`` (UTF-16 offset/length, the REAL
    ``document_id``) and the text the entity must wrap. The entity is the
    payload; the glyph is only the character it rides on.
    """
    if not _is_positive_int(document_id):
        raise ValueError("build_inline_payload requires a real custom-emoji document id")
    if not isinstance(glyph, str) or not glyph:
        raise ValueError("build_inline_payload requires the emoji glyph the entity wraps")
    if not isinstance(prefix, str):
        raise ValueError("build_inline_payload requires a string prefix")
    offset = utf16_length(prefix)
    length = utf16_length(glyph)
    entity = {
        "type": "MessageEntityCustomEmoji",
        "offset": offset,
        "length": length,
        "document_id": int(document_id),
    }
    text = prefix + glyph
    _trace(
        "OUTBOUND_PAYLOAD_BUILT",
        document_id=int(document_id),
        offset=offset,
        length=length,
        text_utf16_len=utf16_length(text),
    )
    return {
        "text": text,
        "entities": [entity],
        "entity": entity,
        "glyph": glyph,
        "prefix": prefix,
        "span_utf16_len": length,
    }


def validate_inline_payload(payload: Any) -> str:
    """The outbound payload's own validation — ``""`` means valid.

    The payload is the last thing this module controls before the helper bot
    submits it, so it is checked against itself: a custom-emoji entity with a
    real document id, usable UTF-16 offset/length, a span that resolves in the
    text and covers EXACTLY the glyph the entity rides on.
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
    span = _span_text(text, offset, length)
    if not span:
        return "the entity span does not cover any text"
    if span != payload.get("glyph"):
        return "the entity span does not cover the emoji glyph"
    if length != utf16_length(payload.get("glyph") or ""):
        return "the entity length does not match the glyph in UTF-16 units"
    if payload.get("entities") != [entity]:
        return "the payload must carry exactly the one entity"
    return ""


def build_inline_result(
    document_id: Any, glyph: Any, prefix: str = PREFIX, title: str = ""
) -> types.InputBotInlineResult:
    """The helper bot's answer: an article result carrying the REAL entity.

    ``InputBotInlineMessageText.entities`` is the schema capability the whole
    feature rests on — the entity is validated as a payload first, then built
    as a TL object. Raises ``ValueError`` when the payload cannot be validated
    (the inline builder refuses to submit something it could not check).
    """
    payload = build_inline_payload(document_id, glyph, prefix)
    issue = validate_inline_payload(payload)
    if issue:
        raise ValueError(f"refusing to build an inline result: {issue}")
    entity = payload["entity"]
    message = types.InputBotInlineMessageText(
        message=payload["text"],
        entities=[
            types.MessageEntityCustomEmoji(
                entity["offset"], entity["length"], entity["document_id"]
            )
        ],
    )
    label = title or f"Premium emoji {glyph}"
    return types.InputBotInlineResult(
        id="0",
        type="article",
        title=label[:255],
        send_message=message,
    )


def inline_query_for(document_id: Any, glyph: Any) -> str:
    """The inline query that carries the emoji to the helper bot.

    The entity itself is rebuilt by :func:`build_inline_result` from these two
    values; the query is transport only (``<key>:<document_id>:<glyph>``).
    """
    if not _is_positive_int(document_id):
        raise ValueError("inline_query_for requires a real custom-emoji document id")
    if not isinstance(glyph, str) or not glyph:
        raise ValueError("inline_query_for requires the emoji glyph")
    return f"{INLINE_QUERY_KEY}:{int(document_id)}:{glyph}"


def parse_inline_query_extra(extra: Any) -> tuple[int | None, str]:
    """``"<document_id>:<glyph>"`` → ``(document_id, glyph)``.

    Anything unusable returns ``(None, "")`` — the inline builder then answers
    with no result rather than inventing one.
    """
    if not isinstance(extra, str) or not extra:
        return None, ""
    head, _, glyph = extra.partition(":")
    if not head.isdigit() or not glyph:
        return None, ""
    document_id = int(head)
    if not _is_positive_int(document_id):
        return None, ""
    return document_id, glyph


def _empty_answer_evidence() -> dict[str, Any]:
    """What the HELPER BOT's own answer carried — the shape
    :func:`backend.helper.inline_engine.last_inline_answer` reports.

    ``event.answer`` returns a boolean and nothing else, so this is the only
    place the bot's ``setInlineBotResults`` submission is observable: without
    it, a stored result that lost the entity cannot be attributed to Telegram
    or to this application. ``recorded=False`` means no answer of this bot was
    seen for the query key since the query started — never "nothing submitted".
    """
    return {
        "recorded": False,
        "ok": None,
        "error": "",
        "result_count": None,
        "custom_emoji_count": None,
        "document_ids": [],
    }


def _empty_inline_evidence() -> dict[str, Any]:
    """The stored-inline-result evidence record — every key present."""
    return {
        "attempted": False,
        "ok": False,
        "error": None,
        "reason": None,
        "result_count": None,
        "wrapper_class": "",
        "tl_class": "",
        "entity_present": None,
        "document_id": None,
        "offset": None,
        "length": None,
        "span_text": "",
        "text": "",
        "document_id_match": None,
        "span_match": None,
        "answer": _empty_answer_evidence(),
    }


def _empty_readback(message_id: int | None = None) -> dict[str, Any]:
    """The stored-message evidence record — every key present."""
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
        "stored_text": "",
        "stored_text_utf16_len": None,
        "document_id_match": None,
        "span_match": None,
        "text_match": None,
        "via_bot_id": None,
        "via_bot_match": None,
    }


def _empty_eligibility() -> dict[str, Any]:
    """The eligibility facts the flow records — all optional, never assumed."""
    return {
        "owner_premium": None,
        "owner_premium_error": None,
        "helper_bot_id": None,
        "helper_bot_username": "",
        "document": None,
        "document_error": None,
        "glyph_source": "",
    }


def _failed(
    error: str,
    detail: str,
    diagnosis: str,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """An honest not-sent result: no message id, no read-back, full context."""
    result: dict[str, Any] = {
        "ok": False,
        "verified": False,
        "error": error,
        "detail": detail,
        "diagnosis": diagnosis,
        "send_path": None,
        "destination_chat_id": None,
        "message_id": None,
        "payload": payload,
        "inline_result": _empty_inline_evidence(),
        "readback": _empty_readback(),
        "eligibility": _empty_eligibility(),
    }
    return result


def _entity_missing_detail(evidence: Any) -> str:
    """WHY the stored inline result carries no custom-emoji entity.

    The stored payload and the helper bot's own answer are two DIFFERENT
    facts, and comparing them attributes the loss instead of guessing: an
    entity the bot submitted that is absent from the stored payload was
    dropped by Telegram, while an answer that raised or carried none is this
    application's own outcome. Without a recorded answer the cause stays
    unproven and is reported as such.
    """
    answer = evidence.get("answer") if isinstance(evidence, dict) else None
    if not isinstance(answer, dict) or not answer.get("recorded"):
        return (
            "Telegram stored the helper bot's inline result WITHOUT the "
            "custom-emoji entity, and no answer of this bot was recorded for "
            "this query, so where the entity was lost is unproven — the send "
            "was not attempted"
        )
    if not answer.get("ok"):
        return (
            "the helper bot's answer to the inline query did not complete "
            f"({answer.get('error') or 'no reason recorded'}), so Telegram "
            "stored a result carrying no custom-emoji entity — the send was "
            "not attempted"
        )
    submitted = answer.get("custom_emoji_count")
    if not isinstance(submitted, int) or submitted < 1:
        return (
            "the helper bot answered the inline query with no custom-emoji "
            "entity at all, so Telegram stored exactly what it was given — "
            "the send was not attempted"
        )
    documents = ", ".join(f"#{value}" for value in (answer.get("document_ids") or []))
    return (
        f"the helper bot's own answer submitted {submitted} custom-emoji "
        f"entit{'y' if submitted == 1 else 'ies'}"
        + (f" (document {documents})" if documents else "")
        + " and its submission raised nothing, while the stored inline result "
        "Telegram returned carries none — the entity was dropped by Telegram, "
        "not by this application. The send was not attempted and no fallback "
        "is ever used"
    )


def classify_diagnosis(
    inline_result: dict[str, Any], readback: dict[str, Any], sent_message_id: int | None
) -> str:
    """The ONE outcome the recorded evidence establishes — never more.

    Separates an implementation failure from a Telegram-side restriction: an
    empty answer (``INLINE_RESULT_EMPTY``), a shape this module refuses to
    inspect (``INLINE_RESULT_UNSUPPORTED``), a real result with no send-message
    payload (``INLINE_RESULT_NO_SEND_MESSAGE``), ``INLINE_RESULT_REJECTED``
    (the query itself failed), ``INLINE_RESULT_ENTITY_MISSING`` (a real stored
    result lost the entity), ``INLINE_SEND_FAILED`` (the send itself failed),
    and then the stored-message verdicts. A retained entity with the expected
    id, span and ``via_bot_id`` is still only ``…_RENDER_UNVERIFIED``.
    """
    if not inline_result.get("ok"):
        reason = inline_result.get("reason")
        if reason == "empty":
            return INLINE_RESULT_EMPTY
        if reason == "unsupported":
            return INLINE_RESULT_UNSUPPORTED
        if reason == "no_send_message":
            return INLINE_RESULT_NO_SEND_MESSAGE
        return INLINE_RESULT_REJECTED
    if not inline_result.get("entity_present"):
        return INLINE_RESULT_ENTITY_MISSING
    if not _is_positive_int(sent_message_id):
        return INLINE_SEND_FAILED
    if not readback.get("ok"):
        return READBACK_FAILED
    if not readback.get("entity_present"):
        return STORED_ENTITY_STRIPPED
    if not (readback.get("document_id_match") and readback.get("span_match")):
        return STORED_ENTITY_MISMATCH
    if not readback.get("via_bot_match"):
        return STORED_ATTRIBUTION_MISSING
    return STORED_ENTITY_VERIFIED_RENDER_UNVERIFIED


async def _owner_premium(self_client: Any) -> tuple[bool | None, str]:
    """The owner account's OBSERVED Premium status (``None`` when unreadable)."""
    try:
        me = await get_me(self_client)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        return None, f"the owner's Premium status could not be read: {exc}"
    premium = me.get("premium") if isinstance(me, dict) else None
    if isinstance(premium, bool):
        return premium, ""
    return None, "Telegram returned no usable Premium flag for this account"


async def _document_facts(self_client: Any, document_id: int) -> tuple[dict[str, Any] | None, str]:
    """The custom-emoji document's own facts (alt, free, text_color), or a reason."""
    try:
        documents = await get_custom_emoji_documents(self_client, [document_id])
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        return None, f"the document lookup failed: {exc}"
    for document in documents or []:
        if document.get("document_id") == document_id:
            return document, ""
    return None, "Telegram did not return this document for the given id"


def _resolve_inline_result(result: Any) -> tuple[Any | None, Any | None, str, str]:
    """The STORED payload behind ONE inline result, for either real shape.

    ``client.inline_query`` returns ``telethon.tl.custom.InlineResults`` — a
    list of ``InlineResult`` WRAPPERS. A wrapper keeps the raw TL object in
    ``.result`` and exposes ``.message``/``.click``; it carries NO
    ``send_message`` attribute of its own, so reading one straight off the
    returned element finds nothing even when Telegram kept the payload. A raw
    ``BotInlineResult``/``BotInlineMediaResult`` is understood as well.

    Returns ``(send_message, tl_object, kind, detail)``. On success ``kind``
    and ``detail`` are ``""``. Otherwise ``send_message`` is ``None`` and
    ``kind`` is ``"unsupported"`` (a shape this module refuses to guess about)
    or ``"no_send_message"`` (a real result whose payload genuinely is not
    there), with a bounded human ``detail`` — an unknown shape is never read as
    a stripped entity.
    """
    if result is None:
        return None, None, "unsupported", "the inline result object is None"
    if isinstance(result, _INLINE_RESULT_TYPES):
        tl_object = result
    else:
        tl_object = getattr(result, "result", None)
        if not isinstance(tl_object, _INLINE_RESULT_TYPES):
            return (
                None,
                None,
                "unsupported",
                f"the inline result wrapper {_class_path(result)} carries no "
                f"stored BotInlineResult (its .result is {_class_path(tl_object)})",
            )
    if not hasattr(tl_object, "send_message"):
        return (
            None,
            tl_object,
            "no_send_message",
            f"the stored {_class_path(tl_object)} carries no send_message field",
        )
    send_message = getattr(tl_object, "send_message", None)
    if send_message is None:
        return (
            None,
            tl_object,
            "no_send_message",
            f"the stored {_class_path(tl_object)} carries no send_message payload",
        )
    return send_message, tl_object, "", ""


async def _inspect_inline_result(
    results: Any, payload: dict[str, Any], answer: Any = None
) -> dict[str, Any]:
    """Checkpoint 2 — what Telegram KEPT in the stored inline result.

    Only the first result is inspected (the one the send clicks), and it is
    read through the REAL Telethon shapes: ``results[0]`` is a
    ``custom.InlineResult`` wrapper whose underlying ``BotInlineResult`` lives
    in ``.result`` — the payload is ``.result.send_message``. A raw
    ``BotInlineResult`` is accepted too.

    ``answer`` is what the HELPER BOT's own answer carried (its recorded
    ``setInlineBotResults`` submission, read through
    :func:`backend.helper.inline_engine.last_inline_answer`, keyed to this
    attempt). It is NOT evidence about what Telegram stored — it is the other
    half of the comparison, and the only way a stored payload that lost the
    entity can be attributed honestly.

    ``ok`` means the stored payload was obtained and inspected; ``reason``
    names WHICH absence was seen (``"empty"``, ``"unsupported"``,
    ``"no_send_message"``, or ``""`` when a payload was inspected);
    ``entity_present`` means a real ``MessageEntityCustomEmoji`` survived
    INSIDE that payload. Only a genuine, present payload with the entity
    missing is a Telegram-side restriction — an empty result or an unsupported
    shape is reported as such, never as proof of stripping.
    """
    evidence = _empty_inline_evidence()
    if isinstance(answer, dict):
        evidence["answer"] = answer
    evidence["attempted"] = True
    try:
        count = len(results)
    except Exception:
        count = None
    evidence["result_count"] = count
    if not count:
        evidence["error"] = "the helper bot returned no inline result to inspect"
        evidence["reason"] = "empty"
        evidence["entity_present"] = False
        _trace(
            "INLINE_RESULT_INSPECTED",
            entity_present=False,
            reason="empty",
            result_count=0,
        )
        return evidence
    first = results[0]
    evidence["wrapper_class"] = _class_path(first)
    send_message, tl_object, kind, detail = _resolve_inline_result(first)
    if tl_object is not None:
        evidence["tl_class"] = _class_path(tl_object)
    if send_message is None:
        evidence["error"] = detail
        evidence["reason"] = kind or "unsupported"
        evidence["entity_present"] = False
        _trace(
            "INLINE_RESULT_INSPECTED",
            entity_present=False,
            reason=evidence["reason"],
            wrapper=evidence["wrapper_class"],
            tl=evidence["tl_class"] or None,
            result_count=count,
        )
        return evidence
    evidence["ok"] = True
    text = getattr(send_message, "message", "") or ""
    evidence["text"] = text
    scan = _scan_custom_emoji(text, getattr(send_message, "entities", None))
    expected = payload["entity"]
    if scan is None:
        evidence["entity_present"] = False
        evidence["span_text"] = _span_text(
            text, expected["offset"], expected["length"]
        )
        _trace(
            "INLINE_RESULT_INSPECTED",
            entity_present=False,
            wrapper=evidence["wrapper_class"],
            tl=evidence["tl_class"] or None,
            text_utf16_len=utf16_length(text),
            entity_count=len(getattr(send_message, "entities", None) or []),
            answer_recorded=evidence["answer"].get("recorded"),
            answer_ok=evidence["answer"].get("ok"),
            answer_custom_emoji_count=evidence["answer"].get("custom_emoji_count"),
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
    _trace(
        "INLINE_RESULT_INSPECTED",
        entity_present=True,
        wrapper=evidence["wrapper_class"],
        tl=evidence["tl_class"] or None,
        document_id=scan["document_id"],
        document_id_match=evidence["document_id_match"],
        offset=scan["offset"],
        length=scan["length"],
        span_match=evidence["span_match"],
    )
    return evidence


async def _send_result(self_client: Any, chat_id: int, results: Any) -> tuple[Any, str]:
    """Send the first stored inline result AS THE USER (``sendInlineBotResult``)."""
    try:
        return await guarded_await(
            inline_engine.click_result(self_client, chat_id, results[0]),
            name="telegram:premium_inline:click",
            timeout=_INLINE_CALL_TIMEOUT_S,
        )
    except asyncio.CancelledError:
        raise
    except asyncio.TimeoutError:
        return None, f"the inline send timed out after {_INLINE_CALL_TIMEOUT_S:g}s"


async def _fetch_stored_message(
    self_client: Any, chat_id: int, message_id: int
) -> tuple[Any, str]:
    """Checkpoint 3 — fetch the EXACT sent message by id (never a recent scan)."""
    try:
        fetched = await guarded_await(
            self_client.get_messages(chat_id, ids=message_id),
            name="telegram:premium_inline:readback",
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


def _read_back(stored: Any, payload: dict[str, Any], helper_bot_id: int) -> dict[str, Any]:
    """Classify ONLY what the stored message shows, including the attribution."""
    expected = payload["entity"]
    evidence = _empty_readback()
    evidence["attempted"] = True
    evidence["ok"] = True
    text = _message_text(stored)
    evidence["stored_text"] = text
    evidence["stored_text_utf16_len"] = utf16_length(text)
    evidence["text_match"] = text == payload["text"]
    via_bot_id = getattr(stored, "via_bot_id", None)
    evidence["via_bot_id"] = via_bot_id
    if _is_positive_int(helper_bot_id) and via_bot_id is not None:
        evidence["via_bot_match"] = via_bot_id == helper_bot_id
    else:
        evidence["via_bot_match"] = False
    scan = _scan_custom_emoji(text, getattr(stored, "entities", None))
    if scan is None:
        evidence["entity_present"] = False
        evidence["span_text"] = _span_text(text, expected["offset"], expected["length"])
        evidence["span_match"] = evidence["span_text"] == payload["glyph"]
        _trace(
            "READBACK_RESULT",
            entity_present=False,
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
    _trace(
        "READBACK_RESULT",
        entity_present=True,
        document_id=scan["document_id"],
        document_id_match=evidence["document_id_match"],
        offset=scan["offset"],
        length=scan["length"],
        span_match=evidence["span_match"],
        via_bot_id=via_bot_id,
        via_bot_match=evidence["via_bot_match"],
    )
    return evidence


async def send_premium_emoji_via_inline(
    self_client: Any,
    chat_id: Any,
    document_id: Any,
    source_glyph: Any,
    owner_id: Any,
) -> dict[str, Any]:
    """Send ONE genuine custom emoji through the inline-bot path.

    The destination is the owner's Saved Messages only (``chat_id ==
    owner_id``) — the one chat Telegram documents as allowing custom emoji for
    a non-Premium account. ``source_glyph`` is the text the SOURCE entity's
    span covered; the glyph actually wrapped on the way out is Telegram's own
    ``alt`` for the document when that resolves, so the documented "the entity
    must wrap exactly the emoji in ``documentAttributeCustomEmoji.alt``" rule
    is satisfied by construction (the fallback is recorded as
    ``glyph_source``).

    Returns the full record: ``ok`` (Telegram accepted the user's inline send —
    a SEND fact, never a render claim), ``verified`` (the stored message
    carries the exact entity with the helper bot's own attribution),
    ``diagnosis`` (one of this module's outcome constants), ``send_path``,
    ``message_id``, ``payload``, and the three evidence blocks
    (``inline_result`` / ``readback`` / ``eligibility``).
    """
    if not _is_positive_int(owner_id):
        _trace("SEND_STARTED", started=False, reason="owner")
        return _failed(ERROR_OWNER, "no valid owner for this operation", SOURCE_ENTITY_MISSING)
    if not _is_positive_int(chat_id) or chat_id != owner_id:
        _trace("SEND_STARTED", started=False, reason="destination")
        return _failed(
            ERROR_DESTINATION,
            "only your Saved Messages is a supported destination — Telegram "
            "documents the non-Premium custom-emoji allowance for the self "
            "chat alone, so nothing was sent",
            UNSUPPORTED_DESTINATION,
        )
    if not _is_positive_int(document_id):
        _trace("SEND_STARTED", started=False, reason="document_id")
        return _failed(
            ERROR_SOURCE,
            "the source message carries no usable custom-emoji document id",
            SOURCE_ENTITY_MISSING,
        )

    eligibility = _empty_eligibility()
    eligibility["helper_bot_id"] = helper_client.get_bot_id() or None
    eligibility["helper_bot_username"] = inline_engine.get_helper_username()
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
        _trace("SEND_STARTED", started=False, reason="glyph")
        failed = _failed(
            ERROR_SOURCE,
            "the emoji's own fallback text could not be resolved — Telegram's "
            "documented rule requires the entity to wrap exactly the emoji in "
            "the document's alt text, so nothing was sent",
            SOURCE_ENTITY_MISSING,
        )
        failed["eligibility"] = eligibility
        return failed

    payload = build_inline_payload(int(document_id), glyph)
    issue = validate_inline_payload(payload)
    if issue:
        _trace("OUTBOUND_PAYLOAD_BUILT", valid=False, issue=_bounded(issue))
        failed = _failed(
            ERROR_PAYLOAD,
            f"the outbound payload failed its own validation: {issue}",
            OUTBOUND_PAYLOAD_INVALID,
            payload,
        )
        failed["eligibility"] = eligibility
        return failed

    reason = inline_engine.inline_unavailable_reason()
    if reason:
        _trace("SEND_STARTED", started=False, reason="helper")
        failed = _failed(
            ERROR_HELPER,
            f"the inline bot cannot be used — {reason}",
            INLINE_UNAVAILABLE,
            payload,
        )
        failed["eligibility"] = eligibility
        return failed

    query = inline_query_for(int(document_id), glyph)
    query_started = time.monotonic()
    _trace(
        "INLINE_QUERY_STARTED",
        started=True,
        via="helper_bot_inline",
        entity_count=len(payload["entities"]),
        offset=payload["entity"]["offset"],
        length=payload["entity"]["length"],
    )
    try:
        results, query_error = await guarded_await(
            inline_engine.query_results(self_client, chat_id, query),
            name="telegram:premium_inline:query",
            timeout=_INLINE_CALL_TIMEOUT_S,
        )
    except asyncio.CancelledError:
        raise
    except asyncio.TimeoutError:
        results, query_error = None, f"the inline query timed out after {_INLINE_CALL_TIMEOUT_S:g}s"
    if results is None:
        _trace("INLINE_QUERY_RESULT", ok=False, error=_bounded(query_error))
        if query_error == inline_engine.INLINE_ZERO_RESULTS_REASON:
            failed = _failed(
                ERROR_QUERY,
                "the helper bot answered the inline query with no result — "
                "nothing could be inspected and the send was not attempted",
                INLINE_RESULT_EMPTY,
                payload,
            )
        else:
            failed = _failed(
                ERROR_QUERY,
                f"the helper bot could not answer the inline query: {query_error}",
                INLINE_RESULT_REJECTED,
                payload,
            )
        failed["eligibility"] = eligibility
        failed["destination_chat_id"] = chat_id
        return failed

    record: dict[str, Any] = {
        "ok": False,
        "verified": False,
        "error": None,
        "detail": None,
        "diagnosis": INLINE_SEND_FAILED,
        "send_path": None,
        "destination_chat_id": chat_id,
        "message_id": None,
        "payload": payload,
        "inline_result": await _inspect_inline_result(
            results,
            payload,
            inline_engine.last_inline_answer(INLINE_QUERY_KEY, since=query_started),
        ),
        "readback": _empty_readback(),
        "eligibility": eligibility,
    }

    def _fail(error: str, detail: str) -> dict[str, Any]:
        """Close the record on its recorded evidence — the verdict is derived, never chosen."""
        record["error"] = error
        record["detail"] = detail
        record["diagnosis"] = classify_diagnosis(
            record["inline_result"], record["readback"], record["message_id"]
        )
        _trace("DIAGNOSIS", diagnosis=record["diagnosis"])
        return record

    if not record["inline_result"]["ok"]:
        inline_evidence = record["inline_result"]
        reason = inline_evidence["reason"]
        if reason == "empty":
            detail = (
                "the helper bot returned no inline result — nothing was "
                "inspected and the send was not attempted"
            )
        elif reason == "unsupported":
            detail = (
                "the inline result's runtime shape could not be inspected "
                f"safely ({inline_evidence['error']}) — the send was not attempted"
            )
        elif reason == "no_send_message":
            detail = (
                "the stored inline result carries no send-message payload "
                f"({inline_evidence['error']}) — the send was not attempted"
            )
        else:
            detail = (
                "the stored inline result could not be inspected: "
                f"{inline_evidence['error']}"
            )
        return _fail(ERROR_QUERY, detail)
    if not record["inline_result"]["entity_present"]:
        return _fail(
            ERROR_QUERY,
            _entity_missing_detail(record["inline_result"]),
        )

    _trace("INLINE_SEND_STARTED", started=True, via=SEND_PATH_INLINE_BOT_RESULT)
    sent, send_error = await _send_result(self_client, chat_id, results)
    if sent is None:
        _trace("INLINE_SEND_ACCEPTED", accepted=False, error=_bounded(send_error))
        return _fail(
            ERROR_SEND, f"the user account's inline send failed: {send_error}"
        )

    raw_id = getattr(sent, "id", 0)
    message_id = raw_id if _is_positive_int(raw_id) else None
    record["ok"] = True
    record["send_path"] = SEND_PATH_INLINE_BOT_RESULT
    record["message_id"] = message_id
    _trace("INLINE_SEND_ACCEPTED", accepted=True, message_id=message_id)
    if message_id is None:
        return _fail(
            ERROR_READBACK,
            "the inline send returned no message id — the stored message cannot be read back",
        )

    stored, readback_error = await _fetch_stored_message(self_client, chat_id, message_id)
    if stored is None:
        readback = _empty_readback(message_id)
        readback["attempted"] = True
        readback["error"] = readback_error
        record["readback"] = readback
        _trace("READBACK_RESULT", fetched=False, error=_bounded(readback_error))
        return _fail(ERROR_READBACK, f"the read-back failed: {readback_error}")

    record["readback"] = _read_back(stored, payload, helper_client.get_bot_id())
    record["diagnosis"] = classify_diagnosis(
        record["inline_result"], record["readback"], message_id
    )
    record["verified"] = record["diagnosis"] == STORED_ENTITY_VERIFIED_RENDER_UNVERIFIED
    _trace("DIAGNOSIS", diagnosis=record["diagnosis"])
    return record


def outcome_summary(result: Any) -> str:
    """ONE honest sentence about the outcome — never a render claim."""
    if not isinstance(result, dict):
        return "the send produced no result."
    diagnosis = result.get("diagnosis")
    if result.get("error"):
        return f"{result.get('detail') or 'the send did not happen'}"
    if diagnosis == STORED_ENTITY_VERIFIED_RENDER_UNVERIFIED:
        return (
            "Telegram stored the exact custom-emoji entity with the helper "
            "bot's own attribution — whether your client renders it is the "
            "one thing this check cannot prove."
        )
    if diagnosis == STORED_ATTRIBUTION_MISSING:
        via = result.get("readback", {}).get("via_bot_id")
        return (
            "the stored message carries the custom-emoji entity but not the "
            f"helper bot's attribution (via_bot_id={via!r}) — the inline path "
            "is not what produced it."
        )
    if diagnosis == STORED_ENTITY_STRIPPED:
        return (
            "the inline result carried the entity and the send was accepted, "
            "but Telegram stored the message WITHOUT the custom-emoji entity."
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
    if diagnosis == INLINE_RESULT_EMPTY:
        return "the helper bot answered the inline query with no result — nothing was sent."
    if diagnosis == INLINE_RESULT_UNSUPPORTED:
        return (
            "the inline result's runtime shape could not be inspected safely, "
            "so nothing was sent — this is NOT evidence that Telegram stripped "
            "the entity."
        )
    if diagnosis == INLINE_RESULT_NO_SEND_MESSAGE:
        return (
            "the stored inline result carries no send-message payload — "
            "nothing was sent."
        )
    if diagnosis == INLINE_RESULT_ENTITY_MISSING:
        return (
            "Telegram stored the helper bot's inline result without the "
            "custom-emoji entity — nothing was sent."
        )
    if diagnosis == INLINE_RESULT_REJECTED:
        return "the helper bot's inline result was rejected or never returned."
    if diagnosis == INLINE_SEND_FAILED:
        return "the user account's inline send failed — no message was stored."
    return f"outcome {diagnosis!r}."
