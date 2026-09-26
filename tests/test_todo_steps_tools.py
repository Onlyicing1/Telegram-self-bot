"""The multi-step Todo AI surface — ToolRegistry → ToolExecutor → tool → service.

Every test drives the REAL chain (no stubbed tools) against an in-memory task
repository, and asserts the durable rows afterwards. The AI never reaches the
database, the executor stays the only caller of ``tool.execute()``, an
ambiguous todo OR step reference can never reach a mutation, and the parent
todo is never completed while one of its steps remains.
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
STEPS = ("جمع‌آوری منابع", "نوشتن گزارش", "آماده‌سازی ارائه")


def _manager_with(repository):
    manager = dbm.RepositoryManager(supabase_available=False)
    manager._task = repository
    return manager


def _chain(repo, owner=OWNER):
    ctx = ToolContext(telegram=None, owner_id=owner, tz_str="Asia/Tehran", extra={})
    registry = create_default_registry(ctx)
    return registry, ctx, ToolExecutor(registry, ctx), _manager_with(repo)


async def _run(executor, manager, name, arguments, owner=OWNER):
    with patch.object(dbm, "get_repository_manager", return_value=manager):
        results = await executor.execute_calls(
            [{"name": name, "arguments": arguments}], owner_id=owner
        )
    return results[0]


# ── registration + provider visibility ───────────────────────────────────────


def test_the_step_tools_are_registered_with_their_safety_levels():
    registry, _ctx, _executor, _manager = _chain(InMemoryTaskRepository())
    levels = {
        "todo_step_add": PermissionLevel.READ_WRITE,
        "todo_step_list": PermissionLevel.READ_ONLY,
        "todo_step_transition": PermissionLevel.READ_WRITE,
        "todo_step_edit": PermissionLevel.READ_WRITE,
        "todo_step_delete": PermissionLevel.READ_WRITE,
    }
    for name, level in levels.items():
        tool = registry.get(name)
        assert tool is not None, name
        assert tool.permission_level is level, name
        assert tool.safe is True, name
    assert set(registry.get("todo_step_transition").parameters) == {
        "action", "step", "step_query", "task_id", "query",
    }
    assert set(registry.get("todo_step_add").parameters) == {
        "title", "steps", "task_id", "query",
    }
    assert set(registry.get("todo_add").parameters) == {"title", "steps"}
    assert "complete_steps" in registry.get("task_transition").parameters


def test_the_step_tools_are_visible_in_the_provider_schemas():
    from backend.ai.engine.dispatcher import Dispatcher

    registry, _ctx, _executor, _manager = _chain(InMemoryTaskRepository())
    dispatcher = object.__new__(Dispatcher)
    dispatcher.set_tool_registry(registry)
    names = {d["function"]["name"] for d in Dispatcher._build_tool_definitions(dispatcher)}
    assert {
        "todo_step_add",
        "todo_step_list",
        "todo_step_transition",
        "todo_step_edit",
        "todo_step_delete",
    } <= names


# ── creating a todo with its steps ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_todo_add_creates_the_todo_and_its_steps_in_one_call():
    repo = InMemoryTaskRepository()
    _registry, _ctx, executor, manager = _chain(repo)

    result = await _run(
        executor, manager, "todo_add", {"title": "پروژه دانشگاه", "steps": list(STEPS)}
    )
    assert result.success is True
    assert [entry["step"] for entry in result.data["steps"]] == [1, 2, 3]
    assert [entry["title"] for entry in result.data["steps"]] == list(STEPS)
    stored = await repo.list_tasks(OWNER)
    assert len(stored) == 1
    steps = await repo.list_steps(OWNER, stored[0].id)
    assert [s.title for s in steps] == list(STEPS)
    assert result.data["durable"] is True
    # The reported steps are the STORED ones, not the request echoed.
    assert result.data["steps"][0]["step_id"] == steps[0].id


@pytest.mark.asyncio
async def test_todo_add_with_a_bad_step_creates_nothing():
    repo = InMemoryTaskRepository()
    _registry, _ctx, executor, manager = _chain(repo)
    for arguments in (
        {"title": "x", "steps": ["ok", "   "]},
        {"title": "x", "steps": "not-a-list"},
        {"title": "x", "steps": ["ok"] * 21},
        {"title": "x", "steps": ["y" * 257]},
    ):
        result = await _run(executor, manager, "todo_add", arguments)
        assert result.success is False, arguments
    assert await repo.list_tasks(OWNER) == []
    assert repo._steps == {}


@pytest.mark.asyncio
async def test_an_explicitly_empty_step_list_makes_a_todo_without_steps():
    repo = InMemoryTaskRepository()
    _registry, _ctx, executor, manager = _chain(repo)
    result = await _run(executor, manager, "todo_add", {"title": "x", "steps": []})
    assert result.success is True
    assert "steps" not in result.data
    assert await repo.list_steps(OWNER, result.data["task_id"]) == []


# ── adding steps to an existing todo ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_todo_step_add_appends_one_or_several_steps_by_id_and_by_words():
    repo = InMemoryTaskRepository()
    _registry, _ctx, executor, manager = _chain(repo)
    created = await _run(executor, manager, "todo_add", {"title": "پروژه دانشگاه"})
    task_id = created.data["task_id"]

    one = await _run(
        executor, manager, "todo_step_add", {"task_id": task_id, "title": "جمع‌آوری منابع"}
    )
    assert one.success is True and one.data["steps"][0]["step"] == 1

    many = await _run(
        executor,
        manager,
        "todo_step_add",
        {"query": "پروژه دانشگاه", "steps": ["نوشتن گزارش", "آماده‌سازی ارائه"]},
    )
    assert many.success is True
    assert [entry["step"] for entry in many.data["steps"]] == [2, 3]
    steps = await repo.list_steps(OWNER, task_id)
    assert [s.title for s in steps] == list(STEPS)

    missing = await _run(executor, manager, "todo_step_add", {"query": "nope", "title": "x"})
    assert missing.success is False and missing.data["outcome"] == "not_found"
    blank = await _run(executor, manager, "todo_step_add", {"task_id": task_id})
    assert blank.success is False


# ── listing + progress ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_todo_step_list_reports_progress_and_the_next_step():
    repo = InMemoryTaskRepository()
    _registry, _ctx, executor, manager = _chain(repo)
    created = await _run(
        executor, manager, "todo_add", {"title": "پروژه دانشگاه", "steps": list(STEPS)}
    )
    task_id = created.data["task_id"]
    steps = await repo.list_steps(OWNER, task_id)
    await _run(
        executor,
        manager,
        "todo_step_transition",
        {"task_id": task_id, "step": 1, "action": "completed"},
    )

    listing = await _run(executor, manager, "todo_step_list", {"task_id": task_id})
    assert listing.success is True
    assert listing.data["total"] == 3 and listing.data["completed"] == 1
    assert listing.data["remaining"] == 2 and listing.data["next_step"] == 2
    assert "1 / 3 steps completed" in listing.message
    assert f"✓ 1. {STEPS[0]}" in listing.message
    assert f"○ 2. {STEPS[1]}" in listing.message
    assert f"Next: 2. {STEPS[1]}" in listing.message
    assert [entry["step_id"] for entry in listing.data["steps"]] == [s.id for s in steps]


@pytest.mark.asyncio
async def test_todo_find_and_task_inspect_show_the_step_summary():
    repo = InMemoryTaskRepository()
    _registry, _ctx, executor, manager = _chain(repo)
    created = await _run(
        executor, manager, "todo_add", {"title": "پروژه دانشگاه", "steps": list(STEPS)}
    )
    task_id = created.data["task_id"]

    found = await _run(executor, manager, "todo_find", {"query": "پروژه دانشگاه"})
    assert found.success is True
    assert found.data["steps_total"] == 3 and found.data["steps_remaining"] == 3
    assert "Progress: 0 / 3 steps completed" in found.message

    inspected = await _run(executor, manager, "task_inspect", {"task_id": task_id})
    assert inspected.success is True
    assert "Progress: 0 / 3 steps completed" in inspected.message
    assert f"○ 3. {STEPS[2]}" in inspected.message


# ── per-step lifecycle through the tools ─────────────────────────────────────


@pytest.mark.asyncio
async def test_todo_step_transition_completes_and_reopens_by_number_and_words():
    repo = InMemoryTaskRepository()
    _registry, _ctx, executor, manager = _chain(repo)
    created = await _run(
        executor, manager, "todo_add", {"title": "پروژه دانشگاه", "steps": list(STEPS)}
    )
    task_id = created.data["task_id"]

    done = await _run(
        executor,
        manager,
        "todo_step_transition",
        {"task_id": task_id, "step": 2, "action": "completed"},
    )
    assert done.success is True and done.data["status"] == "completed"
    assert done.data["completed"] == 1 and done.data["total"] == 3
    assert "completed" in done.message
    assert (await repo.list_steps(OWNER, task_id))[1].completed_at is not None

    by_words = await _run(
        executor,
        manager,
        "todo_step_transition",
        {"query": "پروژه دانشگاه", "step_query": "ارائه", "action": "completed"},
    )
    assert by_words.success is True and by_words.data["step"] == 3

    reopened = await _run(
        executor,
        manager,
        "todo_step_transition",
        {"task_id": task_id, "step_query": "گزارش", "action": "active"},
    )
    assert reopened.success is True and reopened.data["status"] == "active"

    bad_action = await _run(
        executor,
        manager,
        "todo_step_transition",
        {"task_id": task_id, "step": 1, "action": "paused"},
    )
    assert bad_action.success is False and "completed" in bad_action.message


@pytest.mark.asyncio
async def test_step_references_refuse_to_guess():
    repo = InMemoryTaskRepository()
    _registry, _ctx, executor, manager = _chain(repo)
    ambiguous_todo = await _run(
        executor, manager, "todo_add", {"title": "پروژه دانشگاه — شنبه", "steps": ["a"]}
    )
    await _run(
        executor, manager, "todo_add", {"title": "پروژه دانشگاه — ترم", "steps": ["a"]}
    )
    both = await _run(
        executor, manager, "todo_step_list", {"query": "پروژه دانشگاه"}
    )
    assert both.success is False and both.data["outcome"] == "ambiguous"
    assert len(both.data["candidates"]) == 2

    # An ambiguous STEP reference inside ONE todo is refused the same way.
    repo_two = InMemoryTaskRepository()
    _r2, _c2, executor_two, manager_two = _chain(repo_two)
    created = await _run(
        executor_two,
        manager_two,
        "todo_add",
        {"title": "پروژه", "steps": ["گزارش دانشگاه — شنبه", "گزارش دانشگاه — ترم"]},
    )
    ambiguous = await _run(
        executor_two,
        manager_two,
        "todo_step_transition",
        {"task_id": created.data["task_id"], "step_query": "گزارش دانشگاه", "action": "completed"},
    )
    assert ambiguous.success is False and ambiguous.data["outcome"] == "ambiguous"
    assert {c["step"] for c in ambiguous.data["candidates"]} == {1, 2}
    assert "which one do you mean?" in ambiguous.message
    assert all(
        s.status == "active"
        for s in await repo_two.list_steps(OWNER, created.data["task_id"])
    )

    # A step number that does not exist is an honest refusal.
    out_of_range = await _run(
        executor, manager, "todo_step_transition",
        {"task_id": ambiguous_todo.data["task_id"], "step": 7, "action": "completed"},
    )
    assert out_of_range.success is False and out_of_range.data["outcome"] == "not_found"


@pytest.mark.asyncio
async def test_todo_step_edit_renames_the_step_and_never_the_todo():
    repo = InMemoryTaskRepository()
    _registry, _ctx, executor, manager = _chain(repo)
    created = await _run(
        executor, manager, "todo_add", {"title": "پروژه دانشگاه", "steps": list(STEPS)}
    )
    task_id = created.data["task_id"]

    edited = await _run(
        executor, manager, "todo_step_edit",
        {"task_id": task_id, "step": 1, "title": "جمع‌آوری منابع ترم"},
    )
    assert edited.success is True and edited.data["title"] == "جمع‌آوری منابع ترم"
    stored_task = await repo.get_task(OWNER, task_id)
    assert stored_task.label == "پروژه دانشگاه"
    assert (await repo.list_steps(OWNER, task_id))[0].title == "جمع‌آوری منابع ترم"

    blank = await _run(
        executor, manager, "todo_step_edit", {"task_id": task_id, "step": 1, "title": "  "}
    )
    assert blank.success is False


@pytest.mark.asyncio
async def test_todo_step_delete_removes_the_step_and_never_the_todo():
    repo = InMemoryTaskRepository()
    _registry, _ctx, executor, manager = _chain(repo)
    created = await _run(
        executor, manager, "todo_add", {"title": "پروژه دانشگاه", "steps": list(STEPS)}
    )
    task_id = created.data["task_id"]

    removed = await _run(
        executor, manager, "todo_step_delete", {"task_id": task_id, "step": 2}
    )
    assert removed.success is True and removed.data["removed"] is True
    assert removed.data["remaining"] == 2
    assert await repo.get_task(OWNER, task_id) is not None
    assert [s.title for s in await repo.list_steps(OWNER, task_id)] == [
        STEPS[0], STEPS[2],
    ]
    again = await _run(
        executor, manager, "todo_step_delete", {"task_id": task_id, "step": 2}
    )
    assert again.success is True  # the ordinal now names the former third step
    assert len(await repo.list_steps(OWNER, task_id)) == 1


@pytest.mark.asyncio
async def test_a_step_tool_acts_on_the_version_it_just_read():
    """The CAS guard is the version the resolver read — never a remembered one.

    A concurrent writer moves the step on; the next tool call resolves the step
    again, so it acts on the CURRENT row (and reports the new version) instead
    of silently overwriting newer state with a stale value. A write that loses
    the race fails closed at the repository (see the service/repository suite).
    """
    repo = InMemoryTaskRepository()
    _registry, _ctx, executor, manager = _chain(repo)
    created = await _run(
        executor, manager, "todo_add", {"title": "پروژه دانشگاه", "steps": ["only"]}
    )
    task_id = created.data["task_id"]
    step = (await repo.list_steps(OWNER, task_id))[0]
    await repo.update_step(OWNER, step.id, step.version, {"status": "completed"})
    assert (await repo.list_steps(OWNER, task_id))[0].version == step.version + 1

    reopened = await _run(
        executor, manager, "todo_step_transition",
        {"task_id": task_id, "step": 1, "action": "active"},
    )
    assert reopened.success is True
    assert reopened.data["version"] == step.version + 2
    assert (await repo.list_steps(OWNER, task_id))[0].status == "active"

    # The stale path itself: an explicit version that no longer matches is
    # refused by the repository and writes nothing.
    current = (await repo.list_steps(OWNER, task_id))[0]
    assert await repo.update_step(OWNER, current.id, step.version, {"title": "x"}) is None
    assert (await repo.list_steps(OWNER, task_id))[0].title == "only"


# ── parent completion with remaining steps ───────────────────────────────────


@pytest.mark.asyncio
async def test_task_transition_refuses_to_complete_a_todo_with_remaining_steps():
    repo = InMemoryTaskRepository()
    _registry, _ctx, executor, manager = _chain(repo)
    created = await _run(
        executor, manager, "todo_add", {"title": "پروژه دانشگاه", "steps": list(STEPS)}
    )
    task_id = created.data["task_id"]

    refused = await _run(
        executor, manager, "task_transition",
        {"task_id": task_id, "action": "completed", "expected_version": created.data["version"]},
    )
    assert refused.success is False
    assert "3 of 3 steps remain" in refused.message
    assert (await repo.get_task(OWNER, task_id)).status == "active"


@pytest.mark.asyncio
async def test_complete_steps_completes_the_todo_and_what_it_still_had():
    repo = InMemoryTaskRepository()
    _registry, _ctx, executor, manager = _chain(repo)
    created = await _run(
        executor, manager, "todo_add", {"title": "پروژه دانشگاه", "steps": list(STEPS)}
    )
    task_id = created.data["task_id"]
    task = await repo.get_task(OWNER, task_id)
    steps = await repo.list_steps(OWNER, task_id)
    await repo.update_step(OWNER, steps[0].id, steps[0].version, {"status": "completed"})

    done = await _run(
        executor, manager, "task_transition",
        {
            "task_id": task_id,
            "action": "completed",
            "expected_version": task.version,
            "complete_steps": True,
        },
    )
    assert done.success is True and done.data["status"] == "completed"
    remaining = [s for s in await repo.list_steps(OWNER, task_id) if s.status != "completed"]
    assert remaining == []

    # And a stale todo version is still refused, with the steps untouched.
    reopened = await _run(
        executor, manager, "task_transition",
        {"task_id": task_id, "action": "active", "expected_version": done.data["version"]},
    )
    assert reopened.success is True
    await repo.update_step(
        OWNER,
        steps[1].id,
        (await repo.get_step(OWNER, steps[1].id)).version,
        {"status": "active"},
    )
    stale = await _run(
        executor, manager, "task_transition",
        {
            "task_id": task_id,
            "action": "completed",
            "expected_version": task.version,
            "complete_steps": True,
        },
    )
    assert stale.success is False and "stale" in stale.message


# ── owner scoping ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_every_step_tool_is_owner_scoped():
    repo = InMemoryTaskRepository()
    _registry, _ctx, executor, manager = _chain(repo)
    created = await _run(
        executor, manager, "todo_add", {"title": "mine", "steps": ["one", "two"]}
    )
    task_id = created.data["task_id"]

    _registry, _ctx, foreign_executor, manager = _chain(repo, owner=OTHER)
    attempts = (
        ("todo_step_list", {"task_id": task_id}),
        ("todo_step_list", {"query": "mine"}),
        ("todo_step_add", {"task_id": task_id, "title": "hijacked"}),
        ("todo_step_transition", {"task_id": task_id, "step": 1, "action": "completed"}),
        ("todo_step_edit", {"task_id": task_id, "step": 1, "title": "hijacked"}),
        ("todo_step_delete", {"task_id": task_id, "step": 1}),
        ("task_transition", {"query": "mine", "action": "completed"}),
        ("task_transition", {"query": "mine", "action": "completed", "complete_steps": True}),
        ("task_delete", {"query": "mine"}),
    )
    for name, arguments in attempts:
        result = await _run(foreign_executor, manager, name, arguments, owner=OTHER)
        assert result.success is False, name
    stored = await repo.get_task(OWNER, task_id)
    assert stored.label == "mine" and stored.status == "active"
    steps = await repo.list_steps(OWNER, task_id)
    assert [s.title for s in steps] == ["one", "two"]
    assert all(s.status == "active" for s in steps)


# ── the structured (JSON) action path ────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_json_action_path_resolves_the_step_actions():
    from backend.ai.actions import EXECUTABLE_ACTION_NAMES, parse_action_text

    for name in (
        "todo_step_add",
        "todo_step_list",
        "todo_step_transition",
        "todo_step_edit",
        "todo_step_delete",
    ):
        assert name in EXECUTABLE_ACTION_NAMES

    add = parse_action_text(
        '{"action":"todo_step_add","query":"پروژه دانشگاه","steps":["a","b"]}'
    )
    assert add.tool_calls == [
        {"name": "todo_step_add", "arguments": {"steps": ["a", "b"], "query": "پروژه دانشگاه"}}
    ]

    one = parse_action_text(
        '{"action":"todo_step_add","task_id":3,"title":"a"}'
    )
    assert one.tool_calls == [
        {"name": "todo_step_add", "arguments": {"steps": ["a"], "task_id": 3}}
    ]

    listing = parse_action_text('{"action":"todo_step_list","query":"x"}')
    assert listing.tool_calls == [{"name": "todo_step_list", "arguments": {"query": "x"}}]

    complete = parse_action_text(
        '{"action":"todo_step_transition","query":"x","step":2,"action_status":"completed"}'
    )
    assert complete.tool_calls == [
        {
            "name": "todo_step_transition",
            "arguments": {"query": "x", "step": 2, "action": "completed"},
        }
    ]

    edit = parse_action_text(
        '{"action":"todo_step_edit","task_id":3,"step_query":"docs","title":"notes"}'
    )
    assert edit.tool_calls == [
        {
            "name": "todo_step_edit",
            "arguments": {"task_id": 3, "step_query": "docs", "title": "notes"},
        }
    ]

    delete = parse_action_text('{"action":"todo_step_delete","query":"x","step":1}')
    assert delete.tool_calls == [
        {"name": "todo_step_delete", "arguments": {"query": "x", "step": 1}}
    ]

    multi = parse_action_text(
        '{"action":"todo_add","title":"پروژه","steps":["one","two"]}'
    )
    assert multi.tool_calls == [
        {"name": "todo_add", "arguments": {"title": "پروژه", "steps": ["one", "two"]}}
    ]

    finish = parse_action_text(
        '{"action":"task_transition","query":"پروژه","action_status":"completed",'
        '"complete_steps":true}'
    )
    assert finish.tool_calls == [
        {
            "name": "task_transition",
            "arguments": {
                "query": "پروژه",
                "action": "completed",
                "complete_steps": True,
            },
        }
    ]

    # Invalid shapes are rejected locally, never executed.
    # An explicitly empty CREATE step list is not a contradiction: it is the
    # same no-step todo a request without steps makes.
    assert parse_action_text('{"action":"todo_add","title":"x","steps":[]}').tool_calls == [
        {"name": "todo_add", "arguments": {"title": "x"}}
    ]
    for payload in (
        '{"action":"todo_step_add","query":"x"}',
        '{"action":"todo_step_add","query":"x","steps":[]}',
        '{"action":"todo_step_transition","query":"x","action_status":"completed"}',
        '{"action":"todo_step_transition","query":"x","step":1,"action_status":"paused"}',
        '{"action":"todo_step_delete","query":"x","step":1,"expected_version":2}',
        '{"action":"task_transition","query":"x","action_status":"active","complete_steps":true}',
    ):
        assert parse_action_text(payload).kind == "invalid", payload


@pytest.mark.asyncio
async def test_the_json_action_path_executes_through_the_executor():
    from backend.ai.actions import parse_action_text

    repo = InMemoryTaskRepository()
    _registry, _ctx, executor, manager = _chain(repo)
    action = parse_action_text(
        '{"action":"todo_add","title":"پروژه دانشگاه","steps":["a","b"]}'
    )
    first_call = action.tool_calls[0]
    result = await _run(executor, manager, first_call["name"], first_call["arguments"])
    assert result.success is True
    task_id = result.data["task_id"]
    assert [s.title for s in await repo.list_steps(OWNER, task_id)] == ["a", "b"]

    step_action = parse_action_text(
        f'{{"action":"todo_step_transition","task_id":{task_id},"step":1,'
        '"action_status":"completed"}'
    )
    step_call = step_action.tool_calls[0]
    step_result = await _run(executor, manager, step_call["name"], step_call["arguments"])
    assert step_result.success is True
    assert (await repo.list_steps(OWNER, task_id))[0].status == "completed"
