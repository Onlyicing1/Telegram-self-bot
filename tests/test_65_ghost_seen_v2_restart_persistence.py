"""Ghost Seen v2 — allow-list persistence across a full Self Bot restart.

Proves the required lifecycle end-to-end against a fake ``bot_settings``
table: enable → persist (write) → fresh-process reset (restart) → load
from DB (read) → the restored allow-list is what the service enforces.

Also locks the two persistence races fixed in the service:
  - a toggle concurrent with the initial load must await that load, so it
    can never persist a partial list over the persisted one;
  - rapid toggles must never leave a stale (older) list in the DB.
"""
from __future__ import annotations

import asyncio
import json
import threading

import pytest

from backend.services import ghost_seen_v2 as service_module


class _FakeResult:
    def __init__(self, data):
        self.data = data


class _FakeBotSettingsDB:
    """Chainable fake for the exact ``bot_settings`` access chains used by
    ``backend/services/ghost_seen_v2.py`` (load: select/eq/maybe_single/
    execute; persist: probe select/eq/execute + update-or-insert)."""

    def __init__(self, row_exists: bool = False, stored_value: str | None = None, gate: threading.Event | None = None):
        self.row_exists = row_exists
        self.stored_value = stored_value
        self.updates: list[str] = []
        self.inserts: list[dict] = []
        self._gate = gate
        self._pending_update: dict | None = None
        self._pending_insert: dict | None = None
        self._is_single = False

    def table(self, name):
        assert name == "bot_settings"
        return self

    def select(self, _columns):
        self._is_single = False
        return self

    def eq(self, _column, _key):
        return self

    def maybe_single(self):
        self._is_single = True
        return self

    def update(self, payload):
        self._pending_update = payload
        return self

    def insert(self, payload):
        self._pending_insert = payload
        return self

    def execute(self):
        if self._gate is not None:
            self._gate.wait()
        if self._pending_insert is not None:
            payload = self._pending_insert
            self._pending_insert = None
            self.inserts.append(payload)
            self.row_exists = True
            self.stored_value = payload["value"]
            return _FakeResult([payload])
        if self._pending_update is not None:
            payload = self._pending_update
            self._pending_update = None
            self.updates.append(payload["value"])
            self.row_exists = True
            self.stored_value = payload["value"]
            return _FakeResult([])
        if self._is_single:
            self._is_single = False
            if self.row_exists:
                return _FakeResult({"value": self.stored_value})
            return _FakeResult({})
        return _FakeResult([{"key": "ghost_seen_allowed_chats"}] if self.row_exists else [])


class _ThreadRecorder:
    """Wraps ``threading.Thread`` so tests can join the persist threads
    deterministically instead of polling."""

    def __init__(self, original):
        self._original = original
        self.threads: list[threading.Thread] = []

    def __call__(self, *args, **kwargs):
        thread = self._original(*args, **kwargs)
        self.threads.append(thread)
        return thread


def _fresh_process() -> None:
    """Simulate a complete restart: empty runtime allow-list, nothing loaded."""
    service_module._allowed_chats.clear()
    service_module._allowed_loaded = False
    service_module._allowed_load_task = None


def _record_threads(monkeypatch) -> _ThreadRecorder:
    recorder = _ThreadRecorder(threading.Thread)
    monkeypatch.setattr(threading, "Thread", recorder)
    return recorder


def _join(recorder: _ThreadRecorder) -> None:
    for thread in recorder.threads:
        thread.join(timeout=5)
    recorder.threads.clear()


@pytest.mark.asyncio
async def test_allow_chat_persists_json_array_to_bot_settings(monkeypatch):
    db = _FakeBotSettingsDB(row_exists=False)
    monkeypatch.setattr("backend.db.client.get_db", lambda: db)
    recorder = _record_threads(monkeypatch)
    _fresh_process()

    service_module.allow_chat(123)
    _join(recorder)

    assert db.inserts, "first enable must INSERT the bot_settings row"
    assert db.inserts[0]["key"] == "ghost_seen_allowed_chats"
    assert db.inserts[0]["value"] == "[123]"
    assert db.inserts[0]["value_type"] == "str"


@pytest.mark.asyncio
async def test_allowed_list_restored_from_db_after_restart(monkeypatch):
    db = _FakeBotSettingsDB(row_exists=True, stored_value=json.dumps([111, 222]))
    monkeypatch.setattr("backend.db.client.get_db", lambda: db)
    recorder = _record_threads(monkeypatch)
    _fresh_process()

    await service_module._ensure_allowed_loaded_async()
    assert service_module.is_chat_allowed(111) is True
    assert service_module.is_chat_allowed(222) is True
    assert service_module.is_chat_allowed(999) is False

    service_module.allow_chat(333)
    _join(recorder)
    assert db.updates and json.loads(db.updates[-1]) == [111, 222, 333]


@pytest.mark.asyncio
async def test_toggle_during_initial_load_persists_full_list(monkeypatch):
    """Regression: a Manage toggle racing the startup preload must await the
    in-flight DB read and persist the union — never a partial list."""
    gate = threading.Event()
    db = _FakeBotSettingsDB(row_exists=True, stored_value=json.dumps([111, 222]), gate=gate)
    monkeypatch.setattr("backend.db.client.get_db", lambda: db)
    recorder = _record_threads(monkeypatch)
    _fresh_process()

    load_task = asyncio.create_task(service_module._ensure_allowed_loaded_async())
    await asyncio.sleep(0.01)
    toggle_wait = asyncio.create_task(service_module._ensure_allowed_loaded_async())
    await asyncio.sleep(0.01)
    assert not toggle_wait.done(), "concurrent caller must await the in-flight load"

    gate.set()
    await load_task
    await toggle_wait
    assert service_module.get_allowed_chats() == frozenset({111, 222})

    service_module.allow_chat(333)
    _join(recorder)
    assert db.updates and json.loads(db.updates[-1]) == [111, 222, 333]


@pytest.mark.asyncio
async def test_rapid_toggles_never_persist_stale_list(monkeypatch):
    """Regression: serialized persist writes snapshot the set inside the
    write lock, so the DB never ends on an out-of-order stale value."""
    db = _FakeBotSettingsDB(row_exists=True, stored_value=json.dumps([111]))
    monkeypatch.setattr("backend.db.client.get_db", lambda: db)
    recorder = _record_threads(monkeypatch)
    _fresh_process()

    await service_module._ensure_allowed_loaded_async()
    service_module.allow_chat(222)
    service_module.allow_chat(333)
    _join(recorder)
    assert json.loads(db.stored_value) == [111, 222, 333]


@pytest.mark.asyncio
async def test_disallow_removes_chat_from_persisted_list(monkeypatch):
    db = _FakeBotSettingsDB(row_exists=True, stored_value=json.dumps([111]))
    monkeypatch.setattr("backend.db.client.get_db", lambda: db)
    recorder = _record_threads(monkeypatch)
    _fresh_process()

    await service_module._ensure_allowed_loaded_async()
    service_module.allow_chat(222)
    _join(recorder)
    service_module.disallow_chat(111)
    _join(recorder)
    assert json.loads(db.updates[-1]) == [222]
    assert service_module.is_chat_allowed(111) is False
