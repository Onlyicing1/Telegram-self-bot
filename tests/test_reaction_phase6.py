"""Reactions — Phase 6 (Emoji & Reaction, ROADMAP §27).

Pins the reaction subsystem end to end, offline:

* the typed Telegram wrapper (`backend/telegram_api/reactions.py`) — payload
  correctness for both reaction forms, input validation BEFORE any RPC,
  bounded timeout, exception normalization, one attempt and no fallback;
* the reaction service (`backend/services/reaction_service.py`) — explicit
  target resolution, stale/foreign/missing target rejection, the owner
  boundary, Telegram-free refusals, honest failures;
* the Glass UI flow (`backend/bot/handlers/emoji.py`) — action registration,
  the reply-mode arming step, deterministic reply-target resolution, custom
  emoji from the reply's own entity, honest panels, and the callback router's
  owner gate.

Architecture pins close the phase: no second client/loop/scheduler/executor,
no `events.NewMessage`, no regex/keyword routing, no AI, and no interaction
with the Phase 3 replacement state or the Phase 4 reconstruction pipeline.

Everything runs offline: the Telegram boundary is faked at the TL-request
surface and no live Telegram or Supabase call is made.
"""
from __future__ import annotations

import ast
import asyncio
import inspect
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from telethon.tl.functions.messages import SendReactionRequest
from telethon.tl.types import (
    MessageEntityCustomEmoji,
    MessageEntityBold,
    ReactionCustomEmoji,
    ReactionEmoji,
)

from backend.bot.handlers import emoji
from backend.db import client as db_client
from backend.helper import inline_engine, input_state, panels
from backend.helper.lifecycle import get_lifecycle
from backend.services import emoji_category_service as cat_service
from backend.services import emoji_replacement_service as repl
from backend.services import emoji_state_service as state_service
from backend.services import reaction_service
from backend.telegram_api import reactions
from backend.telegram_api._helpers import serialize_message
from backend.telegram_api.exceptions import TelegramAPIError, TelegramTimeoutError

OWNER = 7770001
OTHER = 991199

CHAT = -1001234567890
CHAT2 = -1009998887771

TARGET = 4242
REPLY = 4243

KEY = "🫪"
DOC = 42001


@pytest.fixture(autouse=True)
def _env():
    for key in ("emoji_library", "emoji_categories", "emoji_mappings", "emoji_chat_overrides"):
        db_client._fallback[key] = []
    db_client._fallback["emoji_state"] = {}
    input_state.clear_all()
    repl.reset_loop_guard()
    inline_engine.set_owner_id(OWNER)
    yield
    for key in ("emoji_library", "emoji_categories", "emoji_mappings", "emoji_chat_overrides"):
        db_client._fallback[key] = []
    db_client._fallback["emoji_state"] = {}
    input_state.clear_all()
    repl.reset_loop_guard()


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ── telegram-level fakes (the surface the wrapper + facade consume) ──────────


class _FakeSelfClient:
    """Self-client fake: the message read the facade performs plus the raw TL
    request surface (``__call__``) the reaction wrapper uses."""

    def __init__(
        self,
        messages: dict[tuple[int, int], Any] | None = None,
        *,
        get_error: Exception | None = None,
        call_error: Exception | None = None,
        call_delay: float = 0.0,
    ) -> None:
        self._messages = dict(messages or {})
        self.get_error = get_error
        self.call_error = call_error
        self.call_delay = call_delay
        self.calls: list[Any] = []

    def add(self, chat_id: int, msg_id: int, message: Any) -> None:
        self._messages[(chat_id, msg_id)] = message

    async def get_messages(self, chat_id, ids=None):
        if self.get_error is not None:
            raise self.get_error
        if isinstance(ids, list):
            return [self._messages.get((chat_id, i)) for i in ids]
        return self._messages.get((chat_id, ids))

    async def __call__(self, request):
        self.calls.append(request)
        if self.call_delay:
            await asyncio.sleep(self.call_delay)
        if self.call_error is not None:
            raise self.call_error
        return SimpleNamespace(updates=[])


def _message(
    msg_id: int,
    *,
    chat_id: int = CHAT,
    text: str = "",
    entities: list | None = None,
    sender_id: int = OWNER,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=msg_id,
        chat_id=chat_id,
        sender_id=sender_id,
        text=text,
        message=text,
        date=None,
        media=None,
        reply_to=None,
        reply_to_msg_id=None,
        out=True,
        entities=list(entities or []),
    )


def _reply(
    msg_id: int = REPLY,
    *,
    target: int | None = TARGET,
    text: str = "",
    entities: list | None = None,
    header_peer: Any = None,
    chat_id: int = CHAT,
) -> SimpleNamespace:
    header = SimpleNamespace(reply_to_msg_id=target, reply_to_peer_id=header_peer)
    return SimpleNamespace(
        id=msg_id,
        chat_id=chat_id,
        sender_id=OWNER,
        text=text,
        message=text,
        date=None,
        media=None,
        reply_to=header,
        reply_to_msg_id=target,
        out=True,
        entities=list(entities or []),
    )


def _custom_emoji_entity(document_id: int, offset: int = 0, length: int = 2) -> Any:
    return MessageEntityCustomEmoji(offset, length, document_id)


def _datas(buttons) -> list[str]:
    out = []
    for row in buttons:
        for button in row:
            raw = getattr(button, "data", button)
            out.append(raw.decode("utf-8") if isinstance(raw, bytes) else str(raw))
    return out


def _texts(buttons) -> list[str]:
    return [str(getattr(button, "text", button)) for row in buttons for button in row]


def _emoji_reaction(emoji_char: str = "👍") -> dict[str, Any]:
    return {"kind": "emoji", "emoji": emoji_char}


def _custom_reaction(document_id: int = DOC) -> dict[str, Any]:
    return {"kind": "custom_emoji", "document_id": document_id}


# ── replacement-subsystem seeding (used by the separation tests) ─────────────


def _lib(document_id: int = DOC, alt: str = "😀") -> dict:
    row = {
        "owner_id": OWNER,
        "document_id": document_id,
        "alt_text": alt,
        "source": "imported",
        "source_msg_id": 5,
        "created_at": "2026-10-06T10:00:00+00:00",
    }
    db_client._fallback["emoji_library"].append(row)
    return row


def _cat(name: str = "002") -> dict:
    result = _run(cat_service.create_category(OWNER, name))
    assert result["ok"], result
    return result["category"]


def _map(category_id: int, simple: str, document_id: int = DOC) -> None:
    result = _run(cat_service.create_mapping(OWNER, category_id, simple, document_id))
    assert result["ok"], result


def _active(category_id: int) -> None:
    _run(state_service.set_replacement_enabled(OWNER, True))
    _run(state_service.set_global_default_category(OWNER, category_id))


# ── 1. wrapper: payload correctness for both forms ───────────────────────────


def test_emoji_reaction_sends_exactly_one_typed_request():
    client = _FakeSelfClient()
    result = _run(reactions.send_reaction(client, CHAT, TARGET, _emoji_reaction()))
    assert len(client.calls) == 1
    request = client.calls[0]
    assert isinstance(request, SendReactionRequest)
    assert request.peer == CHAT
    assert request.msg_id == TARGET
    assert len(request.reaction) == 1
    assert isinstance(request.reaction[0], ReactionEmoji)
    assert request.reaction[0].emoticon == "👍"
    assert request.big is False
    assert request.add_to_recent is True
    assert result == {
        "chat_id": CHAT,
        "message_id": TARGET,
        "reaction": {"kind": "emoji", "emoji": "👍"},
        "big": False,
    }


def test_custom_emoji_reaction_serializes_the_document_id():
    client = _FakeSelfClient()
    result = _run(reactions.send_reaction(client, CHAT, TARGET, _custom_reaction(555000)))
    request = client.calls[0]
    assert isinstance(request, SendReactionRequest)
    assert isinstance(request.reaction[0], ReactionCustomEmoji)
    assert request.reaction[0].document_id == 555000
    assert result["reaction"] == {"kind": "custom_emoji", "document_id": 555000}


def test_the_request_carries_the_big_flag_only_when_asked():
    client = _FakeSelfClient()
    _run(reactions.send_reaction(client, CHAT, TARGET, _emoji_reaction(), big=True))
    assert client.calls[0].big is True


def test_the_wrapper_result_is_a_plain_json_safe_dict():
    client = _FakeSelfClient()
    result = _run(reactions.send_reaction(client, CHAT, TARGET, _custom_reaction()))
    json.dumps(result)
    assert all(
        isinstance(value, (int, str, bool, dict, type(None))) for value in result.values()
    )


def test_a_negative_chat_id_is_a_valid_supergroup_target():
    client = _FakeSelfClient()
    _run(reactions.send_reaction(client, -1001234567890, TARGET, _emoji_reaction()))
    assert client.calls[0].peer == -1001234567890


# ── 2. wrapper: invalid input is refused BEFORE any RPC ──────────────────────


@pytest.mark.parametrize(
    "chat_id,msg_id",
    [
        (0, TARGET),
        (None, TARGET),
        (True, TARGET),
        ("1", TARGET),
        (CHAT, 0),
        (CHAT, -1),
        (CHAT, None),
        (CHAT, "42"),
        (CHAT, True),
    ],
)
def test_an_unusable_target_is_refused_before_any_rpc(chat_id, msg_id):
    client = _FakeSelfClient()
    with pytest.raises(TelegramAPIError):
        _run(reactions.send_reaction(client, chat_id, msg_id, _emoji_reaction()))
    assert client.calls == []


@pytest.mark.parametrize(
    "reaction",
    [
        None,
        "👍",
        {},
        {"kind": "paid", "document_id": 1},
        {"kind": "emoji"},
        {"kind": "emoji", "emoji": ""},
        {"kind": "emoji", "emoji": "   "},
        {"kind": "emoji", "emoji": " 👍"},
        {"kind": "emoji", "emoji": "👍 "},
        {"kind": "emoji", "emoji": "two words"},
        {"kind": "emoji", "emoji": 12},
        {"kind": "custom_emoji"},
        {"kind": "custom_emoji", "document_id": 0},
        {"kind": "custom_emoji", "document_id": -5},
        {"kind": "custom_emoji", "document_id": True},
        {"kind": "custom_emoji", "document_id": "abc"},
    ],
)
def test_an_unusable_reaction_is_refused_before_any_rpc(reaction):
    client = _FakeSelfClient()
    with pytest.raises(TelegramAPIError):
        _run(reactions.send_reaction(client, CHAT, TARGET, reaction))
    assert client.calls == []


def test_normalize_reaction_accepts_exactly_the_two_forms():
    assert reactions.normalize_reaction(_emoji_reaction()) == {"kind": "emoji", "emoji": "👍"}
    assert reactions.normalize_reaction(_custom_reaction(7)) == {
        "kind": "custom_emoji", "document_id": 7,
    }
    assert reactions.normalize_reaction({"kind": "custom_emoji", "document_id": "7"}) == {
        "kind": "custom_emoji", "document_id": 7,
    }


def test_the_emoji_bound_is_enforced_at_its_edge():
    boundary = "😀" * 16  # 32 UTF-16 units
    assert reactions.normalize_emoji(boundary) == boundary
    assert reactions.normalize_emoji(boundary + "😀") is None
    assert reactions.normalize_emoji("") is None


def test_a_zwj_sequence_is_a_valid_single_reaction():
    family = "\U0001F468\u200D\U0001F469\u200D\U0001F467"
    assert reactions.normalize_emoji(family) == family


# ── 3. wrapper: bounded timeout + exception normalization ────────────────────


def test_a_hanging_reaction_call_is_bounded(monkeypatch):
    monkeypatch.setattr(reactions, "_SHORT_CALL_TIMEOUT", 0.05)
    client = _FakeSelfClient(call_delay=5.0)
    with pytest.raises(TelegramTimeoutError):
        _run(reactions.send_reaction(client, CHAT, TARGET, _emoji_reaction()))
    assert len(client.calls) == 1


def test_a_timeout_error_from_telegram_is_normalized():
    client = _FakeSelfClient(call_error=asyncio.TimeoutError())
    with pytest.raises(TelegramTimeoutError):
        _run(reactions.send_reaction(client, CHAT, TARGET, _emoji_reaction()))


def test_a_telegram_rejection_is_normalized_with_its_cause():
    client = _FakeSelfClient(call_error=RuntimeError("REACTION_INVALID"))
    with pytest.raises(TelegramAPIError) as info:
        _run(reactions.send_reaction(client, CHAT, TARGET, _emoji_reaction()))
    assert "REACTION_INVALID" in str(info.value)
    assert isinstance(info.value.__cause__, RuntimeError)
    assert len(client.calls) == 1  # exactly one attempt — no silent retry


def test_an_existing_telegram_api_error_is_passed_through():
    original = TelegramAPIError("already normalized")
    client = _FakeSelfClient(call_error=original)
    with pytest.raises(TelegramAPIError) as info:
        _run(reactions.send_reaction(client, CHAT, TARGET, _emoji_reaction()))
    assert info.value is original


def test_cancellation_is_never_swallowed():
    client = _FakeSelfClient(call_error=asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        _run(reactions.send_reaction(client, CHAT, TARGET, _emoji_reaction()))


def test_the_wrapper_uses_the_existing_bounded_watchdog():
    source = Path(reactions.__file__).read_text()
    assert "guarded_await(" in source
    assert "timeout=_SHORT_CALL_TIMEOUT" in source


# ── 4. service: deterministic success path ──────────────────────────────────


def _seeded_client(*, target_chat: int = CHAT, reaction_error: Exception | None = None):
    client = _FakeSelfClient(call_error=reaction_error)
    client.add(CHAT, TARGET, _message(TARGET, chat_id=target_chat))
    return client


def test_the_service_reacts_to_the_explicit_target_once():
    client = _seeded_client()
    result = _run(reaction_service.react_to_message(client, OWNER, CHAT, TARGET, _emoji_reaction()))
    assert result["ok"] is True
    assert result["status"] == reaction_service.STATUS_REACTED
    assert result["error"] is None
    assert result["chat_id"] == CHAT
    assert result["message_id"] == TARGET
    assert result["reaction"] == {"kind": "emoji", "emoji": "👍"}
    assert len(client.calls) == 1
    assert client.calls[0].msg_id == TARGET


def test_the_service_reacts_with_a_custom_emoji():
    client = _seeded_client()
    result = _run(reaction_service.react_to_message(client, OWNER, CHAT, TARGET, _custom_reaction(909)))
    assert result["ok"] is True
    assert isinstance(client.calls[0].reaction[0], ReactionCustomEmoji)
    assert client.calls[0].reaction[0].document_id == 909


def test_the_service_leaves_the_replacement_state_alone():
    client = _seeded_client()
    _run(reaction_service.react_to_message(client, OWNER, CHAT, TARGET, _emoji_reaction()))
    assert db_client._fallback["emoji_state"] == {}
    assert _run(state_service.replacement_enabled(OWNER)) is False
    assert db_client._fallback["emoji_mappings"] == []
    assert db_client._fallback["emoji_categories"] == []


# ── 5. service: fail-closed target resolution ───────────────────────────────


def test_a_missing_target_is_refused_without_reacting():
    client = _FakeSelfClient()
    result = _run(reaction_service.react_to_message(client, OWNER, CHAT, TARGET, _emoji_reaction()))
    assert result["ok"] is False
    assert result["error"] == reaction_service.ERROR_TARGET
    assert client.calls == []


def test_a_foreign_target_is_refused_without_reacting():
    client = _seeded_client(target_chat=CHAT2)
    result = _run(reaction_service.react_to_message(client, OWNER, CHAT, TARGET, _emoji_reaction()))
    assert result["ok"] is False
    assert result["error"] == reaction_service.ERROR_TARGET_FOREIGN
    assert client.calls == []


def test_a_stale_target_read_is_reported_honestly():
    client = _FakeSelfClient(get_error=TelegramAPIError("CHANNEL_INVALID"))
    result = _run(reaction_service.react_to_message(client, OWNER, CHAT, TARGET, _emoji_reaction()))
    assert result["ok"] is False
    assert result["error"] == reaction_service.ERROR_TARGET_STALE
    assert "CHANNEL_INVALID" in result["detail"]
    assert client.calls == []


def test_a_deleted_target_id_is_not_accepted():
    client = _FakeSelfClient()
    client.add(CHAT, TARGET, _message(0, chat_id=CHAT))
    result = _run(reaction_service.react_to_message(client, OWNER, CHAT, TARGET, _emoji_reaction()))
    assert result["ok"] is False
    assert result["error"] == reaction_service.ERROR_TARGET
    assert client.calls == []


# ── 6. service: owner boundary + input validation precede any RPC ───────────


@pytest.mark.parametrize("owner_id", [0, -1, None, "1", True])
def test_an_invalid_owner_is_refused_before_any_rpc(owner_id):
    client = _seeded_client()
    result = _run(reaction_service.react_to_message(client, owner_id, CHAT, TARGET, _emoji_reaction()))
    assert result["ok"] is False
    assert result["error"] == reaction_service.ERROR_OWNER
    assert client.calls == []


@pytest.mark.parametrize("chat_id,msg_id", [(0, TARGET), (CHAT, 0), (None, None), (CHAT, -3)])
def test_an_unusable_target_pair_is_refused_before_any_rpc(chat_id, msg_id):
    client = _seeded_client()
    result = _run(reaction_service.react_to_message(client, OWNER, chat_id, msg_id, _emoji_reaction()))
    assert result["ok"] is False
    assert result["error"] == reaction_service.ERROR_TARGET
    assert client.calls == []


def test_an_unusable_reaction_is_refused_before_any_rpc():
    client = _seeded_client()
    result = _run(reaction_service.react_to_message(client, OWNER, CHAT, TARGET, {"kind": "emoji", "emoji": "a b"}))
    assert result["ok"] is False
    assert result["error"] == reaction_service.ERROR_REACTION
    assert client.calls == []


def test_a_missing_self_client_is_reported_honestly():
    result = _run(reaction_service.react_to_message(None, OWNER, CHAT, TARGET, _emoji_reaction()))
    assert result["ok"] is False
    assert result["error"] == reaction_service.ERROR_NO_CLIENT


def test_owner_validity_is_one_predicate():
    assert reaction_service.owner_is_valid(OWNER) is True
    assert reaction_service.owner_is_valid(0) is False
    assert reaction_service.owner_is_valid(True) is False
    assert reaction_service.owner_is_valid("7") is False


# ── 7. service: a rejected reaction is honest and never retried ─────────────


def test_a_rejected_reaction_reports_the_telegram_verdict():
    client = _seeded_client(reaction_error=RuntimeError("REACTION_INVALID"))
    result = _run(reaction_service.react_to_message(client, OWNER, CHAT, TARGET, _custom_reaction(909)))
    assert result["ok"] is False
    assert result["error"] == reaction_service.ERROR_TELEGRAM
    assert "REACTION_INVALID" in result["detail"]
    assert result["reaction"] == {"kind": "custom_emoji", "document_id": 909}


def test_a_rejected_reaction_is_not_retried_in_another_representation():
    client = _seeded_client(reaction_error=RuntimeError("REACTION_INVALID"))
    _run(reaction_service.react_to_message(client, OWNER, CHAT, TARGET, _custom_reaction(909)))
    assert len(client.calls) == 1
    assert isinstance(client.calls[0].reaction[0], ReactionCustomEmoji)
    assert not any(isinstance(call.reaction[0], ReactionEmoji) for call in client.calls)


def test_an_emoji_rejection_is_never_retried_as_a_custom_emoji():
    client = _seeded_client(reaction_error=RuntimeError("REACTION_INVALID"))
    _run(reaction_service.react_to_message(client, OWNER, CHAT, TARGET, _emoji_reaction()))
    assert len(client.calls) == 1
    assert isinstance(client.calls[0].reaction[0], ReactionEmoji)


def test_no_message_side_effects_are_performed_by_a_reaction():
    client = _seeded_client()
    _run(reaction_service.react_to_message(client, OWNER, CHAT, TARGET, _emoji_reaction()))
    assert [type(call) for call in client.calls] == [SendReactionRequest]
    source = Path(reaction_service.__file__).read_text()
    for forbidden in ("delete_messages", "send_message", "SendMessagesRequest", "forward_messages"):
        assert forbidden not in source, forbidden


# ── 8. service: owner-facing label ──────────────────────────────────────────


def test_reaction_label_is_never_fabricated():
    assert reaction_service.reaction_label(_emoji_reaction()) == "👍"
    assert reaction_service.reaction_label(_custom_reaction(909)) == "custom #909"
    assert reaction_service.reaction_label(None) == "?"
    assert reaction_service.reaction_label({"kind": "paid"}) == "?"


# ── 9. UI: registration + arming reply mode ─────────────────────────────────


def test_the_react_action_is_registered_and_reachable_from_the_panel():
    emoji.register(client=None, owner_id=OWNER)
    assert panels.get_action("emoji_react") is not None
    _title, _body, buttons = _run(emoji._emoji_panel_handler(None, ""))
    assert "action:emoji_react" in _datas(buttons)
    assert any("React" in text for text in _texts(buttons))


def test_react_callback_data_stays_bounded():
    emoji.register(client=None, owner_id=OWNER)
    _title, _body, buttons = _run(emoji._emoji_panel_handler(None, ""))
    for data in _datas(buttons):
        assert len(data.encode("utf-8")) <= 64


def test_the_react_action_arms_reply_mode_for_the_current_chat(monkeypatch):
    client = _FakeSelfClient()
    monkeypatch.setattr(emoji, "get_self_client", lambda: client)
    title, body, _buttons = _run(emoji._react_action(SimpleNamespace(message_id=5), "", CHAT))
    assert title == "React"
    assert "Reply to the message" in body
    pending = input_state.get_pending(OWNER)
    assert pending is not None
    assert pending["panel_id"] == "emoji_react"
    assert pending["chat_id"] == CHAT
    assert pending["inline_msg_id"] == 5
    assert pending["handler"] is emoji._react_reply_wait_handler
    assert client.calls == []  # arming performs no Telegram call


def test_the_react_action_without_a_self_client_fails_honestly(monkeypatch):
    monkeypatch.setattr(emoji, "get_self_client", lambda: None)
    _title, body, _buttons = _run(emoji._react_action(SimpleNamespace(message_id=5), "", CHAT))
    assert "not connected" in body.lower()
    assert input_state.get_pending(OWNER) is None


def test_the_react_action_without_an_owner_fails_honestly(monkeypatch):
    monkeypatch.setattr(emoji, "get_self_client", lambda: _FakeSelfClient())
    inline_engine.set_owner_id(0)
    _title, body, _buttons = _run(emoji._react_action(SimpleNamespace(message_id=5), "", CHAT))
    assert "owner" in body.lower()
    assert input_state.get_pending(0) is None


def test_the_react_action_without_a_chat_fails_honestly(monkeypatch):
    monkeypatch.setattr(emoji, "get_self_client", lambda: _FakeSelfClient())
    _title, body, _buttons = _run(emoji._react_action(SimpleNamespace(message_id=5), "", 0))
    assert "chat" in body.lower()


# ── 10. UI: the reply flow (target + reaction value) ────────────────────────


def _capture_panel(monkeypatch):
    captured: dict[str, Any] = {}

    async def _fake_edit(inline_chat_id, inline_msg_id, title, body, buttons):
        captured["edit"] = (inline_chat_id, inline_msg_id, title, body, buttons)

    monkeypatch.setattr(emoji, "_edit_inline", _fake_edit)
    return captured


def _wired_reply_client(reply, *, target_chat: int = CHAT, reaction_error: Exception | None = None):
    client = _FakeSelfClient(call_error=reaction_error)
    client.add(CHAT, reply.id, reply)
    client.add(CHAT, TARGET, _message(TARGET, chat_id=target_chat))
    return client


def _run_reply_handler(client, monkeypatch, *, text: str = ""):
    monkeypatch.setattr(emoji, "get_self_client", lambda: client)
    captured = _capture_panel(monkeypatch)
    _run(emoji._react_reply_wait_handler(text, CHAT, REPLY, CHAT, 5))
    return captured["edit"]


def test_the_reply_flow_reacts_to_the_replied_message(monkeypatch):
    client = _wired_reply_client(_reply(text="👍"))
    edit = _run_reply_handler(client, monkeypatch, text="👍")
    assert len(client.calls) == 1
    request = client.calls[0]
    assert isinstance(request, SendReactionRequest)
    assert request.msg_id == TARGET          # the replied-to message, never the reply
    assert request.reaction[0].emoticon == "👍"
    assert "✓" in edit[3]
    assert f"#{TARGET}" in edit[3]


def test_the_reply_flow_uses_the_reply_entity_for_a_custom_emoji(monkeypatch):
    reply = _reply(text="😀", entities=[_custom_emoji_entity(606060)])
    client = _wired_reply_client(reply)
    edit = _run_reply_handler(client, monkeypatch, text="😀")
    assert len(client.calls) == 1
    assert isinstance(client.calls[0].reaction[0], ReactionCustomEmoji)
    assert client.calls[0].reaction[0].document_id == 606060
    assert "custom #606060" in edit[3]


def test_the_reply_flow_ignores_unrelated_entities(monkeypatch):
    reply = _reply(text="👍", entities=[MessageEntityBold(0, 2)])
    client = _wired_reply_client(reply)
    _run_reply_handler(client, monkeypatch, text="👍")
    assert isinstance(client.calls[0].reaction[0], ReactionEmoji)


def test_the_reply_flow_refuses_a_non_reply(monkeypatch):
    client = _wired_reply_client(_reply(target=None, text="👍"))
    edit = _run_reply_handler(client, monkeypatch, text="👍")
    assert client.calls == []
    assert "not a reply" in edit[3].lower()


def test_the_reply_flow_refuses_a_cross_chat_reply_header(monkeypatch):
    header_peer = SimpleNamespace(channel_id=12345)
    client = _wired_reply_client(_reply(text="👍", header_peer=header_peer))
    edit = _run_reply_handler(client, monkeypatch, text="👍")
    assert client.calls == []
    assert "another chat" in edit[3].lower()


def test_the_reply_flow_reports_a_missing_reply_message(monkeypatch):
    client = _FakeSelfClient()
    monkeypatch.setattr(emoji, "get_self_client", lambda: client)
    captured = _capture_panel(monkeypatch)
    _run(emoji._react_reply_wait_handler("👍", CHAT, REPLY, CHAT, 5))
    assert client.calls == []
    assert "could not read your reply" in captured["edit"][3].lower()


def test_the_reply_flow_reports_an_unreadable_reply_message(monkeypatch):
    client = _FakeSelfClient(get_error=RuntimeError("CHANNEL_INVALID"))
    monkeypatch.setattr(emoji, "get_self_client", lambda: client)
    captured = _capture_panel(monkeypatch)
    _run(emoji._react_reply_wait_handler("👍", CHAT, REPLY, CHAT, 5))
    assert client.calls == []
    assert "could not read your reply" in captured["edit"][3].lower()


def test_the_reply_flow_reports_an_empty_reaction_honestly(monkeypatch):
    client = _wired_reply_client(_reply(text=""))
    edit = _run_reply_handler(client, monkeypatch, text="")
    assert client.calls == []
    assert "reply with the emoji" in edit[3].lower()


def test_the_reply_flow_reports_an_invalid_reaction_value(monkeypatch):
    client = _wired_reply_client(_reply(text="a b"))
    edit = _run_reply_handler(client, monkeypatch, text="a b")
    assert client.calls == []
    assert "✗" in edit[3]
    assert reaction_service.ERROR_REACTION in edit[3]


def test_the_reply_flow_reports_a_telegram_rejection_honestly(monkeypatch):
    client = _wired_reply_client(
        _reply(text="👍"), reaction_error=RuntimeError("REACTION_INVALID"),
    )
    edit = _run_reply_handler(client, monkeypatch, text="👍")
    assert len(client.calls) == 1
    assert "✗" in edit[3]
    assert reaction_service.ERROR_TELEGRAM in edit[3]
    assert "REACTION_INVALID" in edit[3]


def test_the_reply_flow_reports_a_stale_target_honestly(monkeypatch):
    client = _FakeSelfClient()
    client.add(CHAT, REPLY, _reply(text="👍"))
    monkeypatch.setattr(emoji, "get_self_client", lambda: client)
    captured = _capture_panel(monkeypatch)
    _run(emoji._react_reply_wait_handler("👍", CHAT, REPLY, CHAT, 5))
    assert client.calls == []
    assert reaction_service.ERROR_TARGET in captured["edit"][3]


def test_the_reply_flow_reports_a_foreign_target_honestly(monkeypatch):
    client = _wired_reply_client(_reply(text="👍"), target_chat=CHAT2)
    edit = _run_reply_handler(client, monkeypatch, text="👍")
    assert client.calls == []
    assert reaction_service.ERROR_TARGET_FOREIGN in edit[3]


def test_the_reply_flow_without_a_self_client_fails_honestly(monkeypatch):
    monkeypatch.setattr(emoji, "get_self_client", lambda: None)
    captured = _capture_panel(monkeypatch)
    _run(emoji._react_reply_wait_handler("👍", CHAT, REPLY, CHAT, 5))
    assert "not connected" in captured["edit"][3].lower()


def test_the_reply_flow_performs_no_message_side_effect(monkeypatch):
    client = _wired_reply_client(_reply(text="👍"))
    _run_reply_handler(client, monkeypatch, text="👍")
    assert [type(call) for call in client.calls] == [SendReactionRequest]


def test_the_result_panel_offers_the_next_reaction_slot():
    _title, _body, buttons = emoji._render_reaction_result(
        {"ok": True, "reaction": _emoji_reaction()}, TARGET
    )
    assert "action:emoji_react" in _datas(buttons)
    failed = emoji._render_reaction_result({"ok": False, "error": "E_X"}, TARGET)
    assert "E_X" in failed[1]


# ── 11. UI: callback owner validation through the existing router ───────────


class _CallbackEvent:
    def __init__(self, data: str, sender_id: int, *, chat_id: int = CHAT, msg_id: int = 5) -> None:
        self.data = data.encode("utf-8")
        self.sender_id = sender_id
        self.chat_id = chat_id
        self.message_id = msg_id
        self.inline_message_id = None
        self.answered = False
        self.edits: list[tuple[str, Any]] = []

    async def answer(self, *args, **kwargs) -> None:
        self.answered = True

    async def edit(self, text, buttons=None, **kwargs) -> None:
        self.edits.append((text, buttons))


def _register_router(owner_id: int):
    captured: dict[str, Any] = {}

    class _HelperBot:
        def on(self, _event_builder):
            def _decorator(func):
                captured["router"] = func
                return func
            return _decorator

    panels.register_callback_handlers(_HelperBot(), owner_id)
    return captured["router"]


def test_a_non_owner_callback_never_reaches_the_react_action(monkeypatch):
    emoji.register(client=None, owner_id=OWNER)
    router = _register_router(OWNER)
    event = _CallbackEvent("action:emoji_react", OTHER)
    _run(router(event))
    assert event.answered is True
    assert event.edits == []
    assert input_state.get_pending(OWNER) is None


def test_the_owner_callback_reaches_the_react_action_through_the_router(monkeypatch):
    emoji.register(client=None, owner_id=OWNER)
    inline_engine.set_owner_id(OWNER)
    monkeypatch.setattr(emoji, "get_self_client", lambda: _FakeSelfClient())
    router = _register_router(OWNER)
    sessions = get_lifecycle().sessions
    sessions.create(CHAT, 5, panel_type="emoji", owner_id=OWNER)
    try:
        event = _CallbackEvent("action:emoji_react", OWNER)
        _run(router(event))
        assert event.answered is True
        assert len(event.edits) == 1
        assert "Reply to the message" in event.edits[0][0]
        pending = input_state.get_pending(OWNER)
        assert pending is not None and pending["chat_id"] == CHAT
    finally:
        sessions.destroy(CHAT, 5)
        input_state.clear_all()


def test_the_react_flow_adds_no_second_listener():
    section = _react_ui_section()
    assert "action:emoji_react" in section
    for forbidden in ("events.NewMessage", "events.CallbackQuery", "@client.on", "client.on("):
        assert forbidden not in section, forbidden


# ── 12. separation from the replacement subsystems ──────────────────────────


def test_a_pending_react_input_is_left_alone_by_the_replacement_pipeline(monkeypatch):
    _lib(DOC, alt=KEY)
    category = _cat()
    _map(category["id"], KEY)
    _active(category["id"])

    client = _FakeSelfClient()
    monkeypatch.setattr(emoji, "get_self_client", lambda: client)
    _run(emoji._react_action(SimpleNamespace(message_id=5), "", CHAT))
    assert input_state.get_pending(OWNER) is not None

    owner_message = _message(77, text=f"hello {KEY}")
    outcome = _run(repl.process_outgoing_message(
        owner_id=OWNER,
        client=client,
        message=serialize_message(owner_message),
    ))
    assert outcome["status"] == repl.STATUS_PENDING_INPUT
    assert client.calls == []


def test_the_reaction_modules_contain_no_replacement_surface():
    for module in (reactions, reaction_service):
        code = _code_only(Path(module.__file__).read_text())
        for forbidden in (
            "emoji_state",
            "resolve_effective_category",
            "emoji_mappings",
            "emoji_transformer",
            "process_outgoing_message",
            "transform_message",
            "send_reconstructed",
            "bridge",
        ):
            assert forbidden not in code, (module.__file__, forbidden)


def _react_ui_section() -> str:
    source = Path(inspect.getfile(emoji)).read_text()
    start = source.index("# ── reaction (Phase 6")
    end = source.index("# ── shared edit helpers")
    return source[start:end]


def test_the_react_ui_section_never_reconstructs_a_message():
    section = _react_ui_section()
    code = _code_only(section)
    for forbidden in (
        "state_service",
        "cat_service",
        "transform_message",
        "emoji_replacement_service",
        "delete_messages",
        "send_message",
        "forward",
    ):
        assert forbidden not in code, forbidden
    assert "reaction_service . react_to_message (" in code
    assert "reaction_service.react_to_message(" in section


# ── 13. architecture audit ──────────────────────────────────────────────────


PHASE6_MODULES = (reactions.__file__, reaction_service.__file__)


def _code_only(text: str) -> str:
    """A module's executable text — comments and string literals stripped.

    The scanners below check behaviour, not prose: a boundary statement in a
    docstring ("this never touches `emoji_state`") must never be mistaken for
    a real reference, and a real reference must never hide in a docstring.
    """
    import io
    import tokenize

    pieces: list[str] = []
    try:
        tokens = tokenize.generate_tokens(io.StringIO(text).readline)
        for token in tokens:
            if token.type in (tokenize.COMMENT, tokenize.STRING, tokenize.NL):
                continue
            pieces.append(token.string)
    except tokenize.TokenError:
        return text
    return " ".join(pieces)


def _imports_of(module_path: str) -> list[str]:
    tree = ast.parse(Path(module_path).read_text())
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.append(node.module)
    return names


def test_phase6_modules_import_no_ai_and_no_runtime_recovery():
    for module_path in PHASE6_MODULES:
        imported = _imports_of(module_path)
        assert not any(n == "backend.ai" or n.startswith("backend.ai.") for n in imported), module_path
        for forbidden in (
            "backend.profile.scheduler",
            "backend.runtime.supervisor",
            "backend.runtime.task_guard",
            "backend.ai.tools.executor",
            "backend.helper.inline_engine",
        ):
            assert forbidden not in imported, (module_path, forbidden)


def test_phase6_modules_add_no_second_loop_client_or_executor():
    for module_path in PHASE6_MODULES:
        source = _code_only(Path(module_path).read_text())
        for forbidden in (
            "TelegramClient",
            "run_until_disconnected",
            "create_task",
            "immortal_create_task",
            "guarded_create_task",
            "asyncio.Lock",
            "new_event_loop",
            "run_until_complete",
            "call_later",
            "forward_messages",
            "SendMessagesRequest",
            "events.NewMessage",
        ):
            assert forbidden not in source, (module_path, forbidden)


def test_phase6_modules_use_no_regex_or_keyword_routing():
    for module_path in PHASE6_MODULES:
        source = _code_only(Path(module_path).read_text())
        assert "import re" not in source, module_path
        assert "re . match" not in source and "re . search" not in source, module_path


def test_the_wrapper_exposes_only_the_typed_reaction_forms():
    parameters = list(inspect.signature(reactions.send_reaction).parameters)
    assert parameters == ["client", "chat_id", "msg_id", "reaction", "big", "add_to_recent"]
    # No public callable accepts an arbitrary TL request: the module builds
    # exactly one typed request itself, so arbitrary RPC execution is not
    # reachable through this boundary.
    for name, obj in vars(reactions).items():
        if name.startswith("_") or not inspect.isfunction(obj):
            continue
        assert "request" not in inspect.signature(obj).parameters, name
    assert list(inspect.signature(reactions.send_reaction).parameters)[3] == "reaction"


def test_the_phase6_modules_create_no_database_or_schema_surface():
    for module_path in PHASE6_MODULES:
        code = _code_only(Path(module_path).read_text())
        for forbidden in ("db_client", "supabase", "Supabase", "to_thread", "insert(", "upsert"):
            assert forbidden not in code, (module_path, forbidden)


def test_the_reaction_flow_uses_one_hop_through_the_existing_registry():
    from backend.helper.panel_registry import registry as get_registry

    emoji.register(client=None, owner_id=OWNER)
    assert panels.get_action("emoji_react") is not None
    assert get_registry().get("emoji_react") is None  # an action, not a new panel
    assert panels.get_input("emoji", "emoji_react") is None



