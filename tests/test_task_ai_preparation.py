"""Occurrence-time AI preparation tests (dormant-contract activation).

Covers the confirmed architectural gap closed by this change: tasks that
persist an ``ai_instruction`` now resolve their arguments through the
existing ProviderManager at occurrence time, while static tasks keep the
exact deterministic zero-provider path. The model is never an execution
authority: prepared calls must keep the task's own tool names and still
flow through the SAME ToolExecutor, and every preparation failure is
fail-closed through the existing retry/interrupted semantics.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from backend.ai.database.task_repository import InMemoryTaskRepository
from backend.ai.task_execution import (
    AIActionPreparator,
    TaskExecutionCoordinator,
    TaskPreparationError,
)
from backend.ai.tools.base import PermissionLevel, ToolResult
from backend.ai.tools.context import ToolContext
from backend.ai.tools.executor import ToolExecutor
from backend.ai.tools.registry import ToolRegistry

OWNER = 42


class RecordingTool:
    permission_level = PermissionLevel.READ_WRITE
    long_running = False
    safe = True
    return_type = "object"
    description = "test tool"
    parameters = {}

    def __init__(self, name: str, calls: list, success: bool = True) -> None:
        self.name = name
        self.calls = calls
        self.success = success

    async def execute(self, context, arguments):
        self.calls.append((self.name, dict(arguments), context.owner_id))
        return ToolResult(self.success, "ok" if self.success else "failed")


class FakePreparator:
    """Test double standing in for the AIActionPreparator seam."""

    def __init__(self, prepared=None, error: Exception | None = None) -> None:
        self.prepared = prepared or []
        self.error = error
        self.requests: list[tuple[str, list]] = []

    async def prepare(self, instruction, templates, *, owner_id, tz_str):
        self.requests.append((instruction, list(templates)))
        if self.error is not None:
            raise self.error
        return [dict(action) for action in self.prepared]


def task_data(**overrides):
    data = {
        "label": "ai task",
        "schedule_type": "once",
        "schedule": {"at": "2026-09-01T12:00:00", "timezone": "UTC"},
        "timezone": "UTC",
        "actions": [{"name": "bio_set_text", "arguments": {}}],
        "notification_destination": {},
    }
    data.update(overrides)
    return data


async def _claimed_occurrence(repo, task, actions=None, key="k"):
    occurrence = await repo.create_occurrence(OWNER, {
        "task_id": task.id,
        "occurrence_key": key,
        "definition_version": task.version,
        "action_snapshot": actions if actions is not None else task.actions,
        "scheduled_for": task.next_run_at or datetime.now(timezone.utc),
    })
    claimed = await repo.claim_occurrence(OWNER, task.id, key)
    return occurrence, claimed


def _coordinator(repo, preparator=None):
    registry = ToolRegistry()
    ctx = ToolContext(None, OWNER, "UTC")
    executor = ToolExecutor(registry, ctx)
    coordinator = TaskExecutionCoordinator(repo, executor, OWNER, ctx, preparator=preparator)
    return coordinator, registry


# ── Static tasks: zero provider work, unchanged path ────────────────────────


@pytest.mark.asyncio
async def test_static_task_skips_preparation_and_executes_deterministically():
    repo = InMemoryTaskRepository()
    task = await repo.create_task(OWNER, task_data())
    calls: list = []
    coordinator, registry = _coordinator(repo)
    registry.register(RecordingTool("bio_set_text", calls))
    _, claimed = await _claimed_occurrence(repo, task, [{"name": "bio_set_text", "arguments": {"text": "hello"}}])
    preparator = FakePreparator(error=AssertionError("preparator must not run for static tasks"))

    result = await TaskExecutionCoordinator(
        repo, coordinator.executor, OWNER, ToolContext(None, OWNER, "UTC"),
        preparator=preparator,
    ).execute(claimed)

    assert result.success and result.status == "succeeded"
    assert calls == [("bio_set_text", {"text": "hello"}, OWNER)]
    assert preparator.requests == []


# ── AI-assisted task: preparation through the same executor ─────────────────


@pytest.mark.asyncio
async def test_ai_instruction_resolves_arguments_through_tool_executor():
    repo = InMemoryTaskRepository()
    task = await repo.create_task(OWNER, task_data(
        ai_instruction="Write a fresh one-line bio quote.",
        actions=[{"name": "bio_set_text", "arguments": {}}],
    ))
    calls: list = []
    coordinator, registry = _coordinator(repo)
    registry.register(RecordingTool("bio_set_text", calls))
    preparator = FakePreparator(prepared=[{"name": "bio_set_text", "arguments": {"text": "stay curious"}}])
    _, claimed = await _claimed_occurrence(repo, task)

    result = await TaskExecutionCoordinator(
        repo, coordinator.executor, OWNER, ToolContext(None, OWNER, "UTC"),
        preparator=preparator,
    ).execute(claimed)

    assert result.success and result.status == "succeeded"
    assert calls == [("bio_set_text", {"text": "stay curious"}, OWNER)]
    # The preparator received the task's own action templates.
    assert preparator.requests[0][0] == "Write a fresh one-line bio quote."
    assert preparator.requests[0][1] == [{"name": "bio_set_text", "arguments": {}}]

    stored = await repo.get_occurrence(OWNER, task.id, "k")
    assert stored.status == "succeeded"
    # Audit record: the prepared action is persisted for this occurrence.
    preparation = stored.preparation_metadata
    assert preparation.get("kind") == "prepared_action"
    assert preparation["action"]["name"] == "bio_set_text"
    assert preparation["action"]["arguments"] == {"text": "stay curious"}
    assert preparation["definition_version"] == task.version


@pytest.mark.asyncio
async def test_prepared_call_still_fails_closed_when_tool_unregistered():
    repo = InMemoryTaskRepository()
    task = await repo.create_task(OWNER, task_data(
        ai_instruction="generate arguments",
    ))
    calls: list = []
    coordinator, registry = _coordinator(repo)
    # The tool exists at creation... but the snapshot template check runs
    # BEFORE preparation, so an unregistered template never reaches the model.
    _, claimed = await _claimed_occurrence(repo, task, [{"name": "ghost_tool", "arguments": {}}])
    preparator = FakePreparator(error=AssertionError("preparation must not run for unregistered templates"))

    result = await TaskExecutionCoordinator(
        repo, coordinator.executor, OWNER, ToolContext(None, OWNER, "UTC"),
        preparator=preparator,
    ).execute(claimed)

    assert not result.success
    assert result.error == "unregistered_action"
    assert calls == []
    assert (await repo.get_occurrence(OWNER, task.id, "k")).status == "failed"


# ── Fail-closed preparation failures ────────────────────────────────────────


@pytest.mark.asyncio
async def test_provider_failure_fails_closed_without_execution():
    repo = InMemoryTaskRepository()
    task = await repo.create_task(OWNER, task_data(ai_instruction="write bio"))
    calls: list = []
    coordinator, registry = _coordinator(repo)
    registry.register(RecordingTool("bio_set_text", calls))
    preparator = FakePreparator(error=TaskPreparationError("task preparation provider failed: x"))
    _, claimed = await _claimed_occurrence(repo, task)

    result = await TaskExecutionCoordinator(
        repo, coordinator.executor, OWNER, ToolContext(None, OWNER, "UTC"),
        preparator=preparator,
    ).execute(claimed)

    assert not result.success
    # Pre-existing handle_failure semantics: UNKNOWN failures persist the
    # classifier reason (the raw detail is a log concern, not the contract).
    assert result.error == "unclassified"
    assert calls == []  # nothing executed on a failed preparation
    stored = await repo.get_occurrence(OWNER, task.id, "k")
    assert stored.status == "failed"
    assert stored.error_metadata.get("error_class") == "unclassified"


@pytest.mark.asyncio
async def test_preparation_timeout_is_retryable_under_existing_contract():
    repo = InMemoryTaskRepository()
    task = await repo.create_task(OWNER, task_data(ai_instruction="write bio"))
    calls: list = []
    coordinator, registry = _coordinator(repo)
    registry.register(RecordingTool("bio_set_text", calls))
    preparator = FakePreparator(error=TimeoutError("task preparation provider timed out"))
    _, claimed = await _claimed_occurrence(repo, task)

    result = await TaskExecutionCoordinator(
        repo, coordinator.executor, OWNER, ToolContext(None, OWNER, "UTC"),
        preparator=preparator,
    ).execute(claimed)

    assert not result.success
    assert result.status == "retry_pending"
    assert calls == []
    stored = await repo.get_occurrence(OWNER, task.id, "k")
    assert stored.status == "retry_pending"
    assert stored.attempt == 2
    assert stored.retry_at is not None


@pytest.mark.asyncio
async def test_tool_name_swap_is_rejected_before_execution():
    repo = InMemoryTaskRepository()
    task = await repo.create_task(OWNER, task_data(
        ai_instruction="write bio",
        actions=[{"name": "bio_set_text", "arguments": {}}],
    ))
    calls: list = []
    coordinator, registry = _coordinator(repo)
    registry.register(RecordingTool("bio_set_text", calls))
    registry.register(RecordingTool("delete", calls))
    preparator = FakePreparator(prepared=[{"name": "delete", "arguments": {"mode": "all"}}])
    _, claimed = await _claimed_occurrence(repo, task)

    result = await TaskExecutionCoordinator(
        repo, coordinator.executor, OWNER, ToolContext(None, OWNER, "UTC"),
        preparator=preparator,
    ).execute(claimed)

    assert not result.success
    assert calls == []
    stored = await repo.get_occurrence(OWNER, task.id, "k")
    assert stored.status == "failed"


@pytest.mark.asyncio
async def test_malformed_preparation_output_fails_closed():
    repo = InMemoryTaskRepository()
    task = await repo.create_task(OWNER, task_data(ai_instruction="write bio"))
    calls: list = []
    coordinator, registry = _coordinator(repo)
    registry.register(RecordingTool("bio_set_text", calls))
    preparator = FakePreparator(prepared=[{"name": "bio_set_text", "arguments": "not-a-dict"}])
    _, claimed = await _claimed_occurrence(repo, task)

    result = await TaskExecutionCoordinator(
        repo, coordinator.executor, OWNER, ToolContext(None, OWNER, "UTC"),
        preparator=preparator,
    ).execute(claimed)

    assert not result.success
    assert calls == []
    assert (await repo.get_occurrence(OWNER, task.id, "k")).status == "failed"


@pytest.mark.asyncio
async def test_missing_preparation_authority_fails_closed():
    repo = InMemoryTaskRepository()
    task = await repo.create_task(OWNER, task_data(ai_instruction="write bio"))
    calls: list = []
    coordinator, registry = _coordinator(repo)
    registry.register(RecordingTool("bio_set_text", calls))
    _, claimed = await _claimed_occurrence(repo, task)

    async def _unavailable(*args, **kwargs):
        raise TaskPreparationError("task preparation authority is unavailable")

    class DeadPreparator:
        def prepare(self, *args, **kwargs):
            return _unavailable(*args, **kwargs)

    result = await TaskExecutionCoordinator(
        repo, coordinator.executor, OWNER, ToolContext(None, OWNER, "UTC"),
        preparator=DeadPreparator(),
    ).execute(claimed)

    assert not result.success
    assert calls == []
    assert (await repo.get_occurrence(OWNER, task.id, "k")).status == "failed"


# ── AIActionPreparator unit contract ────────────────────────────────────────


class _StubProviderManager:
    def __init__(self, response) -> None:
        self.response = response
        self.calls: list = []

    async def chat(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        return self.response


def _provider_response(text: str, success: bool = True):
    from backend.ai.providers.base import ProviderResponse
    return ProviderResponse(text=text, provider_name="stub", success=success)


@pytest.mark.asyncio
async def test_preparator_requests_no_tools_and_parses_final_arguments():
    manager = _StubProviderManager(_provider_response(
        '{"actions": [{"name": "bio_set_text", "arguments": {"text": "fresh"}}]}'
    ))
    preparator = AIActionPreparator(manager)
    prepared = await preparator.prepare(
        "write a bio",
        [{"name": "bio_set_text", "arguments": {}}],
        owner_id=OWNER,
        tz_str="UTC",
    )
    assert prepared == [{"name": "bio_set_text", "arguments": {"text": "fresh"}}]
    # The provider can never execute anything: no tool definitions attached.
    assert manager.calls[0][1] == {"tools": []}
    # The instruction and the task's own templates travel to the model.
    user_message = manager.calls[0][0][-1]["content"]
    assert "write a bio" in user_message
    assert "bio_set_text" in user_message


@pytest.mark.asyncio
async def test_preparator_tolerates_markdown_fence():
    manager = _StubProviderManager(_provider_response(
        '```json\n{"actions": [{"name": "bio_set_text", "arguments": {"text": "fenced"}}]}\n```'
    ))
    prepared = await AIActionPreparator(manager).prepare(
        "write a bio",
        [{"name": "bio_set_text", "arguments": {}}],
        owner_id=OWNER,
        tz_str="UTC",
    )
    assert prepared == [{"name": "bio_set_text", "arguments": {"text": "fenced"}}]


@pytest.mark.asyncio
async def test_preparator_rejects_wrong_action_count_and_swapped_names():
    manager = _StubProviderManager(_provider_response(
        '{"actions": [{"name": "other_tool", "arguments": {}}]}'
    ))
    with pytest.raises(TaskPreparationError):
        await AIActionPreparator(manager).prepare(
            "instruction",
            [{"name": "bio_set_text", "arguments": {}}],
            owner_id=OWNER,
            tz_str="UTC",
        )

    count_manager = _StubProviderManager(_provider_response('{"actions": []}'))
    with pytest.raises(TaskPreparationError):
        await AIActionPreparator(count_manager).prepare(
            "instruction",
            [{"name": "bio_set_text", "arguments": {}}],
            owner_id=OWNER,
            tz_str="UTC",
        )


@pytest.mark.asyncio
async def test_preparator_rejects_failed_provider_and_prose():
    failing = _StubProviderManager(_provider_response("not available", success=False))
    with pytest.raises(TaskPreparationError):
        await AIActionPreparator(failing).prepare(
            "instruction", [{"name": "bio_set_text", "arguments": {}}],
            owner_id=OWNER, tz_str="UTC",
        )

    prose = _StubProviderManager(_provider_response("Here is your bio text!"))
    with pytest.raises(TaskPreparationError):
        await AIActionPreparator(prose).prepare(
            "instruction", [{"name": "bio_set_text", "arguments": {}}],
            owner_id=OWNER, tz_str="UTC",
        )


@pytest.mark.asyncio
async def test_default_preparator_resolves_process_provider_manager():
    from backend.ai.engine import engine as engine_module

    class _FakeEngine:
        provider_manager = object()

    import backend.ai.task_execution as te

    original = te._default_preparator
    try:
        # Patch the engine accessor to prove the default path resolves the
        # process ProviderManager (single authority) without constructing one.
        async def _noop():
            return None

        def _fake_get_engine():
            return _FakeEngine()

        import unittest.mock as mock

        with mock.patch.object(engine_module, "get_engine", _fake_get_engine):
            preparator = te._default_preparator()
        assert preparator is not None
        assert isinstance(preparator, AIActionPreparator)
    finally:
        te._default_preparator = original
