"""
Execution 21 follow-up — database-management statistics extension.

The contract's next unblocked item after per-message Details is the
additive Database Statistics read surface for AI usage/provider rows and
an optional Ghost Room registry. These tests keep all reads behind the
existing repository/database abstractions and pin safe degradation.
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.ai.database.manager import RepositoryManager
from backend.ai.database.provider_stats_repository import (
    SupabaseProviderStatsRepository,
)
from backend.ai.database.usage_repository import (
    SupabaseUsageRepository,
    UsageRecord,
)
from backend.services import database_service


def _saved_stats() -> dict:
    return {
        "total": 3,
        "by_type": {"Photo": 2, "Document": 1},
        "size_estimate": 2048,
        "oldest": "2026-08-20T10:00:00+00:00",
        "newest": "2026-08-22T10:00:00+00:00",
    }


@pytest.mark.asyncio
async def test_in_memory_ai_row_counts_are_owner_scoped():
    repos = RepositoryManager(supabase_available=False)
    repos.usage.create(UsageRecord(
        id="u1", owner_id=1, total_tokens=10,
        created_at=datetime.now(timezone.utc),
    ))
    repos.usage.create(UsageRecord(
        id="u2", owner_id=1, total_tokens=20,
        created_at=datetime.now(timezone.utc),
    ))
    repos.usage.create(UsageRecord(
        id="u3", owner_id=2, total_tokens=30,
        created_at=datetime.now(timezone.utc),
    ))
    repos.provider_stats.record_request(
        "gemini", 1, success=True, prompt_tokens=1,
        completion_tokens=1, latency_ms=1,
    )
    repos.provider_stats.record_request(
        "openai", 2, success=True, prompt_tokens=1,
        completion_tokens=1, latency_ms=1,
    )

    with patch(
        "backend.ai.database.manager.get_repository_manager",
        return_value=repos,
    ):
        assert await database_service._ai_database_counts(1) == (2, 1)
        assert await database_service._ai_database_counts(2) == (1, 1)
        assert await database_service._ai_database_counts(3) == (0, 0)


class _FakeQuery:
    def __init__(self, count: int = 0, error: Exception | None = None):
        self._count = count
        self._error = error

    def select(self, *_args, **_kwargs):
        return self

    def eq(self, *_args, **_kwargs):
        return self

    def execute(self):
        if self._error:
            raise self._error
        return SimpleNamespace(count=self._count, data=[])


class _FakeDb:
    def __init__(self, counts: dict[str, int] | None = None,
                 errors: dict[str, Exception] | None = None):
        self._counts = counts or {}
        self._errors = errors or {}

    def table(self, name: str):
        return _FakeQuery(
            count=self._counts.get(name, 0),
            error=self._errors.get(name),
        )


@pytest.mark.parametrize(
    "repository, table, expected",
    [
        (SupabaseUsageRepository(), "ai_usage", 4),
        (SupabaseProviderStatsRepository(), "ai_provider_stats", 2),
    ],
)
def test_supabase_repository_count_uses_exact_owner_count(repository, table, expected):
    fake_db = _FakeDb(counts={table: expected})
    with patch(
        "backend.db.client.get_db",
        return_value=fake_db,
    ):
        assert repository.count(7) == expected


@pytest.mark.parametrize(
    "repository",
    [SupabaseUsageRepository(), SupabaseProviderStatsRepository()],
)
def test_supabase_repository_count_degrades_when_table_read_fails(repository):
    fake_db = _FakeDb(errors={
        "ai_usage": RuntimeError("missing table"),
        "ai_provider_stats": RuntimeError("missing table"),
    })
    with patch("backend.db.client.get_db", return_value=fake_db):
        assert repository.count(7) is None


def test_ghost_room_count_reads_exact_rows_when_table_exists():
    from backend.db import client as db_client

    fake_db = _FakeDb(counts={"ghost_chats": 5})
    with patch("backend.db.client.get_db", return_value=fake_db):
        assert db_client._count_ghost_chats_sync() == 5


def test_ghost_room_count_returns_none_without_available_database():
    with patch("backend.db.client.get_db", return_value=None):
        from backend.db import client as db_client
        assert db_client._count_ghost_chats_sync() is None


@pytest.mark.asyncio
async def test_database_stats_adds_ai_and_ghost_counts_without_changing_saved_stats():
    repos = RepositoryManager(supabase_available=False)
    repos.usage.create(UsageRecord(
        id="u1", owner_id=42, total_tokens=10,
        created_at=datetime.now(timezone.utc),
    ))
    repos.provider_stats.record_request(
        "gemini", 42, success=True, prompt_tokens=1,
        completion_tokens=1, latency_ms=1,
    )

    with patch.object(database_service.db_client, "get_stats", new=AsyncMock(return_value=_saved_stats())), \
         patch.object(database_service.db_client, "count_ghost_chats", new=AsyncMock(return_value=4)), \
         patch.object(database_service.db_client, "log", new=AsyncMock()), \
         patch.object(database_service, "record_event") as record_event, \
         patch("backend.ai.database.manager.get_repository_manager", return_value=repos):
        result = await database_service.do_stats(42, "UTC")

    assert "Total saved items: `3`" in result
    assert "📷 Photo: `2`" in result
    assert "📄 Document: `1`" in result
    assert "**Database size estimate:** `2.0 KB`" in result
    assert "**AI usage rows:** `1`" in result
    assert "**AI provider rows:** `1`" in result
    assert "**Ghost Room chats:** `4`" in result
    record_event.assert_called_once()


@pytest.mark.asyncio
async def test_database_stats_reports_unavailable_optional_tables_and_keeps_legacy_success():
    class _BrokenRepository:
        def count(self, owner_id):
            raise RuntimeError("AI table unavailable")

    broken = SimpleNamespace(
        usage=_BrokenRepository(),
        provider_stats=_BrokenRepository(),
    )
    with patch.object(database_service.db_client, "get_stats", new=AsyncMock(return_value=_saved_stats())), \
         patch.object(database_service.db_client, "count_ghost_chats", new=AsyncMock(return_value=None)), \
         patch.object(database_service.db_client, "log", new=AsyncMock()), \
         patch.object(database_service, "record_event"), \
         patch("backend.ai.database.manager.get_repository_manager", return_value=broken):
        result = await database_service.do_stats(42, "UTC")

    assert "Total saved items: `3`" in result
    assert "**AI usage rows:** `Unavailable`" in result
    assert "**AI provider rows:** `Unavailable`" in result
    assert "**Ghost Room chats:** `Unavailable`" in result
    assert "Stats error" not in result


@pytest.mark.asyncio
async def test_database_stats_preserves_failure_boundary_for_saved_items():
    with patch.object(
        database_service.db_client,
        "get_stats",
        new=AsyncMock(side_effect=RuntimeError("saved_items unavailable")),
    ), patch.object(database_service, "record_event"):
        result = await database_service.do_stats(42, "UTC")

    assert result.startswith("❌ Stats error:")
    assert "saved_items unavailable" in result


def test_database_stats_tool_description_mentions_optional_counts():
    from backend.ai.tools.context import ToolContext
    from backend.ai.tools.database import DatabaseStatsTool

    tool = DatabaseStatsTool(ToolContext(telegram=None, owner_id=42, tz_str="UTC"))
    assert "AI" in tool.description
    assert "Ghost" in tool.description
    assert "api_key" not in tool.description.lower()
