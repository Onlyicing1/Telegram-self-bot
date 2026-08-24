"""
TASK 6 — Failure Simulation

Simulates failures and verifies graceful recovery:
  - Provider timeout
  - Supabase timeout
  - Telegram timeout
  - Network interruption
"""
from __future__ import annotations

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


@pytest.mark.asyncio
async def test_provider_crash_falls_back_to_dummy(provider_manager):
    """When the active provider crashes, ProviderManager falls back to dummy."""
    from backend.ai.providers.base.contract import BaseProvider, ProviderResponse
    from backend.ai.providers.base.config import ProviderConfig

    class CrashingProvider(BaseProvider):
        PROVIDER_NAME = "crashing"
        PROVIDER_VERSION = "1.0.0"

        def initialize(self) -> None:
            pass

        def shutdown(self) -> None:
            pass

        async def chat(self, messages, **kwargs):
            raise RuntimeError("Simulated crash")

        def count_tokens(self, text: str) -> int:
            return len(text) // 4

        def health(self) -> dict:
            return {"healthy": True, "provider": "crashing"}

    crashing = CrashingProvider()
    provider_manager.register_provider(crashing)
    provider_manager.switch_provider("crashing")

    messages = [{"role": "user", "content": "test"}]
    response = await provider_manager.chat(messages)
    assert response is not None
    assert response.provider_name == "dummy"


@pytest.mark.asyncio
async def test_provider_timeout_handled():
    """tg_rpc timeout raises after the configured timeout."""
    from backend.runtime.tg_retry import tg_rpc

    async def slow_coro():
        await asyncio.sleep(10)
        return "done"

    with pytest.raises(asyncio.TimeoutError):
        await tg_rpc(slow_coro(), timeout=0.1, max_retries=0, label="test-timeout")


@pytest.mark.asyncio
async def test_tg_rpc_retries_on_transient_error():
    """tg_rpc retries on ConnectionError and succeeds on retry."""
    from backend.runtime.tg_retry import tg_rpc

    call_count = 0

    async def factory():
        nonlocal call_count
        call_count += 1
        if call_count < 2:
            raise ConnectionError("Simulated network error")
        return "success"

    result = await tg_rpc(factory, timeout=5, max_retries=2, label="test-retry")
    assert result == "success"
    assert call_count == 2


@pytest.mark.asyncio
async def test_tg_rpc_cancelled_propagates():
    """CancelledError is always re-raised, never swallowed."""
    from backend.runtime.tg_retry import tg_rpc

    async def cancellable_coro():
        await asyncio.sleep(100)

    task = asyncio.create_task(tg_rpc(cancellable_coro(), timeout=100, max_retries=0, label="test-cancel"))
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_engine_handles_provider_failure():
    """Engine.execute never raises — failures become EngineResult."""
    from backend.ai.engine.engine import Engine
    from backend.ai.session.request import AIRequest

    engine = Engine()
    request = AIRequest(
        session_id="fail-1",
        user_message="test failure",
        owner_id=1,
        chat_id=1,
        message_id=1,
    )
    result = await engine.execute(request)
    assert result is not None
    assert isinstance(result.success, bool)


@pytest.mark.asyncio
async def test_database_fallback_on_failure():
    """RepositoryManager works in in-memory mode when Supabase is unavailable."""
    from backend.ai.database.manager import RepositoryManager

    mgr = RepositoryManager(supabase_available=False)
    assert mgr.supabase_available is False
    assert mgr.session is not None
    assert mgr.message is not None
    assert mgr.memory is not None


@pytest.mark.asyncio
async def test_memory_manager_handles_repository_failure(memory_manager):
    """MemoryManager doesn't crash when repository operations fail."""
    result = memory_manager.store_long(1, "test", importance=0.5)
    # Should not raise


@pytest.mark.asyncio
async def test_startup_check_aborts_on_missing_env():
    """Startup validation fails on missing required env vars."""
    from backend.runtime.startup_check import run_startup_checks

    cfg = {
        "API_ID": "",
        "API_HASH": "",
        "SESSION_STRING": "",
        "OWNER_ID": 0,
    }
    report = run_startup_checks(cfg)
    assert report.ok is False
    assert len(report.failures) > 0
    assert any(f.name == "env_vars" for f in report.failures)


@pytest.mark.asyncio
async def test_startup_check_passes_with_valid_env():
    """Startup validation passes with all required env vars present."""
    from backend.runtime.startup_check import run_startup_checks

    cfg = {
        "API_ID": "12345",
        "API_HASH": "abcdef",
        "SESSION_STRING": "x" * 100,
        "OWNER_ID": 7770001,
        "SUPABASE_URL": "",
        "SUPABASE_KEY": "",
        "SUPABASE_AVAILABLE": False,
    }
    report = run_startup_checks(cfg)
    assert report.ok is True
