"""Durable action-chain waiting (Todo Part 3B) — a chain pauses at a future instant.

The product contract under test: one durable task's ordered actions may carry a
per-action wait boundary (``not_before``); the chain runs the actions whose
boundary has arrived, and when it reaches one that has not, the SAME occurrence
parks on that exact instant — as the existing durable eligibility pair
(``retry_pending`` + ``retry_at``) — and resumes there when the single wake loop
serves it. The waiting action stays ``pending`` (a wait is eligibility, never a
new action state), a wait consumes no attempt, no occurrence is duplicated, and
the requested instant is never shifted.

Boundaries preserved by these tests: one scheduler (``TaskScheduler``), one
execution authority (``TaskExecutionCoordinator`` → ``ToolExecutor``), the
ToolRegistry as the capability allowlist, no schema change, and the existing
crash-safety contract (a running occurrence is still resolved by recovery; a
parked wait is not a running action).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from backend.ai.database.task_repository import InMemoryTaskRepository
from backend.ai.task_candidate import TaskCandidate, TaskCandidateError
from backend.ai.task_contract import (
    MAX_WAIT_AHEAD_SECONDS,
    MAX_WAIT_CHARS,
    WAIT_KEY,
    TaskContractError,
    resolve_action_waits,
    resolve_wait_boundary,
)
from backend.ai.task_creation import TaskCreationError, TaskCreationService
from backend.ai.task_execution import TaskExecutionCoordinator
from backend.ai.task_interpreter import CANDIDATE_SCHEMA
from backend.ai.task_scheduler import TaskScheduler
from backend.ai.tools.base import PermissionLevel, ToolResult
from backend.ai.tools.context import ToolContext
from backend.ai.tools.executor import ToolExecutor
from backend.ai.tools.registry import ToolRegistry

OWNER = 4242
OTHER_OWNER = 999
#: The chain starts at 14:00 and its last action waits until 18:00 — the
#: exact scenario of the phase (SEARCH → SAVE → TAG → WAIT → DELIVER).
START = datetime(2026, 9, 26, 14, 0, tzinfo=timezone.utc)
BOUNDARY = datetime(2026, 9, 26, 18, 0, tzinfo=timezone.utc)
BOUNDARY_ISO = "2026-09-26T18:00:00+00:00"
NOW = START


# ── Registry/executor/coordinator harness (the Phase 3A doubles) ────────────


class ChainTool:
    """A registered tool double whose calls, results and chainable fields the
    test declares; the real ToolRegistry and ToolExecutor still own it."""

    permission_level = PermissionLevel.READ_WRITE
    long_running = False
    safe = True
    description = "chain test tool"
    parameters = {}
    return_type = "object"

    def __init__(self, name, calls, *, data=None, fields=(), plan=None, events=None):
        self.name = name
        self.calls = calls
        self.data = dict(data or {})
        self.consumable_output_fields = tuple(fields)
        self._plan = list(plan or [])
        self._events = events if events is not None else []
        self._runs = 0

    async def execute(self, context, arguments):
        self._events.append(("start", self.name))
        self.calls.append({
            "name": self.name,
            "arguments": dict(arguments),
            "owner": context.owner_id,
            "scheduled": bool((context.extra or {}).get("scheduled_occurrence")),
        })
        entry = self._plan[self._runs] if self._runs < len(self._plan) else None
        self._runs += 1
        self._events.append(("end", self.name))
        if isinstance(entry, BaseException):
            raise entry
        return entry if entry is not None else ToolResult(True, "ok", dict(self.data))


class CountingExecutor(ToolExecutor):
    """The real executor, remembering the batches it was asked to run."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.batches = []

    async def execute_calls(self, tool_calls, **kwargs):
        self.batches.append([dict(call) for call in tool_calls])
        return await super().execute_calls(tool_calls, **kwargs)


def _task_payload(actions, *, timezone_name="UTC", schedule=None):
    return {
        "label": "durable wait",
        "schedule_type": "once",
        "schedule": schedule or {"at": "2027-01-01T09:00:00", "timezone": timezone_name},
        "timezone": timezone_name,
        "actions": actions,
        "notification_destination": {},
    }


async def _start(repo, actions, *, key="k", owner=OWNER, timezone_name="UTC"):
    """Create the task + claimed occurrence this chain will execute."""
    task = await repo.create_task(owner, _task_payload(actions, timezone_name=timezone_name))
    await repo.create_occurrence(owner, {
        "task_id": task.id,
        "occurrence_key": key,
        "definition_version": task.version,
        "action_snapshot": actions,
        "scheduled_for": NOW,
    })
    claimed = await repo.claim_occurrence(owner, task.id, key)
    return task, claimed


def _coordinator(repo, owner=OWNER, registry=None, executor_factory=ToolExecutor):
    ctx = ToolContext(None, owner, "UTC")
    registry = registry if registry is not None else ToolRegistry()
    executor = executor_factory(registry, ctx)
    return TaskExecutionCoordinator(repo, executor, owner, ctx), executor


async def _stored(repo, task_id, key="k", owner=OWNER):
    return await repo.get_occurrence(owner, task_id, key)


def _chain_registry(calls, *, events=None, save_code="S0042", plan=None):
    """web_search / save_by_link / update_save_tags / send_message doubles.

    They mirror the real registered tools' names and argument contracts and
    stand in for the services they wrap; only the services are doubled — the
    registry, the executor, the coordinator, the reference resolution and the
    durable occurrence state are the real code.
    """
    registry = ToolRegistry()
    registry.register(ChainTool(
        "web_search", calls, events=events,
        data={"top_title": "University closed tomorrow", "top_url": "https://t.me/c/1/2"},
        fields=("top_title", "top_url"),
    ))
    registry.register(ChainTool(
        "save_by_link", calls, events=events,
        data={"save_code": save_code}, fields=("save_code",),
    ))
    registry.register(ChainTool(
        "update_save_tags", calls, events=events,
        data={"save_code": save_code, "summary": f"saved {save_code}"},
        fields=("save_code", "summary"),
    ))
    registry.register(ChainTool(
        "send_message", calls, events=events,
        data={"text": "delivered"}, fields=("text",), plan=plan,
    ))
    return registry


def _deliver_chain(*, boundary=None):
    """SEARCH → SAVE → TAG → DELIVER, with the optional wait on DELIVER."""
    actions = [
        {"name": "web_search", "arguments": {"query": "دانشگاه فردا تعطیل"}},
        {"name": "save_by_link", "arguments": {
            "link": {"$ref": {"action": 1, "field": "top_url"}},
        }},
        {"name": "update_save_tags", "arguments": {
            "save_code": {"$ref": {"action": 2, "field": "save_code"}},
            "tags": ["دانشگاه"], "mode": "add",
        }},
        {"name": "send_message", "arguments": {
            "text": {"$ref": {"action": 3, "field": "summary"}},
            "save_code": {"$ref": {"action": 2, "field": "save_code"}},
        }},
    ]
    if boundary is not None:
        actions[3][WAIT_KEY] = boundary
    return actions


def _names(calls):
    return [call["name"] for call in calls]


# ── 1/2/3: the wait-boundary contract (shape, timezone, bounds, order) ──────


def test_a_naive_boundary_is_the_tasks_local_wall_clock_time():
    local = resolve_wait_boundary("2026-09-26T18:00:00", timezone_name="Asia/Tehran")
    assert local == BOUNDARY.replace(hour=14, minute=30)
    # An explicit offset naming the same instant resolves identically, and a
    # Z value is absolute — three spellings, one instant.
    assert resolve_wait_boundary("2026-09-26T18:00:00+03:30", timezone_name="UTC") == local
    assert resolve_wait_boundary("2026-09-26T14:30:00Z", timezone_name="UTC") == local
    assert resolve_wait_boundary("2026-09-26 18:00", timezone_name="Asia/Tehran") == local
    # Resolved VALUES are timezone-aware and normalized to UTC.
    assert local.tzinfo is not None and local.utcoffset() == timedelta(0)


@pytest.mark.parametrize("value", [
    "18:00",                         # a clock without a date
    "2026-09-26",                    # a date without a time
    "", "   ",
    "tomorrow at six",
    "2026-13-45T99:99:00",           # shape-valid, not a real instant
    "2026-09-26T18:00:00+99:00",     # impossible offset
    "x" * (MAX_WAIT_CHARS + 1),
    42,
    {"at": "18:00"},
    ["2026-09-26T18:00:00"],
    True,
])
def test_a_wait_boundary_must_be_a_full_iso_date_and_time(value):
    with pytest.raises(TaskContractError):
        resolve_wait_boundary(value, timezone_name="UTC")


def test_an_unresolvable_task_timezone_fails_closed():
    for zone in ("", "   ", "Mars/Olympus", None, 7):
        with pytest.raises(TaskContractError):
            resolve_wait_boundary("2026-09-26T18:00:00", timezone_name=zone)


def test_boundaries_are_normalized_bounded_and_ordered():
    reference = NOW
    actions = [
        {"name": "one", "arguments": {}},
        {"name": "two", "arguments": {}, WAIT_KEY: "2026-09-26T18:00:00"},
        {"name": "three", "arguments": {}, WAIT_KEY: "2026-09-27T01:00:00+03:30"},
    ]
    boundaries, normalized, error = resolve_action_waits(
        actions, timezone_name="UTC", reference=reference
    )
    assert error == ""
    assert boundaries[0] is None
    assert boundaries[1] == BOUNDARY
    assert normalized[1][WAIT_KEY] == BOUNDARY_ISO
    assert normalized[2][WAIT_KEY] == "2026-09-26T21:30:00+00:00"
    assert "not_before" not in normalized[0]

    # A PAST boundary is valid (immediately eligible — never shifted).
    _, past_normalized, error = resolve_action_waits(
        [{"name": "one", "arguments": {}, WAIT_KEY: "2026-09-26T09:00:00"}],
        timezone_name="UTC", reference=reference,
    )
    assert error == "" and past_normalized[0][WAIT_KEY] == "2026-09-26T09:00:00+00:00"

    # A boundary further ahead than the interval cap is unreasonable.
    _, _, error = resolve_action_waits(
        [{"name": "one", "arguments": {}, WAIT_KEY: "9999-01-01T00:00:00"}],
        timezone_name="UTC", reference=reference,
    )
    assert "unreasonably far ahead" in error

    # Ordering: a later action may not wait for an earlier instant.
    _, _, error = resolve_action_waits(
        [
            {"name": "one", "arguments": {}, WAIT_KEY: "2026-09-26T18:00:00"},
            {"name": "two", "arguments": {}, WAIT_KEY: "2026-09-26T17:00:00"},
        ],
        timezone_name="UTC", reference=reference,
    )
    assert "earlier than a previous action's boundary" in error

    # A malformed value is reported with its action position.
    _, _, error = resolve_action_waits(
        [{"name": "one", "arguments": {}}, {"name": "two", "arguments": {}, WAIT_KEY: "x"}],
        timezone_name="UTC", reference=reference,
    )
    assert error.startswith("action 2:")

    # The reference instant itself must be timezone-aware (never assumed).
    _, _, error = resolve_action_waits(
        [{"name": "one", "arguments": {}, WAIT_KEY: "2026-09-26T18:00:00"}],
        timezone_name="UTC", reference=datetime(2026, 9, 26, 12, 0),
    )
    assert "timezone-aware reference" in error
    assert MAX_WAIT_AHEAD_SECONDS == 366 * 24 * 3600 * 10


# ── 4/5: creation validates and persists the boundary ──────────────────────


@pytest.mark.asyncio
async def test_creation_stores_the_boundary_as_an_absolute_instant():
    calls = []
    registry = _chain_registry(calls)
    repo = InMemoryTaskRepository()
    service = TaskCreationService(repo, OWNER, tool_registry=registry)
    created = await service.create({
        "label": "tehran wait",
        "schedule_type": "once",
        "schedule": {"at": "2026-09-27T09:00:00", "timezone": "Asia/Tehran"},
        "timezone": "Asia/Tehran",
        "notification_destination": {},
        "actions": [
            {"name": "web_search", "arguments": {"query": "x"}},
            {"name": "send_message", "arguments": {"text": "hello"},
             WAIT_KEY: "2026-09-27T18:00:00"},
        ],
    }, NOW)

    stored = await repo.get_task(OWNER, created.id)
    assert stored.actions[1][WAIT_KEY] == "2026-09-27T14:30:00+00:00"
    assert stored.actions[1]["name"] == "send_message"
    assert stored.actions[1]["arguments"] == {"text": "hello"}
    assert WAIT_KEY not in stored.actions[0]


@pytest.mark.asyncio
async def test_creation_rejects_a_malformed_wait_boundary():
    calls = []
    registry = _chain_registry(calls)
    service = TaskCreationService(InMemoryTaskRepository(), OWNER, tool_registry=registry)
    for bad in ("18:00", "2026-09-26", "not a time", "", 42, ["x"], {"at": "18:00"}):
        with pytest.raises(TaskCreationError) as excinfo:
            await service.create({
                "label": "bad wait",
                "schedule_type": "once",
                "schedule": {"at": "2026-09-27T09:00:00", "timezone": "UTC"},
                "timezone": "UTC",
                "notification_destination": {},
                "actions": [
                    {"name": "web_search", "arguments": {"query": "x"}},
                    {"name": "send_message", "arguments": {"text": "hi"}, WAIT_KEY: bad},
                ],
            }, NOW)
        assert "wait boundary" in str(excinfo.value)


@pytest.mark.asyncio
async def test_creation_rejects_decreasing_and_unreasonable_boundaries():
    calls = []
    registry = _chain_registry(calls)
    service = TaskCreationService(InMemoryTaskRepository(), OWNER, tool_registry=registry)

    def _candidate(actions):
        return {
            "label": "wait order", "schedule_type": "once",
            "schedule": {"at": "2026-09-27T09:00:00", "timezone": "UTC"},
            "timezone": "UTC", "notification_destination": {}, "actions": actions,
        }

    with pytest.raises(TaskCreationError) as excinfo:
        await service.create(_candidate([
            {"name": "web_search", "arguments": {"query": "x"},
             WAIT_KEY: "2026-09-26T18:00:00"},
            {"name": "send_message", "arguments": {"text": "hi"},
             WAIT_KEY: "2026-09-26T17:00:00"},
        ]), NOW)
    assert "earlier than a previous action's boundary" in str(excinfo.value)

    with pytest.raises(TaskCreationError) as excinfo:
        await service.create(_candidate([
            {"name": "send_message", "arguments": {"text": "hi"},
             WAIT_KEY: "9999-01-01T00:00:00"},
        ]), NOW)
    assert "unreasonably far ahead" in str(excinfo.value)


def test_the_model_candidate_boundary_keeps_one_optional_wait_field():
    candidate = TaskCandidate.from_untrusted({
        "label": "chain", "schedule_type": "once",
        "schedule": {"at": "2026-09-27T09:00:00", "timezone": "UTC"},
        "timezone": "UTC", "notification_destination": {},
        "actions": [
            {"name": "web_search", "arguments": {"query": "x"}, "destination": "somewhere"},
            {"name": "send_message", "arguments": {"text": "hi"},
             WAIT_KEY: "2026-09-26T18:00:00", "chat_id": 1234},
        ],
    })
    # The wait survives the untrusted boundary; every other stray key is still
    # dropped (never smuggled into the persisted action).
    assert candidate.actions[0] == {"name": "web_search", "arguments": {"query": "x"}}
    assert candidate.actions[1] == {
        "name": "send_message",
        "arguments": {"text": "hi"},
        WAIT_KEY: "2026-09-26T18:00:00",
    }
    with pytest.raises(TaskCandidateError) as excinfo:
        TaskCandidate.from_untrusted({
            "label": "chain", "schedule_type": "once",
            "schedule": {"at": "2026-09-27T09:00:00", "timezone": "UTC"},
            "timezone": "UTC", "notification_destination": {},
            "actions": [{"name": "send_message", "arguments": {"text": "hi"}, WAIT_KEY: 42}],
        })
    assert "timestamp string" in str(excinfo.value)


def test_the_candidate_schema_declares_the_optional_wait_field():
    item_schema = CANDIDATE_SCHEMA["properties"]["actions"]["items"]
    assert item_schema["required"] == ["name", "arguments"]
    assert WAIT_KEY in item_schema["properties"]
    assert WAIT_KEY not in item_schema["required"]


# ── 6/7/8: execution parks the SAME occurrence at the boundary ─────────────


@pytest.mark.asyncio
async def test_a_chain_without_a_wait_still_executes_every_action():
    repo = InMemoryTaskRepository()
    calls = []
    registry = _chain_registry(calls)
    task, occurrence = await _start(repo, _deliver_chain())
    coordinator, _ = _coordinator(repo, registry=registry)

    result = await coordinator.execute(occurrence, now=NOW)

    assert result.success and result.status == "succeeded"
    assert _names(calls) == ["web_search", "save_by_link", "update_save_tags", "send_message"]
    stored = await _stored(repo, task.id)
    assert stored.status == "succeeded"
    assert [run["status"] for run in stored.result_metadata["actions"]] == ["succeeded"] * 4


@pytest.mark.asyncio
async def test_actions_before_the_wait_execute_and_later_actions_do_not():
    repo = InMemoryTaskRepository()
    calls, events = [], []
    registry = _chain_registry(calls, events=events)
    _, occurrence = await _start(repo, _deliver_chain(boundary=BOUNDARY_ISO))
    coordinator, _ = _coordinator(repo, registry=registry)

    result = await coordinator.execute(occurrence, now=NOW)

    assert result.status == "waiting" and not result.success
    assert _names(calls) == ["web_search", "save_by_link", "update_save_tags"]
    assert events[-1] == ("end", "update_save_tags")  # DELIVER never started


@pytest.mark.asyncio
async def test_the_wait_boundary_is_persisted_with_the_occurrence():
    repo = InMemoryTaskRepository()
    calls = []
    registry = _chain_registry(calls)
    task, occurrence = await _start(repo, _deliver_chain(boundary=BOUNDARY_ISO))
    coordinator, _ = _coordinator(repo, registry=registry)

    await coordinator.execute(occurrence, now=NOW)

    stored = await _stored(repo, task.id)
    # The existing durable eligibility pair, not a new state: the wake loop's
    # own "not before retry_at" rule now carries the wait.
    assert stored.status == "retry_pending"
    assert stored.retry_at == BOUNDARY
    # A wait consumes no attempt: it is not a failure.
    assert stored.attempt == 1
    assert stored.error_metadata["waiting_action"] == 4
    assert stored.error_metadata["waiting_until"] == BOUNDARY_ISO
    assert stored.result_metadata["waiting_until"] == BOUNDARY_ISO
    assert [run["status"] for run in stored.result_metadata["actions"]] == [
        "succeeded", "succeeded", "succeeded", "pending",
    ]
    # The task's own boundary is untouched by the wait.
    assert (await repo.get_task(OWNER, task.id)).next_run_at is None


@pytest.mark.asyncio
async def test_the_waiting_action_stays_pending_and_is_never_marked_succeeded():
    repo = InMemoryTaskRepository()
    calls = []
    registry = _chain_registry(calls)
    task, occurrence = await _start(repo, _deliver_chain(boundary=BOUNDARY_ISO))
    coordinator, _ = _coordinator(repo, registry=registry)

    await coordinator.execute(occurrence, now=NOW - timedelta(days=1))
    # A second wake that arrives while the boundary still has not passed parks
    # again: the waiting action stays pending, never succeeded.
    reclaimed = await repo.claim_occurrence(OWNER, task.id, "k")
    again = await coordinator.execute(reclaimed, now=NOW - timedelta(hours=1))
    assert again.status == "waiting"

    stored = await _stored(repo, task.id)
    assert stored.status == "retry_pending"
    runs = {run["position"]: run for run in stored.result_metadata["actions"]}
    assert runs[4]["status"] == "pending" and "output" not in runs[4]
    assert _names(calls) == ["web_search", "save_by_link", "update_save_tags"]


# ── 9/10/11/12: eligibility, resume, references, executor ──────────────────


@pytest.mark.asyncio
async def test_the_waiting_action_is_eligible_exactly_at_its_boundary():
    repo = InMemoryTaskRepository()
    calls = []
    registry = _chain_registry(calls)
    _, occurrence = await _start(repo, _deliver_chain(boundary=BOUNDARY_ISO))
    coordinator, _ = _coordinator(repo, registry=registry)

    result = await coordinator.execute(occurrence, now=BOUNDARY - timedelta(seconds=1))
    assert result.status == "waiting"
    assert "send_message" not in _names(calls)

    claimed = await repo.claim_occurrence(OWNER, occurrence.task_id, "k")
    at_boundary = await coordinator.execute(claimed, now=BOUNDARY)
    assert at_boundary.success, at_boundary.error
    assert _names(calls)[-1] == "send_message"


@pytest.mark.asyncio
async def test_a_past_due_boundary_is_immediately_eligible():
    repo = InMemoryTaskRepository()
    calls = []
    registry = _chain_registry(calls)
    task, occurrence = await _start(
        repo, _deliver_chain(boundary="2026-09-26T09:00:00+00:00")
    )
    coordinator, _ = _coordinator(repo, registry=registry)

    result = await coordinator.execute(occurrence, now=NOW)

    assert result.success and result.status == "succeeded"
    # No park at all: the occurrence completed in ONE attempt.
    stored = await _stored(repo, task.id)
    assert stored.status == "succeeded"
    assert _names(calls) == ["web_search", "save_by_link", "update_save_tags", "send_message"]


@pytest.mark.asyncio
async def test_resumption_skips_succeeded_actions_and_runs_the_waiting_action():
    repo = InMemoryTaskRepository()
    calls = []
    registry = _chain_registry(calls)
    task, occurrence = await _start(repo, _deliver_chain(boundary=BOUNDARY_ISO))
    coordinator, _ = _coordinator(repo, registry=registry)
    await coordinator.execute(occurrence, now=NOW)
    calls.clear()

    # A FRESH coordinator on a FRESH claim (a new process) resumes the chain.
    claimed = await repo.claim_occurrence(OWNER, task.id, "k")
    restarted, _ = _coordinator(repo, registry=registry)
    result = await restarted.execute(claimed, now=BOUNDARY)

    assert result.success, result.error
    assert _names(calls) == ["send_message"]  # nothing already succeeded replayed
    stored = await _stored(repo, task.id)
    assert stored.status == "succeeded"
    assert [run["status"] for run in stored.result_metadata["actions"]] == [
        "succeeded", "succeeded", "succeeded", "succeeded",
    ]


@pytest.mark.asyncio
async def test_a_result_reference_still_resolves_after_the_wait():
    repo = InMemoryTaskRepository()
    calls = []
    registry = _chain_registry(calls, save_code="S0042")
    task, occurrence = await _start(repo, _deliver_chain(boundary=BOUNDARY_ISO))
    coordinator, _ = _coordinator(repo, registry=registry)
    await coordinator.execute(occurrence, now=NOW)

    claimed = await repo.claim_occurrence(OWNER, task.id, "k")
    result = await coordinator.execute(claimed, now=BOUNDARY)

    assert result.success, result.error
    deliver = calls[-1]
    assert deliver["name"] == "send_message"
    # Resolved from the occurrence's own DURABLE record, written before the wait.
    assert deliver["arguments"]["save_code"] == "S0042"
    assert deliver["arguments"]["text"] == "saved S0042"
    park_record = (await _stored(repo, task.id)).error_metadata
    assert park_record["actions"][1]["output"] == {"save_code": "S0042"}


@pytest.mark.asyncio
async def test_the_post_wait_action_runs_through_the_single_executor():
    repo = InMemoryTaskRepository()
    calls = []
    registry = _chain_registry(calls)
    task, occurrence = await _start(repo, _deliver_chain(boundary=BOUNDARY_ISO))
    coordinator, executor = _coordinator(repo, registry=registry, executor_factory=CountingExecutor)

    await coordinator.execute(occurrence, now=NOW)
    assert executor.batches == [
        [{"name": "web_search", "arguments": {"query": "دانشگاه فردا تعطیل"}}],
        [{"name": "save_by_link", "arguments": {"link": "https://t.me/c/1/2"}}],
        [{"name": "update_save_tags", "arguments": {
            "save_code": "S0042", "tags": ["دانشگاه"], "mode": "add"}}],
    ]

    claimed = await repo.claim_occurrence(OWNER, task.id, "k")
    await coordinator.execute(claimed, now=BOUNDARY)

    assert executor.batches[-1] == [{"name": "send_message", "arguments": {
        "text": "saved S0042", "save_code": "S0042"}}]
    assert len(executor.batches) == 4  # one batch per action, exactly one call each
    assert all(call["scheduled"] is True for call in calls)


# ── 13/14: the wait cannot bypass validation, ownership or the registry ────


@pytest.mark.asyncio
async def test_a_malformed_stored_boundary_fails_closed_before_any_execution():
    repo = InMemoryTaskRepository()
    calls = []
    registry = _chain_registry(calls)
    actions = _deliver_chain()
    actions[0][WAIT_KEY] = 42  # hand-built snapshot bypassing creation validation
    task, occurrence = await _start(repo, actions)
    coordinator, _ = _coordinator(repo, registry=registry)

    result = await coordinator.execute(occurrence, now=NOW)

    assert not result.success and calls == []
    stored = await _stored(repo, task.id)
    assert stored.status == "failed"
    assert "invalid_wait_boundary" in stored.error_metadata["error_class"]


@pytest.mark.asyncio
async def test_a_stored_chain_with_decreasing_boundaries_fails_closed():
    repo = InMemoryTaskRepository()
    calls = []
    registry = _chain_registry(calls)
    actions = _deliver_chain()
    actions[2][WAIT_KEY] = "2026-09-26T18:00:00+00:00"
    actions[3][WAIT_KEY] = "2026-09-26T17:00:00+00:00"
    task, occurrence = await _start(repo, actions)
    coordinator, _ = _coordinator(repo, registry=registry)

    result = await coordinator.execute(occurrence, now=NOW)

    assert not result.success and calls == []
    assert "invalid_wait_boundary" in (await _stored(repo, task.id)).error_metadata["error_class"]


@pytest.mark.asyncio
async def test_a_foreign_owner_cannot_honor_another_owners_wait():
    repo = InMemoryTaskRepository()
    calls = []
    registry = _chain_registry(calls)
    task, occurrence = await _start(repo, _deliver_chain(boundary=BOUNDARY_ISO))
    foreign, _ = _coordinator(repo, owner=OTHER_OWNER, registry=registry)

    result = await foreign.execute(occurrence, now=BOUNDARY)

    assert not result.success and result.error == "owner_mismatch" and calls == []
    assert await repo.claim_occurrence(OTHER_OWNER, task.id, "k") is None
    assert (await _stored(repo, task.id)).status == "running"


@pytest.mark.asyncio
async def test_an_unregistered_waiting_action_fails_before_the_earlier_actions_run():
    repo = InMemoryTaskRepository()
    calls = []
    registry = _chain_registry(calls)
    actions = _deliver_chain()
    actions[3] = {"name": "ghost", "arguments": {}, WAIT_KEY: BOUNDARY_ISO}
    task, occurrence = await _start(repo, actions)
    coordinator, _ = _coordinator(repo, registry=registry)

    result = await coordinator.execute(occurrence, now=NOW)

    assert not result.success and calls == []
    assert (await _stored(repo, task.id)).error_metadata["error_class"] == "unregistered_action"


# ── 15/16: failure after the wait follows the existing contract ────────────


@pytest.mark.asyncio
async def test_a_failure_after_the_wait_follows_the_retry_contract():
    repo = InMemoryTaskRepository()
    calls = []
    registry = _chain_registry(calls, plan=[__import__("asyncio").TimeoutError("tool timed out")])
    task, occurrence = await _start(repo, _deliver_chain(boundary=BOUNDARY_ISO))
    coordinator, _ = _coordinator(repo, registry=registry)
    await coordinator.execute(occurrence, now=NOW)

    claimed = await repo.claim_occurrence(OWNER, task.id, "k")
    failed = await coordinator.execute(claimed, now=BOUNDARY)

    assert failed.status == "retry_pending"
    stored = await _stored(repo, task.id)
    assert stored.status == "retry_pending" and stored.attempt == 2
    assert [run["status"] for run in stored.error_metadata["actions"]] == [
        "succeeded", "succeeded", "succeeded", "failed",
    ]
    # The next wake serves the retry and resumes at the failed action only.
    calls.clear()
    retried = await repo.claim_occurrence(OWNER, task.id, "k")
    second = await coordinator.execute(retried, now=BOUNDARY + timedelta(minutes=5))
    assert second.success, second.error
    assert _names(calls) == ["send_message"]


@pytest.mark.asyncio
async def test_a_non_retryable_failure_after_the_wait_fails_the_occurrence():
    repo = InMemoryTaskRepository()
    calls = []
    registry = _chain_registry(calls, plan=[ToolResult(False, "send refused")])
    task, occurrence = await _start(repo, _deliver_chain(boundary=BOUNDARY_ISO))
    coordinator, _ = _coordinator(repo, registry=registry)
    await coordinator.execute(occurrence, now=NOW)

    claimed = await repo.claim_occurrence(OWNER, task.id, "k")
    failed = await coordinator.execute(claimed, now=BOUNDARY)

    assert not failed.success
    stored = await _stored(repo, task.id)
    assert stored.status == "failed" and stored.attempt == 1
    assert stored.error_metadata["actions"][3]["status"] == "failed"
    assert await repo.claim_occurrence(OWNER, task.id, "k") is None  # terminal


# ── 17/18/19: the single scheduler serves the boundary ────────────────────


@pytest.mark.asyncio
async def test_the_scheduler_does_not_claim_before_the_boundary():
    repo = InMemoryTaskRepository()
    calls = []
    registry = _chain_registry(calls)
    task, occurrence = await _start(repo, _deliver_chain(boundary=BOUNDARY_ISO))
    coordinator, _ = _coordinator(repo, registry=registry)
    await coordinator.execute(occurrence, now=NOW)
    calls.clear()

    scheduler = TaskScheduler(repo, OWNER, coordinator, outcome_notifier=None)
    early = BOUNDARY - timedelta(minutes=1)

    assert await repo.list_due_retry_occurrences(OWNER, early) == []
    assert await scheduler.run_once(now=early) == 0
    assert calls == []
    assert (await _stored(repo, task.id)).status == "retry_pending"


@pytest.mark.asyncio
async def test_the_scheduler_claims_and_resumes_after_the_boundary():
    repo = InMemoryTaskRepository()
    calls = []
    registry = _chain_registry(calls)
    task, occurrence = await _start(repo, _deliver_chain(boundary=BOUNDARY_ISO))
    coordinator, _ = _coordinator(repo, registry=registry)
    await coordinator.execute(occurrence, now=NOW)
    calls.clear()

    scheduler = TaskScheduler(repo, OWNER, coordinator, outcome_notifier=None)
    due = await repo.list_due_retry_occurrences(OWNER, BOUNDARY)
    assert [item.occurrence_key for item in due] == [occurrence.occurrence_key]

    assert await scheduler.run_once(now=BOUNDARY) == 1
    assert _names(calls) == ["send_message"]
    assert (await _stored(repo, task.id)).status == "succeeded"


@pytest.mark.asyncio
async def test_a_restart_leaves_the_parked_wait_untouched():
    repo = InMemoryTaskRepository()
    calls = []
    registry = _chain_registry(calls)
    task, occurrence = await _start(repo, _deliver_chain(boundary=BOUNDARY_ISO))
    coordinator, _ = _coordinator(repo, registry=registry)
    await coordinator.execute(occurrence, now=NOW)
    calls.clear()

    # The process dies at 14:01 and comes back at 17:00: a NEW scheduler and a
    # NEW coordinator over the same durable store.
    restarted_coordinator, _ = _coordinator(repo, registry=registry)
    restarted = TaskScheduler(repo, OWNER, restarted_coordinator, outcome_notifier=None)

    assert await restarted.recover() == 0  # a parked wait is not a running action
    assert await restarted.run_once(now=BOUNDARY - timedelta(hours=1)) == 0
    assert calls == []
    stored = await _stored(repo, task.id)
    assert stored.status == "retry_pending" and stored.retry_at == BOUNDARY
    assert stored.error_metadata.get("error_class") is None  # never "uncertain"


@pytest.mark.asyncio
async def test_a_restart_resumes_at_the_waiting_action_after_the_boundary():
    repo = InMemoryTaskRepository()
    calls = []
    registry = _chain_registry(calls)
    task, occurrence = await _start(repo, _deliver_chain(boundary=BOUNDARY_ISO))
    coordinator, _ = _coordinator(repo, registry=registry)
    await coordinator.execute(occurrence, now=NOW)
    calls.clear()

    restarted_coordinator, _ = _coordinator(repo, registry=registry)
    restarted = TaskScheduler(repo, OWNER, restarted_coordinator, outcome_notifier=None)
    await restarted.recover()

    assert await restarted.run_once(now=BOUNDARY + timedelta(minutes=5)) == 1
    assert _names(calls) == ["send_message"]
    stored = await _stored(repo, task.id)
    assert stored.status == "succeeded"
    assert [run["status"] for run in stored.result_metadata["actions"]] == ["succeeded"] * 4
    assert stored.error_metadata["waiting_until"] == BOUNDARY_ISO  # the instant never moved


@pytest.mark.asyncio
async def test_a_wait_creates_no_duplicate_occurrence_and_does_not_shift_the_boundary():
    calls = []
    registry = _chain_registry(calls)
    repo = InMemoryTaskRepository()
    service = TaskCreationService(repo, OWNER, tool_registry=registry)
    task = await service.create({
        "label": "daily deliver",
        "schedule_type": "daily",
        "schedule": {"hour": 14, "minute": 0, "timezone": "UTC"},
        "timezone": "UTC",
        "notification_destination": {},
        "actions": _deliver_chain(boundary="2026-09-26T18:00:00"),
    }, START - timedelta(hours=2))
    assert task.next_run_at == START

    coordinator, _ = _coordinator(repo, registry=registry)
    scheduler = TaskScheduler(repo, OWNER, coordinator, outcome_notifier=None)
    assert await scheduler.run_once(now=START) == 1

    # Exactly one occurrence, keyed by the SCHEDULED boundary; the wait did not
    # create a second one nor move the task's cadence.
    occurrences = await repo.list_occurrences(OWNER, task.id)
    assert len(occurrences) == 1
    assert occurrences[0].occurrence_key == f"{task.id}:{START.isoformat()}"
    parked = occurrences[0]
    assert parked.status == "retry_pending" and parked.retry_at == BOUNDARY
    assert (await repo.get_task(OWNER, task.id)).next_run_at == START + timedelta(days=1)

    # The NEXT day's wake serves BOTH: the still-parked wait (its absolute
    # 18:00 boundary is long past) and the new day's own boundary, which runs
    # the whole chain again — recurrence and wait stay separate concepts, and
    # a wait never wedges the task itself.
    next_day = START + timedelta(days=1)
    assert await scheduler.run_once(now=next_day) == 2
    assert _names(calls) == [
        "web_search", "save_by_link", "update_save_tags", "send_message",
        "web_search", "save_by_link", "update_save_tags", "send_message",
    ]
    occurrences = await repo.list_occurrences(OWNER, task.id)
    assert len(occurrences) == 2
    assert all(item.status == "succeeded" for item in occurrences)


@pytest.mark.asyncio
async def test_a_second_wait_boundary_is_served_by_the_same_mechanism():
    repo = InMemoryTaskRepository()
    calls = []
    registry = _chain_registry(calls, plan=[ToolResult(True, "ok", {"text": "delivered"})])
    actions = _deliver_chain()
    actions[2][WAIT_KEY] = "2026-09-26T18:00:00+00:00"
    actions.insert(3, {
        "name": "update_save_tags",
        "arguments": {
            "save_code": {"$ref": {"action": 2, "field": "save_code"}},
            "tags": ["later"], "mode": "add",
        },
        WAIT_KEY: "2026-09-26T20:00:00+00:00",
    })
    task, occurrence = await _start(repo, actions)
    coordinator, _ = _coordinator(repo, registry=registry)

    first = await coordinator.execute(occurrence, now=NOW)
    assert first.status == "waiting"
    assert (await _stored(repo, task.id)).retry_at == BOUNDARY

    claimed = await repo.claim_occurrence(OWNER, task.id, "k")
    second = await coordinator.execute(claimed, now=BOUNDARY + timedelta(minutes=30))
    assert second.status == "waiting"
    stored = await _stored(repo, task.id)
    assert stored.retry_at == datetime(2026, 9, 26, 20, 0, tzinfo=timezone.utc)
    assert [run["status"] for run in stored.result_metadata["actions"]] == [
        "succeeded", "succeeded", "succeeded", "pending", "pending",
    ]

    claimed = await repo.claim_occurrence(OWNER, task.id, "k")
    third = await coordinator.execute(claimed, now=datetime(2026, 9, 26, 20, 1, tzinfo=timezone.utc))
    assert third.success, third.error
    assert _names(calls) == [
        "web_search", "save_by_link", "update_save_tags", "update_save_tags", "send_message",
    ]


# ── 20: the acceptance chain, end to end through creation + the scheduler ──


@pytest.mark.asyncio
async def test_end_to_end_search_save_tag_wait_deliver():
    """SEARCH → SAVE → TAG → WAIT until 18:00 → DELIVER.

    The real creation boundary, the real scheduler, the real coordinator, the
    real registry/executor and the durable occurrence state; only the four
    services behind the tools are doubled. It proves DELIVER does not execute
    before the boundary — across a simulated process restart — and executes
    exactly once after it, using the save code recorded before the wait.
    """
    repo = InMemoryTaskRepository()
    calls = []
    registry = _chain_registry(calls)
    service = TaskCreationService(repo, OWNER, tool_registry=registry)
    task = await service.create({
        "label": "پری این موضوع رو سرچ کن، سیو کن، تگش کن، ساعت ۶ بده",
        "schedule_type": "once",
        "schedule": {"at": "2026-09-26T14:00:00", "timezone": "UTC"},
        "timezone": "UTC",
        "notification_destination": {},
        "actions": _deliver_chain(boundary="2026-09-26T18:00:00"),
    }, START - timedelta(minutes=30))
    # Creation normalized the naive local boundary to an absolute instant.
    stored_task = await repo.get_task(OWNER, task.id)
    assert stored_task.actions[3][WAIT_KEY] == BOUNDARY_ISO

    coordinator, _ = _coordinator(repo, registry=registry)
    scheduler = TaskScheduler(repo, OWNER, coordinator, outcome_notifier=None)

    # 14:00 — SEARCH, SAVE and TAG run; DELIVER must not.
    assert await scheduler.run_once(now=START) == 1
    occurrence = (await repo.list_occurrences(OWNER, task.id))[0]
    key = occurrence.occurrence_key
    assert occurrence.status == "retry_pending" and occurrence.retry_at == BOUNDARY
    assert _names(calls) == ["web_search", "save_by_link", "update_save_tags"]

    # 17:59 — still nothing.
    assert await scheduler.run_once(now=BOUNDARY - timedelta(minutes=1)) == 0
    assert _names(calls) == ["web_search", "save_by_link", "update_save_tags"]

    # 17:00, a process restart: recovery must leave the parked wait alone.
    restarted_coordinator, _ = _coordinator(repo, registry=registry)
    restarted = TaskScheduler(repo, OWNER, restarted_coordinator, outcome_notifier=None)
    assert await restarted.recover() == 0
    assert await restarted.run_once(now=BOUNDARY - timedelta(hours=1)) == 0
    assert (await _stored(repo, task.id, key)).status == "retry_pending"

    # 18:00 — DELIVER becomes eligible, is claimed normally and runs once.
    assert await restarted.run_once(now=BOUNDARY) == 1
    final = await _stored(repo, task.id, key)
    assert final.status == "succeeded" and final.attempt == 1
    assert _names(calls) == [
        "web_search", "save_by_link", "update_save_tags", "send_message",
    ]
    deliver = calls[3]
    assert deliver["arguments"]["save_code"] == "S0042"   # recorded BEFORE the wait
    assert deliver["arguments"]["text"] == "saved S0042"
    assert final.result_metadata["actions"][3]["output"] == {"text": "delivered"}
    # The requested instant was never shifted, and no occurrence was duplicated.
    assert final.error_metadata["waiting_until"] == BOUNDARY_ISO
    occurrences = await repo.list_occurrences(OWNER, task.id)
    assert len(occurrences) == 1
    assert occurrences[0].occurrence_key == key
    assert json.dumps(final.result_metadata)  # the record stays JSON-serializable


def test_the_wait_record_fits_its_bounded_budget():
    """The park record is structurally inside the existing 8192-byte metadata bound."""
    actions = _deliver_chain(boundary=BOUNDARY_ISO)
    boundaries, normalized, error = resolve_action_waits(
        actions, timezone_name="UTC", reference=NOW
    )
    assert error == ""
    record = {
        "action_count": len(actions),
        "successful_action_count": 3,
        "duration_ms": 12.5,
        "waiting_action": 4,
        "waiting_until": BOUNDARY_ISO,
        "actions": [
            {"position": index, "tool": action["name"], "status": "pending"}
            for index, action in enumerate(normalized, start=1)
        ],
    }
    assert len(json.dumps(record, ensure_ascii=False).encode()) < 8192
