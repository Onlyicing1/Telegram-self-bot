"""Transport evidence for the recurring Supabase task-repository failures.

Production (after `aee3e16`) still reports, once per episode:

    TASK_FALLBACK_CLASSIFIED reason=local_resource exception=ReadError
    chain=ReadError<ReadError<BlockingIOError(errno=11)

The chain proves the failure is a LOCAL socket-layer would-block, not a store
outage, not an expired read deadline in the library's own mapping and not
executor saturation. It does NOT prove *why* the socket returned ``EAGAIN``, and
two remaining causes produce the identical chain:

* the read transport deadline (8s) expired and the platform surfaced it as a
  syscall would-block — the call was in flight for ~8s;
* the pooled HTTPS connection was unusable and the socket refused the read
  immediately — the call failed in milliseconds.

These tests pin the evidence that separates them (`elapsed`, measured inside the
shared worker so executor queueing is excluded), the transport `phase`, and the
gap since the last durable success. They also pin that nothing became retried:
an ambiguous write is still issued exactly once, a cooldown-skipped call adds no
second line, and a remote HTTP/2 termination is still `unavailable`.

In-process only: no live Telegram, no live Supabase, no Render access.
"""
from __future__ import annotations

import errno
import logging
import os
import re
import threading
import time

import httpx
import pytest

from backend.ai.database.task_repository import (
    FALLBACK_REASON_LOCAL_RESOURCE,
    FALLBACK_REASON_UNAVAILABLE,
    InMemoryTaskRepository,
    SupabaseTaskRepository,
    _format_evidence_seconds,
    _transport_phase,
)
from tests.test_task_repository import FakeClient, row_task, task_data

_EVIDENCE = re.compile(r"phase=(\S+) .*elapsed=(\S+) since_ok=(\S+) ")


def _eagain_read_error() -> httpx.ReadError:
    """The exact production chain: httpx.ReadError <- httpcore.ReadError <- EAGAIN."""
    cause = OSError(errno.EAGAIN, os.strerror(errno.EAGAIN))
    inner = httpx.ReadError(str(cause))
    inner.__cause__ = cause
    outer = httpx.ReadError(str(cause))
    outer.__cause__ = inner
    return outer


def _remote_h2_termination() -> httpx.RemoteProtocolError:
    """The other production line: an h2 ConnectionTerminated over the same transport."""
    exc = httpx.RemoteProtocolError(
        "ConnectionTerminated error_code:1, last_stream_id:0, additional_data:None"
    )
    exc.__cause__ = RuntimeError("h2 connection terminated")
    return exc


def _evidence_lines(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if "TASK_FALLBACK_CLASSIFIED" in r.getMessage()]


def _evidence_fields(line: str) -> tuple[str, str, str]:
    match = _EVIDENCE.search(line)
    assert match is not None, line
    return match.group(1), match.group(2), match.group(3)


class _SlowFailingClient(FakeClient):
    """Fails after spending real time inside the shared worker."""

    def __init__(self, error, delay: float) -> None:
        super().__init__(error=error)
        self._delay = delay

    def table(self, name):  # noqa: ANN001, ANN201 - fake client
        self._table_name = name
        return _SlowQuery(self, name, self._delay)


class _SlowQuery:
    def __init__(self, client, name, delay) -> None:  # noqa: ANN001 - fake client
        self._client = client
        self._name = name
        self._delay = delay

    def select(self, *_args, **_kwargs):
        return self

    def eq(self, *_args, **_kwargs):
        return self

    def in_(self, *_args, **_kwargs):
        return self

    def order(self, *_args, **_kwargs):
        return self

    def limit(self, *_args, **_kwargs):
        return self

    def maybe_single(self):
        return self

    def execute(self):
        self._client.queries.append(self)
        time.sleep(self._delay)
        raise self._client.error


# ── The measurement is taken inside the worker, and nothing is retried ──


@pytest.mark.asyncio
async def test_inflight_duration_is_measured_in_the_worker_and_the_call_is_issued_once():
    from backend.db import client as dbc

    calls: list[int] = []
    failure = _eagain_read_error()

    def failing_call():
        calls.append(1)
        time.sleep(0.05)
        raise failure

    with pytest.raises(httpx.ReadError) as info:
        await dbc.run_sync_db(failing_call)

    # The SAME exception object travels out of the worker: the duration is a
    # diagnostic attribute, never a wrapped/re-raised replacement.
    assert info.value is failure
    elapsed = dbc.db_inflight_seconds(failure)
    assert elapsed is not None
    assert 0.05 <= elapsed < 2.0
    assert calls == [1]


@pytest.mark.asyncio
async def test_a_successful_dispatch_carries_no_failure_evidence():
    from backend.db import client as dbc

    assert await dbc.run_sync_db(lambda: "row") == "row"


@pytest.mark.asyncio
async def test_the_dispatch_deadline_reports_no_transport_duration():
    """A watchdog abandonment is not evidence about the socket.

    The thread is still blocked inside the transport, so no in-flight duration
    exists; printing one (or treating it as a socket failure) would fabricate
    evidence. The blocked thread is released at the end of the test.
    """
    from backend.db import client as dbc
    from backend.runtime.operation_watchdog import OperationTimeoutError

    release = threading.Event()
    calls: list[int] = []

    def stuck_call():
        calls.append(1)
        release.wait(5.0)

    try:
        with pytest.raises(OperationTimeoutError) as info:
            await dbc.run_sync_db(stuck_call, timeout=0.05)
        assert dbc.db_inflight_seconds(info.value) is None
        assert calls == [1]
    finally:
        release.set()


# ── Phase classification (names only) ──


def test_phase_matrix_covers_every_pinned_transport_shape():
    assert _transport_phase(_eagain_read_error()) == "read"
    assert _transport_phase(httpx.ReadTimeout("timed out")) == "read"
    assert _transport_phase(httpx.ConnectTimeout("timed out")) == "connect"
    assert _transport_phase(httpx.ConnectError("refused")) == "connect"
    assert _transport_phase(httpx.WriteError("write failed")) == "write"
    assert _transport_phase(httpx.WriteTimeout("timed out")) == "write"
    assert _transport_phase(httpx.PoolTimeout("no connection")) == "pool"
    assert _transport_phase(_remote_h2_termination()) == "protocol"

    class ConnectionTerminated(Exception):
        pass

    wrapped = RuntimeError("transport")
    wrapped.__cause__ = ConnectionTerminated("error_code:1")
    assert _transport_phase(wrapped) == "protocol"

    from backend.runtime.operation_watchdog import OperationTimeoutError

    assert _transport_phase(OperationTimeoutError("db:list", 10.0, 10.0)) == "deadline"
    assert _transport_phase(RuntimeError("no transport at all")) == "unknown"
    assert _transport_phase(None) == "unknown"


def test_seconds_formatting_is_bounded_and_honest():
    assert _format_evidence_seconds(0.0) == "0.0s"
    assert _format_evidence_seconds(7.94) == "7.9s"
    assert _format_evidence_seconds(10 ** 9) == "86400.0s"
    assert _format_evidence_seconds(None) == "n/a"
    assert _format_evidence_seconds(-1.0) == "n/a"
    assert _format_evidence_seconds("8") == "n/a"


# ── The once-per-episode line separates an expiry from an instant refusal ──


@pytest.mark.asyncio
async def test_an_immediate_would_block_and_a_deadline_are_distinguishable(caplog):
    """Same errno chain, different in-flight duration — the decisive evidence."""
    instant = SupabaseTaskRepository(
        _SlowFailingClient(_eagain_read_error(), 0.0), InMemoryTaskRepository()
    )
    waiting = SupabaseTaskRepository(
        _SlowFailingClient(_eagain_read_error(), 0.25), InMemoryTaskRepository()
    )

    with caplog.at_level(logging.WARNING, logger="backend.ai.database.task_repository"):
        assert await instant.list_tasks(10) == []
        immediate_line = _evidence_lines(caplog)[-1]
        assert await waiting.list_tasks(10) == []
        waiting_line = _evidence_lines(caplog)[-1]

    instant_phase, instant_elapsed, _ = _evidence_fields(immediate_line)
    waiting_phase, waiting_elapsed, _ = _evidence_fields(waiting_line)

    assert instant_phase == waiting_phase == "read"
    assert "chain=ReadError<ReadError<BlockingIOError(errno=11)" in immediate_line
    assert "chain=ReadError<ReadError<BlockingIOError(errno=11)" in waiting_line
    assert instant_elapsed.endswith("s") and waiting_elapsed.endswith("s")
    assert float(instant_elapsed[:-1]) < 0.1
    assert float(waiting_elapsed[:-1]) >= 0.25


@pytest.mark.asyncio
async def test_the_line_reports_the_gap_since_the_last_durable_success(caplog):
    repo = SupabaseTaskRepository(FakeClient([row_task()]), InMemoryTaskRepository())

    with caplog.at_level(logging.WARNING, logger="backend.ai.database.task_repository"):
        assert len(await repo.list_tasks(10)) == 1
        repo._client.error = _eagain_read_error()
        assert await repo.list_tasks(10) == []

    _, _, since_ok = _evidence_fields(_evidence_lines(caplog)[-1])
    assert since_ok.endswith("s")
    assert float(since_ok[:-1]) < 2.0


@pytest.mark.asyncio
async def test_a_repository_that_never_succeeded_says_so(caplog):
    repo = SupabaseTaskRepository(
        FakeClient(error=_eagain_read_error()), InMemoryTaskRepository()
    )

    with caplog.at_level(logging.WARNING, logger="backend.ai.database.task_repository"):
        assert await repo.list_tasks(10) == []

    _, _, since_ok = _evidence_fields(_evidence_lines(caplog)[-1])
    assert since_ok == "never"


# ── A remote HTTP/2 termination is still an outage, never a local resource ──


@pytest.mark.asyncio
async def test_remote_h2_termination_stays_unavailable_and_skips_the_local_line(caplog):
    repo = SupabaseTaskRepository(
        FakeClient(error=_remote_h2_termination()), InMemoryTaskRepository()
    )

    with caplog.at_level(logging.WARNING, logger="backend.ai.database.task_repository"):
        assert await repo.list_tasks(10) == []

    assert repo.fallback_active is True
    assert repo.fallback_reason == FALLBACK_REASON_UNAVAILABLE
    # No local-resource episode: the bounded cooldown is never armed...
    assert repo._in_local_resource_cooldown() is False
    # ...and the once-per-episode LOCAL line is not used for a store failure.
    assert _evidence_lines(caplog) == []
    # The failure itself is still reported, truthfully, per operation.
    assert "using fallback" in caplog.text


# ── Nothing became retried: an ambiguous write is issued exactly once ──


@pytest.mark.asyncio
async def test_an_ambiguous_failed_insert_is_issued_exactly_once(caplog):
    client = FakeClient(error=_eagain_read_error())

    with caplog.at_level(logging.WARNING, logger="backend.ai.database.task_repository"):
        repo = SupabaseTaskRepository(client, InMemoryTaskRepository())
        task = await repo.create_task(10, task_data())

    inserts = [q for q in client.queries if getattr(q, "operation", None) == "insert"]
    assert len(inserts) == 1
    assert task.fallback_backend == "InMemoryTaskRepository"
    assert task.fallback_reason == FALLBACK_REASON_LOCAL_RESOURCE
    assert len(_evidence_lines(caplog)) == 1


@pytest.mark.asyncio
async def test_a_cooldown_skipped_call_adds_no_second_evidence_line(caplog):
    repo = SupabaseTaskRepository(
        FakeClient(error=_eagain_read_error()), InMemoryTaskRepository()
    )

    with caplog.at_level(logging.WARNING, logger="backend.ai.database.task_repository"):
        assert await repo.list_tasks(10) == []
        assert await repo.list_tasks(10) == []
        assert await repo.list_tasks(10) == []

    assert len(_evidence_lines(caplog)) == 1
    assert repo.fallback_reason == FALLBACK_REASON_LOCAL_RESOURCE
    # The cooldown is armed once and never extended by a skipped call.
    assert repo._in_local_resource_cooldown() is True


# ── The evidence fields carry names and numbers only ──


@pytest.mark.asyncio
async def test_evidence_fields_are_bounded_and_secret_free(caplog):
    """A chain may wrap anything; the evidence fields must stay inert."""
    secret = "https://project.supabase.co/rest/v1/ai_tasks?apikey=super-secret-token"
    cause = OSError(errno.EAGAIN, os.strerror(errno.EAGAIN))
    outer = httpx.ReadError("transport failure")
    outer.__cause__ = cause
    outer.__dict__["request_url"] = secret

    repo = SupabaseTaskRepository(
        FakeClient(error=outer), InMemoryTaskRepository()
    )
    with caplog.at_level(logging.WARNING, logger="backend.ai.database.task_repository"):
        assert await repo.list_tasks(10) == []

    line = _evidence_lines(caplog)[-1]
    phase, elapsed, since_ok = _evidence_fields(line)
    assert re.fullmatch(r"[a-z]+", phase)
    assert re.fullmatch(r"\d+\.\ds|n/a", elapsed)
    assert re.fullmatch(r"\d+\.\ds|never", since_ok)
    assert "chain=ReadError<BlockingIOError(errno=11)" in line
    assert secret not in line
    assert "apikey" not in line
    assert "super-secret-token" not in line
    assert len(line) < 400
