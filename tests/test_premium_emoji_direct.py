"""
Premium custom emoji — DIRECT owner-authored delivery (controlled live test).

Pins the ONE question this route exists to answer, offline:

  1. the payload geometry is right — the entity wraps the document's own alt
     text at its REAL UTF-16 offset/length, and the message text is exactly the
     label plus that glyph (no unrelated content);
  2. the send is made by the SELF client to the owner's own Saved Messages,
     carrying a real ``MessageEntityCustomEmoji`` (document id + geometry), and
     the rebuilt TL entity must match the validated payload or nothing is sent;
  3. the EXACT stored message is fetched back BY ID (never a recent scan) and
     the decisive verdict is the read-back's own entity — submission (``ok``)
     and preservation (``entity_preserved``) are separate facts;
  4. the read-back also records the stored message's own chat, so a message
     that is not the one written to the owner's Saved Messages can never read
     as verified;
  5. entity preservation and ``via_bot_id`` attribution are SEPARATE: a direct
     owner-authored message is never claimed to satisfy an attribution
     requirement, and an unexpected ``via_bot_id`` is flagged;
  6. every failure fails CLOSED: a missing/altered entity, an unusable source,
     a non-Saved-Messages destination, an unbuildable entity and a failed
     read-back all produce an honest diagnosis and never a Unicode fallback;
  7. the test path touches Saved Messages only — one send, one read of that
     send's exact id, no delete/edit/forward of anything, and no helper-bot
     inline query or click anywhere in it.

No live Telegram and no Supabase call is made: the Telegram boundary is faked
at the surface the service consumes (``send_message`` / ``get_messages`` on the
self client, the document lookup, ``get_me``), and nothing here claims that
Telegram really preserves the entity on a live send — that is exactly what the
live run must establish.
"""
from __future__ import annotations

import asyncio
import io
import logging
import tokenize
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from telethon.tl.types import MessageEntityCustomEmoji

from backend.bot.handlers import emoji
from backend.helper import inline_engine, input_state
from backend.helper.panel_registry import registry as get_registry
from backend.helper.panels import get_action
from backend.services import premium_emoji_direct_service as direct
from backend.services import premium_emoji_inline_service as svc
from backend.telegram_api._helpers import utf16_length

OWNER = 7770001
OTHER_CHAT = -1001234567890
DOC = 5361626279781934801
ALT = "😵"  # astral: TWO UTF-16 units
BMP_GLYPH = "☺"  # one UTF-16 unit
PREFIX = direct.DIRECT_PREFIX
SELECTION = 909
SENT_ID = 4242


@pytest.fixture(autouse=True)
def _env():
    input_state.clear_all()
    inline_engine.set_self_client(None)
    inline_engine.set_owner_id(OWNER)
    yield
    input_state.clear_all()
    inline_engine.set_self_client(None)


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ── telegram-level fakes ─────────────────────────────────────────────────────


class _StoredMessage:
    """The surface the read-back consumes on a stored message."""

    def __init__(
        self,
        text: str,
        entities: list[Any] | None = None,
        *,
        message_id: int = SENT_ID,
        chat_id: int | None = OWNER,
        via_bot_id: int | None = None,
    ) -> None:
        self.message = text
        self.text = text
        self.entities = entities or []
        self.id = message_id
        self.chat_id = chat_id
        self.via_bot_id = via_bot_id


class _FakeSelfClient:
    """The surface the direct route consumes on the self client.

    Records every interaction so a test can prove the flow touched ONLY the
    message it sent: no scan, no delete, no edit, no forward — and no
    helper-bot inline query or click anywhere in it.
    """

    def __init__(
        self,
        *,
        send_error: Exception | None = None,
        read_error: Exception | None = None,
        documents: list[Any] | None = None,
        documents_error: Exception | None = None,
        next_id: int = SENT_ID,
    ) -> None:
        self.sent: list[dict[str, Any]] = []
        self.reads: list[tuple[Any, Any]] = []
        self.inline_queries: list[tuple[Any, Any]] = []
        self.deletes: list[Any] = []
        self.edits: list[Any] = []
        self.forwards: list[Any] = []
        self.messages: dict[int, Any] = {}
        self.documents = documents or []
        self.documents_error = documents_error
        self.send_error = send_error
        self.read_error = read_error
        self.next_id = next_id

    async def __call__(self, request):
        if self.documents_error is not None:
            raise self.documents_error
        return list(self.documents)

    async def send_message(self, chat_id, text, **kwargs):
        if self.send_error is not None:
            raise self.send_error
        self.sent.append({"chat_id": chat_id, "text": text, "kwargs": kwargs})
        return SimpleNamespace(id=self.next_id, chat_id=chat_id, message=text, entities=[])

    async def get_messages(self, chat_id, ids=None):
        self.reads.append((chat_id, ids))
        if self.read_error is not None:
            raise self.read_error
        return self.messages.get(ids)

    async def inline_query(self, *args, **kwargs):
        self.inline_queries.append((args, kwargs))
        raise AssertionError(
            "the direct path must never run a helper-bot inline query"
        )

    async def delete_messages(self, chat_id, ids):
        self.deletes.append((chat_id, ids))

    async def edit_message(self, *args, **kwargs):
        self.edits.append((args, kwargs))

    async def forward_messages(self, *args, **kwargs):
        self.forwards.append((args, kwargs))

    def iter_messages(self, *args, **kwargs):
        raise AssertionError("the direct test must never scan recent messages")


def _entity(document_id: int = DOC, offset: int = 0, length: int = 2):
    return MessageEntityCustomEmoji(offset, length, document_id)


def _payload_text(glyph: str = ALT) -> str:
    return PREFIX + glyph


def _stored_with(
    glyph: str = ALT,
    document_id: int = DOC,
    *,
    length: int | None = None,
    chat_id: int | None = OWNER,
):
    return _StoredMessage(
        _payload_text(glyph),
        [
            _entity(
                document_id,
                offset=utf16_length(PREFIX),
                length=length if length is not None else utf16_length(glyph),
            )
        ],
        chat_id=chat_id,
    )


def _patch_facts(
    monkeypatch,
    *,
    premium: bool = False,
    free: bool = True,
    text_color: int | None = None,
    alt: str | None = ALT,
    me_error: Exception | None = None,
    docs_error: Exception | None = None,
    document_id: int = DOC,
):
    """The two lookups the direct route shares with the inline route.

    Both live in ``premium_emoji_inline_service`` and are patched there, which
    is also evidence that the direct route REUSES them instead of duplicating
    them.
    """

    async def _fake_get_me(client):
        if me_error is not None:
            raise me_error
        return {"id": OWNER, "premium": premium}

    async def _fake_docs(client, ids):
        if docs_error is not None:
            raise docs_error
        return [
            {
                "document_id": document_id,
                "alt": alt if alt is not None else "",
                "set": None,
                "free": free,
                "text_color": text_color,
            }
        ]

    monkeypatch.setattr(svc, "get_me", _fake_get_me)
    monkeypatch.setattr(svc, "get_custom_emoji_documents", _fake_docs)


def _send(client, monkeypatch, **overrides):
    facts = overrides.pop("facts", {})
    _patch_facts(monkeypatch, **facts)
    return _run(
        direct.send_premium_emoji_direct(
            client,
            overrides.pop("chat_id", OWNER),
            overrides.pop("document_id", DOC),
            overrides.pop("source_glyph", ALT),
            overrides.pop("owner_id", OWNER),
        )
    )


def _sent_entities(client) -> list[Any]:
    assert len(client.sent) == 1
    return client.sent[0]["kwargs"].get("formatting_entities") or []


# ── 1. payload geometry ──────────────────────────────────────────────────────


def test_the_entity_wraps_the_glyph_at_its_real_utf16_offset(monkeypatch):
    client = _FakeSelfClient()
    client.messages[SENT_ID] = _stored_with()
    outcome = _send(client, monkeypatch)

    entities = _sent_entities(client)
    assert len(entities) == 1
    entity = entities[0]
    assert isinstance(entity, MessageEntityCustomEmoji)
    assert entity.document_id == DOC
    assert entity.offset == utf16_length(PREFIX)  # UTF-16 units, not chars
    assert entity.length == utf16_length(ALT) == 2  # astral emoji: two units
    assert outcome["payload"]["entity"] == {
        "type": "MessageEntityCustomEmoji",
        "offset": utf16_length(PREFIX),
        "length": 2,
        "document_id": DOC,
    }


def test_a_bmp_glyph_keeps_a_one_unit_span(monkeypatch):
    client = _FakeSelfClient()
    client.messages[SENT_ID] = _stored_with(BMP_GLYPH)
    outcome = _send(client, monkeypatch, facts={"alt": BMP_GLYPH})

    entity = _sent_entities(client)[0]
    assert entity.length == 1
    assert outcome["payload"]["glyph"] == BMP_GLYPH


def test_the_test_message_is_labelled_and_carries_nothing_else(monkeypatch):
    client = _FakeSelfClient()
    client.messages[SENT_ID] = _stored_with()
    _send(client, monkeypatch)

    assert client.sent[0]["text"] == PREFIX + ALT
    assert client.sent[0]["text"] == "Direct self-chat test: " + ALT
    assert client.sent[0]["kwargs"].get("formatting_entities") is not None


def test_the_send_goes_to_the_owners_own_saved_messages(monkeypatch):
    client = _FakeSelfClient()
    client.messages[SENT_ID] = _stored_with()
    outcome = _send(client, monkeypatch)

    assert client.sent[0]["chat_id"] == OWNER
    assert outcome["send_path"] == direct.SEND_PATH_DIRECT_SELF
    assert outcome["destination_chat_id"] == OWNER


# ── 2. the outbound entity is validated before it is sent ───────────────────


def test_an_invalid_payload_is_never_sent(monkeypatch):
    client = _FakeSelfClient()
    monkeypatch.setattr(direct, "validate_inline_payload", lambda payload: "span mismatch")
    outcome = _send(client, monkeypatch)

    assert client.sent == []
    assert outcome["diagnosis"] == svc.OUTBOUND_PAYLOAD_INVALID
    assert outcome["error"] == svc.ERROR_PAYLOAD
    assert outcome["entity_preserved"] is None
    assert "span mismatch" in outcome["detail"]


def test_an_entity_that_cannot_be_rebuilt_is_never_sent(monkeypatch):
    client = _FakeSelfClient()

    async def _boom(client_, entities):
        raise RuntimeError("unknown entity type")

    monkeypatch.setattr(direct, "dict_entities_to_tl", _boom)
    outcome = _send(client, monkeypatch)

    assert client.sent == []
    assert outcome["diagnosis"] == svc.OUTBOUND_PAYLOAD_INVALID
    assert "unknown entity type" in outcome["detail"]


def test_a_rebuilt_entity_with_other_geometry_is_never_sent(monkeypatch):
    client = _FakeSelfClient()

    async def _wrong(client_, entities):
        return [_entity(document_id=DOC, offset=0, length=1)]

    monkeypatch.setattr(direct, "dict_entities_to_tl", _wrong)
    outcome = _send(client, monkeypatch)

    assert client.sent == []
    assert outcome["diagnosis"] == svc.OUTBOUND_PAYLOAD_INVALID
    assert "does not match the validated payload" in outcome["detail"]


# ── 3. exact-message read-back and preservation ─────────────────────────────


def test_a_preserved_entity_is_verified_without_any_attribution_claim(monkeypatch):
    client = _FakeSelfClient()
    client.messages[SENT_ID] = _stored_with()
    outcome = _send(client, monkeypatch)

    assert outcome["ok"] is True
    assert outcome["entity_preserved"] is True
    assert outcome["verified"] is True
    assert outcome["diagnosis"] == svc.STORED_ENTITY_VERIFIED_RENDER_UNVERIFIED
    readback = outcome["readback"]
    assert readback["attempted"] is True and readback["ok"] is True
    assert readback["entity_present"] is True
    assert readback["document_id"] == DOC
    assert readback["document_id_match"] is True
    assert readback["span_match"] is True
    assert readback["text_match"] is True
    assert readback["stored_chat_id"] == OWNER
    assert readback["destination_match"] is True
    assert readback["via_bot_id"] is None
    assert readback["via_bot_match"] is None


def test_the_read_back_fetches_the_exact_sent_id_and_never_scans(monkeypatch):
    client = _FakeSelfClient()
    client.messages[SENT_ID] = _stored_with()
    _send(client, monkeypatch)

    assert client.reads == [(OWNER, SENT_ID)]
    assert client.deletes == [] and client.edits == [] and client.forwards == []


def test_a_stripped_entity_distinguishes_submission_from_preservation(monkeypatch):
    client = _FakeSelfClient()
    client.messages[SENT_ID] = _StoredMessage(_payload_text(), [])
    outcome = _send(client, monkeypatch)

    assert outcome["ok"] is True  # Telegram accepted the RPC
    assert outcome["entity_preserved"] is False  # but stored no entity
    assert outcome["verified"] is False
    assert outcome["diagnosis"] == svc.STORED_ENTITY_STRIPPED
    assert outcome["readback"]["entity_present"] is False


def test_an_altered_document_id_is_a_mismatch(monkeypatch):
    client = _FakeSelfClient()
    client.messages[SENT_ID] = _stored_with(document_id=DOC + 1)
    outcome = _send(client, monkeypatch)

    assert outcome["ok"] is True
    assert outcome["entity_preserved"] is False
    assert outcome["diagnosis"] == svc.STORED_ENTITY_MISMATCH
    assert outcome["readback"]["document_id_match"] is False


def test_an_altered_span_is_a_mismatch(monkeypatch):
    client = _FakeSelfClient()
    client.messages[SENT_ID] = _stored_with(length=1)
    outcome = _send(client, monkeypatch)

    assert outcome["entity_preserved"] is False
    assert outcome["diagnosis"] == svc.STORED_ENTITY_MISMATCH
    assert outcome["readback"]["span_match"] is False


def test_a_stored_message_from_another_chat_is_never_verified(monkeypatch):
    client = _FakeSelfClient()
    client.messages[SENT_ID] = _stored_with(chat_id=OTHER_CHAT)
    outcome = _send(client, monkeypatch)

    assert outcome["ok"] is True  # the send happened
    assert outcome["verified"] is False  # but the fetched message is not ours
    assert outcome["diagnosis"] == direct.STORED_DESTINATION_MISMATCH
    assert outcome["readback"]["destination_match"] is False
    assert outcome["readback"]["stored_chat_id"] == OTHER_CHAT
    assert "not your Saved Messages" in direct.outcome_summary(outcome)


def test_a_missing_entity_after_sending_is_reported_as_stripped_not_sent_again(monkeypatch):
    client = _FakeSelfClient()
    client.messages[SENT_ID] = _StoredMessage(f"{PREFIX}plain text", [])
    outcome = _send(client, monkeypatch)

    assert client.sent and len(client.sent) == 1  # exactly ONE message ever sent
    assert outcome["diagnosis"] == svc.STORED_ENTITY_STRIPPED
    summary = direct.outcome_summary(outcome)
    assert "WITHOUT the custom-emoji entity" in summary
    assert "does not preserve it" in summary


# ── 4. preservation vs via_bot_id attribution ───────────────────────────────


def test_a_direct_message_records_attribution_as_not_applicable(monkeypatch):
    client = _FakeSelfClient()
    client.messages[SENT_ID] = _stored_with()
    outcome = _send(client, monkeypatch)

    attribution = outcome["attribution"]
    assert attribution["applicable"] is False
    assert attribution["via_bot_id"] is None
    assert attribution["via_bot_match"] is None
    assert "does not satisfy a via_bot_id requirement" in attribution["note"]


def test_an_unexpected_via_bot_id_is_flagged_and_never_claimed(monkeypatch):
    client = _FakeSelfClient()
    client.messages[SENT_ID] = _stored_with()
    client.messages[SENT_ID].via_bot_id = 5550001
    outcome = _send(client, monkeypatch)

    assert outcome["entity_preserved"] is True  # the entity IS preserved
    assert outcome["verified"] is False  # but the route is not proven
    assert outcome["diagnosis"] == direct.STORED_UNEXPECTED_ATTRIBUTION
    assert outcome["attribution"]["via_bot_id"] == 5550001
    assert outcome["attribution"]["via_bot_match"] is None


def test_the_summary_never_turns_entity_preservation_into_attribution(monkeypatch):
    client = _FakeSelfClient()
    client.messages[SENT_ID] = _stored_with()
    outcome = _send(client, monkeypatch)

    summary = direct.outcome_summary(outcome)
    assert "NOT bot-attributed" in summary
    assert "never a `via_bot_id` attribution" in summary
    assert "confirm visually" in summary


def test_classify_direct_diagnosis_covers_every_readback_state():
    assert direct.classify_direct_diagnosis({}) == svc.READBACK_FAILED
    assert direct.classify_direct_diagnosis(
        {"ok": True, "destination_match": False, "entity_present": True}
    ) == direct.STORED_DESTINATION_MISMATCH
    assert direct.classify_direct_diagnosis(
        {"ok": True, "entity_present": False}
    ) == svc.STORED_ENTITY_STRIPPED
    assert direct.classify_direct_diagnosis(
        {"ok": True, "entity_present": True, "document_id_match": False, "span_match": True}
    ) == svc.STORED_ENTITY_MISMATCH
    assert direct.classify_direct_diagnosis(
        {"ok": True, "entity_present": True, "document_id_match": True, "span_match": True,
         "via_bot_id": 7}
    ) == direct.STORED_UNEXPECTED_ATTRIBUTION
    assert direct.classify_direct_diagnosis(
        {"ok": True, "entity_present": True, "document_id_match": True, "span_match": True,
         "via_bot_id": None}
    ) == svc.STORED_ENTITY_VERIFIED_RENDER_UNVERIFIED


# ── 5. fail-closed behaviour ────────────────────────────────────────────────


def test_a_readback_failure_is_reported_separately_from_the_send(monkeypatch):
    client = _FakeSelfClient(read_error=RuntimeError("boom"))
    outcome = _send(client, monkeypatch)

    assert outcome["ok"] is True  # the send happened
    assert outcome["entity_preserved"] is None  # nothing about the entity is known
    assert outcome["diagnosis"] == svc.READBACK_FAILED
    assert outcome["readback"]["attempted"] is True
    assert "boom" in outcome["readback"]["error"]
    assert len(client.sent) == 1


def test_a_send_without_a_message_id_cannot_be_read_back(monkeypatch):
    client = _FakeSelfClient(next_id=0)
    outcome = _send(client, monkeypatch)

    assert outcome["ok"] is True
    assert outcome["message_id"] is None
    assert outcome["diagnosis"] == svc.READBACK_FAILED
    assert outcome["readback"]["attempted"] is False
    assert client.reads == []


def test_a_failed_send_is_reported_and_nothing_is_read(monkeypatch):
    client = _FakeSelfClient(send_error=RuntimeError("CHAT_WRITE_FORBIDDEN"))
    outcome = _send(client, monkeypatch)

    assert outcome["ok"] is False
    assert outcome["entity_preserved"] is None
    assert outcome["diagnosis"] == direct.DIRECT_SEND_FAILED
    assert outcome["error"] == svc.ERROR_SEND
    assert "CHAT_WRITE_FORBIDDEN" in outcome["detail"]
    assert client.reads == []


def test_a_non_saved_messages_destination_is_refused_before_anything_is_sent(monkeypatch):
    client = _FakeSelfClient()
    outcome = _send(client, monkeypatch, chat_id=OTHER_CHAT)

    assert client.sent == [] and client.reads == []
    assert outcome["diagnosis"] == svc.UNSUPPORTED_DESTINATION
    assert outcome["error"] == svc.ERROR_DESTINATION


def test_an_unusable_document_id_never_reaches_telegram(monkeypatch):
    client = _FakeSelfClient()
    outcome = _send(client, monkeypatch, document_id=0)

    assert client.sent == []
    assert outcome["diagnosis"] == svc.SOURCE_ENTITY_MISSING
    assert outcome["error"] == svc.ERROR_SOURCE


def test_an_unusable_owner_never_reaches_telegram(monkeypatch):
    client = _FakeSelfClient()
    outcome = _send(client, monkeypatch, owner_id=0)

    assert client.sent == []
    assert outcome["diagnosis"] == svc.SOURCE_ENTITY_MISSING
    assert outcome["error"] == svc.ERROR_OWNER


def test_an_unresolvable_glyph_fails_closed(monkeypatch):
    client = _FakeSelfClient()
    outcome = _send(client, monkeypatch, source_glyph="", facts={"alt": ""})

    assert client.sent == []
    assert outcome["diagnosis"] == svc.SOURCE_ENTITY_MISSING
    assert "alt text" in outcome["detail"]


def test_a_failed_document_lookup_falls_back_to_the_source_span(monkeypatch):
    client = _FakeSelfClient()
    client.messages[SENT_ID] = _stored_with()
    outcome = _send(client, monkeypatch, facts={"docs_error": RuntimeError("no docs")})

    assert outcome["eligibility"]["glyph_source"] == "source_span"
    assert outcome["eligibility"]["document_error"]
    assert outcome["payload"]["glyph"] == ALT
    assert outcome["diagnosis"] == svc.STORED_ENTITY_VERIFIED_RENDER_UNVERIFIED


def test_the_document_alt_wins_over_the_source_span(monkeypatch):
    client = _FakeSelfClient()
    client.messages[SENT_ID] = _stored_with()
    outcome = _send(client, monkeypatch, source_glyph="x", facts={"alt": ALT})

    assert outcome["eligibility"]["glyph_source"] == "document_alt"
    assert outcome["payload"]["glyph"] == ALT


def test_premium_and_document_facts_are_recorded_as_evidence(monkeypatch):
    client = _FakeSelfClient()
    client.messages[SENT_ID] = _stored_with()
    outcome = _send(client, monkeypatch, facts={"premium": True, "free": True, "text_color": 1})

    eligibility = outcome["eligibility"]
    assert eligibility["owner_premium"] is True
    assert eligibility["document"]["free"] is True
    assert eligibility["document"]["text_color"] == 1


def test_an_unreadable_premium_flag_does_not_block_the_test(monkeypatch):
    client = _FakeSelfClient()
    client.messages[SENT_ID] = _stored_with()
    outcome = _send(client, monkeypatch, facts={"me_error": RuntimeError("me unavailable")})

    assert outcome["eligibility"]["owner_premium"] is None
    assert outcome["eligibility"]["owner_premium_error"]
    assert outcome["diagnosis"] == svc.STORED_ENTITY_VERIFIED_RENDER_UNVERIFIED


def test_the_direct_flow_never_runs_an_inline_query(monkeypatch):
    """No helper-bot inline query or click exists anywhere in this path."""
    client = _FakeSelfClient()
    client.messages[SENT_ID] = _stored_with()
    _send(client, monkeypatch)

    assert client.inline_queries == []


# ── 6. the Glass UI entry point ─────────────────────────────────────────────


def _reply(
    *,
    text: str = ALT,
    entities: list[Any] | None = None,
    message_id: int = 910,
    reply_to: int | None = SELECTION,
    peer: Any = None,
):
    return SimpleNamespace(
        id=message_id,
        message=text,
        text=text,
        entities=list(entities) if entities is not None else [_entity()],
        reply_to=SimpleNamespace(reply_to_msg_id=reply_to, reply_to_peer_id=peer),
    )


def _arm(client, monkeypatch) -> dict[str, Any]:
    """Point the handler at a client and capture what it renders."""
    inline_engine.set_self_client(client)
    captured: dict[str, Any] = {}

    async def _fake_edit(inline_chat_id, inline_msg_id, title, body, buttons):
        captured.update(
            chat_id=inline_chat_id,
            msg_id=inline_msg_id,
            title=title,
            body=body,
            buttons=buttons,
        )

    monkeypatch.setattr(emoji, "_edit_inline", _fake_edit)
    return captured


def test_the_direct_action_is_registered_but_not_a_primary_panel_button(monkeypatch):
    from backend.db import client as db_client

    emoji.register(client=None, owner_id=OWNER)
    assert get_action(emoji._DIRECT_ACTION) is not None
    assert get_registry().get(emoji._DIRECT_ACTION) is None  # an action, not a panel

    db_client._fallback["emoji_library"] = []
    _title, _body, buttons = _run(emoji._emoji_panel_handler(None, ""))
    datas = [
        btn.data.decode() if isinstance(btn.data, bytes) else str(btn.data)
        for row in buttons
        for btn in (row if isinstance(row, list) else [row])
    ]
    # The standalone direct-test button is gone from the primary panel; the
    # panel exposes EXACTLY ONE application-level button (the diagnostics one).
    assert f"action:{emoji._DIRECT_ACTION}" not in datas
    assert datas == [f"action:{emoji._DIAGNOSTICS_ACTION}"]


def test_the_action_arms_reply_mode_on_the_exact_selection_message():
    client = _FakeSelfClient(next_id=SELECTION)
    inline_engine.set_self_client(client)
    title, body, _buttons = _run(
        emoji._premium_direct_action(SimpleNamespace(message_id=555), "", 111)
    )
    assert title == "Direct Emoji Test"
    assert f"#{SELECTION}" in body
    pending = input_state.get_pending(OWNER)
    assert pending is not None
    assert pending["panel_id"] == emoji._DIRECT_ACTION
    assert pending["extra"] == str(SELECTION)
    input_state.clear_all()


def test_the_action_fails_honestly_without_a_self_client():
    inline_engine.set_self_client(None)
    title, body, _buttons = _run(
        emoji._premium_direct_action(SimpleNamespace(message_id=555), "", 111)
    )
    assert "not connected" in body
    input_state.clear_all()


def test_the_action_refuses_without_the_configured_owner(monkeypatch):
    monkeypatch.setattr(emoji, "get_owner_id", lambda: 0)
    client = _FakeSelfClient(next_id=SELECTION)
    inline_engine.set_self_client(client)
    _title, body, _buttons = _run(
        emoji._premium_direct_action(SimpleNamespace(message_id=555), "", 111)
    )
    assert "Owner is not set" in body
    assert client.sent == []
    assert input_state.get_pending(OWNER) is None
    input_state.clear_all()


def test_a_reply_to_another_message_never_reaches_the_service(monkeypatch):
    client = _FakeSelfClient()
    client.messages[911] = _reply(message_id=911, reply_to=1234)
    captured = _arm(client, monkeypatch)

    async def _boom(*args, **kwargs):
        raise AssertionError("the service must not run for a foreign reply")

    monkeypatch.setattr(emoji.premium_direct_service, "send_premium_emoji_direct", _boom)
    _run(emoji._premium_direct_reply_handler("", OWNER, 911, 111, 555, extra=str(SELECTION)))

    assert client.sent == []
    assert "not the selection message" in captured["body"]


def test_a_non_reply_never_reaches_the_service(monkeypatch):
    client = _FakeSelfClient()
    client.messages[910] = _reply(reply_to=None)

    async def _boom(*args, **kwargs):
        raise AssertionError("the service must not run for a non-reply")

    monkeypatch.setattr(emoji.premium_direct_service, "send_premium_emoji_direct", _boom)
    _run(emoji._premium_direct_reply_handler("", OWNER, 910, 111, 555, extra=str(SELECTION)))
    assert client.sent == []


def test_a_unicode_reply_is_refused_and_never_sent(monkeypatch):
    client = _FakeSelfClient()
    client.messages[910] = _reply(text="🙂", entities=[])
    captured = _arm(client, monkeypatch)

    async def _boom(*args, **kwargs):
        raise AssertionError("a plain Unicode emoji is not a custom emoji")

    monkeypatch.setattr(emoji.premium_direct_service, "send_premium_emoji_direct", _boom)
    _run(emoji._premium_direct_reply_handler("", OWNER, 910, 111, 555, extra=str(SELECTION)))

    assert client.sent == []
    assert "Unicode emoji is not a Premium custom emoji" in captured["body"]


def test_a_reply_in_another_chat_is_refused_by_the_service(monkeypatch):
    client = _FakeSelfClient()
    client.messages[910] = _reply()
    captured = _arm(client, monkeypatch)
    _patch_facts(monkeypatch)

    _run(
        emoji._premium_direct_reply_handler(
            "", OTHER_CHAT, 910, 111, 555, extra=str(SELECTION)
        )
    )

    assert client.sent == []
    assert svc.UNSUPPORTED_DESTINATION in captured["body"]
    assert "Saved Messages" in captured["body"]


def test_the_reply_handler_runs_the_direct_test_and_reports_it(monkeypatch):
    client = _FakeSelfClient()
    client.messages[SENT_ID] = _stored_with()
    client.messages[910] = _reply()
    captured = _arm(client, monkeypatch)
    _patch_facts(monkeypatch)

    _run(emoji._premium_direct_reply_handler("", OWNER, 910, 111, 555, extra=str(SELECTION)))

    assert len(client.sent) == 1
    assert client.sent[0]["chat_id"] == OWNER
    assert client.sent[0]["text"] == PREFIX + ALT
    assert captured["title"] == "Direct Emoji Test ✓"
    assert svc.STORED_ENTITY_VERIFIED_RENDER_UNVERIFIED in captured["body"]
    assert "Entity preserved: yes" in captured["body"]
    assert captured["buttons"]


def test_an_unknown_selection_id_refuses_before_any_read(monkeypatch):
    client = _FakeSelfClient()
    captured = _arm(client, monkeypatch)
    _run(emoji._premium_direct_reply_handler("", OWNER, 910, 111, 555, extra=""))
    assert client.reads == [] and client.sent == []
    assert "no longer known" in captured["body"]


# ── 7. the report never over-claims ─────────────────────────────────────────


def test_the_report_states_preservation_and_attribution_separately(monkeypatch):
    client = _FakeSelfClient()
    client.messages[SENT_ID] = _stored_with()
    outcome = _send(client, monkeypatch)
    report = emoji._premium_direct_report(outcome)

    assert "Entity preserved: yes" in report
    assert "Bot attribution: NOT applicable" in report
    assert "never satisfies a `via_bot_id` requirement" in report
    assert "confirm visually" in report
    assert "rendered" not in report


def test_the_stripped_report_says_so(monkeypatch):
    client = _FakeSelfClient()
    client.messages[SENT_ID] = _StoredMessage(_payload_text(), [])
    outcome = _send(client, monkeypatch)
    report = emoji._premium_direct_report(outcome)

    assert svc.STORED_ENTITY_STRIPPED in report
    assert "Entity preserved: NO" in report
    assert "Message: `#4242`" in report


def test_the_failed_report_names_the_path_and_no_message(monkeypatch):
    client = _FakeSelfClient(send_error=RuntimeError("nope"))
    outcome = _send(client, monkeypatch)
    report = emoji._premium_direct_report(outcome)

    assert direct.DIRECT_SEND_FAILED in report
    assert "Message: not sent" in report
    assert "Path: not reached" in report


# ── 8. architecture: additive, bounded, no second infrastructure ────────────


def _code_only(path: Path) -> str:
    pieces: list[str] = []
    try:
        for token in tokenize.generate_tokens(io.StringIO(path.read_text()).readline):
            if token.type in (tokenize.COMMENT, tokenize.STRING, tokenize.NL):
                continue
            pieces.append(token.string)
    except tokenize.TokenError:
        return path.read_text()
    return " ".join(pieces)


def test_the_direct_module_adds_no_second_infrastructure():
    code = _code_only(Path(direct.__file__))
    for forbidden in (
        "TelegramClient",
        "run_until_disconnected",
        "create_task",
        "asyncio.Lock",
        "new_event_loop",
        "iter_messages",
        "forward_messages",
        "events.",
        "httpx",
    ):
        assert forbidden not in code, forbidden


def test_the_direct_module_adds_no_listener_and_no_update_loop():
    source = Path(direct.__file__).read_text()
    assert "add_event_handler" not in source
    assert "on(events." not in source
    assert "register_inline_handler" not in source


def test_the_direct_module_touches_no_database_and_no_ai():
    source = Path(direct.__file__).read_text()
    for forbidden in ("db_client", "supabase", "Supabase", "to_thread", "insert(", "upsert"):
        assert forbidden not in source, forbidden
    code = _code_only(Path(direct.__file__))
    assert "backend.ai" not in code


def test_the_direct_module_reuses_the_shared_entity_and_readback_helpers():
    source = Path(direct.__file__).read_text()
    assert "from backend.services.premium_emoji_inline_service import" in source
    for shared in (
        "build_inline_payload",
        "validate_inline_payload",
        "_scan_custom_emoji",
        "_span_text",
        "_fetch_stored_message",
        "_document_facts",
        "_owner_premium",
    ):
        assert shared in source, shared


def test_the_ui_holds_no_send_logic_and_no_payload_building():
    ui = Path(emoji.__file__).read_text()
    assert "formatting_entities" not in ui  # the service owns the TL entities
    assert "dict_entities_to_tl" not in ui
    assert "build_inline_payload" not in ui
    code = _code_only(Path(emoji.__file__))
    assert "send_premium_emoji_direct" in code  # the UI delegates the send


def test_the_inline_route_is_not_rerouted_through_the_direct_one():
    inline_code = _code_only(Path(svc.__file__))
    assert "premium_emoji_direct_service" not in inline_code
    assert "send_premium_emoji_direct" not in inline_code
    assert "DIRECT_PREFIX" not in inline_code
    # the fail-closed verdict the production route rests on is unchanged
    assert svc.INLINE_RESULT_ENTITY_MISSING == "INLINE_RESULT_ENTITY_MISSING"
    assert svc.STORED_ENTITY_VERIFIED_RENDER_UNVERIFIED == "STORED_ENTITY_VERIFIED_RENDER_UNVERIFIED"


def test_the_direct_trace_is_bounded_and_names_no_secret(monkeypatch, caplog):
    client = _FakeSelfClient()
    client.messages[SENT_ID] = _stored_with()
    caplog.set_level(logging.INFO, logger="backend.services.premium_emoji_direct_service")
    _send(client, monkeypatch)

    text = "\n".join(record.getMessage() for record in caplog.records)
    for stage in (
        "SOURCE_ENTITY_ACCEPTED",
        "DIRECT_SEND_STARTED",
        "DIRECT_SEND_ACCEPTED",
        "READBACK_RESULT",
        "DIAGNOSIS",
    ):
        assert stage in text, stage
    assert "BOT_TOKEN" not in text
    assert "access_hash" not in text
    assert "session" not in text.lower()
