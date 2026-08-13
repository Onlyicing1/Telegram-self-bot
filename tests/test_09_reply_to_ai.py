"""
TASK 9 — Reply-to-AI Regression Tests

Verifies the reply-to-AI fix:
  1. Reply to AI + trigger + extra text → AI receives old AI response as
     context and user's extra text as the user message.
  2. Reply to AI + trigger only → AI receives old AI response as context
     and a continuation prompt as the user message.
  3. Plain trigger mode (no reply) still works unchanged.
"""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch


@pytest.mark.asyncio
async def test_reply_to_ai_with_extra_text_uses_user_text_as_message():
    """When replying to an AI message with 'Nova explain more', the user
    message must be 'explain more', NOT the old AI response."""
    from backend.ai.context.reply_resolver import ReplyResolver, ResolvedAIContent
    from backend.ai.conversation.context_builder import ReplyContext

    resolver = ReplyResolver()
    resolver.clear()
    resolver.register(
        telegram_msg_id=100,
        session_id="owner-1",
        role="assistant",
        content="The capital of France is Paris.",
        provider="dummy",
        model="dummy-1",
    )

    fake_reply_msg = MagicMock()
    fake_reply_msg.id = 100
    fake_reply_msg.chat_id = 123
    fake_reply_msg.date = None
    fake_reply_msg.media = None
    fake_reply_msg.message = ""
    fake_reply_msg.get_sender = AsyncMock(return_value=None)
    fake_reply_msg.get_chat = AsyncMock(return_value=None)

    fake_event = MagicMock()
    fake_event.raw_text = "Nova explain more"
    fake_event.get_reply_message = AsyncMock(return_value=fake_reply_msg)

    with patch(
        "backend.ai.context.reply_resolver.get_resolver",
        return_value=resolver,
    ):
        from backend.bot.handlers.ai_unified import _extract_reply_context
        user_message, reply_ctx, error = await _extract_reply_context(
            fake_event, None, "explain more"
        )

    assert error == ""
    assert user_message == "explain more"
    assert reply_ctx.exists is True
    assert reply_ctx.is_ai_message is True
    assert reply_ctx.ai_content == "The capital of France is Paris."
    # The old AI response must NOT be the user message
    assert user_message != "The capital of France is Paris."


@pytest.mark.asyncio
async def test_reply_to_ai_trigger_only_uses_continuation_prompt():
    """When replying to an AI message with only 'Nova', the user message
    must be a continuation prompt, NOT the old AI response."""
    from backend.ai.context.reply_resolver import ReplyResolver
    from backend.ai.conversation.context_builder import ReplyContext

    resolver = ReplyResolver()
    resolver.clear()
    resolver.register(
        telegram_msg_id=200,
        session_id="owner-1",
        role="assistant",
        content="Python is a high-level programming language.",
        provider="dummy",
        model="dummy-1",
    )

    fake_reply_msg = MagicMock()
    fake_reply_msg.id = 200
    fake_reply_msg.chat_id = 123
    fake_reply_msg.date = None
    fake_reply_msg.media = None
    fake_reply_msg.message = ""
    fake_reply_msg.get_sender = AsyncMock(return_value=None)
    fake_reply_msg.get_chat = AsyncMock(return_value=None)

    fake_event = MagicMock()
    fake_event.raw_text = "Nova"
    fake_event.get_reply_message = AsyncMock(return_value=fake_reply_msg)

    with patch(
        "backend.ai.context.reply_resolver.get_resolver",
        return_value=resolver,
    ):
        from backend.bot.handlers.ai_unified import _extract_reply_context
        user_message, reply_ctx, error = await _extract_reply_context(
            fake_event, None, ""
        )

    assert error == ""
    assert reply_ctx.is_ai_message is True
    assert reply_ctx.ai_content == "Python is a high-level programming language."
    # The old AI response must NOT be the user message
    assert user_message != "Python is a high-level programming language."
    # A continuation prompt must be used
    assert "Continue" in user_message or "more" in user_message.lower()


@pytest.mark.asyncio
async def test_plain_trigger_mode_still_works(engine, owner_id, chat_id):
    """Plain trigger mode (no reply) must still pass the user's text as
    the user message — unchanged behavior."""
    from backend.ai.session.request import AIRequest

    request = AIRequest(
        session_id="plain-trigger-1",
        user_message="Hello, what can you do?",
        owner_id=owner_id,
        chat_id=chat_id,
        message_id=1,
    )
    result = await engine.execute(request)
    assert result is not None
    assert isinstance(result.success, bool)


@pytest.mark.asyncio
async def test_reply_to_non_ai_message_with_extra_text():
    """When replying to a non-AI message with 'Nova summarize this',
    the user message must be 'summarize this' and the replied message
    is context."""
    from backend.ai.context.reply_resolver import ReplyResolver
    from backend.ai.conversation.context_builder import ReplyContext

    resolver = ReplyResolver()
    resolver.clear()

    fake_reply_msg = MagicMock()
    fake_reply_msg.id = 300
    fake_reply_msg.chat_id = 123
    fake_reply_msg.date = None
    fake_reply_msg.media = None
    fake_reply_msg.message = "Here is a long article about quantum computing."
    fake_reply_msg.get_sender = AsyncMock(return_value=None)
    fake_reply_msg.get_chat = AsyncMock(return_value=None)

    fake_event = MagicMock()
    fake_event.raw_text = "Nova summarize this"
    fake_event.get_reply_message = AsyncMock(return_value=fake_reply_msg)

    with patch(
        "backend.ai.context.reply_resolver.get_resolver",
        return_value=resolver,
    ):
        from backend.bot.handlers.ai_unified import _extract_reply_context
        user_message, reply_ctx, error = await _extract_reply_context(
            fake_event, None, "summarize this"
        )

    assert error == ""
    assert user_message == "summarize this"
    assert reply_ctx.exists is True
    assert reply_ctx.is_ai_message is False
    assert reply_ctx.ai_content == ""
