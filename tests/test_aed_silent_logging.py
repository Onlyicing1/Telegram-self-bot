"""Anti-Edit / Anti-Delete must emit NO logs at any level.

The AED subsystem used to log one INFO line per skipped non-human peer, a
DEBUG line per unresolved entity, a WARNING per capture failure and several
configuration/retention warnings. Those lines are gone — not downgraded — so
this module proves two things against the REAL handler registrations and the
REAL service module:

  * no AED log record is produced for a captured, skipped, unconfirmed or
    failing operation, at ANY level (DEBUG included);
  * capture behavior is unchanged: a confirmed human private peer is still
    recorded, and a bot / unresolved / non-User peer is still fail-closed.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.bot.handlers import anti_edit_delete as aed
from backend.services import anti_edit_delete_service as svc

OWNER = 880101
PEER = 5001

_HANDLER_MODULE = Path(aed.__file__)
_SERVICE_MODULE = Path(svc.__file__)


class _Client:
    """Records the handlers ``register`` wires, like the Telethon client does."""

    def __init__(self) -> None:
        self.handlers: dict[str, object] = {}

    def on(self, event_type):
        def _decorator(fn):
            self.handlers[type(event_type).__name__] = fn
            return fn

        return _decorator


class User:
    """A resolved Telegram ``User`` (the handler keys off the class NAME)."""

    def __init__(self, bot=None, username="peer"):
        self.bot = bot
        self.username = username
        self.first_name = "Peer"
        self.last_name = ""


class Chat:
    """A resolved NON-User entity — never a confirmed human."""


def _message(**overrides):
    base = dict(
        chat_id=PEER,
        id=7,
        message="hello",
        text="hello",
        media=None,
        date=None,
        edit_date=None,
        out=True,
        sender_id=OWNER,
        via_bot_id=None,
        action=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _event(message, entity):
    async def _get_chat():
        return entity

    return SimpleNamespace(
        message=message,
        chat_id=getattr(message, "chat_id", None),
        deleted_ids=[getattr(message, "id", 1)],
        is_private=True,
        sender_id=getattr(message, "sender_id", None),
        get_chat=_get_chat,
    )


async def _noop(*args, **kwargs):
    return None


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    svc.reset_in_memory()
    svc.configure_owner(OWNER)
    yield
    svc.reset_in_memory()


def _register(monkeypatch, *, enabled=True, record_message=None, record_deletion=None):
    monkeypatch.setattr(svc, "ensure_loaded_async", _noop)
    monkeypatch.setattr(svc, "is_enabled", lambda: enabled)
    if record_message is not None:
        monkeypatch.setattr(svc, "record_message", record_message)
    if record_deletion is not None:
        monkeypatch.setattr(svc, "record_deletion", record_deletion)
    client = _Client()
    aed.register(client, OWNER, "UTC")
    return client


def _aed_records(caplog):
    """Every record that came from an AED module or carries an AED marker."""
    return [
        record
        for record in caplog.records
        if record.name in {aed.__name__, svc.__name__} or "AED" in record.getMessage()
    ]


def _run(handler, event):
    return asyncio.run(handler(event))


def _snapshot_handler(client):
    return client.handlers["NewMessage"]


def _edit_handler(client):
    return client.handlers["MessageEdited"]


def _deletion_handler(client):
    return client.handlers["MessageDeleted"]


# ── 1. source guarantee: no log statement exists in the AED subsystem ────────


@pytest.mark.parametrize("path", [_HANDLER_MODULE, _SERVICE_MODULE], ids=["handler", "service"])
def test_no_aed_module_contains_a_log_statement(path):
    source = path.read_text(encoding="utf-8")
    assert "logger." not in source
    assert "getLogger(__name__)" not in source


# ── 2. skips are silent ─────────────────────────────────────────────────────


def test_unconfirmed_peer_snapshot_skip_is_silent(monkeypatch, caplog):
    calls: list[dict] = []

    async def _record(**kwargs):
        calls.append(kwargs)

    client = _register(monkeypatch, record_message=_record)
    caplog.set_level(logging.DEBUG)
    with caplog.at_level(logging.DEBUG):
        _run(_snapshot_handler(client), _event(_message(), None))

    assert calls == []
    assert _aed_records(caplog) == []


def test_bot_peer_snapshot_skip_is_silent_and_fail_closed(monkeypatch, caplog):
    calls: list[dict] = []

    async def _record(**kwargs):
        calls.append(kwargs)

    client = _register(monkeypatch, record_message=_record)
    caplog.set_level(logging.DEBUG)
    with caplog.at_level(logging.DEBUG):
        _run(_snapshot_handler(client), _event(_message(), User(bot=True)))

    assert calls == []
    assert _aed_records(caplog) == []


def test_non_user_entity_is_silent_and_fail_closed(monkeypatch, caplog):
    calls: list[dict] = []

    async def _record(**kwargs):
        calls.append(kwargs)

    client = _register(monkeypatch, record_message=_record)
    caplog.set_level(logging.DEBUG)
    with caplog.at_level(logging.DEBUG):
        _run(_snapshot_handler(client), _event(_message(), Chat()))

    assert calls == []
    assert _aed_records(caplog) == []


def test_unconfirmed_peer_edit_skip_is_silent(monkeypatch, caplog):
    calls: list[dict] = []

    async def _record(**kwargs):
        calls.append(kwargs)

    client = _register(monkeypatch, record_message=_record)
    caplog.set_level(logging.DEBUG)
    with caplog.at_level(logging.DEBUG):
        _run(_edit_handler(client), _event(_message(), None))

    assert calls == []
    assert _aed_records(caplog) == []


def test_unconfirmed_peer_deletion_skip_is_silent(monkeypatch, caplog):
    calls: list[dict] = []

    async def _record_deletion(**kwargs):
        calls.append(kwargs)

    client = _register(monkeypatch, record_deletion=_record_deletion)
    caplog.set_level(logging.DEBUG)
    with caplog.at_level(logging.DEBUG):
        _run(_deletion_handler(client), _event(_message(), None))

    assert calls == []
    assert _aed_records(caplog) == []


# ── 3. capture failures are silent ──────────────────────────────────────────


@pytest.mark.parametrize("event_kind", ["NewMessage", "MessageEdited", "MessageDeleted"])
def test_capture_failure_is_silent(monkeypatch, caplog, event_kind):
    async def _boom(**kwargs):
        raise RuntimeError("capture exploded")

    client = _register(monkeypatch, record_message=_boom, record_deletion=_boom)
    caplog.set_level(logging.DEBUG)
    with caplog.at_level(logging.DEBUG):
        _run(client.handlers[event_kind], _event(_message(), User(bot=False)))

    assert _aed_records(caplog) == []


def test_disabled_feature_is_silent(monkeypatch, caplog):
    calls: list[dict] = []

    async def _record(**kwargs):
        calls.append(kwargs)

    client = _register(monkeypatch, enabled=False, record_message=_record)
    caplog.set_level(logging.DEBUG)
    with caplog.at_level(logging.DEBUG):
        _run(_snapshot_handler(client), _event(_message(), User(bot=False)))

    assert calls == []
    assert _aed_records(caplog) == []


# ── 4. functionality unchanged (and still silent) ───────────────────────────


def test_confirmed_human_snapshot_is_still_captured_silently(monkeypatch, caplog):
    calls: list[dict] = []

    async def _record(**kwargs):
        calls.append(kwargs)
        return {"id": "row"}

    client = _register(monkeypatch, record_message=_record)
    caplog.set_level(logging.DEBUG)
    with caplog.at_level(logging.DEBUG):
        _run(_snapshot_handler(client), _event(_message(), User(bot=False, username="friend")))

    assert len(calls) == 1
    captured = calls[0]
    assert captured["kind"] == svc.KIND_SNAPSHOT
    assert captured["peer_is_bot"] is False
    assert captured["owner"] == OWNER
    assert captured["identity"]["username"] == "friend"
    assert captured["identity"]["peer_id"] == PEER
    assert _aed_records(caplog) == []


def test_confirmed_human_edit_and_deletion_are_still_captured_silently(monkeypatch, caplog):
    edits: list[dict] = []
    deletions: list[dict] = []

    async def _record(**kwargs):
        edits.append(kwargs)

    async def _record_deletion(**kwargs):
        deletions.append(kwargs)

    client = _register(monkeypatch, record_message=_record, record_deletion=_record_deletion)
    caplog.set_level(logging.DEBUG)
    with caplog.at_level(logging.DEBUG):
        _run(_edit_handler(client), _event(_message(), User(bot=False)))
        _run(_deletion_handler(client), _event(_message(), User(bot=False)))

    assert [call["kind"] for call in edits] == [svc.KIND_EDIT]
    assert len(deletions) == 1
    assert deletions[0]["peer_is_bot"] is False
    assert _aed_records(caplog) == []
