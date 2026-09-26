"""Durable action chains — bounded result passing between a task's actions.

The product contract under test (Todo Part 3A): a durable task's ordered
actions execute SEQUENTIALLY through the single ToolExecutor; an action may
consume a DECLARED, bounded output field of an EARLIER action of the SAME
occurrence through one explicit reference shape, resolved deterministically by
the coordinator (never by the model, never guessed); every action carries
durable per-action state, so a retry resumes at the action that did not succeed
instead of replaying a side effect that already happened; and every unknown
reference fails the occurrence closed BEFORE anything runs.

Boundaries preserved by these tests: the ToolRegistry stays the capability
allowlist, the ToolExecutor stays the sole caller of ``tool.execute()``, the
coordinator keeps the single occurrence/claim authority, and no schema change
is involved — the record lives in the occurrence's own bounded metadata.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from backend.ai.database.task_repository import InMemoryTaskRepository
from backend.ai.task_contract import (
    MAX_ACTION_RUNS_BYTES,
    TaskContractError,
    action_reference_error,
    bounded_action_output,
    build_action_run,
    resolve_action_arguments,
    validate_action_reference,
)
from backend.ai.task_creation import TaskCreationError, TaskCreationService
from backend.ai.task_execution import TaskExecutionCoordinator
from backend.ai.task_scheduler import TaskScheduler
from backend.ai.tools.base import PermissionLevel, ToolResult
from backend.ai.tools.context import ToolContext
from backend.ai.tools.executor import ToolExecutor
from backend.ai.tools.registry import ToolRegistry
from backend.ai.tools.save import SaveTool
from backend.db import client as db_client

OWNER = 4242
OTHER_OWNER = 999
CHAT = -100123
NOW = datetime(2026, 9, 26, 12, 0, tzinfo=timezone.utc)


# ── Telegram/DB-shaped fakes (the external boundary only) ───────────────────


class FakeUser:
    def __init__(self, uid=11, first_name="Ali", last_name="Rezaei"):
        self.id = uid
        self.first_name = first_name
        self.last_name = last_name
        self.username = ""


class FakeSent:
    def __init__(self):
        self.chat_id = OWNER
        self.id = 900
        self.media = None


class FakeSourceMessage:
    """A source message as ``execute_save`` reads it (text-only source)."""

    def __init__(self, *, sender_id=11, chat_id=-100555, msg_id=200, text="source text"):
        self.sender_id = sender_id
        self.chat_id = chat_id
        self.id = msg_id
        self.text = text
        self.media = None

    async def get_sender(self):
        return FakeUser(uid=self.sender_id)


class FakeSaveClient:
    """Only the calls ``execute_save`` makes for a TEXT-only source."""

    def __init__(self, message=None):
        self.calls = []
        self._message = message

    async def send_message(self, entity, text):
        self.calls.append(("send_message", entity, text))
        return FakeSent()

    async def get_messages(self, chat_id, ids=None):
        self.calls.append(("get_messages", chat_id, ids))
        return self._message


@pytest.fixture(autouse=True)
def _clean_state():
    db_client._fallback["saved_items"] = []
    db_client._fallback["bot_logs"] = []
    yield
    db_client._fallback["saved_items"] = []
    db_client._fallback["bot_logs"] = []


# ── Registry/executor/coordinator harness ───────────────────────────────────


class ChainTool:
    """A registered tool double whose calls, results and chainable fields the
    test declares.

    It is a *double for the service layer*, not for the chain machinery: it
    still goes through the real ToolRegistry (capability allowlist) and the real
    ToolExecutor (the sole caller of ``execute()``), which is exactly what the
    chain contract exercises. ``plan`` supplies per-call outcomes in call order
    (an exception entry is raised, exactly as a real tool failure would be) and
    is consumed once; ``events`` records start/end so ordering is provable.
    """

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
            raise entry  # the executor classifies it exactly as it does in prod
        return entry if entry is not None else ToolResult(True, "ok", dict(self.data))


class CountingExecutor(ToolExecutor):
    """The real executor, remembering the batches it was asked to run."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.batches = []

    async def execute_calls(self, tool_calls, **kwargs):
        self.batches.append([dict(call) for call in tool_calls])
        return await super().execute_calls(tool_calls, **kwargs)


def _actions(*specs):
    return [{"name": name, "arguments": dict(args)} for name, args in specs]


def _task_payload(actions):
    return {
        "label": "chain",
        "schedule_type": "once",
        "schedule": {"at": "2027-01-01T09:00:00", "timezone": "UTC"},
        "timezone": "UTC",
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


def _statuses(runs):
    return [run["status"] for run in runs]


# ── 1/2/3: ordering, sequentiality, per-action records ──────────────────────


@pytest.mark.asyncio
async def test_single_action_task_succeeds_and_records_its_run():
    repo = InMemoryTaskRepository()
    calls = []
    registry = ToolRegistry()
    registry.register(ChainTool("send_message", calls, data={"text": "hello"}))
    task, occurrence = await _start(repo, _actions(("send_message", {"text": "hello"})))
    coordinator, _ = _coordinator(repo, registry=registry)

    result = await coordinator.execute(occurrence)

    assert result.success and result.status == "succeeded"
    assert [call["name"] for call in calls] == ["send_message"]
    stored = await _stored(repo, task.id)
    assert stored.status == "succeeded"
    assert stored.result_metadata["action_count"] == 1
    assert stored.result_metadata["successful_action_count"] == 1
    assert stored.result_metadata["terminal_status"] == "succeeded"
    assert [(run["position"], run["tool"], run["status"]) for run in stored.result_metadata["actions"]] == [
        (1, "send_message", "succeeded")
    ]


@pytest.mark.asyncio
async def test_multi_action_chain_executes_in_order_and_records_every_action():
    repo = InMemoryTaskRepository()
    calls = []
    registry = ToolRegistry()
    for name in ("one", "two", "three"):
        registry.register(ChainTool(name, calls))
    actions = _actions(("one", {}), ("two", {"n": 2}), ("three", {}))
    task, occurrence = await _start(repo, actions)
    coordinator, _ = _coordinator(repo, registry=registry)

    result = await coordinator.execute(occurrence)

    assert result.success, result.error
    assert [call["name"] for call in calls] == ["one", "two", "three"]
    assert calls[1]["arguments"] == {"n": 2}
    stored = await _stored(repo, task.id)
    assert [run["status"] for run in stored.result_metadata["actions"]] == [
        "succeeded", "succeeded", "succeeded"
    ]
    assert stored.result_metadata["action_count"] == 3
    assert stored.result_metadata["successful_action_count"] == 3


@pytest.mark.asyncio
async def test_action_two_does_not_start_before_action_one_finishes():
    repo = InMemoryTaskRepository()
    calls, events = [], []
    registry = ToolRegistry()
    registry.register(ChainTool("one", calls, events=events))
    registry.register(ChainTool("two", calls, events=events))
    _, occurrence = await _start(repo, _actions(("one", {}), ("two", {})))
    coordinator, _ = _coordinator(repo, registry=registry)

    await coordinator.execute(occurrence)

    assert events == [
        ("start", "one"), ("end", "one"),
        ("start", "two"), ("end", "two"),
    ]


# ── 4/5: bounded structured results and reference resolution ────────────────


@pytest.mark.asyncio
async def test_only_the_declared_bounded_fields_are_recorded():
    repo = InMemoryTaskRepository()
    calls = []
    registry = ToolRegistry()
    registry.register(ChainTool(
        "web_search", calls,
        data={"top_url": "https://example.com/a", "internal_blob": {"huge": "x" * 500}},
        fields=("top_url",),
    ))
    task, occurrence = await _start(repo, _actions(("web_search", {"query": "x"})))
    coordinator, _ = _coordinator(repo, registry=registry)

    await coordinator.execute(occurrence)

    run = (await _stored(repo, task.id)).result_metadata["actions"][0]
    assert run["output"] == {"top_url": "https://example.com/a"}


@pytest.mark.asyncio
async def test_action_two_consumes_action_ones_declared_output():
    repo = InMemoryTaskRepository()
    calls = []
    registry = ToolRegistry()
    registry.register(ChainTool(
        "save_by_link", calls, data={"save_code": "S0042", "mode": "deep"},
        fields=("save_code",),
    ))
    registry.register(ChainTool(
        "update_save_tags", calls, data={"save_code": "S0042"}, fields=("save_code",),
    ))
    actions = _actions(
        ("save_by_link", {"link": "https://t.me/c/1/2"}),
        ("update_save_tags", {
            "save_code": {"$ref": {"action": 1, "field": "save_code"}},
            "tags": ["university"], "mode": "add",
        }),
    )
    task, occurrence = await _start(repo, actions)
    coordinator, _ = _coordinator(repo, registry=registry)

    result = await coordinator.execute(occurrence)

    assert result.success, result.error
    assert calls[1]["arguments"]["save_code"] == "S0042"
    assert calls[1]["arguments"]["tags"] == ["university"]
    stored = await _stored(repo, task.id)
    assert stored.result_metadata["actions"][1]["output"] == {"save_code": "S0042"}


@pytest.mark.asyncio
async def test_end_to_end_search_save_tag_chain():
    """The Phase 3A acceptance chain: SEARCH → SAVE → TAG.

    The doubles mirror the REAL registered tools' names and argument contracts
    (``web_search`` / ``save_by_link`` / ``update_save_tags``) and stand in for
    the services they wrap; the registry, the executor, the coordinator, the
    reference resolution and the durable occurrence state are the real code.
    """
    repo = InMemoryTaskRepository()
    calls, events = [], []
    registry = ToolRegistry()
    registry.register(ChainTool(
        "web_search", calls, events=events,
        data={"top_title": "University closed tomorrow", "top_url": "https://t.me/c/1/2"},
        fields=("top_title", "top_url"),
    ))
    registry.register(ChainTool(
        "save_by_link", calls, events=events, data={"save_code": "S0042"}, fields=("save_code",),
    ))
    registry.register(ChainTool(
        "update_save_tags", calls, events=events,
        data={"save_code": "S0042"}, fields=("save_code",),
    ))
    actions = _actions(
        ("web_search", {"query": "دانشگاه فردا تعطیل"}),
        ("save_by_link", {"link": {"$ref": {"action": 1, "field": "top_url"}}}),
        ("update_save_tags", {
            "save_code": {"$ref": {"action": 2, "field": "save_code"}},
            "tags": ["دانشگاه"], "mode": "add",
        }),
    )
    task, occurrence = await _start(repo, actions)
    coordinator, _ = _coordinator(repo, registry=registry)

    result = await coordinator.execute(occurrence)

    # 1/2 — SEARCH executed and returned its bounded structured result.
    assert result.success, result.error
    assert calls[0]["name"] == "web_search"
    assert calls[0]["scheduled"] is True and calls[0]["owner"] == OWNER
    # 3/4 — SAVE received the referenced SEARCH output, not its message text.
    assert calls[1]["arguments"]["link"] == "https://t.me/c/1/2"
    # 5/6 — TAG received the save_code through the reference mechanism and ran.
    assert calls[2]["arguments"]["save_code"] == "S0042"
    assert calls[2]["arguments"]["tags"] == ["دانشگاه"]
    # 7 — the workflow records per-action success durably.
    stored = await _stored(repo, task.id)
    assert stored.status == "succeeded"
    assert [(run["position"], run["tool"], run["status"]) for run in stored.result_metadata["actions"]] == [
        (1, "web_search", "succeeded"),
        (2, "save_by_link", "succeeded"),
        (3, "update_save_tags", "succeeded"),
    ]
    assert stored.result_metadata["actions"][0]["output"] == {
        "top_title": "University closed tomorrow", "top_url": "https://t.me/c/1/2"
    }
    assert events == [
        ("start", "web_search"), ("end", "web_search"),
        ("start", "save_by_link"), ("end", "save_by_link"),
        ("start", "update_save_tags"), ("end", "update_save_tags"),
    ]


# ── 8/9/10/11: reference validation (fail closed, bounded) ──────────────────


@pytest.mark.parametrize("value,fragment", [
    ({"$ref": {"action": 1}}, "must name exactly"),
    ({"$ref": {"action": 1, "field": "x", "extra": 1}}, "must name exactly"),
    ({"$ref": 1}, "must name exactly"),
    ({"$ref": {"action": 1, "field": "x"}, "extra": 1}, "must contain only"),
    ({"$ref": {"action": 0, "field": "x"}}, "positive action number"),
    ({"$ref": {"action": True, "field": "x"}}, "positive action number"),
    ({"$ref": {"action": "1", "field": "x"}}, "positive action number"),
    ({"$ref": {"action": 1, "field": ""}}, "bounded nonblank name"),
    ({"$ref": {"action": 1, "field": "x" * 65}}, "bounded nonblank name"),
])
def test_the_only_accepted_reference_shape(value, fragment):
    with pytest.raises(TaskContractError) as excinfo:
        validate_action_reference(value)
    assert fragment in str(excinfo.value)


def _reference_registry(*tools):
    registry = ToolRegistry()
    for tool in tools:
        registry.register(tool)
    return registry


def test_reference_validation_rejects_every_unresolvable_shape():
    calls = []
    registry = _reference_registry(
        ChainTool("search", calls, data={"url": "u"}, fields=("url",)),
        ChainTool("tag", calls, data={"save_code": "S1"}, fields=("save_code",)),
    )
    # A later action, the action itself, and a non-existent position.
    assert "not an earlier action" in action_reference_error(
        _actions(("search", {"q": {"$ref": {"action": 2, "field": "save_code"}}}),
                 ("tag", {"save_code": "S1"})), registry)
    assert "not an earlier action" in action_reference_error(
        _actions(("search", {"q": {"$ref": {"action": 1, "field": "url"}}})), registry)
    assert "not an earlier action" in action_reference_error(
        _actions(("search", {"q": "x"}), ("tag", {"save_code": "S1"}),
                 ("search", {"q": {"$ref": {"action": 9, "field": "url"}}})), registry)
    # An undeclared output field, and a target tool that is not registered.
    assert "does not declare as chainable" in action_reference_error(
        _actions(("search", {"q": "x"}),
                 ("tag", {"save_code": {"$ref": {"action": 1, "field": "save_code"}}})), registry)
    assert "not registered" in action_reference_error(
        _actions(("missing_tool", {"q": "x"}),
                 ("tag", {"save_code": {"$ref": {"action": 1, "field": "save_code"}}})), registry)
    # A reserved key nested anywhere below an argument value is refused.
    assert "nests a reserved" in action_reference_error(
        _actions(("search", {"q": "x"}),
                 ("tag", {"tags": ["a", {"$ref": {"action": 1, "field": "url"}}]})), registry)
    assert "nests a reserved" in action_reference_error(
        _actions(("search", {"q": {"wrapper": {"$ref": {"action": 1, "field": "url"}}}})), registry)
    assert "nests a reserved" in action_reference_error(
        _actions(("search", {"q": "x"}), ("tag", {"args": {"$other": 1}})), registry)
    # A valid chain passes, and a reference-free list always passes.
    assert action_reference_error(
        _actions(("search", {"q": "x"}),
                 ("tag", {"save_code": {"$ref": {"action": 1, "field": "url"}}})), registry) is None
    assert action_reference_error(_actions(("search", {"q": "x"})), registry) is None


def test_reference_validation_is_skipped_without_a_registry_but_positions_hold():
    assert action_reference_error(
        _actions(("search", {"q": "x"}),
                 ("tag", {"save_code": {"$ref": {"action": 1, "field": "save_code"}}})), None) is None
    assert "not an earlier action" in action_reference_error(
        _actions(("search", {"q": {"$ref": {"action": 3, "field": "x"}}})), None)


@pytest.mark.asyncio
async def test_creation_accepts_a_valid_chain_and_rejects_unresolvable_references():
    calls = []
    registry = _reference_registry(
        ChainTool("search", calls, data={"url": "u"}, fields=("url",)),
        ChainTool("tag", calls, data={"save_code": "S1"}, fields=("save_code",)),
    )
    repo = InMemoryTaskRepository()
    service = TaskCreationService(repo, OWNER, tool_registry=registry)

    created = await service.create(
        {
            "label": "chained", "schedule_type": "once",
            "schedule": {"at": "2027-01-01T09:00:00", "timezone": "UTC"},
            "timezone": "UTC", "notification_destination": {},
            "actions": _actions(
                ("search", {"q": "x"}),
                ("tag", {"save_code": {"$ref": {"action": 1, "field": "url"}}}),
            ),
        },
        NOW,
    )
    assert created.actions[1]["arguments"]["save_code"] == {"$ref": {"action": 1, "field": "url"}}

    for bad in (
        _actions(("search", {"q": {"$ref": {"action": 2, "field": "save_code"}}}),
                 ("tag", {"save_code": "S1"})),
        _actions(("search", {"q": "x"}),
                 ("tag", {"save_code": {"$ref": {"action": 1, "field": "nope"}}})),
        _actions(("search", {"q": "x"}), ("tag", {"tags": [{"$ref": {"action": 1, "field": "url"}}]})),
    ):
        with pytest.raises(TaskCreationError):
            await service.create(
                {
                    "label": "bad chain", "schedule_type": "once",
                    "schedule": {"at": "2027-01-01T09:00:00", "timezone": "UTC"},
                    "timezone": "UTC", "notification_destination": {}, "actions": bad,
                },
                NOW,
            )


@pytest.mark.asyncio
async def test_creation_refuses_a_reference_on_a_generated_task():
    calls = []
    registry = _reference_registry(
        ChainTool("search", calls, data={"url": "u"}, fields=("url",)),
        ChainTool("tag", calls, data={"save_code": "S1"}, fields=("save_code",)),
    )
    service = TaskCreationService(InMemoryTaskRepository(), OWNER, tool_registry=registry)

    with pytest.raises(TaskCreationError) as excinfo:
        await service.create(
            {
                "label": "generated chain", "schedule_type": "once",
                "schedule": {"at": "2027-01-01T09:00:00", "timezone": "UTC"},
                "timezone": "UTC", "notification_destination": {},
                "ai_instruction": "write about the university",
                "actions": _actions(
                    ("search", {"q": "x"}),
                    ("tag", {"save_code": {"$ref": {"action": 1, "field": "url"}}}),
                ),
            },
            NOW,
        )
    assert "per-occurrence generated arguments" in str(excinfo.value)


def test_bounded_action_output_never_coerces_an_unusable_value():
    assert bounded_action_output({"a": "x", "b": "y" * 200, "c": 3}, ("a", "b", "c")) == {
        "a": "x", "c": 3
    }
    assert bounded_action_output({"a": {"nested": 1}}, ("a",)) == {}
    assert bounded_action_output({"a": None}, ("a",)) == {}
    assert bounded_action_output({"a": "   "}, ("a",)) == {}
    assert bounded_action_output({"a": "x"}, ("a",)) == {"a": "x"}
    # The field count itself is bounded, so one run cannot fill the budget.
    assert bounded_action_output(
        {"a": "1", "b": "2", "c": "3", "d": "4"}, ("a", "b", "c", "d")
    ) == {"a": "1", "b": "2", "c": "3"}


@pytest.mark.asyncio
async def test_an_oversized_declared_value_is_not_chainable_and_fails_closed():
    repo = InMemoryTaskRepository()
    calls = []
    registry = ToolRegistry()
    registry.register(ChainTool(
        "save_by_link", calls, data={"save_code": "S" * 200}, fields=("save_code",),
    ))
    registry.register(ChainTool("update_save_tags", calls, data={"save_code": "S1"}, fields=("save_code",)))
    _, occurrence = await _start(repo, _actions(
        ("save_by_link", {"link": "https://t.me/c/1/2"}),
        ("update_save_tags", {
            "save_code": {"$ref": {"action": 1, "field": "save_code"}},
            "tags": ["t"], "mode": "add",
        }),
    ))
    coordinator, _ = _coordinator(repo, registry=registry)

    result = await coordinator.execute(occurrence)

    assert not result.success
    stored = await _stored(repo, occurrence.task_id)
    assert "reference_field_unavailable" in stored.error_metadata["actions"][1]["error"]
    # The referencing action never executed with a guessed value.
    assert [call["name"] for call in calls] == ["save_by_link"]


def test_a_nested_reserved_key_is_refused_at_resolution():
    runs = [build_action_run(1, "search", "succeeded", output={"url": "u"})]
    resolved, reason = resolve_action_arguments(
        {"tags": ["a", {"$ref": {"action": 1, "field": "url"}}]}, runs
    )
    assert resolved == {} and "invalid_reference" in reason


def test_resolution_refuses_a_reference_the_record_cannot_satisfy():
    runs = [build_action_run(1, "search", "failed", error="boom")]
    resolved, reason = resolve_action_arguments(
        {"url": {"$ref": {"action": 1, "field": "url"}}}, runs
    )
    assert resolved == {} and "reference_target_not_succeeded" in reason
    resolved, reason = resolve_action_arguments(
        {"url": {"$ref": {"action": 1, "field": "url"}}},
        [build_action_run(1, "search", "succeeded", output={"other": "u"})],
    )
    assert resolved == {} and "reference_field_unavailable" in reason
    resolved, reason = resolve_action_arguments(
        {"url": {"$ref": {"action": 1, "field": "url"}}}, [{"junk": True}]
    )
    assert resolved == {} and "action_record_invalid" in reason


# ── 12/13: isolation and record integrity ──────────────────────────────────


@pytest.mark.asyncio
async def test_records_are_occurrence_scoped_so_a_new_occurrence_reruns_its_actions():
    repo = InMemoryTaskRepository()
    calls = []
    registry = ToolRegistry()
    registry.register(ChainTool("one", calls, data={"save_code": "S1"}, fields=("save_code",)))
    registry.register(ChainTool("two", calls, data={"save_code": "S1"}, fields=("save_code",)))
    actions = _actions(
        ("one", {}),
        ("two", {"save_code": {"$ref": {"action": 1, "field": "save_code"}}}),
    )
    task, first = await _start(repo, actions, key="k1")
    coordinator, _ = _coordinator(repo, registry=registry)
    assert (await coordinator.execute(first)).success

    # A NEW boundary of the SAME task is a NEW occurrence: it inherits no
    # per-action state from the previous one, so its actions really run.
    await repo.create_occurrence(OWNER, {
        "task_id": task.id, "occurrence_key": "k2", "definition_version": task.version,
        "action_snapshot": actions, "scheduled_for": NOW,
    })
    second = await repo.claim_occurrence(OWNER, task.id, "k2")
    calls.clear()
    assert (await coordinator.execute(second)).success
    assert [call["name"] for call in calls] == ["one", "two"]

    first_runs = (await _stored(repo, task.id, "k1")).result_metadata["actions"]
    second_runs = (await _stored(repo, task.id, "k2")).result_metadata["actions"]
    assert first_runs == second_runs  # same shape, independently recorded


@pytest.mark.asyncio
async def test_a_record_that_names_another_tool_fails_closed_before_any_execution():
    repo = InMemoryTaskRepository()
    calls = []
    registry = ToolRegistry()
    registry.register(ChainTool("one", calls))
    registry.register(ChainTool("two", calls))
    _, occurrence = await _start(
        repo, _actions(("one", {}), ("two", {})),
        error_metadata={"actions": [{"position": 1, "tool": "something_else", "status": "succeeded", "output": {}}]},
    )
    coordinator, _ = _coordinator(repo, registry=registry)

    result = await coordinator.execute(occurrence)

    assert not result.success and calls == []
    stored = await _stored(repo, occurrence.task_id)
    assert stored.status == "failed"
    assert "invalid_action_record" in stored.error_metadata["error_class"]


@pytest.mark.asyncio
async def test_a_malformed_record_fails_closed_before_any_execution():
    repo = InMemoryTaskRepository()
    calls = []
    registry = ToolRegistry()
    registry.register(ChainTool("one", calls))
    registry.register(ChainTool("two", calls))
    _, occurrence = await _start(
        repo, _actions(("one", {}), ("two", {})),
        error_metadata={"actions": [{"position": 1, "tool": "one"}]},
    )
    coordinator, _ = _coordinator(repo, registry=registry)

    result = await coordinator.execute(occurrence)

    assert not result.success and calls == []
    assert "invalid_action_record" in (await _stored(repo, occurrence.task_id)).error_metadata["error_class"]


# ── 14/15/16: failure, retry and resume ────────────────────────────────────


@pytest.mark.asyncio
async def test_action_failure_stops_the_chain_and_marks_later_actions_pending():
    repo = InMemoryTaskRepository()
    calls = []
    registry = ToolRegistry()
    registry.register(ChainTool("one", calls))
    registry.register(ChainTool(
        "two", calls,    plan=[ToolResult(False, "two refused")],
    ))
    registry.register(ChainTool("three", calls))
    _, occurrence = await _start(repo, _actions(("one", {}), ("two", {}), ("three", {})))
    coordinator, _ = _coordinator(repo, registry=registry)

    result = await coordinator.execute(occurrence)

    assert not result.success
    assert [call["name"] for call in calls] == ["one", "two"]  # three never ran
    stored = await _stored(repo, occurrence.task_id)
    assert stored.status == "failed"
    runs = stored.error_metadata["actions"]
    assert [(run["position"], run["status"]) for run in runs] == [
        (1, "succeeded"), (2, "failed"), (3, "pending")
    ]
    assert runs[1]["error"] == "two refused"
    assert stored.error_metadata["successful_action_count"] == 1


@pytest.mark.asyncio
async def test_retry_resumes_at_the_failed_action_without_replaying_action_one():
    repo = InMemoryTaskRepository()
    calls = []
    registry = ToolRegistry()
    registry.register(ChainTool("one", calls, data={"save_code": "S1"}, fields=("save_code",)))
    registry.register(ChainTool(
        "two", calls, data={"save_code": "S1"}, fields=("save_code",),
        plan=[__import__("asyncio").TimeoutError("tool timed out")],
    ))
    registry.register(ChainTool("three", calls, data={}, fields=("save_code",)))
    actions = _actions(
        ("one", {}),
        ("two", {"save_code": {"$ref": {"action": 1, "field": "save_code"}}}),
        ("three", {}),
    )
    task, occurrence = await _start(repo, actions)
    coordinator, _ = _coordinator(repo, registry=registry)

    first = await coordinator.execute(occurrence)

    assert first.status == "retry_pending"  # a timeout keeps its retry contract
    stored = await _stored(repo, task.id)
    assert stored.attempt == 2 and stored.status == "retry_pending"
    assert [(run["position"], run["status"]) for run in stored.error_metadata["actions"]] == [
        (1, "succeeded"), (2, "failed"), (3, "pending")
    ]

    # A fresh claim and a FRESH coordinator (new process) resume the chain.
    claimed = await repo.claim_occurrence(OWNER, task.id, "k")
    calls.clear()
    restarted, _ = _coordinator(repo, registry=registry)
    second = await restarted.execute(claimed)

    assert second.success, second.error
    assert [call["name"] for call in calls] == ["two", "three"]  # action one NOT replayed
    assert calls[0]["arguments"]["save_code"] == "S1"  # consumed the recorded result
    final = await _stored(repo, task.id)
    assert final.status == "succeeded"
    assert _statuses(final.result_metadata["actions"]) == ["succeeded", "succeeded", "succeeded"]


@pytest.mark.asyncio
async def test_a_chain_cut_mid_flight_is_failed_by_recovery_and_never_replayed():
    repo = InMemoryTaskRepository()
    calls = []
    registry = ToolRegistry()
    registry.register(ChainTool("one", calls))
    registry.register(ChainTool("two", calls))
    task, occurrence = await _start(repo, _actions(("one", {}), ("two", {})))
    coordinator, _ = _coordinator(repo, registry=registry)
    # The process died after action one: the occurrence is still `running` and
    # its durable progress is on the record.
    await repo.transition_occurrence(OWNER, task.id, "k", "running", result_metadata={
        "actions": [build_action_run(1, "one", "succeeded", output={})],
    })

    scheduler = TaskScheduler(repo, OWNER, coordinator, outcome_notifier=None)
    assert await scheduler.recover() == 1

    stored = await _stored(repo, task.id)
    assert stored.status == "failed"
    assert stored.error_metadata["error_class"] == "restart_side_effect_uncertain"
    assert stored.result_metadata["actions"][0]["status"] == "succeeded"
    # A failed occurrence is terminal: nothing can claim it again.
    assert await repo.claim_occurrence(OWNER, task.id, "k") is None
    assert calls == []


# ── 17/18/19: ownership, capability boundary, single execution authority ────


@pytest.mark.asyncio
async def test_a_foreign_owner_cannot_execute_another_owners_chain():
    repo = InMemoryTaskRepository()
    calls = []
    registry = ToolRegistry()
    registry.register(ChainTool("one", calls))
    registry.register(ChainTool("two", calls))
    _, occurrence = await _start(repo, _actions(("one", {}), ("two", {})))
    coordinator, _ = _coordinator(repo, owner=OTHER_OWNER, registry=registry)

    result = await coordinator.execute(occurrence)

    assert not result.success and result.error == "owner_mismatch" and calls == []
    assert (await _stored(repo, occurrence.task_id)).status == "running"


@pytest.mark.asyncio
async def test_an_unregistered_action_fails_before_the_earlier_actions_run():
    repo = InMemoryTaskRepository()
    calls = []
    registry = ToolRegistry()
    registry.register(ChainTool("one", calls))
    _, occurrence = await _start(repo, _actions(("one", {}), ("ghost", {})))
    coordinator, _ = _coordinator(repo, registry=registry)

    result = await coordinator.execute(occurrence)

    assert not result.success and calls == []
    assert (await _stored(repo, occurrence.task_id)).error_metadata["error_class"] == "unregistered_action"


@pytest.mark.asyncio
async def test_a_reference_that_could_never_resolve_fails_before_any_execution():
    repo = InMemoryTaskRepository()
    calls = []
    registry = ToolRegistry()
    registry.register(ChainTool("one", calls, data={"url": "u"}, fields=("url",)))
    registry.register(ChainTool("two", calls))
    # Hand-built snapshot (bypassing the creation boundary): a later-action
    # reference must still be refused by the execution-time re-proof.
    _, occurrence = await _start(repo, _actions(
        ("one", {}),
        ("two", {"url": {"$ref": {"action": 3, "field": "url"}}}),
    ))
    coordinator, _ = _coordinator(repo, registry=registry)

    result = await coordinator.execute(occurrence)

    assert not result.success and calls == []
    stored = await _stored(repo, occurrence.task_id)
    assert "invalid_action_reference" in stored.error_metadata["error_class"]


@pytest.mark.asyncio
async def test_chain_execution_runs_every_action_through_the_single_executor():
    repo = InMemoryTaskRepository()
    calls = []
    registry = ToolRegistry()
    registry.register(ChainTool("one", calls))
    registry.register(ChainTool("two", calls))
    _, occurrence = await _start(repo, _actions(("one", {}), ("two", {})))
    coordinator, executor = _coordinator(repo, registry=registry, executor_factory=CountingExecutor)

    await coordinator.execute(occurrence)

    # One executor batch per action, exactly one call in each: the chain never
    # reaches a tool by any other path.
    assert executor.batches == [[{"name": "one", "arguments": {}}], [{"name": "two", "arguments": {}}]]
    assert all(call["scheduled"] is True for call in calls)


@pytest.mark.asyncio
async def test_a_reference_free_chain_keeps_the_existing_metadata_contract():
    repo = InMemoryTaskRepository()
    calls = []
    registry = ToolRegistry()
    registry.register(ChainTool("one", calls))
    registry.register(ChainTool("two", calls))
    task, occurrence = await _start(repo, _actions(("one", {}), ("two", {})))
    coordinator, _ = _coordinator(repo, registry=registry)

    result = await coordinator.execute(occurrence)

    assert result.success
    metadata = (await _stored(repo, task.id)).result_metadata
    assert metadata["action_count"] == 2
    assert metadata["successful_action_count"] == 2
    assert metadata["terminal_status"] == "succeeded"
    assert isinstance(metadata["duration_ms"], float)
    # The record is bounded by construction.
    assert len(json.dumps(metadata).encode()) <= MAX_ACTION_RUNS_BYTES + 512
    assert {run["status"] for run in metadata["actions"]} == {"succeeded"}


# ── 20/21: the Save result contract (the D-4 fix) ──────────────────────────


class FakeTelegram:
    """The TelegramAPI facade as the save tool reaches it."""

    def __init__(self, client):
        self.client = client


def _save_context(client, *, request_text=""):
    return ToolContext(
        telegram=FakeTelegram(client), owner_id=OWNER, tz_str="UTC", client=client,
        extra={"reply_msg": {"chat_id": -100555, "message_id": 200}, "request_text": request_text},
    )


@pytest.mark.asyncio
async def test_save_tool_exposes_the_save_code_in_structured_data():
    from backend.services.save_service import SaveOutcome

    ctx = _save_context(FakeSaveClient(FakeSourceMessage()))
    with patch.object(
        db_client, "get_next_save_code", AsyncMock(return_value="S0042")
    ), patch(
        "backend.services.save_service.execute_save",
        AsyncMock(return_value=SaveOutcome("✅ Saved", save_code="S0042", saved=True)),
    ):
        result = await SaveTool(ctx).execute(ctx, {})

    assert result.success is True
    assert result.data["save_code"] == "S0042"
    assert result.data["mode"] == "deep"
    assert SaveTool(ctx).consumable_output_fields == ("save_code",)


@pytest.mark.asyncio
async def test_save_tool_without_a_reported_code_is_unchanged():
    ctx = _save_context(FakeSaveClient(FakeSourceMessage()))
    with patch("backend.services.save_service.execute_save", AsyncMock(return_value="✅ Saved")):
        result = await SaveTool(ctx).execute(ctx, {})

    assert result.success is True
    assert result.data == {"mode": "deep"}  # byte-identical to the old contract
    assert result.message == "✅ Saved"


@pytest.mark.asyncio
async def test_the_real_pipeline_reports_the_save_code_as_a_string_subclass():
    client = FakeSaveClient()
    message = FakeSourceMessage()
    result = await _run_real_save(client, message)

    assert isinstance(result, str) and "Saved Successfully" in result  # every old caller still works
    assert result.saved is True
    assert result.save_code.startswith("S") and len(result.save_code) == 5
    assert db_client._fallback["saved_items"][-1]["save_code"] == result.save_code


async def _run_real_save(client, message):
    from backend.services import save_service

    with patch.object(db_client, "get_next_save_code", AsyncMock(return_value="S0042")):
        return await save_service.execute_save(client, OWNER, message, "UTC")


# ── 22: real tools chain a resolved identity into a real write ─────────────


@pytest.mark.asyncio
async def test_real_tools_chain_a_resolved_identity_into_a_tag_write():
    """`rename_save` (real resolver) → `update_save_tags` (real writer).

    The downstream action addresses the item by the ``save_code`` the previous
    action RESOLVED, through the reference mechanism; both tools, the resolver,
    the registry and the executor are the real ones — only the database
    fallback (already the project's own) and Telegram are faked.
    """
    from backend.ai.tools.registry import create_default_registry

    db_client._fallback["saved_items"].append({
        "id": 1, "save_code": "S0001", "owner_id": OWNER, "display_name": "University Schedule",
        "tags": [], "media_type": "Document", "mime_type": "application/pdf", "file_size": 10,
        "file_name": "x.pdf", "caption": "caption", "created_at": "2026-09-15T10:08:00+00:00",
        "saved_chat_id": OWNER, "saved_msg_id": 400, "origin_chat_id": -1009999, "origin_msg_id": 4321,
    })
    ctx = ToolContext(telegram=None, owner_id=OWNER, tz_str="UTC", client=AsyncMock())
    registry = create_default_registry(ctx)
    actions = _actions(
        ("rename_save", {"query": "university schedule", "display_name": "Semester Two"}),
        ("update_save_tags", {
            "save_code": {"$ref": {"action": 1, "field": "save_code"}},
            "tags": ["semester-2"], "mode": "add",
        }),
    )
    repo = InMemoryTaskRepository()
    task, occurrence = await _start(repo, actions)
    coordinator, _ = _coordinator(repo, registry=registry)

    result = await coordinator.execute(occurrence)

    assert result.success, result.error
    row = db_client._fallback["saved_items"][-1]
    assert row["display_name"] == "Semester Two"
    assert "semester-2" in row["tags"]
    stored = await _stored(repo, task.id)
    assert [(run["position"], run["status"]) for run in stored.result_metadata["actions"]] == [
        (1, "succeeded"), (2, "succeeded")
    ]
    assert stored.result_metadata["actions"][0]["output"] == {"save_code": "S0001"}
