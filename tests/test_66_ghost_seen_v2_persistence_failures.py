from __future__ import annotations

import asyncio
import json

import pytest

from backend.services import ghost_seen_v2 as service


class _Result:
    def __init__(self, data):
        self.data = data


class _FailingDB:
    def table(self, name):
        assert name == "bot_settings"
        return self

    def select(self, _columns):
        return self

    def eq(self, _column, _value):
        return self

    def maybe_single(self):
        return self

    def execute(self):
        raise RuntimeError("database unavailable")


@pytest.mark.asyncio
async def test_database_write_failure_is_reported(monkeypatch):
    monkeypatch.setattr("backend.db.client.get_db", lambda: _FailingDB())
    service.reset_allowed_chats()

    assert await service.allow_chat_and_persist(123) is False
    assert service.is_chat_allowed(123) is True


@pytest.mark.asyncio
async def test_malformed_stored_json_fails_closed(monkeypatch):
    class _MalformedDB:
        def table(self, name):
            assert name == "bot_settings"
            return self

        def select(self, _columns):
            return self

        def eq(self, _column, _value):
            return self

        def maybe_single(self):
            return self

        def execute(self):
            return _Result({"value": "not-json"})

    monkeypatch.setattr("backend.db.client.get_db", lambda: _MalformedDB())
    service._allowed_chats.clear()
    service._allowed_loaded = False
    service._allowed_load_task = None

    await service._ensure_allowed_loaded_async()
    assert service.get_allowed_chats() == frozenset()
    assert service.is_chat_allowed(123) is False
