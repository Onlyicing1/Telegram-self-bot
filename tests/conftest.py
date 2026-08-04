"""
Pytest configuration and shared fixtures for the LifeOS integration test suite.

Provides reusable fixtures that construct in-memory (no-network) instances
of every subsystem, so tests run deterministically without Supabase,
Telegram, or external AI providers.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from datetime import datetime, timezone

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture
def owner_id() -> int:
    return 7770001


@pytest.fixture
def chat_id() -> int:
    return -1001234567890


@pytest.fixture
def session_id(owner_id: int) -> str:
    return f"test-session-{owner_id}"


@pytest.fixture
def now() -> datetime:
    return datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def memory_manager():
    from backend.ai.memory.manager import MemoryManager
    return MemoryManager()


@pytest.fixture
def conversation_manager():
    from backend.ai.conversation.conversation import ConversationManager
    return ConversationManager()


@pytest.fixture
def runtime_manager():
    from backend.ai.runtime.manager import ConversationManager as RuntimeCM
    return RuntimeCM(idle_timeout_seconds=60, token_budget=4000)


@pytest.fixture
def prompt_builder():
    from backend.ai.prompt.builder import PromptBuilder
    return PromptBuilder()


@pytest.fixture
def provider_manager():
    from backend.ai.providers.manager.manager import ProviderManager
    return ProviderManager()


@pytest.fixture
def tool_registry(owner_id):
    from backend.ai.tools.registry import create_default_registry
    from backend.ai.tools.context import ToolContext
    ctx = ToolContext(telegram=None, owner_id=owner_id, tz_str="UTC")
    return create_default_registry(ctx)


@pytest.fixture
def engine():
    from backend.ai.engine.engine import Engine
    return Engine()
