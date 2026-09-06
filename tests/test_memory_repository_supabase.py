"""
Supabase memory repository schema-compatibility regression coverage.

Pins ``SupabaseMemoryRepository`` / ``backend.ai.persistence`` memory
helpers against the EXISTING fixed ``public.ai_memories`` schema:

    id, owner_id, tier, category, content, importance, expires_at,
    metadata, created_at

The repository must serialize ONLY compatible columns, must never
reference a nonexistent column, must reconstruct rows with the exact
nine-column schema (nullable importance defaults to 0.5, the schema
default), must stay owner-scoped, must keep the duplicate-content
idempotency guarantee, and must report persistence rejection/failure
honestly (save() -> False -> tier store -> None).

No Supabase/SQL is touched: the DB is a recording fake client injected
through ``backend.ai.persistence._get_db``.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from backend.ai.database.memory_repository import SupabaseMemoryRepository
from backend.ai.memory.limits import MAX_MEMORY_ENTRY_CHARS, MEMORY_WRITE_TIMEOUT_S
from backend.ai.memory.manager import MemoryManager
from backend.ai.memory.types import MemoryCategory, MemoryEntry, MemoryQuery, MemoryTier

SCHEMA_COLUMNS = frozenset({
    "id", "owner_id", "tier", "category", "content",
    "importance", "expires_at", "metadata", "created_at",
})
INSERT_COLUMNS = SCHEMA_COLUMNS - {"id", "created_at"}  # database-generated


class _Builder:
    """Minimal supabase-py-style builder chain for the fake client."""

    def __init__(self, fake) -> None:
        self._fake = fake
        self._operation = ""
        self._payload: dict | None = None
        self._filters: dict[str, tuple[str, object]] = {}
        self._order: list[tuple[str, bool]] = []
        self._limit: int | None = None
        self._count_mode: str | None = None
        self.data: list[dict] = []
        self.count: int | None = None
        self.columns_used: set[str] = set()

    def _record(self, col: str, op: str, value: object) -> None:
        self.columns_used.add(col)
        self._filters[col] = (op, value)

    def insert(self, payload: dict):
        self._operation = "insert"
        self._payload = dict(payload)
        return self

    def select(self, *cols: str, count: str | None = None):
        self._operation = "select"
        self._count_mode = count
        for col in cols:
            if col != "*":  # wildcard selects reference no specific column
                self.columns_used.add(col)
        return self

    def delete(self):
        self._operation = "delete"
        return self

    def eq(self, col: str, value):
        self._record(col, "eq", value)
        return self

    def gte(self, col: str, value):
        self._record(col, "gte", value)
        return self

    def lt(self, col: str, value):
        self._record(col, "lt", value)
        return self

    def order(self, col: str, desc: bool = False):
        self.columns_used.add(col)
        self._order.append((col, desc))
        return self

    def limit(self, n: int):
        self._limit = n
        return self

    def _matches(self, row: dict) -> bool:
        for col, (op, value) in self._filters.items():
            rv = row.get(col)
            if op == "eq":
                if str(rv) != str(value):
                    return False
            elif op == "gte":
                if rv is None or not (rv >= value):
                    return False
            elif op == "lt":
                if rv is None or not (rv < value):
                    return False
        return True

    def execute(self):
        fake = self._fake
        fake.builder_calls.append(self)
        if self._operation == "insert":
            row = dict(self._payload)
            row.setdefault("id", len(fake.rows) + 1)
            row.setdefault("created_at", "2026-01-01T00:00:00+00:00")
            fake.rows.append(row)
            fake.inserts.append(dict(self._payload))
            self.data = [dict(row)]
            return self
        if self._operation == "delete":
            matching = [r for r in fake.rows if self._matches(r)]
            fake.rows = [r for r in fake.rows if r not in matching]
            self.data = [dict(r) for r in matching]
            return self
        rows = [r for r in fake.rows if self._matches(r)]
        for col, desc in reversed(self._order):
            rows.sort(key=lambda r, c=col: (r.get(c) is None, r.get(c)), reverse=desc)
        if self._limit is not None:
            rows = rows[: self._limit]
        if self._count_mode:
            self.count = len(rows)
            self.data = []
        else:
            self.data = [dict(r) for r in rows]
        return self


class FakeSupabaseDb:
    """Recording fake for ``backend.ai.persistence._get_db``."""

    def __init__(self, rows: list[dict] | None = None) -> None:
        self.rows = list(rows or [])
        self.inserts: list[dict] = []
        self.builder_calls: list[_Builder] = []

    def table(self, name: str) -> _Builder:
        assert name == "ai_memories"
        return _Builder(self)


class InsertFailingDb(FakeSupabaseDb):
    """Fake whose INSERT always raises — persistence failure path."""

    def table(self, name: str) -> _Builder:
        builder = super().table(name)
        real_execute = builder.execute

        def _execute():
            if builder._operation == "insert":
                raise RuntimeError("db down")
            return real_execute()

        builder.execute = _execute
        return builder


def _row(
    entry_id: int,
    owner_id: int,
    content: str,
    tier: str = "long",
    category: str = "summary",
    importance: float | None = 0.5,
    expires_at: str | None = None,
    metadata: dict | None = None,
    created_at: str | None = "2026-01-01T00:00:00+00:00",
) -> dict:
    return {
        "id": entry_id,
        "owner_id": owner_id,
        "tier": tier,
        "category": category,
        "content": content,
        "importance": importance,
        "expires_at": expires_at,
        "metadata": metadata,
        "created_at": created_at,
    }


def _entry(
    owner_id: int,
    content: str,
    tier: MemoryTier = MemoryTier.LONG,
    category: MemoryCategory = MemoryCategory.SUMMARY,
    importance: float = 0.7,
    expires_at: datetime | None = None,
    metadata: dict | None = None,
) -> MemoryEntry:
    return MemoryEntry(
        id=str(uuid.uuid4()),
        owner_id=owner_id,
        tier=tier,
        category=category,
        content=content,
        importance=importance,
        expires_at=expires_at,
        metadata=metadata or {},
    )


@pytest.fixture
def repo(monkeypatch):
    fake = FakeSupabaseDb()
    monkeypatch.setattr("backend.ai.persistence._get_db", lambda: fake)
    return SupabaseMemoryRepository(), fake


# ── Persistence payload: only ai_memories-compatible columns ───────────────


def test_save_payload_uses_only_schema_columns(repo):
    repository, fake = repo
    entry = _entry(7, "Owner prefers concise replies")
    assert repository.save(entry) is True
    assert len(fake.inserts) == 1
    payload = fake.inserts[0]
    assert set(payload.keys()) == INSERT_COLUMNS
    assert set(payload.keys()) <= SCHEMA_COLUMNS
    assert "id" not in payload and "created_at" not in payload  # DB-generated


def test_save_persists_all_fields(repo):
    repository, fake = repo
    expires = datetime(2026, 12, 31, 23, 59, tzinfo=timezone.utc)
    entry = _entry(
        7, "remember this", category=MemoryCategory.FACT, importance=0.9,
        expires_at=expires, metadata={"source": "test", "tags": ["x"]},
    )
    assert repository.save(entry) is True
    payload = fake.inserts[0]
    assert payload["owner_id"] == 7
    assert payload["tier"] == "long"
    assert payload["category"] == "fact"
    assert payload["content"] == "remember this"
    assert payload["importance"] == 0.9
    assert payload["expires_at"] == expires.isoformat()
    assert payload["metadata"] == {"source": "test", "tags": ["x"]}


def test_save_serializes_nullable_fields_when_absent(repo):
    repository, fake = repo
    entry = _entry(7, "no expiry, no metadata")
    assert repository.save(entry) is True
    payload = fake.inserts[0]
    assert payload["expires_at"] is None
    assert payload["metadata"] == {}


def test_save_permanent_tier_payload(repo):
    repository, fake = repo
    entry = _entry(7, "Owner's name is Sara", tier=MemoryTier.PERMANENT, category=MemoryCategory.FACT, importance=1.0)
    assert repository.save(entry) is True
    payload = fake.inserts[0]
    assert payload["tier"] == "permanent"
    assert payload["expires_at"] is None  # permanent entries never expire


# ── Duplicate-content idempotency ──────────────────────────────────────────


def test_duplicate_save_is_idempotent_regardless_of_rank(repo):
    """A duplicate that is NOT the top-ranked row must still be detected."""
    repository, fake = repo
    fake.rows.append(_row(1, 7, "same fact", importance=0.9))
    fake.rows.append(_row(2, 7, "same fact", importance=0.1))  # lower-ranked dup
    entry = _entry(7, "same fact", importance=0.5)
    assert repository.save(entry) is True
    assert fake.inserts == []  # never duplicated


def test_duplicate_save_after_insert_is_idempotent(repo):
    repository, fake = repo
    entry = _entry(7, "unique fact")
    assert repository.save(entry) is True
    assert len(fake.inserts) == 1
    assert repository.save(entry) is True
    assert len(fake.inserts) == 1  # second identical write: no new row


def test_different_content_same_owner_tier_still_inserts(repo):
    repository, fake = repo
    assert repository.save(_entry(7, "fact one")) is True
    assert repository.save(_entry(7, "fact two")) is True
    assert len(fake.inserts) == 2


# ── Honest failure semantics ───────────────────────────────────────────────


def test_oversized_save_rejected_without_db_contact(repo):
    repository, fake = repo
    entry = _entry(7, "z" * (MAX_MEMORY_ENTRY_CHARS + 1))
    assert repository.save(entry) is False
    assert fake.inserts == []
    assert fake.builder_calls == []


def test_save_rejection_propagates_to_tier_store_as_none(repo):
    repository, fake = repo
    manager = MemoryManager(long_repository=repository, permanent_repository=repository)
    assert manager.store_long(7, "z" * (MAX_MEMORY_ENTRY_CHARS + 1)) is None
    assert manager.store_permanent(7, "z" * (MAX_MEMORY_ENTRY_CHARS + 1)) is None


def test_save_db_failure_returns_false(repo, monkeypatch):
    fake = InsertFailingDb()
    monkeypatch.setattr("backend.ai.persistence._get_db", lambda: fake)
    repository = SupabaseMemoryRepository()
    assert repository.save(_entry(7, "will fail")) is False


def test_save_db_failure_propagates_to_tier_store_as_none(repo, monkeypatch):
    fake = InsertFailingDb()
    monkeypatch.setattr("backend.ai.persistence._get_db", lambda: fake)
    repository = SupabaseMemoryRepository()
    manager = MemoryManager(long_repository=repository, permanent_repository=repository)
    assert manager.store_long(7, "will fail") is None
    assert manager.store_permanent(7, "will fail") is None


# ── Row reconstruction with the exact nine-column schema ───────────────────


def test_query_reconstructs_exact_nine_column_rows(repo):
    repository, fake = repo
    fake.rows.append(_row(
        12, 7, "Owner likes espresso", tier="long", category="preference",
        importance=None, expires_at=None, metadata=None, created_at="2026-06-01T10:00:00+00:00",
    ))
    entries = repository.query(MemoryQuery(owner_id=7, tier=MemoryTier.LONG, limit=10))
    assert len(entries) == 1
    entry = entries[0]
    assert entry.id == "12"
    assert entry.owner_id == 7
    assert entry.tier is MemoryTier.LONG
    assert entry.category is MemoryCategory.PREFERENCE
    assert entry.content == "Owner likes espresso"
    assert entry.importance == 0.5          # schema default for NULL, never 0.0
    assert entry.expires_at is None
    assert entry.metadata == {}
    assert entry.created_at == datetime(2026, 6, 1, 10, 0, tzinfo=timezone.utc)


def test_query_preserves_stored_importance_values(repo):
    repository, fake = repo
    fake.rows.append(_row(1, 7, "zero importance fact", importance=0.0))
    fake.rows.append(_row(2, 7, "high importance fact", importance=1.0))
    entries = repository.query(MemoryQuery(owner_id=7, limit=10))
    by_id = {e.id: e.importance for e in entries}
    assert by_id == {"1": 0.0, "2": 1.0}  # 0.0 is a real value, not defaulted


def test_query_parses_expires_at_and_metadata(repo):
    repository, fake = repo
    fake.rows.append(_row(
        5, 7, "expiring fact", expires_at="2027-01-01T00:00:00+00:00",
        metadata={"source": "test"},
    ))
    entries = repository.query(MemoryQuery(owner_id=7, limit=10))
    assert entries[0].expires_at == datetime(2027, 1, 1, tzinfo=timezone.utc)
    assert entries[0].metadata == {"source": "test"}


# ── Owner isolation and filtering ──────────────────────────────────────────


def test_query_is_owner_scoped(repo):
    repository, fake = repo
    fake.rows.append(_row(1, 1, "owner one fact", importance=0.9))
    fake.rows.append(_row(2, 2, "owner two fact", importance=0.9))
    entries = repository.query(MemoryQuery(owner_id=1, limit=10))
    assert [e.content for e in entries] == ["owner one fact"]
    owner_filters = [
        b for b in fake.builder_calls
        if b._operation == "select" and b._filters.get("owner_id", ("", None))[0] == "eq"
    ]
    assert owner_filters and all(
        b._filters["owner_id"] == ("eq", 1) for b in owner_filters
    )


def test_query_applies_tier_category_importance_filters(repo):
    repository, fake = repo
    repository.query(MemoryQuery(
        owner_id=7, tier=MemoryTier.LONG, category=MemoryCategory.FACT,
        min_importance=0.3, limit=5,
    ))
    select_calls = [b for b in fake.builder_calls if b._operation == "select"]
    assert select_calls
    assert select_calls[0]._filters.get("tier") == ("eq", "long")
    assert select_calls[0]._filters.get("category") == ("eq", "fact")
    assert select_calls[0]._filters.get("importance") == ("gte", 0.3)


def test_query_orders_and_limits(repo):
    repository, fake = repo
    for i in range(5):
        fake.rows.append(_row(i + 1, 7, f"fact {i}", importance=float(i) / 10))
    entries = repository.query(MemoryQuery(owner_id=7, limit=2))
    assert [e.content for e in entries] == ["fact 4", "fact 3"]  # importance desc
    orders = {
        (col, desc)
        for b in fake.builder_calls for col, desc in b._order
    }
    assert ("importance", True) in orders
    assert ("created_at", True) in orders
    assert ("id", False) in orders


# ── Delete / expire / count ────────────────────────────────────────────────


def test_delete_by_database_id(repo):
    repository, fake = repo
    fake.rows.append(_row(7, 7, "to delete"))
    fake.rows.append(_row(8, 7, "to keep"))
    assert repository.delete("7") is True
    assert [r["content"] for r in fake.rows] == ["to keep"]
    delete_calls = [b for b in fake.builder_calls if b._operation == "delete"]
    assert delete_calls[0]._filters.get("id") == ("eq", "7")


def test_delete_expired_is_tier_scoped(repo):
    repository, fake = repo
    past = "2026-01-01T00:00:00+00:00"
    future = "2099-01-01T00:00:00+00:00"
    fake.rows.append(_row(1, 7, "expired long", tier="long", expires_at=past))
    fake.rows.append(_row(2, 7, "active long", tier="long", expires_at=future))
    fake.rows.append(_row(3, 7, "expired permanent", tier="permanent", expires_at=past))
    assert repository.delete_expired(MemoryTier.LONG) == 1
    remaining = [r["content"] for r in fake.rows]
    assert "active long" in remaining and "expired permanent" in remaining


def test_count_is_owner_and_tier_scoped(repo):
    repository, fake = repo
    fake.rows.append(_row(1, 7, "long one", tier="long"))
    fake.rows.append(_row(2, 7, "long two", tier="long"))
    fake.rows.append(_row(3, 8, "other owner", tier="long"))
    fake.rows.append(_row(4, 7, "permanent one", tier="permanent"))
    assert repository.count(7, MemoryTier.LONG) == 2
    assert repository.count(8, MemoryTier.LONG) == 1
    assert repository.count(7, MemoryTier.PERMANENT) == 1
    count_calls = [b for b in fake.builder_calls if b._count_mode == "exact"]
    assert count_calls and count_calls[0]._filters.get("owner_id") == ("eq", 7)


# ── Global contract: no nonexistent ai_memories column is ever referenced ──


def test_no_nonexistent_schema_column_is_referenced(repo):
    repository, fake = repo
    expires = datetime.now(timezone.utc) + timedelta(days=1)
    fake.rows.append(_row(1, 7, "seeded fact", importance=0.8))
    repository.save(_entry(7, "written fact", expires_at=expires, metadata={"k": "v"}))
    repository.query(MemoryQuery(owner_id=7, tier=MemoryTier.LONG, limit=3))
    repository.delete("1")
    repository.delete_expired(MemoryTier.LONG)
    repository.count(7, MemoryTier.PERMANENT)
    assert fake.inserts
    used = set()
    for builder in fake.builder_calls:
        used |= builder.columns_used
        if builder._payload:
            used |= set(builder._payload.keys())
    assert used <= SCHEMA_COLUMNS


# ── Tool-level behavior through the Supabase repository ────────────────────


def _tool_manager(repo) -> MemoryManager:
    return MemoryManager(long_repository=repo, permanent_repository=repo)


@pytest.mark.asyncio
async def test_memory_store_tool_writes_long_tier_through_supabase_repo(repo, monkeypatch):
    from backend.ai.tools.context import ToolContext
    from backend.ai.tools.memory import MemoryStoreTool

    repository, fake = repo
    monkeypatch.setattr(
        "backend.ai.tools.memory._resolve_memory_manager",
        lambda: _tool_manager(repository),
    )
    tool = MemoryStoreTool(ToolContext(telegram=None, owner_id=42, tz_str="UTC"))
    result = await tool.execute(
        ToolContext(telegram=None, owner_id=42, tz_str="UTC"),
        {"content": "Prefers voice notes", "tier": "long", "importance": 0.8},
    )
    assert result.success is True
    assert fake.inserts[0]["owner_id"] == 42
    assert fake.inserts[0]["tier"] == "long"
    assert fake.inserts[0]["category"] == "summary"


@pytest.mark.asyncio
async def test_memory_store_tool_writes_permanent_tier_through_supabase_repo(repo, monkeypatch):
    from backend.ai.tools.context import ToolContext
    from backend.ai.tools.memory import MemoryStoreTool

    repository, fake = repo
    monkeypatch.setattr(
        "backend.ai.tools.memory._resolve_memory_manager",
        lambda: _tool_manager(repository),
    )
    tool = MemoryStoreTool(ToolContext(telegram=None, owner_id=42, tz_str="UTC"))
    result = await tool.execute(
        ToolContext(telegram=None, owner_id=42, tz_str="UTC"),
        {"content": "Owner's name is Sara", "tier": "permanent"},
    )
    assert result.success is True
    assert fake.inserts[0]["tier"] == "permanent"
    assert fake.inserts[0]["category"] == "fact"
    assert fake.inserts[0]["expires_at"] is None


@pytest.mark.asyncio
async def test_memory_list_tool_reads_supabase_rows(repo, monkeypatch):
    from backend.ai.tools.context import ToolContext
    from backend.ai.tools.memory import MemoryListTool

    repository, fake = repo
    fake.rows.append(_row(1, 42, "remembers espresso preference", importance=0.9))
    monkeypatch.setattr(
        "backend.ai.tools.memory._resolve_memory_manager",
        lambda: _tool_manager(repository),
    )
    tool = MemoryListTool(ToolContext(telegram=None, owner_id=42, tz_str="UTC"))
    result = await tool.execute(
        ToolContext(telegram=None, owner_id=42, tz_str="UTC"), {},
    )
    assert result.success is True
    assert "remembers espresso preference" in result.message


@pytest.mark.asyncio
async def test_memory_tool_owner_isolation_with_supabase_repo(repo, monkeypatch):
    from backend.ai.tools.context import ToolContext
    from backend.ai.tools.memory import MemoryListTool, MemoryStoreTool

    repository, fake = repo
    monkeypatch.setattr(
        "backend.ai.tools.memory._resolve_memory_manager",
        lambda: _tool_manager(repository),
    )
    store = MemoryStoreTool(ToolContext(telegram=None, owner_id=42, tz_str="UTC"))
    result = await store.execute(
        ToolContext(telegram=None, owner_id=42, tz_str="UTC"),
        {"content": "owner 42 secret"},
    )
    assert result.success is True
    lst = MemoryListTool(ToolContext(telegram=None, owner_id=99, tz_str="UTC"))
    other = await lst.execute(
        ToolContext(telegram=None, owner_id=99, tz_str="UTC"), {},
    )
    assert other.success is True
    assert "No memories stored" in other.message
    assert fake.inserts[0]["owner_id"] == 42


# ── Bounds preserved ───────────────────────────────────────────────────────


def test_memory_write_timeout_bound_is_preserved():
    assert MEMORY_WRITE_TIMEOUT_S == 3.0


def test_engine_repository_availability():
    """The default Engine still wires repository-backed memory tiers."""
    from backend.ai.engine.engine import Engine

    engine = Engine()
    status = engine.memory_manager.status()
    assert status["long_available"] is True
    assert status["permanent_available"] is True