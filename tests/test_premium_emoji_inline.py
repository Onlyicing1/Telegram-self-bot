"""
Premium emoji through the inline-bot path — production feature.

Source-proven contracts this file pins (see IMPLEMENTATION_REPORT.md and
INVESTIGATION.md):

  1. ``inspect_source_message`` accepts ONLY a real
     ``MessageEntityCustomEmoji`` with a usable document id AND a UTF-16 span
     that resolves inside the message text; a plain Unicode emoji, a media-only
     message and an unusable id all fail closed with an honest reason.
  2. ``build_inline_payload`` / ``build_inline_result`` put the REAL entity
     (document id + UTF-16 offset/length) into an ``InputBotInlineMessageText``
     the helper bot answers with — and validate it before submission.
  3. The send goes through ``inline_engine.query_results`` +
     ``inline_engine.click_result`` (the two halves of the existing
     ``trigger``), so the owner's own account is the sender and Telegram's own
     ``via_bot_id`` is the only attribution — never a fabricated one.
  4. ``send_premium_emoji_via_inline`` records the stored inline result
     (checkpoint 2) AND the exact stored message read back by id
     (checkpoint 3), and classifies ONLY what that evidence shows: a retained
     entity with the expected id/span/attribution is still
     ``…_RENDER_UNVERIFIED``.
  5. A failure NEVER falls back to a plain Unicode emoji: no other payload is
     ever built or sent, and a stripped entity is reported as stripped.
  6. The Glass UI entry point is the production action
     (``emoji_premium_inline``) wired to the registered inline builder;
     ``inline_engine.trigger`` keeps its exact contract, and the two serializer
     additions (``free``/``text_color``, ``premium``) are additive.
  7. The pending-input listener — the flow's ONLY update path — reaches the
     production reply handler with the selection id (``extra``) intact, and
     every dispatched reply emits its first ``[PREMIUM_INLINE]`` trace
     (``SOURCE_ENTITY_*``); a reply in another chat dispatches nothing and
     emits none.

No live Telegram and no Supabase call is made: the Telegram boundary is faked
at the surface the service consumes (``inline_query`` / ``click`` /
``get_messages`` / ``get_me`` on the self client, the document lookup), and no
test claims Telegram will keep the entity on a real send.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from telethon import types

import backend.helper.client as helper_client
from backend.bot.handlers import emoji
from backend.helper import inline_engine
from backend.helper.panels import get_action
from backend.services import premium_emoji_inline_service as svc
from backend.telegram_api._helpers import serialize_user, utf16_length

OWNER = 7770001
OTHER_CHAT = -1001234567890
BOT_ID = 5550001
DOC = 5361626279781934801
ALT = "😵"
PREFIX = svc.PREFIX
SELECTION = 909


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    inline_engine.set_self_client(None)
    inline_engine.set_owner_id(OWNER)
    inline_engine.set_helper_username("nitro_selfbot")
    inline_engine.set_helper_id(BOT_ID)
    helper_client._bot_id = BOT_ID
    monkeypatch.setattr(helper_client, "is_available", lambda: True)
    yield
    helper_client._bot_id = 0
    inline_engine.set_helper_username("")
    inline_engine.set_helper_id(0)
    inline_engine.set_self_client(None)


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ── telegram-level fakes ─────────────────────────────────────────────────────


class _FakeMessage:
    """The surface the read-back path consumes on a stored message."""

    def __init__(
        self,
        text: str,
        entities: list[Any] | None = None,
        *,
        message_id: int = 4242,
        via_bot_id: int | None = None,
    ) -> None:
        self.message = text
        self.text = text
        self.entities = entities or []
        self.id = message_id
        self.via_bot_id = via_bot_id


class _FakeInlineResult:
    """A stored inline result as ``getInlineBotResults`` returns it.

    Telegram returns ``BotInlineResult`` objects, whose field is
    ``send_message`` (a ``BotInlineMessageText``), not ``message``.
    The service's checkpoint-2 inspection reads ``send_message``, so this
    fake mirrors that field name exactly.
    """

    def __init__(
        self,
        text: str,
        entities: list[Any] | None,
        *,
        message_id: int = 4242,
        via_bot_id: int | None = BOT_ID,
        click_error: Exception | None = None,
        click_returns_none: bool = False,
    ) -> None:
        self.send_message = SimpleNamespace(message=text, entities=entities or [])
        self.clicks = 0
        self._message_id = message_id
        self._via_bot_id = via_bot_id
        self._click_error = click_error
        self._click_returns_none = click_returns_none

    async def click(self, chat_id):
        self.clicks += 1
        if self._click_error is not None:
            raise self._click_error
        if self._click_returns_none:
            return None
        return _FakeMessage(
            self.send_message.message,
            self.send_message.entities,
            message_id=self._message_id,
            via_bot_id=self._via_bot_id,
        )


class _FakeSelfClient:
    """The surface the service consumes on the self client."""

    def __init__(
        self,
        results: Any = None,
        *,
        stored: Any = None,
        query_error: Exception | None = None,
        read_error: Exception | None = None,
        reply: Any = None,
        result_for_query: Any = None,
    ) -> None:
        self.results = results
        self.stored = stored
        self.query_error = query_error
        self.read_error = read_error
        self.reply = reply
        self.queries: list[tuple[Any, str, Any]] = []
        self.reads: list[tuple[Any, Any]] = []
        self.sent_messages: list[Any] = []
        self.result_for_query = result_for_query

    async def inline_query(self, username, query, entity=None):
        self.queries.append((username, query, entity))
        if self.query_error is not None:
            raise self.query_error
        if self.result_for_query is not None:
            return [
                _FakeInlineResult(
                    *self.result_for_query,
                    message_id=(self.stored.id if self.stored is not None else 4242),
                    via_bot_id=getattr(self.stored, "via_bot_id", BOT_ID),
                )
            ]
        return self.results if self.results is not None else []

    async def get_messages(self, chat_id, ids=None):
        self.reads.append((chat_id, ids))
        if self.read_error is not None:
            raise self.read_error
        if self.stored is not None and ids == getattr(self.stored, "id", None):
            return self.stored
        if self.reply is not None and ids == getattr(self.reply, "id", None):
            return self.reply
        return None

    async def send_message(self, chat_id, text, **kwargs):
        message = _FakeMessage(text, [], message_id=SELECTION)
        message.chat_id = OWNER
        self.sent_messages.append((chat_id, text))
        return message

    async def delete_messages(self, chat_id, ids):
        return None


def _entity(document_id: int = DOC, offset: int = 0, length: int = 2):
    return types.MessageEntityCustomEmoji(offset, length, document_id)


def _payload_text(glyph: str = ALT) -> str:
    return PREFIX + glyph


def _result_payload(glyph: str = ALT, document_id: int = DOC):
    """The exact result the helper bot is expected to answer with.

    The entity sits at the payload's real UTF-16 offset, so a stored message
    built from this pair matches the expected geometry exactly.
    """

    return (
        _payload_text(glyph),
        [_entity(document_id, offset=utf16_length(PREFIX), length=utf16_length(glyph))],
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


def _send(client, monkeypatch, **kwargs):
    _patch_facts(monkeypatch, **kwargs.pop("facts", {}))
    return _run(
        svc.send_premium_emoji_via_inline(
            client,
            kwargs.pop("chat_id", OWNER),
            kwargs.pop("document_id", DOC),
            kwargs.pop("source_glyph", ALT),
            kwargs.pop("owner_id", OWNER),
        )
    )


# ── source entity extraction ────────────────────────────────────────────────


def test_valid_custom_emoji_entity_is_read_from_the_entity_not_the_glyph():
    message = _FakeMessage("Yo " + ALT, [_entity(offset=3, length=2)])
    found = svc.inspect_source_message(message)
    assert found["kind"] == svc.KIND_CUSTOM_EMOJI
    assert found["document_id"] == DOC
    assert found["offset"] == 3
    assert found["length"] == 2
    assert found["span_text"] == ALT


def test_embedding_emoji_after_text_uses_utf16_offsets():
    text = "a😀" + ALT + "b"          # 😀 is 2 UTF-16 units, ALT is 2 more
    message = _FakeMessage(text, [_entity(offset=3, length=2)])
    found = svc.inspect_source_message(message)
    assert found["span_text"] == ALT
    assert found["offset"] == utf16_length("a😀")


def test_missing_entity_reports_unicode_honestly():
    found = svc.inspect_source_message(_FakeMessage("just a reply"))
    assert found["kind"] == svc.KIND_UNICODE
    assert found["document_id"] is None
    assert "custom-emoji entity" in found["detail"]


def test_empty_message_is_none():
    found = svc.inspect_source_message(_FakeMessage(""))
    assert found["kind"] == svc.KIND_NONE


def test_custom_emoji_entity_with_unusable_document_id_fails_closed():
    message = _FakeMessage(ALT, [_entity(document_id=0)])
    found = svc.inspect_source_message(message)
    assert found["kind"] == svc.KIND_NONE
    assert "unusable document id" in found["detail"]


def test_custom_emoji_entity_with_unresolvable_span_fails_closed():
    message = _FakeMessage(ALT, [_entity(offset=99, length=2)])
    found = svc.inspect_source_message(message)
    assert found["kind"] == svc.KIND_NONE
    assert "does not resolve" in found["detail"]


def test_non_custom_emoji_entities_are_ignored_not_promoted():
    message = _FakeMessage("bold text", [types.MessageEntityBold(0, 4)])
    assert svc.inspect_source_message(message)["kind"] == svc.KIND_UNICODE


# ── payload construction and validation ─────────────────────────────────────


def test_payload_offsets_are_utf16_code_units():
    payload = svc.build_inline_payload(DOC, ALT, "پ 😀")
    assert payload["text"] == "پ 😀" + ALT
    assert payload["entity"]["offset"] == utf16_length("پ 😀")
    assert payload["entity"]["length"] == utf16_length(ALT) == 2
    assert payload["entity"]["document_id"] == DOC
    assert svc.validate_inline_payload(payload) == ""


def test_payload_requires_a_real_document_id_and_glyph():
    with pytest.raises(ValueError):
        svc.build_inline_payload(0, ALT)
    with pytest.raises(ValueError):
        svc.build_inline_payload(DOC, "")


def test_payload_validation_rejects_each_broken_shape():
    good = svc.build_inline_payload(DOC, ALT)
    broken = dict(good)
    broken["entity"] = dict(good["entity"], type="MessageEntityBold")
    assert svc.validate_inline_payload(broken)
    broken = dict(good)
    broken["entity"] = dict(good["entity"], document_id=0)
    assert "document id" in svc.validate_inline_payload(broken)
    broken = dict(good)
    broken["entity"] = dict(good["entity"], offset=len(PREFIX) + 1)
    assert svc.validate_inline_payload(broken)
    broken = dict(good)
    broken["entity"] = dict(good["entity"], length=4)
    assert svc.validate_inline_payload(broken)
    broken = dict(good)
    broken["entities"] = []
    assert svc.validate_inline_payload(broken)
    assert svc.validate_inline_payload(None)


# ── inline result construction ──────────────────────────────────────────────


def test_inline_result_carries_the_real_entity():
    result = svc.build_inline_result(DOC, ALT)
    message = result.send_message
    assert isinstance(result, types.InputBotInlineResult)
    assert result.type == "article"
    assert message.message == PREFIX + ALT
    assert message.reply_markup is None
    assert len(message.entities) == 1
    entity = message.entities[0]
    assert isinstance(entity, types.MessageEntityCustomEmoji)
    assert (entity.offset, entity.length, entity.document_id) == (15, 2, DOC)


def test_inline_result_refuses_an_unusable_document_id():
    with pytest.raises(ValueError):
        svc.build_inline_result(0, ALT)


def test_inline_query_key_round_trips():
    query = svc.inline_query_for(DOC, ALT)
    assert query == f"{svc.INLINE_QUERY_KEY}:{DOC}:{ALT}"
    assert svc.parse_inline_query_extra(query.split(":", 1)[1]) == (DOC, ALT)
    assert svc.parse_inline_query_extra("nope") == (None, "")
    assert svc.parse_inline_query_extra("0:" + ALT) == (None, "")
    assert svc.parse_inline_query_extra(str(DOC) + ":") == (None, "")


def test_the_helper_bot_builder_answers_with_the_entity():
    results = _run(emoji._premium_inline_builder(None, f"{DOC}:{ALT}"))
    assert len(results) == 1
    entity = results[0].send_message.entities[0]
    assert isinstance(entity, types.MessageEntityCustomEmoji)
    assert entity.document_id == DOC
    assert _run(emoji._premium_inline_builder(None, "garbage")) == []


def test_existing_glass_ui_results_are_untouched_by_the_sanitizer():
    ours = svc.build_inline_result(DOC, ALT)
    glass = inline_engine.make_result("Title", "Body")
    assert inline_engine._sanitize_results([ours, glass]) == [ours, glass]
    assert glass.send_message.entities is None


# ── end-to-end: the accepted path ───────────────────────────────────────────


def test_verified_path_reports_entity_attribution_and_eligibility(monkeypatch):
    text, entities = _result_payload()
    stored = _FakeMessage(text, entities, message_id=4242, via_bot_id=BOT_ID)
    client = _FakeSelfClient(results=[_FakeInlineResult(text, entities)], stored=stored)

    result = _send(client, monkeypatch, facts={"premium": False, "free": True})

    assert result["ok"] is True
    assert result["verified"] is True
    assert result["diagnosis"] == svc.STORED_ENTITY_VERIFIED_RENDER_UNVERIFIED
    assert result["send_path"] == "messages.sendInlineBotResult"
    assert result["destination_chat_id"] == OWNER
    assert result["message_id"] == 4242
    assert client.queries[0][0] == "nitro_selfbot"
    assert client.queries[0][1] == f"{svc.INLINE_QUERY_KEY}:{DOC}:{ALT}"
    assert client.queries[0][2] == OWNER
    readback = result["readback"]
    assert (readback["document_id"], readback["offset"], readback["length"]) == (DOC, 15, 2)
    assert readback["document_id_match"] is True
    assert readback["span_match"] is True
    assert readback["via_bot_id"] == BOT_ID
    assert readback["via_bot_match"] is True
    assert result["inline_result"]["entity_present"] is True
    eligibility = result["eligibility"]
    assert eligibility["owner_premium"] is False
    assert eligibility["document"]["free"] is True
    assert eligibility["glyph_source"] == "document_alt"
    assert client.reads == [(OWNER, 4242)]


def test_the_document_alt_wins_over_a_divergent_source_span(monkeypatch):
    text, entities = _result_payload()
    stored = _FakeMessage(text, entities, via_bot_id=BOT_ID)
    client = _FakeSelfClient(results=[_FakeInlineResult(text, entities)], stored=stored)

    result = _send(client, monkeypatch, source_glyph="🤖", facts={"alt": ALT})

    assert result["verified"] is True
    assert result["payload"]["glyph"] == ALT
    assert result["eligibility"]["glyph_source"] == "document_alt"


def test_document_lookup_failure_falls_back_to_the_source_span(monkeypatch):
    glyph = "🥳"
    text, entities = _result_payload(glyph)
    stored = _FakeMessage(text, entities, via_bot_id=BOT_ID)
    client = _FakeSelfClient(results=[_FakeInlineResult(text, entities)], stored=stored)

    result = _send(
        client,
        monkeypatch,
        source_glyph=glyph,
        facts={"docs_error": RuntimeError("boom")},
    )

    assert result["verified"] is True
    assert result["payload"]["glyph"] == glyph
    assert result["eligibility"]["glyph_source"] == "source_span"
    assert "boom" in result["eligibility"]["document_error"]


# ── end-to-end: every failure mode ──────────────────────────────────────────


def test_result_missing_the_entity_stops_before_the_send(monkeypatch):
    text, entities = _result_payload()
    result_without_entity = _FakeInlineResult(text, None)
    client = _FakeSelfClient(results=[result_without_entity], stored=None)

    result = _send(client, monkeypatch)

    assert result["ok"] is False
    assert result["diagnosis"] == svc.INLINE_RESULT_ENTITY_MISSING
    assert result["error"] == svc.ERROR_QUERY
    assert result_without_entity.clicks == 0


def test_inline_query_exception_is_reported_as_rejected(monkeypatch):
    client = _FakeSelfClient(query_error=RuntimeError("QUERY_ID_INVALID"))
    result = _send(client, monkeypatch)
    assert result["ok"] is False
    assert result["diagnosis"] == svc.INLINE_RESULT_REJECTED
    assert "QUERY_ID_INVALID" in result["detail"]


def test_zero_inline_results_is_reported_as_rejected(monkeypatch):
    client = _FakeSelfClient(results=[])
    result = _send(client, monkeypatch)
    assert result["diagnosis"] == svc.INLINE_RESULT_REJECTED
    assert result["readback"]["attempted"] is False


def test_send_exception_is_reported_as_send_failed(monkeypatch):
    text, entities = _result_payload()
    client = _FakeSelfClient(
        results=[_FakeInlineResult(text, entities, click_error=RuntimeError("PEER_ID_INVALID"))]
    )
    result = _send(client, monkeypatch)
    assert result["ok"] is False
    assert result["diagnosis"] == svc.INLINE_SEND_FAILED
    assert "PEER_ID_INVALID" in result["detail"]


def test_send_without_a_message_is_reported_as_send_failed(monkeypatch):
    text, entities = _result_payload()
    client = _FakeSelfClient(
        results=[_FakeInlineResult(text, entities, click_returns_none=True)]
    )
    result = _send(client, monkeypatch)
    assert result["diagnosis"] == svc.INLINE_SEND_FAILED


def test_entity_stripped_after_an_accepted_send_is_reported(monkeypatch):
    text, entities = _result_payload()
    client = _FakeSelfClient(
        results=[_FakeInlineResult(text, entities)],
        stored=_FakeMessage(text, [], via_bot_id=BOT_ID),
    )
    result = _send(client, monkeypatch)
    assert result["ok"] is True          # the send itself was accepted
    assert result["verified"] is False
    assert result["diagnosis"] == svc.STORED_ENTITY_STRIPPED
    assert result["readback"]["entity_present"] is False
    assert result["readback"]["span_match"] is True   # the text still matched


def test_stored_entity_with_a_different_document_id_is_a_mismatch(monkeypatch):
    text, entities = _result_payload()
    stored = _FakeMessage(
        text, [_entity(document_id=DOC + 1)], via_bot_id=BOT_ID
    )
    client = _FakeSelfClient(results=[_FakeInlineResult(text, entities)], stored=stored)
    result = _send(client, monkeypatch)
    assert result["diagnosis"] == svc.STORED_ENTITY_MISMATCH
    assert result["readback"]["document_id_match"] is False


def test_stored_entity_with_a_wrong_span_is_a_mismatch(monkeypatch):
    text, entities = _result_payload()
    stored = _FakeMessage(text, [_entity(offset=0, length=1)], via_bot_id=BOT_ID)
    client = _FakeSelfClient(results=[_FakeInlineResult(text, entities)], stored=stored)
    result = _send(client, monkeypatch)
    assert result["diagnosis"] == svc.STORED_ENTITY_MISMATCH
    assert result["readback"]["span_match"] is False


def test_missing_via_bot_id_is_reported_as_attribution_missing(monkeypatch):
    text, entities = _result_payload()
    stored = _FakeMessage(text, entities, via_bot_id=None)
    client = _FakeSelfClient(results=[_FakeInlineResult(text, entities)], stored=stored)
    result = _send(client, monkeypatch)
    assert result["diagnosis"] == svc.STORED_ATTRIBUTION_MISSING
    assert result["readback"]["via_bot_id"] is None
    assert result["readback"]["via_bot_match"] is False


def test_inconsistent_via_bot_id_is_reported_as_attribution_missing(monkeypatch):
    text, entities = _result_payload()
    stored = _FakeMessage(text, entities, via_bot_id=BOT_ID + 7)
    client = _FakeSelfClient(results=[_FakeInlineResult(text, entities)], stored=stored)
    result = _send(client, monkeypatch)
    assert result["diagnosis"] == svc.STORED_ATTRIBUTION_MISSING
    assert result["verified"] is False


def test_readback_failure_is_reported_separately_from_the_send(monkeypatch):
    text, entities = _result_payload()
    client = _FakeSelfClient(
        results=[_FakeInlineResult(text, entities)],
        read_error=RuntimeError("CHANNEL_INVALID"),
    )
    result = _send(client, monkeypatch)
    assert result["ok"] is True
    assert result["diagnosis"] == svc.READBACK_FAILED
    assert "CHANNEL_INVALID" in result["readback"]["error"]


def test_readback_returning_no_message_is_reported_as_readback_failure(monkeypatch):
    text, entities = _result_payload()
    client = _FakeSelfClient(results=[_FakeInlineResult(text, entities)], stored=None)
    result = _send(client, monkeypatch)
    assert result["ok"] is True
    assert result["diagnosis"] == svc.READBACK_FAILED


def test_unavailable_helper_bot_stops_before_any_query(monkeypatch):
    monkeypatch.setattr(helper_client, "is_available", lambda: False)
    inline_engine.set_helper_username("")
    inline_engine.set_helper_id(0)
    client = _FakeSelfClient(results=[])
    result = _send(client, monkeypatch)
    assert result["ok"] is False
    assert result["diagnosis"] == svc.INLINE_UNAVAILABLE
    assert result["error"] == svc.ERROR_HELPER
    assert client.queries == []


def test_non_saved_messages_destination_is_refused(monkeypatch):
    client = _FakeSelfClient(results=[])
    result = _send(client, monkeypatch, chat_id=OTHER_CHAT)
    assert result["ok"] is False
    assert result["diagnosis"] == svc.UNSUPPORTED_DESTINATION
    assert result["error"] == svc.ERROR_DESTINATION
    assert client.queries == []


def test_invalid_owner_is_refused(monkeypatch):
    client = _FakeSelfClient(results=[])
    result = _send(client, monkeypatch, owner_id=0)
    assert result["error"] == svc.ERROR_OWNER
    assert client.queries == []


def test_missing_document_id_is_refused_before_the_query(monkeypatch):
    client = _FakeSelfClient(results=[])
    result = _send(client, monkeypatch, document_id=None)
    assert result["diagnosis"] == svc.SOURCE_ENTITY_MISSING
    assert client.queries == []


def test_unresolvable_glyph_stops_the_flow(monkeypatch):
    client = _FakeSelfClient(results=[])
    result = _send(
        client,
        monkeypatch,
        source_glyph="",
        facts={"docs_error": RuntimeError("boom")},
    )
    assert result["diagnosis"] == svc.SOURCE_ENTITY_MISSING
    assert client.queries == []


def test_the_owner_premium_status_is_observed_not_assumed(monkeypatch):
    text, entities = _result_payload()
    stored = _FakeMessage(text, entities, via_bot_id=BOT_ID)
    client = _FakeSelfClient(results=[_FakeInlineResult(text, entities)], stored=stored)
    honest = _send(client, monkeypatch, facts={"premium": False})
    assert honest["eligibility"]["owner_premium"] is False
    unreadable = _send(client, monkeypatch, facts={"me_error": RuntimeError("nope")})
    assert unreadable["eligibility"]["owner_premium"] is None
    assert "nope" in unreadable["eligibility"]["owner_premium_error"]


# ── no fallback, ever ───────────────────────────────────────────────────────


def test_no_unicode_fallback_is_ever_sent(monkeypatch):
    text, entities = _result_payload()
    client = _FakeSelfClient(
        results=[_FakeInlineResult(text, entities)],
        stored=_FakeMessage(text, [], via_bot_id=BOT_ID),   # stripped server-side
    )
    result = _send(client, monkeypatch)
    assert result["diagnosis"] == svc.STORED_ENTITY_STRIPPED
    assert client.sent_messages == []            # never a direct/plain send
    assert len(client.queries) == 1              # exactly ONE attempt
    assert result["payload"]["entity"]["type"] == "MessageEntityCustomEmoji"
    assert result["payload"]["text"] == PREFIX + ALT


def test_failure_summaries_never_claim_a_render():
    stripped = {
        "diagnosis": svc.STORED_ENTITY_STRIPPED,
        "error": None,
        "readback": {"ok": True, "entity_present": False},
    }
    summary = svc.outcome_summary(stripped)
    assert "WITHOUT the custom-emoji entity" in summary
    assert "rendered" not in summary
    verified = {
        "diagnosis": svc.STORED_ENTITY_VERIFIED_RENDER_UNVERIFIED,
        "error": None,
        "readback": {},
    }
    summary = svc.outcome_summary(verified)
    assert "cannot prove" in summary
    assert "verified render" not in summary
    assert svc.outcome_summary({}) == "outcome None."


# ── UI wiring ───────────────────────────────────────────────────────────────


def test_the_production_action_and_builder_are_registered():
    emoji.register(client=None, owner_id=OWNER)
    assert get_action(emoji._INLINE_ACTION) is not None
    assert inline_engine.get_inline_builder(svc.INLINE_QUERY_KEY) is not None


def test_the_main_panel_makes_the_production_entry_primary(monkeypatch):
    from backend.db import client as db_client

    db_client._fallback["emoji_library"] = []
    _title, _body, buttons = _run(emoji._emoji_panel_handler(None, ""))
    datas = [
        btn.data.decode() if isinstance(btn.data, bytes) else str(btn.data)
        for row in buttons
        for btn in (row if isinstance(row, list) else [row])
    ]
    assert f"action:{emoji._INLINE_ACTION}" in datas
    assert datas.index(f"action:{emoji._INLINE_ACTION}") == 0
    assert f"action:{emoji._PROBE_ACTION}" in datas


def test_the_action_sends_the_selection_prompt_and_arms_reply_mode(monkeypatch):
    client = _FakeSelfClient()
    inline_engine.set_self_client(client)
    from backend.helper import input_state

    input_state.clear_all()
    title, body, _buttons = _run(emoji._premium_inline_action(None, "", OWNER))
    assert title == "Send Premium Emoji"
    assert client.sent_messages == [("me", emoji._INLINE_PROMPT)]
    pending = input_state.get_pending(OWNER)
    assert pending is not None
    assert pending["panel_id"] == emoji._INLINE_ACTION
    assert pending["extra"] == str(SELECTION)
    input_state.clear_all()


def _reply_targeting(target_id: int, text: str, entities=None, message_id: int = 910):
    reply = _FakeMessage(text, entities, message_id=message_id)
    reply.reply_to = SimpleNamespace(reply_to_msg_id=target_id, reply_to_peer_id=None)
    return reply


def _run_reply(monkeypatch, *, extra=str(SELECTION)):
    captured: dict[str, Any] = {}

    async def _fake_edit(inline_chat_id, inline_msg_id, title_, body, buttons):
        captured["title"] = title_
        captured["body"] = body
        captured["buttons"] = buttons

    monkeypatch.setattr(emoji, "_edit_inline", _fake_edit)
    _run(
        emoji._premium_inline_reply_handler(
            "", OWNER, 910, 111, 222, extra=extra
        )
    )
    return captured


def test_reply_handler_rejects_a_message_that_is_not_a_reply(monkeypatch):
    client = _FakeSelfClient(reply=_FakeMessage("hi", [], message_id=910))
    client.reply.reply_to = None
    inline_engine.set_self_client(client)
    captured = _run_reply(monkeypatch)
    assert "was not a reply" in captured["body"]
    assert client.queries == []


def test_reply_handler_accepts_only_the_exact_selection_message(monkeypatch):
    reply = _reply_targeting(SELECTION + 1, ALT, [_entity()])
    client = _FakeSelfClient(reply=reply)
    inline_engine.set_self_client(client)
    captured = _run_reply(monkeypatch)
    assert "targets message" in captured["body"]
    assert client.queries == []


def test_reply_handler_rejects_a_plain_unicode_reply(monkeypatch):
    reply = _reply_targeting(SELECTION, ALT, [])
    client = _FakeSelfClient(reply=reply)
    inline_engine.set_self_client(client)
    captured = _run_reply(monkeypatch)
    assert svc.SOURCE_ENTITY_MISSING in captured["body"]
    assert client.queries == []


def test_reply_handler_runs_the_full_flow_and_reports_it(monkeypatch):
    reply = _reply_targeting(SELECTION, ALT, [_entity()])
    text, entities = _result_payload()
    stored = _FakeMessage(text, entities, via_bot_id=BOT_ID)
    client = _FakeSelfClient(reply=reply, results=[_FakeInlineResult(text, entities)], stored=stored)
    inline_engine.set_self_client(client)
    _patch_facts(monkeypatch)

    captured = _run_reply(monkeypatch)

    assert captured["title"] == "Send Premium Emoji ✓"
    body = captured["body"]
    assert svc.STORED_ENTITY_VERIFIED_RENDER_UNVERIFIED in body
    assert "messages.sendInlineBotResult" in body
    assert "matches the helper bot" in body
    assert "Owner Premium: no" in body
    assert "non-Premium allowed" in body


def test_reply_handler_reports_a_stripped_entity_honestly(monkeypatch):
    reply = _reply_targeting(SELECTION, ALT, [_entity()])
    text, entities = _result_payload()
    client = _FakeSelfClient(
        reply=reply,
        results=[_FakeInlineResult(text, entities)],
        stored=_FakeMessage(text, [], via_bot_id=BOT_ID),
    )
    inline_engine.set_self_client(client)
    _patch_facts(monkeypatch)

    captured = _run_reply(monkeypatch)

    assert captured["title"] == "Send Premium Emoji"
    assert svc.STORED_ENTITY_STRIPPED in captured["body"]
    assert "NO custom-emoji entity" in captured["body"]


# ── the live dispatch boundary (pending-input listener → reply handler) ─────
#
# The pending-input machinery is the production flow's ONLY update path, so it
# is pinned end to end: arm it through the real action, register the REAL
# listener, deliver the owner's outgoing Saved Messages reply, and assert the
# selection id reached the handler AND the first ``[PREMIUM_INLINE]`` trace was
# emitted. That first trace is the boundary whose absence in a live excerpt
# proves the reply never dispatched: every dispatched reply emits exactly one
# ``SOURCE_ENTITY_*`` line before anything else in the service.


def test_the_real_pending_input_listener_reaches_the_production_flow(monkeypatch, caplog):
    import logging

    from backend.helper import inline_sender, input_state

    reply = _reply_targeting(SELECTION, ALT, [_entity()])
    text, entities = _result_payload()
    stored = _FakeMessage(text, entities, via_bot_id=BOT_ID)
    client = _FakeSelfClient(
        reply=reply, results=[_FakeInlineResult(text, entities)], stored=stored
    )
    inline_engine.set_self_client(client)
    _patch_facts(monkeypatch)

    captured_edits: dict[str, Any] = {}

    async def _fake_edit(inline_chat_id, inline_msg_id, title_, body, buttons):
        captured_edits["title"] = title_
        captured_edits["body"] = body

    monkeypatch.setattr(emoji, "_edit_inline", _fake_edit)

    input_state.clear_all()
    _run(emoji._premium_inline_action(SimpleNamespace(message_id=555), "", 111))
    pending = input_state.get_pending(OWNER)
    assert pending is not None
    assert pending["extra"] == str(SELECTION)

    captured: dict[str, Any] = {}

    def _on(*_args, **_kwargs):
        def _decorator(func):
            captured["listener"] = func
            return func

        return _decorator

    inline_sender.register_input_listener(SimpleNamespace(on=_on), OWNER)
    listener = captured["listener"]

    caplog.set_level(
        logging.INFO, logger="backend.services.premium_emoji_inline_service"
    )
    event = SimpleNamespace(
        raw_text=ALT,
        chat_id=OWNER,          # the Saved Messages chat of the selection message
        sender_id=OWNER,
        message=SimpleNamespace(id=910),
    )
    _run(listener(event))

    assert input_state.get_pending(OWNER) is None      # the pending input was consumed
    assert client.reads[0] == (OWNER, 910)             # the EXACT reply was read back
    traces = [
        record.getMessage()
        for record in caplog.records
        if record.name.endswith("premium_emoji_inline_service")
    ]
    assert traces, "a dispatched reply must emit at least one [PREMIUM_INLINE] trace"
    assert traces[0].startswith("[PREMIUM_INLINE] SOURCE_ENTITY_VALIDATED")
    assert captured_edits["title"] == "Send Premium Emoji ✓"


def test_the_production_listener_ignores_a_reply_in_another_chat(monkeypatch, caplog):
    import logging

    from backend.helper import inline_sender, input_state

    client = _FakeSelfClient(
        reply=_reply_targeting(SELECTION, ALT, [_entity()]),
        results=[_FakeInlineResult(*_result_payload())],
    )
    inline_engine.set_self_client(client)
    _patch_facts(monkeypatch)

    async def _fake_edit(inline_chat_id, inline_msg_id, title_, body, buttons):
        raise AssertionError("the reply handler must not run for another chat")

    monkeypatch.setattr(emoji, "_edit_inline", _fake_edit)

    input_state.clear_all()
    _run(emoji._premium_inline_action(SimpleNamespace(message_id=555), "", 111))

    captured: dict[str, Any] = {}

    def _on(*_args, **_kwargs):
        def _decorator(func):
            captured["listener"] = func
            return func

        return _decorator

    inline_sender.register_input_listener(SimpleNamespace(on=_on), OWNER)
    caplog.set_level(
        logging.INFO, logger="backend.services.premium_emoji_inline_service"
    )

    event = SimpleNamespace(
        raw_text=ALT,
        chat_id=OTHER_CHAT,
        sender_id=OWNER,
        message=SimpleNamespace(id=910),
    )
    _run(captured["listener"](event))

    assert input_state.get_pending(OWNER) is not None   # still armed, nothing consumed
    assert client.reads == []                           # the reply was never read
    assert client.queries == []                         # no inline query ran
    assert [
        record
        for record in caplog.records
        if record.name.endswith("premium_emoji_inline_service")
    ] == []                                             # and no [PREMIUM_INLINE] trace
    input_state.clear_all()


# ── backward compatibility of the touched shared code ───────────────────────


def test_inline_engine_trigger_keeps_its_exact_contract(monkeypatch):
    text, entities = _result_payload()
    result = _FakeInlineResult(text, entities, message_id=777)
    client = _FakeSelfClient(results=[result])
    success, chat_id, msg_id, inline_id = _run(
        inline_engine.trigger(client, OWNER, "emoji:")
    )
    assert (success, chat_id, msg_id, inline_id) == (True, OWNER, 777, "")
    empty = _FakeSelfClient(results=[])
    assert _run(inline_engine.trigger(empty, OWNER, "emoji:")) == (False, OWNER, 0, "")


def test_inline_engine_reports_the_same_unavailable_reasons():
    inline_engine.set_helper_username("")
    inline_engine.set_helper_id(0)
    assert "helper id is 0" in inline_engine.inline_unavailable_reason()
    inline_engine.set_helper_id(BOT_ID)
    assert "no public username" in inline_engine.inline_unavailable_reason()
    inline_engine.set_helper_username("nitro_selfbot")
    assert inline_engine.inline_unavailable_reason() == ""


def test_serialize_user_adds_premium_without_dropping_keys():
    user = SimpleNamespace(
        id=5, first_name="A", last_name="B", username="ab", phone=None,
        about=None, bot=False, deleted=False, premium=True,
    )
    data = serialize_user(user)
    assert data["premium"] is True
    assert set(data) >= {
        "id", "first_name", "last_name", "full_name", "username", "phone",
        "about", "is_bot", "is_deleted",
    }


def test_serialize_document_adds_free_and_text_color_additively():
    from telethon.tl.types import (
        Document,
        DocumentAttributeCustomEmoji,
        InputStickerSetEmpty,
    )

    from backend.telegram_api.custom_emoji import _serialize_document

    document = Document(
        id=DOC,
        access_hash=1,
        file_reference=b"",
        date=None,
        mime_type="application/x-tgsticker",
        size=1,
        dc_id=2,
        attributes=[
            DocumentAttributeCustomEmoji(
                alt=ALT, stickerset=InputStickerSetEmpty(), free=True, text_color=None
            )
        ],
    )
    entry = _serialize_document(document)
    assert entry["document_id"] == DOC
    assert entry["alt"] == ALT
    assert entry["free"] is True
    assert entry["text_color"] is None
    assert "set" in entry
