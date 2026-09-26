"""Bounded conditional branching — ONE condition, two branches, one durable choice.

The product contract under test (Todo Part 3C): a durable task's ordered actions
may carry at most ONE condition action. It declares no tool call — it compares
ONE declared, bounded output field of an EARLIER action of the SAME occurrence
against a bounded literal — and exactly ONE of two labelled action runs then
executes. The RESULT of the condition is its own bounded run record
(``{"matched": …, "selected_branch": "true"|"false"}``), persisted BEFORE either
branch runs, so a restart resumes the SAME branch: the condition is never
re-evaluated, the other branch never starts, and a failed action in the selected
branch never falls back to the other one. Every invalid or unresolvable condition
fails the occurrence closed — a deterministic failure, never a silent ``false``.

Boundaries preserved by these tests: the ToolRegistry stays the capability
allowlist, the ToolExecutor stays the sole caller of ``tool.execute()`` (the
condition is not a tool and never reaches it), TaskScheduler stays the only
scheduler, TaskExecutionCoordinator stays the only occurrence/claim authority,
and no schema change is involved — the condition, its selection and the
permanently inactive branch live in the existing ``actions`` array and the
existing bounded per-action record.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from backend.ai.database.task_repository import InMemoryTaskRepository
from backend.ai.task_candidate import TaskCandidate, TaskCandidateError
from backend.ai.task_contract import (
    BRANCH_KEY,
    BRANCH_SKIPPED_STATUS,
    CONDITION_KEY,
    CONDITION_OPERATORS,
    CONDITION_TOOL,
    MAX_ACTION_OUTPUT_TEXT_CHARS,
    MAX_CONDITION_NUMBER,
    WAIT_KEY,
    TaskContractError,
    action_reference_error,
    evaluate_condition,
    resolve_conditions,
    validate_condition,
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
NOW = datetime(2026, 9, 26, 12, 0, tzinfo=timezone.utc)
START = datetime(2026, 9, 26, 14, 0, tzinfo=timezone.utc)
BOUNDARY = datetime(2026, 9, 26, 18, 0, tzinfo=timezone.utc)
BOUNDARY_ISO = "2026-09-26T18:00:00+00:00"


# ── Registry/executor/coordinator harness (the Phase 3A/3B doubles) ─────────


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


def _branch_registry(
    calls, *, closed=True, events=None, search_plan=None, save_plan=None, tag_plan=None
):
    """web_search / save_by_link / update_save_tags doubles.

    They mirror the real registered tools' names, argument contracts and
    declared chainable fields, and stand in only for the services they wrap.
    ``web_search`` declares a BOOLEAN decision field (``is_closed``) plus the
    link the branches consume; both branches SAVE through the same tool.
    """
    registry = ToolRegistry()
    registry.register(ChainTool(
        "web_search", calls, events=events, plan=search_plan,
        data={"is_closed": closed, "top_url": "https://t.me/c/1/2"},
        fields=("is_closed", "top_url"),
    ))
    registry.register(ChainTool(
        "save_by_link", calls, events=events, plan=save_plan,
        data={"save_code": "S0042"}, fields=("save_code",),
    ))
    registry.register(ChainTool(
        "update_save_tags", calls, events=events, plan=tag_plan,
        data={"save_code": "S0042", "summary": "saved S0042"},
        fields=("save_code", "summary"),
    ))
    return registry


def _branch_actions():
    """SEARCH → CONDITION → (true) SAVE → TAG / (false) SAVE.

    The TRUE branch consumes the SEARCH link (an action BEFORE the branch) and
    then its OWN save code; the FALSE branch consumes the same SEARCH link.
    Both branches reference only actions they may legitimately read.
    """
    actions = [
        {"name": "web_search", "arguments": {"query": "دانشگاه فردا تعطیل"}},
        {"condition": {
            "source": {"action": 1, "field": "is_closed"},
            "operator": "equals",
            "value": True,
        }},
        {
            "name": "save_by_link",
            "arguments": {"link": {"$ref": {"action": 1, "field": "top_url"}}},
            BRANCH_KEY: "true",
        },
        {
            "name": "update_save_tags",
            "arguments": {
                "save_code": {"$ref": {"action": 3, "field": "save_code"}},
                "tags": ["تعطیلی"], "mode": "add",
            },
            BRANCH_KEY: "true",
        },
        {
            "name": "save_by_link",
            "arguments": {"link": {"$ref": {"action": 1, "field": "top_url"}}},
            BRANCH_KEY: "false",
        },
    ]
    return actions


def _task_payload(actions, *, timezone_name="UTC"):
    return {
        "label": "conditional",
        "schedule_type": "once",
        "schedule": {"at": "2027-01-01T09:00:00", "timezone": timezone_name},
        "timezone": timezone_name,
        "actions": actions,
        "notification_destination": {},
    }


async def _start(repo, actions, *, key="k", owner=OWNER, error_metadata=None):
    """Create the task + claimed occurrence this chain will execute."""
    task = await repo.create_task(owner, _task_payload(actions))
    payload = {
        "task_id": task.id,
        "occurrence_key": key,
        "definition_version": task.version,
        "action_snapshot": actions,
        "scheduled_for": NOW,
    }
    if error_metadata is not None:
        payload["error_metadata"] = error_metadata
    await repo.create_occurrence(owner, payload)
    claimed = await repo.claim_occurrence(owner, task.id, key)
    return task, claimed


def _coordinator(repo, owner=OWNER, registry=None, executor_factory=ToolExecutor):
    ctx = ToolContext(None, owner, "UTC")
    registry = registry if registry is not None else ToolRegistry()
    executor = executor_factory(registry, ctx)
    return TaskExecutionCoordinator(repo, executor, owner, ctx), executor


async def _stored(repo, task_id, key="k", owner=OWNER):
    return await repo.get_occurrence(owner, task_id, key)


def _names(calls):
    return [call["name"] for call in calls]


def _statuses(record, *, channel="result_metadata"):
    return [run["status"] for run in record[channel]["actions"]]


# ── 1/2: the condition contract — shape, operators, value types ─────────────


def test_the_only_accepted_condition_shape_is_normalized():
    assert validate_condition({
        "source": {"action": 2, "field": "is_closed"},
        "operator": "not_equals",
        "value": False,
    }) == {
        "source": {"action": 2, "field": "is_closed"},
        "operator": "not_equals",
        "value": False,
    }


@pytest.mark.parametrize("value,fragment", [
    ({}, "must contain exactly"),
    ({"source": {"action": 1, "field": "x"}, "operator": "equals"}, "must contain exactly"),
    ({"source": {"action": 1, "field": "x"}, "operator": "equals", "value": 1, "extra": 2},
     "must contain exactly"),
    ({"source": {"action": 1, "field": "x"}, "operator": "equals", "value": 1, "predicate": "..."},
     "must contain exactly"),
    ({"source": {"action": 1}, "operator": "equals", "value": 1}, "must name exactly"),
    ({"source": {"action": 1, "field": "x", "path": "a.b"}, "operator": "equals", "value": 1},
     "must name exactly"),
    ({"source": 1, "operator": "equals", "value": 1}, "must name exactly"),
    ({"source": {"action": 0, "field": "x"}, "operator": "equals", "value": 1},
     "positive action number"),
    ({"source": {"action": True, "field": "x"}, "operator": "equals", "value": 1},
     "positive action number"),
    ({"source": {"action": "1", "field": "x"}, "operator": "equals", "value": 1},
     "positive action number"),
    ({"source": {"action": 1, "field": ""}, "operator": "equals", "value": 1},
     "bounded nonblank name"),
    ({"source": {"action": 1, "field": "x" * 65}, "operator": "equals", "value": 1},
     "bounded nonblank name"),
])
def test_a_malformed_condition_is_rejected_not_interpreted(value, fragment):
    with pytest.raises(TaskContractError) as excinfo:
        validate_condition(value)
    assert fragment in str(excinfo.value)


def test_the_condition_language_is_deliberately_tiny():
    # Exactly two operators — the ones the current product needs. Every other
    # spell of a "condition language" is refused, never interpreted.
    assert CONDITION_OPERATORS == {"equals", "not_equals"}
    for operator in ("contains", "exists", "greater_than", "less_than", "starts_with",
                     "in", "matches", "and", "or", "not", "==", "!="):
        with pytest.raises(TaskContractError) as excinfo:
            validate_condition({
                "source": {"action": 1, "field": "x"}, "operator": operator, "value": 1,
            })
        assert "operator must be one of" in str(excinfo.value)


@pytest.mark.parametrize("value", ["open", "", "x" * (MAX_ACTION_OUTPUT_TEXT_CHARS + 1), 1,
                                   -1, 1.5, True, False, None,
                                   MAX_CONDITION_NUMBER + 1, float("inf"), float("nan"),
                                   {"nested": 1}, ["a"], {"$ref": {"action": 1, "field": "x"}}])
def test_only_bounded_scalars_are_condition_values(value):
    """Text/bool/finite bounded number/null are values; everything else fails."""
    condition = {"source": {"action": 1, "field": "x"}, "operator": "equals", "value": value}
    allowed = isinstance(value, (bool, type(None))) or (
        isinstance(value, str) and value.strip() and len(value) <= MAX_ACTION_OUTPUT_TEXT_CHARS
    ) or (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and abs(value) == abs(value)
        and value not in (float("inf"), float("-inf"))
        and abs(value) <= MAX_CONDITION_NUMBER
    )
    if allowed:
        assert validate_condition(condition)["value"] == value
        return
    with pytest.raises(TaskContractError):
        validate_condition(condition)


@pytest.mark.parametrize("operator,recorded,value,matched", [
    ("equals", True, True, True),
    ("equals", False, True, False),
    ("equals", True, False, False),
    ("equals", False, False, True),
    ("not_equals", False, True, True),
    ("equals", "closed", "closed", True),
    ("equals", "closed", "open", False),
    ("not_equals", "closed", "closed", False),
    ("equals", 3, 3, True),
    ("equals", 3, 3.0, True),
    ("not_equals", 3, 4, True),
    ("equals", "3", 3, False),      # never coerced
    ("equals", 1, True, False),     # Python's True == 1 shortcut is refused
    ("equals", "true", True, False),
    ("equals", "closed", "CLOSED", False),
    ("equals", "a closed school", "closed", False),   # never a substring match
    ("not_equals", "closed", None, True),
])
def test_condition_comparison_is_strict_and_never_coerced(operator, recorded, value, matched):
    runs = [{
        "position": 1, "tool": "search", "status": "succeeded",
        "output": {"field": recorded},
    }]
    condition = {"source": {"action": 1, "field": "field"}, "operator": operator, "value": value}
    assert evaluate_condition(condition, runs) == (matched, "")


def test_a_null_comparison_can_never_be_satisfied_by_a_recorded_output():
    """The bounded output contract omits null, so a null value is a DECIDED
    comparison (only null matches null), never a silent match — and a source
    field that carries no usable value fails the condition closed."""
    condition = {"source": {"action": 1, "field": "field"},
                 "operator": "equals", "value": None}
    for output in ({}, {"field": None}):
        runs = [{"position": 1, "tool": "search", "status": "succeeded", "output": output}]
        matched, reason = evaluate_condition(condition, runs)
        assert matched is None
        assert reason == "condition_field_unavailable (action 1, field 'field')"


# ── 6/7: the branch structure is proven over the whole chain ────────────────


def _registry():
    return _branch_registry([])


def test_a_valid_conditional_chain_resolves_into_one_bounded_layout():
    layout, error = resolve_conditions(_branch_actions(), _registry())
    assert error == ""
    assert layout.condition_position == 2
    assert layout.branches == {3: "true", 4: "true", 5: "false"}
    assert layout.true_positions == (3, 4)
    assert layout.false_positions == (5,)
    assert layout.condition["value"] is True


def test_a_chain_without_a_condition_is_unchanged():
    actions = [{"name": "send_message", "arguments": {"text": "hi"}}]
    assert resolve_conditions(actions, _registry()) == (None, "")


def _chain_with_a_second_condition():
    actions = _branch_actions()
    actions[2] = {"condition": {"source": {"action": 1, "field": "is_closed"},
                                "operator": "equals", "value": True}}
    return actions


def _chain_with_a_branch_before_the_condition():
    actions = _branch_actions()
    actions[0][BRANCH_KEY] = "true"
    return actions


def _chain_with_an_extra_condition_key():
    actions = _branch_actions()
    actions[1]["operator"] = "equals"
    return actions


def _chain_with_a_wait_on_the_condition():
    actions = _branch_actions()
    actions[1][WAIT_KEY] = BOUNDARY_ISO
    return actions


def _chain_without_a_true_branch():
    actions = _branch_actions()
    del actions[2:4]
    return actions


def _chain_without_a_false_branch():
    actions = _branch_actions()
    actions.pop()
    return actions


def _chain_with_a_split_true_branch():
    actions = _branch_actions()
    actions[3][BRANCH_KEY] = "false"
    actions[4][BRANCH_KEY] = "true"
    return actions


def _chain_with_the_true_branch_after_the_false_branch():
    actions = _branch_actions()
    actions[2][BRANCH_KEY] = "false"
    actions[3][BRANCH_KEY] = "false"
    actions[4][BRANCH_KEY] = "true"
    return actions


def _chain_with_an_unlabelled_action():
    actions = _branch_actions()
    actions[2].pop(BRANCH_KEY)
    return actions


def _chain_with_an_unknown_branch_label():
    actions = _branch_actions()
    actions[2][BRANCH_KEY] = "maybe"
    return actions


def _chain_with_a_non_string_branch_label():
    actions = _branch_actions()
    actions[2][BRANCH_KEY] = True
    return actions


@pytest.mark.parametrize("builder,fragment", [
    (_chain_with_a_second_condition, "only one condition"),
    (_chain_with_a_branch_before_the_condition, "only actions after the condition"),
    (_chain_with_an_extra_condition_key, "must contain only"),
    (_chain_with_a_wait_on_the_condition, "must contain only"),
    (_chain_without_a_true_branch, "at least one action in each branch"),
    (_chain_without_a_false_branch, "at least one action in each branch"),
    (_chain_with_a_split_true_branch, "true branch must be one contiguous run"),
    (_chain_with_the_true_branch_after_the_false_branch,
     "true branch must come before the false branch"),
    (_chain_with_an_unlabelled_action, "every action after the condition must declare"),
    (_chain_with_an_unknown_branch_label, "every action after the condition must declare"),
    (_chain_with_a_non_string_branch_label, "every action after the condition must declare"),
])
def test_a_chain_that_is_neither_shape_is_refused(builder, fragment):
    layout, error = resolve_conditions(builder(), _registry())
    assert layout is None
    assert fragment in error


def test_a_branch_label_without_a_condition_is_refused():
    actions = [{"name": "send_message", "arguments": {"text": "hi"}, BRANCH_KEY: "true"}]
    layout, error = resolve_conditions(actions, _registry())
    assert layout is None
    assert "no condition" in error


# ── 8/9/10: a condition reads only an earlier, declared, same-occurrence field ─


@pytest.mark.parametrize("source,fragment", [
    ({"action": 2, "field": "is_closed"}, "not an earlier action"),
    ({"action": 3, "field": "is_closed"}, "not an earlier action"),
    ({"action": 9, "field": "is_closed"}, "not an earlier action"),
])
def test_a_future_or_unknown_source_action_is_refused(source, fragment):
    actions = _branch_actions()
    actions[1]["condition"]["source"] = source
    layout, error = resolve_conditions(actions, _registry())
    assert layout is None
    assert fragment in error


@pytest.mark.parametrize("field", ["is_open", "save_code", "top_title"])
def test_a_field_the_source_tool_does_not_declare_is_refused(field):
    actions = _branch_actions()
    actions[1]["condition"]["source"] = {"action": 1, "field": field}
    layout, error = resolve_conditions(actions, _registry())
    assert layout is None
    assert "does not declare as chainable" in error


def test_a_condition_cannot_read_an_unregistered_action():
    actions = _branch_actions()
    actions[0] = {"name": "missing_tool", "arguments": {"query": "x"}}
    layout, error = resolve_conditions(actions, _registry())
    assert layout is None
    assert "tool is not registered" in error


def test_a_condition_is_refused_together_with_generated_content():
    """A model-generated chain would be free to reshape the condition away."""
    layout, error = resolve_conditions(
        _branch_actions(), _registry(), generation_authorized=True
    )
    assert layout is None
    assert "generated arguments" in error


def test_without_a_registry_the_position_rule_still_holds():
    """The execution boundary re-proves with the real registry; the positional
    rule is enforced even where no registry is available at all."""
    assert resolve_conditions(_branch_actions(), None)[1] == ""
    actions = _branch_actions()
    actions[1]["condition"]["source"] = {"action": 2, "field": "is_closed"}
    layout, error = resolve_conditions(actions, None)
    assert layout is None and "not an earlier action" in error


# ── 13: branch-scoped references ───────────────────────────────────────────


def test_a_branch_action_cannot_reference_the_other_branch():
    registry = _registry()
    # A valid chain: each branch reads what it may (before the condition, or its
    # own earlier action).
    assert action_reference_error(_branch_actions(), registry) is None

    # The FALSE branch reaching for the TRUE branch's result: never resolvable,
    # because only one branch ever runs.
    actions = _branch_actions()
    actions[4]["arguments"]["save_code"] = {"$ref": {"action": 3, "field": "save_code"}}
    error = action_reference_error(actions, registry)
    assert "of the 'true' branch" in error
    assert "never produces" in error

    # The TRUE branch reaching forward into the FALSE branch is refused as a
    # later-position reference (the earlier-action rule binds first).
    actions = _branch_actions()
    actions[3]["arguments"]["save_code"] = {"$ref": {"action": 5, "field": "save_code"}}
    assert "not an earlier action" in action_reference_error(actions, registry)

    # The condition itself declares no output, so nothing may read it.
    actions = _branch_actions()
    actions[2]["arguments"]["mode"] = {"$ref": {"action": 2, "field": "matched"}}
    assert "references the condition action" in action_reference_error(actions, registry)


@pytest.mark.asyncio
async def test_a_stored_cross_branch_reference_fails_the_occurrence_closed():
    """The execution boundary re-proves the branch scope: a stored chain that
    reaches across branches fails BEFORE any action runs."""
    repo = InMemoryTaskRepository()
    calls = []
    registry = _branch_registry(calls)
    actions = _branch_actions()
    actions[4]["arguments"]["save_code"] = {"$ref": {"action": 3, "field": "save_code"}}
    task, occurrence = await _start(repo, actions)
    coordinator, _ = _coordinator(repo, registry=registry)

    result = await coordinator.execute(occurrence)

    assert not result.success
    assert "invalid_action_reference" in result.error
    assert calls == []  # nothing ran — not even the search
    stored = await _stored(repo, task.id)
    assert stored.status == "failed"


# ── 3/4/5/14/15: exactly one branch executes, and the choice is durable ────


@pytest.mark.asyncio
async def test_a_true_condition_runs_only_the_true_branch():
    repo = InMemoryTaskRepository()
    calls, events = [], []
    registry = _branch_registry(calls, closed=True, events=events)
    task, occurrence = await _start(repo, _branch_actions())
    coordinator, _ = _coordinator(repo, registry=registry)

    result = await coordinator.execute(occurrence)

    assert result.success, result.error
    assert _names(calls) == ["web_search", "save_by_link", "update_save_tags"]
    assert calls[1]["arguments"]["link"] == "https://t.me/c/1/2"   # from BEFORE the branch
    assert calls[2]["arguments"]["save_code"] == "S0042"           # from the branch's own save
    stored = await _stored(repo, task.id)
    assert stored.status == "succeeded"
    assert [(run["position"], run["tool"], run["status"]) for run in stored.result_metadata["actions"]] == [
        (1, "web_search", "succeeded"),
        (2, CONDITION_TOOL, "succeeded"),
        (3, "save_by_link", "succeeded"),
        (4, "update_save_tags", "succeeded"),
        (5, "save_by_link", BRANCH_SKIPPED_STATUS),
    ]
    assert events == [
        ("start", "web_search"), ("end", "web_search"),
        ("start", "save_by_link"), ("end", "save_by_link"),
        ("start", "update_save_tags"), ("end", "update_save_tags"),
    ]


@pytest.mark.asyncio
async def test_a_false_condition_runs_only_the_false_branch():
    repo = InMemoryTaskRepository()
    calls = []
    registry = _branch_registry(
        calls, closed=False, save_plan=[ToolResult(True, "ok", {"save_code": "S9001"})]
    )
    task, occurrence = await _start(repo, _branch_actions())
    coordinator, _ = _coordinator(repo, registry=registry)

    result = await coordinator.execute(occurrence)

    assert result.success, result.error
    assert _names(calls) == ["web_search", "save_by_link"]
    assert calls[1]["arguments"]["link"] == "https://t.me/c/1/2"
    stored = await _stored(repo, task.id)
    assert stored.status == "succeeded"
    assert [(run["position"], run["status"]) for run in stored.result_metadata["actions"]] == [
        (1, "succeeded"),
        (2, "succeeded"),
        (3, BRANCH_SKIPPED_STATUS),
        (4, BRANCH_SKIPPED_STATUS),
        (5, "succeeded"),
    ]
    # The false branch's own result is recorded and the true branch's is absent.
    assert stored.result_metadata["actions"][4]["output"] == {"save_code": "S9001"}
    assert "output" not in stored.result_metadata["actions"][2]
    assert "output" not in stored.result_metadata["actions"][3]


@pytest.mark.asyncio
async def test_the_condition_result_and_selection_are_persisted():
    repo = InMemoryTaskRepository()
    registry = _branch_registry([])
    task, occurrence = await _start(repo, _branch_actions())
    coordinator, _ = _coordinator(repo, registry=registry)

    await coordinator.execute(occurrence)

    condition_run = (await _stored(repo, task.id)).result_metadata["actions"][1]
    assert condition_run["position"] == 2
    assert condition_run["tool"] == CONDITION_TOOL
    assert condition_run["status"] == "succeeded"
    assert condition_run["output"] == {"matched": True, "selected_branch": "true"}
    assert isinstance(condition_run["output"]["matched"], bool)


@pytest.mark.asyncio
async def test_the_condition_is_not_a_tool_call_and_never_reaches_the_executor():
    """The ToolExecutor stays the only thing that executes, and the condition is
    not one of its calls: it consumes a result the executor already produced."""
    repo = InMemoryTaskRepository()
    calls = []
    registry = _branch_registry(calls)
    task, occurrence = await _start(repo, _branch_actions())
    coordinator, executor = _coordinator(
        repo, registry=registry, executor_factory=CountingExecutor
    )

    await coordinator.execute(occurrence)

    executed = [call["name"] for batch in executor.batches for call in batch]
    assert executed == ["web_search", "save_by_link", "update_save_tags"]
    assert CONDITION_TOOL not in executed
    assert [len(batch) for batch in executor.batches] == [1, 1, 1]
    assert all(call["scheduled"] for call in calls)


# ── 6/7/13: fail closed — an invalid condition never selects a branch ──────


@pytest.mark.asyncio
async def test_an_unresolvable_condition_recorded_in_the_chain_fails_closed():
    """The source action neither produced the field nor succeeded: the
    occurrence fails deterministically and NEITHER branch runs."""
    repo = InMemoryTaskRepository()
    calls = []
    registry = ToolRegistry()
    # web_search declares is_closed but never returns it this run.
    registry.register(ChainTool("web_search", calls, data={"top_url": "u"},
                                fields=("is_closed", "top_url")))
    registry.register(ChainTool("save_by_link", calls, data={"save_code": "S0042"},
                                fields=("save_code",)))
    registry.register(ChainTool("update_save_tags", calls, data={"save_code": "S0042"},
                                fields=("save_code",)))
    task, occurrence = await _start(repo, _branch_actions())
    coordinator, _ = _coordinator(repo, registry=registry)

    result = await coordinator.execute(occurrence)

    assert not result.success and result.status == "failed"
    # The deterministic reason is persisted on the condition's own run record;
    # the occurrence-level class follows the existing failure contract unchanged
    # (a deterministic action failure is not retryable — so the condition is
    # never silently re-evaluated on a retry either).
    assert _names(calls) == ["web_search"]      # neither branch ran
    stored = await _stored(repo, task.id)
    assert stored.status == "failed"
    assert stored.error_metadata["error_class"] == "unclassified"
    assert stored.error_metadata["actions"][1]["status"] == "failed"
    assert "condition_field_unavailable" in stored.error_metadata["actions"][1]["error"]
    assert stored.error_metadata["actions"][2]["status"] == "pending"
    assert stored.error_metadata["actions"][4]["status"] == "pending"


@pytest.mark.asyncio
async def test_a_source_that_did_not_succeed_fails_the_condition_closed():
    repo = InMemoryTaskRepository()
    calls = []
    registry = _branch_registry(calls, search_plan=[ValueError("search exploded")])
    task, occurrence = await _start(repo, _branch_actions())
    coordinator, _ = _coordinator(repo, registry=registry)

    result = await coordinator.execute(occurrence)

    assert not result.success
    assert _names(calls) == ["web_search"]
    stored = await _stored(repo, task.id)
    assert stored.error_metadata["actions"][0]["status"] == "failed"
    assert stored.error_metadata["actions"][1]["status"] == "pending"


@pytest.mark.asyncio
async def test_a_malformed_stored_condition_fails_closed_before_any_execution():
    repo = InMemoryTaskRepository()
    calls = []
    registry = _branch_registry(calls)
    actions = _branch_actions()
    actions[1]["condition"]["operator"] = "greater_than"
    task, occurrence = await _start(repo, actions)
    coordinator, _ = _coordinator(repo, registry=registry)

    result = await coordinator.execute(occurrence)

    assert not result.success
    assert "invalid_condition" in result.error
    assert "operator must be one of" in result.error
    assert calls == []
    assert (await _stored(repo, task.id)).status == "failed"


@pytest.mark.asyncio
async def test_a_stored_condition_with_a_nested_predicate_fails_closed():
    repo = InMemoryTaskRepository()
    calls = []
    registry = _branch_registry(calls)
    actions = _branch_actions()
    actions[1]["condition"]["value"] = {"greater_than": 3}
    task, occurrence = await _start(repo, actions)
    coordinator, _ = _coordinator(repo, registry=registry)

    result = await coordinator.execute(occurrence)

    assert not result.success and "invalid_condition" in result.error
    assert calls == []


# ── 16/17/18/19/20: restart, resume, and no branch switching ───────────────


@pytest.mark.asyncio
async def test_a_restart_after_the_condition_resumes_the_selected_branch():
    """The condition is evaluated once; a crash during the TRUE branch resumes
    the TRUE branch, replays nothing, and never starts the FALSE branch."""
    repo = InMemoryTaskRepository()
    calls = []
    registry = _branch_registry(calls, tag_plan=[TimeoutError("provider stalled")])
    task, occurrence = await _start(repo, _branch_actions())
    coordinator, _ = _coordinator(repo, registry=registry)

    first = await coordinator.execute(occurrence)
    assert not first.success and first.status == "retry_pending"
    assert _names(calls) == ["web_search", "save_by_link", "update_save_tags"]
    parked = await _stored(repo, task.id)
    assert parked.error_metadata["actions"][1]["output"] == {
        "matched": True, "selected_branch": "true"
    }
    calls.clear()

    # A NEW process: new coordinator and registry doubles over the same store.
    restarted_coordinator, _ = _coordinator(repo, registry=registry)
    claimed = await repo.claim_occurrence(OWNER, task.id, "k")
    second = await restarted_coordinator.execute(claimed)

    assert second.success, second.error
    assert _names(calls) == ["update_save_tags"]        # only the failed action retried
    stored = await _stored(repo, task.id)
    assert stored.status == "succeeded"
    assert [run["status"] for run in stored.result_metadata["actions"]] == [
        "succeeded", "succeeded", "succeeded", "succeeded", BRANCH_SKIPPED_STATUS,
    ]


@pytest.mark.asyncio
async def test_a_restart_during_the_selected_branch_keeps_the_same_branch():
    """After a restart the recorded selection is still TRUE: the condition is
    not re-evaluated and the FALSE branch never becomes reachable."""
    repo = InMemoryTaskRepository()
    calls = []
    registry = _branch_registry(calls, tag_plan=[TimeoutError("stalled")])
    task, occurrence = await _start(repo, _branch_actions())
    coordinator, _ = _coordinator(repo, registry=registry)
    await coordinator.execute(occurrence)
    calls.clear()

    # The recorded selection, read straight back from the durable record.
    stored = await _stored(repo, task.id)
    condition_run = stored.error_metadata["actions"][1]
    matched, reason = evaluate_condition(
        _branch_actions()[1][CONDITION_KEY], stored.error_metadata["actions"]
    )
    assert (matched, reason) == (True, "")
    assert condition_run["output"]["selected_branch"] == "true"

    # The source result is unchanged and the source action is NOT re-executed.
    restarted_coordinator, _ = _coordinator(repo, registry=registry)
    claimed = await repo.claim_occurrence(OWNER, task.id, "k")
    result = await restarted_coordinator.execute(claimed)

    assert result.success, result.error
    assert _names(calls) == ["update_save_tags"]
    assert "web_search" not in _names(calls)


@pytest.mark.asyncio
async def test_the_condition_is_evaluated_exactly_once_across_attempts(monkeypatch):
    import backend.ai.task_execution as task_execution

    repo = InMemoryTaskRepository()
    calls = []
    registry = _branch_registry(calls, tag_plan=[TimeoutError("stalled")])
    task, occurrence = await _start(repo, _branch_actions())
    coordinator, _ = _coordinator(repo, registry=registry)

    seen = []
    real = task_execution.evaluate_condition

    def counting(condition, runs):
        seen.append(condition)
        return real(condition, runs)

    monkeypatch.setattr(task_execution, "evaluate_condition", counting)

    await coordinator.execute(occurrence)          # attempt 1 evaluates the condition
    assert len(seen) == 1
    claimed = await repo.claim_occurrence(OWNER, task.id, "k")
    await coordinator.execute(claimed)             # attempt 2 must NOT re-evaluate

    assert len(seen) == 1


@pytest.mark.asyncio
async def test_a_failed_selected_action_never_falls_back_to_the_other_branch():
    repo = InMemoryTaskRepository()
    calls = []
    registry = _branch_registry(calls, tag_plan=[ValueError("tag rejected")])
    task, occurrence = await _start(repo, _branch_actions())
    coordinator, _ = _coordinator(repo, registry=registry)

    result = await coordinator.execute(occurrence)

    assert not result.success and result.status == "failed"
    assert _names(calls) == ["web_search", "save_by_link", "update_save_tags"]
    stored = await _stored(repo, task.id)
    assert stored.status == "failed"
    # The FALSE branch is durably marked as never-to-run, not as "retry me later".
    assert stored.error_metadata["actions"][4]["status"] == BRANCH_SKIPPED_STATUS
    assert stored.error_metadata["actions"][1]["output"]["selected_branch"] == "true"
    assert stored.error_metadata["error_class"] == "unclassified"


@pytest.mark.asyncio
async def test_a_skipped_branch_is_never_executed_even_if_it_is_claimed_again():
    repo = InMemoryTaskRepository()
    calls = []
    registry = _branch_registry(
        calls, closed=False, save_plan=[ToolResult(True, "ok", {"save_code": "S9001"})]
    )
    task, occurrence = await _start(repo, _branch_actions())
    coordinator, _ = _coordinator(repo, registry=registry)

    first = await coordinator.execute(occurrence)
    assert first.success and _names(calls) == ["web_search", "save_by_link"]

    # A defensive re-claim: the occurrence is terminal, and even a forced claim
    # cannot make the unselected branch run.
    claimed = await repo.claim_occurrence(OWNER, task.id, "k")
    assert claimed is None
    assert _names(calls) == ["web_search", "save_by_link"]


# ── 11/12: ownership — one occurrence, one record, one selection ───────────


@pytest.mark.asyncio
async def test_a_condition_reads_only_its_own_occurrence():
    """Two occurrences of the same task each decide from their OWN recorded
    result: the selection is per occurrence, never shared or inherited."""
    repo = InMemoryTaskRepository()
    calls = []
    registry = _branch_registry(calls)
    actions = _branch_actions()
    task = await repo.create_task(OWNER, _task_payload(actions))
    for key, closed in (("first", True), ("second", False)):
        await repo.create_occurrence(OWNER, {
            "task_id": task.id, "occurrence_key": key, "definition_version": task.version,
            "action_snapshot": actions, "scheduled_for": NOW,
        })
        claimed = await repo.claim_occurrence(OWNER, task.id, key)
        registry = _branch_registry(calls, closed=closed)
        coordinator, _ = _coordinator(repo, registry=registry)
        result = await coordinator.execute(claimed)
        assert result.success, result.error

    first = await _stored(repo, task.id, "first")
    second = await _stored(repo, task.id, "second")
    assert first.result_metadata["actions"][1]["output"] == {
        "matched": True, "selected_branch": "true"
    }
    assert second.result_metadata["actions"][1]["output"] == {
        "matched": False, "selected_branch": "false"
    }

    # A foreign owner can neither execute nor read the occurrence, and an empty
    # record proves the condition is occurrence-scoped, not global.
    other, _ = _coordinator(repo, owner=OTHER_OWNER, registry=registry)
    assert (await other.execute(first)).error == "owner_mismatch"
    assert evaluate_condition(actions[1][CONDITION_KEY], []) == (
        None, "condition_source_not_succeeded (action 1)"
    )


# ── 21/22: Phase 3A references and Phase 3B waits stay intact ──────────────


@pytest.mark.asyncio
async def test_phase_3a_references_still_resolve_inside_a_selected_branch():
    repo = InMemoryTaskRepository()
    calls = []
    registry = _branch_registry(calls)
    task, occurrence = await _start(repo, _branch_actions())
    coordinator, _ = _coordinator(repo, registry=registry)

    await coordinator.execute(occurrence)

    # Before-the-branch reference (SEARCH result → both branches) and
    # inside-the-branch reference (TRUE SAVE → TRUE TAG) both resolved.
    assert calls[1]["arguments"]["link"] == "https://t.me/c/1/2"
    assert calls[2]["arguments"]["save_code"] == "S0042"
    assert calls[2]["arguments"]["tags"] == ["تعطیلی"]
    stored = await _stored(repo, task.id)
    assert stored.result_metadata["actions"][0]["output"]["top_url"] == "https://t.me/c/1/2"


@pytest.mark.asyncio
async def test_a_wait_inside_the_selected_branch_parks_and_resumes():
    repo = InMemoryTaskRepository()
    calls = []
    registry = _branch_registry(calls)
    actions = _branch_actions()
    actions[3][WAIT_KEY] = BOUNDARY_ISO
    task, occurrence = await _start(repo, actions)
    coordinator, _ = _coordinator(repo, registry=registry)

    first = await coordinator.execute(occurrence, now=NOW)

    assert first.status == "waiting"
    assert _names(calls) == ["web_search", "save_by_link"]
    parked = await _stored(repo, task.id)
    assert parked.status == "retry_pending" and parked.retry_at == BOUNDARY
    assert [run["status"] for run in parked.result_metadata["actions"]] == [
        "succeeded", "succeeded", "succeeded", "pending", BRANCH_SKIPPED_STATUS,
    ]

    claimed = await repo.claim_occurrence(OWNER, task.id, "k")
    second = await coordinator.execute(claimed, now=BOUNDARY + timedelta(minutes=1))

    assert second.success, second.error
    assert _names(calls) == ["web_search", "save_by_link", "update_save_tags"]
    stored = await _stored(repo, task.id)
    assert stored.status == "succeeded"
    assert [run["status"] for run in stored.result_metadata["actions"]] == [
        "succeeded", "succeeded", "succeeded", "succeeded", BRANCH_SKIPPED_STATUS,
    ]


@pytest.mark.asyncio
async def test_an_unselected_branch_boundary_can_never_park_the_chain():
    """Gating precedes the wait check: the FALSE branch's future boundary is
    irrelevant while the TRUE branch is selected."""
    repo = InMemoryTaskRepository()
    calls = []
    registry = _branch_registry(calls)
    actions = _branch_actions()
    actions[4][WAIT_KEY] = BOUNDARY_ISO
    task, occurrence = await _start(repo, actions)
    coordinator, _ = _coordinator(repo, registry=registry)

    result = await coordinator.execute(occurrence, now=NOW)

    assert result.success, result.error
    assert _names(calls) == ["web_search", "save_by_link", "update_save_tags"]
    stored = await _stored(repo, task.id)
    assert stored.status == "succeeded"          # never parked on the dead boundary
    assert stored.result_metadata["actions"][4]["status"] == BRANCH_SKIPPED_STATUS


# ── 23/25: one scheduler, one execution path ───────────────────────────────


@pytest.mark.asyncio
async def test_the_scheduler_remains_the_one_that_serves_branch_occurrences():
    repo = InMemoryTaskRepository()
    calls = []
    registry = _branch_registry(calls)
    service = TaskCreationService(repo, OWNER, tool_registry=registry)
    task = await service.create({
        "label": "conditional",
        "schedule_type": "once",
        "schedule": {"at": "2026-09-26T14:00:00", "timezone": "UTC"},
        "timezone": "UTC",
        "notification_destination": {},
        "actions": _branch_actions(),
    }, START - timedelta(minutes=30))

    coordinator, _ = _coordinator(repo, registry=registry)
    scheduler = TaskScheduler(repo, OWNER, coordinator, outcome_notifier=None)

    assert await scheduler.run_once(now=START) == 1
    assert _names(calls) == ["web_search", "save_by_link", "update_save_tags"]
    occurrences = await repo.list_occurrences(OWNER, task.id)
    assert len(occurrences) == 1
    assert occurrences[0].status == "succeeded"
    # Exactly one occurrence, keyed by the scheduled boundary, served once.
    assert occurrences[0].occurrence_key == f"{task.id}:{START.isoformat()}"
    assert await scheduler.run_once(now=START) == 0


# ── creation path: the model expresses conditions through the SAME contract ─


@pytest.mark.asyncio
async def test_creation_accepts_a_valid_conditional_chain_and_rejects_invalid_ones():
    registry = _registry()
    repo = InMemoryTaskRepository()
    service = TaskCreationService(repo, OWNER, tool_registry=registry)

    created = await service.create({
        "label": "conditional", "schedule_type": "once",
        "schedule": {"at": "2027-01-01T09:00:00", "timezone": "UTC"},
        "timezone": "UTC", "notification_destination": {},
        "actions": _branch_actions(),
    }, NOW)
    assert created.actions[1] == _branch_actions()[1]

    bad_chains = (
        # Unsupported operator.
        [dict(_branch_actions()[0]),
         {"condition": {"source": {"action": 1, "field": "is_closed"},
                        "operator": "greater_than", "value": 1}},
         dict(_branch_actions()[2]), dict(_branch_actions()[3]), dict(_branch_actions()[4])],
        # Cross-branch reference.
        [*_branch_actions()[:4],
         {"name": "save_by_link",
          "arguments": {"link": {"$ref": {"action": 1, "field": "top_url"}},
                        "save_code": {"$ref": {"action": 3, "field": "save_code"}}},
          "branch": "false"}],
        # A branch label with no condition.
        [{"name": "send_message", "arguments": {"text": "hi"}, "branch": "true"}],
    )
    for actions in bad_chains:
        with pytest.raises(TaskCreationError):
            await service.create({
                "label": "bad", "schedule_type": "once",
                "schedule": {"at": "2027-01-01T09:00:00", "timezone": "UTC"},
                "timezone": "UTC", "notification_destination": {}, "actions": actions,
            }, NOW)


def test_the_model_candidate_boundary_keeps_the_bounded_condition():
    candidate = TaskCandidate.from_untrusted({
        "label": "conditional", "schedule_type": "once",
        "schedule": {"at": "2027-01-01T09:00:00", "timezone": "UTC"},
        "timezone": "UTC", "notification_destination": {},
        "actions": _branch_actions(),
    })
    assert candidate.actions == _branch_actions()

    def _candidate(actions):
        return {
            "label": "conditional", "schedule_type": "once",
            "schedule": {"at": "2027-01-01T09:00:00", "timezone": "UTC"},
            "timezone": "UTC", "notification_destination": {}, "actions": actions,
        }

    # A condition dressed as a tool call, a stray key beside it, an unknown
    # operator, a non-scalar value, and a fabricated branch label all fail here.
    for actions in (
        [{"name": "condition", CONDITION_KEY: {"source": {"action": 1, "field": "x"},
                                              "operator": "equals", "value": 1}}],
        [{CONDITION_KEY: {"source": {"action": 1, "field": "x"},
                          "operator": "equals", "value": 1}, "branch": "true"}],
        [{CONDITION_KEY: {"source": {"action": 1, "field": "x"},
                          "operator": "greater_than", "value": 1}}],
        [{CONDITION_KEY: {"source": {"action": 1, "field": "x"},
                          "operator": "equals", "value": {"nested": 1}}}],
        [{"name": "send_message", "arguments": {"text": "hi"}, BRANCH_KEY: "maybe"}],
    ):
        with pytest.raises(TaskCandidateError):
            TaskCandidate.from_untrusted(_candidate(actions))


def test_the_candidate_schema_declares_the_condition_and_branch():
    item_schema = CANDIDATE_SCHEMA["properties"]["actions"]["items"]
    # Phase 3C added the ONE action entry that declares no tool call, so the item
    # object no longer requires name + arguments; the deterministic candidate
    # boundary (not this schema) decides which of the two shapes an entry is.
    assert "required" not in item_schema
    assert {"condition", BRANCH_KEY} <= set(item_schema["properties"])
    assert item_schema["properties"][BRANCH_KEY]["enum"] == ["true", "false"]
    assert item_schema["additionalProperties"] is False
    assert "source" in item_schema["properties"][CONDITION_KEY]["description"]


# ── the acceptance chain, end to end through creation + the scheduler ──────


@pytest.mark.asyncio
async def test_end_to_end_search_condition_true_save_tag():
    """SEARCH → CONDITION → TRUE: SAVE → TAG (the FALSE SAVE must not run)."""
    repo = InMemoryTaskRepository()
    calls, events = [], []
    registry = _branch_registry(calls, closed=True, events=events)
    service = TaskCreationService(repo, OWNER, tool_registry=registry)
    task = await service.create({
        "label": "پری این موضوع رو سرچ کن؛ اگر دانشگاه فردا تعطیل بود ذخیره کن و تگ تعطیلی بزن",
        "schedule_type": "once",
        "schedule": {"at": "2026-09-26T14:00:00", "timezone": "UTC"},
        "timezone": "UTC",
        "notification_destination": {},
        "actions": _branch_actions(),
    }, START - timedelta(minutes=30))

    coordinator, _ = _coordinator(repo, registry=registry)
    scheduler = TaskScheduler(repo, OWNER, coordinator, outcome_notifier=None)
    assert await scheduler.run_once(now=START) == 1

    occurrence = (await repo.list_occurrences(OWNER, task.id))[0]
    assert occurrence.status == "succeeded"
    assert _names(calls) == ["web_search", "save_by_link", "update_save_tags"]
    assert calls[2]["arguments"]["tags"] == ["تعطیلی"]
    assert calls[2]["arguments"]["save_code"] == "S0042"
    assert [run["status"] for run in occurrence.result_metadata["actions"]] == [
        "succeeded", "succeeded", "succeeded", "succeeded", BRANCH_SKIPPED_STATUS,
    ]
    assert occurrence.result_metadata["actions"][1]["output"] == {
        "matched": True, "selected_branch": "true"
    }
    assert events == [
        ("start", "web_search"), ("end", "web_search"),
        ("start", "save_by_link"), ("end", "save_by_link"),
        ("start", "update_save_tags"), ("end", "update_save_tags"),
    ]
    assert json.dumps(occurrence.result_metadata)   # the record stays serializable


@pytest.mark.asyncio
async def test_end_to_end_search_condition_false_save():
    """SEARCH → CONDITION → FALSE: SAVE (no TAG, no TRUE SAVE)."""
    repo = InMemoryTaskRepository()
    calls = []
    registry = _branch_registry(
        calls, closed=False, save_plan=[ToolResult(True, "ok", {"save_code": "S9001"})]
    )
    service = TaskCreationService(repo, OWNER, tool_registry=registry)
    task = await service.create({
        "label": "پری این موضوع رو سرچ کن؛ اگر تعطیل نبود فقط ذخیره کن",
        "schedule_type": "once",
        "schedule": {"at": "2026-09-26T14:00:00", "timezone": "UTC"},
        "timezone": "UTC",
        "notification_destination": {},
        "actions": _branch_actions(),
    }, START - timedelta(minutes=30))

    coordinator, _ = _coordinator(repo, registry=registry)
    scheduler = TaskScheduler(repo, OWNER, coordinator, outcome_notifier=None)
    assert await scheduler.run_once(now=START) == 1

    occurrence = (await repo.list_occurrences(OWNER, task.id))[0]
    assert occurrence.status == "succeeded"
    assert _names(calls) == ["web_search", "save_by_link"]
    assert occurrence.result_metadata["actions"][1]["output"] == {
        "matched": False, "selected_branch": "false"
    }
    assert occurrence.result_metadata["actions"][3]["status"] == BRANCH_SKIPPED_STATUS
    assert occurrence.result_metadata["actions"][4]["output"] == {"save_code": "S9001"}


def test_the_condition_record_fits_its_bounded_budget():
    """The whole conditional record is structurally inside the existing
    8192-byte per-channel metadata bound (the runs list is bounded by 6144)."""
    actions = _branch_actions()
    layout, error = resolve_conditions(actions, _registry())
    assert error == ""
    runs = [
        {"position": position, "tool": action.get("name") or CONDITION_TOOL,
         "status": BRANCH_SKIPPED_STATUS}
        for position, action in enumerate(actions, start=1)
    ]
    runs[layout.condition_position - 1] = {
        "position": layout.condition_position, "tool": CONDITION_TOOL, "status": "succeeded",
        "output": {"matched": True, "selected_branch": "true"},
    }
    record = {
        "action_count": len(actions), "successful_action_count": len(actions) - 1,
        "duration_ms": 12.5, "terminal_status": "succeeded", "actions": runs,
    }
    assert len(json.dumps(record, ensure_ascii=False).encode()) < 8192
