"""
TASK 2 — AI Flow Tests

Tests the complete AI execution pipeline:
  User Message → Conversation Builder → Prompt Builder → Provider →
  Tool Execution (if needed) → Memory Update → Database Update → Telegram Response
"""
from __future__ import annotations

import asyncio
import pytest
from datetime import datetime, timezone


@pytest.mark.asyncio
async def test_ai_flow_user_message_to_provider(engine, owner_id, chat_id):
    from backend.ai.session.request import AIRequest

    request = AIRequest(
        session_id="flow-1",
        user_message="What is 2+2?",
        owner_id=owner_id,
        chat_id=chat_id,
        message_id=1,
    )
    result = await engine.execute(request)
    assert result is not None
    assert result.provider != ""
    assert result.latency >= 0.0


@pytest.mark.asyncio
async def test_ai_flow_prompt_builder_produces_package(prompt_builder, conversation_manager, owner_id, chat_id):
    conversation_manager.start_session(owner_id, chat_id, session_id="flow-prompt")
    conversation_manager.add_user_message("flow-prompt", "Hello AI")

    ctx = conversation_manager.build_context(
        session_id="flow-prompt",
        user_text="Hello AI",
        message_id=1,
    )
    package = prompt_builder.build(ctx)

    assert package.system_prompt != ""
    assert package.user_input != ""
    assert package.estimated_tokens is not None
    assert package.estimated_tokens.estimated_total > 0
    assert len(package.sections) > 0


@pytest.mark.asyncio
async def test_ai_flow_prompt_sections_in_deterministic_order(prompt_builder, conversation_manager, owner_id, chat_id):
    from backend.ai.prompt.template import SECTION_ORDER

    conversation_manager.start_session(owner_id, chat_id, session_id="flow-order")
    conversation_manager.add_user_message("flow-order", "test ordering")

    ctx = conversation_manager.build_context("flow-order", "test ordering", 1)
    package = prompt_builder.build(ctx)

    section_keys = list(package.sections.keys())
    for section in SECTION_ORDER:
        assert section in section_keys, f"Missing section: {section}"


@pytest.mark.asyncio
async def test_ai_flow_provider_returns_response(provider_manager):
    """Stage 4: Provider returns a response (DummyProvider)."""
    messages = [
        {"role": "system", "content": "You are a test assistant."},
        {"role": "user", "content": "Say hello"},
    ]
    response = await provider_manager.chat(messages)
    assert response is not None
    assert response.text != ""
    assert response.provider_name == "dummy"


@pytest.mark.asyncio
async def test_ai_flow_provider_stream(provider_manager):
    messages = [{"role": "user", "content": "stream test"}]
    chunks = list(provider_manager.stream(messages))
    assert len(chunks) > 0
    for chunk in chunks:
        assert chunk is not None


@pytest.mark.asyncio
async def test_ai_flow_memory_update(memory_manager, owner_id):
    memory_manager.store_long(owner_id, "User prefers concise answers", importance=0.9)
    memory_manager.store_permanent(owner_id, "User's name is TestUser", importance=1.0)

    retrieved = memory_manager.retrieve_for_prompt(owner_id, "concise")
    assert "long" in retrieved
    assert "permanent" in retrieved


@pytest.mark.asyncio
async def test_ai_flow_database_update_session_repo():
    from backend.ai.database.session_repository import InMemorySessionRepository, SessionRecord

    repo = InMemorySessionRepository()
    record = SessionRecord(
        session_id="db-test-1",
        owner_id=7770001,
        provider="dummy",
        model="test-model",
    )
    assert repo.create(record) is True
    retrieved = repo.get("db-test-1")
    assert retrieved is not None
    assert retrieved.owner_id == 7770001
    assert repo.update("db-test-1", {"status": "completed"}) is True
    updated = repo.get("db-test-1")
    assert updated.status == "completed"


@pytest.mark.asyncio
async def test_ai_flow_database_update_message_repo():
    from backend.ai.database.message_repository import InMemoryMessageRepository, MessageRecord

    repo = InMemoryMessageRepository()
    record = MessageRecord(
        id="msg-1",
        session_id="db-test-1",
        owner_id=7770001,
        role="user",
        content="Hello",
    )
    assert repo.create(record) is True
    messages = repo.list_messages("db-test-1")
    assert len(messages) == 1
    assert messages[0].content == "Hello"
    assert repo.count("db-test-1") == 1


@pytest.mark.asyncio
async def test_ai_flow_engine_result_has_response(engine, owner_id, chat_id):
    from backend.ai.session.request import AIRequest

    request = AIRequest(
        session_id="flow-response",
        user_message="Tell me a joke",
        owner_id=owner_id,
        chat_id=chat_id,
        message_id=1,
    )
    result = await engine.execute(request)
    assert result is not None


@pytest.mark.asyncio
async def test_ai_flow_consecutive_requests_same_session(engine, owner_id, chat_id):
    from backend.ai.session.request import AIRequest

    for i in range(3):
        request = AIRequest(
            session_id="flow-consecutive",
            user_message=f"Message number {i}",
            owner_id=owner_id,
            chat_id=chat_id,
            message_id=i + 1,
        )
        result = await engine.execute(request)
        assert result is not None
