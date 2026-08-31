from types import SimpleNamespace

import pytest

from backend.ai.database.task_repository import InMemoryTaskRepository, SupabaseTaskRepository


def task_data(**overrides):
    data = {"label": "Daily check", "schedule_type": "daily", "schedule": {"hour": 8, "minute": 30}, "timezone": "Asia/Tehran", "next_run_at": None, "actions": [{"name": "list_saves", "arguments": {}}], "notification_destination": {"chat_id": 123}}
    data.update(overrides); return data


def occurrence_data(task_id, **overrides):
    data = {"task_id": task_id, "occurrence_key": "2026-08-29T08:30:00+00:00", "definition_version": 1, "action_snapshot": [{"name": "list_saves", "arguments": {}}], "scheduled_for": "2026-08-29T08:30:00+00:00"}
    data.update(overrides); return data


@pytest.mark.asyncio
async def test_task_creation_owner_isolation_and_cas():
    repo = InMemoryTaskRepository(); task = await repo.create_task(10, task_data())
    assert task.version == 1; assert await repo.get_task(11, task.id) is None
    updated = await repo.update_task(10, task.id, 1, {"label": "Updated"})
    assert updated.version == 2 and updated.label == "Updated"
    assert await repo.update_task(10, task.id, 1, {"label": "stale"}) is None


@pytest.mark.asyncio
async def test_occurrence_uniqueness_owner_isolation_and_snapshot():
    repo = InMemoryTaskRepository(); task = await repo.create_task(10, task_data())
    first = await repo.create_occurrence(10, occurrence_data(task.id)); duplicate = await repo.create_occurrence(10, occurrence_data(task.id, action_snapshot=[{"name": "changed"}]))
    assert duplicate.id == first.id and duplicate.action_snapshot == first.action_snapshot
    assert await repo.get_occurrence(11, task.id, first.occurrence_key) is None
    with pytest.raises(ValueError): await repo.create_occurrence(11, occurrence_data(task.id))


@pytest.mark.asyncio
async def test_attempt_limit_retry_and_status_transitions():
    repo = InMemoryTaskRepository(); task = await repo.create_task(10, task_data())
    for attempt in (1, 2, 3): assert (await repo.create_occurrence(10, occurrence_data(task.id, occurrence_key=f"k{attempt}", attempt=attempt))).attempt == attempt
    with pytest.raises(ValueError): await repo.create_occurrence(10, occurrence_data(task.id, occurrence_key="k4", attempt=4))
    record = await repo.get_occurrence(10, task.id, "k1"); assert (await repo.claim_occurrence(10, task.id, record.occurrence_key)).status == "running"
    assert (await repo.transition_occurrence(10, task.id, record.occurrence_key, "retry_pending", retry_at="2026-08-29T09:30:00+00:00", attempt=2)).status == "retry_pending"
    assert (await repo.transition_occurrence(10, task.id, record.occurrence_key, "interrupted")).status == "interrupted"


@pytest.mark.asyncio
async def test_terminal_transitions_and_retry_timing_are_rejected():
    repo = InMemoryTaskRepository(); task = await repo.create_task(10, task_data()); record = await repo.create_occurrence(10, occurrence_data(task.id))
    with pytest.raises(ValueError): await repo.transition_occurrence(10, task.id, record.occurrence_key, "retry_pending")
    await repo.claim_occurrence(10, task.id, record.occurrence_key); await repo.transition_occurrence(10, task.id, record.occurrence_key, "succeeded")
    with pytest.raises(ValueError): await repo.transition_occurrence(10, task.id, record.occurrence_key, "running")
    await repo.transition_task(10, task.id, "deleted"); assert (await repo.get_occurrence(10, task.id, record.occurrence_key)).status == "succeeded"


@pytest.mark.asyncio
async def test_snapshot_is_independent_and_terminal_task_is_not_reactivated():
    repo = InMemoryTaskRepository(); task = await repo.create_task(10, task_data()); occurrence = await repo.create_occurrence(10, occurrence_data(task.id))
    await repo.update_task(10, task.id, 1, {"actions": [{"name": "changed", "arguments": {}}]})
    assert (await repo.get_occurrence(10, task.id, occurrence.occurrence_key)).action_snapshot[0]["name"] == "list_saves"
    assert (await repo.transition_task(10, task.id, "completed")).terminal_at is not None
    with pytest.raises(ValueError): await repo.transition_task(10, task.id, "active")


@pytest.mark.asyncio
async def test_bounded_json_and_malformed_payloads_are_rejected():
    repo = InMemoryTaskRepository()
    with pytest.raises(ValueError): await repo.create_task(10, task_data(actions=[]))
    with pytest.raises(ValueError): await repo.create_task(10, task_data(actions=[{"name": "x"}] * 6))
    task = await repo.create_task(10, task_data())
    with pytest.raises(ValueError): await repo.create_occurrence(10, occurrence_data(task.id, action_snapshot={"bad": True}))
    with pytest.raises(ValueError): await repo.create_occurrence(10, occurrence_data(task.id, error_metadata=[]))


class FakeQuery:
    def __init__(self, client, table_name): self.client, self.table_name, self.filters, self.payload, self.single = client, table_name, [], {}, False
    def select(self, *_args, **_kwargs): self.operation = "select"; return self
    def insert(self, payload): self.payload = payload; self.operation = "insert"; return self
    def update(self, payload): self.payload = payload; self.operation = "update"; return self
    def eq(self, key, value): self.filters.append((key, value)); return self
    def in_(self, key, values): self.filters.append((key, set(values))); return self
    def order(self, *_args, **_kwargs): return self
    def limit(self, value): self.limit_value = value; return self
    def maybe_single(self): self.single = True; return self
    def execute(self):
        if self.client.error: raise self.client.error
        rows = self.client.rows[self.table_name]
        matches = [r for r in rows if all(r.get(k) in v if isinstance(v, set) else r.get(k) == v for k, v in self.filters)]
        if getattr(self, "limit_value", None) is not None:
            matches = matches[:self.limit_value]
        if getattr(self, "operation", None) == "insert":
            row = dict(self.payload); row.setdefault("id", self.client.next_id[self.table_name]); self.client.next_id[self.table_name] += 1; rows.append(row); matches = [row]
        elif getattr(self, "operation", None) == "update":
            for row in matches: row.update(self.payload)
        return SimpleNamespace(data=(matches[0] if self.single else (matches[:1] if getattr(self, "operation", None) in {"insert", "update"} else matches)))


class FakeClient:
    def __init__(self, task_rows=None, occurrence_rows=None, error=None):
        self.rows = {"ai_tasks": [dict(r) for r in (task_rows or [])], "ai_task_occurrences": [dict(r) for r in (occurrence_rows or [])]}; self.next_id = {"ai_tasks": 100, "ai_task_occurrences": 200}; self.error = error
    def table(self, name): return FakeQuery(self, name)


def row_task(**overrides):
    row = {"id": 7, "owner_id": 10, "label": "Daily check", "status": "active", "version": 1, "schedule_type": "daily", "schedule": {"hour": 8, "minute": 30}, "timezone": "UTC", "next_run_at": None, "actions": [{"name": "list_saves", "arguments": {}}], "notification_destination": {"chat_id": 123}, "created_at": "2026-08-29T08:00:00+00:00", "updated_at": "2026-08-29T08:00:00+00:00", "terminal_at": None}; row.update(overrides); return row


def row_occurrence(**overrides):
    row = {"id": 9, "task_id": 7, "owner_id": 10, "occurrence_key": "k1", "definition_version": 1, "action_snapshot": [{"name": "list_saves", "arguments": {}}], "scheduled_for": "2026-08-29T08:30:00+00:00", "attempt": 1, "status": "claimed", "claimed_at": None, "started_at": None, "finished_at": None, "retry_at": None, "error_metadata": {}, "result_metadata": {}, "created_at": "2026-08-29T08:00:00+00:00", "updated_at": "2026-08-29T08:00:00+00:00"}; row.update(overrides); return row


@pytest.mark.asyncio
async def test_supabase_task_create_get_list_and_atomic_cas():
    client = FakeClient([row_task()]); repo = SupabaseTaskRepository(client, InMemoryTaskRepository())
    task = await repo.get_task(10, 7); assert task.id == 7; assert len(await repo.list_tasks(10)) == 1
    updated = await repo.update_task(10, 7, 1, {"label": "Updated"}); assert updated.version == 2 and updated.label == "Updated"
    assert await repo.update_task(10, 7, 1, {"label": "stale"}) is None


@pytest.mark.asyncio
async def test_supabase_occurrence_idempotency_claim_and_transition():
    client = FakeClient([row_task()], [row_occurrence()]); repo = SupabaseTaskRepository(client, InMemoryTaskRepository())
    first = await repo.create_occurrence(10, occurrence_data(7)); duplicate = await repo.create_occurrence(10, occurrence_data(7))
    assert first.id == duplicate.id
    claimed = await repo.claim_occurrence(10, 7, "k1"); assert claimed.status == "running"
    succeeded = await repo.transition_occurrence(10, 7, "k1", "succeeded"); assert succeeded.status == "succeeded"
    assert await repo.claim_occurrence(10, 7, "k1") is None


@pytest.mark.asyncio
async def test_supabase_recovery_query_includes_interrupted_and_scopes_owner():
    client = FakeClient(
        [row_task()],
        [row_occurrence(status="interrupted"), row_occurrence(occurrence_key="other", status="succeeded"), row_occurrence(occurrence_key="foreign", owner_id=11, status="interrupted")],
    )
    repo = SupabaseTaskRepository(client, InMemoryTaskRepository())
    rows = await repo.list_recoverable_occurrences(10, limit=10)
    assert [row.occurrence_key for row in rows] == ["k1"]


@pytest.mark.asyncio
async def test_supabase_failure_uses_fallback_without_swallowing_validation():
    fallback = InMemoryTaskRepository(); repo = SupabaseTaskRepository(FakeClient(error=RuntimeError("database unavailable")), fallback)
    created = await repo.create_task(10, task_data()); assert created.owner_id == 10; assert await repo.get_task(11, created.id) is None
    with pytest.raises(ValueError): await repo.create_task(10, task_data(actions=[]))
