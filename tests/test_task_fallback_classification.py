"""Task fallback classification — a local resource error is NOT an outage.

Production evidence: two Supabase operations (``AI record_tool_call`` and the
``SupabaseTaskRepository`` occurrence read) failed at the same instant with
``[Errno 11] Resource temporarily unavailable`` while a Supabase occurrence
insert on the same client succeeded 0.5s earlier.

EAGAIN is a LOCAL OS resource error. The synchronous Supabase HTTP call runs
through ``asyncio.to_thread``, and ``httpx``/``httpcore`` wrap the raw
``OSError`` from the socket layer in a transport error — so the surfaced text
is the OS string but the failure is the local transport, not a database
failure. It must therefore never be reported as "Supabase unavailable".

These tests pin the classification and the user-facing attribution. They are
in-process only: no live Telegram and no live Supabase.
"""
from __future__ import annotations

import errno
import os
import socket

import httpx
import pytest

from backend.ai.database.task_repository import (
    FALLBACK_REASON_LOCAL_RESOURCE,
    FALLBACK_REASON_UNAVAILABLE,
    InMemoryTaskRepository,
    SupabaseTaskRepository,
    _is_local_resource_failure,
)
from backend.ai.task_management import TaskManagementService
from backend.ai.task_management_interface import (
    FALLBACK_NOTE,
    FALLBACK_RESOURCE_NOTE,
    fallback_note,
    list_text,
)
from tests.test_task_repository import FakeClient, row_task, task_data


def _local_resource_transport_error() -> httpx.ConnectError:
    """The production failure shape: a transport error caused by EAGAIN."""
    cause = OSError(errno.EAGAIN, os.strerror(errno.EAGAIN))
    exc = httpx.ConnectError(str(cause))
    exc.__cause__ = cause
    return exc


def _unreachable_transport_error() -> httpx.ConnectError:
    return httpx.ConnectError("connection refused")


class _FailingClient:
    """Every table access fails, so the repository must degrade."""

    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    def table(self, name):  # noqa: ANN001, ANN201 - fake client
        raise self._exc


# ── Classification ──


def test_local_resource_errno_is_not_evidence_of_a_store_outage():
    assert _is_local_resource_failure(_local_resource_transport_error()) is True
    assert _is_local_resource_failure(
        OSError(errno.EMFILE, os.strerror(errno.EMFILE))
    ) is True
    # A genuine store/transport failure keeps the existing "unavailable" meaning.
    assert _is_local_resource_failure(_unreachable_transport_error()) is False
    assert _is_local_resource_failure(RuntimeError("database unavailable")) is False
    # A DNS failure is a reachability failure, not local resource exhaustion.
    assert _is_local_resource_failure(
        socket.gaierror(-3, "Temporary failure in name resolution")
    ) is False
    assert _is_local_resource_failure(None) is False


def test_the_exact_production_shape_is_a_local_resource_failure():
    """httpcore's READ wrapper around EAGAIN, not only the connect wrapper."""
    cause = OSError(errno.EAGAIN, os.strerror(errno.EAGAIN))
    read_error = httpx.ReadError(str(cause))
    read_error.__cause__ = cause
    assert _is_local_resource_failure(read_error) is True

    write_error = httpx.WriteError(str(cause))
    write_error.__cause__ = cause
    assert _is_local_resource_failure(write_error) is True


def test_a_read_timeout_is_not_local_resource_exhaustion():
    """A read TIMEOUT carries no errno: it must stay ``unavailable``.

    Reproduced against the shipped socket behaviour (an SSLSocket with a
    ``settimeout`` raises ``TimeoutError``, errno None), which httpcore maps to
    ``ReadTimeout`` — NOT to ``ReadError(OSError)``. The observed production
    chain is therefore a socket-layer would-block, not an expired deadline.
    """
    assert _is_local_resource_failure(httpx.ReadTimeout("The read operation timed out")) is False
    assert _is_local_resource_failure(httpx.ConnectTimeout("timed out")) is False


def test_other_socket_errnos_stay_distinguishable():
    """Only the documented local-resource errnos are local-resource failures."""
    reset = OSError(errno.ECONNRESET, os.strerror(errno.ECONNRESET))
    exc = httpx.ReadError(str(reset))
    exc.__cause__ = reset
    assert _is_local_resource_failure(exc) is False

    fds = httpx.ReadError(os.strerror(errno.EMFILE))
    fds.__cause__ = OSError(errno.EMFILE, os.strerror(errno.EMFILE))
    assert _is_local_resource_failure(fds) is True


def test_the_wrapped_cause_chain_is_walked():
    cause = OSError(errno.EAGAIN, os.strerror(errno.EAGAIN))
    outer = RuntimeError("wrapped transport failure")
    outer.__cause__ = cause
    assert _is_local_resource_failure(outer) is True


# ── Repository reason + truthful user-facing notes ──


@pytest.mark.asyncio
async def test_genuine_store_failure_keeps_the_supabase_note():
    repo = SupabaseTaskRepository(
        FakeClient(error=_unreachable_transport_error()), InMemoryTaskRepository()
    )
    assert await repo.list_tasks(10) == []
    assert repo.fallback_active is True
    assert repo.fallback_reason == FALLBACK_REASON_UNAVAILABLE

    service = TaskManagementService(repo, 10)
    snapshot = await service.snapshot()
    text = await list_text(service, snapshot=snapshot)
    assert FALLBACK_NOTE in text
    assert snapshot.fallback_active is True
    assert snapshot.fallback_reason == FALLBACK_REASON_UNAVAILABLE


@pytest.mark.asyncio
async def test_local_resource_failure_never_claims_supabase_is_unavailable():
    repo = SupabaseTaskRepository(
        FakeClient(error=_local_resource_transport_error()), InMemoryTaskRepository()
    )
    assert await repo.list_tasks(10) == []
    # Still degraded (the result is a non-durable memory read): only the
    # attribution changes.
    assert repo.fallback_active is True
    assert repo.fallback_reason == FALLBACK_REASON_LOCAL_RESOURCE
    assert fallback_note(repo.fallback_reason) == FALLBACK_RESOURCE_NOTE

    service = TaskManagementService(repo, 10)
    snapshot = await service.snapshot()
    text = await list_text(service, snapshot=snapshot)
    assert FALLBACK_RESOURCE_NOTE in text
    assert FALLBACK_NOTE not in text
    assert "Supabase unavailable" not in text


@pytest.mark.asyncio
async def test_occurrence_read_classification_matches_the_production_failure():
    """The exact production path: the occurrence read fell back at 14:30:54.986."""
    repo = SupabaseTaskRepository(
        FakeClient(error=_local_resource_transport_error()), InMemoryTaskRepository()
    )
    assert await repo.get_occurrence(10, 2, "k1") is None
    assert repo.fallback_reason == FALLBACK_REASON_LOCAL_RESOURCE

    other = SupabaseTaskRepository(
        FakeClient(error=_unreachable_transport_error()), InMemoryTaskRepository()
    )
    assert await other.get_occurrence(10, 2, "k1") is None
    assert other.fallback_reason == FALLBACK_REASON_UNAVAILABLE


# ── Creation stays honest and non-durable, with truthful attribution ──


@pytest.mark.asyncio
async def test_local_resource_create_is_non_durable_with_truthful_attribution():
    repo = SupabaseTaskRepository(
        _FailingClient(_local_resource_transport_error()), InMemoryTaskRepository()
    )
    task = await repo.create_task(10, task_data())

    assert task.fallback_backend == "InMemoryTaskRepository"
    assert task.fallback_reason == FALLBACK_REASON_LOCAL_RESOURCE
    note = fallback_note(task.fallback_reason)
    assert note == FALLBACK_RESOURCE_NOTE
    assert "Supabase unavailable" not in note


@pytest.mark.asyncio
async def test_genuine_create_failure_keeps_the_non_durable_supabase_note():
    repo = SupabaseTaskRepository(
        _FailingClient(_unreachable_transport_error()), InMemoryTaskRepository()
    )
    task = await repo.create_task(10, task_data())

    assert task.fallback_backend == "InMemoryTaskRepository"
    assert task.fallback_reason == FALLBACK_REASON_UNAVAILABLE
    assert fallback_note(task.fallback_reason) == FALLBACK_NOTE


@pytest.mark.asyncio
async def test_create_message_attributes_a_local_resource_error_truthfully():
    """The live message path: CreateTaskTool -> repository -> rendered note."""
    from tests.test_task_list_consistency import _candidate_json, _create_task_via_tool

    repo = SupabaseTaskRepository(
        _FailingClient(_local_resource_transport_error()), InMemoryTaskRepository()
    )
    result = await _create_task_via_tool(repo, "هر ۵ دقیقه بیو را عوض کن", _candidate_json())

    assert result.success is True
    assert result.data["durable"] is False
    assert result.data["fallback_reason"] == FALLBACK_REASON_LOCAL_RESOURCE
    assert FALLBACK_RESOURCE_NOTE in result.message
    assert "Supabase unavailable" not in result.message


# ── Durable success stays durable; a transient failure never leaks ──


@pytest.mark.asyncio
async def test_a_successful_durable_read_is_durable_and_clears_the_reason():
    client = FakeClient([row_task()])
    repo = SupabaseTaskRepository(client, InMemoryTaskRepository())

    assert len(await repo.list_tasks(10)) == 1
    assert repo.fallback_active is False
    assert repo.fallback_reason == ""

    # A transient local resource failure degrades truthfully...
    client.error = _local_resource_transport_error()
    assert await repo.list_tasks(10) == []
    assert repo.fallback_reason == FALLBACK_REASON_LOCAL_RESOURCE

    # ...and the bounded local-resource cooldown serves the fallback for one
    # short window instead of re-issuing the doomed durable call. This is the
    # change that stops a persistent EAGAIN from hammering Supabase on every
    # incoming Telegram event.
    assert repo._in_local_resource_cooldown() is True
    assert await repo.list_tasks(10) == []

    # Once the bounded window expires, the next healthy durable read is
    # attempted again, succeeds, and clears the degraded state immediately.
    repo._local_resource_until = 0.0
    client.error = None
    assert len(await repo.list_tasks(10)) == 1
    assert repo.fallback_active is False
    assert repo.fallback_reason == ""


# ── The once-per-episode diagnostic names the chain's phase and errno ──


def test_the_classification_diagnostic_records_a_bounded_chain_without_content():
    """The next episode must be decidable from class names + errno alone.

    ``TASK_FALLBACK_CLASSIFIED`` used to carry only the outer type and the
    exception's text, which cannot tell a socket READ (ReadError), a connect
    phase (ConnectError), an fd shortage (a different errno) or a read TIMEOUT
    (ReadTimeout, no errno) apart.
    """
    from backend.ai.database.task_repository import _describe_exception_chain

    # ``OSError(EAGAIN)`` is constructed as the ``BlockingIOError`` subclass —
    # exactly the class name the production chain carries.
    cause = OSError(errno.EAGAIN, os.strerror(errno.EAGAIN))
    assert type(cause) is BlockingIOError
    exc = httpx.ReadError(str(cause))
    exc.__cause__ = cause
    assert _describe_exception_chain(exc) == "ReadError<BlockingIOError(errno=11)"

    # A deep chain stays bounded...
    deep = ValueError("x")
    current = deep
    for index in range(20):
        wrapper = RuntimeError(f"layer {index}")
        wrapper.__cause__ = current
        current = wrapper
    chain = _describe_exception_chain(current)
    assert len(chain.split("<")) == 6
    # ...and only names + errno travel: no message, URL or header text.
    assert "layer" not in chain
    assert ":" not in chain and "/" not in chain


@pytest.mark.asyncio
async def test_the_episode_line_carries_that_chain(caplog):
    import logging

    repo = SupabaseTaskRepository(
        FakeClient(error=_local_resource_transport_error()), InMemoryTaskRepository()
    )

    with caplog.at_level(logging.WARNING, logger="backend.ai.database.task_repository"):
        assert await repo.list_tasks(10) == []

    assert "TASK_FALLBACK_CLASSIFIED" in caplog.text
    assert "chain=ConnectError<BlockingIOError(errno=11)" in caplog.text


# ── Audit persistence stays decoupled from task persistence ──


@pytest.mark.asyncio
async def test_audit_persistence_failure_cannot_mark_task_persistence_unavailable(
    monkeypatch,
):
    """A failed ``ai_tool_history`` write is not a task-store outage.

    ``record_tool_call`` is already fire-and-forget (the ToolExecutor spawns it
    through ``guarded_create_task``), so its failure must leave task
    persistence healthy and durable.
    """
    from backend.ai import persistence

    bad_db = _FailingClient(_local_resource_transport_error())
    monkeypatch.setattr(persistence, "_get_db", lambda: bad_db)

    assert await persistence.record_tool_call(
        10, "session", "task_list", {}, True, "ok", 1.0
    ) is False

    client = FakeClient([row_task()])
    repo = SupabaseTaskRepository(client, InMemoryTaskRepository())
    created = await repo.create_task(10, task_data(label="bio update"))

    assert repo.fallback_active is False
    assert repo.fallback_reason == ""
    assert getattr(created, "fallback_backend", "") == ""
    assert getattr(created, "fallback_reason", "") == ""
