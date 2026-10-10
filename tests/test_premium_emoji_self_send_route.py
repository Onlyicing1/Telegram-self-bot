"""
Controlled test — ALTERNATIVE premium custom-emoji delivery route (self-authored).

THE QUESTION
    The helper-bot inline-result route is blocked: the stored answer Telegram
    returns to the self account carries no custom-emoji entity, so
    ``INLINE_RESULT_ENTITY_MISSING`` makes the fail-closed gate refuse the send.
    Can the OWNER'S OWN account deliver a genuine ``MessageEntityCustomEmoji``
    to Saved Messages instead — with **no bot anywhere in the path**, and
    therefore no bot entitlement to check?

THE ROUTE (documented)
    ``messages.sendMessage`` from the existing authenticated owner account with
    ``peer = inputPeerSelf`` (the user's own chat = Saved Messages) and
    ``entities = [messageEntityCustomEmoji(offset, length, document_id)]`` — the
    construction ``core.telegram.org/api/custom-emoji`` describes: "To send a
    message with one or more custom emojis, create and attach
    messageEntityCustomEmoji entities to a message." Telegram documents the
    Saved-Messages allowance for every account, Premium or not: "Everyone can
    also use all custom emoji for free in their Saved Messages chat"
    (``telegram.org/blog/custom-emoji``). ``inputPeerSelf`` is the peer that
    "Defines the current user" (``core.telegram.org/constructor/inputPeerSelf``).

WHAT THIS FILE ESTABLISHES WITHOUT TELEGRAM (every test but the last one)
    * the payload and its UTF-16 geometry are the PRODUCTION ones (reused, not
      re-derived) and the production validator passes on them;
    * the pinned ``telethon==1.34.0`` ``send_message(..., formatting_entities=…)``
      really forwards those entities into ``messages.SendMessageRequest``, and
      that request round-trips the entity through its own serialized bytes;
    * the stored-message verification is fail-closed on every acceptance
      criterion, so "no entity", "wrong document", "wrong span", "wrong chat",
      "wrong text" and an unexpected bot attribution can never read as success;
    * exactly ONE message is ever sent per run, and a failed read-back never
      retries (no duplicate Saved Messages entries);
    * nothing of this experiment exists in production code — it is an isolated
      test-suite experiment, as the task requires.

WHAT ONLY TELEGRAM CAN ANSWER (the opt-in live test at the bottom)
    Whether the server actually KEEPS the entity on a self-authored Saved
    Messages message. A mocked test cannot establish server-side behaviour and
    this file never claims it did.

ATTRIBUTION (stated, not relaxed)
    A self-authored message carries **no** ``via_bot_id``: the field is the
    optional ``via_bot_id:flags.11?long`` of ``message``
    (``core.telegram.org/constructor/message``), present only for a message sent
    through a bot. This route therefore **cannot** satisfy the inline route's
    ``via_bot_id`` criterion — that criterion is a property of the INLINE route,
    not of the artifact. The experiment asserts the ABSENCE of ``via_bot_id``
    (any value fails closed as an anomaly) and reports the limitation instead of
    quietly dropping the check.

HOW TO RUN THE LIVE TEST (skips honestly everywhere else)
    LIFEOS_LIVE_PREMIUM_EMOJI_SELF_SEND=1 \\
        API_ID=… API_HASH=… SESSION_STRING=… BOT_OWNER_ID=… \\
        pytest tests/test_premium_emoji_self_send_route.py -m live_telegram -v -s

    Expected side effects: exactly ONE message is sent to Saved Messages
    (prefix ``LifeOS premium emoji self-send test: ``), then read back by its
    exact id. Nothing is sent to any other chat, no retries run, and no client,
    listener, loop or scheduler is added to the runtime. The message is left in
    place as the evidence — delete it manually if you want the chat clean.

ESCALATION NOTE
    If the live run shows the entity KEPT, the next step is a product decision
    (an owner-authored delivery path or a one-shot migration tool), not a chat
    routing change: this file deliberately exposes no production surface.
"""
from __future__ import annotations

import asyncio
import inspect
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from telethon import TelegramClient, types
from telethon.extensions.binaryreader import BinaryReader
from telethon.tl.functions.messages import SendMessageRequest

from backend.runtime.operation_watchdog import guarded_await
from backend.services import premium_emoji_inline_service as svc
from backend.telegram_api._helpers import utf16_length

#: Identifies the controlled test message in Saved Messages.
SELF_SEND_PREFIX = "LifeOS premium emoji self-send test: "

#: The explicit opt-in. Presence of credentials is NOT enough — a live send to
#: the owner's account must never happen just because the suite runs somewhere
#: with a session configured.
LIVE_ENV_FLAG = "LIFEOS_LIVE_PREMIUM_EMOJI_SELF_SEND"

#: Optional: pin the source custom-emoji document id instead of discovering one.
LIVE_DOC_ENV = "LIFEOS_LIVE_PREMIUM_EMOJI_DOC_ID"

#: Bounds. The history scan is bounded, both Telegram calls are bounded, and the
#: procedure sends exactly once with no retry.
SOURCE_SCAN_LIMIT = 100
_SEND_TIMEOUT_S = 45.0
_READBACK_TIMEOUT_S = 30.0

#: The exact MTProto path this route uses — recorded, never inferred.
SEND_PATH_SELF_SEND = "messages.sendMessage(inputPeerSelf)"

ERROR_OWNER = "E_OWNER"
ERROR_SOURCE = "E_SOURCE"
ERROR_PAYLOAD = "E_PAYLOAD"
ERROR_SEND = "E_SEND"
ERROR_READBACK = "E_READBACK"
ERROR_VERIFY = "E_VERIFY"

#: The ONE outcome each piece of evidence establishes — never more.
SOURCE_ENTITY_MISSING = "SOURCE_ENTITY_MISSING"
OUTBOUND_PAYLOAD_INVALID = "OUTBOUND_PAYLOAD_INVALID"
SEND_FAILED = "SEND_FAILED"
READBACK_FAILED = "READBACK_FAILED"
STORED_ENTITY_MISSING = "STORED_ENTITY_MISSING"
STORED_DOCUMENT_ID_MISMATCH = "STORED_DOCUMENT_ID_MISMATCH"
STORED_SPAN_MISMATCH = "STORED_SPAN_MISMATCH"
STORED_TEXT_MISMATCH = "STORED_TEXT_MISMATCH"
STORED_DESTINATION_MISMATCH = "STORED_DESTINATION_MISMATCH"
STORED_ATTRIBUTION_ANOMALY = "STORED_ATTRIBUTION_ANOMALY"
STORED_ENTITY_VERIFIED_RENDER_UNVERIFIED = "STORED_ENTITY_VERIFIED_RENDER_UNVERIFIED"


def _is_positive_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _message_text(message: Any) -> str:
    raw = getattr(message, "message", None)
    if not isinstance(raw, str) or not raw:
        raw = getattr(message, "text", None)
    return raw if isinstance(raw, str) else ""


# ── payload (production geometry, this route's prefix) ──────────────────────


def build_self_send_payload(document_id: Any, glyph: Any) -> dict[str, Any]:
    """The exact ``messages.sendMessage`` payload for ONE custom emoji, no bot.

    Reuses the PRODUCTION builder and validator (``premium_emoji_inline_service``)
    so the UTF-16 geometry and the fail-closed rules are the same ones the
    deployed feature enforces — only the message prefix differs, because this is
    a clearly identifiable controlled-test artifact.
    """
    payload = svc.build_inline_payload(document_id, glyph, prefix=SELF_SEND_PREFIX)
    issue = svc.validate_inline_payload(payload)
    if issue:
        raise ValueError(f"the self-send payload failed its own validation: {issue}")
    return payload


def build_self_send_request(payload: dict[str, Any]) -> SendMessageRequest:
    """The REAL ``messages.SendMessageRequest`` this route submits.

    ``peer = inputPeerSelf`` (Saved Messages) and ``entities`` carrying the
    rebuilt ``MessageEntityCustomEmoji``. ``random_id`` is left to Telethon's
    own per-request generation, which is what keeps two identical sends from
    collapsing into one server-side duplicate.
    """
    entity = payload["entity"]
    return SendMessageRequest(
        peer=types.InputPeerSelf(),
        message=payload["text"],
        entities=[
            types.MessageEntityCustomEmoji(
                entity["offset"], entity["length"], entity["document_id"]
            )
        ],
    )


# ── stored-message verification (the acceptance criteria, fail closed) ──────


def _empty_readback() -> dict[str, Any]:
    """Every acceptance fact, present — so a missing one can never be read as ok."""
    return {
        "attempted": False,
        "ok": False,
        "error": "",
        "reason": "",
        "destination_match": None,
        "entity_present": None,
        "document_id": None,
        "offset": None,
        "length": None,
        "span_text": "",
        "document_id_match": None,
        "span_match": None,
        "stored_text": "",
        "stored_text_utf16_len": None,
        "text_match": None,
        "chat_id": None,
        "via_bot_id": None,
        "attribution_present": None,
    }


def verify_self_sent_message(
    stored: Any, payload: dict[str, Any], owner_id: Any = None
) -> dict[str, Any]:
    """Classify ONLY what the STORED message shows — the acceptance criteria.

    Checks, in order, and fails closed on the first failure: the message exists;
    it lives in the owner's own chat (Saved Messages); it carries a genuine
    ``MessageEntityCustomEmoji``; that entity's document id matches the source
    emoji; its UTF-16 offset/length match and its span is exactly the glyph; the
    text is the text that was sent; and NO bot attribution is present (a
    self-authored message must not carry ``via_bot_id``).

    ``ok`` is true only when every one of those holds. A retained entity is
    still ``…_RENDER_UNVERIFIED``: whether a client draws it is not this
    module's claim.
    """
    expected = payload["entity"]
    evidence = _empty_readback()
    evidence["attempted"] = True
    if stored is None:
        evidence["error"] = "Telegram returned no message for the read-back id"
        evidence["reason"] = "no_message"
        return evidence

    text = _message_text(stored)
    evidence["stored_text"] = text
    evidence["stored_text_utf16_len"] = utf16_length(text)
    evidence["text_match"] = text == payload["text"]
    chat_id = getattr(stored, "chat_id", None)
    evidence["chat_id"] = chat_id
    if _is_positive_int(owner_id):
        evidence["destination_match"] = chat_id == owner_id
    via_bot_id = getattr(stored, "via_bot_id", None)
    evidence["via_bot_id"] = via_bot_id
    evidence["attribution_present"] = via_bot_id is not None

    def _fail(reason: str, error: str) -> dict[str, Any]:
        evidence["reason"] = reason
        evidence["error"] = error
        return evidence

    if evidence["destination_match"] is False:
        return _fail(
            "not_saved_messages",
            f"Telegram stored the message in chat {chat_id!r}, not in the "
            f"owner's Saved Messages ({owner_id!r})",
        )
    scan = svc._scan_custom_emoji(text, getattr(stored, "entities", None))
    if scan is None:
        evidence["entity_present"] = False
        return _fail(
            "entity_missing",
            "Telegram stored the self-sent message WITHOUT the custom-emoji "
            "entity — the visible text alone is never the emoji",
        )
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
    if not evidence["document_id_match"]:
        return _fail(
            "document_id_mismatch",
            f"the stored entity carries document {scan['document_id']!r}, not "
            f"the source emoji's {expected['document_id']!r}",
        )
    if not evidence["span_match"]:
        return _fail(
            "span_mismatch",
            "the stored entity's UTF-16 offset/length or covered span does not "
            f"match the sent geometry (got offset {scan['offset']!r}, length "
            f"{scan['length']!r}, span {scan['span_text']!r})",
        )
    if not evidence["text_match"]:
        return _fail(
            "text_mismatch",
            "the stored text differs from the text that was sent",
        )
    if evidence["attribution_present"]:
        return _fail(
            "unexpected_attribution",
            f"the self-authored message carries via_bot_id={via_bot_id!r} — "
            "this route is bot-free by construction, so the stored message was "
            "not produced by it",
        )
    evidence["ok"] = True
    evidence["reason"] = STORED_ENTITY_VERIFIED_RENDER_UNVERIFIED
    return evidence


# ── the controlled procedure: ONE send, ONE read-back, no retry ─────────────


async def run_self_send_once(
    client: Any, owner_id: Any, document_id: Any, glyph: Any
) -> dict[str, Any]:
    """Send ONE identifiable message to Saved Messages and verify it.

    The destination is ``inputPeerSelf`` by construction — this route can never
    address another chat — and the stored message is additionally required to
    show ``chat_id == owner_id``. Any failure (payload, send, read-back,
    verification) is RETURNED, never retried, so one run can never leave more
    than the single message it attempted.
    """
    record: dict[str, Any] = {
        "ok": False,
        "error": "",
        "detail": "",
        "diagnosis": "",
        "send_path": SEND_PATH_SELF_SEND,
        "destination_chat_id": owner_id,
        "sent_chat_id": None,
        "message_id": None,
        "send_attempts": 0,
        "payload": None,
        "readback": _empty_readback(),
    }

    def _refuse(error: str, diagnosis: str, detail: str) -> dict[str, Any]:
        record["error"] = error
        record["diagnosis"] = diagnosis
        record["detail"] = detail
        return record

    if not _is_positive_int(owner_id):
        return _refuse(ERROR_OWNER, SOURCE_ENTITY_MISSING, "no usable owner id — nothing was sent")
    if not _is_positive_int(document_id):
        return _refuse(
            ERROR_SOURCE,
            SOURCE_ENTITY_MISSING,
            "the source carries no usable custom-emoji document id — nothing was sent",
        )
    try:
        payload = build_self_send_payload(document_id, glyph)
    except ValueError as exc:
        return _refuse(
            ERROR_PAYLOAD, OUTBOUND_PAYLOAD_INVALID, f"the outbound payload failed its own validation: {exc}"
        )
    record["payload"] = payload
    entity = payload["entity"]
    tl_entity = types.MessageEntityCustomEmoji(
        entity["offset"], entity["length"], entity["document_id"]
    )
    peer = types.InputPeerSelf()
    record["send_attempts"] = 1
    try:
        sent = await guarded_await(
            client.send_message(peer, payload["text"], formatting_entities=[tl_entity]),
            name="telegram:premium_self_send:send",
            timeout=_SEND_TIMEOUT_S,
        )
    except asyncio.CancelledError:
        raise
    except asyncio.TimeoutError:
        return _refuse(
            ERROR_SEND,
            SEND_FAILED,
            f"the self-send timed out after {_SEND_TIMEOUT_S:g}s — it is never retried",
        )
    except Exception as exc:
        return _refuse(ERROR_SEND, SEND_FAILED, f"the self-send failed: {exc}")

    raw_id = getattr(sent, "id", 0)
    message_id = raw_id if _is_positive_int(raw_id) else None
    record["message_id"] = message_id
    record["sent_chat_id"] = getattr(sent, "chat_id", None)
    if message_id is None:
        return _refuse(
            ERROR_READBACK,
            READBACK_FAILED,
            "the send returned no message id — the stored message cannot be read back",
        )
    try:
        stored = await guarded_await(
            client.get_messages(peer, ids=message_id),
            name="telegram:premium_self_send:readback",
            timeout=_READBACK_TIMEOUT_S,
        )
    except asyncio.CancelledError:
        raise
    except asyncio.TimeoutError:
        return _refuse(
            ERROR_READBACK,
            READBACK_FAILED,
            f"the read-back timed out after {_READBACK_TIMEOUT_S:g}s — it is never retried",
        )
    except Exception as exc:
        return _refuse(ERROR_READBACK, READBACK_FAILED, f"the read-back failed: {exc}")

    record["readback"] = verify_self_sent_message(stored, payload, owner_id=owner_id)
    record["ok"] = bool(record["readback"]["ok"])
    record["diagnosis"] = record["readback"]["reason"]
    if not record["ok"]:
        record["error"] = ERROR_VERIFY
        record["detail"] = record["readback"]["error"]
    return record


# ── telegram-level fakes (the Telegram boundary only) ──────────────────────


class _FakeSent:
    def __init__(self, message_id: int, chat_id: int | None) -> None:
        self.id = message_id
        self.chat_id = chat_id
        self.message = SELF_SEND_PREFIX


class _FakeStored:
    """The surface the verification consumes on a stored message."""

    def __init__(
        self,
        text: str,
        entities: list[Any] | None = None,
        *,
        message_id: int = 5150,
        chat_id: int | None = None,
        via_bot_id: int | None = None,
    ) -> None:
        self.message = text
        self.text = text
        self.entities = entities or []
        self.id = message_id
        self.chat_id = chat_id
        self.via_bot_id = via_bot_id


class _FakeSelfClient:
    """Records every Telegram call; the read-back can be made to fail."""

    def __init__(
        self,
        *,
        owner: int,
        stored: Any = None,
        send_error: Exception | None = None,
        read_error: Exception | None = None,
        message_id: int | None = 5150,
    ) -> None:
        self.owner = owner
        self.stored = stored
        self.send_error = send_error
        self.read_error = read_error
        self.message_id = message_id
        self.sends: list[dict[str, Any]] = []
        self.reads: list[tuple[Any, Any]] = []

    async def send_message(self, peer, message, formatting_entities=None, **kwargs):
        self.sends.append(
            {"peer": peer, "message": message, "entities": formatting_entities}
        )
        if self.send_error is not None:
            raise self.send_error
        return _FakeSent(self.message_id, self.owner)

    async def get_messages(self, peer, ids=None):
        self.reads.append((peer, ids))
        if self.read_error is not None:
            raise self.read_error
        return self.stored


OWNER = 7770001
CHAT = -1001234567890
DOC = 5361626279781934801
ALT = "😵"


def _entity(document_id: int = DOC, offset: int | None = None, length: int = 2):
    if offset is None:
        offset = utf16_length(SELF_SEND_PREFIX)
    return types.MessageEntityCustomEmoji(offset, length, document_id)


def _stored(entities: list[Any] | None = None, **kwargs):
    return _FakeStored(SELF_SEND_PREFIX + ALT, entities, chat_id=OWNER, **kwargs)


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _send(client, document_id: int = DOC, glyph: str = ALT, owner_id: int = OWNER):
    return _run(run_self_send_once(client, owner_id, document_id, glyph))


# ── offline: payload, geometry, and the real request ───────────────────────


def test_the_self_send_payload_reuses_the_production_geometry():
    payload = build_self_send_payload(DOC, ALT)
    entity = payload["entity"]
    assert payload["text"] == SELF_SEND_PREFIX + ALT
    assert entity["offset"] == utf16_length(SELF_SEND_PREFIX)
    assert entity["length"] == utf16_length(ALT)
    assert entity["document_id"] == DOC
    assert svc.validate_inline_payload(payload) == ""
    assert (
        svc._span_text(payload["text"], entity["offset"], entity["length"]) == ALT
    )


@pytest.mark.parametrize("glyph", ["🕒", "❤️", "👨‍👩‍👧‍👦", "◾"])
def test_the_self_send_payload_counts_utf16_units_not_characters(glyph):
    payload = build_self_send_payload(DOC, glyph)
    entity = payload["entity"]
    assert entity["offset"] == utf16_length(SELF_SEND_PREFIX)
    assert entity["length"] == utf16_length(glyph)
    assert (
        svc._span_text(payload["text"], entity["offset"], entity["length"]) == glyph
    )


@pytest.mark.parametrize("document_id", [0, -5, True, None, "x", 1.5])
def test_the_self_send_payload_fails_closed_on_an_unusable_document_id(document_id):
    with pytest.raises(ValueError):
        build_self_send_payload(document_id, ALT)


@pytest.mark.parametrize("glyph", ["", None, 7])
def test_the_self_send_payload_fails_closed_on_an_unusable_glyph(glyph):
    with pytest.raises(ValueError):
        build_self_send_payload(DOC, glyph)


def test_the_real_send_request_round_trips_the_entity():
    payload = build_self_send_payload(DOC, ALT)
    request = build_self_send_request(payload)

    parsed = BinaryReader(bytes(request)).tgread_object()

    assert isinstance(parsed, SendMessageRequest)
    assert isinstance(parsed.peer, types.InputPeerSelf)
    assert parsed.message == payload["text"]
    entity = parsed.entities[0]
    assert isinstance(entity, types.MessageEntityCustomEmoji)
    assert (entity.document_id, entity.offset, entity.length) == (
        DOC,
        utf16_length(SELF_SEND_PREFIX),
        utf16_length(ALT),
    )
    assert (
        svc._span_text(parsed.message, entity.offset, entity.length) == ALT
    )


def test_two_requests_never_share_a_random_id():
    """Telegram deduplicates by random_id — two sends can never collapse into one."""
    first = build_self_send_request(build_self_send_payload(DOC, ALT))
    second = build_self_send_request(build_self_send_payload(DOC, ALT))
    assert first.random_id and second.random_id
    assert first.random_id != second.random_id


def test_the_pinned_telethon_forwards_formatting_entities_into_the_request():
    """The call this route uses really carries the entity (pinned 1.34.0)."""
    signature = inspect.signature(TelegramClient.send_message)
    assert "formatting_entities" in signature.parameters
    annotation = str(signature.parameters["formatting_entities"].annotation)
    assert "MessageEntityCustomEmoji" in annotation
    source = inspect.getsource(TelegramClient.send_message)
    assert "SendMessageRequest" in source
    assert "entities=formatting_entities" in source


# ── offline: the stored-message verification ───────────────────────────────


def test_the_verification_accepts_only_the_exact_stored_entity():
    payload = build_self_send_payload(DOC, ALT)
    evidence = verify_self_sent_message(_stored([_entity()]), payload, OWNER)
    assert evidence["ok"] is True
    assert evidence["reason"] == STORED_ENTITY_VERIFIED_RENDER_UNVERIFIED
    assert evidence["destination_match"] is True
    assert evidence["entity_present"] is True
    assert evidence["document_id_match"] is True
    assert evidence["span_match"] is True
    assert evidence["text_match"] is True
    assert evidence["attribution_present"] is False
    assert evidence["via_bot_id"] is None


def test_a_stored_message_without_the_entity_fails_closed():
    payload = build_self_send_payload(DOC, ALT)
    evidence = verify_self_sent_message(_stored(None), payload, OWNER)
    assert evidence["ok"] is False
    assert evidence["reason"] == "entity_missing"
    assert evidence["entity_present"] is False
    assert "never the emoji" in evidence["error"]


def test_a_stored_entity_with_a_different_document_id_fails_closed():
    payload = build_self_send_payload(DOC, ALT)
    evidence = verify_self_sent_message(
        _stored([_entity(document_id=DOC + 1)]), payload, OWNER
    )
    assert evidence["ok"] is False
    assert evidence["reason"] == "document_id_mismatch"
    assert evidence["document_id_match"] is False


@pytest.mark.parametrize(
    "entity",
    [
        _entity(offset=utf16_length(SELF_SEND_PREFIX) + 1),
        _entity(length=1),
        _entity(length=4),
    ],
)
def test_a_stored_entity_with_a_wrong_span_fails_closed(entity):
    payload = build_self_send_payload(DOC, ALT)
    evidence = verify_self_sent_message(_stored([entity]), payload, OWNER)
    assert evidence["ok"] is False
    assert evidence["span_match"] is False


def test_a_stored_message_in_another_chat_fails_closed():
    payload = build_self_send_payload(DOC, ALT)
    stored = _FakeStored(SELF_SEND_PREFIX + ALT, [_entity()], chat_id=CHAT)
    evidence = verify_self_sent_message(stored, payload, OWNER)
    assert evidence["ok"] is False
    assert evidence["reason"] == "not_saved_messages"
    assert evidence["destination_match"] is False


def test_a_stored_message_with_a_bot_attribution_fails_closed():
    """This route is bot-free: a via_bot_id means the message is not its own."""
    payload = build_self_send_payload(DOC, ALT)
    stored = _FakeStored(
        SELF_SEND_PREFIX + ALT, [_entity()], chat_id=OWNER, via_bot_id=5550001
    )
    evidence = verify_self_sent_message(stored, payload, OWNER)
    assert evidence["ok"] is False
    assert evidence["reason"] == "unexpected_attribution"
    assert evidence["attribution_present"] is True


def test_a_stored_message_with_a_different_text_fails_closed():
    """The artifact must be the text that was sent — entity geometry alone is not enough."""
    payload = build_self_send_payload(DOC, ALT)
    stored = _FakeStored(
        payload["text"] + " (extra)", [_entity()], chat_id=OWNER
    )
    evidence = verify_self_sent_message(stored, payload, OWNER)
    assert evidence["ok"] is False
    assert evidence["reason"] == "text_mismatch"
    assert evidence["text_match"] is False
    assert evidence["span_match"] is True and evidence["document_id_match"] is True


def test_no_stored_message_read_back_fails_closed():
    payload = build_self_send_payload(DOC, ALT)
    evidence = verify_self_sent_message(None, payload, OWNER)
    assert evidence["ok"] is False
    assert evidence["reason"] == "no_message"


# ── offline: the procedure's own guarantees ────────────────────────────────


def test_the_route_sends_exactly_once_and_verifies_the_stored_message():
    client = _FakeSelfClient(owner=OWNER, stored=_stored([_entity()]))
    record = _send(client)
    assert record["ok"] is True
    assert record["message_id"] == 5150
    assert record["send_attempts"] == 1
    assert len(client.sends) == 1
    assert len(client.reads) == 1
    assert isinstance(client.sends[0]["peer"], types.InputPeerSelf)
    assert client.sends[0]["message"] == SELF_SEND_PREFIX + ALT
    entity = client.sends[0]["entities"][0]
    assert isinstance(entity, types.MessageEntityCustomEmoji)
    assert (entity.document_id, entity.offset, entity.length) == (
        DOC,
        utf16_length(SELF_SEND_PREFIX),
        utf16_length(ALT),
    )
    assert client.reads[0][1] == 5150


def test_a_failed_read_back_never_triggers_a_second_send():
    client = _FakeSelfClient(
        owner=OWNER, read_error=RuntimeError("read-back exploded")
    )
    record = _send(client)
    assert record["ok"] is False
    assert record["error"] == ERROR_READBACK
    assert record["send_attempts"] == 1
    assert len(client.sends) == 1
    assert len(client.reads) == 1


def test_a_failed_send_is_reported_and_never_retried():
    client = _FakeSelfClient(owner=OWNER, send_error=RuntimeError("send exploded"))
    record = _send(client)
    assert record["ok"] is False
    assert record["error"] == ERROR_SEND
    assert len(client.sends) == 1
    assert client.reads == []


def test_an_entity_free_stored_message_is_reported_and_nothing_more_is_sent():
    client = _FakeSelfClient(owner=OWNER, stored=_stored(None))
    record = _send(client)
    assert record["ok"] is False
    assert record["diagnosis"] == "entity_missing"
    assert record["send_attempts"] == 1 and len(client.sends) == 1
    assert record["detail"] == record["readback"]["error"]


def test_an_unusable_source_never_reaches_telegram():
    client = _FakeSelfClient(owner=OWNER, stored=_stored([_entity()]))
    record = _send(client, document_id=0)
    assert record["ok"] is False
    assert record["error"] == ERROR_SOURCE
    assert record["diagnosis"] == SOURCE_ENTITY_MISSING
    assert client.sends == [] and client.reads == []


def test_no_glyph_only_fallback_is_ever_built_or_sent():
    """The only message this route can send carries the ENTITY, never a glyph."""
    client = _FakeSelfClient(owner=OWNER, stored=_stored(None))
    record = _send(client)
    assert record["ok"] is False
    (sent,) = client.sends
    assert len(sent["entities"]) == 1
    assert isinstance(sent["entities"][0], types.MessageEntityCustomEmoji)
    assert sent["message"] == record["payload"]["text"]


# ── offline: isolation from production ────────────────────────────────────


def test_the_experiment_is_test_only_and_creates_no_runtime_surface():
    """No client, listener, loop, scheduler or production hook is added here.

    The banned names are assembled from fragments so this assertion's own
    literal text cannot satisfy (or trip) it.
    """
    source = Path(__file__).read_text()
    for banned in (
        "Telegram" + "Client(",
        "events" + ".",
        "create" + "_task",
        "forward" + "_messages",
        "backend" + ".ai",
    ):
        assert banned not in source, f"the experiment must not reference {banned!r}"
    before_live_test = source.split("def test_live_")[0]
    assert "from backend" + ".bot.client import" not in before_live_test
    service_source = Path(svc.__file__).read_text()
    assert "run_self_send_once" not in service_source
    assert "SELF_SEND_PREFIX" not in service_source


def test_the_live_test_requires_an_explicit_opt_in():
    reason = _live_skip_reason()
    if os.getenv(LIVE_ENV_FLAG) == "1" and _live_credentials():
        assert reason == ""
    else:
        assert reason, "the live test must skip without its opt-in and credentials"
        assert LIVE_ENV_FLAG in reason or "API_ID" in reason


def test_the_live_test_is_marked_and_gated():
    """The live test ships marked (opt-in) and skipped, never silently active."""
    markers = getattr(
        test_live_self_send_preserves_entity_in_saved_messages, "pytestmark", []
    )
    names = [getattr(marker, "name", "") for marker in markers]
    assert "live_telegram" in names
    assert "skipif" in names


# ── offline: the live harness's own sequence (so only Telegram is unknown) ─


class _FakeLiveClient(_FakeSelfClient):
    """The extra surface the live sequence uses: ``get_me`` + a bounded history."""

    def __init__(self, *, owner: int, history: list[Any], stored: Any) -> None:
        super().__init__(owner=owner, stored=stored)
        self.history = history

    async def get_me(self):
        return SimpleNamespace(id=self.owner)

    def iter_messages(self, peer, limit=None):
        async def _history():
            for message in self.history[: limit if isinstance(limit, int) else None]:
                yield message

        return _history()


def _fake_documents(monkeypatch, entries: list[dict[str, Any]]) -> None:
    async def _get(client, document_ids):
        return [entry for entry in entries if entry["document_id"] in list(document_ids)]

    monkeypatch.setattr(
        "backend.telegram_api.custom_emoji.get_custom_emoji_documents", _get
    )


def test_the_live_discovery_reads_the_entity_never_the_glyph():
    history = [
        _FakeStored("no emoji here", None, message_id=1),
        _FakeStored("Yo " + ALT, [_entity(offset=3, length=2)], message_id=2),
    ]
    client = _FakeLiveClient(owner=OWNER, history=history, stored=None)
    found = _run(_find_source_custom_emoji(client))
    assert found is not None
    assert found["kind"] == svc.KIND_CUSTOM_EMOJI
    assert found["document_id"] == DOC
    assert found["offset"] == 3 and found["length"] == 2
    assert found["span_text"] == ALT


def test_the_live_discovery_reports_no_source_honestly():
    history = [_FakeStored("plain text", None, message_id=1)]
    client = _FakeLiveClient(owner=OWNER, history=history, stored=None)
    assert _run(_find_source_custom_emoji(client)) is None


def test_the_live_glyph_prefers_the_documents_own_alt(monkeypatch):
    doc = {"document_id": DOC, "alt": "🕒", "free": True, "text_color": None}
    _fake_documents(monkeypatch, [doc])
    assert _run(_resolve_glyph(None, DOC, ALT)) == ("🕒", "document_alt")
    # an unresolvable document falls back to the source span, and never invents one
    _fake_documents(monkeypatch, [])
    assert _run(_resolve_glyph(None, DOC, ALT)) == (ALT, "source_span")
    assert _run(_resolve_glyph(None, DOC, "")) == ("", "")


def test_the_live_sequence_verifies_a_fake_stored_message(monkeypatch):
    """The harness end to end, offline: discovery → glyph → one send → verify."""
    _fake_documents(
        monkeypatch,
        [{"document_id": DOC, "alt": ALT, "free": True, "text_color": None}],
    )
    source = _FakeStored("Yo " + ALT, [_entity(offset=3, length=2)], message_id=2)
    client = _FakeLiveClient(
        owner=OWNER, history=[source], stored=_stored([_entity()])
    )

    record = _run(_live_run(client, OWNER))

    assert record["ok"] is True
    assert record["glyph_source"] == "document_alt"
    assert record["send_attempts"] == 1 and len(client.sends) == 1
    assert record["readback"]["document_id_match"] is True
    assert record["readback"]["attribution_present"] is False
    assert record["diagnosis"] == STORED_ENTITY_VERIFIED_RENDER_UNVERIFIED


def test_the_live_sequence_refuses_a_foreign_session(monkeypatch):
    _fake_documents(monkeypatch, [])
    client = _FakeLiveClient(owner=OWNER + 1, history=[], stored=None)
    with pytest.raises(AssertionError):
        _run(_live_run(client, OWNER))
    assert client.sends == []


# ── the opt-in LIVE test (skips without credentials and the explicit flag) ──


def _live_credentials() -> bool:
    return all(
        os.getenv(name) for name in ("API_ID", "API_HASH", "SESSION_STRING", "BOT_OWNER_ID")
    )


def _live_skip_reason() -> str:
    if os.getenv(LIVE_ENV_FLAG) != "1":
        return (
            f"the live Telegram self-send test is opt-in: set {LIVE_ENV_FLAG}=1 "
            "to allow ONE message to be sent to the owner's Saved Messages"
        )
    if not _live_credentials():
        return (
            "live Telegram self-send test requires API_ID, API_HASH, "
            "SESSION_STRING and BOT_OWNER_ID"
        )
    return ""


async def _find_source_custom_emoji(client: Any) -> dict[str, Any] | None:
    """The known source message: a real custom-emoji entity in Saved Messages.

    Bounded scan (``SOURCE_SCAN_LIMIT``), and the entity is read through the
    production ``inspect_source_message`` — a plain glyph is never accepted.
    """
    async for message in client.iter_messages(
        types.InputPeerSelf(), limit=SOURCE_SCAN_LIMIT
    ):
        found = svc.inspect_source_message(message)
        if found["kind"] == svc.KIND_CUSTOM_EMOJI:
            return found
    return None


async def _live_run(client: Any, owner_id: int) -> dict[str, Any]:
    """The live sequence itself: identity → source → glyph → ONE send + verify.

    Split out of the live test so the whole harness can be driven offline with a
    fake client: if the live run fails, the failure is Telegram's answer or the
    session, never this sequence's logic.
    """
    me = await client.get_me()
    assert me.id == owner_id, (
        "the session must belong to the owner account (BOT_OWNER_ID)"
    )
    pinned = os.getenv(LIVE_DOC_ENV)
    if pinned and pinned.isdigit():
        document_id = int(pinned)
        glyph, glyph_source = await _resolve_glyph(client, document_id, "")
    else:
        found = await _find_source_custom_emoji(client)
        if found is None:
            return {
                "skipped": (
                    "no source custom-emoji message found in Saved Messages "
                    f"(scanned {SOURCE_SCAN_LIMIT}) — reply to a message there "
                    "with a Premium emoji first, or set "
                    f"{LIVE_DOC_ENV} to a custom-emoji document id"
                )
            }
        document_id = found["document_id"]
        glyph, glyph_source = await _resolve_glyph(
            client, document_id, found["span_text"]
        )
    if not glyph:
        return {
            "skipped": (
                "the source emoji's own fallback text could not be resolved, so "
                "the documented alt-wrap rule cannot be satisfied — nothing was sent"
            )
        }
    record = await run_self_send_once(client, owner_id, document_id, glyph)
    record["glyph_source"] = glyph_source
    return record


async def _resolve_glyph(client: Any, document_id: int, source_span: str) -> tuple[str, str]:
    """The glyph the entity must wrap — Telegram's own ``alt`` when resolvable."""
    from backend.telegram_api.custom_emoji import get_custom_emoji_documents

    try:
        documents = await get_custom_emoji_documents(client, [document_id])
    except Exception:
        documents = []
    for document in documents or []:
        if document.get("document_id") == document_id:
            alt = document.get("alt")
            if isinstance(alt, str) and alt:
                return alt, "document_alt"
    if isinstance(source_span, str) and source_span:
        return source_span, "source_span"
    return "", ""


def _live_evidence(record: dict[str, Any], glyph_source: str) -> None:
    """Bounded, non-sensitive evidence for the operator (ids/offsets/spans only)."""
    readback = record.get("readback") or {}
    payload = record.get("payload") or {}
    entity = payload.get("entity") or {}
    print("\n[live self-send] ── evidence ──────────────────────────────")
    print(f"[live self-send] path          : {record.get('send_path')}")
    print(f"[live self-send] send attempts : {record.get('send_attempts')}")
    print(f"[live self-send] message id    : {record.get('message_id')}")
    print(f"[live self-send] chat id       : {readback.get('chat_id')} (owner {record.get('destination_chat_id')})")
    print(f"[live self-send] glyph source  : {glyph_source}")
    print(f"[live self-send] sent entity   : doc {entity.get('document_id')} offset {entity.get('offset')} length {entity.get('length')}")
    print(f"[live self-send] stored entity : present={readback.get('entity_present')} doc {readback.get('document_id')} offset {readback.get('offset')} length {readback.get('length')}")
    print(f"[live self-send] doc id match  : {readback.get('document_id_match')} · span match: {readback.get('span_match')}")
    print(f"[live self-send] via_bot_id    : {readback.get('via_bot_id')!r}")
    print(f"[live self-send] verdict       : {record.get('diagnosis')} (ok={record.get('ok')})")
    print("[live self-send] ───────────────────────────────────────────\n")


@pytest.mark.live_telegram
@pytest.mark.skipif(
    bool(_live_skip_reason()), reason=_live_skip_reason() or "live test disabled"
)
def test_live_self_send_preserves_entity_in_saved_messages():
    """The decisive test: send ONE self-authored custom emoji, verify what Telegram stored.

    Every acceptance criterion is asserted on the STORED message read back by its
    exact id — a matching document id in plain text, a local request object, the
    serialized request or a successful API response alone would all be rejected
    here. ``via_bot_id`` is asserted ABSENT (this route cannot produce it) so the
    inline route's attribution criterion is never silently relaxed.
    """
    from backend.bot.client import build_client

    api_id = int(os.environ["API_ID"])
    api_hash = os.environ["API_HASH"]
    session_string = os.environ["SESSION_STRING"]
    owner_id = int(os.environ["BOT_OWNER_ID"])

    async def _live() -> dict[str, Any]:
        client = await build_client(api_id, api_hash, session_string)
        try:
            return await _live_run(client, owner_id)
        finally:
            await client.disconnect()

    record = _run(_live())
    if record.get("skipped"):
        pytest.skip(record["skipped"])

    _live_evidence(record, record.get("glyph_source", ""))

    readback = record["readback"]
    assert record["ok"] is True, f"the stored message did not verify: {record['detail']}"
    assert record["send_attempts"] == 1, "exactly one message may be sent per run"
    assert isinstance(record["message_id"], int) and record["message_id"] > 0
    assert record["send_path"] == SEND_PATH_SELF_SEND
    assert record["diagnosis"] == STORED_ENTITY_VERIFIED_RENDER_UNVERIFIED
    # destination: Saved Messages (the self chat), verified on the stored message
    assert readback["chat_id"] == owner_id
    assert readback["destination_match"] is True
    # the genuine entity, its identity and its UTF-16 geometry
    assert readback["entity_present"] is True
    assert readback["document_id"] == (record["payload"]["entity"]["document_id"])
    assert readback["document_id_match"] is True
    assert readback["offset"] == record["payload"]["entity"]["offset"]
    assert readback["length"] == record["payload"]["entity"]["length"]
    assert readback["span_match"] is True
    assert readback["text_match"] is True
    # attribution: inherently unavailable on this route — asserted, not relaxed
    assert readback["attribution_present"] is False
    assert readback["via_bot_id"] is None
