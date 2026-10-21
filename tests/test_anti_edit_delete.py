"""Anti-Edit / Anti-Delete — capture, archive, browse, retention and panels.

Drives the REAL handler registrations (``register`` → the three Telethon event
handlers plus the Glass UI panels/actions/input), the REAL service, and the REAL
``bot_settings``-backed persistence path. The database is a fake key/value table
and the Telegram boundary is a recording self client — no live Telegram, helper
bot, database or AI is involved.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from telethon import events
from telethon.tl import types

from backend.bot.handlers import anti_edit_delete as aed
from backend.bot.handlers import misc
from backend.db import client as db_client
from backend.helper import inline_engine
from backend.helper import symbols as sym
from backend.helper.panel_registry import registry as get_registry
from backend.helper.panels import get_action, get_input
from backend.services import anti_edit_delete_service as svc

OWNER = 7770001
FRIEND = 5001
BOT = 4242
GROUP = -1001234567890


# ── fake database (bot_settings + the two dedicated AED tables) ──────────────


class _Result:
    def __init__(self, data):
        self.data = data


class _FakeDB:
    """A minimal PostgREST stand-in for the three tables the AED layer touches.

    ``bot_settings`` is the legacy key/value store; ``aed_config`` and
    ``aed_archive_entries`` are the dedicated tables. ``has_aed_tables=False``
    simulates a database where the migration has NOT been applied: touching
    either dedicated table raises the missing-table error the store probe
    detects, so the feature falls back to the legacy shards.
    """

    def __init__(self, fail_write: bool = False, has_aed_tables: bool = True):
        self.rows: dict[str, dict] = {}          # bot_settings key -> row
        self.aed_config: dict[int, dict] = {}    # owner_id -> row
        self.aed_entries: list[dict] = []        # one row per archive record
        self.fail_write = fail_write
        self.has_aed_tables = has_aed_tables
        self._table = None
        self._op = None
        self._payload = None
        self._filters: list[tuple[str, str, object]] = []
        self._order: tuple[str, bool] | None = None
        self._limit = None
        self._single = False
        self._negate = False

    # -- query builder -------------------------------------------------------

    def table(self, name):
        if name in ("aed_config", "aed_archive_entries") and not self.has_aed_tables:
            raise RuntimeError(f'relation "public.{name}" does not exist')
        assert name in ("bot_settings", "aed_config", "aed_archive_entries"), name
        self._table, self._op, self._payload = name, None, None
        self._filters, self._order, self._limit, self._single = [], None, None, False
        self._negate = False
        return self

    def select(self, columns="*"):
        self._op = "select"
        return self

    def insert(self, payload):
        self._op = "insert"
        self._payload = payload
        return self

    def update(self, payload):
        self._op = "update"
        self._payload = payload
        return self

    def upsert(self, payload, on_conflict=None):
        self._op = "upsert"
        self._payload = payload
        self._on_conflict = on_conflict
        return self

    def delete(self):
        self._op = "delete"
        return self

    def eq(self, column, value):
        self._filters.append(("eq", column, value))
        return self

    def like(self, column, pattern):
        self._filters.append(("like", column, pattern))
        return self

    def in_(self, column, value):
        self._filters.append(("not_in" if self._negate else "in", column, value))
        return self

    @property
    def not_(self):
        self._negate = True
        return self

    def order(self, column, desc=False):
        self._order = (column, desc)
        return self

    def limit(self, value):
        self._limit = value
        return self

    def maybe_single(self):
        self._single = True
        return self

    # -- execution -----------------------------------------------------------

    def _store(self) -> list[dict]:
        if self._table == "bot_settings":
            return list(self.rows.values())
        if self._table == "aed_config":
            return list(self.aed_config.values())
        return self.aed_entries

    def _match(self, rows=None):
        out = []
        for row in self._store() if rows is None else rows:
            ok = True
            for op, column, value in self._filters:
                if op == "eq" and row.get(column) != value:
                    ok = False
                if op == "like":
                    prefix = str(value).rstrip("%")
                    if not str(row.get(column, "")).startswith(prefix):
                        ok = False
                if op == "in" and row.get(column) not in value:
                    ok = False
                if op == "not_in" and row.get(column) in value:
                    ok = False
            if ok:
                out.append(row)
        return out

    def execute(self):
        if self._op in ("insert", "update", "upsert", "delete") and self.fail_write:
            raise RuntimeError("write refused")
        if self._op == "insert":
            if self._table == "bot_settings":
                self.rows[self._payload["key"]] = dict(self._payload)
                return _Result([dict(self._payload)])
            if self._table == "aed_config":
                self.aed_config[self._payload["owner_id"]] = dict(self._payload)
                return _Result([dict(self._payload)])
            self.aed_entries.append(dict(self._payload))
            return _Result([dict(self._payload)])
        if self._op == "upsert":
            by_id = {row["id"]: row for row in self.aed_entries}
            for payload in self._payload:
                by_id[payload["id"]] = dict(payload)
            self.aed_entries = list(by_id.values())
            return _Result([dict(p) for p in self._payload])
        if self._op == "update":
            for row in self._match():
                row.update(self._payload)
            return _Result([dict(r) for r in self._match()])
        if self._op == "delete":
            victims = {id(r) for r in self._match()}
            if self._table == "bot_settings":
                for key in [k for k, row in self.rows.items() if id(row) in victims]:
                    self.rows.pop(key, None)
            elif self._table == "aed_config":
                for key in [k for k, row in self.aed_config.items() if id(row) in victims]:
                    self.aed_config.pop(key, None)
            else:
                self.aed_entries = [r for r in self.aed_entries if id(r) not in victims]
            return _Result([])
        matched = self._match()
        if self._order is not None:
            column, desc = self._order
            matched = sorted(matched, key=lambda r: str(r.get(column) or ""), reverse=desc)
        if self._limit is not None:
            matched = matched[: self._limit]
        if self._single:
            return _Result(dict(matched[0]) if matched else None)
        return _Result([dict(r) for r in matched])


# ── fake Telegram boundary ───────────────────────────────────────────────────


class _RecordingClient:
    def __init__(self):
        self.handlers: list[tuple[str, object]] = []

    def on(self, event_type):
        name = type(event_type).__name__

        def _decorator(fn):
            self.handlers.append((name, fn))
            return fn

        return _decorator

    def handler(self, name: str):
        for event_name, fn in self.handlers:
            if event_name == name:
                return fn
        raise AssertionError(f"no handler registered for {name}")


class _FakeHelperBot:
    def __init__(self):
        self.edits: list[tuple] = []

    async def edit_message(self, chat_id, msg_id, text, buttons=None):
        self.edits.append((chat_id, msg_id, text, buttons))


class MessageMediaPhoto:
    def __init__(self, document=None):
        self.document = document


class MessageMediaDocument:
    def __init__(self, document=None):
        self.document = document


class DocumentAttributeAudio:
    def __init__(self, voice=False):
        self.voice = voice


class DocumentAttributeVideo:
    pass


class DocumentAttributeFilename:
    def __init__(self, file_name):
        self.file_name = file_name


def _document(attributes, size=1234):
    return SimpleNamespace(attributes=attributes, size=size)


def _msg(
    *,
    chat_id=FRIEND,
    message_id=7,
    text="hello world",
    media=None,
    date=None,
    edit_date=None,
    out=True,
    sender_id=OWNER,
    via_bot_id=None,
    action=None,
):
    return SimpleNamespace(
        chat_id=chat_id,
        id=message_id,
        message=text,
        text=text,
        media=media,
        date=date or datetime.now(timezone.utc),
        edit_date=edit_date,
        out=out,
        sender_id=sender_id,
        via_bot_id=via_bot_id,
        action=action,
    )


_UNSET = object()


def _human(chat_id):
    """A resolved human ``User`` for a positive private chat id."""
    return User(id=int(chat_id), username="peer", bot=False)


def _deleted_event(*, chat_id=FRIEND, ids=(7,), is_private=True, entity=_UNSET):
    event = SimpleNamespace(
        chat_id=chat_id,
        deleted_ids=list(ids),
        is_private=is_private,
    )
    if entity is _UNSET:
        entity = _human(chat_id) if isinstance(chat_id, int) and not isinstance(chat_id, bool) and chat_id > 0 else None

    async def _get_chat():
        return entity

    event.get_chat = _get_chat
    return event


class User:
    """Minimal stand-in for ``telethon.tl.types.User`` (the handler keys off the
    class NAME, exactly as it does for the real type). ``bot`` mirrors the real
    constructor flag the installed Telethon exposes."""

    def __init__(self, id, username=None, first_name=None, last_name=None, bot=False):
        self.id = id
        self.username = username
        self.first_name = first_name
        self.last_name = last_name
        self.bot = bot


class _Event:
    """A capture event that resolves its own chat entity.

    Omitting ``chat_entity`` means "a resolved human peer for this private
    chat" (the ordinary case); pass ``None`` explicitly to simulate a peer that
    cannot be resolved at all.
    """

    def __init__(self, message, chat_entity=_UNSET, is_private=True, chat_error=None):
        if chat_entity is _UNSET:
            chat_id = getattr(message, "chat_id", None)
            chat_entity = (
                _human(chat_id)
                if isinstance(chat_id, int) and not isinstance(chat_id, bool) and chat_id > 0
                else None
            )
        self.message = message
        self.is_private = is_private
        self.chat_id = getattr(message, "chat_id", None)
        self.sender_id = getattr(message, "sender_id", None)
        self._chat_entity = chat_entity
        self._chat_error = chat_error

    async def get_chat(self):
        if self._chat_error is not None:
            raise self._chat_error
        return self._chat_entity


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ── fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    svc.reset_in_memory()
    db_client.reset_aed_store_mode()
    svc.configure_owner(OWNER)
    inline_engine.set_owner_id(OWNER)
    inline_engine.set_self_client(None)
    yield
    svc.reset_in_memory()
    db_client.reset_aed_store_mode()
    inline_engine.set_self_client(None)


@pytest.fixture()
def db(monkeypatch):
    fake = _FakeDB()
    monkeypatch.setattr("backend.db.client.get_db", lambda: fake)
    return fake


@pytest.fixture()
def client(db):
    recording = _RecordingClient()
    aed.register(recording, OWNER, "Asia/Tehran")
    return recording


@pytest.fixture()
def enabled(db):
    _run(svc.set_enabled(True))
    # Browse/detail re-prove the strict scope against the live self client. A
    # resolver that classifies every peer as a human is the ordinary case; the
    # tests that exercise a bot or an unresolved peer override it.
    inline_engine.set_self_client(_resolving_peer())
    return db


def _seed_rows(db, rows):
    """Seed archive rows straight into the DEDICATED store (the fake tables).

    A malformed entry is stored AS-IS, so the test exercises the service's real
    tolerance of malformed persisted data.
    """
    db.aed_entries.extend(dict(r) if isinstance(r, dict) else r for r in rows)


def _datas(buttons):
    out = []
    for row in buttons:
        for button in row:
            raw = getattr(button, "data", button)
            out.append(raw.decode("utf-8") if isinstance(raw, bytes) else str(raw))
    return out


def _texts(buttons):
    return [str(getattr(button, "text", button)) for row in buttons for button in row]


def _panel(panel_id: str, extra: str = ""):
    handler = get_registry().get_handler(panel_id)
    assert handler is not None, f"panel '{panel_id}' is not registered"
    return _run(handler(None, extra))


def _action(action_id: str, extra: str = ""):
    handler = get_action(action_id)
    assert handler is not None, f"action '{action_id}' is not registered"
    return _run(handler(None, extra, FRIEND))


# ── 1. the startup import contract ───────────────────────────────────────────


def test_the_module_router_imports_exists_and_exports_register():
    # The exact import that failed at startup must now resolve.
    from backend.bot.handlers import anti_edit_delete

    assert callable(anti_edit_delete.register)
    assert anti_edit_delete.register.__code__.co_varnames[:3] == ("client", "owner_id", "tz_str")


def test_the_service_cleanup_hook_target_exists():
    from backend.services import anti_edit_delete_service

    assert callable(anti_edit_delete_service.cleanup_expired)


# ── 2. registration surface ──────────────────────────────────────────────────


def test_register_wires_the_three_capture_handlers(client):
    names = {name for name, _ in client.handlers}
    assert {"NewMessage", "MessageEdited", "MessageDeleted"} <= names


def test_register_wires_every_panel_action_and_input(client):
    for panel_id in ("archive", "archive_list", "archive_item", "archive_cats", "archive_confirm"):
        assert get_registry().get_handler(panel_id) is not None, panel_id
    for action_id in (
        "aed_toggle_enabled", "aed_refresh", "aed_category",
        "aed_page", "aed_delete", "aed_clear_confirm", "aed_noop",
    ):
        assert get_action(action_id) is not None, action_id
    assert get_input("archive", "retention") is not None
    assert get_registry().get_parent("archive") == "cipher"


def test_the_mother_menu_reaches_the_archive_through_cipher(client):
    datas = _datas(misc._build_menu_buttons())
    assert "panel:archive" not in datas
    assert "panel:cipher" in datas


def test_every_callback_data_fits_telegram_limits(client):
    for panel_id in ("archive", "archive_list", "archive_item", "archive_cats", "archive_confirm"):
        _title, _body, buttons = _panel(panel_id)
        for data in _datas(buttons):
            assert len(data.encode("utf-8")) <= 64, (panel_id, data)


# ── 3. configuration ─────────────────────────────────────────────────────────


def test_the_feature_defaults_to_disabled(db):
    assert svc.is_enabled() is False
    assert set(svc.categories()) == set(svc.CATEGORIES)


def test_enabling_persists_and_survives_a_reload(db):
    assert _run(svc.set_enabled(True)) is True
    svc.reset_in_memory()
    svc.configure_owner(OWNER)
    _run(svc.ensure_loaded_async())
    assert svc.is_enabled() is True


def test_a_category_can_be_toggled_but_never_emptied(db):
    assert _run(svc.set_category(svc.CATEGORY_TEXT, False)) is True
    assert svc.CATEGORY_TEXT not in svc.categories()
    # Removing the final category is refused.
    for category in list(svc.categories()):
        if len(svc.categories()) == 1:
            assert _run(svc.set_category(category, False)) is False
        else:
            assert _run(svc.set_category(category, False)) is True
    assert len(svc.categories()) >= 1


def test_retention_validation_bounds(db):
    assert _run(svc.set_retention_days(7)) is True
    assert svc.retention_days() == 7
    assert _run(svc.set_retention_days(0)) is False
    assert _run(svc.set_retention_days(svc.MAX_RETENTION_DAYS + 1)) is False
    assert _run(svc.set_retention_days("7")) is False


# ── 4. scope gate ────────────────────────────────────────────────────────────


def test_scope_gate_accepts_only_confirmed_human_private_chats():
    assert svc.eligible(owner=OWNER, chat_id=FRIEND, is_private=True, peer_is_bot=False) == (
        True, "eligible"
    )
    assert svc.eligible(owner=OWNER, chat_id=GROUP, is_private=True, peer_is_bot=False)[0] is False
    assert svc.eligible(owner=OWNER, chat_id=OWNER, is_private=True, peer_is_bot=False)[1] == "saved_messages"
    assert svc.eligible(owner=OWNER, chat_id=FRIEND, is_private=False, peer_is_bot=False)[0] is False
    assert svc.eligible(owner=OWNER, chat_id=FRIEND, is_private=True,
                        via_bot_id=99, peer_is_bot=False)[0] is False
    # An unclassified peer is never admitted on the strength of a positive id.
    assert svc.eligible(owner=OWNER, chat_id=FRIEND, is_private=True) == (
        False, "peer_unresolved"
    )


# ── 5. snapshot capture ──────────────────────────────────────────────────────


def test_a_private_message_is_snapshotted_while_enabled(client, enabled):
    handler = client.handler("NewMessage")
    _run(handler(_Event(_msg(text="meet at 5pm"))))
    rows = _run(svc.load_rows_async())
    assert len(rows) == 1
    assert rows[0]["kind"] == svc.KIND_SNAPSHOT
    assert rows[0]["body"] == "meet at 5pm"
    assert rows[0]["chat_id"] == FRIEND


def test_capture_is_inert_while_disabled(client, db):
    handler = client.handler("NewMessage")
    _run(handler(_Event(_msg())))
    assert _run(svc.load_rows_async()) == []


def test_out_of_scope_messages_are_never_captured(client, enabled):
    handler = client.handler("NewMessage")
    _run(handler(_Event(_msg(chat_id=GROUP))))
    _run(handler(_Event(_msg(chat_id=OWNER))))
    _run(handler(_Event(_msg(via_bot_id=42))))
    assert _run(svc.load_rows_async()) == []


def test_a_redelivered_snapshot_is_recorded_once(client, enabled):
    handler = client.handler("NewMessage")
    message = _msg()
    _run(handler(_Event(message)))
    _run(handler(_Event(message)))
    assert len(_run(svc.load_rows_async())) == 1


def test_category_filter_is_honoured_on_capture(client, enabled):
    assert _run(svc.set_category(svc.CATEGORY_TEXT, False)) is True
    handler = client.handler("NewMessage")
    _run(handler(_Event(_msg(text="plain"))))
    assert _run(svc.load_rows_async()) == []


# ── 6. edits ─────────────────────────────────────────────────────────────────


def test_an_edit_is_recorded_as_a_new_version(client, enabled):
    _run(client.handler("NewMessage")(_Event(_msg(text="v1"))))
    _run(client.handler("MessageEdited")(
        _Event(_msg(text="v2", edit_date=datetime.now(timezone.utc)))
    ))
    rows = _run(svc.load_rows_async())
    kinds = [r["kind"] for r in rows]
    assert kinds.count(svc.KIND_SNAPSHOT) == 1
    assert kinds.count(svc.KIND_EDIT) == 1


# ── 7. deletions (honest recoverability) ─────────────────────────────────────


def test_a_deletion_after_a_snapshot_is_recoverable(client, enabled):
    _run(client.handler("NewMessage")(_Event(_msg(message_id=9, text="secret"))))
    _run(client.handler("MessageDeleted")(_deleted_event(ids=(9,))))
    rows = _run(svc.load_rows_async())
    deletion = next(r for r in rows if r["kind"] == svc.KIND_DELETE)
    assert deletion["recoverable"] is True
    assert deletion["body"] == "secret"


def test_a_deletion_without_a_snapshot_is_an_honest_miss(client, enabled):
    _run(client.handler("MessageDeleted")(_deleted_event(ids=(555,))))
    rows = _run(svc.load_rows_async())
    deletion = next(r for r in rows if r["kind"] == svc.KIND_DELETE)
    assert deletion["recoverable"] is False
    assert deletion["body"] == ""


def test_a_deletion_in_a_group_is_never_recorded(client, enabled):
    _run(client.handler("MessageDeleted")(_deleted_event(chat_id=GROUP, ids=(7,))))
    assert _run(svc.load_rows_async()) == []


# ── 8. browsing ──────────────────────────────────────────────────────────────


def test_browse_filters_by_category_and_query(client, enabled):
    _run(client.handler("NewMessage")(_Event(_msg(message_id=1, text="alpha"))))
    _run(client.handler("NewMessage")(_Event(_msg(message_id=2, text="beta"))))
    rows = _run(svc.load_rows_async())
    assert [r["body"] for r in svc.filter_rows(rows, query="alph")] == ["alpha"]
    assert len(svc.filter_rows(rows, category=svc.CATEGORY_TEXT)) == 2


def test_caption_filter_matches_media_with_captions(client, enabled):
    media = MessageMediaPhoto()
    _run(client.handler("NewMessage")(
        _Event(_msg(message_id=3, text="a caption", media=media))
    ))
    rows = _run(svc.load_rows_async())
    assert len(svc.filter_rows(rows, category=svc.CATEGORY_CAPTIONS)) == 1
    assert len(svc.filter_rows(rows, category=svc.CATEGORY_PHOTOS)) == 1


def test_pagination_is_bounded_and_clamped(client, enabled):
    for index in range(svc.PAGE_SIZE + 3):
        _run(client.handler("NewMessage")(
            _Event(_msg(message_id=100 + index, text=f"m{index}"))
        ))
    rows = _run(svc.load_rows_async())
    first, page, pages = svc.page_of(rows, 1)
    assert len(first) == svc.PAGE_SIZE and page == 1 and pages == 2
    second, page, pages = svc.page_of(rows, 99)
    assert page == pages


def test_delete_one_row_and_clear_archive(client, enabled):
    _run(client.handler("NewMessage")(_Event(_msg(message_id=1))))
    _run(client.handler("NewMessage")(_Event(_msg(message_id=2))))
    rows = _run(svc.load_rows_async())
    assert _run(svc.delete_row(rows[0]["id"])) is True
    assert len(_run(svc.load_rows_async())) == 1
    assert _run(svc.clear_archive()) == 1
    assert _run(svc.load_rows_async()) == []


def test_deleting_a_missing_row_is_false(client, enabled):
    assert _run(svc.delete_row("deadbeef")) is False


# ── 9. retention cleanup (the existing worker hook) ──────────────────────────


def test_cleanup_expired_removes_only_rows_past_retention(db):
    _run(svc.set_enabled(True))
    _run(svc.set_retention_days(1))
    now = datetime.now(timezone.utc)
    old = (now - timedelta(days=5)).isoformat()
    fresh = now.isoformat()
    rows = [
        {"id": "old1", "owner_id": OWNER, "kind": svc.KIND_SNAPSHOT, "captured_at": old, "media_object": None},
        {"id": "new1", "owner_id": OWNER, "kind": svc.KIND_SNAPSHOT, "captured_at": fresh, "media_object": None},
    ]
    _seed_rows(db, rows)
    stats = _run(svc.cleanup_expired(now=now))
    assert set(stats) == {"archive_deleted", "snapshots_deleted", "objects_deleted"}
    assert stats["snapshots_deleted"] == 1
    remaining = _run(db_client.aed_load_entries(OWNER))
    kept_ids = [r["id"] for r in remaining]
    assert kept_ids == ["new1"]


def test_cleanup_expired_is_a_noop_without_expired_rows(db):
    _run(svc.set_enabled(True))
    stats = _run(svc.cleanup_expired())
    assert stats == {"archive_deleted": 0, "snapshots_deleted": 0, "objects_deleted": 0}


# ── 10. media honesty ────────────────────────────────────────────────────────


def test_media_metadata_is_bounded_and_never_fabricated(client, enabled):
    doc = _document([DocumentAttributeFilename("report.pdf")], size=999)
    media = MessageMediaDocument(doc)
    _run(client.handler("NewMessage")(_Event(_msg(message_id=4, text="", media=media))))
    row = _run(svc.load_rows_async())[0]
    assert row["category"] == svc.CATEGORY_DOCUMENTS
    assert row["media_name"] == "report.pdf"
    assert row["media_size"] == 999
    # No bucket configured → never claims durable media archival.
    assert row["media_stored"] is False
    assert svc.media_storage_available() is False


def test_voice_document_is_classified_as_voice(client, enabled):
    doc = _document([DocumentAttributeAudio(voice=True)])
    media = MessageMediaDocument(doc)
    _run(client.handler("NewMessage")(_Event(_msg(message_id=5, media=media))))
    assert _run(svc.load_rows_async())[0]["category"] == svc.CATEGORY_VOICE


# ── 11. panels ───────────────────────────────────────────────────────────────


def test_main_panel_reports_the_persisted_state(client, db):
    _run(svc.set_enabled(True))
    title, body, buttons = _panel("archive")
    assert "Enabled" in body
    assert "action:aed_toggle_enabled" in _datas(buttons)


def test_toggling_enabled_through_the_action_persists(client, db):
    _action("aed_toggle_enabled")
    assert svc.is_enabled() is True
    _action("aed_toggle_enabled")
    assert svc.is_enabled() is False


def test_category_action_refuses_to_empty(client, db):
    for category in svc.CATEGORIES[:-1]:
        _action("aed_category", category)
    title, body, _ = _action("aed_category", svc.CATEGORIES[-1])
    assert len(svc.categories()) >= 1
    assert "At least one category" in body


def test_list_panel_renders_archived_items(client, enabled):
    _capture(client, _msg(text="remember this"))
    _edit(client, _msg(text="remember this, edited", edit_date=datetime.now(timezone.utc)))
    title, body, buttons = _panel("archive_list")
    rendered = body + " ".join(_texts(buttons))
    assert "remember this, edited" in rendered


def test_item_panel_marks_recoverability(client, enabled):
    _run(client.handler("NewMessage")(_Event(_msg(message_id=9, text="x"))))
    _run(client.handler("MessageDeleted")(_deleted_event(ids=(9,))))
    rows = _run(svc.load_rows_async())
    deletion = next(r for r in rows if r["kind"] == svc.KIND_DELETE)
    title, body, buttons = _panel("archive_item", deletion["id"])
    assert "Recoverable" in body
    assert f"action:aed_delete:{deletion['id']}" in _datas(buttons)


def test_clear_confirm_panel_and_action(client, enabled):
    _run(client.handler("NewMessage")(_Event(_msg())))
    _title, _body, buttons = _panel("archive_confirm")
    assert "action:aed_clear_confirm" in _datas(buttons)
    _action("aed_clear_confirm")
    assert _run(svc.load_rows_async()) == []


def test_retention_input_validates_and_persists(client, db, monkeypatch):
    helper = _FakeHelperBot()
    monkeypatch.setattr("backend.helper.client.get_client", lambda: helper)
    config = get_input("archive", "retention")
    _run(config["handler"]("14", FRIEND, 1, FRIEND, 2))
    assert svc.retention_days() == 14
    _run(config["handler"]("not-a-number", FRIEND, 1, FRIEND, 2))
    assert svc.retention_days() == 14


# ── 12. logical-message grouping (Requirement 1) ─────────────────────────────


def _capture(client, message, *, entity=_UNSET, kind="NewMessage"):
    event = _Event(message, chat_entity=entity)
    _run(client.handler(kind)(event))


def _edit(client, message, *, entity=_UNSET):
    _run(client.handler("MessageEdited")(_Event(message, chat_entity=entity)))


def _list_buttons(client):
    _title, _body, buttons = _panel("archive_list")
    return _datas(buttons)


# ── visible events vs. internal recovery snapshots ─────────────────────────


def test_a_snapshot_only_message_is_not_an_archive_entry(client, enabled):
    """The ordinary case: a human private message is captured for recovery but
    is invisible until an edit or deletion is actually observed."""
    _capture(client, _msg(message_id=7, text="untouched"))
    rows = _run(svc.load_rows_async())
    assert [r["kind"] for r in rows] == [svc.KIND_SNAPSHOT]   # stored internally
    _title, body, buttons = _panel("archive_list")
    assert not [d for d in _datas(buttons) if d.startswith("panel:archive_item:")]
    assert "0 item(s)" in body
    # ... and its detail is not reachable by id either.
    _t, detail, _b = _panel("archive_item", svc.group_id(FRIEND, 7))
    assert "no longer in the archive" in detail


def test_an_observed_edit_makes_the_message_visible_with_its_original(client, enabled):
    _capture(client, _msg(message_id=7, text="original"))
    _edit(client, _msg(message_id=7, text="edited", edit_date=datetime.now(timezone.utc)))
    items = svc.build_items(_run(svc.load_rows_async()))
    assert len(items) == 1 and items[0]["version_count"] == 2
    assert [v["kind"] for v in items[0]["versions"]] == [svc.KIND_EDIT, svc.KIND_SNAPSHOT]
    _t, body, _b = _panel("archive_item", svc.group_id(FRIEND, 7))
    assert "original" in body and "edited" in body
    assert "v1 (original)" in body and "v2 (edit)" in body


def test_a_deletion_is_listed_only_after_the_deletion_event(client, enabled):
    _capture(client, _msg(message_id=9, text="secret"))
    _title, _body, buttons = _panel("archive_list")
    assert not [d for d in _datas(buttons) if d.startswith("panel:archive_item:")]

    _run(client.handler("MessageDeleted")(_deleted_event(ids=(9,))))
    items = svc.build_items(_run(svc.load_rows_async()))
    assert [item["kind"] for item in items] == [svc.KIND_DELETE]
    assert items[0]["recoverable"] is True
    _t, detail, _b = _panel("archive_item", items[0]["id"])
    assert "Recoverable from a pre-deletion snapshot" in detail
    assert "secret" in detail


def test_an_unrecoverable_deletion_is_listed_honestly_without_content(client, enabled):
    _run(client.handler("MessageDeleted")(_deleted_event(ids=(555,))))
    items = svc.build_items(_run(svc.load_rows_async()))
    assert len(items) == 1 and items[0]["kind"] == svc.KIND_DELETE
    assert items[0]["recoverable"] is False and items[0]["body"] == ""
    _t, detail, _b = _panel("archive_item", items[0]["id"])
    assert "never captured — not recoverable" in detail


def test_the_snapshot_survives_after_a_deletion_for_recovery(client, enabled):
    _capture(client, _msg(message_id=9, text="recover me"))
    _run(client.handler("MessageDeleted")(_deleted_event(ids=(9,))))
    rows = _run(svc.load_rows_async())
    snapshot = next(r for r in rows if r["kind"] == svc.KIND_SNAPSHOT)
    deletion = next(r for r in rows if r["kind"] == svc.KIND_DELETE)
    assert snapshot["body"] == "recover me"           # internal record kept
    assert deletion["body"] == "recover me"            # recovered from it
    assert deletion["recoverable"] is True


def test_browse_hides_persisted_rows_from_a_non_private_chat(client, enabled):
    now = datetime.now(timezone.utc).isoformat()
    _seed_rows(enabled, [{
        "id": "grouprow", "owner_id": OWNER, "chat_id": GROUP, "message_id": 3,
        "kind": svc.KIND_EDIT, "category": svc.CATEGORY_TEXT,
        "captured_at": now, "body": "group message",
    }, {
        "id": "channelrow", "owner_id": OWNER, "chat_id": -1009876543210, "message_id": 4,
        "kind": svc.KIND_EDIT, "category": svc.CATEGORY_TEXT,
        "captured_at": now, "body": "channel post",
    }])
    svc.reset_in_memory()
    svc.configure_owner(OWNER)
    _run(svc.set_enabled(True))
    _title, body, buttons = _panel("archive_list")
    rendered = body + " ".join(_texts(buttons))
    assert "group message" not in rendered and "channel post" not in rendered
    assert "2 persisted item(s) hidden" in body
    _t, detail, _b = _panel("archive_item", svc.group_id(GROUP, 3))
    assert "no longer in the archive" in detail


def test_a_channel_post_is_never_captured(client, enabled):
    channel = _msg(chat_id=-1009876543210, message_id=1, text="channel post")
    _run(client.handler("NewMessage")(_Event(channel, is_private=False)))
    assert _run(svc.load_rows_async()) == []


def test_a_resolved_non_user_entity_is_not_a_confirmed_human(client, enabled):
    class Channel:
        def __init__(self):
            self.id = FRIEND
            self.title = "Not a user"

    now = datetime.now(timezone.utc).isoformat()
    _seed_rows(enabled, [{
        "id": "channelrow", "owner_id": OWNER, "chat_id": FRIEND, "message_id": 5,
        "kind": svc.KIND_EDIT, "category": svc.CATEGORY_TEXT,
        "captured_at": now, "body": "not a human",
    }])
    svc.reset_in_memory()
    svc.configure_owner(OWNER)
    _run(svc.set_enabled(True))
    items = svc.build_items(_run(svc.load_rows_async()))
    filtered, hidden = _run(svc.filter_scope_items(_StubResolver(entity=Channel()), items))
    assert filtered == [] and hidden == 1


def test_a_snapshot_and_its_edit_render_as_one_list_button(client, enabled):
    _capture(client, _msg(message_id=7, text="v1"))
    _edit(client, _msg(message_id=7, text="v2", edit_date=datetime.now(timezone.utc)))
    # Two rows are stored (history preserved) but ONE browse item exists.
    assert len(_run(svc.load_rows_async())) == 2
    item_buttons = [d for d in _list_buttons(client) if d.startswith("panel:archive_item:")]
    assert len(item_buttons) == 1
    assert item_buttons[0] == f"panel:archive_item:{svc.group_id(FRIEND, 7)}"


def test_multiple_edits_still_render_as_one_button_with_all_versions(client, enabled):
    _capture(client, _msg(message_id=7, text="v1"))
    for index in range(2, 5):
        _edit(client, _msg(message_id=7, text=f"v{index}",
                           edit_date=datetime.now(timezone.utc)))
    items = svc.build_items(_run(svc.load_rows_async()))
    assert len(items) == 1
    assert items[0]["version_count"] == 4
    item_buttons = [d for d in _list_buttons(client) if d.startswith("panel:archive_item:")]
    assert len(item_buttons) == 1
    assert "4 versions" in " ".join(_texts(_panel("archive_list")[2]))


def test_different_messages_with_identical_content_stay_separate(client, enabled):
    for message_id in (1, 2):
        _capture(client, _msg(message_id=message_id, text="same"))
        _edit(client, _msg(message_id=message_id, text="same edited",
                          edit_date=datetime.now(timezone.utc)))
    items = svc.build_items(_run(svc.load_rows_async()))
    assert len(items) == 2
    assert {i["message_id"] for i in items} == {1, 2}


def test_identical_message_ids_in_different_chats_are_not_grouped(client, enabled):
    for chat_id in (FRIEND, FRIEND + 1):
        _capture(client, _msg(chat_id=chat_id, message_id=7, text="same id"))
        _edit(client, _msg(chat_id=chat_id, message_id=7, text="same id edited",
                          edit_date=datetime.now(timezone.utc)))
    items = svc.build_items(_run(svc.load_rows_async()))
    assert len(items) == 2
    assert {(i["chat_id"], i["message_id"]) for i in items} == {(FRIEND, 7), (FRIEND + 1, 7)}


def test_grouped_detail_shows_version_count_and_history(client, enabled):
    _capture(client, _msg(message_id=7, text="original text"))
    _edit(client, _msg(message_id=7, text="edited text", edit_date=datetime.now(timezone.utc)))
    _title, body, _buttons = _panel("archive_item", svc.group_id(FRIEND, 7))
    assert "2 observed version(s)" in body
    assert "original text" in body
    assert "edited text" in body
    assert "v1 (original)" in body and "v2 (edit)" in body


def test_grouped_detail_uses_the_latest_content_as_the_list_label(client, enabled):
    _capture(client, _msg(message_id=7, text="old"))
    _edit(client, _msg(message_id=7, text="newest", edit_date=datetime.now(timezone.utc)))
    labels = " ".join(_texts(_panel("archive_list")[2]))
    assert "newest" in labels
    assert "old" not in labels


def test_pagination_counts_grouped_items_not_raw_versions(client, enabled):
    # PAGE_SIZE distinct messages, each with an extra edit → 2 rows apiece.
    for index in range(svc.PAGE_SIZE):
        _capture(client, _msg(message_id=100 + index, text=f"m{index}"))
        _edit(client, _msg(message_id=100 + index, text=f"m{index}!",
                           edit_date=datetime.now(timezone.utc)))
    rows = _run(svc.load_rows_async())
    assert len(rows) == svc.PAGE_SIZE * 2
    items = svc.build_items(rows)
    first, page, pages = svc.page_of(items, 1)
    assert pages == 1 and page == 1
    assert len(first) == svc.PAGE_SIZE


def test_category_filtering_is_correct_for_grouped_items(client, enabled):
    media = MessageMediaPhoto()
    _capture(client, _msg(message_id=1, text="plain text"))
    _edit(client, _msg(message_id=1, text="plain text edited",
                       edit_date=datetime.now(timezone.utc)))
    _capture(client, _msg(message_id=2, text="a caption", media=media))
    _edit(client, _msg(message_id=2, text="a caption edited", media=media,
                       edit_date=datetime.now(timezone.utc)))
    items = svc.build_items(_run(svc.load_rows_async()))
    text_items = svc.filter_items(items, category=svc.CATEGORY_TEXT)
    photo_items = svc.filter_items(items, category=svc.CATEGORY_PHOTOS)
    assert [i["message_id"] for i in text_items] == [1]
    assert [i["message_id"] for i in photo_items] == [2]


def test_a_deletion_event_remains_independently_visible_and_recoverable(client, enabled):
    _capture(client, _msg(message_id=9, text="secret"))
    _edit(client, _msg(message_id=9, text="secret v2", edit_date=datetime.now(timezone.utc)))
    _run(client.handler("MessageDeleted")(_deleted_event(ids=(9,))))
    items = svc.build_items(_run(svc.load_rows_async()))
    # ONE grouped content item + ONE independent deletion event = 2 items.
    assert len(items) == 2
    grouped = next(i for i in items if i["kind"] == svc.KIND_MESSAGE)
    deletion = next(i for i in items if i["kind"] == svc.KIND_DELETE)
    assert grouped["version_count"] == 2
    assert grouped["has_deletion"] is True
    assert deletion["recoverable"] is True
    item_buttons = [d for d in _list_buttons(client) if d.startswith("panel:archive_item:")]
    assert len(item_buttons) == 2
    # The deletion keeps its own recoverability display.
    _t, dbody, _b = _panel("archive_item", deletion["id"])
    assert "Recoverable" in dbody


def test_group_delete_removes_the_whole_history_without_orphans(client, enabled):
    _capture(client, _msg(message_id=7, text="v1"))
    _edit(client, _msg(message_id=7, text="v2", edit_date=datetime.now(timezone.utc)))
    removed = _run(svc.delete_group(FRIEND, 7))
    assert removed == 2
    assert _run(svc.load_rows_async()) == []


def test_group_delete_leaves_a_deletion_event_in_place(client, enabled):
    _capture(client, _msg(message_id=9, text="x"))
    _edit(client, _msg(message_id=9, text="y", edit_date=datetime.now(timezone.utc)))
    _run(client.handler("MessageDeleted")(_deleted_event(ids=(9,))))
    assert _run(svc.delete_group(FRIEND, 9)) == 2
    remaining = _run(svc.load_rows_async())
    assert [r["kind"] for r in remaining] == [svc.KIND_DELETE]
    assert remaining[0]["recoverable"] is True


def test_group_delete_action_drives_the_service(client, enabled):
    _capture(client, _msg(message_id=7, text="v1"))
    _edit(client, _msg(message_id=7, text="v2", edit_date=datetime.now(timezone.utc)))
    _action("aed_delete_group", f"{FRIEND}:7")
    assert _run(svc.load_rows_async()) == []


def test_group_delete_action_rejects_malformed_input(client, enabled):
    _capture(client, _msg(message_id=7, text="keep"))
    _action("aed_delete_group", "not-an-id")
    assert len(_run(svc.load_rows_async())) == 1


def test_no_duplicate_deletion_buttons_after_grouping(client, enabled):
    _capture(client, _msg(message_id=9, text="x"))
    _run(client.handler("MessageDeleted")(_deleted_event(ids=(9,))))
    # A redelivered deletion event must not create a second deletion button.
    _run(client.handler("MessageDeleted")(_deleted_event(ids=(9,))))
    items = svc.build_items(_run(svc.load_rows_async()))
    assert sum(1 for i in items if i["kind"] == svc.KIND_DELETE) == 1


# ── 13. peer identity (Requirement 2) ────────────────────────────────────────


def test_outgoing_message_peer_is_the_other_user_not_the_owner(client, enabled):
    entity = User(id=FRIEND, username="alice", first_name="Alice", last_name="Smith")
    _capture(client, _msg(message_id=7, out=True, sender_id=OWNER), entity=entity)
    row = _run(svc.load_rows_async())[0]
    assert row["sender_id"] == OWNER          # the author IS the owner
    assert row["peer_id"] == FRIEND           # the PEER is the other user
    assert row["peer_username"] == "alice"
    assert row["peer_name"] == "Alice Smith"


def test_incoming_message_peer_is_the_sender(client, enabled):
    entity = User(id=FRIEND, username="bob", first_name="Bob")
    _capture(client, _msg(message_id=7, out=False, sender_id=FRIEND), entity=entity)
    row = _run(svc.load_rows_async())[0]
    assert row["peer_id"] == FRIEND
    assert row["peer_username"] == "bob"


def test_detail_view_shows_username_and_numeric_user_id(client, enabled):
    entity = User(id=FRIEND, username="alice", first_name="Alice")
    _capture(client, _msg(message_id=7, text="hi"), entity=entity)
    _edit(client, _msg(message_id=7, text="hi again", edit_date=datetime.now(timezone.utc)), entity=entity)
    _title, body, _buttons = _panel("archive_item", svc.group_id(FRIEND, 7))
    assert f"@alice" in body
    assert f"User ID: {FRIEND}" in body
    # The technical identifiers are retained for troubleshooting.
    assert f"Chat ID: {FRIEND}" in body
    assert "Message ID: 7" in body


def test_identity_falls_back_to_the_numeric_id_without_a_username(client, enabled):
    entity = User(id=FRIEND, username=None, first_name=None, last_name=None)
    _capture(client, _msg(message_id=7, text="hi"), entity=entity)
    _edit(client, _msg(message_id=7, text="hi again", edit_date=datetime.now(timezone.utc)), entity=entity)
    _title, body, _buttons = _panel("archive_item", svc.group_id(FRIEND, 7))
    assert f"User ID: {FRIEND}" in body
    assert "@" not in body.split("User ID:")[0].split("User:")[-1]


def test_an_unresolved_peer_is_never_captured(client, enabled):
    # A peer that cannot be resolved cannot be proven human; the strict scope
    # fails closed instead of admitting it on the strength of a chat id.
    event = _Event(_msg(message_id=7, text="not captured"), chat_error=RuntimeError("boom"))
    _run(client.handler("NewMessage")(event))
    assert _run(svc.load_rows_async()) == []

    edit = _Event(_msg(message_id=7, text="v2", edit_date=datetime.now(timezone.utc)),
                  chat_error=RuntimeError("boom"))
    _run(client.handler("MessageEdited")(edit))
    assert _run(svc.load_rows_async()) == []


def test_legacy_rows_without_identity_render_safely(client, enabled):
    # Simulate a row captured before the identity fields existed.
    legacy = {
        "id": "legacy01", "owner_id": OWNER, "chat_id": FRIEND, "message_id": 7,
        "kind": svc.KIND_EDIT, "category": svc.CATEGORY_TEXT, "has_caption": False,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "body": "old row", "has_media": False, "recoverable": True,
    }
    _seed_rows(enabled, [legacy])
    svc.reset_in_memory()
    svc.configure_owner(OWNER)
    _run(svc.set_enabled(True))
    items = svc.build_items(_run(svc.load_rows_async()))
    assert items[0]["identity"]["peer_id"] == FRIEND   # falls back to chat_id
    assert items[0]["identity"]["username"] is None
    _title, body, _buttons = _panel("archive_item", svc.group_id(FRIEND, 7))
    assert f"User ID: {FRIEND}" in body


def test_identity_and_version_history_survive_a_persisted_reload(client, enabled):
    entity = User(id=FRIEND, username="alice", first_name="Alice")
    _capture(client, _msg(message_id=7, text="v1"), entity=entity)
    _edit(client, _msg(message_id=7, text="v2", edit_date=datetime.now(timezone.utc)), entity=entity)
    svc.reset_in_memory()
    svc.configure_owner(OWNER)
    _run(svc.ensure_loaded_async())
    items = svc.build_items(_run(svc.load_rows_async()))
    assert len(items) == 1
    assert items[0]["version_count"] == 2
    assert items[0]["identity"]["username"] == "alice"
    assert items[0]["identity"]["peer_id"] == FRIEND


def test_a_non_user_entity_is_not_a_human_private_peer(client, enabled):
    class Chat:
        def __init__(self):
            self.id = FRIEND
            self.title = "Not A User"

    _capture(client, _msg(message_id=7, text="hi"), entity=Chat())
    assert _run(svc.load_rows_async()) == []


def test_owner_isolation_is_preserved_across_grouping(client, enabled):
    _capture(client, _msg(message_id=7, text="mine"))
    _edit(client, _msg(message_id=7, text="mine edited", edit_date=datetime.now(timezone.utc)))
    # Inject a row belonging to a DIFFERENT owner into the same store.
    other = {
        "id": "other01", "owner_id": OWNER + 1, "chat_id": FRIEND, "message_id": 8,
        "kind": svc.KIND_EDIT, "category": svc.CATEGORY_TEXT,
        "captured_at": datetime.now(timezone.utc).isoformat(), "body": "not mine",
    }
    _seed_rows(enabled, [other])
    svc.reset_in_memory()
    svc.configure_owner(OWNER)
    _run(svc.set_enabled(True))
    items = svc.build_items(_run(svc.load_rows_async()))
    assert len(items) == 1
    assert items[0]["version_count"] == 2
    assert items[0]["message_id"] == 7
    assert "not mine" not in (items[0]["body"] or "")


def test_media_only_message_uses_an_honest_fallback_label(client, enabled):
    media = MessageMediaPhoto()
    _capture(client, _msg(message_id=7, text="", media=media))
    _edit(client, _msg(message_id=7, text="", media=media, edit_date=datetime.now(timezone.utc)))
    item = svc.build_items(_run(svc.load_rows_async()))[0]
    assert item["body"].startswith("(Photo")
    assert item["media_name"] is None


def test_grouped_media_detail_keeps_the_metadata_only_indicator(client, enabled):
    doc = _document([DocumentAttributeFilename("report.pdf")], size=999)
    media = MessageMediaDocument(doc)
    _capture(client, _msg(message_id=7, text="", media=media))
    _edit(client, _msg(message_id=7, text="", media=media, edit_date=datetime.now(timezone.utc)))
    _title, body, _buttons = _panel("archive_item", svc.group_id(FRIEND, 7))
    assert "metadata only" in body


# ── 14. bot exclusion (the peer's REAL Telegram User.bot flag) ───────────────


def _bot_entity():
    return User(id=BOT, username="sexyboyvpn_bot", first_name="Sexyboy VPN", last_name="Bot", bot=True)


def test_is_bot_entity_requires_a_resolved_bot_user():
    # A resolved bot User is the ONLY positive classification.
    assert svc.is_bot_entity(_bot_entity()) is True
    # A human User (bot=False), missing or non-User entities are NOT classified.
    assert svc.is_bot_entity(User(id=FRIEND, username="alice")) is False
    assert svc.is_bot_entity(None) is False
    assert svc.is_bot_entity("sexyboyvpn_bot") is False


def test_scope_gate_requires_a_confirmed_human_peer():
    assert svc.eligible(owner=OWNER, chat_id=BOT, is_private=True, peer_is_bot=True) == (
        False, "peer_is_bot"
    )
    # Unknown (None) is NOT a human: the gate fails closed.
    assert svc.eligible(owner=OWNER, chat_id=BOT, is_private=True, peer_is_bot=None) == (
        False, "peer_unresolved"
    )
    assert svc.eligible(owner=OWNER, chat_id=FRIEND, is_private=True, peer_is_bot=False) == (
        True, "eligible"
    )


def test_a_bot_private_message_is_never_snapshotted(client, enabled):
    # The observed bug: @sexyboyvpn_bot's private message archived with its bot
    # username shown as the user identity. The resolved entity says bot.
    event = _Event(_msg(chat_id=BOT, message_id=5, out=False, sender_id=BOT), chat_entity=_bot_entity())
    _run(client.handler("NewMessage")(event))
    assert _run(svc.load_rows_async()) == []


def test_a_bot_edit_cannot_create_an_archive_record(client, enabled):
    event = _Event(
        _msg(chat_id=BOT, message_id=5, text="v2", edit_date=datetime.now(timezone.utc)),
        chat_entity=_bot_entity(),
    )
    _run(client.handler("MessageEdited")(event))
    assert _run(svc.load_rows_async()) == []


def test_a_bot_deletion_is_never_recorded_when_the_peer_resolves(client, enabled):
    event = _deleted_event(chat_id=BOT, ids=(9,))

    async def _get_chat():
        return _bot_entity()

    event.get_chat = _get_chat
    _run(client.handler("MessageDeleted")(event))
    assert _run(svc.load_rows_async()) == []


def test_a_deletion_from_an_unclassifiable_peer_is_not_recorded(client, enabled):
    # A deletion event whose peer cannot be resolved does not invent a human
    # classification: nothing is recorded for it.
    _run(client.handler("NewMessage")(_Event(_msg(message_id=9, text="x"))))
    event = _deleted_event(chat_id=BOT, ids=(9,), entity=None)  # peer unknown
    _run(client.handler("MessageDeleted")(event))
    rows = _run(svc.load_rows_async())
    assert [r["kind"] for r in rows] == [svc.KIND_SNAPSHOT]


def test_service_rejects_a_confirmed_bot_peer_on_snapshot_and_deletion(client, enabled):
    assert _run(svc.record_message(
        msg=_msg(chat_id=BOT, message_id=1), kind=svc.KIND_SNAPSHOT, owner=OWNER, peer_is_bot=True
    )) is None
    assert _run(svc.record_deletion(event=_deleted_event(chat_id=BOT, ids=(3,)), owner=OWNER, peer_is_bot=True)) == []
    assert _run(svc.load_rows_async()) == []


def test_a_human_private_message_is_still_archived_next_to_a_bot(client, enabled):
    _capture(client, _msg(message_id=5, text="human words"), entity=User(id=FRIEND, username="alice"))
    bot_event = _Event(_msg(chat_id=BOT, message_id=6, out=False, sender_id=BOT), chat_entity=_bot_entity())
    _run(client.handler("NewMessage")(bot_event))
    rows = _run(svc.load_rows_async())
    assert len(rows) == 1
    assert rows[0]["chat_id"] == FRIEND
    assert rows[0]["body"] == "human words"


def _seed_legacy_rows():
    now = datetime.now(timezone.utc).isoformat()
    rows = [
        {"id": "botrow01", "owner_id": OWNER, "chat_id": BOT, "message_id": 1,
         "kind": svc.KIND_EDIT, "category": svc.CATEGORY_TEXT, "has_caption": False,
         "captured_at": now, "body": "bot advertising", "has_media": False, "recoverable": True},
        {"id": "humrow02", "owner_id": OWNER, "chat_id": FRIEND, "message_id": 2,
         "kind": svc.KIND_EDIT, "category": svc.CATEGORY_TEXT, "has_caption": False,
         "captured_at": now, "body": "human note", "has_media": False, "recoverable": True},
    ]
    return rows


def _resolving_peer(entity=None, broken=False):
    class _PeerResolver:
        async def get_entity(self, chat_id):
            if broken:
                raise RuntimeError("nope")
            if entity is not None and int(chat_id) == BOT:
                return entity
            return User(id=int(chat_id), username="human")

    return _PeerResolver()


def test_persisted_bot_records_are_hidden_from_listing_and_detail(client, enabled, monkeypatch):
    seeded = _seed_legacy_rows()
    _seed_rows(enabled, [seeded[0], seeded[1]])
    svc.reset_in_memory()
    svc.configure_owner(OWNER)
    _run(svc.set_enabled(True))
    monkeypatch.setattr("backend.helper.inline_engine.get_self_client",
                        lambda: _resolving_peer(entity=_bot_entity()))

    _title, body, buttons = _panel("archive_list")
    rendered = body + " ".join(_texts(buttons))
    assert "bot advertising" not in rendered
    assert "human note" in rendered
    assert "hidden" in body

    _title, body, _buttons = _panel("archive_item", svc.group_id(BOT, 1))
    assert "no longer in the archive" in body

    # The human item keeps its full grouped behaviour.
    _title, body, _buttons = _panel("archive_item", svc.group_id(FRIEND, 2))
    assert "human note" in body


def test_no_client_or_unresolvable_peer_is_hidden(client, enabled):
    """Without a confirmed human classification a persisted row is NOT shown:
    the browse scope fails closed instead of guessing the peer's humanity."""
    seeded = _seed_legacy_rows()
    _seed_rows(enabled, [seeded[0], seeded[1]])
    svc.reset_in_memory()
    svc.configure_owner(OWNER)
    _run(svc.set_enabled(True))

    # No client at all: nothing can be classified, so nothing is shown.
    inline_engine.set_self_client(None)
    _title, body, buttons = _panel("archive_list")
    rendered = body + "  ".join(_texts(buttons))
    assert "bot advertising" not in rendered and "human note" not in rendered
    assert "hidden" in body

    # A resolution failure is not guessed either: the rows stay hidden.
    svc.reset_in_memory()
    svc.configure_owner(OWNER)
    _run(svc.set_enabled(True))
    svc._peer_bot_cache.clear()
    filtered, hidden = _run(svc.filter_scope_items(_resolving_peer(broken=True),
                                                   svc.build_items(_run(svc.load_rows_async()))))
    assert hidden == 2
    assert filtered == []


def test_peer_bot_status_is_cached_and_bounded(client, enabled, monkeypatch):
    client_stub = _resolving_peer(entity=_bot_entity())
    calls = []

    original = client_stub.get_entity

    async def _counting(entity_chat_id):
        calls.append(int(entity_chat_id))
        return await original(entity_chat_id)

    client_stub.get_entity = _counting
    for _ in range(3):
        assert _run(svc.peer_bot_status(client_stub, BOT)) is True
    assert len(calls) == 1  # one resolution, cached afterwards
    monkeypatch.setattr(svc, "_PEER_BOT_CACHE_MAX", 2)
    for index in range(5):
        _run(svc.peer_bot_status(client_stub, OWNER + index))
    assert len(svc._peer_bot_cache) <= 2 + 1  # bounded cache, never grows unbounded


# ── 15. resolution reliability (single-flight + FloodWait cooldown) ─────────


class _StubResolver:
    """A get_entity stand-in that records every call."""

    def __init__(self, entity=None, raises=None, delay=0.0):
        self.calls = []
        self._entity = entity
        self._raises = raises
        self._delay = delay

    async def get_entity(self, chat_id):
        self.calls.append(int(chat_id))
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._raises is not None:
            raise self._raises
        if self._entity is not None and int(chat_id) == int(self._entity.id):
            return self._entity
        return User(id=int(chat_id), username="human")


def _flood(seconds=69):
    # The REAL Telethon error type, carrying the server-provided duration.
    from telethon.errors import FloodWaitError

    return FloodWaitError(request=None, capture=seconds)


def test_concurrent_same_peer_share_one_resolution(client, enabled):
    resolver = _StubResolver(entity=_bot_entity(), delay=0.05)

    async def _five():
        return await asyncio.gather(
            *[svc.peer_bot_status(resolver, BOT) for _ in range(5)]
        )

    assert _run(_five()) == [True] * 5
    assert resolver.calls == [BOT]           # ONE in-flight resolution, joined
    assert svc._peer_bot_cache.get(BOT) is True
    assert svc._inflight.get(BOT) is None    # completed task reclaimed


def test_flood_wait_defers_without_immediate_retry(client, enabled):
    resolver = _StubResolver(raises=_flood(69))
    assert _run(svc.peer_bot_status(resolver, BOT)) is False   # unknown, NOT bot
    assert resolver.calls == [BOT]

    followup = _StubResolver(entity=_bot_entity())
    for _ in range(4):
        assert _run(svc.peer_bot_status(followup, BOT)) is False
    assert followup.calls == []              # NO retry while Telegram requires the wait
    assert 0 < svc._cooldown_remaining(BOT) <= 69   # bounded, timed, from the error


def test_flood_wait_larger_than_the_deadline_is_capped_and_deferred(client, enabled):
    resolver = _StubResolver(raises=_flood(99999))
    assert _run(svc.peer_bot_status(resolver, BOT)) is False
    assert 0 < svc._cooldown_remaining(BOT) <= svc._PEER_FLOOD_MAX_S


def test_transient_failure_bounded_cooldown_prevents_request_storms(client, enabled):
    resolver = _StubResolver(raises=RuntimeError("peer gone"))
    for _ in range(6):
        assert _run(svc.peer_bot_status(resolver, BOT)) is False
    assert resolver.calls == [BOT]           # one attempt, then cooldown


def test_failed_resolution_is_never_permanent_poison(client, enabled):
    resolver = _StubResolver(raises=RuntimeError("peer gone"))
    assert _run(svc.peer_bot_status(resolver, BOT)) is False
    assert svc._cooldown_remaining(BOT) > 0.0
    svc._cooldowns.pop(BOT)                  # the cooldown EXPIRES (no real sleeping)
    good = _StubResolver(entity=_bot_entity())
    assert _run(svc.peer_bot_status(good, BOT)) is True
    assert good.calls == [BOT]               # retry after expiry, normal classification


def test_cooldown_expiry_retries_and_classifies_normally(client, enabled):
    resolver = _StubResolver(raises=_flood(69))
    _run(svc.peer_bot_status(resolver, BOT))
    svc._cooldowns[BOT] = time.monotonic() - 1     # expiry, deterministically
    good = _StubResolver(entity=_bot_entity())
    assert _run(svc.peer_bot_status(good, BOT)) is True


def test_later_successful_resolution_shows_only_the_confirmed_human(client, enabled):
    seeded = _seed_legacy_rows()
    _seed_rows(enabled, [seeded[0], seeded[1]])
    svc.reset_in_memory()
    svc.configure_owner(OWNER)
    _run(svc.set_enabled(True))
    broken = _StubResolver(raises=RuntimeError("boom"))
    items = svc.build_items(_run(svc.load_rows_async()))
    filtered, hidden = _run(svc.filter_scope_items(broken, items))
    assert hidden == 2                       # unresolved: nothing is shown
    assert filtered == []
    for key in list(svc._cooldowns):
        svc._cooldowns[key] = time.monotonic() - 1   # after the cooldown expires
    bot_resolver = _StubResolver(entity=_bot_entity())
    filtered, hidden = _run(svc.filter_scope_items(bot_resolver, items))
    assert hidden == 1                       # the resolved bot row is hidden
    assert [item["body"] for item in filtered] == ["human note"]
    assert svc._peer_bot_cache.get(BOT) is True
    assert svc._peer_bot_cache.get(FRIEND) is False


def test_a_cancelled_caller_leaves_the_shared_resolution_alive(client, enabled):
    resolver = _StubResolver(entity=_bot_entity(), delay=0.05)

    async def _scenario():
        owner = asyncio.ensure_future(svc.peer_bot_status(resolver, BOT))
        joiner = asyncio.ensure_future(svc.peer_bot_status(resolver, BOT))
        await asyncio.sleep(0.01)
        assert svc._inflight.get(BOT) is not None     # one shared task
        owner.cancel()
        try:
            await owner
        except asyncio.CancelledError:
            pass
        assert await joiner is True                   # the shared task survived
        assert resolver.calls == [BOT]

    _run(_scenario())
    assert svc._inflight == {}                        # nothing stale survives


def test_reset_and_bounds_behaviour_remains(client, enabled, monkeypatch):
    resolver = _StubResolver(raises=_flood(5))
    _run(svc.peer_bot_status(resolver, FRIEND))
    assert svc._cooldowns and svc._peer_bot_cache == {}
    monkeypatch.setattr(svc, "_COOLDOWN_MAX", 8)
    for index in range(12):
        svc._defer_resolution(OWNER + index, svc._PEER_FAIL_COOLDOWN_S)
    assert len(svc._cooldowns) <= 8                   # bounded cooldown map
    svc.reset_in_memory()
    assert svc._cooldowns == {}                      # no permanent suppression
    assert svc._peer_bot_cache == {}
    assert svc._inflight == {}                       # no stale task references


def test_unknown_stays_unknown_during_deferral(client, enabled):
    resolver = _StubResolver(raises=_flood(5))
    assert _run(svc.peer_bot_status(resolver, BOT)) is False
    assert svc.is_bot_entity(None) is False
    assert svc.is_bot_entity(User(id=BOT, username="sexyboyvpn_bot")) is False  # bot flag absent
    assert svc.is_bot_entity(User(id=BOT, username="sexyboyvpn_bot", bot=False)) is False
    assert svc.is_bot_entity(User(id=BOT, username="sexyboyvpn_bot", bot=True)) is True


# ── 16. dedicated AED tables (the migration's repository integration) ─────────


def _legacy_db(monkeypatch):
    """A database where the AED migration has NOT been applied."""
    fake = _FakeDB(has_aed_tables=False)
    monkeypatch.setattr("backend.db.client.get_db", lambda: fake)
    return fake


def test_store_mode_is_dedicated_when_the_tables_exist(client, db):
    assert _run(db_client.aed_store_mode()) == "dedicated"


def test_store_mode_falls_back_to_legacy_before_the_migration(monkeypatch):
    _legacy_db(monkeypatch)
    assert _run(db_client.aed_store_mode()) == "legacy"


def test_capture_persists_into_the_dedicated_tables(client, enabled):
    _capture(client, _msg(message_id=7, text="hello there"), entity=User(id=FRIEND, username="alice"))
    assert enabled.aed_entries, "the row must be written to aed_archive_entries"
    row = enabled.aed_entries[0]
    assert row["owner_id"] == OWNER
    assert row["chat_id"] == FRIEND
    assert row["message_id"] == 7
    assert row["kind"] == svc.KIND_SNAPSHOT
    assert row["body"] == "hello there"
    assert row["peer_username"] == "alice"
    # The legacy shard documents are NOT touched once the tables exist.
    assert not any(key.startswith("anti_edit_delete_archive_shard") for key in enabled.rows)


def test_config_persists_into_the_dedicated_table(client, db):
    assert _run(svc.set_enabled(True)) is True
    assert db.aed_config[OWNER]["enabled"] is True
    assert not any(key.startswith("anti_edit_delete_config") for key in db.rows)
    assert _run(svc.set_retention_days(9)) is True
    assert db.aed_config[OWNER]["retention_days"] == 9


def test_config_reloads_from_the_dedicated_table(client, db):
    _run(svc.set_enabled(True))
    _run(svc.set_retention_days(3))
    svc.reset_in_memory()
    svc.configure_owner(OWNER)
    _run(svc.ensure_loaded_async())
    assert svc.is_enabled() is True
    assert svc.retention_days() == 3


def test_edit_and_delete_rows_keep_their_distinct_kinds(client, enabled):
    entity = User(id=FRIEND, username="alice")
    _capture(client, _msg(message_id=7, text="v1"), entity=entity)
    _edit(client, _msg(message_id=7, text="v2", edit_date=datetime.now(timezone.utc)), entity=entity)
    _run(client.handler("MessageDeleted")(_deleted_event(ids=(7,))))
    kinds = sorted(r["kind"] for r in enabled.aed_entries)
    assert kinds == [svc.KIND_DELETE, svc.KIND_EDIT, svc.KIND_SNAPSHOT]


def test_owner_isolation_is_enforced_by_the_dedicated_queries(client, enabled):
    _capture(client, _msg(message_id=7, text="mine"))
    _seed_rows(enabled, [{
        "id": "other01", "owner_id": OWNER + 1, "chat_id": FRIEND, "message_id": 8,
        "kind": svc.KIND_SNAPSHOT, "category": svc.CATEGORY_TEXT,
        "captured_at": datetime.now(timezone.utc).isoformat(), "body": "not mine",
    }])
    svc.reset_in_memory()
    svc.configure_owner(OWNER)
    _run(svc.set_enabled(True))
    rows = _run(svc.load_rows_async())
    assert [r["owner_id"] for r in rows] == [OWNER]
    assert all(r.get("body") != "not mine" for r in rows)


def test_a_duplicate_id_is_never_stored_twice(client, enabled):
    entity = User(id=FRIEND, username="alice")
    message = _msg(message_id=7, text="hello")          # the SAME message, same date
    _capture(client, message, entity=entity)
    _capture(client, message, entity=entity)            # a redelivered update
    ids = [r["id"] for r in enabled.aed_entries]
    assert len(ids) == len(set(ids)) == 1


def test_malformed_persisted_rows_are_tolerated(client, enabled):
    _seed_rows(enabled, [
        {"id": "broken01", "owner_id": OWNER},                       # missing fields
        {"id": "broken02", "owner_id": OWNER, "chat_id": None, "message_id": None,
         "kind": "nonsense", "captured_at": "not-a-date"},           # wrong values
        {"id": "ok01", "owner_id": OWNER, "chat_id": FRIEND, "message_id": 3,
         "kind": svc.KIND_EDIT, "category": svc.CATEGORY_TEXT,
         "captured_at": datetime.now(timezone.utc).isoformat(), "body": "fine"},
    ])
    svc.reset_in_memory()
    svc.configure_owner(OWNER)
    _run(svc.set_enabled(True))
    items = svc.build_items(_run(svc.load_rows_async()))
    assert any(i.get("body") == "fine" for i in items)


def test_a_failed_dedicated_write_is_reported_honestly(monkeypatch):
    fake = _FakeDB(fail_write=True)
    monkeypatch.setattr("backend.db.client.get_db", lambda: fake)
    svc.reset_in_memory()
    db_client.reset_aed_store_mode()
    svc.configure_owner(OWNER)
    assert _run(svc.set_enabled(True)) is False       # the write did not land
    assert svc.is_enabled() is False                  # so the switch stays OFF


def test_cleanup_deletes_expired_rows_from_the_dedicated_store(db):
    _run(svc.set_enabled(True))
    _run(svc.set_retention_days(1))
    now = datetime.now(timezone.utc)
    _seed_rows(db, [
        {"id": "old1", "owner_id": OWNER, "kind": svc.KIND_SNAPSHOT,
         "captured_at": (now - timedelta(days=5)).isoformat()},
        {"id": "new1", "owner_id": OWNER, "kind": svc.KIND_SNAPSHOT,
         "captured_at": now.isoformat()},
    ])
    stats = _run(svc.cleanup_expired(now=now))
    assert stats["snapshots_deleted"] == 1
    assert [r["id"] for r in db.aed_entries] == ["new1"]


def test_the_dedicated_schema_cannot_broaden_the_archive_scope():
    """The migration's CHECK is the structural backstop for the private-only scope."""
    from pathlib import Path

    sql = (Path(__file__).resolve().parent.parent / "supabase" / "migrations"
           / "20261017000001_create_anti_edit_delete_tables.sql").read_text(encoding="utf-8")
    executable = re.sub(r"/\*.*?\*/", "", sql, flags=re.S)
    assert "CHECK (chat_id > 0)" in executable
    assert "CHECK (kind IN ('snapshot', 'edit', 'delete'))" in executable


# ── the legacy → dedicated transition (the persistence audit) ───────────────
#
# The dedicated tables become authoritative the moment they exist, but the
# owner's configuration and archive live in the legacy bot_settings documents.
# Without the one-time import the first restart after the migration shows the
# defaults and an empty archive — the reported "reset".


def _legacy_row(row_id: str, owner: int = OWNER) -> dict:
    return {
        "id": row_id, "owner_id": owner, "chat_id": FRIEND, "message_id": 1,
        "kind": svc.KIND_SNAPSHOT, "category": svc.CATEGORY_TEXT,
        "captured_at": datetime.now(timezone.utc).isoformat(), "body": f"body {row_id}",
    }


def _apply_migration(fake: _FakeDB) -> None:
    """Turn the SAME database into one where the dedicated tables now exist."""
    fake.has_aed_tables = True
    db_client.reset_aed_store_mode()
    svc.reset_in_memory()
    svc.configure_owner(OWNER)


def test_the_legacy_configuration_and_archive_survive_the_migration(monkeypatch):
    fake = _legacy_db(monkeypatch)
    assert _run(svc.set_enabled(True)) is True
    assert _run(svc.set_retention_days(2)) is True
    assert _run(db_client.aed_save_archive(list(svc._SHARD_KEYS), [[
        _legacy_row("legacy01"), _legacy_row("other-owner", owner=999999),
    ], [], [], []])) is True
    legacy_document = fake.rows[svc._config_key(OWNER)]["value"]

    _apply_migration(fake)
    assert fake.aed_config == {} and fake.aed_entries == []

    _run(svc.ensure_loaded_async())
    assert svc.is_enabled() is True, "the stored switch must not reset to OFF"
    assert svc.retention_days() == 2
    assert fake.aed_config[OWNER]["enabled"] is True

    rows = _run(svc.load_rows_async())
    assert [r["id"] for r in rows] == ["legacy01"], "owner-scoped, no foreign rows"
    assert {r["id"] for r in fake.aed_entries} == {"legacy01"}
    # The legacy source is preserved, never deleted by the import.
    assert fake.rows[svc._config_key(OWNER)]["value"] == legacy_document


def test_the_import_runs_once_and_newer_dedicated_data_wins(monkeypatch):
    fake = _legacy_db(monkeypatch)
    assert _run(svc.set_retention_days(2)) is True
    assert _run(db_client.aed_save_archive(list(svc._SHARD_KEYS), [[
        _legacy_row("legacy01"),
    ], [], [], []])) is True
    _apply_migration(fake)
    _run(svc.load_rows_async())
    assert {r["id"] for r in fake.aed_entries} == {"legacy01"}

    # A newer dedicated value, and an archive the owner curated afterwards.
    assert _run(svc.set_retention_days(7)) is True
    fake.aed_entries.clear()

    svc.reset_in_memory()
    svc.configure_owner(OWNER)
    _run(svc.ensure_loaded_async())
    assert svc.retention_days() == 7, "a stale legacy value must never win"
    assert _run(svc.load_rows_async()) == [], (
        "the dedicated configuration row is the import marker, so a second start "
        "never resurrects deleted archive rows"
    )


def test_a_restart_reloads_the_configuration_and_archive_from_the_tables(db):
    assert _run(svc.set_enabled(True)) is True
    assert _run(svc.set_retention_days(3)) is True
    _seed_rows(db, [_legacy_row("persisted1"), _legacy_row("persisted2")])

    svc.reset_in_memory()
    svc.configure_owner(OWNER)
    assert svc.is_enabled() is False  # the safe default until the load lands

    _run(svc.ensure_loaded_async())
    assert svc.is_enabled() is True
    assert svc.retention_days() == 3
    rows = _run(svc.load_rows_async())
    assert {r["id"] for r in rows} == {"persisted1", "persisted2"}


def test_an_unreadable_store_leaves_the_feature_off_and_refuses_writes(monkeypatch):
    fake = _FakeDB()
    monkeypatch.setattr("backend.db.client.get_db", lambda: fake)

    async def _unreadable_config(owner_id):
        return None, False

    async def _unreadable_document(key):
        return None, False

    monkeypatch.setattr(db_client, "aed_load_config_state", _unreadable_config)
    monkeypatch.setattr(db_client, "aed_get_document_state", _unreadable_document)

    _run(svc.ensure_loaded_async())
    assert svc._loaded is False, "a failed read must never count as a load"
    assert svc.is_enabled() is False
    assert _run(svc.set_enabled(True)) is False
    assert fake.aed_config == {}, "an unreadable store must never be written over"


def test_the_import_writes_no_defaults_when_a_legacy_source_is_unreadable(monkeypatch):
    fake = _FakeDB()
    monkeypatch.setattr("backend.db.client.get_db", lambda: fake)

    async def _unreadable_document(key):
        return None, False

    monkeypatch.setattr(db_client, "aed_get_document_state", _unreadable_document)
    _run(svc.ensure_loaded_async())
    assert fake.aed_config == {}, "no configuration may be invented from an outage"
    assert svc.is_enabled() is False


def test_the_archive_import_is_deferred_when_the_legacy_shards_are_unreadable(monkeypatch):
    fake = _FakeDB()
    monkeypatch.setattr("backend.db.client.get_db", lambda: fake)

    async def _unreadable_archive(shard_keys):
        return None

    stored_document = json.dumps({"enabled": True, "retention_days": 3})

    async def _document(key):
        return stored_document, True

    monkeypatch.setattr(db_client, "aed_load_archive", _unreadable_archive)
    monkeypatch.setattr(db_client, "aed_get_document_state", _document)

    _run(svc.load_rows_async())
    assert fake.aed_config == {}, (
        "the configuration import waits for the archive, so a retry re-runs both"
    )


def test_legacy_shards_still_work_before_the_migration(monkeypatch):
    """Before the migration is applied the feature keeps its existing behaviour."""
    _legacy_db(monkeypatch)
    svc.reset_in_memory()
    svc.configure_owner(OWNER)
    _run(db_client.aed_save_archive(list(svc._SHARD_KEYS), [[{
        "id": "legacy01", "owner_id": OWNER, "chat_id": FRIEND, "message_id": 1,
        "kind": svc.KIND_SNAPSHOT, "category": svc.CATEGORY_TEXT,
        "captured_at": datetime.now(timezone.utc).isoformat(), "body": "old row",
    }], [], [], []]))
    _run(svc.set_enabled(True))
    rows = _run(svc.load_rows_async())
    assert [r["id"] for r in rows] == ["legacy01"]
    assert _run(svc.set_retention_days(4)) is True     # config still writes the legacy doc


# ── 16. reaction-only updates are never edits ────────────────────────────────
#
# Telegram reports a reaction state change as ``UpdateMessageReactions`` — the
# reaction lives in ``Message.reactions``, NOT in the message's content. These
# tests model the REAL Telethon update/event types and drive the REAL
# ``events.MessageEdited`` builder, so the classification under test is the
# production one: the builder turns only ``UpdateEditMessage`` /
# ``UpdateEditChannelMessage`` into a ``MessageEdited`` event, and an
# edit-shaped update whose message carries no ``edit_date`` stamp is a state
# update, not a content edit.

_DATED = datetime(2026, 10, 21, 10, 0, tzinfo=timezone.utc)


def _reaction_state(*emoticons):
    return types.MessageReactions(
        results=[
            types.ReactionCount(reaction=types.ReactionEmoji(emoticon=emoticon), count=1)
            for emoticon in emoticons
        ]
    )


def _tl_message(
    *,
    message_id=7,
    text="hello world",
    edit_date=None,
    media=None,
    reactions=None,
    date=_DATED,
):
    """A REAL ``telethon.tl.types.Message`` — the object a live update carries."""
    return types.Message(
        id=message_id,
        peer_id=types.PeerUser(FRIEND),
        date=date,
        message=text,
        out=True,
        media=media,
        edit_date=edit_date,
        reactions=reactions,
    )


def _reaction_update(*, message_id=7, reactions=None):
    """The documented reaction update: it never names ``edit_date``."""
    return types.UpdateMessageReactions(
        peer=types.PeerUser(FRIEND),
        msg_id=message_id,
        reactions=reactions if reactions is not None else _reaction_state("👍"),
    )


def _edit_update(message, *, pts=1):
    return types.UpdateEditMessage(message=message, pts=pts, pts_count=1)


class _OfflineClient:
    """What a real Telethon event touches before its handler runs.

    The peer entity is already in the event's own entity map, so ``get_chat()``
    resolves it offline — exactly as it does during a live dispatch.
    """

    def __init__(self):
        self._self_id = OWNER
        self._mb_entity_cache = {}


def _deliver_as_edit(client, update, *, entity=_UNSET):
    """Run ONE real update through Telethon's builder, then the AED handler.

    The built event is wired the way ``TelegramClient._dispatch_update`` wires
    it (entity map + client), so the handler sees a CONFIRMED human peer without
    any RPC. Returns the built event (``None`` when the builder does not
    classify the update as an edit), so a test asserts on the REAL
    classification result rather than on a mocked one.
    """
    event = events.MessageEdited.build(update)
    if event is None:
        return None
    peer = _human(FRIEND) if entity is _UNSET else entity
    if peer is not None:
        event._entities[FRIEND] = peer
    event._set_client(_OfflineClient())
    _run(client.handler("MessageEdited")(event))
    return event


def _rows(client):
    return _run(svc.load_rows_async())


def _kinds(rows):
    return [row["kind"] for row in rows]


def test_the_reaction_update_never_builds_a_message_edited_event(client, enabled):
    # The REAL type Telegram sends for a reaction change is not an edit update.
    assert events.MessageEdited.build(_reaction_update()) is None
    assert _deliver_as_edit(client, _reaction_update()) is None
    assert _rows(client) == []
    # ...while a real edit update still builds one, so the path stays exercised.
    assert _deliver_as_edit(client, _edit_update(_tl_message(edit_date=_DATED))) is not None


def test_a_reaction_on_an_archived_message_creates_no_edit_version(client, enabled):
    _capture(client, _tl_message(text="meet at 5pm"))
    _deliver_as_edit(client, _reaction_update(reactions=_reaction_state("👍")))
    rows = _rows(client)
    assert _kinds(rows) == [svc.KIND_SNAPSHOT]
    # The internal recovery snapshot must not become an archive entry, i.e. the
    # reaction is not an "observed event".
    assert svc.build_items(rows) == []
    _title, body, _buttons = _panel("archive_list")
    assert "0 item(s)" in body
    assert "versions" not in " ".join(_texts(_buttons))


def test_an_edit_shaped_reaction_update_cannot_create_a_version(client, enabled):
    """The defect itself: an ``UpdateEditMessage`` carrying an unchanged message.

    Telegram may deliver a reaction change through an edit-shaped update. The
    message carries the reaction state and no ``edit_date`` — the pre-fix code
    keyed a new ``edit`` version off ``date`` and archived an unchanged message.
    """
    _capture(client, _tl_message(text="hello world"))
    reacted = _tl_message(text="hello world", reactions=_reaction_state("❤"))
    assert reacted.edit_date is None
    assert svc.is_content_edit(reacted) is False

    event = _deliver_as_edit(client, _edit_update(reacted))
    assert event is not None, "the update IS delivered as an edit event — classification must stop it"

    rows = _rows(client)
    assert _kinds(rows) == [svc.KIND_SNAPSHOT]
    assert svc.build_items(rows) == []


def test_an_unchanged_message_stays_invisible_after_a_reaction(client, enabled):
    """No snapshot, only a reaction-only edit-shaped update → nothing archived."""
    reacted = _tl_message(text="never archived before", reactions=_reaction_state("🔥"))
    assert _deliver_as_edit(client, _edit_update(reacted)) is not None
    assert _rows(client) == []
    assert svc.build_items(_rows(client)) == []


def test_adding_and_changing_reactions_never_increments_the_version_count(client, enabled):
    _capture(client, _tl_message(text="steady"))
    for emoticons in (("👍",), ("👍", "❤"), (), ("🔥",)):
        _deliver_as_edit(client, _edit_update(
            _tl_message(text="steady", reactions=_reaction_state(*emoticons))
        ))
        _deliver_as_edit(client, _reaction_update(reactions=_reaction_state(*emoticons)))

    rows = _rows(client)
    assert _kinds(rows) == [svc.KIND_SNAPSHOT]
    # Not one visible version, and no visible item at all.
    assert svc.build_items(rows) == []
    _title, body, _buttons = _panel("archive_list")
    assert "0 item(s)" in body


def test_reaction_state_never_participates_in_the_content_signature():
    plain = _tl_message(text="same text")
    reacted = _tl_message(text="same text", reactions=_reaction_state("👍", "❤"))
    # Different reaction state, IDENTICAL content fingerprint.
    assert svc.content_signature(plain) == svc.content_signature(reacted)
    # ...and a genuinely different text does change it.
    assert svc.content_signature(plain) != svc.content_signature(_tl_message(text="other"))


def test_a_genuine_text_edit_is_still_captured_with_its_original_snapshot(client, enabled):
    _capture(client, _tl_message(text="v1"))
    edited = _tl_message(text="v2", edit_date=datetime.now(timezone.utc),
                         reactions=_reaction_state("👍"))
    event = _deliver_as_edit(client, _edit_update(edited))
    assert event is not None

    rows = _rows(client)
    assert sorted(_kinds(rows)) == [svc.KIND_EDIT, svc.KIND_SNAPSHOT]
    items = svc.build_items(rows)
    assert len(items) == 1
    assert items[0]["version_count"] == 2
    assert items[0]["body"] == "v2"
    bodies = [version["body"] for version in items[0]["versions"]]
    assert sorted(bodies) == ["v1", "v2"]
    # The edited message HAVING reactions must not block it: reactions are not
    # consulted, the edit stamp and the content change are.
    assert "2 versions" in " ".join(_texts(_panel("archive_list")[2]))


def test_a_genuine_caption_edit_on_media_is_still_captured(client, enabled):
    media = types.MessageMediaPhoto(photo=types.PhotoEmpty(id=1))
    _capture(client, _tl_message(text="old caption", media=media))
    edited = _tl_message(
        text="new caption",
        media=types.MessageMediaPhoto(photo=types.PhotoEmpty(id=1)),
        edit_date=datetime.now(timezone.utc),
    )
    assert _deliver_as_edit(client, _edit_update(edited)) is not None

    rows = _rows(client)
    assert sorted(_kinds(rows)) == [svc.KIND_EDIT, svc.KIND_SNAPSHOT]
    edit_row = next(row for row in rows if row["kind"] == svc.KIND_EDIT)
    assert edit_row["category"] == svc.CATEGORY_PHOTOS
    assert edit_row["body"] == "new caption"
    assert edit_row["has_caption"] is True


def test_an_edit_that_advances_the_stamp_is_captured_even_with_identical_text(client, enabled):
    """A real edit always advances ``edit_date`` — that alone is a new version."""
    _capture(client, _tl_message(text="same"))
    later = _DATED + timedelta(minutes=5)
    assert _deliver_as_edit(client, _edit_update(_tl_message(text="same", edit_date=later))) is not None
    assert sorted(_kinds(_rows(client))) == [svc.KIND_EDIT, svc.KIND_SNAPSHOT]


def test_a_stale_unchanged_edit_update_cannot_re_add_a_version(client, enabled):
    _capture(client, _tl_message(text="v1"))
    newest = _DATED + timedelta(minutes=10)
    assert _deliver_as_edit(client, _edit_update(_tl_message(text="v2", edit_date=newest))) is not None
    # Same content as the newest stored version, with an OLDER stamp: no version.
    stale = _DATED + timedelta(minutes=1)
    assert _deliver_as_edit(client, _edit_update(_tl_message(text="v2", edit_date=stale))) is not None
    rows = _rows(client)
    assert sorted(_kinds(rows)) == [svc.KIND_EDIT, svc.KIND_SNAPSHOT]


def test_a_redelivered_edit_is_recorded_once(client, enabled):
    _capture(client, _tl_message(text="v1"))
    edited = _tl_message(text="v2", edit_date=datetime.now(timezone.utc))
    _deliver_as_edit(client, _edit_update(edited))
    _deliver_as_edit(client, _edit_update(edited))
    assert sorted(_kinds(_rows(client))) == [svc.KIND_EDIT, svc.KIND_SNAPSHOT]


def test_reactions_do_not_disturb_deletion_recovery(client, enabled):
    _capture(client, _tl_message(message_id=9, text="secret"))
    _deliver_as_edit(client, _edit_update(
        _tl_message(message_id=9, text="secret", reactions=_reaction_state("👍"))
    ))
    _deliver_as_edit(client, _reaction_update(message_id=9))
    _run(client.handler("MessageDeleted")(_deleted_event(ids=(9,))))

    rows = _rows(client)
    assert _kinds(rows).count(svc.KIND_EDIT) == 0
    deletion = next(row for row in rows if row["kind"] == svc.KIND_DELETE)
    assert deletion["recoverable"] is True
    assert deletion["body"] == "secret"
    assert deletion["recovered_from"] == next(
        row["id"] for row in rows if row["kind"] == svc.KIND_SNAPSHOT
    )


def test_the_reaction_only_classification_is_boundary_safe():
    """No edit stamp → no edit; a missing/malformed message never raises."""
    assert svc.is_content_edit(_tl_message()) is False
    assert svc.is_content_edit(_tl_message(edit_date=_DATED)) is True
    assert svc.is_content_edit(SimpleNamespace()) is False
    assert svc.is_content_edit(None) is False
