"""Keepalive must not treat Telegram's FloodWait as a broken connection.

Production symptom: ``KEEPALIVE_FAILED — FloodWaitError`` followed by
``WATCHDOG_RECOVERY reason=reconnect`` while the connection itself was healthy.
These tests drive the real ``_probe``/``_keepalive_loop`` path with a fake
client: FloodWait suppresses probing for a bounded window, genuine connection
errors and timeouts still reconnect, and cancellation propagates.
"""
from __future__ import annotations

import asyncio
import time

import pytest
from telethon.errors import FloodWaitError

from backend.runtime import keepalive


class _FakeClient:
    def __init__(self, *, exc=None, delay=0.0):
        self.calls = 0
        self.exc = exc
        self.delay = delay

    async def get_me(self):
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.exc is not None:
            raise self.exc
        return {"id": 1}


class _FakeSupervisor:
    def __init__(self, client):
        self.client = client
        self._client_alive = True
        self._recovery_lock = asyncio.Lock()
        self.client_generation = 3
        self.shutdown_event = asyncio.Event()
        self.reconnects = 0

    async def _trigger_reconnect(self):
        self.reconnects += 1


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


@pytest.fixture(autouse=True)
def _restore_keepalive(monkeypatch):
    keepalive._floodwait_until = 0.0
    keepalive._supervisor_ref = None
    keepalive._task = None
    yield
    keepalive._floodwait_until = 0.0
    keepalive._supervisor_ref = None
    keepalive._task = None


def test_floodwait_does_not_trigger_reconnect():
    client = _FakeClient(exc=FloodWaitError(request=None, capture=120))
    sup = _FakeSupervisor(client)

    outcome = _run(keepalive._probe(sup))

    assert outcome == "floodwait"
    assert sup.reconnects == 0
    assert client.calls == 1
    assert keepalive.floodwait_remaining() > 0


def test_next_probe_is_suppressed_until_the_wait_expires():
    client = _FakeClient(exc=FloodWaitError(request=None, capture=120))
    sup = _FakeSupervisor(client)
    assert _run(keepalive._probe(sup)) == "floodwait"

    # Still inside the window: no second get_me at all.
    assert _run(keepalive._probe(sup)) == "floodwait_skip"
    assert client.calls == 1
    assert sup.reconnects == 0

    # Expire the deadline: the next probe is allowed again.
    keepalive._floodwait_until = time.monotonic() - 0.01
    client.exc = None
    assert _run(keepalive._probe(sup)) == "ok"
    assert client.calls == 2
    assert keepalive.floodwait_remaining() == 0


def test_floodwait_is_bounded_even_when_the_server_asks_for_days():
    client = _FakeClient(exc=FloodWaitError(request=None, capture=999999))
    sup = _FakeSupervisor(client)

    _run(keepalive._probe(sup))

    assert keepalive.floodwait_remaining() <= keepalive._FLOODWAIT_MAX_S
    assert keepalive.floodwait_remaining() > 0


def test_genuine_connection_error_still_triggers_reconnect():
    client = _FakeClient(exc=ConnectionError("transport closed"))
    sup = _FakeSupervisor(client)

    outcome = _run(keepalive._probe(sup))

    assert outcome == "reconnect"
    assert sup.reconnects == 1
    assert keepalive.floodwait_remaining() == 0


def test_timeout_behavior_is_unchanged(monkeypatch):
    monkeypatch.setattr(keepalive, "_RPC_TIMEOUT", 0.01)
    client = _FakeClient(delay=0.2)
    sup = _FakeSupervisor(client)

    outcome = _run(keepalive._probe(sup))

    assert outcome == "timeout"
    assert sup.reconnects == 1
    assert keepalive.floodwait_remaining() == 0


def test_a_disconnected_client_is_not_probed_or_reconnected():
    client = _FakeClient()
    sup = _FakeSupervisor(client)
    sup._client_alive = False

    assert _run(keepalive._probe(sup)) == "skipped"
    assert client.calls == 0
    assert sup.reconnects == 0


def test_cancellation_during_floodwait_propagates(monkeypatch):
    monkeypatch.setattr(keepalive, "_INTERVAL", 0.01)
    client = _FakeClient(exc=FloodWaitError(request=None, capture=60))
    sup = _FakeSupervisor(client)
    keepalive.configure(sup)

    async def _exercise():
        task = asyncio.create_task(keepalive._keepalive_loop())
        # Let it hit the FloodWait and enter the suppressed sleep.
        for _ in range(200):
            await asyncio.sleep(0.005)
            if keepalive.floodwait_remaining() > 0:
                break
        assert client.calls >= 1
        assert sup.reconnects == 0
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return task.cancelled()

    assert _run(_exercise()) is True
    assert sup.reconnects == 0
