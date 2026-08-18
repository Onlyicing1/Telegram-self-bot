"""
Bio + Username engine and shared Profile Scheduler regression tests.

Covers:
- Rendering ({time}, {mood}, {text}) for both engines and the corrected Bio
  default template.
- Updater dedup (unchanged → None) and inactive → None.
- Shared scheduler merge: both engines collapse into ONE update dict.
- The shared-scheduler stop bug: turning one engine off must NOT stop the
  other while it is still active.
- start_cron idempotency.
- Symmetric bio/username health telemetry.
- Bio/Username DB get_or_create fallback consistency.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from backend import health
from backend.bio import engine as bio_engine
from backend.db import client as db_client
from backend.profile import scheduler as profile_scheduler
from backend.profile.engine import ProfileEngine
from backend.username import engine as username_engine


class FakeClient:
    """Callable stand-in for the Telethon client (never actually invoked in
    these tests because no profile updates are produced)."""

    def __init__(self) -> None:
        self.calls: list = []

    async def __call__(self, request):
        self.calls.append(request)
        return None


@pytest.fixture(autouse=True)
def _reset_profile_state():
    """Isolate module-global scheduler/engine registries between tests.

    Only synchronous state is reset here. Async tests that start the real
    scheduler task cancel it in their own ``finally`` blocks.
    """
    profile_scheduler._updaters.clear()
    profile_scheduler._active_engines.clear()
    bio_engine._engine._registered = False
    username_engine._engine._registered = False
    yield
    profile_scheduler._updaters.clear()
    profile_scheduler._active_engines.clear()
    bio_engine._engine._registered = False
    username_engine._engine._registered = False


# ── Rendering ──

def test_bio_render_replaces_tokens():
    result = bio_engine.render_bio("🕒 {time} | 💭 {mood} | {text}", "😊", "Working", "UTC")
    assert "{time}" not in result
    assert "{mood}" not in result
    assert "{text}" not in result
    assert "😊" in result
    assert "Working" in result


def test_username_render_replaces_tokens():
    result = username_engine.render_username("{time} | {mood} | {text}", "🔥", "Busy", "UTC")
    assert "{time}" not in result
    assert "{mood}" not in result
    assert "{text}" not in result
    assert "🔥" in result
    assert "Busy" in result


def test_bio_render_uses_wellformed_default_template():
    result = bio_engine.render_bio("", "😊", "Working", "UTC")
    assert "💭 😊" in result
    assert "{time}" not in result
    assert "{mood}" not in result


def test_bio_handler_default_template_is_well_formed():
    from backend.bot.handlers import bio as bio_handler

    assert bio_handler._DEFAULT_TEMPLATE == "🕒 {time} | 💭 {mood}"


def test_engine_field_wiring():
    assert bio_engine._engine.name == "bio"
    assert bio_engine._engine.field == "about"
    assert bio_engine._engine.state_key == "last_bio"
    assert username_engine._engine.name == "username"
    assert username_engine._engine.field == "first_name"
    assert username_engine._engine.state_key == "last_name"


# ── Updater dedup / inactive ──

def _make_engine(field: str, state_key: str, state: dict, update_calls: list) -> ProfileEngine:
    async def get_state(owner_id: int):
        return state

    async def update_state(owner_id: int, updates: dict):
        update_calls.append(updates)

    return ProfileEngine(
        name="test",
        field=field,
        state_key=state_key,
        default_template="{mood}",
        get_state=get_state,
        update_state=update_state,
    )


@pytest.mark.asyncio
async def test_updater_dedup_returns_none_when_unchanged():
    state = {
        "is_active": True,
        "template": "X {mood}",
        "mood": "😊",
        "custom_text": "",
        "last_bio": "X 😊",
    }
    calls: list = []
    engine = _make_engine("about", "last_bio", state, calls)

    result = await engine.updater(7770001, "UTC")
    assert result is None
    assert calls == []


@pytest.mark.asyncio
async def test_updater_returns_field_when_changed():
    state = {
        "is_active": True,
        "template": "X {mood}",
        "mood": "😊",
        "custom_text": "",
        "last_bio": "",
    }
    calls: list = []
    engine = _make_engine("about", "last_bio", state, calls)

    result = await engine.updater(7770001, "UTC")
    assert result == {"about": "X 😊"}
    assert len(calls) == 1
    assert calls[0]["last_bio"] == "X 😊"
    assert "updated_at" in calls[0]


@pytest.mark.asyncio
async def test_updater_inactive_returns_none():
    state = {
        "is_active": False,
        "template": "X {mood}",
        "mood": "😊",
        "custom_text": "",
        "last_bio": "",
    }
    calls: list = []
    engine = _make_engine("about", "last_bio", state, calls)

    result = await engine.updater(7770001, "UTC")
    assert result is None
    assert calls == []


# ── Scheduler merge ──

@pytest.mark.asyncio
async def test_collect_updates_merges_bio_and_username():
    async def bio_updater(owner_id, tz_str):
        return {"about": "bio-value"}

    async def username_updater(owner_id, tz_str):
        return {"first_name": "uname-value"}

    profile_scheduler.register_updater("bio", bio_updater)
    profile_scheduler.register_updater("username", username_updater)

    merged = await profile_scheduler._collect_updates(7770001, "UTC")
    assert merged == {"about": "bio-value", "first_name": "uname-value"}


@pytest.mark.asyncio
async def test_collect_updates_bio_only():
    async def bio_updater(owner_id, tz_str):
        return {"about": "bio-value"}

    async def username_updater(owner_id, tz_str):
        return None

    profile_scheduler.register_updater("bio", bio_updater)
    profile_scheduler.register_updater("username", username_updater)

    merged = await profile_scheduler._collect_updates(7770001, "UTC")
    assert merged == {"about": "bio-value"}


@pytest.mark.asyncio
async def test_collect_updates_username_only():
    async def bio_updater(owner_id, tz_str):
        return None

    async def username_updater(owner_id, tz_str):
        return {"first_name": "uname-value"}

    profile_scheduler.register_updater("bio", bio_updater)
    profile_scheduler.register_updater("username", username_updater)

    merged = await profile_scheduler._collect_updates(7770001, "UTC")
    assert merged == {"first_name": "uname-value"}


@pytest.mark.asyncio
async def test_collect_updates_empty_when_no_change():
    async def noop_updater(owner_id, tz_str):
        return None

    profile_scheduler.register_updater("bio", noop_updater)
    profile_scheduler.register_updater("username", noop_updater)

    merged = await profile_scheduler._collect_updates(7770001, "UTC")
    assert merged == {}


# ── Scheduler lifecycle ──

@pytest.mark.asyncio
async def test_start_cron_is_idempotent():
    try:
        client = FakeClient()
        profile_scheduler.start_cron(client, 7770001, "UTC")
        await asyncio.sleep(0)
        first_task = profile_scheduler._task
        assert profile_scheduler.is_running()

        profile_scheduler.start_cron(client, 7770001, "UTC")
        assert profile_scheduler._task is first_task
    finally:
        await profile_scheduler.stop_cron()


@pytest.mark.asyncio
async def test_bio_off_preserves_username_via_engine():
    try:
        bio_engine.start_cron(FakeClient(), 7770001, "UTC")
        username_engine.start_cron(FakeClient(), 7770001, "UTC")
        await asyncio.sleep(0)
        assert profile_scheduler.is_running()

        await bio_engine.stop_cron()
        assert profile_scheduler.is_running(), "Bio OFF must not stop the active Username engine"

        await username_engine.stop_cron()
        assert not profile_scheduler.is_running()
    finally:
        await profile_scheduler.stop_cron()


@pytest.mark.asyncio
async def test_username_off_preserves_bio():
    try:
        profile_scheduler.set_engine_active("bio", True)
        profile_scheduler.set_engine_active("username", True)
        profile_scheduler.start_cron(FakeClient(), 7770001, "UTC")
        await asyncio.sleep(0)
        assert profile_scheduler.is_running()

        profile_scheduler.set_engine_active("username", False)
        await profile_scheduler.stop_if_idle()
        assert profile_scheduler.is_running(), "Username OFF must not stop the active Bio engine"

        profile_scheduler.set_engine_active("bio", False)
        await profile_scheduler.stop_if_idle()
        assert not profile_scheduler.is_running()
    finally:
        await profile_scheduler.stop_cron()


# ── Health telemetry ──

def test_health_telemetry_separation():
    health.set_bio_cron_ok(True)
    health.set_username_cron_ok(True)
    health.set_last_bio_update()
    health.set_last_username_update()

    snap = health.snapshot()
    assert snap["bio_cron_ok"] is True
    assert snap["username_cron_ok"] is True
    assert snap["last_bio_update_s"] is not None
    assert snap["last_username_update_s"] is not None


def test_record_update_telemetry_dispatch(monkeypatch):
    calls = {"bio": 0, "username": 0}

    def bio_tick():
        calls["bio"] += 1

    def username_tick():
        calls["username"] += 1

    monkeypatch.setattr(health, "set_last_bio_update", bio_tick)
    monkeypatch.setattr(health, "set_last_username_update", username_tick)

    profile_scheduler._record_update_telemetry({"about": "x"})
    assert calls == {"bio": 1, "username": 0}

    profile_scheduler._record_update_telemetry({"first_name": "y"})
    assert calls == {"bio": 1, "username": 1}


# ── DB fallback consistency ──

def test_get_or_create_fallback_consistency():
    class FailingDB:
        def table(self, name):
            raise RuntimeError("db unavailable")

    with patch.object(db_client, "get_db", return_value=FailingDB()):
        bio_state = db_client._get_or_create_bio_state_sync(999001)
        uname_state = db_client._get_or_create_username_state_sync(999001)

    assert bio_state["owner_id"] == 999001
    assert uname_state["owner_id"] == 999001
    assert bio_state["is_active"] is False
    assert uname_state["is_active"] is False


# ── Service do_off must actually await the engine stop ──

@pytest.mark.asyncio
async def test_bio_do_off_awaits_stop(monkeypatch):
    from backend.services import bio_service

    stopped: list = []

    async def fake_stop():
        stopped.append(True)

    monkeypatch.setattr(bio_service.bio_engine, "stop_cron", fake_stop)
    monkeypatch.setattr(
        bio_service.db_client,
        "update_bio_state",
        AsyncMock(return_value=None),
    )

    result = await bio_service.do_off(7770001)
    assert stopped == [True]
    assert "OFF" in result
