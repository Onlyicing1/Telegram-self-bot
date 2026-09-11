"""
Memory tool regression coverage — RC: connecting the implemented memory
subsystem to the AI tool surface (INVESTIGATION.md §13.1).

Pins:
- Registration and schema/permission contract of memory_store/memory_list.
- memory_store writes through the engine-owned MemoryManager (single
  memory authority), owner-scoped, with tier/category/importance validation.
- Honest failure: repository-rejected and repository-raising writes report
  success=False (the tier stores' None contract).
- Bounded execution: a hanging store degrades to an honest failure inside
  MEMORY_WRITE_TIMEOUT_S.
- memory_list reads back owner-scoped entries with tier/query filters.
"""
from __future__ import annotations

import asyncio
import time

import pytest

from backend.ai.database.memory_repository import InMemoryMemoryRepository
from backend.ai.memory.limits import MAX_MEMORY_ENTRY_CHARS
from backend.ai.memory.manager import MemoryManager
from backend.ai.memory.types import MemoryCategory, MemoryTier
from backend.ai.tools.base import PermissionLevel
from backend.ai.tools.context import ToolContext
from backend.ai.tools.memory import MemoryListTool, MemoryStoreTool


def _ctx(owner_id: int = 42) -> ToolContext:
    return ToolContext(telegram=None, owner_id=owner_id, tz_str="UTC")


def _isolated_manager() -> MemoryManager:
    repo = InMemoryMemoryRepository()
    return MemoryManager(long_repository=repo, permanent_repository=repo)


@pytest.fixture
def isolated_manager(monkeypatch):
    """Route the tools at a fresh in-memory manager instead of the engine."""
    manager = _isolated_manager()
    monkeypatch.setattr(
        "backend.ai.tools.memory._resolve_memory_manager",
        lambda: manager,
    )
    return manager


@pytest.fixture
def store_tool(isolated_manager) -> MemoryStoreTool:
    return MemoryStoreTool(_ctx())


@pytest.fixture
def list_tool(isolated_manager) -> MemoryListTool:
    return MemoryListTool(_ctx())


# ── Registration / contract ────────────────────────────────────────────────


def test_memory_tools_are_registered():
    from backend.ai.tools.context import ToolContext as _Ctx
    from backend.ai.tools.registry import create_default_registry

    registry = create_default_registry(_Ctx(telegram=None, owner_id=1, tz_str="UTC"))
    assert registry.has("memory_store")
    assert registry.has("memory_list")
    names = registry.list_names()
    assert len(names) == len(set(names)) == 39


def test_tool_contract():
    store = MemoryStoreTool(_ctx())
    lst = MemoryListTool(_ctx())
    assert store.permission_level is PermissionLevel.READ_WRITE
    assert lst.permission_level is PermissionLevel.READ_ONLY
    assert store.safe is True and lst.safe is True
    for tool in (store, lst):
        assert tool.description
        assert isinstance(tool.parameters, dict)
        assert tool.return_type


# ── memory_store ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_store_long_tier_success(isolated_manager, store_tool):
    result = await store_tool.execute(
        _ctx(), {"content": "Owner's favorite editor is Vim", "tier": "long", "importance": 0.7}
    )
    assert result.success is True
    assert "Owner's favorite editor is Vim" in result.message
    assert result.data["tier"] == "long"
    assert result.data["category"] == "summary"
    assert result.data["importance"] == 0.7
    assert result.data["id"]
    assert isolated_manager.long.count(42) == 1


@pytest.mark.asyncio
async def test_store_permanent_defaults_to_fact(isolated_manager, store_tool):
    result = await store_tool.execute(_ctx(), {"content": "Owner's name is Sara", "tier": "permanent"})
    assert result.success is True
    assert result.data["tier"] == "permanent"
    assert result.data["category"] == "fact"
    entries = isolated_manager.permanent.retrieve_all(42)
    assert len(entries) == 1
    assert entries[0].category is MemoryCategory.FACT
    assert entries[0].tier is MemoryTier.PERMANENT


@pytest.mark.asyncio
async def test_store_rejects_missing_or_oversized_content(store_tool):
    missing = await store_tool.execute(_ctx(), {})
    assert missing.success is False and "content" in missing.message.lower()

    oversized = await store_tool.execute(_ctx(), {"content": "x" * (MAX_MEMORY_ENTRY_CHARS + 1)})
    assert oversized.success is False and "too long" in oversized.message


@pytest.mark.asyncio
async def test_store_rejects_unknown_tier_and_category(store_tool):
    bad_tier = await store_tool.execute(_ctx(), {"content": "f", "tier": "short"})
    assert bad_tier.success is False and "Unknown tier" in bad_tier.message

    bad_category = await store_tool.execute(_ctx(), {"content": "f", "category": "gossip"})
    assert bad_category.success is False and "Unknown category" in bad_category.message

    bad_permanent = await store_tool.execute(
        _ctx(), {"content": "f", "tier": "permanent", "category": "context"}
    )
    assert bad_permanent.success is False and "permanent tier" in bad_permanent.message


@pytest.mark.asyncio
async def test_store_rejects_bad_importance(store_tool):
    too_big = await store_tool.execute(_ctx(), {"content": "f", "importance": 1.5})
    assert too_big.success is False and "Importance" in too_big.message

    not_a_number = await store_tool.execute(_ctx(), {"content": "f", "importance": "high"})
    assert not_a_number.success is False and "Importance" in not_a_number.message


@pytest.mark.asyncio
async def test_store_reports_repository_rejection_honestly(monkeypatch, store_tool):
    """A repository-rejected write (save() -> False) must fail the tool."""
    class RejectingRepo(InMemoryMemoryRepository):
        def save(self, entry):
            return False

    manager = MemoryManager(long_repository=RejectingRepo(), permanent_repository=RejectingRepo())
    monkeypatch.setattr("backend.ai.tools.memory._resolve_memory_manager", lambda: manager)

    result = await store_tool.execute(_ctx(), {"content": "will be rejected"})
    assert result.success is False
    assert "could not be stored" in result.message


@pytest.mark.asyncio
async def test_store_reports_repository_failure_honestly(monkeypatch, store_tool):
    class RaisingRepo(InMemoryMemoryRepository):
        def save(self, entry):
            raise RuntimeError("db down")

    manager = MemoryManager(long_repository=RaisingRepo(), permanent_repository=RaisingRepo())
    monkeypatch.setattr("backend.ai.tools.memory._resolve_memory_manager", lambda: manager)

    result = await store_tool.execute(_ctx(), {"content": "will raise"})
    assert result.success is False
    assert "could not be stored" in result.message


@pytest.mark.asyncio
async def test_store_is_bounded_against_hanging_repository(monkeypatch, store_tool):
    class SlowRepo(InMemoryMemoryRepository):
        def save(self, entry):
            time.sleep(0.4)
            return True

    manager = MemoryManager(long_repository=SlowRepo(), permanent_repository=SlowRepo())
    monkeypatch.setattr("backend.ai.tools.memory._resolve_memory_manager", lambda: manager)
    monkeypatch.setattr("backend.ai.tools.memory.MEMORY_WRITE_TIMEOUT_S", 0.05)

    result = await store_tool.execute(_ctx(), {"content": "slow write"})
    assert result.success is False
    assert "timed out" in result.message


@pytest.mark.asyncio
async def test_store_owner_scoped(isolated_manager, store_tool):
    await store_tool.execute(_ctx(owner_id=1), {"content": "Owner one fact"})
    result = await MemoryListTool(_ctx(owner_id=2)).execute(_ctx(owner_id=2), {})
    assert result.success is True
    assert "No memories stored" in result.message


@pytest.mark.asyncio
async def test_duplicate_content_never_duplicates_rows(isolated_manager, store_tool):
    first = await store_tool.execute(_ctx(), {"content": "same fact", "tier": "long"})
    second = await store_tool.execute(_ctx(), {"content": "same fact", "tier": "long"})
    assert first.success is True and second.success is True
    assert isolated_manager.long.count(42) == 1


# ── memory_list ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_returns_stored_memories(isolated_manager, store_tool, list_tool):
    await store_tool.execute(_ctx(), {"content": "Prefers voice notes", "tier": "long", "importance": 0.9})
    await store_tool.execute(_ctx(), {"content": "Owner's name is Sara", "tier": "permanent"})

    result = await list_tool.execute(_ctx(), {})
    assert result.success is True
    assert "Prefers voice notes" in result.message
    assert "Owner's name is Sara" in result.message
    assert "Permanent" in result.message and "Long" in result.message
    assert result.data["permanent"] and result.data["long"]


@pytest.mark.asyncio
async def test_list_tier_filter(isolated_manager, store_tool, list_tool):
    await store_tool.execute(_ctx(), {"content": "long fact", "tier": "long"})
    await store_tool.execute(_ctx(), {"content": "permanent fact", "tier": "permanent"})

    only_long = await list_tool.execute(_ctx(), {"tier": "long"})
    assert "long fact" in only_long.message
    assert "permanent fact" not in only_long.message

    only_permanent = await list_tool.execute(_ctx(), {"tier": "permanent"})
    assert "permanent fact" in only_permanent.message
    assert "long fact" not in only_permanent.message


@pytest.mark.asyncio
async def test_list_query_filter(isolated_manager, store_tool, list_tool):
    await store_tool.execute(_ctx(), {"content": "likes espresso", "tier": "long"})
    await store_tool.execute(_ctx(), {"content": "likes tea", "tier": "long"})

    result = await list_tool.execute(_ctx(), {"query": "espresso"})
    assert "espresso" in result.message
    assert "likes tea" not in result.message


@pytest.mark.asyncio
async def test_list_empty(isolated_manager, list_tool):
    result = await list_tool.execute(_ctx(), {})
    assert result.success is True
    assert "No memories stored" in result.message
    assert result.data == {"permanent": [], "long": []}


@pytest.mark.asyncio
async def test_list_rejects_unknown_tier_and_bad_limit(list_tool):
    bad_tier = await list_tool.execute(_ctx(), {"tier": "short"})
    assert bad_tier.success is False and "Unknown tier" in bad_tier.message

    bad_limit = await list_tool.execute(_ctx(), {"limit": "many"})
    assert bad_limit.success is False and "Limit" in bad_limit.message


@pytest.mark.asyncio
async def test_list_is_bounded_against_hanging_repository(monkeypatch, list_tool):
    class SlowRepo(InMemoryMemoryRepository):
        def query(self, query):
            time.sleep(0.4)
            return []

    manager = MemoryManager(long_repository=SlowRepo(), permanent_repository=SlowRepo())
    monkeypatch.setattr("backend.ai.tools.memory._resolve_memory_manager", lambda: manager)
    monkeypatch.setattr("backend.ai.tools.memory.MEMORY_WRITE_TIMEOUT_S", 0.05)

    result = await list_tool.execute(_ctx(), {})
    assert result.success is False
    assert "timed out" in result.message


# ── resolver (single memory authority) ─────────────────────────────────────


def test_resolver_prefers_engine_manager(monkeypatch):
    from backend.ai.tools import memory as memory_module

    sentinel = _isolated_manager()

    class _StubEngine:
        memory_manager = sentinel

    monkeypatch.setattr(
        "backend.ai.engine.engine.get_engine", lambda: _StubEngine()
    )
    assert memory_module._resolve_memory_manager() is sentinel


def test_resolver_falls_back_to_standalone_manager(monkeypatch):
    from backend.ai.tools import memory as memory_module

    def _boom():
        raise RuntimeError("no engine")

    monkeypatch.setattr("backend.ai.engine.engine.get_engine", _boom)
    manager = memory_module._resolve_memory_manager()
    assert isinstance(manager, MemoryManager)
