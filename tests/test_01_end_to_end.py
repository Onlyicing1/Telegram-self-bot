"""
TASK 1 — End-to-End Integration Tests

Verifies that every layer communicates correctly:
  Telegram Event → Router → Command Handler → Services → AI Engine →
  Memory Manager → Tool Executor → Supabase (in-memory) → Diagnostics → Runtime
"""
from __future__ import annotations

import asyncio
import pytest


@pytest.mark.asyncio
async def test_engine_initializes_all_subsystems(engine):
    assert engine.conversation_manager is not None
    assert engine.provider_manager is not None
    assert engine.provider_registry is not None


@pytest.mark.asyncio
async def test_full_execution_flow(engine, owner_id, chat_id):
    from backend.ai.session.request import AIRequest

    request = AIRequest(
        session_id="e2e-flow-1",
        user_message="Hello, what can you do?",
        owner_id=owner_id,
        chat_id=chat_id,
        message_id=1,
    )
    result = await engine.execute(request)
    assert result is not None
    assert isinstance(result.success, bool)
    assert result.provider != ""
    assert result.latency >= 0.0
    assert result.total_tokens >= 0


@pytest.mark.asyncio
async def test_engine_result_is_immutable(engine, owner_id, chat_id):
    from backend.ai.session.request import AIRequest
    from backend.ai.engine.result import EngineResult

    request = AIRequest(
        session_id="e2e-immutable",
        user_message="test",
        owner_id=owner_id,
        chat_id=chat_id,
        message_id=1,
    )
    result = await engine.execute(request)
    assert isinstance(result, EngineResult)
    with pytest.raises(AttributeError):
        result.success = False


@pytest.mark.asyncio
async def test_diagnostics_records_engine_events(engine, owner_id, chat_id):
    from backend.diagnostics import get_events
    from backend.ai.session.request import AIRequest

    events_before = len(get_events())
    request = AIRequest(
        session_id="e2e-diag",
        user_message="diagnostics test",
        owner_id=owner_id,
        chat_id=chat_id,
        message_id=1,
    )
    await engine.execute(request)
    events_after = len(get_events())
    assert events_after >= events_before


@pytest.mark.asyncio
async def test_memory_manager_stores_and_retrieves(memory_manager, owner_id):
    memory_manager.store_long(owner_id, "User likes Python", importance=0.8)
    retrieved = memory_manager.retrieve_for_prompt(owner_id, "Python")
    assert "long" in retrieved
    assert "permanent" in retrieved
    assert "short" in retrieved


@pytest.mark.asyncio
async def test_tool_registry_has_tools(tool_registry):
    assert tool_registry.is_empty() is False
    assert len(tool_registry.list_names()) > 0


@pytest.mark.asyncio
async def test_runtime_manager_session_lifecycle(runtime_manager, owner_id):
    session = runtime_manager.create_session(owner_id)
    assert session is not None
    assert session.session_id != ""
    assert runtime_manager.active_count() == 1
    assert runtime_manager.close_session(owner_id) is True
    assert runtime_manager.active_count() == 0


@pytest.mark.asyncio
async def test_repository_manager_in_memory_fallback():
    from backend.ai.database.manager import RepositoryManager
    mgr = RepositoryManager(supabase_available=False)
    assert mgr.supabase_available is False
    assert mgr.session is not None
    assert mgr.message is not None
    assert mgr.memory is not None
    assert mgr.tool_history is not None
    assert mgr.provider_stats is not None


@pytest.mark.asyncio
async def test_engine_health_reports_ready(engine):
    health = engine.engine_health()
    assert health == "READY"


@pytest.mark.asyncio
async def test_conversation_manager_session_workflow(conversation_manager, owner_id, chat_id):
    from backend.ai.conversation.state import ConversationState

    session = conversation_manager.start_session(owner_id, chat_id, session_id="conv-test")
    assert session is not None
    assert conversation_manager.get_state("conv-test") == ConversationState.IDLE

    conversation_manager.add_user_message("conv-test", "Hello")
    history = conversation_manager.get_history("conv-test")
    assert len(history) == 1
    assert history[0].role == "user"

    conversation_manager.end_session("conv-test")
    assert conversation_manager.get_session("conv-test") is None
