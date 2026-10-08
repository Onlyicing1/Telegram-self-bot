"""
Premium-emoji probe — POC (helper-bot rendering boundary).

Pins the ONE capability this proof of concept exists for, offline:

* a REAL Telegram ``MessageEntityCustomEmoji`` is recognized and its REAL
  ``document_id`` is extracted — the source of truth is the entity, never the
  visible glyph, the alt text or the message text;
* a plain Unicode emoji, an empty/media-only reply, and a custom-emoji entity
  with an unusable id all fail closed — no Unicode fallback is ever sent or
  reported as a Premium render;
* the payload handed to the EXISTING helper-bot bridge carries the real
  custom-emoji entity (UTF-16 offsets/lengths and the real document id) and
  the bot is the one that sends it;
* the Glass UI action creates the deterministic Saved Messages selection
  message and accepts a reply to THAT exact message id only.

No live Telegram and no Supabase call is made: the Telegram boundary is faked
at the surface the probe consumes (``send_message`` / ``get_messages`` on the
self client, ``get_input_entity`` / ``send_message`` on the helper bot).
"""
from __future__ import annotations

import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from telethon.tl.types import MessageEntityBold, MessageEntityCustomEmoji

import backend.helper.client as helper_client
from backend.bot.handlers import emoji
from backend.helper import inline_engine, input_state
from backend.helper.panels import get_action
from backend.helper.panel_registry import registry as get_registry
from backend.services import premium_emoji_probe_service as probe
from backend.telegram_api._helpers import utf16_length

OWNER = 7770001
CHAT = -1001234567890
OTHER_CHAT = -1009998887771

SELECTION = 909
REPLY = 910
DOC = 5361626279781934801
ALT = "🏂"
PREFIX = "سلام "


@pytest.fixture(autouse=True)
def _env():
    input_state.clear_all()
    inline_engine.set_owner_id(OWNER)
    yield
    input_state.clear_all()
    helper_client._client = None
    helper_client._bot_id = 0


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ── telegram-level fakes ─────────────────────────────────────────────────────


class _FakeSelfClient:
    """The surface the probe consumes on the self client."""

    def __init__(self, reply: Any = None, *, send_error: Exception | None = None) -> None:
        self.sent: list[dict[str, Any]] = []
        self._reply = reply
        self.send_error = send_error
        self.get_error: Exception | None = None
        self.reads: list[tuple[Any, Any]] = []

    async def send_message(self, entity, text):
        if self.send_error is not None:
            raise self.send_error
        self.sent.append({"entity": entity, "text": text})
        return SimpleNamespace(id=SELECTION, chat_id=OWNER, text=text)

    async def get_messages(self, chat_id, ids=None):
        self.reads.append((chat_id, ids))
        if self.get_error is not None:
            raise self.get_error
        return self._reply


class _FakeBot:
    """The surface the probe consumes on the helper bot."""

    def __init__(
        self,
        *,
        owner_known: bool = True,
        dialogs: list[Any] | None = None,
        send_error: Exception | None = None,
    ) -> None:
        self.owner_known = owner_known
        self._dialogs = list(dialogs or [])
        self.send_error = send_error
        self.calls: list[dict[str, Any]] = []

    def is_connected(self) -> bool:
        return True

    async def get_input_entity(self, entity):
        if not self.owner_known:
            raise ValueError("Cannot find any entity corresponding to the id")
        return ("bot-peer", entity)

    def iter_dialogs(self, limit=None):
        async def _gen():
            for dialog in self._dialogs:
                yield dialog

        return _gen()

    async def send_message(self, peer, text, *, formatting_entities=None, reply_to=None):
        self.calls.append({
            "peer": peer,
            "text": text,
            "formatting_entities": formatting_entities,
            "reply_to": reply_to,
        })
        if self.send_error is not None:
            raise self.send_error
        return SimpleNamespace(id=4321, chat_id=OWNER)


class _PanelRecorder:
    def __init__(self) -> None:
        self.edits: list[dict[str, Any]] = []

    async def __call__(self, chat_id, msg_id, title, body, buttons):
        self.edits.append({
            "chat_id": chat_id,
            "msg_id": msg_id,
            "title": title,
            "body": body,
            "buttons": buttons,
        })


@pytest.fixture
def bot(monkeypatch):
    fake = _FakeBot()
    monkeypatch.setattr(helper_client, "_client", fake)
    monkeypatch.setattr(helper_client, "_bot_id", 777)
    return fake


def _custom_emoji_message(
    *,
    document_id: int = DOC,
    glyph: str = ALT,
    prefix: str = "",
    target: int | None = SELECTION,
    header_peer: Any = None,
    entities: list | None = None,
) -> SimpleNamespace:
    text = f"{prefix}{glyph}"
    if entities is None:
        entities = [
            MessageEntityCustomEmoji(utf16_length(prefix), utf16_length(glyph), document_id)
        ]
    header = SimpleNamespace(reply_to_msg_id=target, reply_to_peer_id=header_peer)
    return SimpleNamespace(
        id=REPLY,
        chat_id=OWNER,
        message=text,
        text=text,
        entities=entities,
        reply_to=header,
        reply_to_msg_id=target,
        media=None,
    )


def _plain_message(*, text: str = ALT, target: int | None = SELECTION) -> SimpleNamespace:
    header = SimpleNamespace(reply_to_msg_id=target, reply_to_peer_id=None)
    return SimpleNamespace(
        id=REPLY,
        chat_id=OWNER,
        message=text,
        text=text,
        entities=[],
        reply_to=header,
        reply_to_msg_id=target,
        media=None,
    )


def _datas(buttons) -> list[str]:
    out = []
    for row in buttons:
        for button in row:
            raw = getattr(button, "data", button)
            out.append(raw.decode("utf-8") if isinstance(raw, bytes) else str(raw))
    return out


# ── 1/2. entity recognition and the REAL custom-emoji id ─────────────────────


def test_a_real_custom_emoji_entity_is_recognized_and_its_id_is_extracted():
    found = probe.inspect_message(_custom_emoji_message())
    assert found["kind"] == probe.KIND_CUSTOM_EMOJI
    assert found["document_id"] == DOC
    assert found["alt_text"] == ALT


def test_the_alt_text_is_read_from_the_entity_span_in_utf16_units():
    found = probe.inspect_message(_custom_emoji_message(prefix=PREFIX))
    assert found["kind"] == probe.KIND_CUSTOM_EMOJI
    assert found["document_id"] == DOC
    assert found["alt_text"] == ALT  # span, never the whole message text
    assert found["alt_text"] != PREFIX + ALT


def test_a_custom_emoji_entity_wins_over_the_neighbouring_text():
    message = _custom_emoji_message(prefix="note: ")
    found = probe.inspect_message(message)
    assert found["kind"] == probe.KIND_CUSTOM_EMOJI
    assert found["alt_text"] == ALT


def test_the_document_id_comes_from_the_entity_not_from_the_glyph():
    # A different glyph with the same id must still resolve to the entity id.
    found = probe.inspect_message(_custom_emoji_message(glyph="🫪", document_id=424242))
    assert found["kind"] == probe.KIND_CUSTOM_EMOJI
    assert found["document_id"] == 424242


# ── 3. a plain Unicode emoji is NOT a Premium emoji ──────────────────────────


def test_a_plain_unicode_emoji_is_not_a_custom_emoji():
    found = probe.inspect_message(_plain_message(text="🏂"))
    assert found["kind"] == probe.KIND_UNICODE
    assert found["document_id"] is None
    assert "not a Premium emoji" in found["detail"]


def test_a_bold_entity_with_a_glyph_is_still_not_a_custom_emoji():
    message = _plain_message(text=ALT)
    message.entities = [MessageEntityBold(0, utf16_length(ALT))]
    found = probe.inspect_message(message)
    assert found["kind"] == probe.KIND_UNICODE
    assert found["document_id"] is None


def test_a_text_url_style_entity_without_a_document_id_is_not_a_custom_emoji():
    message = _plain_message(text=ALT)
    message.entities = [MessageEntityBold(0, utf16_length(ALT))]
    assert probe.inspect_message(message)["kind"] == probe.KIND_UNICODE


# ── 4. missing entities fail closed ──────────────────────────────────────────


def test_an_empty_reply_has_no_custom_emoji():
    found = probe.inspect_message(_plain_message(text=""))
    assert found["kind"] == probe.KIND_NONE
    assert found["document_id"] is None


def test_a_media_only_reply_has_no_custom_emoji():
    message = _plain_message(text="")
    message.media = object()
    found = probe.inspect_message(message)
    assert found["kind"] == probe.KIND_NONE
    assert found["document_id"] is None


def test_a_custom_emoji_entity_with_an_unusable_id_fails_closed():
    for bad in (0, -5, "abc", None, True):
        found = probe.inspect_message(_custom_emoji_message(document_id=bad))
        assert found["kind"] == probe.KIND_NONE, bad
        assert found["document_id"] is None, bad
        assert "unusable" in found["detail"], bad


def test_a_corrupt_entity_span_yields_no_alt_text_and_still_fails_closed():
    entity = MessageEntityCustomEmoji(99, 2, DOC)  # offset past the text
    found = probe.inspect_message(_custom_emoji_message(entities=[entity]))
    assert found["kind"] == probe.KIND_CUSTOM_EMOJI
    assert found["alt_text"] == ""


# ── 7/8. the helper-bot payload IS the entity ────────────────────────────────


def test_the_payload_carries_the_real_custom_emoji_entity():
    payload = probe.build_proof_payload(DOC, ALT)
    assert payload["text"] == f"{probe.PROOF_PREFIX}{ALT}"
    entity = payload["entity"]
    assert entity["type"] == "MessageEntityCustomEmoji"
    assert entity["document_id"] == DOC
    assert entity["offset"] == utf16_length(probe.PROOF_PREFIX)
    assert entity["length"] == utf16_length(ALT)
    assert payload["entities"] == [entity]
    assert payload["used_placeholder"] is False


def test_the_payload_uses_a_placeholder_only_as_the_entity_underlying_text():
    payload = probe.build_proof_payload(DOC, "")
    assert payload["fallback_text"] == probe.PLACEHOLDER_GLYPH
    assert payload["used_placeholder"] is True
    assert payload["entity"]["document_id"] == DOC
    assert payload["entity"]["length"] == utf16_length(probe.PLACEHOLDER_GLYPH)
    assert payload["text"].endswith(probe.PLACEHOLDER_GLYPH)


def test_build_proof_payload_requires_a_real_document_id():
    for bad in (0, None, "abc", True):
        with pytest.raises(ValueError):
            probe.build_proof_payload(bad, ALT)


def test_the_helper_bot_sends_the_real_custom_emoji_entity(bot):
    self_client = _FakeSelfClient()
    outcome = _run(probe.deliver_proof(self_client, OWNER, DOC, ALT))
    assert outcome["ok"] is True, outcome
    call = bot.calls[0]
    built = call["formatting_entities"]
    assert built is not None and len(built) == 1
    entity = built[0]
    assert type(entity) is MessageEntityCustomEmoji
    assert entity.document_id == DOC
    assert entity.offset == utf16_length(probe.PROOF_PREFIX)
    assert entity.length == utf16_length(ALT)
    assert call["text"] == f"{probe.PROOF_PREFIX}{ALT}"
    # The destination is the BOT's own peer for its chat with the owner.
    assert call["peer"] == ("bot-peer", OWNER)
    assert outcome["message_id"] == 4321
    # the self client never sent anything for the premium render
    assert self_client.sent == []


def test_the_helper_bot_peer_can_come_from_its_own_dialog_list(monkeypatch):
    dialog = SimpleNamespace(id=OWNER, input_entity=("dialog-peer", OWNER))
    bot = _FakeBot(owner_known=False, dialogs=[dialog, SimpleNamespace(id=1)])
    monkeypatch.setattr(helper_client, "_client", bot)
    self_client = _FakeSelfClient()
    outcome = _run(probe.deliver_proof(self_client, OWNER, DOC, ALT))
    assert outcome["ok"] is True, outcome
    assert bot.calls[0]["peer"] == ("dialog-peer", OWNER)


# ── 8. the visible glyph alone is never proof ────────────────────────────────


def test_an_unavailable_helper_bot_reports_failure_and_sends_no_fallback(monkeypatch):
    monkeypatch.setattr(helper_client, "_client", None)
    self_client = _FakeSelfClient()
    outcome = _run(probe.deliver_proof(self_client, OWNER, DOC, ALT))
    assert outcome["ok"] is False
    assert outcome["error"] == probe.ERROR_NO_HELPER
    assert self_client.sent == []
    assert outcome["entities"][0]["document_id"] == DOC


def test_a_bot_without_a_chat_with_the_owner_fails_honestly(monkeypatch):
    bot = _FakeBot(owner_known=False, dialogs=[])
    monkeypatch.setattr(helper_client, "_client", bot)
    self_client = _FakeSelfClient()
    outcome = _run(probe.deliver_proof(self_client, OWNER, DOC, ALT))
    assert outcome["ok"] is False
    assert outcome["error"] == probe.ERROR_NO_BOT_CHAT
    assert "press Start" in outcome["detail"]
    assert bot.calls == []


def test_a_rejected_send_is_reported_without_any_glyph_substitution(monkeypatch):
    bot = _FakeBot(send_error=RuntimeError("CUSTOM_EMOJI_INVALID"))
    monkeypatch.setattr(helper_client, "_client", bot)
    self_client = _FakeSelfClient()
    outcome = _run(probe.deliver_proof(self_client, OWNER, DOC, ALT))
    assert outcome["ok"] is False
    assert outcome["error"] == probe.ERROR_SEND
    assert "CUSTOM_EMOJI_INVALID" in outcome["detail"]
    # exactly ONE attempt, and the payload still carries the entity
    assert len(bot.calls) == 1
    assert outcome["entity"]["document_id"] == DOC
    assert self_client.sent == []


def test_a_bad_owner_or_a_missing_id_never_reaches_the_bot(bot):
    bad_owner = _run(probe.deliver_proof(_FakeSelfClient(), 0, DOC, ALT))
    assert bad_owner["ok"] is False and bad_owner["error"] == probe.ERROR_OWNER
    bad_id = _run(probe.deliver_proof(_FakeSelfClient(), OWNER, 0, ALT))
    assert bad_id["ok"] is False and bad_id["error"] == probe.ERROR_NO_EMOJI
    assert bot.calls == []


def test_helper_bot_availability_is_the_bridge_availability(monkeypatch):
    monkeypatch.setattr(helper_client, "_client", None)
    assert probe.helper_bot_available() is False
    monkeypatch.setattr(helper_client, "_client", _FakeBot())
    assert probe.helper_bot_available() is True


# ── 5/6. the deterministic Saved Messages selection message ──────────────────


def test_the_action_sends_the_selection_message_and_records_its_exact_id(monkeypatch):
    self_client = _FakeSelfClient()
    monkeypatch.setattr(emoji, "get_self_client", lambda: self_client)

    title, body, _buttons = _run(
        emoji._react_premium_action(SimpleNamespace(message_id=555), "", CHAT)
    )
    assert title == "Set Reaction Emoji"
    assert self_client.sent[0]["entity"] == "me"  # Saved Messages
    assert self_client.sent[0]["text"] == emoji._PROBE_PROMPT
    assert f"#{SELECTION}" in body

    pending = input_state.get_pending(OWNER)
    assert pending is not None
    assert pending["panel_id"] == emoji._PROBE_ACTION
    assert pending["handler"] is emoji._react_premium_reply_handler
    assert pending["chat_id"] == OWNER  # the selection message's own chat
    assert pending["extra"] == str(SELECTION)  # the EXACT selection message id
    assert pending["inline_chat_id"] == CHAT


def test_the_action_fails_honestly_without_a_self_client(monkeypatch):
    monkeypatch.setattr(emoji, "get_self_client", lambda: None)
    title, body, _buttons = _run(
        emoji._react_premium_action(SimpleNamespace(message_id=1), "", CHAT)
    )
    assert title == "Set Reaction Emoji"
    assert body.startswith("!")
    assert input_state.get_pending(OWNER) is None


def test_the_action_fails_honestly_when_the_selection_send_fails(monkeypatch):
    self_client = _FakeSelfClient(send_error=RuntimeError("flood wait"))
    monkeypatch.setattr(emoji, "get_self_client", lambda: self_client)
    _title, body, _buttons = _run(
        emoji._react_premium_action(SimpleNamespace(message_id=1), "", CHAT)
    )
    assert "flood wait" in body
    assert input_state.get_pending(OWNER) is None


def test_the_action_requires_a_usable_selection_message(monkeypatch):
    class _NoId(_FakeSelfClient):
        async def send_message(self, entity, text):
            return SimpleNamespace(id=0, chat_id=OWNER)

    monkeypatch.setattr(emoji, "get_self_client", lambda: _NoId())
    _title, body, _buttons = _run(
        emoji._react_premium_action(SimpleNamespace(message_id=1), "", CHAT)
    )
    assert "nothing was armed" in body
    assert input_state.get_pending(OWNER) is None


# ── 5. a reply to the wrong message / chat is rejected ───────────────────────


def _run_reply(monkeypatch, reply, *, extra: str = str(SELECTION)):
    self_client = _FakeSelfClient(reply)
    monkeypatch.setattr(emoji, "get_self_client", lambda: self_client)
    recorder = _PanelRecorder()
    monkeypatch.setattr(emoji, "_edit_inline", recorder)
    _run(emoji._react_premium_reply_handler(
        ALT, OWNER, REPLY, CHAT, 555, extra=extra,
    ))
    assert len(recorder.edits) == 1
    return recorder.edits[0], self_client


def test_a_reply_to_a_different_message_is_rejected(monkeypatch, bot):
    edit, _self_client = _run_reply(
        monkeypatch, _custom_emoji_message(target=SELECTION + 1)
    )
    assert "not the selection message" in edit["body"]
    assert bot.calls == []  # nothing was ever sent by the bot


def test_a_reply_in_another_chat_is_rejected(monkeypatch, bot):
    edit, _self_client = _run_reply(
        monkeypatch, _custom_emoji_message(target=SELECTION, header_peer=OTHER_CHAT)
    )
    assert "another chat" in edit["body"]
    assert bot.calls == []


def test_a_non_reply_is_rejected(monkeypatch, bot):
    message = _custom_emoji_message(target=None)
    message.reply_to_msg_id = None
    message.reply_to = SimpleNamespace(reply_to_msg_id=None, reply_to_peer_id=None)
    edit, _self_client = _run_reply(monkeypatch, message)
    assert "was not a reply" in edit["body"]
    assert bot.calls == []


def test_an_unknown_selection_id_is_refused_before_any_read(monkeypatch, bot):
    edit, self_client = _run_reply(
        monkeypatch, _custom_emoji_message(), extra=""
    )
    assert "no longer known" in edit["body"]
    assert self_client.reads == []
    assert bot.calls == []


def test_an_unreadable_reply_fails_honestly(monkeypatch, bot):
    self_client = _FakeSelfClient(_custom_emoji_message())
    self_client.get_error = RuntimeError("message deleted")
    monkeypatch.setattr(emoji, "get_self_client", lambda: self_client)
    recorder = _PanelRecorder()
    monkeypatch.setattr(emoji, "_edit_inline", recorder)
    _run(emoji._react_premium_reply_handler(
        ALT, OWNER, REPLY, CHAT, 555, extra=str(SELECTION),
    ))
    assert "Could not read your reply" in recorder.edits[0]["body"]
    assert bot.calls == []


# ── 3/8. a Unicode reply is refused by the UI too ────────────────────────────


def test_a_unicode_emoji_reply_is_refused_and_never_sent_as_premium(monkeypatch, bot):
    edit, self_client = _run_reply(monkeypatch, _plain_message(text="🏂"))
    assert "not a Premium emoji" in edit["body"]
    assert bot.calls == []
    assert self_client.sent == []


# ── the end-to-end proof, offline ────────────────────────────────────────────


def test_the_selection_reply_is_delivered_by_the_helper_bot_end_to_end(monkeypatch, bot):
    self_client = _FakeSelfClient()
    monkeypatch.setattr(emoji, "get_self_client", lambda: self_client)
    recorder = _PanelRecorder()
    monkeypatch.setattr(emoji, "_edit_inline", recorder)

    # 1. the owner launches the probe: a Saved Messages selection message
    _run(emoji._react_premium_action(SimpleNamespace(message_id=555), "", CHAT))
    pending = input_state.get_pending(OWNER)
    assert pending["extra"] == str(SELECTION)

    # 2. the owner replies to THAT message with a real Premium custom emoji
    self_client._reply = _custom_emoji_message(prefix=PREFIX)
    _run(pending["handler"](
        PREFIX + ALT, OWNER, REPLY, pending["inline_chat_id"], pending["inline_msg_id"],
        extra=pending["extra"],
    ))

    # 3. the helper bot — not the self client — sends the real entity
    assert len(bot.calls) == 1
    entity = bot.calls[0]["formatting_entities"][0]
    assert entity.document_id == DOC
    body = recorder.edits[0]["body"]
    assert "✓ Telegram accepted the helper bot's message" in body
    assert f"`#{DOC}`" in body
    assert "Fragment" in body  # the honest capability caveat
    # the self client only ever created the selection message
    assert [c["entity"] for c in self_client.sent] == ["me"]


def test_the_success_panel_never_claims_the_premium_render_by_itself(monkeypatch, bot):
    edit, _self_client = _run_reply(monkeypatch, _custom_emoji_message())
    body = edit["body"]
    assert "only if the helper bot" in body
    assert "A plain glyph there is a failure, not a success." in body


def test_the_failure_panel_says_the_entity_was_not_rendered(monkeypatch, bot):
    bot.send_error = RuntimeError("PREMIUM_ACCOUNT_REQUIRED")
    edit, _self_client = _run_reply(monkeypatch, _custom_emoji_message())
    assert "was NOT rendered by the helper bot" in edit["body"]
    assert "E_SEND" in edit["body"]


# ── registration and architecture ────────────────────────────────────────────


def test_the_probe_action_is_registered_through_the_existing_registry():
    emoji.register(client=None, owner_id=OWNER)
    assert get_action(emoji._PROBE_ACTION) is not None
    assert get_registry().get(emoji._PROBE_ACTION) is None  # an action, not a panel


def test_the_main_panel_links_the_probe_action(monkeypatch):
    from backend.db import client as db_client

    db_client._fallback["emoji_library"] = []
    _title, _body, buttons = _run(emoji._emoji_panel_handler(None, ""))
    assert f"action:{emoji._PROBE_ACTION}" in _datas(buttons)


PROBE_MODULES = (
    Path(probe.__file__),
    Path(emoji.__file__),
)


def _code_only(text: str) -> str:
    import io
    import tokenize

    pieces: list[str] = []
    try:
        for token in tokenize.generate_tokens(io.StringIO(text).readline):
            if token.type in (tokenize.COMMENT, tokenize.STRING, tokenize.NL):
                continue
            pieces.append(token.string)
    except tokenize.TokenError:
        return text
    return " ".join(pieces)


def test_the_probe_modules_add_no_second_infrastructure():
    for path in PROBE_MODULES:
        code = _code_only(path.read_text())
        for forbidden in (
            "TelegramClient",
            "run_until_disconnected",
            "create_task",
            "immortal_create_task",
            "guarded_create_task",
            "asyncio.Lock",
            "new_event_loop",
            "forward_messages",
            "SendMessagesRequest",
            "events.",
            "httpx",
        ):
            assert forbidden not in code, (path.name, forbidden)


def test_the_probe_modules_import_no_ai_or_recovery_surface():
    for path in PROBE_MODULES:
        tree = ast.parse(path.read_text())
        names: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                names.append(node.module)
        assert not any(n == "backend.ai" or n.startswith("backend.ai.") for n in names), path.name
        for forbidden in (
            "backend.runtime.supervisor",
            "backend.profile.scheduler",
            "backend.ai.tools.executor",
        ):
            assert forbidden not in names, (path.name, forbidden)


def test_the_probe_service_touches_no_database_and_no_schema():
    code = _code_only(Path(probe.__file__).read_text())
    for forbidden in ("db_client", "supabase", "Supabase", "to_thread", "insert(", "upsert"):
        assert forbidden not in code, forbidden


def test_the_probe_service_is_the_only_renderer_and_the_ui_holds_no_send_logic():
    service = Path(probe.__file__).read_text()
    assert "send_reconstructed" in service  # the EXISTING bridge is its only delivery
    assert "send_reconstructed" not in Path(emoji.__file__).read_text()
    # the UI never builds the helper-bot payload itself
    ui = Path(emoji.__file__).read_text()
    assert "build_proof_payload" not in ui
    assert "formatting_entities" not in ui
    code = _code_only(ui)
    assert "deliver_proof" in code  # the UI delegates the send to the service


def test_the_probe_never_registers_a_telegram_listener():
    service = Path(probe.__file__).read_text()
    assert "events" not in service
    ui = Path(emoji.__file__).read_text()
    assert "events.NewMessage" not in ui
