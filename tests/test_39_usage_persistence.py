"""
TASK 39 — Usage persistence (ai_usage + ai_provider_stats).

Verifies that every AI execution produces exactly one persisted usage row
and one provider aggregate update, with honest token-source semantics and
metadata correctness, and that persistence failures never break AI
execution.
"""
from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

from backend.ai.database.manager import RepositoryManager
from backend.ai.database.usage_recorder import record_usage
from backend.ai.engine.telemetry import AIExecutionRecord


def _record(**overrides) -> AIExecutionRecord:
    base: dict = {
        "timestamp": "2026-08-22T10:00:00+00:00",
        "provider": "gemini",
        "model": "gemini-2.5-flash",
        "status": "success",
        "input_tokens": 100,
        "output_tokens": 20,
        "total_tokens": 120,
        "token_source": "actual",
        "latency": 0.5,
        "owner_id": 1,
    }
    base.update(overrides)
    return AIExecutionRecord(**base)


@pytest.mark.asyncio
async def test_record_usage_writes_row_and_aggregate():
    repos = RepositoryManager(supabase_available=False)
    ok = await record_usage(
        _record(), session_id="session-1", repos=repos,
    )
    assert ok is True

    rows = repos.usage.recent(1, limit=10)
    assert len(rows) == 1
    row = rows[0]
    assert row.session_id == "session-1"
    assert row.provider == "gemini"
    assert row.model == "gemini-2.5-flash"
    assert row.prompt_tokens == 100
    assert row.completion_tokens == 20
    assert row.total_tokens == 120
    assert row.metadata.get("token_source") == "actual"
    assert row.latency_ms == 500.0

    stats = repos.provider_stats.get("gemini", 1)
    assert stats is not None
    assert stats.total_requests == 1
    assert stats.successful_requests == 1
    assert stats.total_prompt_tokens == 100
    assert stats.total_completion_tokens == 20


@pytest.mark.asyncio
async def test_token_source_semantics_persisted():
    repos = RepositoryManager(supabase_available=False)
    for source in ("actual", "estimated", "unavailable"):
        ok = await record_usage(
            _record(token_source=source, owner_id=2),
            repos=repos,
        )
        assert ok is True

    rows = repos.usage.recent(2, limit=10)
    assert {r.metadata.get("token_source") for r in rows} == {
        "actual", "estimated", "unavailable",
    }


@pytest.mark.asyncio
async def test_failure_record_keeps_unavailable_semantics():
    repos = RepositoryManager(supabase_available=False)
    ok = await record_usage(
        _record(status="failed", input_tokens=0, output_tokens=0,
                total_tokens=0, token_source="unavailable", provider=""),
        session_id="failed-1", repos=repos,
    )
    assert ok is True

    rows = repos.usage.recent(1, limit=10)
    failed = [r for r in rows if r.session_id == "failed-1"]
    assert len(failed) == 1
    assert failed[0].total_tokens == 0
    assert failed[0].metadata.get("token_source") == "unavailable"
    # No provider aggregate is updated for an empty provider.
    assert repos.provider_stats.get("", 1) is None


@pytest.mark.asyncio
async def test_retry_execution_accounted_exactly_once():
    """A retried request persists exactly one row with merged totals."""
    from backend.ai.engine.engine import Engine
    from backend.ai.providers.base import ProviderResponse
    from backend.ai.providers.manager.manager import ProviderManager
    from backend.ai.session.request import AIRequest

    class _RetryStub(ProviderManager):
        def __init__(self) -> None:
            super().__init__()
            self.calls = 0

        def get_active_name(self) -> str:
            return "dummy"

        async def chat(self, messages: list, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return ProviderResponse(
                    text="", provider_name="dummy", success=True,
                    usage={"prompt_tokens": 100, "completion_tokens": 0,
                           "total_tokens": 100},
                    metadata={"finish_reason": "stop"},
                )
            return ProviderResponse(
                text="ok", provider_name="dummy", success=True,
                usage={"prompt_tokens": 50, "completion_tokens": 20,
                       "total_tokens": 70},
                metadata={"model": "stub-model"},
            )

    repos = RepositoryManager(supabase_available=False)
    request = AIRequest(
        session_id="retry-session", user_message="hello",
        owner_id=1, chat_id=1, message_id=1,
    )
    with patch("backend.ai.database.manager.get_repository_manager",
               return_value=repos):
        engine = Engine(providers=_RetryStub())
        result = await engine.execute(request)
        # Drain the guarded persistence task (to_thread + bounded timeout).
        await asyncio.sleep(0.3)

    assert result.success is True
    assert result.prompt_tokens == 150  # discarded + final, exactly once
    rows = repos.usage.recent(1, limit=10)
    rows = [r for r in rows if r.session_id == "retry-session"]
    assert len(rows) == 1
    assert rows[0].prompt_tokens == 150
    assert rows[0].completion_tokens == 20
    assert rows[0].total_tokens == 170
    assert rows[0].metadata.get("token_source") == "actual"
    assert rows[0].provider == "dummy"
    assert rows[0].model == "stub-model"

    stats = repos.provider_stats.get("dummy", 1)
    assert stats is not None
    assert stats.total_requests == 1  # one logical execution, one aggregate update


@pytest.mark.asyncio
async def test_persistence_failure_does_not_break_execution():
    from backend.ai.engine.engine import Engine
    from backend.ai.providers.base import ProviderResponse
    from backend.ai.providers.manager.manager import ProviderManager
    from backend.ai.session.request import AIRequest

    class _OkStub(ProviderManager):
        def __init__(self) -> None:
            super().__init__()

        def get_active_name(self) -> str:
            return "dummy"

        async def chat(self, messages: list, **kwargs):
            return ProviderResponse(
                text="ok", provider_name="dummy", success=True,
                usage={"prompt_tokens": 5, "completion_tokens": 3,
                       "total_tokens": 8},
            )

    class _RaisingRepos:
        usage = None
        provider_stats = None

        def __init__(self) -> None:
            class _Usage:
                def create(self, record):
                    raise RuntimeError("db down")
            class _Stats:
                def record_request(self, *args, **kwargs):
                    raise RuntimeError("db down")
            self.usage = _Usage()
            self.provider_stats = _Stats()

    request = AIRequest(
        session_id="fail-session", user_message="hello",
        owner_id=1, chat_id=1, message_id=1,
    )
    with patch("backend.ai.database.manager.get_repository_manager",
               return_value=_RaisingRepos()):
        engine = Engine(providers=_OkStub())
        result = await engine.execute(request)
        await asyncio.sleep(0.3)

    # Persistence failure must never break an otherwise successful request.
    assert result.success is True
    assert result.prompt_tokens == 5
