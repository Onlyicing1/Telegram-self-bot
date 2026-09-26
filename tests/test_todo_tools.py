"""The basic Todo AI surface — ToolRegistry → ToolExecutor → tool → service.

Every test drives the REAL chain (no stubbed tools): the registered tool is
executed through the ``ToolExecutor`` against an in-memory task repository,
and the durable row is asserted afterwards. The AI never reaches the database
directly, the executor stays the only caller of ``tool.execute()``, and an
ambiguous title reference can never reach a mutation.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from backend.ai.database import manager as dbm
from backend.ai.database.task_repository import InMemoryTaskRepository
from backend.ai.tools.base import PermissionLevel
from backend.ai.tools.context import ToolContext
from backend.ai.tools.executor import ToolExecutor
from backend.ai.tools.registry import create_default_registry

OWNER = 4242
OTHER = 9999


def _scheduled_data(label="scheduled"):
    return {
        "label": label,
        "schedule_type": "interval",
        "schedule": {"seconds": 300},
        "timezone": "UTC",
        "actions": [{"name": "send_message", "arguments": {"text": "hello"}}],
        "notification_destination": {},
    }


def _manager_with(repository):
    manager = dbm.RepositoryManager(supabase_available=False)
    manager._task = repository
    return manager


def _chain(repo, owner=OWNER):
    ctx = ToolContext(telegram=None, owner_id=owner, tz_str="Asia/Tehran", extra={})
    registry = create_default_registry(ctx)
    return registry, ctx, ToolExecutor(registry, ctx), _manager_with(repo)


async def _run(executor, manager, name, arguments):
    with patch.object(dbm, "get_repository_manager", return_value=manager):
        results = await executor.execute_calls(
            [{"name": name, "arguments": arguments}], owner_id=OWNER
        )
    return results[0]


# ── registration + provider visibility ───────────────────────────────────────


def test_todo_tools_are_registered_with_their_safety_levels():
    registry, _ctx, _executor, _manager = _chain(InMemoryTaskRepository())
    add = registry.get("todo_add")
    find = registry.get("todo_find")
    edit = registry.get("todo_edit")
    assert add is not None and find is not None and edit is not None
    assert add.permission_level is PermissionLevel.READ_WRITE and add.safe is True
    assert find.permission_level is PermissionLevel.READ_ONLY and find.safe is True
    assert edit.permission_level is PermissionLevel.READ_WRITE and edit.safe is True
    assert add.required_arguments == ("title",)
    assert find.required_arguments == ("query",)
    assert set(edit.parameters) == {"title", "task_id", "expected_version", "query"}


def test_todo_tools_are_visible_in_the_provider_schemas():
    from backend.ai.engine.dispatcher import Dispatcher

    registry, _ctx, _executor, _manager = _chain(InMemoryTaskRepository())
    dispatcher = object.__new__(Dispatcher)
    dispatcher.set_tool_registry(registry)
    names = {d["function"]["name"] for d in Dispatcher._build_tool_definitions(dispatcher)}
    assert {"todo_add", "todo_find", "todo_edit"} <= names


# ── create ───────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_todo_add_creates_a_durable_todo_through_the_executor():
    repo = InMemoryTaskRepository()
    _registry, _ctx, executor, manager = _chain(repo)

    result = await _run(executor, manager, "todo_add", {"title": "  گزارش دانشگاه  "})
    assert result.success is True
    assert result.data["durable"] is True and result.data["version"] == 1

    stored = await repo.get_task(OWNER, result.data["task_id"])
    assert stored is not None
    assert stored.label == "گزارش دانشگاه"
    assert stored.schedule_type == "todo"
    assert stored.actions == [] and stored.next_run_at is None
    assert stored.timezone == "Asia/Tehran"  # the trusted runtime zone
    assert "Todo #" in result.message


@pytest.mark.asyncio
async def test_todo_add_asks_for_the_missing_title_instead_of_inventing_one():
    repo = InMemoryTaskRepository()
    _registry, _ctx, executor, manager = _chain(repo)
    for arguments in ({}, {"title": ""}, {"title": "   "}, {"title": 5}):
        result = await _run(executor, manager, "todo_add", arguments)
        assert result.success is False
        assert "title" in result.message.lower()
    assert await repo.list_tasks(OWNER) == []


# ── find ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_todo_find_resolves_one_two_or_zero_matches():
    repo = InMemoryTaskRepository()
    _registry, _ctx, executor, manager = _chain(repo)
    first = await _run(executor, manager, "todo_add", {"title": "گزارش دانشگاه — شنبه"})
    second = await _run(executor, manager, "todo_add", {"title": "گزارش دانشگاه — پایان‌ترم"})
    third = await _run(executor, manager, "todo_add", {"title": "خرید نان"})

    unique = await _run(executor, manager, "todo_find", {"query": "خرید نان"})
    assert unique.success is True
    assert unique.data["outcome"] == "unique"
    assert unique.data["task_id"] == third.data["task_id"]
    assert "خرید نان" in unique.message

    ambiguous = await _run(executor, manager, "todo_find", {"query": "گزارش دانشگاه"})
    assert ambiguous.success is False
    assert ambiguous.data["outcome"] == "ambiguous"
    assert {c["task_id"] for c in ambiguous.data["candidates"]} == {
        first.data["task_id"], second.data["task_id"]
    }
    assert "which one do you mean?" in ambiguous.message

    missing = await _run(executor, manager, "todo_find", {"query": "خرید ماشین"})
    assert missing.success is False
    assert missing.data["outcome"] == "not_found"
    assert "No todo found" in missing.message


# ── edit ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_todo_edit_by_id_uses_the_version_and_rejects_a_stale_one():
    repo = InMemoryTaskRepository()
    _registry, _ctx, executor, manager = _chain(repo)
    created = await _run(executor, manager, "todo_add", {"title": "old title"})

    edited = await _run(
        executor,
        manager,
        "todo_edit",
        {
            "task_id": created.data["task_id"],
            "expected_version": created.data["version"],
            "title": "new title",
        },
    )
    assert edited.success is True and edited.data["title"] == "new title"
    stored = await repo.get_task(OWNER, created.data["task_id"])
    assert stored.label == "new title" and stored.version == created.data["version"] + 1

    stale = await _run(
        executor,
        manager,
        "todo_edit",
        {
            "task_id": created.data["task_id"],
            "expected_version": created.data["version"],
            "title": "overwritten",
        },
    )
    assert stale.success is False and "stale" in stale.message
    assert (await repo.get_task(OWNER, created.data["task_id"])).label == "new title"

    no_version = await _run(
        executor, manager, "todo_edit", {"task_id": created.data["task_id"], "title": "x"}
    )
    assert no_version.success is False
    assert "version" in no_version.message


@pytest.mark.asyncio
async def test_todo_edit_by_title_refuses_an_ambiguous_reference():
    repo = InMemoryTaskRepository()
    _registry, _ctx, executor, manager = _chain(repo)
    await _run(executor, manager, "todo_add", {"title": "گزارش دانشگاه — شنبه"})
    await _run(executor, manager, "todo_add", {"title": "گزارش دانشگاه — ترم"})

    ambiguous = await _run(executor, manager, "todo_edit", {"query": "گزارش دانشگاه", "title": "x"})
    assert ambiguous.success is False
    assert ambiguous.data["outcome"] == "ambiguous"
    for task in await repo.list_tasks(OWNER):
        assert task.label.startswith("گزارش دانشگاه — ")

    unique = await _run(executor, manager, "todo_edit", {"query": "ترم", "title": "گزارش ترم"})
    assert unique.success is True
    assert sorted(t.label for t in await repo.list_tasks(OWNER)) == [
        "گزارش ترم", "گزارش دانشگاه — شنبه",
    ]


# ── complete / reopen through task_transition ────────────────────────────────


@pytest.mark.asyncio
async def test_task_transition_completes_and_reopens_a_todo_by_title():
    repo = InMemoryTaskRepository()
    _registry, _ctx, executor, manager = _chain(repo)
    await _run(executor, manager, "todo_add", {"title": "گزارش دانشگاه"})

    completed = await _run(
        executor, manager, "task_transition", {"query": "گزارش دانشگاه", "action": "completed"}
    )
    assert completed.success is True
    assert completed.data["status"] == "completed"
    assert "completed" in completed.message
    stored = (await repo.list_tasks(OWNER))[0]
    assert stored.status == "completed"

    reopened = await _run(
        executor, manager, "task_transition", {"query": "گزارش دانشگاه", "action": "active"}
    )
    assert reopened.success is True
    assert reopened.data["status"] == "active"
    assert "reopened" in reopened.message
    assert (await repo.list_tasks(OWNER))[0].status == "active"


@pytest.mark.asyncio
async def test_task_transition_by_id_still_requires_the_version():
    repo = InMemoryTaskRepository()
    _registry, _ctx, executor, manager = _chain(repo)
    created = await _run(executor, manager, "todo_add", {"title": "todo"})

    missing = await _run(
        executor, manager, "task_transition", {"task_id": created.data["task_id"], "action": "completed"}
    )
    assert missing.success is False and "version" in missing.message
    assert (await repo.list_tasks(OWNER))[0].status == "active"

    done = await _run(
        executor,
        manager,
        "task_transition",
        {
            "task_id": created.data["task_id"],
            "expected_version": created.data["version"],
            "action": "completed",
        },
    )
    assert done.success is True and done.data["status"] == "completed"

    stale = await _run(
        executor,
        manager,
        "task_transition",
        {
            "task_id": created.data["task_id"],
            "expected_version": created.data["version"],
            "action": "active",
        },
    )
    assert stale.success is False and "stale" in stale.message
    assert (await repo.list_tasks(OWNER))[0].status == "completed"


@pytest.mark.asyncio
async def test_task_transition_never_reopens_a_completed_scheduled_task():
    repo = InMemoryTaskRepository()
    _registry, _ctx, executor, manager = _chain(repo)
    task = await repo.create_task(OWNER, _scheduled_data())
    done = await _run(
        executor,
        manager,
        "task_transition",
        {"task_id": task.id, "expected_version": task.version, "action": "completed"},
    )
    assert done.success is True
    reopened = await _run(
        executor,
        manager,
        "task_transition",
        {"task_id": task.id, "expected_version": done.data["version"], "action": "active"},
    )
    assert reopened.success is False
    assert "only a todo can be reopened" in reopened.message
    assert (await repo.get_task(OWNER, task.id)).status == "completed"


@pytest.mark.asyncio
async def test_transition_by_an_ambiguous_reference_changes_nothing():
    repo = InMemoryTaskRepository()
    _registry, _ctx, executor, manager = _chain(repo)
    await _run(executor, manager, "todo_add", {"title": "گزارش دانشگاه — شنبه"})
    await _run(executor, manager, "todo_add", {"title": "گزارش دانشگاه — ترم"})

    result = await _run(
        executor, manager, "task_transition", {"query": "گزارش دانشگاه", "action": "completed"}
    )
    assert result.success is False and result.data["outcome"] == "ambiguous"
    assert [t.status for t in await repo.list_tasks(OWNER)] == ["active", "active"]


# ── delete through task_delete ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_task_delete_removes_a_todo_by_title_but_refuses_ambiguity():
    repo = InMemoryTaskRepository()
    _registry, _ctx, executor, manager = _chain(repo)
    await _run(executor, manager, "todo_add", {"title": "گزارش دانشگاه — شنبه"})
    await _run(executor, manager, "todo_add", {"title": "گزارش دانشگاه — ترم"})
    await _run(executor, manager, "todo_add", {"title": "خرید نان"})

    ambiguous = await _run(executor, manager, "task_delete", {"query": "گزارش دانشگاه"})
    assert ambiguous.success is False and ambiguous.data["outcome"] == "ambiguous"
    assert len(await repo.list_tasks(OWNER)) == 3

    deleted = await _run(executor, manager, "task_delete", {"query": "نان"})
    assert deleted.success is True and deleted.data["deleted"] is True
    assert sorted(t.label for t in await repo.list_tasks(OWNER)) == [
        "گزارش دانشگاه — ترم", "گزارش دانشگاه — شنبه",
    ]

    missing = await _run(executor, manager, "task_delete", {"query": "نان"})
    assert missing.success is False and missing.data["outcome"] == "not_found"


@pytest.mark.asyncio
async def test_the_todo_tools_are_owner_scoped():
    repo = InMemoryTaskRepository()
    _registry, _ctx, executor, manager = _chain(repo)
    created = await _run(executor, manager, "todo_add", {"title": "other owner"})
    assert created.success is True

    _registry, _ctx, foreign_executor, manager = _chain(repo, owner=OTHER)
    for name, arguments in (
        ("todo_find", {"query": "other owner"}),
        ("todo_edit", {"query": "other owner", "title": "hijacked"}),
        ("task_transition", {"query": "other owner", "action": "completed"}),
        ("task_delete", {"query": "other owner"}),
    ):
        with patch.object(dbm, "get_repository_manager", return_value=manager):
            results = await foreign_executor.execute_calls(
                [{"name": name, "arguments": arguments}], owner_id=OTHER
            )
        assert results[0].success is False, name
    stored = await repo.get_task(OWNER, created.data["task_id"])
    assert stored.label == "other owner" and stored.status == "active"


# ── the structured (JSON) action path ────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_json_action_path_executes_through_the_same_tools():
    from backend.ai.actions import EXECUTABLE_ACTION_NAMES, parse_action_text

    for name in ("todo_add", "todo_find", "todo_edit"):
        assert name in EXECUTABLE_ACTION_NAMES

    add = parse_action_text('{"action":"todo_add","title":"گزارش دانشگاه"}')
    assert add.kind == "executable"
    assert add.tool_calls == [{"name": "todo_add", "arguments": {"title": "گزارش دانشگاه"}}]

    find = parse_action_text('{"action":"todo_find","query":"گزارش دانشگاه"}')
    assert find.tool_calls == [{"name": "todo_find", "arguments": {"query": "گزارش دانشگاه"}}]

    edit = parse_action_text(
        '{"action":"todo_edit","query":"گزارش دانشگاه","title":"گزارش ترم"}'
    )
    assert edit.tool_calls == [
        {"name": "todo_edit", "arguments": {"query": "گزارش دانشگاه", "title": "گزارش ترم"}}
    ]

    complete = parse_action_text(
        '{"action":"task_transition","query":"گزارش دانشگاه","action_status":"completed"}'
    )
    assert complete.tool_calls == [
        {"name": "task_transition", "arguments": {"query": "گزارش دانشگاه", "action": "completed"}}
    ]

    remove = parse_action_text('{"action":"task_delete","query":"گزارش دانشگاه"}')
    assert remove.tool_calls == [{"name": "task_delete", "arguments": {"query": "گزارش دانشگاه"}}]

    # An id AND a title reference is refused, never silently preferred.
    both = parse_action_text(
        '{"action":"task_delete","task_id":3,"expected_version":2,"query":"x"}'
    )
    assert both.kind == "invalid"
    # An empty title is refused, never filled.
    assert parse_action_text('{"action":"todo_add","title":"  "}').kind == "invalid"
