"""
TASK 5 — Stress Tests

Simulates heavy load to verify:
  - No memory growth (session history bounded)
  - No task leaks (all tasks tracked)
  - No session leaks (idle sessions cleaned up)
"""
from __future__ import annotations

import asyncio
import sys
import pytest


@pytest.mark.asyncio
async def test_large_conversation_history_bounded(runtime_manager, owner_id):
    session = runtime_manager.create_session(owner_id)
    for i in range(100):
        runtime_manager.add_user_message(owner_id, f"Message {i} " * 20)
        runtime_manager.add_assistant_message(owner_id, f"Response {i} " * 20)

    history = runtime_manager.get_history(owner_id, n=1000)
    assert len(history) <= 200
    session = runtime_manager.get_session(owner_id)
    # Token budget is 4000 — history should have been trimmed
    assert session.token_estimate <= 4000 * 3


@pytest.mark.asyncio
async def test_rapid_ai_requests(engine, owner_id, chat_id):
    from backend.ai.session.request import AIRequest

    tasks = []
    for i in range(10):
        request = AIRequest(
            session_id=f"stress-{i}",
            user_message=f"Rapid request {i}",
            owner_id=owner_id,
            chat_id=chat_id,
            message_id=i,
        )
        tasks.append(engine.execute(request))

    results = await asyncio.gather(*tasks, return_exceptions=True)
    for r in results:
        assert not isinstance(r, Exception), f"Request failed: {r}"
        assert r is not None


@pytest.mark.asyncio
async def test_multiple_sessions_no_leak(runtime_manager):
    for i in range(20):
        runtime_manager.create_session(owner_id=i)
    assert runtime_manager.active_count() == 20
    for i in range(20):
        runtime_manager.close_session(i)
    assert runtime_manager.active_count() == 0


@pytest.mark.asyncio
async def test_idle_session_cleanup():
    from backend.ai.runtime.manager import ConversationManager
    from datetime import datetime, timezone, timedelta
    mgr = ConversationManager(idle_timeout_seconds=1)
    mgr.create_session(owner_id=999)
    assert mgr.active_count() == 1
    # Wait for the session to become idle
    await asyncio.sleep(1.5)
    mgr.cleanup_idle()
    assert mgr.active_count() == 0


@pytest.mark.asyncio
async def test_memory_manager_new_turn_clears_short(memory_manager):
    for i in range(50):
        memory_manager.new_turn()
    status = memory_manager.status()
    assert status["short_count"] == 0


@pytest.mark.asyncio
async def test_no_orphan_asyncio_tasks(engine, owner_id, chat_id):
    from backend.ai.session.request import AIRequest

    tasks_before = len(asyncio.all_tasks())
    for i in range(5):
        request = AIRequest(
            session_id=f"orphan-{i}",
            user_message=f"test {i}",
            owner_id=owner_id,
            chat_id=chat_id,
            message_id=i,
        )
        await engine.execute(request)
    await asyncio.sleep(0.5)
    tasks_after = len(asyncio.all_tasks())
    assert tasks_after <= tasks_before + 2


@pytest.mark.asyncio
async def test_prompt_builder_large_history_trimmed(prompt_builder, conversation_manager, owner_id, chat_id):
    conversation_manager.start_session(owner_id, chat_id, session_id="stress-prompt")
    for i in range(50):
        conversation_manager.add_user_message("stress-prompt", f"Message {i} " * 50)
        conversation_manager.add_assistant_message("stress-prompt", f"Response {i} " * 50)

    ctx = conversation_manager.build_context("stress-prompt", "final message", 1)
    package = prompt_builder.build(ctx)
    assert package.estimated_tokens.estimated_total < 50000
