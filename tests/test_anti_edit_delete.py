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
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

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
GROUP = -1001234567890


# ── fake database (the bot_settings key/value table) ─────────────────────────


class _Result:
    def __init__(self, data):
        self.data = data


class _FakeDB:
    """``bot_settings`` with the query shapes the db helpers use."""

    def __init__(self, fail_write: bool = False):
        self.rows: dict[str, dict] = {}
        self.fail_write = fail_write
        self._table = None
        self._op = None
        self._payload = None
        self._filters: list[tuple[str, str, object]] = []
        self._limit = None
        self._single = False

    def table(self, name):
        assert name == "bot_settings", name
        self._table, self._op, self._payload = name, None, None
        self._filters, self._limit, self._single = [], None, False
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

    def eq(self, column, value):
        self._filters.append(("eq", column, value))
        return self

    def like(self, column, pattern):
        self._filters.append(("like", column, pattern))
        return self

    def limit(self, value):
        self._limit = value
        return self

    def maybe_single(self):
        self._single = True
        return self

    def _match(self):
        out = []
        for key, row in self.rows.items():
            ok = True
            for op, column, value in self._filters:
                if op == "eq" and row.get(column) != value:
                    ok = False
                if op == "like":
                    prefix = str(value).rstrip("%")
                    if not str(row.get(column, "")).startswith(prefix):
                        ok = False
            if ok:
                out.append(row)
        return out

    def execute(self):
        if self._op == "insert":
            if self.fail_write:
                raise RuntimeError("write refused")
            self.rows[self._payload["key"]] = dict(self._payload)
            return _Result([dict(self._payload)])
        if self._op == "update":
            if self.fail_write:
                raise RuntimeError("write refused")
            for row in self._match():
                row.update(self._payload)
            return _Result([dict(r) for r in self._match()])
        matched = self._match()
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


def _deleted_event(*, chat_id=FRIEND, ids=(7,), is_private=True):
    return SimpleNamespace(
        chat_id=chat_id,
        deleted_ids=list(ids),
        is_private=is_private,
    )


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ── fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    svc.reset_in_memory()
    svc.configure_owner(OWNER)
    inline_engine.set_owner_id(OWNER)
    inline_engine.set_self_client(None)
    yield
    svc.reset_in_memory()
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
    return db


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
    assert get_registry().get_parent("archive") == "menu"


def test_the_mother_menu_opens_the_archive_panel(client):
    assert "panel:archive" in _datas(misc._build_menu_buttons())


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


def test_scope_gate_accepts_only_private_user_chats():
    assert svc.eligible(owner=OWNER, chat_id=FRIEND, is_private=True) == (True, "eligible")
    assert svc.eligible(owner=OWNER, chat_id=GROUP, is_private=True)[0] is False
    assert svc.eligible(owner=OWNER, chat_id=OWNER, is_private=True)[1] == "saved_messages"
    assert svc.eligible(owner=OWNER, chat_id=FRIEND, is_private=False)[0] is False
    assert svc.eligible(owner=OWNER, chat_id=FRIEND, is_private=True, via_bot_id=99)[0] is False


# ── 5. snapshot capture ──────────────────────────────────────────────────────


def test_a_private_message_is_snapshotted_while_enabled(client, enabled):
    handler = client.handler("NewMessage")
    _run(handler(SimpleNamespace(message=_msg(text="meet at 5pm"))))
    rows = _run(svc.load_rows_async())
    assert len(rows) == 1
    assert rows[0]["kind"] == svc.KIND_SNAPSHOT
    assert rows[0]["body"] == "meet at 5pm"
    assert rows[0]["chat_id"] == FRIEND


def test_capture_is_inert_while_disabled(client, db):
    handler = client.handler("NewMessage")
    _run(handler(SimpleNamespace(message=_msg())))
    assert _run(svc.load_rows_async()) == []


def test_out_of_scope_messages_are_never_captured(client, enabled):
    handler = client.handler("NewMessage")
    _run(handler(SimpleNamespace(message=_msg(chat_id=GROUP))))
    _run(handler(SimpleNamespace(message=_msg(chat_id=OWNER))))
    _run(handler(SimpleNamespace(message=_msg(via_bot_id=42))))
    assert _run(svc.load_rows_async()) == []


def test_a_redelivered_snapshot_is_recorded_once(client, enabled):
    handler = client.handler("NewMessage")
    message = _msg()
    _run(handler(SimpleNamespace(message=message)))
    _run(handler(SimpleNamespace(message=message)))
    assert len(_run(svc.load_rows_async())) == 1


def test_category_filter_is_honoured_on_capture(client, enabled):
    assert _run(svc.set_category(svc.CATEGORY_TEXT, False)) is True
    handler = client.handler("NewMessage")
    _run(handler(SimpleNamespace(message=_msg(text="plain"))))
    assert _run(svc.load_rows_async()) == []


# ── 6. edits ─────────────────────────────────────────────────────────────────


def test_an_edit_is_recorded_as_a_new_version(client, enabled):
    _run(client.handler("NewMessage")(SimpleNamespace(message=_msg(text="v1"))))
    _run(client.handler("MessageEdited")(
        SimpleNamespace(message=_msg(text="v2", edit_date=datetime.now(timezone.utc)))
    ))
    rows = _run(svc.load_rows_async())
    kinds = [r["kind"] for r in rows]
    assert kinds.count(svc.KIND_SNAPSHOT) == 1
    assert kinds.count(svc.KIND_EDIT) == 1


# ── 7. deletions (honest recoverability) ─────────────────────────────────────


def test_a_deletion_after_a_snapshot_is_recoverable(client, enabled):
    _run(client.handler("NewMessage")(SimpleNamespace(message=_msg(message_id=9, text="secret"))))
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
    _run(client.handler("NewMessage")(SimpleNamespace(message=_msg(message_id=1, text="alpha"))))
    _run(client.handler("NewMessage")(SimpleNamespace(message=_msg(message_id=2, text="beta"))))
    rows = _run(svc.load_rows_async())
    assert [r["body"] for r in svc.filter_rows(rows, query="alph")] == ["alpha"]
    assert len(svc.filter_rows(rows, category=svc.CATEGORY_TEXT)) == 2


def test_caption_filter_matches_media_with_captions(client, enabled):
    media = MessageMediaPhoto()
    _run(client.handler("NewMessage")(
        SimpleNamespace(message=_msg(message_id=3, text="a caption", media=media))
    ))
    rows = _run(svc.load_rows_async())
    assert len(svc.filter_rows(rows, category=svc.CATEGORY_CAPTIONS)) == 1
    assert len(svc.filter_rows(rows, category=svc.CATEGORY_PHOTOS)) == 1


def test_pagination_is_bounded_and_clamped(client, enabled):
    for index in range(svc.PAGE_SIZE + 3):
        _run(client.handler("NewMessage")(
            SimpleNamespace(message=_msg(message_id=100 + index, text=f"m{index}"))
        ))
    rows = _run(svc.load_rows_async())
    first, page, pages = svc.page_of(rows, 1)
    assert len(first) == svc.PAGE_SIZE and page == 1 and pages == 2
    second, page, pages = svc.page_of(rows, 99)
    assert page == pages


def test_delete_one_row_and_clear_archive(client, enabled):
    _run(client.handler("NewMessage")(SimpleNamespace(message=_msg(message_id=1))))
    _run(client.handler("NewMessage")(SimpleNamespace(message=_msg(message_id=2))))
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
    _run(db_client.aed_save_archive(list(svc._SHARD_KEYS), [[rows[0]], [rows[1]], [], []]))
    stats = _run(svc.cleanup_expired(now=now))
    assert set(stats) == {"archive_deleted", "snapshots_deleted", "objects_deleted"}
    assert stats["snapshots_deleted"] == 1
    remaining = _run(db_client.aed_load_archive(list(svc._SHARD_KEYS)))
    kept_ids = [r["id"] for shard in remaining for r in shard]
    assert kept_ids == ["new1"]


def test_cleanup_expired_is_a_noop_without_expired_rows(db):
    _run(svc.set_enabled(True))
    stats = _run(svc.cleanup_expired())
    assert stats == {"archive_deleted": 0, "snapshots_deleted": 0, "objects_deleted": 0}


# ── 10. media honesty ────────────────────────────────────────────────────────


def test_media_metadata_is_bounded_and_never_fabricated(client, enabled):
    doc = _document([DocumentAttributeFilename("report.pdf")], size=999)
    media = MessageMediaDocument(doc)
    _run(client.handler("NewMessage")(SimpleNamespace(message=_msg(message_id=4, text="", media=media))))
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
    _run(client.handler("NewMessage")(SimpleNamespace(message=_msg(message_id=5, media=media))))
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
    _run(client.handler("NewMessage")(SimpleNamespace(message=_msg(text="remember this"))))
    title, body, buttons = _panel("archive_list")
    rendered = body + " ".join(_texts(buttons))
    assert "remember this" in rendered


def test_item_panel_marks_recoverability(client, enabled):
    _run(client.handler("NewMessage")(SimpleNamespace(message=_msg(message_id=9, text="x"))))
    _run(client.handler("MessageDeleted")(_deleted_event(ids=(9,))))
    rows = _run(svc.load_rows_async())
    deletion = next(r for r in rows if r["kind"] == svc.KIND_DELETE)
    title, body, buttons = _panel("archive_item", deletion["id"])
    assert "Recoverable" in body
    assert f"action:aed_delete:{deletion['id']}" in _datas(buttons)


def test_clear_confirm_panel_and_action(client, enabled):
    _run(client.handler("NewMessage")(SimpleNamespace(message=_msg())))
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
