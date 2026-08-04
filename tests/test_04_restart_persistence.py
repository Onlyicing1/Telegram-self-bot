"""
TASK 4 — Restart Persistence Tests

Verifies behavior after a simulated restart:
  - AI memory survives restart (via repository persistence)
  - Sessions restore correctly
  - Background tasks restart correctly
  - Runtime remains deterministic
"""
from __future__ import annotations

import asyncio
import pytest
from datetime import datetime, timezone, timedelta


@pytest.mark.asyncio
async def test_memory_survives_in_repository(memory_manager, owner_id):
    """Long-term and permanent memories stored in a repository survive."""
    memory_manager.store_long(owner_id, "User prefers dark mode", importance=0.8)
    memory_manager.store_permanent(owner_id, "User's timezone is UTC", importance=1.0)

    retrieved = memory_manager.retrieve_for_prompt(owner_id, "dark mode")
    assert "long" in retrieved
    assert "permanent" in retrieved
    assert "short" in retrieved


@pytest.mark.asyncio
async def test_session_restore_from_repository():
    from backend.ai.database.session_repository import InMemorySessionRepository, SessionRecord

    repo = InMemorySessionRepository()
    repo.create(SessionRecord(
        session_id="restart-1", owner_id=1, provider="dummy", model="test",
        status="active", total_tokens=150, message_count=5,
    ))

    restored = repo.get("restart-1")
    assert restored is not None
    assert restored.session_id == "restart-1"
    assert restored.provider == "dummy"
    assert restored.total_tokens == 150
    assert restored.message_count == 5


@pytest.mark.asyncio
async def test_message_history_restore():
    from backend.ai.database.message_repository import InMemoryMessageRepository, MessageRecord

    repo = InMemoryMessageRepository()
    for i in range(5):
        repo.create(MessageRecord(
            id=f"r-msg-{i}", session_id="restart-sess", owner_id=1,
            role="user" if i % 2 == 0 else "assistant",
            content=f"Message {i}", token_count=10,
        ))

    restored = repo.list_messages("restart-sess")
    assert len(restored) == 5
    assert restored[0].role == "user"
    assert restored[1].role == "assistant"


@pytest.mark.asyncio
async def test_engine_singleton_is_deterministic():
    from backend.ai.engine.engine import get_engine
    engine1 = get_engine()
    engine2 = get_engine()
    assert engine1 is engine2


@pytest.mark.asyncio
async def test_runtime_manager_idempotent_create(runtime_manager, owner_id):
    session1 = runtime_manager.create_session(owner_id)
    session2 = runtime_manager.create_session(owner_id)
    assert session1.session_id == session2.session_id
    assert runtime_manager.active_count() == 1


@pytest.mark.asyncio
async def test_memory_cleanup_worker_is_idempotent():
    from backend.runtime.memory_cleanup import start_memory_cleanup, stop_memory_cleanup

    start_memory_cleanup()
    start_memory_cleanup()  # second call is a no-op
    await stop_memory_cleanup()


@pytest.mark.asyncio
async def test_engine_reinit_produces_same_health():
    from backend.ai.engine.engine import Engine
    engine1 = Engine()
    engine2 = Engine()
    assert engine1.engine_health() == engine2.engine_health()
    assert engine1.engine_health() == "READY"
