"""Runtime diagnostics classification + heartbeat freshness.

Production symptom this file locks down: the periodic ``ASYNC_TASK_DUMP``
reported the normal long-lived runtime loops as ``BOUNDED`` and then as
``TASK_NO_PROGRESS`` / ``TASK_STARVATION`` while the same sample showed fresh
Telethon/update/RPC timestamps and sub-millisecond loop latency:

  - Telethon ``_update_loop`` / ``_recv_loop`` / ``_send_loop``
  - mtprotosender loops
  - ``lifeos-task-scheduler`` waiting on its normal wake event
  - ``lifeos-helper`` / the web-server wrapper

and the ``health: heartbeat stale`` warning contradicted those fresh
timestamps because ``_last_heartbeat`` was written only once, at startup.

Contract under test:

- Long-lived runtime/transport loops are classified permanent, whatever their
  task name (Telethon names its loops ``Task-N``).
- A genuinely stalled BOUNDED task is still detected (both TASK_NO_PROGRESS
  and TASK_STARVATION), and loop-latency stall detection still fires.
- Starvation is only reported for tasks that still exist right now.
- The canonical heartbeat loop refreshes the health timestamp, so
  ``process_alive`` / the staleness warning describe the live runtime — while a
  heartbeat that really stops is still detected.

In-process only: no live Telegram, no live Supabase.
"""
from __future__ import annotations

import asyncio
import logging
import time

import pytest

from backend.runtime import diagnostics as diag


@pytest.fixture(autouse=True)
def _restore_diagnostics_state():
    saved = (
        dict(diag._prev_stacks),
        dict(diag._stack_unchanged_count),
        dict(diag._task_first_seen),
    )
    diag._prev_stacks.clear()
    diag._stack_unchanged_count.clear()
    diag._task_first_seen.clear()
    yield
    diag._prev_stacks.clear()
    diag._prev_stacks.update(saved[0])
    diag._stack_unchanged_count.clear()
    diag._stack_unchanged_count.update(saved[1])
    diag._task_first_seen.clear()
    diag._task_first_seen.update(saved[2])


@pytest.fixture(autouse=True)
def _restore_health_state():
    from backend import health

    saved = (health._last_heartbeat, health._last_stale_warn, health._started_at)
    yield
    health._last_heartbeat, health._last_stale_warn, health._started_at = saved


async def _update_loop():  # the coroutine name Telethon actually uses
    await asyncio.sleep(60)


async def _bounded_slow_op():
    await asyncio.sleep(60)


async def _scheduler_wait():
    stop = asyncio.Event()
    await asyncio.wait_for(stop.wait(), timeout=60)


# ── permanent classification ───────────────────────────────────────────────


def test_telethon_transport_loops_are_permanent_by_name_and_source():
    assert diag._is_permanent_task("Task-1", "_update_loop") is True
    assert diag._is_permanent_task("Task-2", "_recv_loop") is True
    assert diag._is_permanent_task("Task-3", "_send_loop") is True
    assert diag._is_permanent_task("Task-4", "_keepalive_loop") is True
    # Name-independent: any Telethon machinery is long-lived by construction.
    assert diag._is_permanent_task(
        "Task-5", "unknown_coro",
        "/app/.venv/lib/python3.10/site-packages/telethon/client/updates.py:258",
    ) is True
    # A bounded application coroutine is never permanent.
    assert diag._is_permanent_task("bounded-slow-op", "_bounded_slow_op", "/app/backend/x.py") is False


def test_runtime_loops_are_permanent_by_their_assigned_names():
    for name in (
        "lifeos-run", "lifeos-heartbeat", "lifeos-keepalive", "lifeos-failsafe",
        "lifeos-diagnostics", "lifeos-memory-cleanup", "lifeos-web-server",
        "lifeos-helper", "lifeos-task-scheduler", "lifeos-profile-scheduler",
    ):
        assert diag._is_permanent_task(name, "run") is True, name


@pytest.mark.asyncio
async def test_task_scheduler_wait_is_not_starvation(caplog):
    scheduler = asyncio.create_task(_scheduler_wait(), name="lifeos-task-scheduler")
    telethon = asyncio.create_task(_update_loop(), name="Task-9")
    try:
        await asyncio.sleep(0)
        with caplog.at_level(logging.WARNING, logger="backend.diagnostics_loop"):
            for _ in range(4):
                await diag._dump_tasks()
    finally:
        for task in (scheduler, telethon):
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    assert "lifeos-task-scheduler" not in caplog.text
    assert "Task-9" not in caplog.text
    assert "NO_PROGRESS — " not in caplog.text or "lifeos-task-scheduler" not in caplog.text


# ── genuine bounded stalls are still detected ──────────────────────────────


@pytest.mark.asyncio
async def test_a_genuinely_unchanged_bounded_task_is_still_detected(caplog):
    task = asyncio.create_task(_bounded_slow_op(), name="bounded-slow-op")
    try:
        await asyncio.sleep(0)
        with caplog.at_level(logging.WARNING, logger="backend.diagnostics_loop"):
            for _ in range(4):
                await diag._dump_tasks()
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert "bounded-slow-op" in caplog.text
    assert "TASK_NO_PROGRESS" in caplog.text
    assert "TASK_STARVATION" in caplog.text
    # The permanent loops are never part of that report.
    assert "lifeos-" not in caplog.text


@pytest.mark.asyncio
async def test_starvation_state_is_pruned_when_the_task_is_gone(caplog):
    """A counter left by a finished task must not be reported forever."""
    diag._stack_unchanged_count["finished-task"] = 9
    diag._task_first_seen["finished-task"] = 0.0

    with caplog.at_level(logging.WARNING, logger="backend.diagnostics_loop"):
        await diag._dump_tasks()

    assert "finished-task" not in diag._stack_unchanged_count
    assert "finished-task" not in diag._task_first_seen
    assert "STARVATION: finished-task" not in caplog.text


class _Clock:
    """Deterministic clock: [before_sleep, after_sleep, ...] for monotonic."""

    def __init__(self, values: list[float]) -> None:
        self._values = list(values)
        self._last = values[-1] if values else 0.0

    def monotonic(self) -> float:
        if self._values:
            self._last = self._values.pop(0)
        return self._last

    def time(self) -> float:
        return 1_000_000.0


@pytest.mark.asyncio
async def test_event_loop_stall_detection_still_fires(monkeypatch, caplog):
    monkeypatch.setattr(diag, "time", _Clock([0.0, 0.6]))
    with caplog.at_level(logging.WARNING, logger="backend.diagnostics_loop"):
        await diag._dump_tasks()
    assert "EVENT_LOOP_STALL" in caplog.text
    assert diag._STALL_THRESHOLD_MS == 500.0


@pytest.mark.asyncio
async def test_a_fast_dump_never_reports_a_stall(monkeypatch, caplog):
    monkeypatch.setattr(diag, "time", _Clock([0.0, 0.001]))
    with caplog.at_level(logging.WARNING, logger="backend.diagnostics_loop"):
        await diag._dump_tasks()
    assert "EVENT_LOOP_STALL" not in caplog.text


# ── heartbeat freshness ────────────────────────────────────────────────────


def test_heartbeat_is_fresh_after_the_canonical_writer_runs():
    from backend import health

    health.mark_started()
    health.set_heartbeat()
    snapshot = health.snapshot()
    assert snapshot["process_alive"] is True
    assert snapshot["heartbeat_age_s"] is not None
    assert snapshot["heartbeat_age_s"] < 1.0


def test_a_really_stale_heartbeat_is_still_detected(caplog):
    from backend import health

    health.mark_started()
    health._last_heartbeat = time.time() - 500.0
    health._last_stale_warn = 0.0
    with caplog.at_level(logging.WARNING, logger="backend.health"):
        snapshot = health.snapshot()
    assert snapshot["process_alive"] is False
    assert snapshot["status"] == "degraded"
    assert "heartbeat stale" in caplog.text


def test_the_staleness_window_exceeds_one_beat_interval():
    """15s against a 30s loop reported a healthy runtime as stale half the time."""
    from backend import health
    from backend.runtime import heartbeat

    assert health._STALE_THRESHOLD > heartbeat._INTERVAL


@pytest.mark.asyncio
async def test_the_heartbeat_loop_is_the_writer_of_the_canonical_timestamp(monkeypatch):
    from backend import health
    from backend.runtime import heartbeat

    monkeypatch.setattr(heartbeat, "_INTERVAL", 0.01)
    health.mark_started()
    health._last_heartbeat = 0.0  # dead timestamp, exactly like production

    task = asyncio.create_task(heartbeat._heartbeat_loop())
    try:
        for _ in range(200):
            await asyncio.sleep(0.01)
            if health._last_heartbeat > 0:
                break
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert health._last_heartbeat > 0
    assert health._heartbeat_age() < 1.0
    assert health.snapshot()["process_alive"] is True
