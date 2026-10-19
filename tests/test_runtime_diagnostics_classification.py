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

_INTERVAL_S = diag._INTERVAL


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


def _lifecycle_task(source: str, filename: str, name: str) -> asyncio.Task:
    """A real task whose coroutine has the production frame (file + name).

    ``_get_await_location``/``_get_coro_name`` read the coroutine's OWN frame,
    so compiling the coroutine with the production filename reproduces exactly
    what the dump sees for an asyncio.run()-created lifecycle task — no fake
    frame objects and no live runtime.
    """
    namespace: dict = {}
    exec(compile(source, filename, "exec"), namespace)
    return asyncio.create_task(namespace["main"](), name=name)


_LIFECYCLE_BODY = (
    "import asyncio\n"
    "async def main():\n"
    "    await asyncio.Event().wait()\n"
)


def test_the_process_entrypoint_task_is_permanent_not_a_stall():
    """Production symptom: ``Task-1`` = ``main`` awaiting shutdown_event.wait()."""
    assert diag._is_permanent_task(
        "Task-1", "main", "/app/backend/main.py:140"
    ) is True
    # The line suffix must not defeat the source match, and a relative entry
    # path (``python backend/main.py``) must match too.
    assert diag._is_permanent_task("Task-1", "main", "backend/main.py:140") is True


def test_the_uvicorn_lifespan_task_is_permanent():
    """Production symptom: ``Task-27`` = uvicorn's detached lifespan ``main``."""
    assert diag._is_permanent_task(
        "Task-27", "main",
        "/app/.venv/lib/python3.12/site-packages/uvicorn/lifespan/on.py:63",
    ) is True


def test_an_application_coroutine_named_main_is_still_bounded():
    """The rule is the entrypoint FILE, never the name ``main`` alone."""
    assert diag._is_permanent_task(
        "Task-7", "main", "/app/backend/services/worker.py:21"
    ) is False
    # A third-party file that merely happens to be called main.py is not the
    # process entrypoint either.
    assert diag._is_permanent_task(
        "Task-8", "main", "/app/.venv/lib/python3.12/site-packages/acme/main.py:5"
    ) is False


def test_serve_is_matched_exactly_so_observe_and_preserve_stay_bounded():
    """``serve`` was a substring pattern: ``_observe_*`` was never monitored."""
    assert diag._is_permanent_task(
        "Task-9", "serve", "/app/.venv/lib/python3.12/site-packages/uvicorn/server.py:75"
    ) is True
    for bounded in ("_observe_durable_ceiling", "_preserve_state", "observe_all"):
        assert diag._is_permanent_task(
            "bounded-op", bounded, "/app/backend/ai/database/task_repository.py:709"
        ) is False, bounded


@pytest.mark.asyncio
async def test_the_two_production_lifecycle_tasks_are_never_reported(caplog):
    """The exact post-58fa664 report: Task-1 and Task-27 as BOUNDED stalls."""
    process = _lifecycle_task(_LIFECYCLE_BODY, "/app/backend/main.py", "Task-1")
    lifespan = _lifecycle_task(
        _LIFECYCLE_BODY, "/app/.venv/lib/python3.12/site-packages/uvicorn/lifespan/on.py",
        "Task-27",
    )
    try:
        await asyncio.sleep(0)
        with caplog.at_level(logging.WARNING, logger="backend.diagnostics_loop"):
            for _ in range(4):
                await diag._dump_tasks()
    finally:
        for task in (process, lifespan):
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    reported = _reported_task_names(caplog.text)
    assert "Task-1" not in reported
    assert "Task-27" not in reported
    assert "TASK_NO_PROGRESS" not in caplog.text
    assert "TASK_STARVATION" not in caplog.text
    assert diag._stack_unchanged_count.get(process, 0) >= 3


@pytest.mark.asyncio
async def test_an_app_task_awaiting_a_shutdown_wait_is_still_reported(caplog):
    """A bounded coroutine that awaits a lifecycle-style wait is NOT hidden."""
    source = (
        "import asyncio\n"
        "_EVENT = asyncio.Event()\n"
        "async def _shutdown_wait():\n"
        "    await _EVENT.wait()\n"
        "async def main():\n"
        "    await _shutdown_wait()\n"
    )
    task = _lifecycle_task(source, "/app/backend/services/worker.py", "bounded-worker")
    try:
        await asyncio.sleep(0)
        with caplog.at_level(logging.WARNING, logger="backend.diagnostics_loop"):
            for _ in range(4):
                await diag._dump_tasks()
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert "bounded-worker" in _reported_task_names(caplog.text)
    assert "TASK_NO_PROGRESS" in caplog.text
    assert "TASK_STARVATION" in caplog.text


def test_runtime_loops_are_permanent_by_their_assigned_names():
    for name in (
        "lifeos-run", "lifeos-heartbeat", "lifeos-keepalive", "lifeos-failsafe",
        "lifeos-diagnostics", "lifeos-memory-cleanup", "lifeos-web-server",
        "lifeos-helper", "lifeos-task-scheduler", "lifeos-profile-scheduler",
    ):
        assert diag._is_permanent_task(name, "run") is True, name


def _reported_task_names(text: str) -> set[str]:
    """Names the diagnostics report itself lists in its warning blocks.

    Matching the report's own ``name`` fields instead of searching the raw
    text keeps the assertion off incidental mentions — an auto-named task
    (``Task-9009``) is not the named task ``Task-9``, but a naive substring
    check cannot tell them apart.
    """
    names: set[str] = set()
    for line in text.splitlines():
        stripped = line.strip()
        for marker in ("Task: ", "STARVATION: ", "DEADLOCK: "):
            if stripped.startswith(marker):
                names.add(stripped[len(marker):].split(" ", 1)[0])
    return names


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

    reported = _reported_task_names(caplog.text)
    assert "lifeos-task-scheduler" not in reported
    assert "Task-9" not in reported
    assert "NO_PROGRESS — " not in caplog.text or "lifeos-task-scheduler" not in reported


def test_reported_task_names_are_exact_names_not_substrings():
    """An auto-named ``Task-9009`` must never count as the named ``Task-9``."""
    text = (
        "TASK_NO_PROGRESS — 1 bounded tasks unchanged since last dump:\n\n"
        "  Task: Task-9009\n  Coroutine: wait\n"
        "TASK_STARVATION — 1 tasks unchanged across 3+ dumps:\n"
        "  STARVATION: Task-9009 unchanged for 3 dumps (0s)\n"
    )
    reported = _reported_task_names(text)
    assert "Task-9009" in reported
    assert "Task-9" not in reported


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


# ── per-TASK identity, not per-NAME ────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_reused_name_is_not_inherited_by_a_new_task(caplog):
    """A finished task's history must not be inherited by the next task's name.

    Production symptom: ``TASK_NO_PROGRESS`` / ``TASK_STARVATION`` for tasks
    that had only just started. The runtime re-creates bounded tasks under a
    stable name (a recovery attempt, a state preload, a panel timer), so keyed
    by NAME the finished task's unchanged-stack counter and first-seen instant
    were inherited by the next task created with that name: it was reported
    after one dump instead of three, with an elapsed time belonging to the
    earlier task.
    """
    with caplog.at_level(logging.WARNING, logger="backend.diagnostics_loop"):
        first = asyncio.create_task(_bounded_slow_op(), name="lifeos-keepalive-recovery")
        try:
            await asyncio.sleep(0)
            await diag._dump_tasks()   # baseline for the name
            await diag._dump_tasks()   # `first` is now unchanged for one dump
        finally:
            first.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first

        caplog.clear()
        second = asyncio.create_task(_bounded_slow_op(), name="lifeos-keepalive-recovery")
        try:
            await asyncio.sleep(0)
            await diag._dump_tasks()   # ONE dump since the NEW task appeared
            after_one_dump = caplog.text
            reported_after_one_dump = _reported_task_names(after_one_dump)

            caplog.clear()
            for _ in range(3):         # three dumps of ITS OWN existence
                await diag._dump_tasks()
            after_four_dumps = caplog.text
            reported_after_four_dumps = _reported_task_names(after_four_dumps)
        finally:
            second.cancel()
            with pytest.raises(asyncio.CancelledError):
                await second

        second_unchanged_dumps = diag._stack_unchanged_count.get(second, 0)
        second_first_seen = diag._task_first_seen.get(second)

    # The production symptom: a brand-new task reported as stalled after a
    # single dump, with the finished task's history behind it.
    assert "lifeos-keepalive-recovery" not in reported_after_one_dump
    assert "TASK_NO_PROGRESS" not in after_one_dump
    assert "TASK_STARVATION" not in after_one_dump
    # The per-task state belongs to the NEW task, and the finished task's state
    # is gone rather than inherited.
    assert first not in diag._task_first_seen
    assert second_first_seen is not None and second_first_seen > time.time() - 60.0
    assert second_unchanged_dumps == 3
    # ...while the SAME task, unchanged for three dumps of its own, still is.
    assert "lifeos-keepalive-recovery" in reported_after_four_dumps
    assert "TASK_NO_PROGRESS" in after_four_dumps
    assert "TASK_STARVATION" in after_four_dumps


@pytest.mark.asyncio
async def test_two_concurrent_tasks_sharing_a_name_are_tracked_per_task():
    """Identity, not the name, owns the per-task stall state."""
    name = "panel:delayed-delete"
    first = asyncio.create_task(_bounded_slow_op(), name=name)
    second = asyncio.create_task(_bounded_slow_op(), name=name)
    try:
        await asyncio.sleep(0)
        await diag._dump_tasks()
        assert {first, second} <= set(diag._stack_unchanged_count)
        assert {first, second} <= set(diag._task_first_seen)
        assert {first, second} <= set(diag._prev_stacks)
    finally:
        for task in (first, second):
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task


@pytest.mark.asyncio
async def test_a_task_blocked_on_a_sync_primitive_is_still_reported(caplog):
    """The deadlock report is per TASK too, and still fires when it should."""
    event = asyncio.Event()
    task = asyncio.create_task(event.wait(), name="blocked-slow-op")
    try:
        await asyncio.sleep(0)
        diag._task_first_seen[task] = time.time() - (_INTERVAL_S * 3)
        with caplog.at_level(logging.WARNING, logger="backend.diagnostics_loop"):
            await diag._dump_tasks()
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert "DEADLOCK_DETECTED" in caplog.text
    assert "DEADLOCK: blocked-slow-op blocked on" in caplog.text


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
