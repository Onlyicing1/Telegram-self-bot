"""
TASK 37 — Memory wiring + bounds regression coverage.

Verifies that the existing three-tier memory system is actually consumed by
the normal AI execution path and that every bound (records, prompt tokens,
entry size, latency, duplicates, ordering, owner isolation) is enforced
without breaking AI execution when memory is unavailable or failing.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import pytest

from backend.ai.database.memory_repository import (
    InMemoryMemoryRepository,
    MemoryRepository,
)
from backend.ai.providers.manager.manager import ProviderManager
from backend.ai.memory.manager import MemoryManager
from backend.ai.memory.limits import (
    MAX_LONG_RECORDS,
    MAX_MEMORY_ENTRY_CHARS,
    MAX_MEMORY_PROMPT_TOKENS,
    MAX_PERMANENT_RECORDS,
)
from backend.ai.memory.types import (
    MemoryCategory,
    MemoryEntry,
    MemoryQuery,
    MemoryTier,
)
from backend.ai.prompt.budget import estimate_tokens
from backend.ai.prompt.template import PromptSection


def _entry(
    owner_id: int,
    content: str,
    tier: MemoryTier = MemoryTier.LONG,
    importance: float = 0.5,
    created_at: datetime | None = None,
    entry_id: str | None = None,
) -> MemoryEntry:
    return MemoryEntry(
        id=entry_id or f"{owner_id}-{abs(hash(content))}",
        owner_id=owner_id,
        tier=tier,
        category=MemoryCategory.FACT if tier != MemoryTier.SHORT else MemoryCategory.CONTEXT,
        content=content,
        importance=importance,
        created_at=created_at or datetime(2026, 1, 1, tzinfo=timezone.utc),
    )


class RecordingRepo(MemoryRepository):
    """Stub repository that records call counts and returns seeded entries."""

    def __init__(self, entries: list[MemoryEntry] | None = None) -> None:
        self._entries = list(entries or [])
        self.query_calls = 0
        self.saved: list[MemoryEntry] = []
        self.fail_query = False

    def save(self, entry: MemoryEntry) -> bool:
        self.saved.append(entry)
        return True

    def query(self, query: MemoryQuery) -> list[MemoryEntry]:
        self.query_calls += 1
        if self.fail_query:
            raise RuntimeError("db down")
        results = [e for e in self._entries if e.owner_id == query.owner_id]
        if query.tier:
            results = [e for e in results if e.tier == query.tier]
        if query.category:
            results = [e for e in results if e.category == query.category]
        if query.query_text:
            results = [e for e in results if query.query_text.lower() in e.content.lower()]
        results.sort(key=lambda e: (e.importance, e.created_at, e.id), reverse=True)
        return results[: query.limit]

    def delete(self, entry_id: str) -> bool:
        return False

    def delete_expired(self, tier: MemoryTier) -> int:
        return 0

    def count(self, owner_id: int, tier: MemoryTier) -> int:
        return sum(1 for e in self._entries if e.owner_id == owner_id and e.tier == tier)


class RaisingRepo(MemoryRepository):
    """Repository whose operations always raise — used for failure tests."""

    def save(self, entry: MemoryEntry) -> bool:
        raise RuntimeError("db down")

    def query(self, query: MemoryQuery) -> list[MemoryEntry]:
        raise RuntimeError("db down")

    def delete(self, entry_id: str) -> bool:
        raise RuntimeError("db down")

    def delete_expired(self, tier: MemoryTier) -> int:
        raise RuntimeError("db down")

    def count(self, owner_id: int, tier: MemoryTier) -> int:
        raise RuntimeError("db down")


class SlowRepo(RecordingRepo):
    """Repository whose queries block longer than the dispatcher timeout."""

    def query(self, query: MemoryQuery) -> list[MemoryEntry]:
        time.sleep(2.5)
        return super().query(query)


class SuccessStubProvider(ProviderManager):
    """ProviderManager stand-in that always returns a successful response.

    The real DummyProvider deliberately never fakes success, so tests that
    need a successful execution use this stub instead.
    """

    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    def get_active_name(self) -> str:
        return "dummy"

    async def chat(self, messages: list, **kwargs):
        from backend.ai.providers.base import ProviderResponse

        self.calls += 1
        return ProviderResponse(
            text="ok", provider_name="dummy", success=True,
            usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        )


class RetryStubProvider(ProviderManager):
    """ProviderManager stand-in that returns an empty response once, then text.

    Drives the dispatcher's bounded empty-response retry without network.
    """

    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    def get_active_name(self) -> str:
        return "dummy"

    async def chat(self, messages: list, **kwargs):
        from backend.ai.providers.base import ProviderResponse

        self.calls += 1
        if self.calls == 1:
            return ProviderResponse(
                text="", provider_name="dummy", success=True,
                metadata={"finish_reason": "stop"},
            )
        return ProviderResponse(
            text="ok", provider_name="dummy", success=True,
            usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        )


def _request(owner_id: int = 1):
    from backend.ai.session.request import AIRequest

    return AIRequest(
        session_id=f"mem-test-{owner_id}",
        user_message="hello",
        owner_id=owner_id,
        chat_id=1,
        message_id=1,
    )


@pytest.mark.asyncio
async def test_engine_wires_memory_repositories():
    """Requirement 1: the default Engine wires memory repositories in."""
    from backend.ai.engine.engine import Engine

    engine = Engine()
    status = engine.memory_manager.status()
    assert status["long_available"] is True
    assert status["permanent_available"] is True


@pytest.mark.asyncio
async def test_dispatcher_consumes_memory_during_execution():
    """Requirement 1/2: execution queries memory once per tier through the manager."""
    from backend.ai.engine.engine import Engine

    repo = RecordingRepo(entries=[
        _entry(1, "User prefers dark mode", importance=0.9),
        _entry(1, "User likes Python", importance=0.5),
    ])
    engine = Engine(
        providers=SuccessStubProvider(),
        memory_manager=MemoryManager(
            long_repository=repo, permanent_repository=repo,
        ),
    )
    result = await engine.execute(_request())
    assert result.success is True
    # Long + permanent tiers each queried exactly once per logical request.
    assert repo.query_calls == 2
    # No writes are performed by normal execution.
    assert repo.saved == []


@pytest.mark.asyncio
async def test_existing_memory_reaches_prompt_memory_section():
    """Requirement 2: stored memory populates the [Memory] prompt section."""
    from backend.ai.conversation.context_builder import (
        ConversationContext,
        ReplyContext,
        RuntimeContext,
        SettingsContext,
        ToolContext,
    )
    from backend.ai.conversation.state import ConversationState
    from backend.ai.prompt.builder import PromptBuilder

    repo = InMemoryMemoryRepository()
    repo.save(_entry(1, "User's name is TestUser", tier=MemoryTier.PERMANENT, importance=1.0))
    repo.save(_entry(1, "User prefers concise answers", importance=0.9))
    mgr = MemoryManager(long_repository=repo, permanent_repository=repo)
    memory_data = mgr.retrieve_for_prompt(1)

    ctx = ConversationContext(
        session_id="s1", owner_id=1, chat_id=1, message_id=1,
        state=ConversationState.IDLE, current_menu="main", current_panel="",
        current_category="", current_flow="", pending_action="",
        language="English", timezone="UTC", current_time="2026-01-01 12:00",
        user_text="hello", reply=ReplyContext(), tool=ToolContext(),
        settings=SettingsContext(), runtime=RuntimeContext(), history=[],
        memory=memory_data,
    )
    package = PromptBuilder().build(ctx)
    memory_section = package.sections.get(PromptSection.MEMORY, "")
    assert "[Memory]" in memory_section
    assert "[Permanent Facts]" in memory_section
    assert "TestUser" in memory_section
    assert "concise" in memory_section


@pytest.mark.asyncio
async def test_no_memory_produces_clean_empty_section():
    """Requirement 3: no memory → empty blocks and an empty rendered section."""
    from backend.ai.prompt.builder import PromptBuilder
    from types import SimpleNamespace

    mgr = MemoryManager(
        long_repository=InMemoryMemoryRepository(),
        permanent_repository=InMemoryMemoryRepository(),
    )
    memory_data = mgr.retrieve_for_prompt(1)
    assert memory_data["permanent"] == ""
    assert memory_data["long"] == ""
    assert memory_data["short"] == ""

    rendered = PromptBuilder()._render_memory(SimpleNamespace(memory=memory_data))
    assert rendered == ""


@pytest.mark.asyncio
async def test_memory_is_owner_scoped():
    """Requirement 4: memory lookup is scoped to the owner."""
    repo = InMemoryMemoryRepository()
    repo.save(_entry(1, "Owner one secret fact", importance=0.9))
    repo.save(_entry(2, "Owner two private fact", importance=0.9))
    mgr = MemoryManager(long_repository=repo, permanent_repository=repo)

    owner_one = mgr.retrieve_for_prompt(1)
    owner_two = mgr.retrieve_for_prompt(2)
    assert "Owner one secret fact" in owner_one["long"]
    assert "Owner one secret fact" not in owner_two["long"]
    assert "Owner two private fact" in owner_two["long"]


@pytest.mark.asyncio
async def test_memory_retrieval_is_bounded():
    """Requirement 5: at most MAX_LONG_RECORDS entries are retrieved."""
    repo = InMemoryMemoryRepository()
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for i in range(25):
        repo.save(_entry(
            1, f"fact number {i}", importance=0.5 + (i % 5) * 0.1,
            created_at=now + timedelta(minutes=i),
        ))
    mgr = MemoryManager(long_repository=repo, permanent_repository=repo)
    memory_data = mgr.retrieve_for_prompt(1)
    lines = [l for l in memory_data["long"].splitlines() if l.startswith("  - ")]
    assert len(lines) <= MAX_LONG_RECORDS

    results = repo.query(MemoryQuery(
        owner_id=1, tier=MemoryTier.LONG, limit=MAX_LONG_RECORDS,
    ))
    assert len(results) == MAX_LONG_RECORDS


@pytest.mark.asyncio
async def test_prompt_memory_content_is_token_bounded():
    """Requirement 6: the rendered memory section never exceeds the budget."""
    repo = InMemoryMemoryRepository()
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for i in range(30):
        repo.save(_entry(
            1, "x" * 400, tier=MemoryTier.PERMANENT, importance=1.0,
            created_at=now + timedelta(minutes=i),
        ))
    mgr = MemoryManager(long_repository=repo, permanent_repository=repo)
    memory_data = mgr.retrieve_for_prompt(1)
    for block in ("permanent", "long", "short"):
        assert estimate_tokens(memory_data[block]) <= MAX_MEMORY_PROMPT_TOKENS


@pytest.mark.asyncio
async def test_oversized_memory_is_deterministically_reduced():
    """Requirement 7: oversized memory is reduced by deterministic ranking."""
    repo = InMemoryMemoryRepository()
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for i in range(30):
        repo.save(_entry(
            1, f"chunk-{i} " + "y" * 300, tier=MemoryTier.PERMANENT,
            importance=1.0, created_at=now + timedelta(minutes=i),
        ))
    mgr = MemoryManager(long_repository=repo, permanent_repository=repo)
    memory_data = mgr.retrieve_for_prompt(1)

    assert "chunk-29" in memory_data["permanent"]  # newest ranked first
    assert "chunk-0" not in memory_data["permanent"]  # oldest dropped
    lines = [l for l in memory_data["permanent"].splitlines() if l.startswith("  - ")]
    assert len(lines) < 30


@pytest.mark.asyncio
async def test_memory_ordering_is_deterministic():
    """Requirement 8: repeated retrievals return identical, ranked content."""
    repo = InMemoryMemoryRepository()
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for i in range(6):
        repo.save(_entry(
            1, f"entry {i}", importance=float(i) / 10,
            created_at=now + timedelta(minutes=i),
        ))
    mgr = MemoryManager(long_repository=repo, permanent_repository=repo)
    first = mgr.retrieve_for_prompt(1)
    second = mgr.retrieve_for_prompt(1)
    assert first["long"] == second["long"]
    assert first["long"] != ""

    results = repo.query(MemoryQuery(owner_id=1, tier=MemoryTier.LONG, limit=10))
    importances = [e.importance for e in results]
    assert importances == sorted(importances, reverse=True)


@pytest.mark.asyncio
async def test_duplicate_memory_writes_are_prevented():
    """Requirement 9: identical writes never create duplicate entries."""
    repo = InMemoryMemoryRepository()
    entry = _entry(1, "same fact", importance=0.8)
    assert repo.save(entry) is True
    assert repo.save(entry) is True
    assert repo.count(1, MemoryTier.LONG) == 1

    mgr = MemoryManager(long_repository=repo, permanent_repository=repo)
    mgr.store_long(1, "same fact", importance=0.8)
    assert repo.count(1, MemoryTier.LONG) == 1


@pytest.mark.asyncio
async def test_oversized_write_is_rejected():
    """Requirement 11: writes over the entry cap are rejected, not truncated."""
    repo = InMemoryMemoryRepository()
    huge = _entry(1, "z" * (MAX_MEMORY_ENTRY_CHARS + 10))
    assert repo.save(huge) is False
    assert repo.count(1, MemoryTier.LONG) == 0


@pytest.mark.asyncio
async def test_memory_failure_does_not_break_execution():
    """Requirement 10: a failing memory store degrades, never crashes the request."""
    from backend.ai.engine.engine import Engine

    engine = Engine(
        providers=SuccessStubProvider(),
        memory_manager=MemoryManager(
            long_repository=RaisingRepo(), permanent_repository=RaisingRepo(),
        ),
    )
    result = await engine.execute(_request())
    assert result.success is True


@pytest.mark.asyncio
async def test_memory_write_failure_is_observable():
    """Requirement 11: write failure surfaces as None + warning, never silence."""
    mgr = MemoryManager(
        long_repository=RaisingRepo(), permanent_repository=RaisingRepo(),
    )
    assert mgr.store_long(1, "will fail", importance=0.5) is None
    assert mgr.store_permanent(1, "will fail", importance=1.0) is None


@pytest.mark.asyncio
async def test_provider_retry_does_not_multiply_memory_activity():
    """Requirement 12: provider retries do not re-query or write memory."""
    from backend.ai.engine.engine import Engine

    repo = RecordingRepo(entries=[
        _entry(1, "User prefers concise answers", importance=0.9),
    ])
    engine = Engine(
        providers=RetryStubProvider(),
        memory_manager=MemoryManager(
            long_repository=repo, permanent_repository=repo,
        ),
    )
    result = await engine.execute(_request())
    assert result.success is True
    assert repo.saved == []                      # no writes at all
    assert repo.query_calls == 2                 # long + permanent, exactly once


@pytest.mark.asyncio
async def test_memory_read_timeout_degrades_gracefully():
    """Requirement (latency bound): a hanging store never stalls the request."""
    from backend.ai.engine.engine import Engine

    repo = SlowRepo(entries=[_entry(1, "slow fact", importance=0.9)])
    engine = Engine(
        providers=SuccessStubProvider(),
        memory_manager=MemoryManager(
            long_repository=repo, permanent_repository=repo,
        ),
    )
    started = time.monotonic()
    result = await engine.execute(_request())
    elapsed = time.monotonic() - started
    assert result.success is True
    # The dispatcher caps memory retrieval at MEMORY_READ_TIMEOUT_S (2s);
    # the request itself must still complete promptly.
    assert elapsed < 2.5


@pytest.mark.asyncio
async def test_token_budget_respected_with_memory():
    """Requirement 13: the full prompt budget stays within limits with memory."""
    from backend.ai.conversation.context_builder import (
        ConversationContext,
        ReplyContext,
        RuntimeContext,
        SettingsContext,
        ToolContext,
    )
    from backend.ai.conversation.state import ConversationState
    from backend.ai.prompt.builder import PromptBuilder

    repo = InMemoryMemoryRepository()
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for i in range(20):
        repo.save(_entry(
            1, "w" * 200, tier=MemoryTier.PERMANENT, importance=1.0,
            created_at=now + timedelta(minutes=i),
        ))
    mgr = MemoryManager(long_repository=repo, permanent_repository=repo)
    ctx = ConversationContext(
        session_id="s1", owner_id=1, chat_id=1, message_id=1,
        state=ConversationState.IDLE, current_menu="main", current_panel="",
        current_category="", current_flow="", pending_action="",
        language="English", timezone="UTC", current_time="2026-01-01 12:00",
        user_text="hello", reply=ReplyContext(), tool=ToolContext(),
        settings=SettingsContext(), runtime=RuntimeContext(), history=[],
        memory=mgr.retrieve_for_prompt(1),
    )
    package = PromptBuilder().build(ctx)
    assert package.estimated_tokens.within_budget is True
    memory_section = package.sections.get(PromptSection.MEMORY, "")
    assert estimate_tokens(memory_section) <= MAX_MEMORY_PROMPT_TOKENS


@pytest.mark.asyncio
async def test_ai_behavior_unchanged_when_memory_unavailable():
    """Requirement 14: without any memory, AI execution behaves exactly as before."""
    from backend.ai.engine.engine import Engine

    engine = Engine(providers=SuccessStubProvider())  # in-memory repos, no entries
    result = await engine.execute(_request())
    assert result.success is True
