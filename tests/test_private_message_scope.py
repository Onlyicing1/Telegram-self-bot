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
    monkeypatch.setattr(pm, "_generate_ai_reply", _async_return("generated"))

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


def test_the_ai_reply_carries_both_disclosures(db):
    body = svc.compose_ai_reply("  متنی  ")
    assert body.endswith(f"{svc.CLOSED_NOTICE}\n{svc.AI_NOTICE}")
    assert body.startswith("متنی")
    assert svc.compose_ai_reply("") is None
    assert svc.compose_ai_reply(None) is None
    assert svc.compose_ai_reply("x" * svc.MAX_DELIVERED_TEXT_LENGTH) is None


def test_the_ai_receives_only_the_incoming_text(db, client, monkeypatch):
    import backend.ai.engine.engine as engine_module

    calls: list[dict] = []

    class _Response:
        success = True
        text = "ok"

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


def _ai_handler(db, client, monkeypatch, *, reply_text="generated", fail_generation=False):
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
    assert "part 10 of 10" in doc
    assert "private_message_senders" in doc
