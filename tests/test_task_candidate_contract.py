"""Regression tests for the create_task provider→candidate contract.

Production evidence (Render): the provider answered successfully with a JSON
candidate, but every action was rejected with
``TASK_INTERPRET_REJECTED reason=candidate_invalid
detail=each action requires a tool name``.

Root cause: the provider-facing CANDIDATE_SCHEMA declared ``actions`` as a
bare array (no item shape) while the validator requires
``{"name": ..., "arguments": ...}`` objects — the model was never told the
action-object contract and models commonly emit ``tool``/``parameters``-style
action keys. The fix makes the schema/prompt state the exact action object
form and lets the validator normalize the same field-name aliases the
execution layer already accepts (task_execution.py reads ``name or tool``,
``arguments or parameters``).

These tests exercise the REAL parser/interpreter/tool/repository/scheduler
path with a scripted provider (no network) and assert both directions:
alias-shaped valid candidates are accepted end-to-end, and genuinely
malformed candidates are still rejected before persistence.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import patch

import pytest

from backend.ai.database import manager as dbm
from backend.ai.database.task_repository import InMemoryTaskRepository
from backend.ai.providers.base.capabilities import ProviderCapabilities
from backend.ai.providers.base.config import ProviderConfig
from backend.ai.providers.base.contract import BaseProvider, ProviderResponse
from backend.ai.providers.manager.manager import ProviderManager
from backend.ai.scheduling import parse_schedule
from backend.ai.task_candidate import TaskCandidate, TaskCandidateError, parse_candidate_output
from backend.ai.task_interpreter import CANDIDATE_SCHEMA, TaskInterpreter, TaskInterpretationError

REF = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)

# The exact production request and its intended semantics.
PRODUCTION_REQUEST = "یه تسک بساز هر سه دقیقه بگو پری کوچولو هستم"
PRODUCTION_ACTION_TEXT = "پری کوچولو هستم"


# ── Candidate-layer contract ──


def test_alias_shaped_action_is_normalized_and_accepted():
    value = {
        "label": "say hello",
        "schedule_type": "interval",
        "schedule": {"seconds": 180},
        "timezone": "UTC",
        "actions": [{"tool": "send_message", "parameters": {"text": "hello"}}],
        "notification_destination": {},
    }
    candidate = parse_candidate_output(value)
    assert isinstance(candidate, TaskCandidate)
    assert candidate.actions == [{"name": "send_message", "arguments": {"text": "hello"}}]


def test_singular_action_alias_is_normalized_and_accepted():
    value = {
        "label": "say hello",
        "schedule_type": "interval",
        "schedule": {"seconds": 180},
        "timezone": "UTC",
        "action": [{"name": "send_message", "arguments": {"text": "سلام"}}],
        "notification_destination": {},
    }
    candidate = parse_candidate_output(value)
    assert candidate.actions == [{"name": "send_message", "arguments": {"text": "سلام"}}]


def test_args_alias_is_normalized_and_accepted():
    value = {
        "label": "say hello",
        "schedule_type": "interval",
        "schedule": {"seconds": 60},
        "timezone": "UTC",
        "actions": [{"name": "send_message", "args": {"text": "hi"}}],
        "notification_destination": {},
    }
    candidate = parse_candidate_output(value)
    assert candidate.actions == [{"name": "send_message", "arguments": {"text": "hi"}}]


def test_genuinely_malformed_actions_are_still_rejected():
    for bad_actions in ([{"nope": 1}], [{"name": "  "}], ["send_message"], []):
        value = {
            "label": "x",
            "schedule_type": "interval",
            "schedule": {"seconds": 60},
            "timezone": "UTC",
            "actions": bad_actions,
            "notification_destination": {},
        }
        with pytest.raises(TaskCandidateError):
            parse_candidate_output(value)


def test_unsendable_action_content_is_still_rejected():
    value = {
        "label": "x",
        "schedule_type": "interval",
        "schedule": {"seconds": 60},
        "timezone": "UTC",
        "actions": [{"name": "send_message", "arguments": {"text": "   "}}],
        "notification_destination": {},
    }
    with pytest.raises(TaskCandidateError):
        parse_candidate_output(value)


def test_provider_schema_declares_the_action_object_contract():
    items = CANDIDATE_SCHEMA["properties"]["actions"]
    assert items["type"] == "array"
    assert items["minItems"] == 1
    item_schema = items["items"]
    assert item_schema["required"] == ["name", "arguments"]
    assert set(item_schema["properties"]) == {"name", "arguments"}


def test_provider_schema_documents_the_schedule_shapes():
    schedule_schema = CANDIDATE_SCHEMA["properties"]["schedule"]
    description = schedule_schema["description"]
    assert "'seconds'" in description
    assert "0=Monday" in description


def _candidate_with_schedule(schedule: dict[str, Any]) -> dict[str, Any]:
    return {
        "label": "say hello",
        "schedule_type": "interval",
        "schedule": schedule,
        "timezone": "UTC",
        "actions": [{"name": "send_message", "arguments": {"text": "hi"}}],
        "notification_destination": {},
    }


@pytest.mark.parametrize("schedule,expected_seconds", [
    ({"seconds": 180}, 180.0),
    ({"minutes": 3}, 180.0),
    ({"minute": 3}, 180.0),
    ({"mins": 3}, 180.0),
    ({"hours": 1}, 3600.0),
    ({"hour": 2}, 7200.0),
    ({"hrs": 0.5}, 1800.0),
    ({"days": 1}, 86400.0),
    ({"weeks": 2}, 1209600.0),
    ({"minutes": 1.5}, 90.0),
])
def test_unit_keyed_interval_schedules_are_canonicalized(schedule, expected_seconds):
    candidate = parse_candidate_output(_candidate_with_schedule(schedule))
    parsed = parse_schedule(candidate.schedule_type, candidate.schedule)
    assert parsed.interval == timedelta(seconds=expected_seconds)


@pytest.mark.parametrize("schedule", [
    {"minutes": 3, "hours": 1},
    {"minutes": "سه"},
    {"minutes": True},
    {"minutes": 0},
    {"hours": -1},
    {"minutes": [3]},
])
def test_invalid_unit_keyed_interval_schedules_are_still_rejected(schedule):
    with pytest.raises(TaskCandidateError):
        parse_candidate_output(_candidate_with_schedule(schedule))


# ── (value, unit) pair shapes: {"interval": 3, "unit": "minutes"} ──


@pytest.mark.parametrize("schedule,expected_seconds", [
    ({"interval": 3, "unit": "minutes"}, 180.0),
    ({"every": 5, "unit": "minutes"}, 300.0),
    ({"interval": 1, "unit": "minute"}, 60.0),
    ({"interval": 2, "unit": "hours"}, 7200.0),
    ({"interval": "10", "unit": "minutes"}, 600.0),
    ({"interval": "۳", "unit": "minutes"}, 180.0),
    ({"interval": 3, "unit": "دقیقه"}, 180.0),
    ({"interval": 3, "unit": "Minutes"}, 180.0),
    ({"interval": 1, "unit": "seconds"}, 1.0),
    ({"interval": 3, "unit": "minutes", "timezone": "UTC"}, 180.0),
    ({"minutes": 3, "timezone": "UTC"}, 180.0),
    ({"interval_minutes": 3}, 180.0),
    ({"every_hours": 2}, 7200.0),
    ({"seconds": "180"}, 180.0),
])
def test_semantically_equivalent_interval_shapes_are_canonicalized(schedule, expected_seconds):
    candidate = parse_candidate_output(_candidate_with_schedule(schedule))
    assert candidate.schedule == {"seconds": expected_seconds}


@pytest.mark.parametrize("schedule", [
    {"interval": 3, "unit": "fortnights"},
    {"interval": 3, "unit": 5},
    {"interval": 3},
    {"unit": "minutes"},
    {"interval": 3, "unit": "minutes", "label": "x"},
    {"interval": 0, "unit": "minutes"},
    {"interval": -3, "unit": "minutes"},
    {"interval": "سه", "unit": "minutes"},
    {"interval": True, "unit": "minutes"},
    {"interval": [3], "unit": "minutes"},
    {"interval": 3, "interval2": 5, "unit": "minutes"},
])
def test_ambiguous_or_invalid_pair_schedules_are_still_rejected(schedule):
    with pytest.raises(TaskCandidateError):
        parse_candidate_output(_candidate_with_schedule(schedule))


def test_unrecognized_schedule_shape_rejection_carries_structure():
    """The production diagnostic: an unrecognized interval shape is rejected
    by parse_schedule with the schedule structure embedded, so one Render
    occurrence identifies the exact provider shape (keys/types only)."""
    with pytest.raises(TaskCandidateError) as excinfo:
        parse_candidate_output(_candidate_with_schedule({"fortnights": 2}))
    message = str(excinfo.value)
    assert "malformed schedule payload" in message
    assert "keys=fortnights" in message
    assert "has_seconds=false" in message
    assert "unit_key=false" in message
    assert "nested=false" in message


def test_rejection_structure_reports_types_and_seconds():
    with pytest.raises(TaskCandidateError) as excinfo:
        parse_candidate_output(_candidate_with_schedule(
            {"seconds": "abc", "when": {"x": 1}}
        ))
    message = str(excinfo.value)
    assert "keys=seconds,when" in message
    assert "types=str,dict" in message
    assert "has_seconds=true" in message
    assert "nested=true" in message


def test_once_daily_weekly_validation_remains_intact():
    base = {
        "label": "check", "timezone": "UTC",
        "actions": [{"name": "send_message", "arguments": {"text": "hi"}}],
        "notification_destination": {},
    }
    daily = parse_candidate_output(base | {
        "schedule_type": "daily", "schedule": {"hour": 9, "timezone": "UTC"},
    })
    assert daily.schedule == {"hour": 9, "timezone": "UTC"}
    with pytest.raises(TaskCandidateError):
        parse_candidate_output(base | {"schedule_type": "daily", "schedule": {"hour": 25, "timezone": "UTC"}})
    with pytest.raises(TaskCandidateError):
        parse_candidate_output(base | {
            "schedule_type": "weekly",
            "schedule": {"weekday": 9, "hour": 9, "timezone": "UTC"},
        })
    # Interval schedules carry no timezone (original contract): a stray tz
    # key is dropped by canonicalization, not silently turned into a
    # timezone-dependent schedule.
    interval_with_tz = parse_candidate_output(base | {
        "schedule_type": "interval", "schedule": {"seconds": 180, "timezone": "UTC"},
    })
    assert interval_with_tz.schedule == {"seconds": 180.0}


# ── Interpreter end-to-end (scripted provider, real parser) ──


class _ScriptedProvider(BaseProvider):
    def __init__(self, name: str, response_text: str) -> None:
        super().__init__(ProviderConfig(provider_name=name, enabled=True, default_model="m"))
        self._name = name
        self._response_text = response_text
        self.last_messages: list[dict[str, Any]] | None = None

    @property
    def name(self) -> str:
        return self._name

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(supports_tools=True, supports_function_call=True)

    async def chat(self, messages, **kwargs):
        self.last_messages = list(messages)
        return ProviderResponse(text=self._response_text, provider_name=self._name, success=True)

    def initialize(self) -> None:
        return None

    def shutdown(self) -> None:
        return None

    def count_tokens(self, text: str) -> int:
        return max(1, len(text) // 4)

    def health(self) -> dict[str, Any]:
        return {"healthy": True}


# The exact production failure shape: singular `action` + `tool`/`parameters`
# keys — a compliant candidate under the aliases the model actually emits.
_PRODUCTION_SHAPED_CANDIDATE = json.dumps({
    "label": "پری کوچولو",
    "schedule_type": "interval",
    "schedule": {"seconds": 180},
    "timezone": "UTC",
    "action": [{"tool": "send_message", "parameters": {"text": "پری کوچولو هستم"}}],
    "notification_destination": {},
}, ensure_ascii=False)


@pytest.mark.asyncio
async def test_provider_alias_output_yields_valid_candidate():
    provider = _ScriptedProvider("fake", _PRODUCTION_SHAPED_CANDIDATE)
    manager = ProviderManager()
    manager.register_provider(provider)
    manager.switch_provider("fake")
    manager._fallback_chain = []

    candidate = await TaskInterpreter(manager).interpret(
        "یه تسک بساز هر سه دقیقه بگو پری کوچولو هستم", timezone="UTC", request_id="rid-alias"
    )
    assert candidate.actions == [{"name": "send_message", "arguments": {"text": "پری کوچولو هستم"}}]
    assert candidate.schedule_type == "interval"


@pytest.mark.asyncio
async def test_interpreter_prompt_states_the_action_object_contract():
    provider = _ScriptedProvider("fake", _PRODUCTION_SHAPED_CANDIDATE)
    manager = ProviderManager()
    manager.register_provider(provider)
    manager.switch_provider("fake")
    manager._fallback_chain = []

    await TaskInterpreter(manager).interpret("هر ۳ دقیقه بنویس سلام", timezone="UTC")
    assert provider.last_messages is not None
    system_blob = " ".join(
        str(m.get("content")) for m in provider.last_messages if m.get("role") == "system"
    )
    assert "'name'" in system_blob and "'arguments'" in system_blob
    assert '"actions"' in system_blob


@pytest.mark.asyncio
async def test_unfixable_candidate_still_rejected_without_persistence():
    provider = _ScriptedProvider("fake", json.dumps({
        "label": "x", "schedule_type": "interval", "schedule": {"seconds": 60},
        "timezone": "UTC", "actions": [{"nope": 1}], "notification_destination": {},
    }))
    manager = ProviderManager()
    manager.register_provider(provider)
    manager.switch_provider("fake")
    manager._fallback_chain = []

    with pytest.raises(TaskInterpretationError) as excinfo:
        await TaskInterpreter(manager).interpret("every minute say hi", timezone="UTC")
    assert isinstance(excinfo.value.__cause__, TaskCandidateError)


# ── Tool → persistence → scheduler discovery (real pipeline) ──


def _tool_context(provider_manager, owner_id=777):
    from backend.ai.tools.context import ToolContext

    return ToolContext(
        telegram=None, owner_id=owner_id, tz_str="UTC", client=None,
        extra={"provider_manager": provider_manager, "chat_id": -1001,
               "request_id": "rid-e2e"},
    )


@pytest.mark.asyncio
async def test_alias_candidate_persists_and_is_discoverable_by_scheduler():
    from backend.ai.task_scheduler import TaskScheduler
    from backend.ai.tools.task import CreateTaskTool

    pm = ProviderManager()
    provider = _ScriptedProvider("fake", _PRODUCTION_SHAPED_CANDIDATE)
    pm.register_provider(provider)
    pm.switch_provider("fake")
    pm._fallback_chain = []

    manager = dbm.RepositoryManager(supabase_available=False)
    repo = manager.task
    assert isinstance(repo, InMemoryTaskRepository)
    with patch.object(dbm, "get_repository_manager", return_value=manager):
        tool = CreateTaskTool(_tool_context(pm))
        result = await tool.execute(_tool_context(pm), {
            "request": "یه تسک بساز هر سه دقیقه بگو پری کوچولو هستم",
        })

    assert result.success is True, result.message
    task = await repo.get_task(777, result.data["task_id"])
    assert task is not None
    assert task.owner_id == 777
    assert task.schedule_type == "interval"
    assert parse_schedule(task.schedule_type, task.schedule).interval == timedelta(seconds=180)
    # Canonical action survives persistence: send_message with bounded text.
    assert task.actions == [{"name": "send_message", "arguments": {"text": "پری کوچولو هستم"}}]
    assert task.status == "active"
    assert task.next_run_at is not None

    # Immediate discovery: the scheduler reads due tasks from the repository
    # on every wake (run() -> run_once() -> list_due_tasks); no refresh or
    # restart is required. At next_run_at the task must produce an occurrence.
    scheduler = TaskScheduler(repo, 777)
    processed = await scheduler.run_once(task.next_run_at + timedelta(seconds=1))
    assert processed >= 1
    occurrences = await repo.list_occurrences(777, task_id=task.id)
    assert len(occurrences) == 1
    assert occurrences[0].action_snapshot == task.actions
    # The recurring task advanced to its next interval occurrence.
    refreshed = await repo.get_task(777, task.id)
    assert refreshed.next_run_at > task.next_run_at


@pytest.mark.asyncio
async def test_interpretation_failure_still_reports_persisted_false():
    from backend.ai.tools.task import CreateTaskTool

    pm = ProviderManager()
    provider = _ScriptedProvider("fake", json.dumps(None))
    pm.register_provider(provider)
    pm.switch_provider("fake")
    pm._fallback_chain = []

    manager = dbm.RepositoryManager(supabase_available=False)
    with patch.object(dbm, "get_repository_manager", return_value=manager):
        tool = CreateTaskTool(_tool_context(pm))
        result = await tool.execute(_tool_context(pm), {"request": "every minute say hi"})

    assert result.success is False
    repo = manager.task
    assert await repo.list_tasks(777) == []


# ── Unknown/arbitrary tools stay rejected end-to-end ──


@pytest.mark.asyncio
async def test_unknown_tool_candidate_is_rejected_by_candidate_validation():
    # Non-send action names pass the candidate boundary (the candidate layer
    # does not own the registry) only if well-formed; a name-less action
    # never does.
    with pytest.raises(TaskCandidateError):
        parse_candidate_output(_candidate_with_schedule({"seconds": 180}) | {
            "actions": [{"name": "", "arguments": {}}],
        })


@pytest.mark.asyncio
async def test_registered_non_send_tool_survives_but_unknown_tool_fails_execution():
    """The safety chain: candidate validation accepts well-formed non-send
    actions, but a tool that is NOT in the ToolRegistry fails at execution
    with ``unregistered_action`` and the occurrence is marked failed."""
    from backend.ai.task_execution import TaskExecutionCoordinator
    from backend.ai.tools.context import ToolContext
    from backend.ai.tools.executor import ToolExecutor
    from backend.ai.tools.registry import ToolRegistry

    value = _candidate_with_schedule({"seconds": 180})
    value["actions"] = [{"name": "mystery_tool", "arguments": {"x": 1}}]
    candidate = parse_candidate_output(value)
    assert candidate.actions == [{"name": "mystery_tool", "arguments": {"x": 1}}]

    repo = InMemoryTaskRepository()
    task = await repo.create_task(5, candidate.as_creation_candidate() | {
        "next_run_at": REF + timedelta(seconds=1),
    })
    await repo.create_occurrence(5, {
        "task_id": task.id,
        "occurrence_key": f"{task.id}:once",
        "definition_version": task.version,
        "action_snapshot": task.actions,
        "scheduled_for": task.next_run_at,
    })
    await repo.claim_occurrence(5, task.id, f"{task.id}:once")
    ctx = ToolContext(None, 5, "UTC")
    result = await TaskExecutionCoordinator(
        repo, ToolExecutor(ToolRegistry(), ctx), 5, ctx
    ).execute(await repo.get_occurrence(5, task.id, f"{task.id}:once"))
    assert not result.success
    occurrence = await repo.get_occurrence(5, task.id, f"{task.id}:once")
    assert occurrence.status == "failed"
    assert occurrence.error_metadata.get("error_class") == "unregistered_action"


# ── Production-shaped replay: REAL interpreter → tool → repository ──


@pytest.mark.asyncio
async def test_production_request_persists_from_value_unit_provider_shape():
    """The exact production request through the REAL TaskInterpreter with a
    provider emitting the (value, unit) shape {"interval": 3,
    "unit": "minutes"} — deterministic replay of the observed failure class,
    no network."""
    from backend.ai.tools.task import CreateTaskTool

    candidate_json = json.dumps({
        "label": "پری کوچولو",
        "schedule_type": "interval",
        "schedule": {"interval": 3, "unit": "minutes"},
        "timezone": "UTC",
        "actions": [{"name": "send_message", "arguments": {"text": PRODUCTION_ACTION_TEXT}}],
        "notification_destination": {},
    }, ensure_ascii=False)

    pm = ProviderManager()
    provider = _ScriptedProvider("fake", candidate_json)
    pm.register_provider(provider)
    pm.switch_provider("fake")
    pm._fallback_chain = []

    manager = dbm.RepositoryManager(supabase_available=False)
    repo = manager.task
    with patch.object(dbm, "get_repository_manager", return_value=manager):
        tool = CreateTaskTool(_tool_context(pm))
        result = await tool.execute(_tool_context(pm), {"request": PRODUCTION_REQUEST})

    assert result.success is True, result.message
    task = await repo.get_task(777, result.data["task_id"])
    assert task.owner_id == 777
    assert task.schedule_type == "interval"
    assert task.schedule == {"seconds": 180}
    assert task.actions == [{"name": "send_message", "arguments": {"text": PRODUCTION_ACTION_TEXT}}]
    assert task.status == "active"
    assert task.next_run_at is not None


@pytest.mark.asyncio
async def test_interpretation_rejection_records_schedule_structure():
    """An unrecognized schedule shape surfaces its structure in the
    rejection so the next production occurrence identifies the exact shape."""
    candidate_json = json.dumps({
        "label": "x", "schedule_type": "interval",
        "schedule": {"fortnights": 2}, "timezone": "UTC",
        "actions": [{"name": "send_message", "arguments": {"text": "hi"}}],
        "notification_destination": {},
    })
    pm = ProviderManager()
    provider = _ScriptedProvider("fake", candidate_json)
    pm.register_provider(provider)
    pm.switch_provider("fake")
    pm._fallback_chain = []

    with pytest.raises(TaskInterpretationError) as excinfo:
        await TaskInterpreter(pm).interpret(PRODUCTION_REQUEST, timezone="UTC")
    message = str(excinfo.value.__cause__)
    assert "malformed schedule payload" in message
    assert "keys=fortnights" in message
