"""Private Message Management — the scope gate, the action model and the limits.

THE acceptance criterion: this feature may act on an INCOMING DIRECT MESSAGE
ONLY when the event is a genuine private conversation with the owner's account
AND the sender is a genuine human Telegram user. A private chat with a BOT is
still a private chat; a linked discussion group and a supergroup are
``PeerChannel``; a channel post is not a private message at all. Every one of
those must be rejected WITHOUT any side effect.

Everything here drives the REAL enforcement entry point
(``backend/bot/handlers/private_message.enforce_incoming``, called through the
existing ``task_events`` handler) with REAL Telethon entities, a recording
Telegram spy and a fake table pair — no live Telegram, no live database, no AI
network call.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from telethon import types

from backend.bot.handlers import private_message as pm
from backend.bot.handlers import task_events
from backend.helper import inline_engine
from backend.services import private_message_service as svc

OWNER = 7770001
STRANGER = 5001
OTHER = 5002

REPO = Path(__file__).resolve().parent.parent
MIGRATION = REPO / "supabase" / "migrations" / "20260929000001_create_private_message_tables.sql"


# ── fakes ────────────────────────────────────────────────────────────────────


class _Result:
    def __init__(self, data):
        self.data = data


class _FakeDB:
    """``private_message_config`` + ``private_message_senders`` (+ the legacy
    ``bot_settings`` document), with the CAS semantics PostgREST really has:
    an ``update`` returns only the rows its filters matched."""

    def __init__(self, legacy=None, config=None, senders=None,
                 fail_config_write=False, fail_counter_read=False):
        self.legacy = dict(legacy or {})
        self.config = dict(config or {})
        self.senders = list(senders or [])
        self.fail_config_write = fail_config_write
        self.fail_counter_read = fail_counter_read
        self._table = None
        self._op = None
        self._payload = None
        self._filters = []
        self._limit = None
        self._order = None
        self._single = False

    def table(self, name):
        assert name in ("bot_settings", "private_message_config", "private_message_senders")
        self._table, self._op, self._payload = name, None, None
        self._filters, self._limit, self._order, self._single = [], None, None, False
        return self

    def select(self, columns="*"):
        self._op = "select"
        return self

    def insert(self, payload):
        if self._table == "private_message_config" and self.fail_config_write:
            raise RuntimeError("write refused")
        self._op, self._payload = "insert", payload
        return self

    def update(self, payload):
        if self._table == "private_message_config" and self.fail_config_write:
            raise RuntimeError("write refused")
        self._op, self._payload = "update", payload
        return self

    def delete(self):
        self._op = "delete"
        return self

    def eq(self, column, value):
        self._filters.append((column, value))
        return self

    def limit(self, count):
        self._limit = count
        return self

    def order(self, column, desc=False):
        self._order = (column, bool(desc))
        return self

    def maybe_single(self):
        self._single = True
        return self

    def execute(self):
        if self._table == "bot_settings":
            key = self._filters[0][1] if self._filters else None
            if self._op == "insert":
                self.legacy[self._payload["key"]] = self._payload["value"]
                return _Result([self._payload])
            value = self.legacy.get(key)
            if self._single:
                return _Result({"value": value} if value is not None else {})
            return _Result([{"value": value}] if value is not None else [])
        if self._table == "private_message_config":
            owner = self._owner()
            row = self.config.get(owner)
            if self._op == "insert":
                self.config[int(self._payload["owner_id"])] = {
                    key: value for key, value in self._payload.items() if key != "owner_id"
                }
                return _Result([])
            if self._op == "update":
                if row is None:
                    return _Result([])
                row.update(self._payload)
                return _Result([dict(row)])
            return _Result([dict(row)] if row is not None else [])
        return self._senders()

    def _owner(self):
        for column, value in self._filters:
            if column == "owner_id":
                return int(value)
        return None

    def _senders(self):
        if self._op == "select" and self.fail_counter_read:
            raise RuntimeError("counter store unavailable")
        if self._op == "insert":
            self.senders.append(dict(self._payload))
            return _Result([dict(self._payload)])
        if self._op == "delete":
            owner = self._owner()
            self.senders = [row for row in self.senders if row.get("owner_id") != owner]
            return _Result([])
        wanted = dict(self._filters)
        matched = [
            row for row in self.senders
            if all(row.get(key) == value for key, value in wanted.items())
        ]
        if self._op == "update":
            for row in matched:
                row.update(self._payload)
            return _Result([dict(row) for row in matched])
        if self._order:
            column, desc = self._order
            matched = sorted(matched, key=lambda row: str(row.get(column)), reverse=desc)
        if self._limit is not None:
            matched = matched[: self._limit]
        return _Result([dict(row) for row in matched])


class _RecordingClient:
    """The Telegram boundary: what the handler could have done to Telegram."""

    def __init__(self, delete_fails: bool = False, send_fails: bool = False):
        self.deleted: list[tuple[int, list[int]]] = []
        self.sent: list[tuple[int, str]] = []
        self.entities: dict[int, object] = {}
        self.delete_fails = delete_fails
        self.send_fails = send_fails
        self.handlers: list = []

    def on(self, event_type):
        def _decorator(fn):
            self.handlers.append(fn)
            return fn

        return _decorator

    async def delete_messages(self, chat_id, ids, revoke=False):
        if self.delete_fails:
            raise RuntimeError("delete refused")
        self.deleted.append((chat_id, list(ids)))

    async def send_message(self, entity, text):
        if self.send_fails:
            raise RuntimeError("send refused")
        self.sent.append((entity, text))

    async def get_entity(self, reference):
        if reference not in self.entities:
            raise ValueError(f"cannot resolve {reference}")
        return self.entities[reference]


def _user(user_id: int, *, bot: bool = False, deleted: bool = False):
    """A REAL Telethon ``User``: the gate checks the entity type AND the
    entity's own authoritative ``bot`` flag, so a stand-in would not do."""
    return types.User(
        user_id, False, False, False, deleted, bot, False, False, False, False,
        False, False, False, False, False, 1,
    )


def _bot_entity(user_id: int):
    return _user(user_id, bot=True)


def _event(
    chat_id,
    sender_id,
    message_id=7,
    *,
    text="hello",
    out=False,
    is_private=True,
    is_group=False,
    is_channel=False,
    peer=None,
    chat_entity="default",
    sender_entity="default",
    chat_fails=False,
    sender_fails=False,
    message=True,
):
    resolved_chat = _user(chat_id) if chat_entity == "default" else chat_entity
    resolved_sender = _user(sender_id) if sender_entity == "default" else sender_entity

    async def get_chat():
        if chat_fails:
            raise ValueError("cannot resolve chat")
        return resolved_chat

    async def get_sender():
        if sender_fails:
            raise ValueError("cannot resolve sender")
        return resolved_sender

    return SimpleNamespace(
        chat_id=chat_id,
        sender_id=sender_id,
        out=out,
        is_private=is_private,
        is_group=is_group,
        is_channel=is_channel,
        raw_text=text,
        message=(
            SimpleNamespace(
                id=message_id,
                message=text,
                media=None,
                reply_to_msg_id=None,
                date=datetime.now(timezone.utc),
                peer_id=peer if peer is not None else types.PeerUser(chat_id),
            )
            if message
            else None
        ),
        get_chat=get_chat,
        get_sender=get_sender,
    )


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    svc.reset_in_memory()
    svc.configure_owner(OWNER)
    task_events.configure(None)
    task_events.configure_answer_resolver(None)
    inline_engine.set_owner_id(OWNER)
    yield
    svc.reset_in_memory()
    task_events.configure(None)
    task_events.configure_answer_resolver(None)


@pytest.fixture()
def db(monkeypatch):
    fake = _FakeDB()
    monkeypatch.setattr("backend.db.client.get_db", lambda: fake)
    return fake


@pytest.fixture()
def client():
    return _RecordingClient()


def _handler(client):
    """The REAL entry point: the existing task_events handler, which calls the
    private-message enforcement hook first."""
    task_events.register(client, OWNER, "Asia/Tehran")
    assert len(client.handlers) == 1
    return client.handlers[0]


def _restricted(db, **actions):
    """Configure one unauthorized policy with the requested independent actions."""
    _run(svc.set_mode(svc.MODE_CLOSED))
    for field_name, value in actions.items():
        assert _run(svc.set_action(field_name, value)) is True
    return svc.state()


def _classify(**overrides):
    kwargs = dict(
        owner_id=OWNER,
        is_private=True,
        is_group=False,
        is_channel=False,
        chat_id=STRANGER,
        sender_id=STRANGER,
        message_id=7,
        peer_user_id=STRANGER,
        chat_is_user=True,
        sender_is_user=True,
        chat_entity=_user(STRANGER),
        sender_entity=_user(STRANGER),
    )
    kwargs.update(overrides)
    return svc.classify_scope(**kwargs)


# ── A. the eligible case ─────────────────────────────────────────────────────


def test_a_genuine_human_private_message_is_eligible_and_the_actions_run(db, client):
    _restricted(db, delete_incoming=True, fixed_response_enabled=True)
    _run(svc.set_fixed_response_text("closed"))
    handler = _handler(client)

    _run(handler(_event(STRANGER, STRANGER, message_id=11)))

    assert client.deleted == [(STRANGER, [11])]
    assert client.sent == [(STRANGER, "closed")]


def test_the_human_private_verdict_names_the_identity(db):
    verdict = _classify()
    assert verdict.eligible is True
    assert verdict.reason == svc.SCOPE_ELIGIBLE
    assert (verdict.chat_id, verdict.sender_id, verdict.message_id) == (STRANGER, STRANGER, 7)


# ── B/J. bots in a private chat ──────────────────────────────────────────────


def test_b_a_bot_in_a_private_chat_is_rejected_with_no_side_effects(db, client):
    _restricted(
        db,
        delete_incoming=True,
        fixed_response_enabled=True,
        ai_response_enabled=True,
    )
    _run(svc.set_fixed_response_text("closed"))
    handler = _handler(client)
    bot = _bot_entity(STRANGER)

    _run(handler(_event(STRANGER, STRANGER, chat_entity=bot, sender_entity=bot)))

    assert client.deleted == [] and client.sent == []
    assert db.senders == []


def test_j_a_bot_is_rejected_even_with_every_action_enabled_and_unlimited(db, client):
    _restricted(
        db,
        delete_incoming=True,
        fixed_response_enabled=True,
        ai_response_enabled=True,
    )
    _run(svc.set_fixed_response_text("closed"))
    _run(svc.set_ai_unlimited_replies(True))
    handler = _handler(client)
    bot = _bot_entity(STRANGER)

    for message_id in range(5):
        _run(handler(_event(STRANGER, STRANGER, message_id=message_id, chat_entity=bot, sender_entity=bot)))

    assert client.deleted == [] and client.sent == []
    assert db.senders == []


def test_a_bot_that_is_only_the_sender_entity_is_still_rejected(db, client):
    _restricted(db, delete_incoming=True)
    handler = _handler(client)

    _run(handler(_event(STRANGER, STRANGER, chat_entity=_user(STRANGER), sender_entity=_bot_entity(STRANGER))))

    assert client.deleted == []


def test_the_scope_classifier_reports_the_bot_verdict(db):
    assert _classify(sender_entity=_bot_entity(STRANGER)).reason == svc.SCOPE_BOT
    assert _classify(chat_entity=_bot_entity(STRANGER)).reason == svc.SCOPE_BOT


# ── C/D/E/F. groups, supergroups, channels, linked discussion groups ─────────


@pytest.mark.parametrize(
    "overrides,expected",
    [
        ({"is_private": False, "is_group": True}, svc.SCOPE_GROUP),
        ({"is_private": False, "is_group": True, "chat_id": -1001234567890, "sender_id": STRANGER,
          "peer_user_id": STRANGER}, svc.SCOPE_GROUP),
        ({"is_private": False, "is_channel": True}, svc.SCOPE_CHANNEL),
        ({"is_private": False}, svc.SCOPE_CHAT_UNKNOWN),
        ({"is_private": None}, svc.SCOPE_CHAT_UNKNOWN),
        ({"is_group": None}, svc.SCOPE_CHAT_UNKNOWN),
        ({"is_channel": None}, svc.SCOPE_CHAT_UNKNOWN),
    ],
)
def test_cdef_non_private_conversations_are_rejected_by_the_classifier(overrides, expected):
    assert _classify(**overrides).reason == expected


def test_cdef_no_action_runs_for_a_group_a_supergroup_or_a_channel(db, client):
    _restricted(db, delete_incoming=True, fixed_response_enabled=True, ai_response_enabled=True)
    _run(svc.set_fixed_response_text("closed"))
    handler = _handler(client)

    supergroup = _event(-1001234567890, STRANGER, is_private=False, is_group=True)
    channel = _event(-1009876543210, STRANGER, is_private=False, is_channel=True)
    discussion = _event(-1001234567890, STRANGER, is_private=False, is_group=True)
    for event in (supergroup, channel, discussion):
        _run(handler(event))

    assert client.deleted == [] and client.sent == []
    assert db.senders == []


def test_a_peer_that_is_not_a_user_is_rejected(db, client):
    _restricted(db, delete_incoming=True)
    handler = _handler(client)
    from telethon.tl.types import PeerChannel

    _run(handler(_event(STRANGER, STRANGER, peer=PeerChannel(123))))

    assert client.deleted == []


def test_a_channel_entity_as_the_chat_is_rejected():
    channel = types.Channel(
        123, False, False, False, False, False, False, False, False, False,
        False, False, False, False, False, None, 1,
    )
    verdict = _classify(chat_entity=channel, chat_is_user=False)
    assert verdict.eligible is False
    assert verdict.reason in (svc.SCOPE_NOT_PRIVATE, svc.SCOPE_SENDER_TYPE_UNKNOWN)


# ── G/H/I. unresolved, malformed and half-known events ───────────────────────


def test_g_a_missing_sender_entity_is_rejected(db, client):
    _restricted(db, delete_incoming=True, fixed_response_enabled=True, ai_response_enabled=True)
    _run(svc.set_fixed_response_text("closed"))
    handler = _handler(client)

    _run(handler(_event(STRANGER, STRANGER, message_id=71, sender_entity=None)))

    assert client.deleted == [] and client.sent == []
    assert db.senders == []


def test_g_a_missing_chat_entity_is_rejected(db, client):
    _restricted(db, delete_incoming=True)
    handler = _handler(client)
    _run(handler(_event(STRANGER, STRANGER, chat_entity=None)))

    assert client.deleted == []


def test_h_an_entity_without_a_usable_bot_flag_is_rejected():
    assert _classify(sender_entity=SimpleNamespace(id=STRANGER)).reason == svc.SCOPE_SENDER_TYPE_UNKNOWN
    assert _classify(sender_entity=SimpleNamespace(id=STRANGER, bot="yes")).reason == svc.SCOPE_SENDER_TYPE_UNKNOWN


def test_h_a_deleted_account_is_rejected():
    assert _classify(sender_entity=_user(STRANGER, deleted=True)).reason == svc.SCOPE_DELETED


def test_h_a_sender_entity_that_is_not_a_user_is_rejected():
    assert _classify(sender_is_user=False).reason == svc.SCOPE_SENDER_NOT_USER


def test_i_an_entity_resolution_failure_is_rejected(db, client):
    _restricted(db, delete_incoming=True, fixed_response_enabled=True)
    _run(svc.set_fixed_response_text("closed"))
    handler = _handler(client)

    _run(handler(_event(STRANGER, STRANGER, message_id=31, sender_fails=True)))
    _run(handler(_event(STRANGER, STRANGER, message_id=32, chat_fails=True)))

    assert client.deleted == [] and client.sent == []
    assert db.senders == []


# ── K. contradictory or unsupported events ───────────────────────────────────


@pytest.mark.parametrize(
    "overrides,expected",
    [
        ({"chat_id": STRANGER, "sender_id": OTHER}, svc.SCOPE_INCONSISTENT),
        ({"peer_user_id": OTHER}, svc.SCOPE_INCONSISTENT),
        ({"chat_id": None}, svc.SCOPE_INCONSISTENT),
        ({"sender_id": True}, svc.SCOPE_INCONSISTENT),
        ({"sender_id": 0}, svc.SCOPE_INCONSISTENT),
        ({"message_id": None}, svc.SCOPE_INCONSISTENT),
        ({"owner_id": "7770001"}, svc.SCOPE_INCONSISTENT),
        ({"owner_id": OWNER, "chat_id": OWNER, "sender_id": OWNER, "peer_user_id": OWNER}, svc.SCOPE_SELF),
        ({"sender_entity": _user(OTHER)}, svc.SCOPE_INCONSISTENT),
        ({"chat_entity": _user(OTHER)}, svc.SCOPE_INCONSISTENT),
    ],
)
def test_k_contradictory_or_unsupported_events_are_rejected(overrides, expected):
    assert _classify(**overrides).reason == expected


def test_k_an_outgoing_message_is_never_enforced(db, client):
    _restricted(db, delete_incoming=True, fixed_response_enabled=True)
    _run(svc.set_fixed_response_text("closed"))
    handler = _handler(client)

    _run(handler(_event(STRANGER, STRANGER, out=True)))

    assert client.deleted == [] and client.sent == []


def test_k_a_message_less_event_is_rejected(db, client):
    _restricted(db, delete_incoming=True)
    handler = _handler(client)

    _run(handler(_event(STRANGER, STRANGER, message=False)))

    assert client.deleted == []


# ── L. rejected events consume nothing ───────────────────────────────────────


def test_l_repeated_rejected_events_never_consume_a_humans_allowance(db, client):
    _restricted(
        db,
        delete_incoming=True,
        fixed_response_enabled=True,
        ai_response_enabled=True,
    )
    _run(svc.set_fixed_response_text("closed"))
    _run(svc.set_ai_max_replies_per_person(1))
    _run(svc.set_ai_interval_seconds(0))
    handler = _handler(client)
    bot = _bot_entity(OTHER)

    for message_id in range(15):
        _run(handler(_event(OTHER, OTHER, message_id=message_id, chat_entity=bot, sender_entity=bot)))
        _run(handler(_event(-1001234567890, OTHER, message_id=message_id, is_private=False, is_group=True)))
        _run(handler(_event(OTHER, OTHER, message_id=message_id, sender_fails=True)))

    assert db.senders == []
    assert client.deleted == [] and client.sent == []


# ── the independent action model ─────────────────────────────────────────────


def test_delete_only_deletes_and_sends_nothing(db, client):
    _restricted(db, delete_incoming=True)
    handler = _handler(client)

    _run(handler(_event(STRANGER, STRANGER, message_id=41)))

    assert client.deleted == [(STRANGER, [41])]
    assert client.sent == []


def test_fixed_only_sends_without_deleting(db, client):
    _restricted(db, fixed_response_enabled=True)
    _run(svc.set_fixed_response_text("closed"))
    handler = _handler(client)

    _run(handler(_event(STRANGER, STRANGER, message_id=42)))

    assert client.deleted == []
    assert client.sent == [(STRANGER, "closed")]


def test_an_enabled_fixed_response_without_text_sends_nothing(db, client):
    _restricted(db, fixed_response_enabled=True)
    handler = _handler(client)

    _run(handler(_event(STRANGER, STRANGER, message_id=43)))

    assert client.sent == []


def test_the_actions_execute_in_the_documented_order(db, client, monkeypatch):
    order: list[str] = []

    async def _delete(client_, chat_id, message_id):
        order.append("delete")
        return True

    async def _send(client_, chat_id, text, label):
        order.append(label)
        return True

    monkeypatch.setattr(pm, "_delete_incoming", _delete)
    monkeypatch.setattr(pm, "_send_text", _send)
    monkeypatch.setattr(pm, "_generate_ai_reply", _async_return("پیام شما رسید."))

    _restricted(
        db,
        delete_incoming=True,
        fixed_response_enabled=True,
        ai_response_enabled=True,
    )
    _run(svc.set_fixed_response_text("closed"))
    handler = _handler(client)

    _run(handler(_event(STRANGER, STRANGER, message_id=44, text="hi")))

    assert order == ["delete", "fixed_response", "ai_response"]


def _async_return(value):
    async def _inner(*args, **kwargs):
        return value

    return _inner


# ── the AI boundary ──────────────────────────────────────────────────────────


def test_the_ai_reply_carries_both_disclosures_exactly_once(db):
    body = svc.compose_ai_reply("  متنی  ")
    assert body.endswith(f"{svc.CLOSED_NOTICE}\n{svc.AI_NOTICE}")
    assert body.startswith("متنی")
    assert body.count(svc.CLOSED_NOTICE) == 1
    assert body.count(svc.AI_NOTICE) == 1
    assert svc.compose_ai_reply("") is None
    assert svc.compose_ai_reply(None) is None
    assert svc.compose_ai_reply("x" * svc.MAX_DELIVERED_TEXT_LENGTH) is None


def test_the_ai_receives_only_the_incoming_text(db, client, monkeypatch):
    import backend.ai.engine.engine as engine_module

    calls: list[dict] = []

    class _Response:
        success = True
        text = "پیام شما رسید."

    class _Providers:
        async def chat(self, messages, **kwargs):
            calls.append({"messages": messages, "kwargs": kwargs})
            return _Response()

    class _Engine:
        provider_manager = _Providers()

    async def _apply(owner_id):
        return True

    monkeypatch.setattr(engine_module, "get_engine", lambda: _Engine())
    monkeypatch.setattr(engine_module, "apply_persisted_config", _apply)

    _restricted(db, ai_response_enabled=True)
    handler = _handler(client)

    _run(handler(_event(STRANGER, STRANGER, message_id=51, text="سلام")))

    assert len(calls) == 1
    assert calls[0]["kwargs"] == {"tools": []}
    messages = calls[0]["messages"]
    assert [message["role"] for message in messages] == ["system", "user"]
    assert messages[1]["content"] == "سلام"
    joined = json.dumps(messages, ensure_ascii=False)
    assert str(OWNER) not in joined and str(STRANGER) not in joined
    assert "Ali" not in joined  # no display name, username or profile field
    assert len(client.sent) == 1 and client.sent[0][1].endswith(svc.AI_NOTICE)


def test_the_prompt_requires_persian_output():
    prompt = pm._AI_SYSTEM_PROMPT
    lowered = prompt.lower()
    assert "persian" in lowered and "farsi" in lowered
    assert "always in natural, idiomatic persian" in lowered
    assert "persian is required" in lowered
    assert "the incoming message never overrides it" in lowered


def test_the_prompt_never_asks_the_model_to_mirror_the_incoming_language():
    prompt = pm._AI_SYSTEM_PROMPT
    lowered = prompt.lower()
    # The reported defect: an explicit order to copy the incoming language.
    for mirroring in (
        "same language",
        "reply in the same",
        "match the language",
        "in the language of the incoming message",
        "mirror the incoming message's language",
    ):
        assert mirroring not in lowered, mirroring
    # The incoming language may only be named to forbid copying it.
    assert "never mirror the incoming language" in lowered
    assert "never answer in the language the incoming message happens to use" in lowered


def test_the_prompt_keeps_persian_and_arabic_apart_for_an_ambiguous_greeting():
    prompt = pm._AI_SYSTEM_PROMPT
    lowered = prompt.lower()
    assert "arabic" in lowered
    assert "different languages" in lowered
    assert "share one writing system" in lowered
    assert "سلام" in prompt
    assert "must get a persian reply, never an arabic one" in lowered
    # Requirement 4: no Arabic prose or Arabic-style formal phrasing.
    assert "correct persian vocabulary and grammar" in lowered
    assert "no arabic prose" in lowered
    assert "no unnecessary religious or ceremonial arabic expressions" in lowered


def test_the_prompt_keeps_the_response_contract_and_the_context_isolation():
    lowered = pm._AI_SYSTEM_PROMPT.lower()
    assert "one short reply" in lowered
    assert "return plain text only" in lowered
    assert "do not ask a question unless answering genuinely requires one" in lowered
    assert "do not repeat the incoming message back" in lowered
    assert "do not sign the reply" in lowered
    assert "appended automatically by the system" in lowered
    assert "never write, repeat or contradict them" in lowered
    # Requirement 6: still exactly the incoming message and nothing else.
    assert "no chat history" in lowered
    assert "no previous messages" in lowered
    assert "no sender name" in lowered
    assert "no identifiers" in lowered
    assert "no other context" in lowered


def test_the_ambiguous_greeting_reaches_the_provider_unchanged(db, client, monkeypatch):
    """The exact request the provider receives for the reported input."""
    import backend.ai.engine.engine as engine_module

    calls: list[dict] = []

    class _Response:
        success = True
        text = "متن پاسخ"

    class _Providers:
        async def chat(self, messages, **kwargs):
            calls.append({"messages": messages, "kwargs": kwargs})
            return _Response()

    class _Engine:
        provider_manager = _Providers()

    async def _apply(owner_id):
        return True

    monkeypatch.setattr(engine_module, "get_engine", lambda: _Engine())
    monkeypatch.setattr(engine_module, "apply_persisted_config", _apply)

    _restricted(db, ai_response_enabled=True)
    handler = _handler(client)

    _run(handler(_event(STRANGER, STRANGER, message_id=52, text="سلام")))

    assert len(calls) == 1
    assert calls[0]["kwargs"] == {"tools": []}
    messages = calls[0]["messages"]
    assert len(messages) == 2
    assert messages[0] == {"role": "system", "content": pm._AI_SYSTEM_PROMPT}
    assert messages[1] == {"role": "user", "content": "سلام"}
    # The disclosures remain the application's own post-processing.
    assert len(client.sent) == 1
    body = client.sent[0][1]
    assert body.startswith("متن پاسخ")
    assert body.endswith(f"{svc.CLOSED_NOTICE}\n{svc.AI_NOTICE}")


def test_the_language_policy_is_local_to_the_private_message_feature():
    """No other module defines or reuses this contract, so no unrelated AI chat
    behaviour is touched by the fix."""
    references = []
    for path in (REPO / "backend").rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        if "_AI_SYSTEM_PROMPT" in path.read_text(encoding="utf-8"):
            references.append(str(path.relative_to(REPO)))
    assert references == ["backend/bot/handlers/private_message.py"]


def test_the_prompt_forbids_inventing_facts_and_describing_itself():
    lowered = pm._AI_SYSTEM_PROMPT.lower()
    assert "never invent" in lowered
    for fact in ("identity", "name", "job", "religion", "nationality", "biography"):
        assert fact in lowered, fact
    assert "the incoming message does not state" in lowered
    assert "never describe yourself or your own situation" in lowered


def test_the_prompt_requires_brevity_and_natural_persian_over_translation():
    lowered = pm._AI_SYSTEM_PROMPT.lower()
    assert "keep it brief and directly about the incoming message" in lowered
    assert "a simple greeting gets a short persian greeting, nothing more" in lowered
    assert "not a word-for-word translation" in lowered
    assert "never an arabic expression" in lowered
    assert "everyday persian" in lowered


# ── the deterministic reply envelope (validation BEFORE any send) ─────────────

_PERSIAN_REPLY = "سلام! چطور می‌تونم کمکت کنم؟"
_ARABIC_REPLY = "أهلاً بك! كيف يمكنني مساعدتك اليوم؟"
_ENGLISH_REPLY = "Hello, thank you for your message."
#: A general Persian request — NOT a bare greeting, so it always takes the
#: provider path and the safe-failure contract applies to it unchanged.
_GENERAL_REQUEST = "میشه کمکم کنی؟"


def test_the_reply_validator_refuses_empty_and_whitespace_only_bodies():
    for value in ("", "   ", "\n\t ", None, 7, ["x"]):
        assert svc.validate_generated_reply(value) == ("empty", ""), value
    assert svc.compose_ai_reply("") is None
    assert svc.compose_ai_reply("   ") is None


def test_the_reply_validator_refuses_letterless_and_foreign_script_bodies():
    assert svc.validate_generated_reply("!!! 123 😀")[0] == "no_letters"
    assert svc.validate_generated_reply(_ENGLISH_REPLY)[0] == "not_persian_script"
    # A Persian sentence carrying a Latin word is still Persian: only a
    # Latin-DOMINANT body is refused.
    reason, body = svc.validate_generated_reply("سلام! این یه فایل PDF بود.")
    assert reason == "" and body == "سلام! این یه فایل PDF بود."


def test_the_reply_validator_refuses_clearly_arabic_bodies():
    for arabic in (
        _ARABIC_REPLY,
        "مرحبا بك، شكرا لك على رسالتك",
        "كيف حالك؟",
        "السلام عليكم",
        "شكرا جزيلا لك",
        "وعليكم السلام",
        "أنا هنا لمساعدتك",
    ):
        assert svc.validate_generated_reply(arabic)[0] == "arabic", arabic
        assert svc.compose_ai_reply(arabic) is None


def test_the_reply_validator_keeps_valid_persian_and_the_ambiguous_greeting():
    """Everyday Persian — including text with no Persian-only letter — must NOT
    be rejected by the shared-code-point check."""
    for persian in (
        "سلام",
        "سلام! چطور هستید؟",
        "سلام، خوبی؟",
        "ممنونم، پیام شما رسید.",
        "حتماً، الان پاسخ می‌دم.",
        "سلام! خوبم، مرسی که پیام دادی.",
        "باشه، خبرت می‌کنم.",
        "وقت بخیر، الان پاسخ می‌دم.",
        "ممنون از پیامت",
        "بله، حتماً",
        "خواهش می‌کنم.",
        "کجاست؟",
        "آقا سلام",
    ):
        reason, body = svc.validate_generated_reply(persian)
        assert reason == "", (persian, reason)
        assert body == persian
    # The shared-code-point guarantee: `سلام` is never classified as Arabic.
    assert svc.is_clearly_arabic("سلام") is False
    assert svc.is_clearly_arabic("") is False


def test_the_reply_validator_refuses_an_over_long_body():
    long_body = "سلام " + "خیلی طولانی " * 200
    assert len(long_body) > svc.MAX_AI_GENERATED_LENGTH
    assert svc.validate_generated_reply(long_body)[0] == "too_long"
    assert svc.compose_ai_reply(long_body) is None


def test_the_reply_validator_reads_only_the_generated_text():
    """No incoming text, no sender, no chat: Persian is required regardless."""
    parameters = list(inspect.signature(svc.validate_generated_reply).parameters)
    assert parameters == ["generated"]


def test_the_disclosures_are_appended_exactly_once_even_when_the_model_echoes_them():
    echoed = f"{_PERSIAN_REPLY}\n\n{svc.CLOSED_NOTICE}\n{svc.AI_NOTICE}"
    body = svc.compose_ai_reply(echoed)
    assert body is not None
    assert body.count(svc.CLOSED_NOTICE) == 1
    assert body.count(svc.AI_NOTICE) == 1
    assert body.startswith("سلام!")  # the model's own words survive untouched
    plain = svc.compose_ai_reply(_PERSIAN_REPLY)
    assert plain.count(svc.CLOSED_NOTICE) == 1 and plain.count(svc.AI_NOTICE) == 1


def test_a_body_that_is_only_the_notices_is_refused():
    reason, _ = svc.validate_generated_reply(f"{svc.CLOSED_NOTICE}\n{svc.AI_NOTICE}")
    assert reason == "notices_only"
    assert svc.compose_ai_reply(f"\n{svc.AI_NOTICE}\n") is None


def test_the_scope_gate_runs_outside_the_ai(db, client, monkeypatch):
    """A rejected event never reaches the model, with every action enabled."""
    import backend.ai.engine.engine as engine_module

    calls: list = []

    class _Providers:
        async def chat(self, messages, **kwargs):
            calls.append(messages)
            raise AssertionError("the AI must never be asked about a bot")

    class _Engine:
        provider_manager = _Providers()

    async def _apply(owner_id):
        return True

    monkeypatch.setattr(engine_module, "get_engine", lambda: _Engine())
    monkeypatch.setattr(engine_module, "apply_persisted_config", _apply)

    _restricted(db, delete_incoming=True, fixed_response_enabled=True, ai_response_enabled=True)
    _run(svc.set_fixed_response_text("closed"))
    handler = _handler(client)
    bot = _bot_entity(STRANGER)

    _run(handler(_event(STRANGER, STRANGER, chat_entity=bot, sender_entity=bot)))

    assert calls == []
    assert client.deleted == [] and client.sent == []


def test_a_message_without_text_gets_no_ai_reply_and_spends_nothing(db, client):
    _restricted(db, ai_response_enabled=True)
    handler = _handler(client)

    _run(handler(_event(STRANGER, STRANGER, message_id=52, text="")))

    assert client.sent == []
    assert db.senders == []


# ── the durable per-person allowance ─────────────────────────────────────────


def _ai_handler(db, client, monkeypatch, *, reply_text="پیام شما رسید.", fail_generation=False):
    import backend.ai.engine.engine as engine_module

    class _Response:
        success = not fail_generation
        text = reply_text

    class _Providers:
        async def chat(self, messages, **kwargs):
            return _Response()

    class _Engine:
        provider_manager = _Providers()

    async def _apply(owner_id):
        return True

    monkeypatch.setattr(engine_module, "get_engine", lambda: _Engine())
    monkeypatch.setattr(engine_module, "apply_persisted_config", _apply)
    _restricted(db, ai_response_enabled=True)
    return _handler(client)


def test_the_finite_limit_denies_after_the_configured_count(db, client, monkeypatch):
    handler = _ai_handler(db, client, monkeypatch)
    _run(svc.set_ai_max_replies_per_person(3))
    _run(svc.set_ai_interval_seconds(0))

    for message_id in range(5):
        _run(handler(_event(STRANGER, STRANGER, message_id=message_id, text="hi")))

    assert len(client.sent) == 3
    assert db.senders[0]["ai_replies_used"] == 3


def test_the_unlimited_option_ignores_the_limit(db, client, monkeypatch):
    handler = _ai_handler(db, client, monkeypatch)
    _run(svc.set_ai_max_replies_per_person(1))
    _run(svc.set_ai_unlimited_replies(True))
    _run(svc.set_ai_interval_seconds(0))

    for message_id in range(1, 5):
        _run(handler(_event(STRANGER, STRANGER, message_id=message_id, text="hi")))

    assert len(client.sent) == 4


def test_a_zero_second_interval_allows_back_to_back_replies(db, client, monkeypatch):
    handler = _ai_handler(db, client, monkeypatch)
    _run(svc.set_ai_max_replies_per_person(10))
    _run(svc.set_ai_interval_seconds(0))

    _run(handler(_event(STRANGER, STRANGER, message_id=61, text="hi")))
    _run(handler(_event(STRANGER, STRANGER, message_id=62, text="hi")))

    assert len(client.sent) == 2


def test_a_positive_interval_denies_until_it_elapses(db, client, monkeypatch):
    handler = _ai_handler(db, client, monkeypatch)
    _run(svc.set_ai_max_replies_per_person(10))
    _run(svc.set_ai_interval_seconds(600))

    _run(handler(_event(STRANGER, STRANGER, message_id=63, text="hi")))
    _run(handler(_event(STRANGER, STRANGER, message_id=64, text="hi")))
    assert len(client.sent) == 1

    # The stored instant is moved back beyond the interval: the next reply is due.
    db.senders[0]["last_ai_reply_at"] = (
        datetime.now(timezone.utc) - timedelta(seconds=601)
    ).isoformat()
    _run(handler(_event(STRANGER, STRANGER, message_id=65, text="hi")))
    assert len(client.sent) == 2


def test_the_limits_are_per_person(db, client, monkeypatch):
    handler = _ai_handler(db, client, monkeypatch)
    _run(svc.set_ai_max_replies_per_person(1))
    _run(svc.set_ai_interval_seconds(0))

    _run(handler(_event(STRANGER, STRANGER, message_id=71, text="hi")))
    _run(handler(_event(STRANGER, STRANGER, message_id=72, text="hi")))
    _run(handler(_event(OTHER, OTHER, message_id=73, text="hi")))

    assert len(client.sent) == 2
    assert {row["sender_id"] for row in db.senders} == {STRANGER, OTHER}


def test_only_a_delivered_reply_consumes_the_allowance(db, client, monkeypatch):
    import backend.ai.engine.engine as engine_module

    class _Providers:
        async def chat(self, messages, **kwargs):
            raise RuntimeError("provider down")

    class _Engine:
        provider_manager = _Providers()

    async def _apply(owner_id):
        return True

    monkeypatch.setattr(engine_module, "get_engine", lambda: _Engine())
    monkeypatch.setattr(engine_module, "apply_persisted_config", _apply)
    _restricted(db, ai_response_enabled=True)
    _run(svc.set_ai_max_replies_per_person(1))
    _run(svc.set_ai_interval_seconds(0))
    handler = _handler(client)

    _run(handler(_event(STRANGER, STRANGER, message_id=81, text="hi")))

    assert client.sent == []
    assert db.senders[0]["ai_replies_used"] == 0


def test_a_failed_delivery_gives_the_claim_back(db, client, monkeypatch):
    client.send_fails = True
    handler = _ai_handler(db, client, monkeypatch)
    _run(svc.set_ai_max_replies_per_person(1))
    _run(svc.set_ai_interval_seconds(0))

    _run(handler(_event(STRANGER, STRANGER, message_id=82, text="hi")))

    assert client.sent == []
    assert db.senders[0]["ai_replies_used"] == 0


def test_an_unreadable_counter_store_fails_closed(db, client, monkeypatch):
    handler = _ai_handler(db, client, monkeypatch)
    db.fail_counter_read = True

    _run(handler(_event(STRANGER, STRANGER, message_id=83, text="hi")))

    assert client.sent == []
    assert db.senders == []


def test_concurrent_messages_from_one_person_cannot_exceed_the_limit(db, client, monkeypatch):
    handler = _ai_handler(db, client, monkeypatch)
    _run(svc.set_ai_max_replies_per_person(1))
    _run(svc.set_ai_interval_seconds(0))

    async def _both():
        await asyncio.gather(
            handler(_event(STRANGER, STRANGER, message_id=91, text="hi")),
            handler(_event(STRANGER, STRANGER, message_id=92, text="hi")),
        )

    _run(_both())
    assert len(client.sent) == 1


def test_a_duplicate_update_is_enforced_only_once(db, client):
    _restricted(db, delete_incoming=True, fixed_response_enabled=True)
    _run(svc.set_fixed_response_text("closed"))
    handler = _handler(client)

    event = _event(STRANGER, STRANGER, message_id=93)
    _run(handler(event))

    assert client.deleted == [(STRANGER, [93])]
    assert svc.already_handled(STRANGER, 93) is True


def test_counters_are_never_reset_implicitly_and_only_the_owner_resets_them(db, client, monkeypatch):
    handler = _ai_handler(db, client, monkeypatch)
    _run(svc.set_ai_max_replies_per_person(1))
    _run(svc.set_ai_interval_seconds(0))

    _run(handler(_event(STRANGER, STRANGER, message_id=95, text="hi")))
    assert db.senders[0]["ai_replies_used"] == 1

    # A restart (memory dropped, state re-read) keeps the durable count.
    svc.reset_in_memory()
    svc.configure_owner(OWNER)
    _run(svc.ensure_loaded_async())
    assert _run(svc.counter_rows(OWNER))[0]["ai_replies_used"] == 1

    _run(handler(_event(STRANGER, STRANGER, message_id=96, text="hi")))
    assert len(client.sent) == 1  # still denied

    assert _run(svc.reset_counters(OWNER)) is True
    assert db.senders == []
    _run(handler(_event(STRANGER, STRANGER, message_id=97, text="hi")))
    assert len(client.sent) == 2


# ── failure semantics ────────────────────────────────────────────────────────


def test_a_failed_delete_does_not_suppress_the_responses(db, client):
    client.delete_fails = True
    _restricted(db, delete_incoming=True, fixed_response_enabled=True)
    _run(svc.set_fixed_response_text("closed"))
    handler = _handler(client)

    _run(handler(_event(STRANGER, STRANGER, message_id=101)))

    assert client.deleted == []
    assert client.sent == [(STRANGER, "closed")]


def test_a_failed_fixed_response_still_leaves_the_message_stopped(db, client):
    client.send_fails = True
    _restricted(db, fixed_response_enabled=True)
    _run(svc.set_fixed_response_text("closed"))
    handler = _handler(client)

    _run(handler(_event(STRANGER, STRANGER, message_id=102)))

    assert client.sent == []


# ── architecture: no second client, loop, scheduler or executor ──────────────


def test_no_second_client_loop_scheduler_or_executor_is_introduced():
    source = inspect.getsource(pm)
    for forbidden in (
        "TelegramClient",
        "asyncio.create_task",
        "asyncio.new_event_loop",
        "while True",
        "TaskScheduler",
        "ToolExecutor",
        "Dispatcher(",
        "forward_messages",
    ):
        assert forbidden not in source, forbidden
    # This module adds NO listener of its own: it is a hook on the ONE existing
    # all-messages handler, so there is no second update loop to leak.
    assert "events.NewMessage" not in source


def test_one_incoming_message_has_exactly_one_action_path():
    """Structural guarantee behind the single-reply contract: the enforcement
    has ONE action entry point, ONE AI-response step and ONE generation call, so
    a single verified update cannot fan out into several replies.
    """
    source = inspect.getsource(pm)
    assert source.count("await _apply_actions(") == 1
    assert source.count("await _ai_response(") == 1
    assert source.count("await _generate_ai_reply(") == 1
    assert source.count("service.already_handled(") == 1
    assert source.count("await _send_text(") == 2  # one fixed response, one AI reply


def test_the_incoming_hook_is_reached_through_the_existing_handler(client):
    """The ONE existing all-messages handler calls the enforcement hook first."""
    handler = _handler(client)
    assert inspect.iscoroutinefunction(handler)
    source = inspect.getsource(task_events)
    assert "enforce_incoming" in source
    assert source.index("enforce_incoming") < source.index("extract_event_context")


# ── UI: registry symbols only, no emoji literals ─────────────────────────────


def test_the_panel_uses_the_symbol_registry_and_no_emoji_literals():
    from backend.helper import symbols as sym

    source = inspect.getsource(pm)
    for glyph in (sym.STATE_WARNING, sym.ICON_SELECT, sym.ICON_STATS, sym.OP_EDIT):
        assert glyph not in source, "a glyph must come from the registry, not a literal"


# ── the migration contract ───────────────────────────────────────────────────


def test_the_migration_creates_exactly_the_two_feature_tables_and_is_additive():
    sql = MIGRATION.read_text(encoding="utf-8")
    # The header's rollback text is documentation, not an executed statement;
    # only the executable SQL is inspected.
    body = re.sub(r"/\*.*?\*/", "", sql, flags=re.S)
    executable = "\n".join(
        line for line in body.splitlines() if not line.strip().startswith("--")
    )
    assert "CREATE TABLE IF NOT EXISTS private_message_config" in executable
    assert "CREATE TABLE IF NOT EXISTS private_message_senders" in executable
    for forbidden in ("DROP TABLE", "DROP COLUMN", "TRUNCATE", "DELETE FROM"):
        assert forbidden not in executable, forbidden
    assert executable.count("CREATE TABLE") == 2
    assert "PRIMARY KEY (owner_id, sender_id)" in executable
    assert "ENABLE ROW LEVEL SECURITY" in executable
    assert "secondary tables" not in executable


def test_the_migration_columns_cover_every_configured_setting():
    sql = MIGRATION.read_text(encoding="utf-8")
    for column in (
        "enabled", "mode", "delete_incoming", "fixed_response_enabled",
        "fixed_response_text", "ai_response_enabled", "ai_max_replies_per_person",
        "ai_unlimited_replies", "ai_interval_seconds", "allowlist", "exceptions",
        "temporary", "schedule", "ai_replies_used", "last_ai_reply_at",
    ):
        assert column in sql, column
    # The limits the panel enforces mirror the table's own constraints.
    assert f"CHECK (ai_max_replies_per_person BETWEEN 1 AND {svc.MAX_AI_REPLIES_PER_PERSON})" in sql
    assert f"CHECK (ai_interval_seconds BETWEEN 0 AND {svc.MAX_AI_INTERVAL_SECONDS})" in sql
    assert f"CHECK (length(fixed_response_text) <= {svc.MAX_FIXED_RESPONSE_LENGTH})" in sql


def test_the_feature_documentation_exists_and_states_the_manual_action():
    doc = (REPO / "DATABASE_ARCHITECTURE.md").read_text(encoding="utf-8")
    assert "## 33. Private Message Management Persistence" in doc
    assert "20260929000001_create_private_message_tables.sql" in doc
    assert "20261016000001_add_private_message_counter_reset.sql" in doc
    assert "part 10 of 13" in doc
    assert "part 11 of 13" in doc
    assert "part 13 of 13" in doc
    assert "20261020144423_harden_private_message_table_access.sql" in doc
    assert "private_message_senders" in doc


# ── duplicate protection, end to end ─────────────────────────────────────────


def _counting_ai_handler(db, client, monkeypatch, *, reply_text=_PERSIAN_REPLY):
    """The end-to-end harness with an observable provider-call count."""
    import backend.ai.engine.engine as engine_module

    calls: list[dict] = []

    class _Response:
        success = True
        text = reply_text

    class _Providers:
        async def chat(self, messages, **kwargs):
            calls.append({"messages": messages, "kwargs": kwargs})
            return _Response()

    class _Engine:
        provider_manager = _Providers()

    async def _apply(owner_id):
        return True

    monkeypatch.setattr(engine_module, "get_engine", lambda: _Engine())
    monkeypatch.setattr(engine_module, "apply_persisted_config", _apply)
    _restricted(db, ai_response_enabled=True)
    _run(svc.set_ai_max_replies_per_person(10))
    _run(svc.set_ai_interval_seconds(0))
    return _handler(client), calls


def test_a_simple_greeting_is_answered_once_in_persian_with_one_copy_of_each_notice(db, client, monkeypatch):
    handler, calls = _counting_ai_handler(db, client, monkeypatch)

    _run(handler(_event(STRANGER, STRANGER, message_id=110, text="سلام")))

    assert len(calls) == 1
    assert calls[0]["messages"][1]["content"] == "سلام"
    assert len(client.sent) == 1
    body = client.sent[0][1]
    assert body == f"{_PERSIAN_REPLY}\n\n{svc.CLOSED_NOTICE}\n{svc.AI_NOTICE}"
    assert body.count(svc.CLOSED_NOTICE) == 1 and body.count(svc.AI_NOTICE) == 1


def test_the_incoming_language_never_changes_the_contract(db, client, monkeypatch):
    """Arabic and English inputs are answered by the SAME Persian contract."""
    handler, calls = _counting_ai_handler(db, client, monkeypatch)

    _run(handler(_event(STRANGER, STRANGER, message_id=111, text="مرحبا كيف حالك؟")))
    _run(handler(_event(STRANGER, STRANGER, message_id=112, text="hello")))

    assert [call["messages"][1]["content"] for call in calls] == [
        "مرحبا كيف حالك؟", "hello",
    ]
    assert all(call["messages"][0]["content"] == pm._AI_SYSTEM_PROMPT for call in calls)
    assert all(call["kwargs"] == {"tools": []} for call in calls)
    assert len(client.sent) == 2


def test_an_arabic_or_english_reply_is_never_sent_and_the_claim_is_given_back(db, monkeypatch):
    # The incoming text is deliberately NOT a bare greeting: a bare greeting is
    # answered deterministically (see the Persian-quality section), so the
    # safe-failure contract is pinned on a general request instead.
    for message_id, reply in ((113, _ARABIC_REPLY), (114, _ENGLISH_REPLY)):
        client = _RecordingClient()
        handler, _calls = _counting_ai_handler(db, client, monkeypatch, reply_text=reply)

        _run(handler(_event(STRANGER, STRANGER, message_id=message_id, text=_GENERAL_REQUEST)))

        assert client.sent == [], reply
        assert all(row.get("ai_replies_used", 0) == 0 for row in db.senders), reply


def test_an_over_long_reply_takes_the_safe_failure_path_and_releases_the_claim(db, client, monkeypatch):
    handler, _calls = _counting_ai_handler(
        db, client, monkeypatch, reply_text="سلام " + "خیلی طولانی " * 200
    )

    _run(handler(_event(STRANGER, STRANGER, message_id=115, text=_GENERAL_REQUEST)))

    assert client.sent == []
    assert all(row.get("ai_replies_used", 0) == 0 for row in db.senders)


def test_a_duplicate_delivery_of_one_message_answers_once(db, client, monkeypatch):
    handler, calls = _counting_ai_handler(db, client, monkeypatch)

    _run(handler(_event(STRANGER, STRANGER, message_id=120, text="سلام")))
    _run(handler(_event(STRANGER, STRANGER, message_id=120, text="سلام")))

    assert len(calls) == 1
    assert len(client.sent) == 1


def test_concurrent_deliveries_of_one_message_answer_once(db, client, monkeypatch):
    handler, calls = _counting_ai_handler(db, client, monkeypatch)

    async def _both():
        await asyncio.gather(
            handler(_event(STRANGER, STRANGER, message_id=121, text="سلام")),
            handler(_event(STRANGER, STRANGER, message_id=121, text="سلام")),
        )

    _run(_both())
    assert len(calls) == 1
    assert len(client.sent) == 1


def test_a_different_message_from_the_same_sender_is_still_answered(db, client, monkeypatch):
    handler, calls = _counting_ai_handler(db, client, monkeypatch)

    _run(handler(_event(STRANGER, STRANGER, message_id=122, text="سلام")))
    _run(handler(_event(STRANGER, STRANGER, message_id=123, text="سلام دوباره")))

    assert len(calls) == 2
    assert len(client.sent) == 2


def test_both_independent_responses_run_once_for_one_message(db, client, monkeypatch):
    """The duplicate guard suppresses a REPEAT, never two separate actions."""
    handler, calls = _counting_ai_handler(db, client, monkeypatch)
    assert _run(svc.set_action(svc.ACTION_FIXED, True)) is True
    _run(svc.set_fixed_response_text("این پی‌وی بسته است."))

    _run(handler(_event(STRANGER, STRANGER, message_id=124, text="سلام")))
    _run(handler(_event(STRANGER, STRANGER, message_id=124, text="سلام")))

    assert [text for _chat, text in client.sent] == [
        "این پی‌وی بسته است.",
        f"{_PERSIAN_REPLY}\n\n{svc.CLOSED_NOTICE}\n{svc.AI_NOTICE}",
    ]
    assert len(calls) == 1


def test_a_failure_before_the_actions_does_not_remember_the_message_as_handled(db, client, monkeypatch):
    _restricted(db, ai_response_enabled=True)
    handler = _handler(client)

    async def _boom(*args, **kwargs):
        raise RuntimeError("enforcement exploded")

    monkeypatch.setattr(pm, "_apply_actions", _boom)

    _run(handler(_event(STRANGER, STRANGER, message_id=125, text="سلام")))

    # The actions never ran, so the pair must NOT be remembered as handled.
    assert svc.already_handled(STRANGER, 125) is False


def test_a_completed_enforcement_keeps_the_duplicate_mark(db, client):
    _restricted(db, delete_incoming=True)
    handler = _handler(client)

    _run(handler(_event(STRANGER, STRANGER, message_id=126)))
    _run(handler(_event(STRANGER, STRANGER, message_id=126)))

    assert client.deleted == [(STRANGER, [126])]
    assert svc.already_handled(STRANGER, 126) is True


def test_the_accounts_own_sent_reply_is_never_an_incoming_human_message(db, client, monkeypatch):
    """The AI reply is sent BY the account, so its own update is outgoing and
    can never re-enter the incoming policy path."""
    handler, calls = _counting_ai_handler(db, client, monkeypatch)

    _run(handler(_event(
        STRANGER, OWNER, message_id=127, text=_PERSIAN_REPLY, out=True,
    )))

    assert calls == []
    assert client.sent == [] and client.deleted == []
    assert db.senders == []


class _StaleReadDB:
    """``private_message_senders`` whose first ``stale_reads`` SELECTs return the
    state as it was BEFORE any claim — a second process that read the row before
    the first process wrote it — while every conditional UPDATE matches against
    the LIVE rows, which is exactly PostgREST's semantics (an ``update`` returns
    only the rows its filters matched).
    """

    def __init__(self, senders, stale_reads: int = 1):
        self.senders = [dict(row) for row in senders]
        self._snapshot = [dict(row) for row in senders]
        self._stale_reads = int(stale_reads)
        self._reads = 0
        self._op = None
        self._payload = None
        self._filters: list[tuple[str, object]] = []

    def table(self, name):
        assert name == "private_message_senders", name
        self._op, self._payload = None, None
        self._filters = []
        return self

    def select(self, columns="*"):
        self._op = "select"
        return self

    def insert(self, payload):
        self._op, self._payload = "insert", payload
        return self

    def update(self, payload):
        self._op, self._payload = "update", payload
        return self

    def eq(self, column, value):
        self._filters.append((column, value))
        return self

    def limit(self, count):
        return self

    def execute(self):
        if self._op == "select":
            self._reads += 1
            source = self._snapshot if self._reads <= self._stale_reads else self.senders
            return _Result([dict(row) for row in source])
        if self._op == "insert":
            self.senders.append(dict(self._payload))
            return _Result([dict(self._payload)])
        wanted = dict(self._filters)
        matched = [
            row for row in self.senders
            if all(row.get(key) == value for key, value in wanted.items())
        ]
        for row in matched:
            row.update(self._payload)
        return _Result([dict(row) for row in matched])


def _mutable_ai_handler(db, client, monkeypatch):
    """The end-to-end harness whose provider body can be swapped between
    messages, so a failure and the following success use the SAME path."""
    import backend.ai.engine.engine as engine_module

    state = {"body": _PERSIAN_REPLY, "calls": 0}

    class _Response:
        success = True

        @property
        def text(self):
            return state["body"]

    class _Providers:
        async def chat(self, messages, **kwargs):
            state["calls"] += 1
            return _Response()

    class _Engine:
        provider_manager = _Providers()

    async def _apply(owner_id):
        return True

    monkeypatch.setattr(engine_module, "get_engine", lambda: _Engine())
    monkeypatch.setattr(engine_module, "apply_persisted_config", _apply)
    _restricted(db, ai_response_enabled=True)
    _run(svc.set_ai_max_replies_per_person(10))
    _run(svc.set_ai_interval_seconds(0))
    return _handler(client), state


# ── Persian reply quality: the reported defect and the two halves of the fix ──
#
# What deterministic code enforces here is the message SHAPE plus the ONE
# deterministic greeting answer. Fluency of an arbitrary generated sentence is
# NOT provable offline — these tests pin what is genuinely guaranteed and never
# claim more.

#: The EXACT body the owner reported as unacceptable. It is Persian script and
#: passed every script-level check the previous fix had, which is why the defect
#: survived: `می توانم` writes the imperfective prefix as its own word, and
#: `من در خدمت شما هستم` is a self-descriptive ceremonial opener.
REPORTED_MALFORMED_REPLY = (
    "سلام، من در خدمت شما هستم. لطفاً حرفی به شما میادی میکنم. "
    "چگونه می توانم به شما کمک کنم؟"
)


def test_the_reported_malformed_persian_reply_is_refused():
    """The exact reported output must be refused, and refused for the REAL
    reason — not as a side effect of a script or length check."""
    assert any("\u0600" <= ch <= "\u06ff" for ch in REPORTED_MALFORMED_REPLY)
    assert svc.is_clearly_arabic(REPORTED_MALFORMED_REPLY) is False
    assert len(REPORTED_MALFORMED_REPLY) < svc.MAX_AI_GENERATED_LENGTH

    reason, body = svc.validate_generated_reply(REPORTED_MALFORMED_REPLY)
    assert reason == "malformed_persian_prefix"
    assert body == ""
    assert svc.compose_ai_reply(REPORTED_MALFORMED_REPLY) is None


def test_a_detached_persian_verb_prefix_is_refused_but_attached_forms_are_not():
    for malformed in ("چگونه می توانم کمکت کنم؟", "نمی دانم.", "من می توانم"):
        assert svc.validate_generated_reply(malformed)[0] == "malformed_persian_prefix", malformed
    assert svc.detached_verb_prefixes("چگونه می توانم") == ("می",)
    # A ZWNJ keeps the prefix inside ONE token, so correct Persian is never hit.
    assert svc.detached_verb_prefixes("می‌تونم") == ()
    assert svc.detached_verb_prefixes("می‌کنم") == ()
    for correct in ("می‌تونم کمکت کنم.", "نمی‌دونم.", "می‌کنم"):
        assert svc.validate_generated_reply(correct)[0] == "", correct


def test_a_reply_that_greets_twice_is_refused():
    assert svc.repeated_greeting_words("سلام! سلام، خوبی؟") == ("سلام",)
    assert svc.validate_generated_reply("سلام! سلام، خوبی؟")[0] == "repetitive_greeting"
    assert svc.repeated_greeting_words("سلام! وقت شما هم بخیر.") == ()
    assert svc.validate_generated_reply("سلام! وقت شما هم بخیر.")[0] == ""


def test_only_a_bare_greeting_is_classified_as_a_trivial_greeting():
    for bare in ("سلام", "  سلام!  ", "سلام؟", "درود", "hello", "Hi There", "وقت بخیر"):
        assert svc.classify_trivial_greeting(bare) is not None, bare
    # Arabic keyboard spellings of the same greeting match after folding.
    assert svc.classify_trivial_greeting("السلام عليكم") is not None
    for not_bare in (
        "", "   ", None, 7, ["سلام"],
        "سلام خوبی؟",              # a greeting PLUS a question
        "سلام، پیامم رو دیدی؟",     # extra content
        _GENERAL_REQUEST,
        "سلام " + "و " * 5,        # too many words
        "س" * 80,                  # too long
        "سلام سلام سلام سلام سلام دوباره",
    ):
        assert svc.classify_trivial_greeting(not_bare) is None, not_bare
    assert svc.trivial_greeting_body(svc.classify_trivial_greeting("صبح بخیر")) == (
        "سلام! صبح شما هم بخیر."
    )


def test_the_deterministic_greeting_is_short_natural_and_carries_both_disclosures_once():
    for probe in ("سلام", "صبح بخیر", "شب بخیر"):
        kind = svc.classify_trivial_greeting(probe)
        body = svc.compose_trivial_greeting_reply(kind)
        assert body is not None
        assert body.count(svc.CLOSED_NOTICE) == 1 and body.count(svc.AI_NOTICE) == 1
        assert body.endswith(f"{svc.CLOSED_NOTICE}\n{svc.AI_NOTICE}")
        spoken = body.split("\n")[0]
        assert len(spoken) <= svc.MAX_TRIVIAL_GREETING_REPLY_LENGTH
        assert svc.validate_generated_reply(spoken) == ("", spoken)
        # Nothing about the owner or the assistant is invented: no first-person
        # self-description, no claim of identity, role, ability or biography.
        assert "من " not in spoken
        for forbidden in ("هستم", "کارمند", "معلم", "مسلمان", "متولد"):
            assert forbidden not in spoken, forbidden


def test_the_prompt_carries_idiomatic_few_shot_examples_without_weakening_the_contract():
    """The examples are part of the SAME system contract (never extra messages),
    and every previous requirement is still stated."""
    prompt = pm._AI_SYSTEM_PROMPT
    for example in (
        '"سلام" → "سلام! وقت شما هم بخیر."',
        '"ممنون از پیامت" → "خواهش می\u200cکنم."',
        '"thanks a lot" → "خواهش می\u200cکنم."',
        '"سلام، خوبی؟" → "سلام! ممنون از پیامتون، امیدوارم شما هم خوب باشید."',
        '"بعداً صحبت می\u200cکنیم" → "باشه، حتماً."',
    ):
        assert example in prompt, example
    lowered = prompt.lower()
    assert "examples, never templates" in lowered
    assert "never copy one verbatim" in lowered
    # The greeting example must be answered by Persian even when the message is
    # written in another language.
    assert "still answered in persian" in lowered
    assert "never invent" in lowered
    # The repair instruction is a SEPARATE constant, so the first attempt always
    # sends exactly the base contract.
    assert pm._AI_CORRECTION_PROMPT not in prompt
    assert "never write the imperfective prefix" in pm._AI_CORRECTION_PROMPT.lower()
    assert svc.AI_GENERATION_ATTEMPTS == 2


def test_the_reported_malformed_reply_makes_a_bare_greeting_get_the_short_greeting(db, client, monkeypatch):
    """The reported case, end to end: the provider is still asked through the
    unchanged boundary, its broken body is never delivered, and the person gets
    one short idiomatic Persian greeting instead of broken text."""
    handler, calls = _counting_ai_handler(
        db, client, monkeypatch, reply_text=REPORTED_MALFORMED_REPLY
    )

    _run(handler(_event(STRANGER, STRANGER, message_id=130, text="سلام")))

    assert len(calls) == 1
    assert calls[0]["kwargs"] == {"tools": []}
    assert [message["role"] for message in calls[0]["messages"]] == ["system", "user"]
    assert calls[0]["messages"][0]["content"] == pm._AI_SYSTEM_PROMPT
    assert calls[0]["messages"][1]["content"] == "سلام"

    assert len(client.sent) == 1
    delivered = client.sent[0][1]
    assert delivered == svc.compose_trivial_greeting_reply(
        svc.classify_trivial_greeting("سلام")
    )
    assert REPORTED_MALFORMED_REPLY not in delivered
    assert "میادی" not in delivered
    assert db.senders[0]["ai_replies_used"] == 1  # a delivered reply consumes one


def test_an_elaborate_but_valid_reply_to_a_greeting_is_replaced_by_the_short_greeting(db, client, monkeypatch):
    """A greeting gets a greeting, not a self-describing paragraph — even when
    the paragraph is grammatical enough to pass every shape check."""
    elaborate = "سلام " + "خیلی ممنون که پیام دادید و امیدوارم حالتان خوب باشد " * 3
    assert svc.validate_generated_reply(elaborate)[0] == ""
    assert len(elaborate.split("\n")[0]) > svc.MAX_TRIVIAL_GREETING_REPLY_LENGTH
    handler, _calls = _counting_ai_handler(db, client, monkeypatch, reply_text=elaborate)

    _run(handler(_event(STRANGER, STRANGER, message_id=131, text="سلام")))

    assert len(client.sent) == 1
    assert client.sent[0][1].startswith("سلام! وقت شما هم بخیر.")
    assert "خیلی ممنون که پیام دادید" not in client.sent[0][1]


def test_a_provider_failure_never_produces_a_canned_reply(db, client, monkeypatch):
    """The deterministic greeting is a FALLBACK FOR A BAD BODY, never a
    substitute for a failed generation: an unavailable provider still takes the
    ordinary safe-failure path and releases the claim."""
    import backend.ai.engine.engine as engine_module

    class _Providers:
        async def chat(self, messages, **kwargs):
            raise RuntimeError("provider down")

    class _Engine:
        provider_manager = _Providers()

    async def _apply(owner_id):
        return True

    monkeypatch.setattr(engine_module, "get_engine", lambda: _Engine())
    monkeypatch.setattr(engine_module, "apply_persisted_config", _apply)
    _restricted(db, ai_response_enabled=True)
    _run(svc.set_ai_max_replies_per_person(10))
    _run(svc.set_ai_interval_seconds(0))
    handler = _handler(client)

    _run(handler(_event(STRANGER, STRANGER, message_id=132, text="سلام")))

    assert client.sent == []
    assert all(row.get("ai_replies_used", 0) == 0 for row in db.senders)


def test_a_refused_body_for_a_general_request_is_regenerated_once_then_dropped(db, client, monkeypatch):
    """Bounded regeneration: exactly ONE retry, through the SAME provider path
    and the SAME context isolation, and nothing is sent when it fails too."""
    handler, calls = _counting_ai_handler(db, client, monkeypatch, reply_text=_ENGLISH_REPLY)

    _run(handler(_event(STRANGER, STRANGER, message_id=133, text=_GENERAL_REQUEST)))

    assert len(calls) == 2  # bounded: never a third attempt
    retry = calls[1]
    assert retry["kwargs"] == {"tools": []}
    assert [message["role"] for message in retry["messages"]] == ["system", "user"]
    assert retry["messages"][0]["content"] == pm._AI_SYSTEM_PROMPT + pm._AI_CORRECTION_PROMPT
    assert retry["messages"][1]["content"] == _GENERAL_REQUEST
    joined = json.dumps(retry["messages"], ensure_ascii=False)
    assert str(OWNER) not in joined and str(STRANGER) not in joined

    assert client.sent == []
    assert all(row.get("ai_replies_used", 0) == 0 for row in db.senders)


def test_the_bounded_regeneration_delivers_a_good_second_attempt(db, client, monkeypatch):
    """The retry is a real repair path, not a no-op: a good second body ships."""
    import backend.ai.engine.engine as engine_module

    bodies = iter([REPORTED_MALFORMED_REPLY, "ممنون از پیامتون."])

    class _Response:
        success = True

        def __init__(self, text):
            self.text = text

    class _Providers:
        async def chat(self, messages, **kwargs):
            return _Response(next(bodies))

    class _Engine:
        provider_manager = _Providers()

    async def _apply(owner_id):
        return True

    monkeypatch.setattr(engine_module, "get_engine", lambda: _Engine())
    monkeypatch.setattr(engine_module, "apply_persisted_config", _apply)
    _restricted(db, ai_response_enabled=True)
    _run(svc.set_ai_max_replies_per_person(10))
    _run(svc.set_ai_interval_seconds(0))
    handler = _handler(client)

    _run(handler(_event(STRANGER, STRANGER, message_id=134, text=_GENERAL_REQUEST)))

    assert [text for _chat, text in client.sent] == [
        f"ممنون از پیامتون.\n\n{svc.CLOSED_NOTICE}\n{svc.AI_NOTICE}"
    ]
    assert db.senders[0]["ai_replies_used"] == 1


def test_the_disclosures_land_exactly_once_on_every_delivery_path(db, monkeypatch):
    for message_id, text, reply in (
        (135, "سلام", _PERSIAN_REPLY),           # the model's own short body
        (136, "سلام", REPORTED_MALFORMED_REPLY),  # the deterministic greeting
        (137, _GENERAL_REQUEST, _PERSIAN_REPLY),  # a general request
    ):
        client = _RecordingClient()   # one registration per fresh client
        handler, _calls = _counting_ai_handler(db, client, monkeypatch, reply_text=reply)
        _run(handler(_event(STRANGER, STRANGER, message_id=message_id, text=text)))
        assert len(client.sent) == 1, (message_id, text)
        delivered = client.sent[0][1]
        assert delivered.count(svc.CLOSED_NOTICE) == 1, message_id
        assert delivered.count(svc.AI_NOTICE) == 1, message_id
        assert delivered.endswith(f"{svc.CLOSED_NOTICE}\n{svc.AI_NOTICE}"), message_id


# ── the configurable automatic per-person reset ──────────────────────────────


def _quota_handler(db, client, monkeypatch, *, max_replies=1, interval=0, reset=None):
    """The end-to-end harness with the quota configured (reset optional)."""
    handler, calls = _counting_ai_handler(db, client, monkeypatch)
    _run(svc.set_ai_max_replies_per_person(max_replies))
    _run(svc.set_ai_interval_seconds(interval))
    if reset is not None:
        assert _run(svc.set_ai_counter_reset_enabled(True)) is True
        assert _run(svc.set_ai_counter_reset_seconds(reset)) is True
    return handler, calls


def _age_window(db, sender_id: int, seconds: int) -> str:
    """Move ONE person's stored window start into the past (a real elapsed
    window) and return the instant it now holds."""
    aged = (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()
    for row in db.senders:
        if row.get("sender_id") == sender_id:
            row["window_started_at"] = aged
    return aged


def test_automatic_reset_is_off_by_default_and_nothing_is_ever_rolled(db, client, monkeypatch):
    handler, _calls = _quota_handler(db, client, monkeypatch, max_replies=1)
    assert svc.state()["ai_counter_reset_enabled"] is False

    _run(handler(_event(STRANGER, STRANGER, message_id=140, text=_GENERAL_REQUEST)))
    assert len(client.sent) == 1
    assert db.senders[0]["ai_replies_used"] == 1
    assert db.senders[0]["window_started_at"] is not None  # durable anchor

    _age_window(db, STRANGER, 30 * 86400)
    _run(handler(_event(STRANGER, STRANGER, message_id=141, text=_GENERAL_REQUEST)))
    assert len(client.sent) == 1                            # still denied
    assert db.senders[0]["ai_replies_used"] == 1


def test_the_first_reply_starts_the_window_and_the_limit_holds_inside_it(db, client, monkeypatch):
    handler, _calls = _quota_handler(db, client, monkeypatch, max_replies=2, reset=86400)

    _run(handler(_event(STRANGER, STRANGER, message_id=150, text=_GENERAL_REQUEST)))
    first_window = db.senders[0]["window_started_at"]
    assert db.senders[0]["ai_replies_used"] == 1

    _run(handler(_event(STRANGER, STRANGER, message_id=151, text=_GENERAL_REQUEST)))
    assert len(client.sent) == 2
    assert db.senders[0]["window_started_at"] == first_window  # not restarted per reply

    _run(handler(_event(STRANGER, STRANGER, message_id=152, text=_GENERAL_REQUEST)))
    assert len(client.sent) == 2                               # the ceiling holds
    assert db.senders[0]["window_started_at"] == first_window


def test_the_same_person_becomes_eligible_again_only_after_the_window_expires(db, client, monkeypatch):
    handler, _calls = _quota_handler(db, client, monkeypatch, max_replies=1, reset=3600)

    _run(handler(_event(STRANGER, STRANGER, message_id=160, text=_GENERAL_REQUEST)))
    first_window = db.senders[0]["window_started_at"]
    _age_window(db, STRANGER, 3599)                            # one second short
    _run(handler(_event(STRANGER, STRANGER, message_id=161, text=_GENERAL_REQUEST)))
    assert len(client.sent) == 1
    assert db.senders[0]["window_started_at"] != first_window  # the age is what we set
    assert db.senders[0]["ai_replies_used"] == 1

    _age_window(db, STRANGER, 3601)                            # just past it
    _run(handler(_event(STRANGER, STRANGER, message_id=162, text=_GENERAL_REQUEST)))
    assert len(client.sent) == 2                               # available again
    assert db.senders[0]["ai_replies_used"] == 1               # the COUNT restarted
    assert db.senders[0]["window_started_at"] != first_window

    _run(handler(_event(STRANGER, STRANGER, message_id=163, text=_GENERAL_REQUEST)))
    assert len(client.sent) == 2                               # the new window limits again


def test_two_people_keep_independent_reset_windows(db, client, monkeypatch):
    handler, _calls = _quota_handler(db, client, monkeypatch, max_replies=1, reset=3600)

    _run(handler(_event(STRANGER, STRANGER, message_id=170, text=_GENERAL_REQUEST)))
    _run(handler(_event(OTHER, OTHER, message_id=171, text=_GENERAL_REQUEST)))
    assert len(client.sent) == 2

    _age_window(db, STRANGER, 3601)   # only STRANGER's window expired
    _run(handler(_event(STRANGER, STRANGER, message_id=172, text=_GENERAL_REQUEST)))
    assert len(client.sent) == 3
    _run(handler(_event(OTHER, OTHER, message_id=173, text=_GENERAL_REQUEST)))
    assert len(client.sent) == 3      # the other person is untouched

    windows = {row["sender_id"]: row["window_started_at"] for row in db.senders}
    assert set(windows) == {STRANGER, OTHER}


def test_a_manual_reset_clears_the_counts_and_the_windows(db, client, monkeypatch):
    handler, _calls = _quota_handler(db, client, monkeypatch, max_replies=1, reset=86400)
    _run(handler(_event(STRANGER, STRANGER, message_id=180, text=_GENERAL_REQUEST)))
    _run(handler(_event(OTHER, OTHER, message_id=181, text=_GENERAL_REQUEST)))
    assert len(db.senders) == 2

    assert _run(svc.reset_counters(OWNER)) is True
    assert db.senders == []                     # counts AND windows are gone

    _run(handler(_event(STRANGER, STRANGER, message_id=182, text=_GENERAL_REQUEST)))
    _run(handler(_event(OTHER, OTHER, message_id=183, text=_GENERAL_REQUEST)))
    assert len(client.sent) == 4                # both people start fresh windows
    assert len(db.senders) == 2


def test_unlimited_mode_bypasses_the_ceiling_but_keeps_the_minimum_interval(db, client, monkeypatch):
    handler, _calls = _quota_handler(db, client, monkeypatch, max_replies=1, interval=0, reset=60)
    assert _run(svc.set_ai_unlimited_replies(True)) is True

    _run(handler(_event(STRANGER, STRANGER, message_id=190, text=_GENERAL_REQUEST)))
    _run(handler(_event(STRANGER, STRANGER, message_id=191, text=_GENERAL_REQUEST)))
    assert len(client.sent) == 2                   # the count ceiling is gone
    assert db.senders[0]["ai_replies_used"] == 2

    # The minimum interval is a SEPARATE floor: unlimited never disables it, and
    # an expired window does not shorten it either — it is read from the last
    # DELIVERED reply, independently of the window.
    assert _run(svc.set_ai_interval_seconds(600)) is True
    _age_window(db, STRANGER, 3600)
    _run(handler(_event(STRANGER, STRANGER, message_id=192, text=_GENERAL_REQUEST)))
    assert len(client.sent) == 2
    assert db.senders[0]["ai_replies_used"] == 2   # a denied claim writes nothing


def test_an_out_of_scope_event_never_creates_or_rolls_a_reset_window(db, client, monkeypatch):
    """The scope gate precedes the claim, so a bot, a group or an unresolvable
    sender can neither create a window nor roll an expired one — even with
    automatic reset widened in the configuration."""
    handler, calls = _quota_handler(db, client, monkeypatch, max_replies=1, reset=60)
    expired = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    db.senders.append({
        "owner_id": OWNER, "sender_id": STRANGER, "ai_replies_used": 1,
        "last_ai_reply_at": expired, "window_started_at": expired,
    })
    bot = _bot_entity(STRANGER)

    _run(handler(_event(STRANGER, STRANGER, message_id=230, chat_entity=bot, sender_entity=bot)))
    _run(handler(_event(-1001234567890, STRANGER, message_id=231, is_private=False, is_group=True)))

    assert calls == []
    assert client.sent == [] and client.deleted == []
    assert db.senders[0]["ai_replies_used"] == 1          # nothing was claimed
    assert db.senders[0]["window_started_at"] == expired  # nothing was rolled


def test_a_counter_row_that_predates_the_window_column_is_not_silently_forgiven(db, client, monkeypatch):
    """A counter row that already sits at its ceiling with NO recorded window has
    no counted reply to anchor a window to, so enabling automatic reset does not
    hand it a fresh allowance: the window is written by the first COUNTED reply
    and cannot expire before one exists. This is a deliberate fail-closed choice
    (the feature never grants an allowance it cannot justify) and it is stated in
    the migration header; the owner's manual reset stays the escape hatch.
    """
    handler, _calls = _quota_handler(db, client, monkeypatch, max_replies=1, reset=60)
    db.senders.append({
        "owner_id": OWNER, "sender_id": STRANGER, "ai_replies_used": 1,
        "last_ai_reply_at": (datetime.now(timezone.utc) - timedelta(days=30)).isoformat(),
        "window_started_at": None,
    })

    _run(handler(_event(STRANGER, STRANGER, message_id=240, text=_GENERAL_REQUEST)))
    assert client.sent == []
    assert db.senders[0]["ai_replies_used"] == 1
    assert db.senders[0]["window_started_at"] is None   # nothing was rolled

    assert _run(svc.reset_counters(OWNER)) is True      # the documented escape hatch
    _run(handler(_event(STRANGER, STRANGER, message_id=241, text=_GENERAL_REQUEST)))
    assert len(client.sent) == 1
    assert db.senders[0]["window_started_at"] is not None  # the live window starts here


def test_a_counter_store_failure_at_a_reset_boundary_fails_closed(db, client, monkeypatch):
    handler, _calls = _quota_handler(db, client, monkeypatch, max_replies=1, reset=60)
    db.senders.append({
        "owner_id": OWNER, "sender_id": STRANGER, "ai_replies_used": 1,
        "last_ai_reply_at": (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(),
        "window_started_at": (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(),
    })
    db.fail_counter_read = True

    _run(handler(_event(STRANGER, STRANGER, message_id=200, text=_GENERAL_REQUEST)))

    assert client.sent == []
    assert db.senders[0]["ai_replies_used"] == 1   # the expired window was not rolled


def test_a_failed_generation_restores_the_expired_window_instead_of_restarting_it(db, client, monkeypatch):
    handler, state = _mutable_ai_handler(db, client, monkeypatch)
    _run(svc.set_ai_counter_reset_enabled(True))
    _run(svc.set_ai_counter_reset_seconds(60))
    expired = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    db.senders.append({
        "owner_id": OWNER, "sender_id": STRANGER, "ai_replies_used": 4,
        "last_ai_reply_at": expired, "window_started_at": expired,
    })

    state["body"] = _ENGLISH_REPLY
    _run(handler(_event(STRANGER, STRANGER, message_id=201, text=_GENERAL_REQUEST)))
    assert client.sent == []
    assert db.senders[0]["ai_replies_used"] == 4
    assert db.senders[0]["window_started_at"] == expired

    state["body"] = _PERSIAN_REPLY
    _run(handler(_event(STRANGER, STRANGER, message_id=202, text=_GENERAL_REQUEST)))
    assert len(client.sent) == 1
    assert db.senders[0]["ai_replies_used"] == 1        # the window rolled ONCE
    assert db.senders[0]["window_started_at"] != expired


def test_the_window_survives_a_restart(db, client, monkeypatch):
    """A process restart must not hand anyone a fresh allowance or restart
    every person's window: both live in the durable row."""
    handler, _calls = _quota_handler(db, client, monkeypatch, max_replies=1, reset=86400)
    _run(handler(_event(STRANGER, STRANGER, message_id=210, text=_GENERAL_REQUEST)))
    window = db.senders[0]["window_started_at"]

    svc.reset_in_memory()                               # restart: memory is gone
    svc.configure_owner(OWNER)
    _run(svc.ensure_loaded_async())

    _run(handler(_event(STRANGER, STRANGER, message_id=211, text=_GENERAL_REQUEST)))
    assert len(client.sent) == 1                        # still denied
    assert db.senders[0]["window_started_at"] == window  # the window was not restarted


def test_concurrent_messages_at_a_reset_boundary_do_not_bypass_the_quota(db, client, monkeypatch):
    handler, _calls = _quota_handler(db, client, monkeypatch, max_replies=1, reset=60)
    expired = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    db.senders.append({
        "owner_id": OWNER, "sender_id": STRANGER, "ai_replies_used": 1,
        "last_ai_reply_at": expired, "window_started_at": expired,
    })

    async def _both():
        await asyncio.gather(
            handler(_event(STRANGER, STRANGER, message_id=220, text=_GENERAL_REQUEST)),
            handler(_event(STRANGER, STRANGER, message_id=221, text=_GENERAL_REQUEST)),
        )

    _run(_both())

    assert len(client.sent) == 1                        # the reset handed out no extra reply
    assert db.senders[0]["ai_replies_used"] == 1
    assert db.senders[0]["window_started_at"] != expired


def test_the_persistence_contract_itself_closes_the_window_race(db, monkeypatch):
    """The CAS identity at the boundary, driven through the REAL db contract.

    Two claims that both observed the SAME expired window (the second read
    happened before the first write) must not both roll it: the loser's
    conditional update matches no row and its re-read is denied by the freshly
    started window. Without the window in the filter the loser's condition
    (`ai_replies_used = 1`) would still match the rolled row and hand out a
    second reply.
    """
    expired = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    fake = _StaleReadDB(
        [{
            "owner_id": OWNER, "sender_id": STRANGER, "ai_replies_used": 1,
            "last_ai_reply_at": expired, "window_started_at": expired,
        }],
        stale_reads=2,
    )
    monkeypatch.setattr("backend.db.client.get_db", lambda: fake)
    from backend.db import client as db_client

    now_iso = datetime.now(timezone.utc).isoformat()

    async def _claim():
        return await db_client.claim_private_message_ai_reply(
            OWNER, STRANGER, max_replies=1, unlimited=False, interval_seconds=0,
            reset_enabled=True, reset_seconds=60, now_iso=now_iso,
        )

    first = _run(_claim())
    second = _run(_claim())

    assert first["ok"] is True and first["claimed"] is True
    assert first["token"]["read_window"] == expired
    assert first["token"]["window_expired"] is True
    assert second["ok"] is True and second["claimed"] is False, second
    assert second["reason"] == "limit_reached"
    assert fake.senders[0]["ai_replies_used"] == 1
    assert fake.senders[0]["window_started_at"] != expired


def test_the_reset_window_is_part_of_the_claim_cas_identity():
    from backend.db import client as db_client

    token = {
        "read_used": 1,
        "read_window": "2026-10-16T10:00:00+00:00",
        "used": 1,
        "last": "2026-10-16T12:00:00+00:00",
        "window": "2026-10-16T12:00:00+00:00",
        "restore_used": 1,
        "restore_last": None,
        "restore_window": "2026-10-16T10:00:00+00:00",
        "window_expired": True,
    }
    assert db_client._pm_cas_filters(token) == [
        ("ai_replies_used", 1),
        ("window_started_at", "2026-10-16T10:00:00+00:00"),
    ]
    # A row with no window has nothing to double-roll, so it is not filtered on.
    assert db_client._pm_cas_filters(dict(token, read_window=None)) == [
        ("ai_replies_used", 1)
    ]


def test_the_reset_configuration_is_validated_and_never_half_applied(db):
    assert svc.state()["ai_counter_reset_seconds"] == svc.DEFAULT_AI_COUNTER_RESET_SECONDS
    assert svc.state()["ai_counter_reset_enabled"] is False

    for bad in (0, -60, True, "3600", 1.5, None, svc.MIN_AI_COUNTER_RESET_SECONDS - 1,
                svc.MAX_AI_COUNTER_RESET_SECONDS + 1, 10 ** 20):
        assert _run(svc.set_ai_counter_reset_seconds(bad)) is False, bad
    for bad in (1, 0, None, "yes"):
        assert _run(svc.set_ai_counter_reset_enabled(bad)) is False, bad
    assert svc.state()["ai_counter_reset_seconds"] == svc.DEFAULT_AI_COUNTER_RESET_SECONDS

    assert _run(svc.set_ai_counter_reset_seconds(7200)) is True
    assert _run(svc.set_ai_counter_reset_enabled(True)) is True
    stored = db.config[OWNER]
    assert stored["ai_counter_reset_seconds"] == 7200
    assert stored["ai_counter_reset_enabled"] is True

    # A stored value outside the bounds falls back to the default, never to a
    # dangerous one (an interval of 0 would expire every window instantly).
    assert svc.sanitize_state({"ai_counter_reset_seconds": 0})["ai_counter_reset_seconds"] == (
        svc.DEFAULT_AI_COUNTER_RESET_SECONDS
    )


def test_the_reset_interval_input_accepts_units_and_refuses_anything_else():
    assert svc.parse_reset_interval("30") == 1800
    assert svc.parse_reset_interval("30m") == 1800
    assert svc.parse_reset_interval(" 90s ") == 90
    assert svc.parse_reset_interval("2H") == 7200
    assert svc.parse_reset_interval("3d") == 259200
    assert svc.parse_reset_interval(45) == 2700
    for bad in ("", "   ", "-5", "-5m", "nope", "1.5h", "5x", "1e9", "0", "0m",
                "59s", "366d", "999999999999999999999m", None, True, 0):
        assert svc.parse_reset_interval(bad) is None, bad
    assert svc.format_reset_interval(86400) == "1 day"
    assert svc.format_reset_interval(172800) == "2 days"
    assert svc.format_reset_interval(3600) == "1 hour"
    assert svc.format_reset_interval(1800) == "30 minutes"
    assert svc.format_reset_interval(90) == "90 seconds"
    assert svc.format_reset_interval(0) == "—" and svc.format_reset_interval(None) == "—"
