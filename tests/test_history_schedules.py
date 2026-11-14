"""Phase 3B — durable History schedules and oldest-first retrieval.

The repository tests run against an explicitly injected in-memory fake and a
fake Supabase table shaped like the durable client; no live Supabase is
contacted. Production History state is durable-only: an unavailable store must
fail closed and never be replayed against process memory. The retrieval tests
use a Telethon-shaped fake supporting ``reverse=True``. Nothing here claims live
Telegram behaviour.
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import pytest

from backend.ai.context.provenance import AI_PROVENANCE_MARKER
from backend.ai.database.task_repository import (
    MAX_HISTORY_SUMMARY_BYTES,
    HistoryStorageUnavailable,
    InMemoryTaskRepository,
    SupabaseTaskRepository,
)
from backend.services import history_ai_service
from backend.services.history_service import (
    HistoryMessage,
    fetch_oldest_history,
)
from backend.telegram_api.api import TelegramAPI

OWNER = 7770001
OTHER = 7770002
PEER = -100555000
NOW = datetime(2026, 11, 13, 12, 0, tzinfo=timezone.utc)


def _task_data(label: str = "History") -> dict[str, Any]:
    return {
        "label": label,
        "schedule_type": "daily",
        "schedule": {"hour": 8, "minute": 30},
        "timezone": "UTC",
        "actions": [{"name": "list_saves", "arguments": {}}],
        "notification_destination": {"chat_id": OWNER},
    }


async def _task(repo: InMemoryTaskRepository, owner: int = OWNER, label: str = "History"):
    return await repo.create_task(owner, _task_data(label))


# ── durable state: creation, ownership, uniqueness ──────────────────────────


@pytest.mark.asyncio
async def test_create_get_by_task_and_by_peer_are_owner_scoped():
    repo = InMemoryTaskRepository()
    task = await _task(repo)
    created = await repo.create_history_schedule(
        OWNER, task.id, PEER, baseline_cursor=10,
    )
    assert created.baseline_cursor == created.scanned_cursor == created.summarized_cursor == 10
    assert created.status == "active" and created.version == 1 and created.revision == 0

    assert (await repo.get_history_schedule(OWNER, task.id)).id == created.id
    assert (await repo.get_history_schedule_by_peer(OWNER, PEER)).id == created.id
    assert await repo.get_history_schedule(OTHER, task.id) is None
    assert await repo.get_history_schedule_by_peer(OTHER, PEER) is None


@pytest.mark.asyncio
async def test_second_schedule_for_same_owner_and_peer_is_refused():
    repo = InMemoryTaskRepository()
    first_task = await _task(repo, label="First")
    second_task = await _task(repo, label="Second")
    assert await repo.create_history_schedule(OWNER, first_task.id, PEER) is not None
    assert await repo.create_history_schedule(OWNER, second_task.id, PEER) is None
    # Another owner is independent, with a task of their own.
    other_task = await _task(repo, owner=OTHER, label="Other owner")
    assert await repo.create_history_schedule(OTHER, other_task.id, PEER) is not None


@pytest.mark.asyncio
async def test_paused_and_terminal_states_retain_ownership():
    repo = InMemoryTaskRepository()
    task = await _task(repo)
    state = await repo.create_history_schedule(OWNER, task.id, PEER)
    other_task = await _task(repo, label="Replacement")
    for status in ("paused", "completed", "failed", "expired"):
        state = await repo.commit_history_success(
            OWNER,
            PEER,
            expected_version=state.version,
            summary=f"summary-{status}",
            scanned_cursor=state.scanned_cursor,
            summarized_cursor=state.summarized_cursor,
            status=status,
        )
        assert state is not None and state.status == status
        assert await repo.create_history_schedule(OWNER, other_task.id, PEER) is None


@pytest.mark.asyncio
async def test_legacy_deleted_state_is_repaired_not_a_permanent_block():
    repo = InMemoryTaskRepository()
    first_task = await _task(repo, label="Legacy")
    state = await repo.create_history_schedule(OWNER, first_task.id, PEER, baseline_cursor=21)
    # A pre-existing legacy row, not a normal write target.
    repo._history_schedules[(OWNER, PEER)].status = "deleted"
    repo._history_schedules[(OWNER, PEER)].summary = "old"
    pristine = repo._history_schedules[(OWNER, PEER)]

    replacement_task = await _task(repo, label="Replacement")
    repaired = await repo.create_history_schedule(OWNER, replacement_task.id, PEER, baseline_cursor=50)
    assert repaired is not None
    assert repaired.id == state.id, "repair reuses the one owner+peer row"
    assert repaired.task_id == replacement_task.id and repaired.status == "active"
    assert repaired.baseline_cursor == repaired.scanned_cursor == repaired.summarized_cursor == 50
    assert repaired.summary == "" and repaired.last_error == ""
    assert repaired.version == state.version + 1 and repaired.revision == state.revision + 1
    assert pristine is not None


@pytest.mark.asyncio
async def test_empty_summary_cannot_replace_a_previous_summary():
    repo = InMemoryTaskRepository()
    task = await _task(repo)
    state = await repo.create_history_schedule(OWNER, task.id, PEER)
    durable = await repo.commit_history_success(
        OWNER, PEER, expected_version=state.version,
        summary="durable summary", scanned_cursor=10, summarized_cursor=10,
    )
    with pytest.raises(ValueError, match="empty summary"):
        await repo.commit_history_success(
            OWNER, PEER, expected_version=durable.version,
            summary="", scanned_cursor=20, summarized_cursor=10,
        )
    current = await repo.get_history_schedule_by_peer(OWNER, PEER)
    assert current.summary == "durable summary"
    assert current.scanned_cursor == current.summarized_cursor == 10
    assert current.version == durable.version


@pytest.mark.asyncio
async def test_task_deletion_cascades_the_schedule_in_the_fallback():
    repo = InMemoryTaskRepository()
    task = await _task(repo)
    state = await repo.create_history_schedule(OWNER, task.id, PEER)
    result = await repo.delete_task(OWNER, task.id, task.version)
    assert result.deleted is True
    assert await repo.get_history_schedule_by_peer(OWNER, PEER) is None
    assert await repo.get_history_schedule(OWNER, task.id) is None
    assert state.id not in {r.id for r in repo._history_schedules.values()}


@pytest.mark.asyncio
async def test_create_requires_an_owned_task():
    repo = InMemoryTaskRepository()
    task = await _task(repo)
    with pytest.raises(ValueError):
        await repo.create_history_schedule(OWNER, 404, PEER)
    with pytest.raises(ValueError):
        await repo.create_history_schedule(OTHER, task.id, PEER)
    with pytest.raises(ValueError):
        await repo.create_history_schedule(OWNER, task.id, 0)


# ── atomic commit / failure / reset invariants ──────────────────────────────


@pytest.mark.asyncio
async def test_commit_is_one_cas_write_and_stale_version_is_rejected():
    repo = InMemoryTaskRepository()
    task = await _task(repo)
    state = await repo.create_history_schedule(OWNER, task.id, PEER)
    committed = await repo.commit_history_success(
        OWNER,
        PEER,
        expected_version=state.version,
        summary="first summary",
        scanned_cursor=30,
        summarized_cursor=30,
    )
    assert committed is not None
    assert committed.summary == "first summary"
    assert committed.scanned_cursor == committed.summarized_cursor == 30
    assert committed.version == 2 and committed.revision == 1
    assert committed.last_committed_at is not None

    stale = await repo.commit_history_success(
        OWNER,
        PEER,
        expected_version=1,
        summary="stale overwrite",
        scanned_cursor=99,
        summarized_cursor=99,
    )
    assert stale is None
    current = await repo.get_history_schedule_by_peer(OWNER, PEER)
    assert current.summary == "first summary" and current.scanned_cursor == 30
    assert current.version == 2


@pytest.mark.asyncio
async def test_failure_never_replaces_summary_or_advances_cursors():
    repo = InMemoryTaskRepository()
    task = await _task(repo)
    state = await repo.create_history_schedule(OWNER, task.id, PEER)
    state = await repo.commit_history_success(
        OWNER, PEER, expected_version=state.version,
        summary="durable summary", scanned_cursor=40, summarized_cursor=35,
    )
    failed = await repo.record_history_failure(
        OWNER, PEER, expected_version=state.version,
        error="provider timeout", status="failed",
    )
    assert failed is not None and failed.status == "failed"
    assert failed.summary == "durable summary"
    assert failed.scanned_cursor == 40 and failed.summarized_cursor == 35
    assert failed.revision == state.revision, "a failure is not a content revision"
    assert failed.version == state.version + 1
    assert failed.last_error == "provider timeout" and failed.last_error_at is not None

    # The failed attempt owns the next CAS token; the old version is stale.
    assert await repo.commit_history_success(
        OWNER, PEER, expected_version=state.version,
        summary="stale", scanned_cursor=41, summarized_cursor=41,
    ) is None
    current = await repo.get_history_schedule_by_peer(OWNER, PEER)
    assert current.summary == "durable summary" and current.scanned_cursor == 40


@pytest.mark.asyncio
async def test_summary_size_is_a_hard_utf8_bound_and_never_truncated():
    repo = InMemoryTaskRepository()
    task = await _task(repo)
    state = await repo.create_history_schedule(OWNER, task.id, PEER)
    at_limit = "x" * MAX_HISTORY_SUMMARY_BYTES
    committed = await repo.commit_history_success(
        OWNER, PEER, expected_version=state.version,
        summary=at_limit, scanned_cursor=10, summarized_cursor=10,
    )
    assert committed.summary == at_limit

    with pytest.raises(ValueError):
        await repo.commit_history_success(
            OWNER, PEER, expected_version=committed.version,
            summary=at_limit + "x", scanned_cursor=11, summarized_cursor=11,
        )
    current = await repo.get_history_schedule_by_peer(OWNER, PEER)
    assert current.summary == at_limit and current.scanned_cursor == 10

    # Multi-byte text is bounded by UTF-8 bytes, not characters.
    with pytest.raises(ValueError):
        await repo.commit_history_success(
            OWNER, PEER, expected_version=current.version,
            summary="é" * (MAX_HISTORY_SUMMARY_BYTES // 2 + 1),
            scanned_cursor=11, summarized_cursor=11,
        )


@pytest.mark.asyncio
async def test_safe_skipped_only_batch_can_advance_scanned_not_summarized():
    repo = InMemoryTaskRepository()
    task = await _task(repo)
    state = await repo.create_history_schedule(OWNER, task.id, PEER)
    committed = await repo.commit_history_success(
        OWNER, PEER, expected_version=state.version,
        summary="", scanned_cursor=60, summarized_cursor=0,
    )
    assert committed.scanned_cursor == 60
    assert committed.summarized_cursor == 0
    assert committed.summary == ""
    assert committed.revision == 1


@pytest.mark.asyncio
async def test_cursors_must_be_monotonic_and_ordered():
    repo = InMemoryTaskRepository()
    task = await _task(repo)
    state = await repo.create_history_schedule(OWNER, task.id, PEER, baseline_cursor=10)
    with pytest.raises(ValueError):
        await repo.commit_history_success(
            OWNER, PEER, expected_version=state.version,
            summary="bad", scanned_cursor=5, summarized_cursor=5,
        )
    state = await repo.commit_history_success(
        OWNER, PEER, expected_version=state.version,
        summary="ok", scanned_cursor=20, summarized_cursor=15,
    )
    with pytest.raises(ValueError):
        await repo.commit_history_success(
            OWNER, PEER, expected_version=state.version,
            summary="rewind", scanned_cursor=19, summarized_cursor=15,
        )
    with pytest.raises(ValueError):
        await repo.commit_history_success(
            OWNER, PEER, expected_version=state.version,
            summary="ahead", scanned_cursor=20, summarized_cursor=21,
        )


@pytest.mark.asyncio
async def test_reset_is_explicit_and_resets_coverage_not_the_scan_cursor():
    repo = InMemoryTaskRepository()
    task = await _task(repo)
    state = await repo.create_history_schedule(OWNER, task.id, PEER)
    state = await repo.commit_history_success(
        OWNER, PEER, expected_version=state.version,
        summary="old summary", scanned_cursor=50, summarized_cursor=45,
    )
    reset = await repo.reset_history_summary(OWNER, PEER, expected_version=state.version)
    assert reset.summary == "" and reset.last_error == ""
    assert reset.scanned_cursor == 50 and reset.summarized_cursor == 50
    assert reset.version == state.version + 1 and reset.revision == state.revision + 1
    assert await repo.reset_history_summary(OWNER, PEER, expected_version=state.version) is None


@pytest.mark.asyncio
async def test_writes_are_owner_scoped_and_failure_metadata_is_bounded():
    repo = InMemoryTaskRepository()
    task = await _task(repo)
    state = await repo.create_history_schedule(OWNER, task.id, PEER)
    assert await repo.commit_history_success(
        OTHER, PEER, expected_version=state.version,
        summary="wrong owner", scanned_cursor=1, summarized_cursor=1,
    ) is None
    assert await repo.record_history_failure(
        OTHER, PEER, expected_version=state.version, error="wrong owner",
    ) is None
    with pytest.raises(ValueError):
        await repo.record_history_failure(
            OWNER, PEER, expected_version=state.version, error="",
        )
    with pytest.raises(ValueError):
        await repo.record_history_failure(
            OWNER, PEER, expected_version=state.version, error="x" * 2049,
        )
    assert (await repo.get_history_schedule_by_peer(OWNER, PEER)).last_error == ""


# ── durable CAS shape (fake Supabase table, never live) ─────────────────────


class _FakeQuery:
    def __init__(self, client, table: str):
        self.client, self.table = client, table
        self.filters: list[tuple[str, Any]] = []
        self.operation = ""
        self.payload: dict[str, Any] = {}
        self.single = False
        self.limit_value: int | None = None

    def select(self, *_args, **_kwargs):
        self.operation = "select"; return self

    def insert(self, payload):
        self.operation, self.payload = "insert", dict(payload); return self

    def update(self, payload):
        self.operation, self.payload = "update", dict(payload); return self

    def eq(self, key, value):
        self.filters.append((key, value)); return self

    def order(self, *_args, **_kwargs):
        return self

    def limit(self, value):
        self.limit_value = value; return self

    def maybe_single(self):
        self.single = True; return self

    def execute(self):
        self.client.queries.append(self)
        rows = self.client.rows[self.table]
        matches = [
            row for row in rows
            if all(row.get(key) == value for key, value in self.filters)
        ]
        if self.limit_value is not None:
            matches = matches[: self.limit_value]
        if self.operation == "insert":
            row = dict(self.payload)
            row.setdefault("id", self.client.next_id[self.table])
            self.client.next_id[self.table] += 1
            row.setdefault("status", "active")
            row.setdefault("baseline_cursor", 0)
            row.setdefault("scanned_cursor", 0)
            row.setdefault("summarized_cursor", 0)
            row.setdefault("summary", "")
            row.setdefault("revision", 0)
            row.setdefault("version", 1)
            row.setdefault("last_error", "")
            row.setdefault("last_error_at", None)
            row.setdefault("last_committed_at", None)
            row.setdefault("created_at", "2026-11-13T12:00:00+00:00")
            row.setdefault("updated_at", "2026-11-13T12:00:00+00:00")
            rows.append(row)
            matches = [row]
        elif self.operation == "update":
            for row in matches:
                row.update(self.payload)
        if self.single:
            return SimpleNamespace(data=(matches[0] if matches else None))
        return SimpleNamespace(data=matches)


class _FakeClient:
    def __init__(self, schedule_rows: list[dict[str, Any]] | None = None):
        self.rows = {"history_schedules": [dict(row) for row in (schedule_rows or [])]}
        self.next_id = {"history_schedules": 50}
        self.queries: list[_FakeQuery] = []

    def table(self, name: str):
        return _FakeQuery(self, name)


def _row(**overrides):
    row = {
        "id": 9, "task_id": 7, "owner_id": OWNER, "peer_id": PEER,
        "status": "active", "baseline_cursor": 10, "scanned_cursor": 20,
        "summarized_cursor": 18, "summary": "old", "revision": 2, "version": 3,
        "last_error": "", "last_error_at": None,
        "last_committed_at": "2026-11-12T12:00:00+00:00",
        "created_at": "2026-11-10T12:00:00+00:00",
        "updated_at": "2026-11-12T12:00:00+00:00",
    }
    row.update(overrides); return row


@pytest.mark.asyncio
async def test_supabase_commit_is_one_cas_update_with_summary_and_cursors_together():
    client = _FakeClient([_row()])
    repo = SupabaseTaskRepository(client, InMemoryTaskRepository())
    result = await repo.commit_history_success(
        OWNER, PEER, expected_version=3,
        summary="new summary", scanned_cursor=40, summarized_cursor=35,
    )
    assert result is not None and result.version == 4 and result.revision == 3
    assert result.summary == "new summary"
    writes = [q for q in client.queries if q.operation == "update"]
    assert len(writes) == 1
    query = writes[0]
    assert ("owner_id", OWNER) in query.filters
    assert ("peer_id", PEER) in query.filters
    assert ("version", 3) in query.filters
    assert query.payload["summary"] == "new summary"
    assert query.payload["scanned_cursor"] == 40
    assert query.payload["summarized_cursor"] == 35
    assert query.payload["version"] == 4


@pytest.mark.asyncio
async def test_supabase_failure_and_reset_keep_their_own_payload_contract():
    client = _FakeClient([_row()])
    repo = SupabaseTaskRepository(client, InMemoryTaskRepository())
    failed = await repo.record_history_failure(
        OWNER, PEER, expected_version=3, error="provider failed",
    )
    failure_write = next(q for q in client.queries if q.operation == "update")
    assert failed.summary == "old" and failed.scanned_cursor == 20
    for untouched in ("summary", "scanned_cursor", "summarized_cursor"):
        assert untouched not in failure_write.payload
    assert failure_write.payload["last_error"] == "provider failed"
    assert ("version", 3) in failure_write.filters

    client.queries.clear()
    reset = await repo.reset_history_summary(OWNER, PEER, expected_version=failed.version)
    reset_write = next(q for q in client.queries if q.operation == "update")
    assert reset.summary == "" and reset.summarized_cursor == 20
    assert reset_write.payload["summary"] == ""
    assert reset_write.payload["summarized_cursor"] == 20
    assert reset_write.payload["version"] == failed.version + 1


@pytest.mark.asyncio
async def test_supabase_stale_commit_issues_no_write():
    client = _FakeClient([_row(version=4)])
    repo = SupabaseTaskRepository(client, InMemoryTaskRepository())
    result = await repo.commit_history_success(
        OWNER, PEER, expected_version=3,
        summary="stale", scanned_cursor=40, summarized_cursor=40,
    )
    assert result is None
    assert all(q.operation != "update" for q in client.queries)


@pytest.mark.asyncio
async def test_supabase_create_refuses_a_task_that_is_not_the_owners(monkeypatch):
    client = _FakeClient()
    repo = SupabaseTaskRepository(client, InMemoryTaskRepository())

    async def missing(_owner_id, _task_id):
        return None

    monkeypatch.setattr(repo, "get_task", missing)
    with pytest.raises(ValueError, match="task not found for owner"):
        await repo.create_history_schedule(OWNER, 7, PEER)
    assert all(query.operation != "insert" for query in client.queries)

    async def owned(_owner_id, task_id):
        return SimpleNamespace(id=task_id, owner_id=OWNER)

    monkeypatch.setattr(repo, "get_task", owned)
    created = await repo.create_history_schedule(OWNER, 7, PEER)
    assert created is not None and created.task_id == 7


# ── durable-only: no runtime in-memory fallback for History ─────────────────


class _UnavailableClient:
    """Every table call fails: store down, or the migration is not applied."""

    def table(self, name: str):
        raise RuntimeError(f"supabase unavailable ({name})")


class _WriteFailingClient(_FakeClient):
    """Reads work, writes fail: a CAS write that cannot be confirmed."""

    def table(self, name: str):
        query = super().table(name)
        if name == "history_schedules":
            def _refuse(_payload):
                raise RuntimeError("write failed")
            query.update = _refuse
        return query


@pytest.mark.asyncio
async def test_unavailable_store_reads_fail_closed_and_never_read_memory():
    fallback = InMemoryTaskRepository()
    task = await _task(fallback)
    seeded = await fallback.create_history_schedule(OWNER, task.id, PEER, baseline_cursor=5)
    repo = SupabaseTaskRepository(_UnavailableClient(), fallback)

    with pytest.raises(HistoryStorageUnavailable):
        await repo.get_history_schedule_by_peer(OWNER, PEER)
    with pytest.raises(HistoryStorageUnavailable):
        await repo.get_history_schedule(OWNER, task.id)
    # The injected fake was neither read through nor mutated.
    assert (await fallback.get_history_schedule_by_peer(OWNER, PEER)).version == seeded.version


@pytest.mark.asyncio
async def test_unavailable_store_writes_fail_closed_without_touching_memory():
    fallback = InMemoryTaskRepository()
    task = await _task(fallback)
    await fallback.create_history_schedule(OWNER, task.id, PEER)
    repo = SupabaseTaskRepository(_UnavailableClient(), fallback)

    with pytest.raises(HistoryStorageUnavailable):
        await repo.commit_history_success(
            OWNER, PEER, expected_version=1, summary="new",
            scanned_cursor=5, summarized_cursor=5,
        )
    with pytest.raises(HistoryStorageUnavailable):
        await repo.record_history_failure(OWNER, PEER, expected_version=1, error="boom")
    with pytest.raises(HistoryStorageUnavailable):
        await repo.reset_history_summary(OWNER, PEER, expected_version=1)

    current = await fallback.get_history_schedule_by_peer(OWNER, PEER)
    assert current.summary == "" and current.scanned_cursor == 0
    assert current.summarized_cursor == 0 and current.version == 1


@pytest.mark.asyncio
async def test_unavailable_store_refuses_history_creation_instead_of_using_memory():
    fallback = InMemoryTaskRepository()
    task = await _task(fallback)
    repo = SupabaseTaskRepository(_UnavailableClient(), fallback)

    with pytest.raises(HistoryStorageUnavailable):
        await repo.create_history_schedule(OWNER, task.id, PEER)
    assert await fallback.get_history_schedule_by_peer(OWNER, PEER) is None


@pytest.mark.asyncio
async def test_a_failed_cas_write_is_never_replayed_against_memory():
    repo = SupabaseTaskRepository(_WriteFailingClient([_row()]), InMemoryTaskRepository())

    with pytest.raises(HistoryStorageUnavailable):
        await repo.commit_history_success(
            OWNER, PEER, expected_version=3, summary="new",
            scanned_cursor=40, summarized_cursor=35,
        )
    with pytest.raises(HistoryStorageUnavailable):
        await repo.record_history_failure(
            OWNER, PEER, expected_version=3, error="provider failed",
        )
    with pytest.raises(HistoryStorageUnavailable):
        await repo.reset_history_summary(OWNER, PEER, expected_version=3)


def test_the_supabase_history_contract_never_invokes_the_fallback():
    """Durable-only is structural: no History method may reference the fake."""
    import inspect

    for name in (
        "create_history_schedule", "_repair_deleted_history_schedule",
        "get_history_schedule", "get_history_schedule_by_peer",
        "commit_history_success", "record_history_failure",
        "reset_history_summary",
    ):
        source = inspect.getsource(getattr(SupabaseTaskRepository, name))
        assert "self._fallback." not in source, f"{name} can still fall back to memory"


@pytest.mark.asyncio
async def test_an_explicitly_injected_in_memory_repository_still_works():
    repo = InMemoryTaskRepository()
    task = await _task(repo)
    created = await repo.create_history_schedule(OWNER, task.id, PEER, baseline_cursor=3)
    assert created is not None and created.baseline_cursor == 3


# ── oldest-first retrieval ──────────────────────────────────────────────────


class _FakeMessage:
    def __init__(self, mid: int, text: str = "", *, ai: bool = False, media: bool = False):
        self.id = mid
        self.chat_id = PEER
        self.sender_id = OWNER
        self.text = text + (AI_PROVENANCE_MARKER if ai else "")
        self.date = NOW
        self.media = object() if media else None
        self.reply_to = None
        self.out = True


class _FakeTelegram:
    def __init__(self, messages: list[_FakeMessage]):
        self.messages = list(messages)
        self.calls: list[dict[str, Any]] = []

    def iter_messages(self, chat_id: Any, **kwargs: Any):
        self.calls.append({"chat_id": chat_id, **kwargs})
        lower = kwargs.get("min_id")
        upper = kwargs.get("max_id")
        limit = kwargs.get("limit")
        selected = [
            message for message in self.messages
            if (lower is None or message.id > lower)
            and (upper is None or message.id < upper)
        ]
        selected.sort(key=lambda message: message.id, reverse=not kwargs.get("reverse", False))
        if limit is not None:
            selected = selected[:limit]

        async def _aiter():
            for message in selected:
                yield message

        return _aiter()


@pytest.mark.asyncio
async def test_oldest_first_batch_requests_reverse_and_returns_chronological_order():
    client = _FakeTelegram([_FakeMessage(mid, f"m{mid}") for mid in range(10, 20)])
    batch = await fetch_oldest_history(
        client, PEER, after_cursor=0, upper_cursor=100, limit=4, page_size=3,
    )
    assert [m.message_id for m in batch.messages] == [10, 11, 12, 13]
    assert batch.next_cursor == 13 and batch.scanned == 4 and batch.has_more is True
    assert client.calls[0]["reverse"] is True
    assert client.calls[0]["min_id"] == 0 and client.calls[0]["max_id"] == 100


@pytest.mark.asyncio
async def test_incremental_pages_have_no_gaps_or_duplicates_under_a_stable_upper_bound():
    client = _FakeTelegram([_FakeMessage(mid, f"m{mid}") for mid in range(10, 20)])
    seen: list[int] = []
    cursor = 0
    rounds = 0
    while True:
        batch = await fetch_oldest_history(
            client, PEER, after_cursor=cursor, upper_cursor=100,
            limit=4, page_size=3,
        )
        seen.extend(message.message_id for message in batch.messages)
        if not batch.has_more:
            break
        assert batch.next_cursor > cursor
        cursor = batch.next_cursor
        rounds += 1
        assert rounds < 10
    assert seen == list(range(10, 20))
    assert len(seen) == len(set(seen))
    # Messages arriving after the upper bound was captured can never appear.
    client.messages.append(_FakeMessage(105, "late"))
    batch = await fetch_oldest_history(
        client, PEER, after_cursor=0, upper_cursor=100, limit=100, page_size=50,
    )
    assert 105 not in {message.message_id for message in batch.messages}


@pytest.mark.asyncio
async def test_deleted_gaps_and_ai_skips_advance_the_safe_cursor_once():
    client = _FakeTelegram([
        _FakeMessage(10, "alpha"),
        _FakeMessage(11, "ai answer", ai=True),
        _FakeMessage(14, "beta"),
    ])
    batch = await fetch_oldest_history(
        client, PEER, after_cursor=0, upper_cursor=100, limit=10,
    )
    assert [m.message_id for m in batch.messages] == [10, 14]
    assert batch.scanned == 3
    assert batch.next_cursor == 14
    assert batch.has_more is False

    # Passing the batch cursor back never re-reads the skipped AI message.
    again = await fetch_oldest_history(
        client, PEER, after_cursor=batch.next_cursor, upper_cursor=100, limit=10,
    )
    assert again.messages == () and client.calls[-1]["min_id"] == 14


@pytest.mark.asyncio
async def test_empty_range_never_calls_telegram_and_upper_is_required():
    client = _FakeTelegram([_FakeMessage(5, "x")])
    batch = await fetch_oldest_history(
        client, PEER, after_cursor=10, upper_cursor=10, limit=5,
    )
    assert batch.messages == () and client.calls == []
    with pytest.raises(Exception):
        await fetch_oldest_history(client, PEER, after_cursor=0, upper_cursor=0)


@pytest.mark.asyncio
async def test_facade_exposes_the_reverse_flag_without_changing_defaults():
    client = _FakeTelegram([_FakeMessage(1, "one")])
    api = TelegramAPI(client)
    await api.iter_messages(PEER, limit=1, min_id=0, max_id=5, reverse=True)
    assert client.calls[-1]["reverse"] is True
    await api.iter_messages(PEER, limit=1)
    assert "reverse" not in client.calls[-1]


# ── AI context isolation ───────────────────────────────────────────────────


def test_scheduled_summary_payload_carries_only_bodies_and_instruction():
    messages = (
        HistoryMessage(101, PEER, 55, True, NOW, "alpha", None, False, False),
        HistoryMessage(102, PEER, 66, False, NOW, "beta", 101, True, True),
        HistoryMessage(103, PEER, 77, True, NOW, "   ", None, False, False),
    )
    payload = history_ai_service.scheduled_summary_payload(
        messages, instruction="Keep names.",
    )
    assert payload.startswith("Owner instruction: Keep names.")
    assert "alpha" in payload and "beta" in payload
    for forbidden in ("101", "102", "103", "55", "66", str(PEER), "User ", "You:", "[media]"):
        assert forbidden not in payload
    assert payload.index("alpha") < payload.index("beta")


def test_scheduled_summary_payload_has_no_metadata_when_no_body_exists():
    payload = history_ai_service.scheduled_summary_payload(
        (HistoryMessage(1, PEER, OWNER, True, NOW, "", None, False, False),),
        instruction="Summarize.",
    )
    assert payload == "Owner instruction: Summarize."
