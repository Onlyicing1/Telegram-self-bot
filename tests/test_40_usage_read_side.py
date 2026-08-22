"""
TASK 40 — AI usage read-side / observability.

Verifies the async read accessor over the persisted usage repositories
(``backend/ai/database/usage_reader.py``): total/daily/recent reads,
provider statistics, token-source honesty in aggregation, safe
degradation on repository failure, and no direct Supabase access outside
the repository layer. Also verifies the Usage panel's additive persisted
line (which must never alter the session view when nothing is saved).
"""
from __future__ import annotations

import inspect
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from backend.ai.database import usage_reader
from backend.ai.database.manager import RepositoryManager
from backend.ai.database.usage_repository import UsageRecord


def _usage(owner_id: int, total: int, source: str = "actual",
           minutes_ago: int = 0, prompt: int = 0, completion: int = 0,
           provider: str = "gemini") -> UsageRecord:
    created = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    return UsageRecord(
        id=str(uuid.uuid4()),
        owner_id=owner_id,
        session_id="s",
        provider=provider,
        model="gemini-2.5-flash",
        prompt_tokens=prompt,
        completion_tokens=completion,
        total_tokens=total,
        created_at=created,
        metadata={"token_source": source},
    )


async def _run_with(repos: RepositoryManager, coro):
    """Run a reader coroutine against a specific manager."""
    with patch("backend.ai.database.manager.get_repository_manager", return_value=repos):
        return await coro


@pytest.mark.asyncio
async def test_total_usage_read():
    repos = RepositoryManager(supabase_available=False)
    repos.usage.create(_usage(1, 100))
    repos.usage.create(_usage(1, 50))
    repos.usage.create(_usage(2, 999))

    assert await _run_with(repos, usage_reader.total_tokens(1)) == 150
    assert await _run_with(repos, usage_reader.total_tokens(2)) == 999
    assert await _run_with(repos, usage_reader.total_tokens(3)) == 0


@pytest.mark.asyncio
async def test_daily_usage_read():
    repos = RepositoryManager(supabase_available=False)
    now = datetime.now(timezone.utc)
    repos.usage.create(_usage(1, 100, minutes_ago=5))       # today
    repos.usage.create(_usage(1, 40, minutes_ago=60 * 26))  # yesterday
    repos.usage.create(_usage(2, 777, minutes_ago=5))

    assert await _run_with(repos, usage_reader.daily_tokens(1, now)) == 100
    yesterday = now - timedelta(days=1)
    assert await _run_with(repos, usage_reader.daily_tokens(1, yesterday)) == 40


@pytest.mark.asyncio
async def test_recent_usage_read():
    repos = RepositoryManager(supabase_available=False)
    repos.usage.create(_usage(1, 10, minutes_ago=30))
    repos.usage.create(_usage(1, 20, minutes_ago=20))
    repos.usage.create(_usage(1, 30, minutes_ago=10))
    repos.usage.create(_usage(2, 999, minutes_ago=5))

    recent = await _run_with(repos, usage_reader.recent(1, limit=2))
    assert [r.total_tokens for r in recent] == [30, 20]  # newest first, bounded


@pytest.mark.asyncio
async def test_provider_statistics_read():
    repos = RepositoryManager(supabase_available=False)
    repos.provider_stats.record_request("gemini", 1, success=True, prompt_tokens=100,
                                        completion_tokens=20, latency_ms=500.0)
    repos.provider_stats.record_request("gemini", 1, success=False, prompt_tokens=10,
                                        completion_tokens=0, latency_ms=300.0)
    repos.provider_stats.record_request("openai", 2, success=True, prompt_tokens=5,
                                        completion_tokens=5, latency_ms=100.0)

    stats = await _run_with(repos, usage_reader.provider_stats(1))
    assert len(stats) == 1
    gemini = stats[0]
    assert gemini.provider_name == "gemini"
    assert gemini.total_requests == 2
    assert gemini.successful_requests == 1
    assert gemini.failed_requests == 1
    assert gemini.total_prompt_tokens == 110
    assert gemini.total_completion_tokens == 20


@pytest.mark.asyncio
async def test_summary_actual_token_source_preserved():
    repos = RepositoryManager(supabase_available=False)
    repos.usage.create(_usage(1, 100, source="actual", prompt=80, completion=20))
    repos.usage.create(_usage(1, 40, source="actual", prompt=40))

    summary = await _run_with(repos, usage_reader.summary(1))
    assert summary.available is True
    assert summary.requests == 2
    assert summary.total_tokens == 140
    assert summary.token_source.actual == 140
    assert summary.token_source.estimated == 0
    assert summary.token_source.unavailable == 0
    assert summary.token_source.sources == ["actual"]
    assert summary.input_tokens == 120
    assert summary.output_tokens == 20


@pytest.mark.asyncio
async def test_summary_estimated_token_source_preserved():
    repos = RepositoryManager(supabase_available=False)
    repos.usage.create(_usage(1, 60, source="estimated"))

    summary = await _run_with(repos, usage_reader.summary(1))
    assert summary.token_source.estimated == 60
    assert summary.token_source.actual == 0
    assert summary.token_source.sources == ["estimated"]


@pytest.mark.asyncio
async def test_summary_unavailable_token_source_preserved():
    repos = RepositoryManager(supabase_available=False)
    repos.usage.create(_usage(1, 0, source="unavailable"))
    repos.usage.create(_usage(1, 0, source="unavailable"))

    summary = await _run_with(repos, usage_reader.summary(1))
    assert summary.requests == 2
    assert summary.total_tokens == 0
    assert summary.token_source.unavailable == 0  # zero counts, labelled unavailable
    assert summary.token_source.sources == []
    # The label survives aggregation even though no tokens were counted.
    assert summary.sources == ("unavailable",)


@pytest.mark.asyncio
async def test_summary_mixed_sources_never_merged_silently():
    repos = RepositoryManager(supabase_available=False)
    repos.usage.create(_usage(1, 100, source="actual"))
    repos.usage.create(_usage(1, 50, source="estimated"))
    repos.usage.create(_usage(1, 25, source="unavailable"))

    summary = await _run_with(repos, usage_reader.summary(1))
    assert summary.total_tokens == 175
    assert summary.token_source.actual == 100
    assert summary.token_source.estimated == 50
    assert summary.token_source.unavailable == 25
    assert summary.token_source.sources == ["actual", "estimated", "unavailable"]


@pytest.mark.asyncio
async def test_summary_window_filter():
    repos = RepositoryManager(supabase_available=False)
    repos.usage.create(_usage(1, 100, minutes_ago=5))       # in window
    repos.usage.create(_usage(1, 999, minutes_ago=60 * 48))  # outside window
    since = datetime.now(timezone.utc) - timedelta(hours=24)

    summary = await _run_with(repos, usage_reader.summary(1, since=since))
    assert summary.requests == 1
    assert summary.total_tokens == 100


@pytest.mark.asyncio
async def test_repository_failure_does_not_break_execution():
    class _Boom:
        def total_tokens(self, owner_id):
            raise RuntimeError("db down")

        def daily_tokens(self, owner_id, date):
            raise RuntimeError("db down")

        def recent(self, owner_id, limit):
            raise RuntimeError("db down")

    class _BoomStats:
        def list_all(self, owner_id):
            raise RuntimeError("db down")

    class _BrokenRepos:
        usage = _Boom()
        provider_stats = _BoomStats()

    with patch("backend.ai.database.manager.get_repository_manager",
               return_value=_BrokenRepos()):
        assert await usage_reader.total_tokens(1) == 0
        assert await usage_reader.daily_tokens(1) == 0
        assert await usage_reader.recent(1) == []
        assert await usage_reader.provider_stats(1) == []
        summary = await usage_reader.summary(1)
        assert summary.available is False
        assert summary.requests == 0
        assert summary.total_tokens == 0


def test_no_direct_supabase_access_outside_repository_layer():
    """The reader must only talk to repositories, never Supabase directly."""
    source = inspect.getsource(usage_reader)
    assert "backend.db.client" not in source
    assert "get_db" not in source
    assert ".table(" not in source
    # It must reach the data exclusively through the repository manager.
    assert "get_repository_manager" in source


@pytest.mark.asyncio
async def test_panel_appends_persisted_line_when_saved_data_exists():
    from backend.bot.handlers import ai as ai_module

    repos = RepositoryManager(supabase_available=False)
    repos.usage.create(_usage(1, 12200, source="actual", prompt=10000, completion=2200))
    repos.usage.create(_usage(1, 200, source="estimated", prompt=200))

    with patch("backend.ai.database.manager.get_repository_manager", return_value=repos), \
         patch.object(ai_module, "_get_owner_id", AsyncMock(return_value=1)):
        title, body, buttons = await ai_module._ai_usage_panel_handler(None, "today")

    assert title == "AI · Usage"
    assert "Saved · 2 requests" in body
    assert "12.4k tokens" in body
    assert "12.2k actual" in body
    assert "200 ≈" in body


@pytest.mark.asyncio
async def test_panel_unchanged_when_nothing_persisted():
    from backend.bot.handlers import ai as ai_module

    repos = RepositoryManager(supabase_available=False)
    with patch("backend.ai.database.manager.get_repository_manager", return_value=repos), \
         patch.object(ai_module, "_get_owner_id", AsyncMock(return_value=1)):
        title, body, buttons = await ai_module._ai_usage_panel_handler(None, "today")

    assert title == "AI · Usage"
    assert "Saved ·" not in body


@pytest.mark.asyncio
async def test_panel_survives_persisted_read_failure():
    from backend.bot.handlers import ai as ai_module

    repos = RepositoryManager(supabase_available=False)
    with patch("backend.ai.database.manager.get_repository_manager", return_value=repos), \
         patch.object(ai_module, "_get_owner_id", AsyncMock(return_value=1)), \
         patch.object(usage_reader, "summary", AsyncMock(side_effect=RuntimeError("db down"))):
        title, body, buttons = await ai_module._ai_usage_panel_handler(None, "today")

    assert title == "AI · Usage"
    assert "Saved ·" not in body


def test_persisted_line_source_formatting():
    from backend.bot.handlers import ai as ai_module
    from backend.ai.database.usage_reader import UsageSummary, TokenSourceBreakdown

    all_actual = UsageSummary(requests=3, total_tokens=2671,
                              token_source=TokenSourceBreakdown(actual=2671))
    assert ai_module._persisted_usage_line(all_actual) == \
        "Saved · 3 requests · 2.7k tokens · actual"

    mixed = UsageSummary(requests=2, total_tokens=300,
                         token_source=TokenSourceBreakdown(actual=200, estimated=100))
    assert ai_module._persisted_usage_line(mixed) == \
        "Saved · 2 requests · 300 tokens · 200 actual + 100 ≈"

    unavailable = UsageSummary(requests=1, total_tokens=0,
                               token_source=TokenSourceBreakdown(unavailable=0),
                               sources=("unavailable",))
    assert ai_module._persisted_usage_line(unavailable) == \
        "Saved · 1 requests · tokens unavailable"
