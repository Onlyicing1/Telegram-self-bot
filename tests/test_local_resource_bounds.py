"""Local resource bounds for the synchronous Supabase path.

Production evidence: Supabase task/occurrence reads and the ``ai_tool_history``
audit write failed with ``[Errno 11] Resource temporarily unavailable`` seconds
after a Supabase insert on the same client succeeded. The previous phase proved
the errno is a LOCAL resource condition (``EAGAIN``) and stopped reporting it as
a Supabase outage; this phase bounds the resource that produces it.

Two resource-lifecycle defects made the process's own socket/thread usage
unbounded, and neither is a Supabase problem:

1. The transport deadline was the supabase-py default (120s) while the
   application abandons the operation at 10s. A thread blocked in a synchronous
   HTTP call cannot be cancelled, so every abandoned call pinned a worker thread
   AND a pooled connection twelve times longer than the application was willing
   to wait, and the next call was pushed onto another thread and another socket.
2. Every synchronous Supabase call drew a fresh worker from the event loop's
   shared default executor with no bound of its own, and the audit/usage/message
   persistence paths spawned an unbounded number of such calls.

These tests exercise the real lifecycle logic in-process. No live Telegram and
no live Supabase; the kernel is never actually exhausted here, so the EAGAIN
*shape* is simulated while the dispatch/lifecycle behaviour is the real code.
"""
from __future__ import annotations

import asyncio
import errno
import os
import threading

import pytest


# ── The transport deadline is pinned to the application's own budget ──


def test_transport_deadline_is_strictly_below_the_dispatch_budget():
    from backend import db as db_pkg  # noqa: F401  (import side effects)
    from backend.db import client as dbc

    assert 0 < dbc._DB_HTTP_TIMEOUT < dbc._DB_TIMEOUT

    from backend.ai.database import task_repository
    from backend.ai import persistence

    # Both callers must stay outside the transport deadline, otherwise the
    # transport would still be holding a thread when the watchdog gives up.
    assert task_repository.DB_TIMEOUT > dbc._DB_HTTP_TIMEOUT
    assert persistence._DB_TIMEOUT > dbc._DB_HTTP_TIMEOUT


def test_shared_supabase_client_is_created_with_the_bounded_transport_deadline(
    monkeypatch,
):
    """Regression: the one shared client must not keep the 120s library default."""
    from backend.db import client as dbc

    captured: dict = {}

    def _fake_create_client(url, key, options=None):
        captured["url"] = url
        captured["options"] = options
        return object()

    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "service-role")
    monkeypatch.setattr(dbc, "_initialised", False)
    monkeypatch.setattr(dbc, "_client", None)
    monkeypatch.setattr(dbc, "_available", False)
    monkeypatch.setattr("supabase.create_client", _fake_create_client)

    assert dbc.get_db() is not None
    options = captured["options"]
    assert options is not None, "the client must be built with explicit options"
    assert options.postgrest_client_timeout == dbc._DB_HTTP_TIMEOUT
    # Under the old implementation this was the supabase-py default of 120s.
    assert options.postgrest_client_timeout < 120


# ── One bounded, reused pool for synchronous Supabase work ──


@pytest.mark.asyncio
async def test_sync_db_dispatch_reuses_a_bounded_number_of_threads():
    from backend.db import client as dbc

    threads: list[int] = []

    def _work():
        threads.append(threading.get_ident())
        return "ok"

    for _ in range(24):
        assert await dbc.run_sync_db(_work) == "ok"

    assert len(threads) == 24
    # Threads are reused: many calls, at most the configured pool size.
    assert len(set(threads)) <= dbc._DB_MAX_WORKERS


@pytest.mark.asyncio
async def test_concurrent_sync_db_dispatch_never_exceeds_the_bound():
    """Regression: the old path used the loop's default executor with no bound."""
    from backend.db import client as dbc

    lock = threading.Lock()
    running = 0
    peak = 0
    release = threading.Event()

    def _work():
        nonlocal running, peak
        with lock:
            running += 1
            peak = max(peak, running)
        release.wait(5)
        with lock:
            running -= 1

    tasks = [
        asyncio.create_task(dbc.run_sync_db(_work))
        for _ in range(dbc._DB_MAX_WORKERS * 3)
    ]
    # Let the pool saturate before releasing the blocked workers.
    for _ in range(50):
        await asyncio.sleep(0.01)
        if peak >= dbc._DB_MAX_WORKERS:
            break
    release.set()
    await asyncio.gather(*tasks)

    assert peak == dbc._DB_MAX_WORKERS
    assert peak <= dbc._DB_MAX_WORKERS


@pytest.mark.asyncio
async def test_the_pool_is_reachable_again_after_deterministic_shutdown():
    from backend.db import client as dbc

    assert await dbc.run_sync_db(lambda: 1) == 1
    dbc.shutdown_db_executor()
    try:
        # A shutdown pool is transparently replaced, so no in-flight persistence
        # path can observe "cannot schedule new futures after shutdown".
        assert await dbc.run_sync_db(lambda: 2) == 2
    finally:
        dbc.shutdown_db_executor()


# ── Task persistence and AI persistence use the same bounded dispatch ──


@pytest.mark.asyncio
async def test_task_repository_dispatches_through_the_bounded_pool(monkeypatch):
    from backend.ai.database import task_repository
    from backend.db import client as dbc
    from tests.test_task_repository import FakeClient, row_task

    seen: list = []

    async def _fake_run_sync(fn, *args, timeout=None, **kwargs):
        seen.append(timeout)
        return fn()

    monkeypatch.setattr(dbc, "run_sync_db", _fake_run_sync)

    repo = task_repository.SupabaseTaskRepository(
        FakeClient([row_task()]), task_repository.InMemoryTaskRepository()
    )
    assert len(await repo.list_tasks(10)) == 1
    assert seen == [task_repository.DB_TIMEOUT]


@pytest.mark.asyncio
async def test_ai_persistence_dispatches_through_the_bounded_pool(monkeypatch):
    from backend.ai import persistence
    from backend.db import client as dbc

    seen: list = []

    async def _fake_run_sync(fn, *args, timeout=None, **kwargs):
        seen.append(timeout)
        return fn(*args, **kwargs)

    monkeypatch.setattr(dbc, "run_sync_db", _fake_run_sync)
    monkeypatch.setattr(persistence, "_get_db", lambda: _RecordingDb())
    monkeypatch.setattr(persistence, "_audit_inflight", 0)
    monkeypatch.setattr(persistence, "_audit_dropped", 0)

    assert await persistence.record_tool_call(
        10, "session", "task_list", {}, True, "ok", 1.0
    ) is True
    assert seen == [persistence._DB_TIMEOUT]


class _RecordingDb:
    """Minimal stand-in for the shared Supabase client (table().insert())."""

    def table(self, _name):
        return self

    def insert(self, _payload):
        return self

    def execute(self):
        return type("R", (), {"data": []})()


# ── Best-effort audit persistence is bounded, counted and never fatal ──


@pytest.mark.asyncio
async def test_audit_scheduling_is_bounded_and_counted(monkeypatch):
    from backend.ai import persistence

    monkeypatch.setattr(persistence, "_audit_inflight", 0)
    monkeypatch.setattr(persistence, "_audit_dropped", 0)

    release = asyncio.Event()
    started = asyncio.Event()

    async def _slow():
        started.set()
        await release.wait()

    for _ in range(persistence._AUDIT_MAX_INFLIGHT):
        assert persistence.schedule_audit(lambda: _slow(), name="ai:audit") is True
    await started.wait()
    assert persistence.audit_inflight() == persistence._AUDIT_MAX_INFLIGHT

    dropped_factory_calls = []

    def _dropped_factory():
        dropped_factory_calls.append(True)
        return _slow()

    # Saturation drops the record instead of queueing an unbounded backlog...
    assert persistence.schedule_audit(_dropped_factory, name="ai:audit") is False
    # ...and the factory is never called, so no unawaited coroutine is left.
    assert dropped_factory_calls == []
    assert persistence.audit_dropped() == 1
    assert persistence.audit_inflight() == persistence._AUDIT_MAX_INFLIGHT

    release.set()
    for _ in range(20):
        await asyncio.sleep(0)
        if persistence.audit_inflight() == 0:
            break
    assert persistence.audit_inflight() == 0


@pytest.mark.asyncio
async def test_audit_failure_never_reaches_the_caller(monkeypatch):
    from backend.ai import persistence

    monkeypatch.setattr(persistence, "_audit_inflight", 0)
    monkeypatch.setattr(persistence, "_audit_dropped", 0)

    async def _boom():
        raise OSError(errno.EAGAIN, os.strerror(errno.EAGAIN))

    assert persistence.schedule_audit(lambda: _boom(), name="ai:audit") is True
    for _ in range(20):
        await asyncio.sleep(0)
        if persistence.audit_inflight() == 0:
            break
    assert persistence.audit_inflight() == 0


@pytest.mark.asyncio
async def test_tool_execution_survives_a_saturated_audit_path(monkeypatch):
    """A saturated audit path neither blocks nor fails the primary execution."""
    from backend.ai import persistence
    from backend.ai.tools.base import PermissionLevel, ToolResult
    from backend.ai.tools.context import ToolContext
    from backend.ai.tools.executor import ToolExecutor
    from backend.ai.tools.registry import ToolRegistry

    monkeypatch.setattr(persistence, "_audit_inflight", persistence._AUDIT_MAX_INFLIGHT)
    monkeypatch.setattr(persistence, "_audit_dropped", 0)

    class _StubTool:
        name = "stub_read"
        description = "reads nothing"
        parameters = {"type": "object", "properties": {}}
        permission_level = PermissionLevel.READ_ONLY
        safe = True
        return_type = "none"
        long_running = False

        async def execute(self, _context, _arguments):
            return ToolResult(success=True, message="stub ok")

    ctx = ToolContext(telegram=None, owner_id=1, tz_str="UTC")
    registry = ToolRegistry()
    registry.register(_StubTool())
    executor = ToolExecutor(registry, ctx)

    results = await executor.execute_calls(
        [{"name": "stub_read", "arguments": {}}], owner_id=1
    )

    # The drop is counted and the tool result is untouched: audit persistence
    # is observability data, never a precondition of execution.
    assert results[0].success is True
    assert results[0].message == "stub ok"
    assert persistence.audit_dropped() >= 1
    assert persistence.audit_inflight() == persistence._AUDIT_MAX_INFLIGHT


@pytest.mark.asyncio
async def test_record_tool_call_failure_leaves_task_persistence_durable(monkeypatch):
    """A failed audit write is not a task-store outage (honesty preserved)."""
    from backend.ai import persistence
    from backend.ai.database import task_repository
    from tests.test_task_repository import FakeClient, row_task, task_data

    class _FailingTable:
        def table(self, _name):
            raise OSError(errno.EAGAIN, os.strerror(errno.EAGAIN))

    monkeypatch.setattr(persistence, "_get_db", lambda: _FailingTable())
    assert await persistence.record_tool_call(
        10, "s", "t", {}, True, "ok", 1.0
    ) is False

    repo = task_repository.SupabaseTaskRepository(
        FakeClient([row_task()]), task_repository.InMemoryTaskRepository()
    )
    created = await repo.create_task(10, task_data(label="bio update"))
    assert repo.fallback_active is False
    assert repo.fallback_reason == ""
    assert getattr(created, "fallback_backend", "") == ""
    assert getattr(created, "fallback_reason", "") == ""


@pytest.mark.asyncio
async def test_local_resource_error_still_classifies_as_local_resource():
    """The previous phase's truthful classification must be preserved."""
    from backend.ai.database.task_repository import (
        FALLBACK_REASON_LOCAL_RESOURCE,
        InMemoryTaskRepository,
        SupabaseTaskRepository,
    )
    from tests.test_task_repository import FakeClient

    class _FailingClient:
        def table(self, _name):
            raise OSError(errno.EAGAIN, os.strerror(errno.EAGAIN))

    repo = SupabaseTaskRepository(_FailingClient(), InMemoryTaskRepository())
    assert await repo.list_tasks(10) == []
    assert repo.fallback_active is True
    assert repo.fallback_reason == FALLBACK_REASON_LOCAL_RESOURCE
