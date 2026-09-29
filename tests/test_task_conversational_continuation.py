"""Conversational continuation — mixed questions + commands in ONE durable chain.

The product contract under test (Todo Part 3F): a durable task's ordered
actions may interleave REGISTERED tool actions and ``ask_owner`` questions in
any bounded order — ACTION → QUESTION → ANSWER → ACTION → QUESTION → ANSWER
→ ACTION — inside ONE task, ONE occurrence, ONE coordinator, ONE scheduler,
ONE ToolExecutor and ONE answer resolver. Part 3F adds NO second execution
model: the answer is consumed by the Part 3D CAS, it enters later actions as
structured workflow data through the EXISTING Part 3A reference mechanism
(``{"$ref": {"action": N, "field": "answer"}}``), and the walk resumes the
SAME occurrence at the next already-defined action — never a new plan.

These tests pin the Part 3F matrix at the mixed level: continuation patterns,
correlation security (owner/chat/identity/occurrence/task), duplicate
protection, restart recovery at every meaningful boundary, failure semantics
(a failed downstream action never reopens an answered question), branch and
wait compatibility, context isolation (no chat history ever reaches an
action), the ToolRegistry/ToolExecutor boundaries, and the absence of
dynamic replanning (the recorded snapshot IS the whole workflow).
"""

from __future__ import annotations

import inspect
from datetime import datetime, timedelta, timezone

import pytest

from backend.ai.database.task_repository import InMemoryTaskRepository
from backend.ai.task_answers import TaskAnswerResolver, extract_answer_context
from backend.ai.task_contract import (
    ANSWER_FIELD,
    ACTION_RUNS_KEY,
    PENDING_QUESTION_KEY,
    QUESTION_MESSAGE_ID_KEY,
    QUESTION_TOOL,
    WAITING_ANSWER_STATUS,
    question_chain_error,
)
from backend.ai.task_creation import TaskCreationError, TaskCreationService
from backend.ai.task_execution import TaskExecutionCoordinator
from backend.ai.task_scheduler import TaskScheduler
from backend.ai.tools.base import PermissionLevel, ToolResult
from backend.ai.tools.context import ToolContext
from backend.ai.tools.executor import ToolExecutor
from backend.ai.tools.registry import ToolRegistry

OWNER = 4242
OTHER_OWNER = 999
OTHER_CHAT = 777
NOW = datetime(2026, 9, 26, 12, 0, tzinfo=timezone.utc)
START = datetime(2026, 9, 26, 14, 0, tzinfo=timezone.utc)
Q1 = "کدام دانشگاه؟"
Q2 = "کدام بخش؟"
A1 = "دانشگاه مونیخ"
A2 = "شهریه"


# ── The Phase 3D/3E harness, unchanged ──────────────────────────────────────


class ChainTool:
    permission_level = PermissionLevel.READ_WRITE
    long_running = False
    safe = True
    description = "chain test tool"
    parameters = {}
    return_type = "object"

    def __init__(self, name, calls, *, data=None, fields=(), plan=None):
        self.name = name
        self.calls = calls
        self.data = dict(data or {})
        self.consumable_output_fields = tuple(fields)
        self._plan = list(plan or [])
        self._runs = 0

    async def execute(self, context, arguments):
        self.calls.append({
            "name": self.name,
            "arguments": dict(arguments),
            "owner": context.owner_id,
            "extra_keys": set((context.extra or {}).keys()),
        })
        entry = self._plan[self._runs] if self._runs < len(self._plan) else None
        self._runs += 1
        if isinstance(entry, BaseException):
            raise entry
        return entry if entry is not None else ToolResult(True, "ok", dict(self.data))


class _QuestionTelegram:
    """Records every send; each question message gets its own durable id."""

    def __init__(self, chat_id=OWNER):
        self.chat_id = chat_id
        self.next_id = 1000
        self.sent = []

    async def send_message(self, chat_id, text, **kwargs):
        self.next_id += 1
        self.sent.append((chat_id, text))
        return {"id": self.next_id, "chat_id": chat_id, "text": text}


class CountingExecutor(ToolExecutor):
    """The real executor, remembering the batches it was asked to run."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.batches = []

    async def execute_calls(self, tool_calls, **kwargs):
        self.batches.append([dict(call) for call in tool_calls])
        return await super().execute_calls(tool_calls, **kwargs)


def _registry(calls, *, telegram=None, search_plan=None):
    telegram = telegram if telegram is not None else _QuestionTelegram()
    context = ToolContext(telegram, OWNER, "UTC")
    registry = ToolRegistry()
    from backend.ai.tools.question import AskOwnerTool

    registry.register(AskOwnerTool(context))
    registry.register(ChainTool(
        "web_search", calls,
        data={"top_title": "TU München", "top_url": "https://tum.de"},
        fields=("top_title", "top_url"), plan=search_plan,
    ))
    registry.register(ChainTool(
        "save", calls, data={"save_code": "S0001"}, fields=("save_code",),
    ))
    registry.register(ChainTool("send_message", calls, data={"sent": True}))
    return registry


def _q(text):
    return {"name": QUESTION_TOOL, "arguments": {"question": text}}


def _search(query):
    return {"name": "web_search", "arguments": {"query": query}}


def _save(caption):
    return {"name": "save", "arguments": {"text": caption}}


def _send(text):
    return {"name": "send_message", "arguments": {"text": text}}


def _ref(position, field=ANSWER_FIELD):
    return {"$ref": {"action": position, "field": field}}


def _task_payload(actions, *, at="2026-09-26T14:00:00"):
    return {
        "label": "continuation",
        "schedule_type": "once",
        "schedule": {"at": at, "timezone": "UTC"},
        "timezone": "UTC",
        "actions": actions,
        "notification_destination": {},
    }


async def _start(repo, actions, *, key="k", owner=OWNER):
    task = await repo.create_task(owner, _task_payload(actions))
    await repo.create_occurrence(owner, {
        "task_id": task.id,
        "occurrence_key": key,
        "definition_version": task.version,
        "action_snapshot": actions,
        "scheduled_for": NOW,
    })
    claimed = await repo.claim_occurrence(owner, task.id, key)
    return task, claimed


def _coordinator(repo, registry, *, owner=OWNER, executor_factory=ToolExecutor):
    telegram = registry.get(QUESTION_TOOL)._context.telegram
    ctx = ToolContext(telegram, owner, "UTC")
    executor = executor_factory(registry, ctx)
    return TaskExecutionCoordinator(repo, executor, owner, ctx), executor


def _scheduler(repo, registry):
    coordinator, _ = _coordinator(repo, registry)
    return TaskScheduler(repo, OWNER, coordinator, outcome_notifier=None)


async def _stored(repo, task_id, key="k", owner=OWNER):
    return await repo.get_occurrence(owner, task_id, key)


def _names(calls):
    return [call["name"] for call in calls]


def _runs_of(occurrence):
    for metadata in (occurrence.error_metadata, occurrence.result_metadata):
        if isinstance(metadata, dict) and ACTION_RUNS_KEY in metadata:
            return metadata[ACTION_RUNS_KEY]
    return []


def _run(occurrence, position):
    return next(run for run in _runs_of(occurrence) if run["position"] == position)


def _answer_event(text=A1, *, chat_id=OWNER, sender=OWNER, reply_to=None,
                  message_id=5000):
    return {
        "chat_id": chat_id,
        "sender_id": sender,
        "message_id": message_id,
        "reply_to_message_id": reply_to,
        "text": text,
        "has_media": False,
    }


def _reply_event(text, message_id, **overrides):
    return {**_answer_event(text, reply_to=message_id), **overrides}


def _question_message_id(stored):
    record = stored.result_metadata.get(PENDING_QUESTION_KEY)
    if record is None:
        record = stored.error_metadata.get(PENDING_QUESTION_KEY)
    assert record is not None
    return record[QUESTION_MESSAGE_ID_KEY]


async def _wake_after_answer(repo, registry, task, *, text=A1, reply_to):
    """Answer → CAS resume → the ONE scheduler wake that continues the chain."""
    resolver = TaskAnswerResolver(repo, OWNER)
    assert await resolver.handle_event(_reply_event(text, reply_to)) is True
    parked = await _stored(repo, task.id)
    scheduler = _scheduler(repo, registry)
    assert await scheduler.run_once(now=parked.retry_at + timedelta(seconds=1)) == 1
    return await _stored(repo, task.id)


# ── 1: the mixed continuation contract ──────────────────────────────────────


def test_a_mixed_chain_of_actions_and_questions_is_the_valid_shape():
    assert question_chain_error([
        _search("دانشگاه"), _q(Q1), _search(_ref(2)), _q(Q2), _send(_ref(4)),
    ]) is None
    assert question_chain_error([
        _q(Q1), _search(_ref(1)), _save(_ref(2, "top_title")), _q(Q2), _send(_ref(4)),
    ]) is None
    assert question_chain_error([_q(Q1), _send(_ref(1)), _q(Q2), _send(_ref(3))]) is None


@pytest.mark.asyncio
async def test_creation_accepts_a_mixed_action_question_chain():
    repo = InMemoryTaskRepository()
    registry = _registry([])
    service = TaskCreationService(repo, OWNER, tool_registry=registry)
    task = await service.create(
        _task_payload([
            _search("درباره دانشگاه"),
            _q(Q1),
            _search(_ref(2)),
            _save(_ref(3, "top_title")),
            _q(Q2),
        ]),
        START - timedelta(minutes=30),
    )
    assert len(task.actions) == 5
    assert task.actions[1]["name"] == QUESTION_TOOL
    assert task.actions[4]["name"] == QUESTION_TOOL


def test_the_interpreter_contract_describes_the_mixed_continuation():
    from backend.ai import task_interpreter
    from backend.ai.task_interpreter import CANDIDATE_SCHEMA

    question_schema = CANDIDATE_SCHEMA["properties"]["actions"]["items"][
        "properties"
    ]["question"]
    assert question_schema["type"] == "string"
    description = question_schema["description"]
    assert "ask_owner" in description
    assert "interleave" in description

    prompt_source = inspect.getsource(task_interpreter)
    assert "INTERLEAVE" in prompt_source
    assert "already-defined" in prompt_source


# ── 2: the continuation patterns ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_action_question_answer_action_continues_in_order():
    repo = InMemoryTaskRepository()
    calls = []
    telegram = _QuestionTelegram()
    registry = _registry(calls, telegram=telegram)
    task, occurrence = await _start(repo, [_search("دانشگاه"), _q(Q1), _send(_ref(2))])
    coordinator, _ = _coordinator(repo, registry)

    first = await coordinator.execute(occurrence)
    assert first.status == WAITING_ANSWER_STATUS
    assert _names(calls) == ["web_search"]  # the earlier action ran first
    stored = await _stored(repo, task.id)
    assert _run(stored, 1)["status"] == "succeeded"
    assert _run(stored, 2)["status"] == "pending"

    done = await _wake_after_answer(repo, registry, task, reply_to=1001)
    assert done.status == "succeeded"
    assert _names(calls) == ["web_search", "send_message"]
    assert calls[1]["arguments"]["text"] == A1  # the answer drives the action


@pytest.mark.asyncio
async def test_question_answer_question_answer_action():
    repo = InMemoryTaskRepository()
    calls = []
    registry = _registry(calls)
    task, occurrence = await _start(repo, [
        _q(Q1), _q(Q2), _send(_ref(2)),
    ])
    coordinator, _ = _coordinator(repo, registry)

    await coordinator.execute(occurrence)
    stored = await _stored(repo, task.id)
    assert _question_message_id(stored) == 1001

    stored = await _wake_after_answer(repo, registry, task, reply_to=1001)
    assert stored.status == WAITING_ANSWER_STATUS  # Q2 is now the active one
    assert _question_message_id(stored) == 1002
    assert _names(calls) == []  # no action ran between the two questions

    done = await _wake_after_answer(repo, registry, task, text=A2, reply_to=1002)
    assert done.status == "succeeded"
    assert calls[0]["arguments"]["text"] == A2


@pytest.mark.asyncio
async def test_action_question_answer_question_answer_action():
    """The primary 3F pattern: ACTION → Q → A → ACTION → Q → A → ACTION."""
    repo = InMemoryTaskRepository()
    calls = []
    telegram = _QuestionTelegram()
    registry = _registry(calls, telegram=telegram)
    task, occurrence = await _start(repo, [
        _search("دانشگاه‌های آلمان"),        # 1
        _q(Q1),                              # 2
        _search(_ref(2)),                    # 3 consumes A1
        _q(Q2),                              # 4
        _send(_ref(4)),                      # 5 consumes A2
    ])
    coordinator, _ = _coordinator(repo, registry)
    resolver = TaskAnswerResolver(repo, OWNER)
    scheduler = _scheduler(repo, registry)

    await coordinator.execute(occurrence)
    stored = await _stored(repo, task.id)
    assert stored.status == WAITING_ANSWER_STATUS
    assert _names(calls) == ["web_search"]
    assert _question_message_id(stored) == 1001

    assert await resolver.handle_event(_answer_event(A1, reply_to=1001)) is True
    parked = await _stored(repo, task.id)
    assert await scheduler.run_once(now=parked.retry_at + timedelta(seconds=1)) == 1
    stored = await _stored(repo, task.id)
    assert stored.status == WAITING_ANSWER_STATUS
    assert _names(calls) == ["web_search", "web_search"]
    assert calls[1]["arguments"]["query"] == A1
    assert _run(stored, 3)["status"] == "succeeded"
    assert _question_message_id(stored) == 1002

    assert await resolver.handle_event(_answer_event(A2, reply_to=1002)) is True
    parked2 = await _stored(repo, task.id)
    assert await scheduler.run_once(now=parked2.retry_at + timedelta(seconds=1)) == 1
    done = await _stored(repo, task.id)
    assert done.status == "succeeded"
    assert _names(calls) == ["web_search", "web_search", "send_message"]
    assert calls[2]["arguments"]["text"] == A2
    assert _run(done, 1)["output"] == {"top_title": "TU München", "top_url": "https://tum.de"}
    assert _run(done, 2)["output"] == {ANSWER_FIELD: A1}
    assert _run(done, 4)["output"] == {ANSWER_FIELD: A2}


@pytest.mark.asyncio
async def test_multiple_answer_references_each_consume_their_own_question():
    repo = InMemoryTaskRepository()
    calls = []
    registry = _registry(calls)
    task, occurrence = await _start(repo, [
        _q(Q1), _q(Q2), _search("دانشگاه رشته"), _send("x"),
    ])
    # Two SEPARATE referencing actions, each consuming one question's answer.
    repo2 = InMemoryTaskRepository()
    actions = [_q(Q1), _q(Q2), _search(_ref(1)), _send(_ref(2))]
    task2, occurrence2 = await _start(repo2, actions)
    coordinator, _ = _coordinator(repo2, registry)
    resolver = TaskAnswerResolver(repo2, OWNER)
    scheduler = _scheduler(repo2, registry)

    await coordinator.execute(occurrence2)
    assert await resolver.handle_event(_answer_event(A1, reply_to=1001)) is True
    parked = await _stored(repo2, task2.id)
    assert await scheduler.run_once(now=parked.retry_at + timedelta(seconds=1)) == 1
    assert await resolver.handle_event(_answer_event(A2, reply_to=1002)) is True
    parked2 = await _stored(repo2, task2.id)
    assert await scheduler.run_once(now=parked2.retry_at + timedelta(seconds=1)) == 1

    done = await _stored(repo2, task2.id)
    assert done.status == "succeeded"
    assert calls[-2]["arguments"]["query"] == A1  # Q1's answer
    assert calls[-1]["arguments"]["text"] == A2   # Q2's answer


# ── 3: correlation security over a mixed chain ──────────────────────────────


@pytest.mark.asyncio
async def test_unrelated_and_wrong_chat_replies_are_ignored():
    repo = InMemoryTaskRepository()
    registry = _registry([])
    task, occurrence = await _start(repo, [_search("x"), _q(Q1), _send(_ref(2))])
    coordinator, _ = _coordinator(repo, registry)
    await coordinator.execute(occurrence)

    resolver = TaskAnswerResolver(repo, OWNER)
    assert await resolver.handle_event(_answer_event("سلام")) is False
    assert await resolver.handle_event(
        _reply_event(A1, 1001, chat_id=OTHER_CHAT)
    ) is False
    stored = await _stored(repo, task.id)
    assert stored.status == WAITING_ANSWER_STATUS
    assert _run(stored, 2)["status"] == "pending"


@pytest.mark.asyncio
async def test_wrong_sender_wrong_task_and_late_replies_are_refused():
    repo = InMemoryTaskRepository()
    calls = []
    registry = _registry(calls)
    task, occurrence = await _start(repo, [
        _search("دانشگاه"), _q(Q1), _search(_ref(2)), _q(Q2), _send(_ref(4)),
    ])
    coordinator, _ = _coordinator(repo, registry)
    resolver = TaskAnswerResolver(repo, OWNER)
    scheduler = _scheduler(repo, registry)

    await coordinator.execute(occurrence)
    # A non-owner can never answer, even with the exact identity.
    assert await resolver.handle_event(
        _reply_event(A1, 1001, sender_id=OTHER_OWNER)
    ) is False
    # A reply to another task's question message is not this task's answer.
    assert await resolver.handle_event(_reply_event(A1, 5555)) is False
    # The correct answer advances the chain to Question #2.
    assert await resolver.handle_event(_reply_event(A1, 1001)) is True
    parked = await _stored(repo, task.id)
    assert await scheduler.run_once(now=parked.retry_at + timedelta(seconds=1)) == 1
    stored = await _stored(repo, task.id)
    assert _question_message_id(stored) == 1002
    # A LATE reply to Question #1's ORIGINAL message answers nothing.
    assert await resolver.handle_event(_reply_event("دیر", 1001)) is False
    assert stored.status == WAITING_ANSWER_STATUS
    assert _names(calls) == ["web_search", "web_search"]


@pytest.mark.asyncio
async def test_a_reply_to_a_second_occurrence_consumes_only_that_occurrence():
    """Two parked occurrences of the SAME task: each reply addresses exactly
    its own occurrence — the wrong-occurrence reply never advances the other."""
    repo = InMemoryTaskRepository()
    calls = []
    telegram = _QuestionTelegram()
    registry = _registry(calls, telegram=telegram)
    actions = [_q(Q1), _send(_ref(1))]
    task = await repo.create_task(OWNER, _task_payload(actions))
    for key in ("k1", "k2"):
        await repo.create_occurrence(OWNER, {
            "task_id": task.id,
            "occurrence_key": key,
            "definition_version": task.version,
            "action_snapshot": actions,
            "scheduled_for": NOW,
        })
    coordinator, _ = _coordinator(repo, registry)
    for key in ("k1", "k2"):
        claimed = await repo.claim_occurrence(OWNER, task.id, key)
        await coordinator.execute(claimed)

    k1 = await _stored(repo, task.id, key="k1")
    k2 = await _stored(repo, task.id, key="k2")
    id1, id2 = _question_message_id(k1), _question_message_id(k2)
    assert id1 != id2  # each occurrence asked its OWN question message

    resolver = TaskAnswerResolver(repo, OWNER)
    # A reply to k2's question must not touch k1.
    assert await resolver.handle_event(_reply_event(A1, id2)) is True
    k1 = await _stored(repo, task.id, key="k1")
    k2 = await _stored(repo, task.id, key="k2")
    assert k1.status == WAITING_ANSWER_STATUS and k1.retry_at is None
    assert k2.status == "retry_pending" and k2.retry_at is not None

    # Only k2 is due: the wake continues k2 and must not touch k1's question.
    assert await _scheduler(repo, registry).run_once(
        now=k2.retry_at + timedelta(seconds=1)
    ) == 1
    k1 = await _stored(repo, task.id, key="k1")
    k2 = await _stored(repo, task.id, key="k2")
    assert k1.status == WAITING_ANSWER_STATUS  # still parked on ITS question
    assert k2.status == "succeeded"
    # And k1 remains unclaimable while parked: no path consumes it but its
    # own correlated reply.
    assert await repo.claim_occurrence(OWNER, task.id, "k1") is None

    assert await resolver.handle_event(_reply_event(A1, id1)) is True
    parked = await _stored(repo, task.id, key="k1")
    assert await _scheduler(repo, registry).run_once(
        now=parked.retry_at + timedelta(seconds=1)
    ) == 1
    k1 = await _stored(repo, task.id, key="k1")
    assert k1.status == "succeeded"


@pytest.mark.asyncio
async def test_a_duplicate_reply_is_accepted_once_and_the_action_runs_once():
    repo = InMemoryTaskRepository()
    calls = []
    registry = _registry(calls)
    task, occurrence = await _start(repo, [
        _search("دانشگاه"), _q(Q1), _search(_ref(2)), _q(Q2),
    ])
    coordinator, _ = _coordinator(repo, registry)
    resolver = TaskAnswerResolver(repo, OWNER)
    scheduler = _scheduler(repo, registry)

    await coordinator.execute(occurrence)
    assert await resolver.handle_event(_reply_event(A1, 1001)) is True
    assert await resolver.handle_event(_reply_event("دوباره", 1001)) is False
    parked = await _stored(repo, task.id)
    assert await scheduler.run_once(now=parked.retry_at + timedelta(seconds=1)) == 1
    stored = await _stored(repo, task.id)
    assert stored.status == WAITING_ANSWER_STATUS
    assert _question_message_id(stored) == 1002
    # The duplicate must not have consumed Question #2's checkpoint either:
    # Q2 is still the ACTIVE question and nothing re-ran.
    assert _names(calls) == ["web_search", "web_search"]  # exactly once each
    assert _run(stored, 4)["status"] == "pending"  # Q2 still waiting
    # The REAL answer to Q2 completes the chain exactly once more.
    done = await _wake_after_answer(
        repo, registry, task, text="پاسخ واقعی", reply_to=1002
    )
    assert done.status == "succeeded"
    assert _names(calls) == ["web_search", "web_search"]


# ── 4: restart recovery at the meaningful boundaries ────────────────────────


@pytest.mark.asyncio
async def test_a_restart_while_a_question_is_pending_never_re_asks():
    repo = InMemoryTaskRepository()
    calls = []
    telegram = _QuestionTelegram()
    registry = _registry(calls, telegram=telegram)
    task, occurrence = await _start(repo, [
        _search("دانشگاه"), _q(Q1), _search(_ref(2)), _q(Q2),
    ])
    coordinator, _ = _coordinator(repo, registry)
    await coordinator.execute(occurrence)

    stored = await _stored(repo, task.id)
    assert stored.status == WAITING_ANSWER_STATUS
    fresh_scheduler = _scheduler(repo, _registry(calls, telegram=telegram))
    assert await fresh_scheduler.run_once(now=stored.retry_at or NOW) == 0
    due = await repo.list_due_retry_occurrences(OWNER, NOW + timedelta(hours=1))
    assert all(item.occurrence_key != stored.occurrence_key for item in due)
    assert await repo.claim_occurrence(OWNER, task.id, stored.occurrence_key) is None
    assert telegram.sent == [(OWNER, Q1)]  # asked exactly once


@pytest.mark.asyncio
async def test_a_restart_after_an_answer_resumes_at_the_next_action():
    repo = InMemoryTaskRepository()
    calls = []
    telegram = _QuestionTelegram()
    registry = _registry(calls, telegram=telegram)
    task, occurrence = await _start(repo, [
        _search("دانشگاه"), _q(Q1), _search(_ref(2)), _q(Q2),
    ])
    coordinator, _ = _coordinator(repo, registry)
    await coordinator.execute(occurrence)

    # A fresh process consumes the answer and wakes the chain.
    fresh = _registry(calls, telegram=telegram)
    resolver = TaskAnswerResolver(repo, OWNER)
    assert await resolver.handle_event(_reply_event(A1, 1001)) is True
    parked = await _stored(repo, task.id)
    assert await _scheduler(repo, fresh).run_once(
        now=parked.retry_at + timedelta(seconds=1)
    ) == 1
    stored = await _stored(repo, task.id)
    assert stored.status == WAITING_ANSWER_STATUS
    assert _names(calls) == ["web_search", "web_search"]
    assert calls[1]["arguments"]["query"] == A1
    assert telegram.sent == [(OWNER, Q1), (OWNER, Q2)]  # Q1 never re-sent


@pytest.mark.asyncio
async def test_a_restart_between_actions_parks_on_the_wait_and_never_replays():
    """After the answer the chain runs ACTION → ACTION and parks on a TIME
    boundary; a restart must not replay the actions that already succeeded."""
    repo = InMemoryTaskRepository()
    calls = []
    telegram = _QuestionTelegram()
    registry = _registry(calls, telegram=telegram)
    task, occurrence = await _start(repo, [
        _q(Q1),                                        # 1
        _search(_ref(1)),                              # 2
        _save(_ref(2, "top_title")),                   # 3
        {"name": "send_message", "arguments": {"text": "ساعت ۶"},
         "not_before": "2027-01-01T18:00:00"},         # 4
    ])
    coordinator, _ = _coordinator(repo, registry)
    await coordinator.execute(occurrence)

    stored = await _wake_after_answer(repo, registry, task, reply_to=1001)
    assert stored.status == "retry_pending"  # parked on the clock, not a question
    assert _names(calls) == ["web_search", "save"]
    assert calls[1]["arguments"]["text"] == "TU München"

    # "Restart": a fresh coordinator over the same repository wakes at the
    # boundary — the wait action runs ONCE, nothing before it is replayed.
    fresh = _registry(calls, telegram=telegram)
    fresh_scheduler = _scheduler(repo, fresh)
    assert await fresh_scheduler.run_once(
        now=datetime(2027, 1, 1, 18, 0, 1, tzinfo=timezone.utc)
    ) == 1
    done = await _stored(repo, task.id)
    assert done.status == "succeeded"
    assert _names(calls) == ["web_search", "save", "send_message"]
    assert _run(done, 1)["output"] == {ANSWER_FIELD: A1}
    assert _run(done, 2)["status"] == "succeeded"
    assert _run(done, 3)["status"] == "succeeded"


@pytest.mark.asyncio
async def test_a_restart_while_the_second_question_is_pending_preserves_both():
    repo = InMemoryTaskRepository()
    calls = []
    telegram = _QuestionTelegram()
    registry = _registry(calls, telegram=telegram)
    task, occurrence = await _start(repo, [
        _q(Q1), _search(_ref(1)), _q(Q2), _send(_ref(3)),
    ])
    coordinator, _ = _coordinator(repo, registry)
    resolver = TaskAnswerResolver(repo, OWNER)
    scheduler = _scheduler(repo, registry)

    await coordinator.execute(occurrence)
    assert await resolver.handle_event(_reply_event(A1, 1001)) is True
    parked = await _stored(repo, task.id)
    assert await scheduler.run_once(now=parked.retry_at + timedelta(seconds=1)) == 1
    stored = await _stored(repo, task.id)
    assert stored.status == WAITING_ANSWER_STATUS  # Q2 pending
    assert _run(stored, 1)["output"] == {ANSWER_FIELD: A1}  # A1 durable

    # Restart #2: a fresh process finishes through Q2's OWN identity.
    fresh = _registry(calls, telegram=telegram)
    fresh_resolver = TaskAnswerResolver(repo, OWNER)
    assert await fresh_resolver.handle_event(_reply_event(A2, 1002)) is True
    parked2 = await _stored(repo, task.id)
    assert await _scheduler(repo, fresh).run_once(
        now=parked2.retry_at + timedelta(seconds=1)
    ) == 1
    done = await _stored(repo, task.id)
    assert done.status == "succeeded"
    assert _run(done, 1)["output"] == {ANSWER_FIELD: A1}  # survived the restart
    assert _run(done, 3)["output"] == {ANSWER_FIELD: A2}
    assert telegram.sent == [(OWNER, Q1), (OWNER, Q2)]


# ── 5: failure semantics — a failed action never reopens a question ────────


@pytest.mark.asyncio
async def test_a_failed_downstream_action_never_reopens_the_answered_question():
    repo = InMemoryTaskRepository()
    calls = []
    telegram = _QuestionTelegram()
    registry = _registry(calls, telegram=telegram)
    registry.get("web_search")._plan = [ToolResult(False, "search refused")]
    task, occurrence = await _start(repo, [
        _q(Q1), _search(_ref(1)), _save(_ref(2, "top_title")),
    ])
    coordinator, _ = _coordinator(repo, registry)
    resolver = TaskAnswerResolver(repo, OWNER)
    await coordinator.execute(occurrence)

    assert await resolver.handle_event(_reply_event(A1, 1001)) is True
    parked = await _stored(repo, task.id)
    scheduler = _scheduler(repo, registry)
    assert await scheduler.run_once(now=parked.retry_at + timedelta(seconds=1)) == 1
    stored = await _stored(repo, task.id)
    # The deterministic refusal either fails the occurrence (no retry) or
    # leaves the action's own failed record on the durable error channel;
    # either way the chain did NOT advance and nothing downstream ran.
    assert stored.status in ("failed", "retry_pending")
    assert _run(stored, 1)["status"] == "succeeded"  # the answer stays consumed
    assert _run(stored, 1)["output"] == {ANSWER_FIELD: A1}
    assert _run(stored, 2)["status"] in ("failed", "running")
    assert _run(stored, 3)["status"] == "pending"
    # The answered question is NEVER re-asked and never re-answered.
    assert await resolver.handle_event(_reply_event("دوباره", 1001)) is False
    assert telegram.sent == [(OWNER, Q1)]


@pytest.mark.asyncio
async def test_a_retryable_downstream_failure_resumes_at_the_failed_action():
    """A timeout keeps the retry contract: the retry resumes AT the failed
    action, consumes the recorded answer, and never re-asks the question."""
    import asyncio as _asyncio

    repo = InMemoryTaskRepository()
    calls = []
    registry = _registry(calls, search_plan=[
        _asyncio.TimeoutError("tool timed out"),   # first attempt fails
        ToolResult(True, "ok", {"top_title": "TU München", "top_url": "u"}),
    ])
    task, occurrence = await _start(repo, [
        _q(Q1), _search(_ref(1)), _save(_ref(2, "top_title")),
    ])
    coordinator, _ = _coordinator(repo, registry)
    resolver = TaskAnswerResolver(repo, OWNER)
    await coordinator.execute(occurrence)

    assert await resolver.handle_event(_reply_event(A1, 1001)) is True
    parked = await _stored(repo, task.id)
    assert await _scheduler(repo, registry).run_once(
        now=parked.retry_at + timedelta(seconds=1)
    ) == 1
    stored = await _stored(repo, task.id)
    assert stored.status == "retry_pending" and stored.attempt == 2
    assert _run(stored, 1)["status"] == "succeeded"
    assert _run(stored, 2)["status"] == "failed"
    assert await resolver.handle_event(_reply_event("دوباره", 1001)) is False

    # Fresh claim + fresh coordinator (a new process) resume at action 2.
    claimed = await repo.claim_occurrence(OWNER, task.id, "k")
    calls.clear()
    restarted, _ = _coordinator(repo, registry)
    second = await restarted.execute(claimed)
    assert second.success, second.error
    assert _names(calls) == ["web_search", "save"]  # question NOT re-asked
    assert calls[0]["arguments"]["query"] == A1     # recorded answer consumed
    done = await _stored(repo, task.id)
    assert done.status == "succeeded"
    assert _run(done, 1)["status"] == "succeeded"


@pytest.mark.asyncio
async def test_an_answered_question_never_becomes_active_again():
    repo = InMemoryTaskRepository()
    calls = []
    telegram = _QuestionTelegram()
    registry = _registry(calls, telegram=telegram)
    task, occurrence = await _start(repo, [
        _q(Q1), _search(_ref(1)), _q(Q2), _send(_ref(3)),
    ])
    coordinator, _ = _coordinator(repo, registry)
    resolver = TaskAnswerResolver(repo, OWNER)
    scheduler = _scheduler(repo, registry)

    await coordinator.execute(occurrence)
    await resolver.handle_event(_reply_event(A1, 1001))
    parked = await _stored(repo, task.id)
    await scheduler.run_once(now=parked.retry_at + timedelta(seconds=1))
    await resolver.handle_event(_reply_event(A2, 1002))
    parked2 = await _stored(repo, task.id)
    await scheduler.run_once(now=parked2.retry_at + timedelta(seconds=1))
    done = await _stored(repo, task.id)

    assert done.status == "succeeded"
    assert _run(done, 1)["status"] == "succeeded"   # Q1 permanently succeeded
    assert _run(done, 3)["status"] == "succeeded"   # Q2 permanently succeeded
    # A late reply to EITHER question mutates nothing.
    assert await resolver.handle_event(_reply_event("دیر", 1001)) is False
    assert await resolver.handle_event(_reply_event("دیر", 1002)) is False
    assert telegram.sent == [(OWNER, Q1), (OWNER, Q2)]  # never re-asked


# ── 6: branch and wait compatibility (3B + 3C) ──────────────────────────────


@pytest.mark.asyncio
async def test_a_question_answer_feeds_the_existing_condition():
    repo = InMemoryTaskRepository()
    calls = []
    registry = _registry(calls)
    actions = [
        _q(Q1),
        {"condition": {"source": {"action": 1, "field": ANSWER_FIELD},
                       "operator": "equals", "value": A1}},
        {**_save("مسیر درست"), "branch": "true"},
        {**_send("مسیر غلط"), "branch": "false"},
    ]
    task, occurrence = await _start(repo, actions)
    coordinator, _ = _coordinator(repo, registry)
    await coordinator.execute(occurrence)

    stored = await _wake_after_answer(repo, registry, task, reply_to=1001)
    assert stored.status == "succeeded"
    assert _run(stored, 2)["output"] == {"matched": True, "selected_branch": "true"}
    assert _names(calls) == ["save"]  # only the TRUE branch ran
    assert calls[0]["extra_keys"] <= {"scheduled_occurrence", "chat_id"}


@pytest.mark.asyncio
async def test_a_branch_question_and_its_answer_drive_the_branch_action():
    repo = InMemoryTaskRepository()
    calls = []
    telegram = _QuestionTelegram()
    registry = _registry(calls, telegram=telegram)
    actions = [
        _search("دانشگاه"),
        {"condition": {"source": {"action": 1, "field": "top_title"},
                       "operator": "equals", "value": "TU München"}},
        {**_q(Q1), "branch": "true"},
        {**_send(_ref(3)), "branch": "true"},
        {**_send("خالی"), "branch": "false"},
    ]
    task, occurrence = await _start(repo, actions)
    coordinator, _ = _coordinator(repo, registry)

    first = await coordinator.execute(occurrence)
    assert first.status == WAITING_ANSWER_STATUS  # the branch question asked
    stored = await _stored(repo, task.id)
    assert _run(stored, 5)["status"] == "skipped"  # non-selected branch
    assert _question_message_id(stored) == 1001

    done = await _wake_after_answer(repo, registry, task, reply_to=1001)
    assert done.status == "succeeded"
    assert calls[-1]["arguments"]["text"] == A1  # the branch action consumed it
    assert _run(done, 5)["status"] == "skipped"  # still durably inactive


@pytest.mark.asyncio
async def test_a_wait_after_questions_parks_on_the_clock_not_a_question():
    repo = InMemoryTaskRepository()
    calls = []
    registry = _registry(calls)
    task, occurrence = await _start(repo, [
        _q(Q1), _send(_ref(1)),
        {"name": "save", "arguments": {"text": "n"}, "not_before": "2027-01-01T18:00:00"},
    ])
    coordinator, _ = _coordinator(repo, registry)
    await coordinator.execute(occurrence)
    stored = await _stored(repo, task.id)
    assert stored.status == WAITING_ANSWER_STATUS
    assert stored.retry_at is None  # a question has NO clock

    stored = await _wake_after_answer(repo, registry, task, reply_to=1001)
    assert stored.status == "retry_pending" and stored.retry_at is not None
    assert _names(calls) == ["send_message"]
    done_stored = await _wake_after_answer(
        repo, registry, task, text="wake", reply_to=1001
    ) if False else stored
    # The wake AT the boundary finishes the chain through the scheduler only.
    scheduler = _scheduler(repo, registry)
    assert await scheduler.run_once(now=stored.retry_at + timedelta(seconds=1)) == 1
    done = await _stored(repo, task.id)
    assert done.status == "succeeded"
    assert _names(calls) == ["send_message", "save"]


# ── 7: boundaries — registry, executor, isolation, no replanning ────────────


@pytest.mark.asyncio
async def test_the_tool_registry_boundary_refuses_unresolvable_mixed_chains():
    from backend.ai.task_contract import action_reference_error

    registry = _registry([])
    # A reference naming a field the target tool does not declare chainable
    # is refused at creation (fail closed before anything can run).
    actions = [_q(Q1), _send({"$ref": {"action": 1, "field": "ghost"}})]
    assert action_reference_error(actions, registry) is not None
    with pytest.raises(TaskCreationError):
        service = TaskCreationService(InMemoryTaskRepository(), OWNER, tool_registry=registry)
        await service.create(_task_payload(actions), START - timedelta(minutes=30))
    # …and a chain whose action is not registered fails BEFORE the question
    # is even sent (the walk never starts).
    repo = InMemoryTaskRepository()
    calls = []
    telegram = _QuestionTelegram()
    open_registry = _registry(calls, telegram=telegram)
    task, occurrence = await _start(repo, [
        _q(Q1), {"name": "ghost_tool", "arguments": {}},
    ])
    coordinator, _ = _coordinator(repo, open_registry)
    result = await coordinator.execute(occurrence)
    assert not result.success and result.error == "unregistered_action"
    assert telegram.sent == []  # the question was never sent
    assert _names(calls) == []


@pytest.mark.asyncio
async def test_the_tool_executor_remains_the_sole_execution_authority():
    from backend.ai import task_answers

    assert "execute_calls" not in inspect.getsource(task_answers)
    assert "tool.execute(" not in inspect.getsource(task_answers)
    assert "resume_waiting_for_answer" in inspect.getsource(task_answers)

    repo = InMemoryTaskRepository()
    calls = []
    registry = _registry(calls)
    task, occurrence = await _start(repo, [_q(Q1), _search(_ref(1))])
    coordinator, executor = _coordinator(repo, registry, executor_factory=CountingExecutor)
    await coordinator.execute(occurrence)
    # The wake must ride the SAME coordinator (and executor) to count batches.
    resolver = TaskAnswerResolver(repo, OWNER)
    assert await resolver.handle_event(_reply_event(A1, 1001)) is True
    parked = await _stored(repo, task.id)
    scheduler = TaskScheduler(repo, OWNER, coordinator, outcome_notifier=None)
    assert await scheduler.run_once(now=parked.retry_at + timedelta(seconds=1)) == 1
    stored = await _stored(repo, task.id)
    assert stored.status == "succeeded"
    # Every tool execution went through the ONE executor, one call per batch.
    assert len(executor.batches) == 2  # the question batch, then the search batch
    assert all(len(batch) == 1 for batch in executor.batches)
    assert [batch[0]["name"] for batch in executor.batches] == [QUESTION_TOOL, "web_search"]


@pytest.mark.asyncio
async def test_the_execution_context_carries_only_structured_workflow_data():
    repo = InMemoryTaskRepository()
    calls = []
    registry = _registry(calls)
    task, occurrence = await _start(repo, [_q(Q1), _send(_ref(1))])
    coordinator, _ = _coordinator(repo, registry)
    await coordinator.execute(occurrence)

    # The answer correlation context is EXACTLY the bounded deterministic set.
    context = extract_answer_context(_reply_event(A1, 1001))
    assert set(context) == {
        "chat_id", "sender_id", "message_id", "reply_to_message_id", "text",
        "has_media",
    }

    stored = await _wake_after_answer(repo, registry, task, reply_to=1001)
    assert stored.status == "succeeded"
    # The action received exactly the resolved reference — nothing else.
    assert calls[0]["arguments"] == {"text": A1}
    # The trusted scheduled-occurrence context: no chat history, no sender
    # history, no conversation state ever reaches a tool call.
    assert calls[0]["extra_keys"] <= {"scheduled_occurrence", "chat_id"}
    assert "scheduled_occurrence" in calls[0]["extra_keys"]


@pytest.mark.asyncio
async def test_no_dynamic_replanning_the_recorded_snapshot_is_the_whole_workflow():
    repo = InMemoryTaskRepository()
    calls = []
    registry = _registry(calls)
    actions = [_search("دانشگاه"), _q(Q1), _search(_ref(2)), _q(Q2), _send(_ref(4))]
    task, occurrence = await _start(repo, actions)
    before = await repo.get_task(OWNER, task.id)

    coordinator, _ = _coordinator(repo, registry)
    await coordinator.execute(occurrence)
    stored = await _wake_after_answer(repo, registry, task, reply_to=1001)
    await _wake_after_answer(repo, registry, task, text=A2, reply_to=1002)
    after = await repo.get_task(OWNER, task.id)
    done = await _stored(repo, task.id)

    # The definition is byte-identical before and after every answer.
    assert after.actions == before.actions == actions
    # Only the snapshot's own actions ever executed — nothing invented.
    snapshot_names = {action["name"] for action in actions}
    assert set(_names(calls)) <= snapshot_names
    assert done.status == "succeeded"
    # Completed checkpoints were never re-evaluated or reopened.
    assert _run(done, 2)["output"] == {ANSWER_FIELD: A1}
    assert _run(done, 4)["output"] == {ANSWER_FIELD: A2}


# ── 8: end to end — the mixed workflow as ONE occurrence ────────────────────


@pytest.mark.asyncio
async def test_end_to_end_search_question_save_question_deliver():
    """SEARCH → Q1 → SEARCH(A1) → SAVE → Q2 → A2 → DELIVER — the example
    request fitted to the bounded 5-action chain: ONE task, ONE occurrence,
    driven through the real scheduler, coordinator, executor and resolver,
    with a process restart between the two conversational checkpoints."""
    repo = InMemoryTaskRepository()
    calls = []
    telegram = _QuestionTelegram()
    registry = _registry(calls, telegram=telegram)
    actions = [
        _q(Q1),                                        # 1
        _search(_ref(1)),                              # 2 consumes A1
        _save(_ref(2, "top_title")),                   # 3 consumes search output
        _q(Q2),                                        # 4
        {"name": "send_message", "arguments": {"text": _ref(4)},
         "not_before": "2027-01-01T18:00:00"},         # 5 deliver at 18:00
    ]
    service = TaskCreationService(repo, OWNER, tool_registry=registry)
    task = await service.create(_task_payload(actions), START - timedelta(minutes=30))
    assert await repo.list_occurrences(OWNER, task.id) == []  # nothing pre-created

    scheduler = _scheduler(repo, registry)
    resolver = TaskAnswerResolver(repo, OWNER)

    # Wake 1: Question #1 parks (nothing else has run).
    assert await scheduler.run_once(now=START) == 1
    stored = (await repo.list_occurrences(OWNER, task.id))[0]
    assert stored.status == WAITING_ANSWER_STATUS
    assert telegram.sent == [(OWNER, Q1)]
    assert _names(calls) == []

    # Answer #1 (fresh process): SEARCH(A1) → SAVE(search result) → Q2 parks.
    assert await resolver.handle_event(_answer_event(A1, reply_to=1001)) is True
    parked = (await repo.list_occurrences(OWNER, task.id))[0]
    fresh = _registry(calls, telegram=telegram)
    assert await _scheduler(repo, fresh).run_once(
        now=parked.retry_at + timedelta(seconds=1)
    ) == 1
    stored = (await repo.list_occurrences(OWNER, task.id))[0]
    assert stored.status == WAITING_ANSWER_STATUS
    assert _names(calls) == ["web_search", "save"]
    assert calls[0]["arguments"]["query"] == A1
    assert calls[1]["arguments"]["text"] == "TU München"
    assert telegram.sent == [(OWNER, Q1), (OWNER, Q2)]
    assert _question_message_id(stored) == 1002

    # A duplicate of Answer #1 answers nothing; the checkpoint stays Q2.
    assert await resolver.handle_event(_answer_event(A1, reply_to=1001)) is False
    assert await scheduler.run_once(now=stored.retry_at or NOW) == 0

    # Answer #2 (fresh process): the wake parks on the 18:00 boundary.
    assert await resolver.handle_event(_answer_event(A2, reply_to=1002)) is True
    parked2 = (await repo.list_occurrences(OWNER, task.id))[0]
    assert await _scheduler(repo, _registry(calls, telegram=telegram)).run_once(
        now=parked2.retry_at + timedelta(seconds=1)
    ) == 1
    stored = (await repo.list_occurrences(OWNER, task.id))[0]
    assert stored.status == "retry_pending"
    assert _run(stored, 5)["status"] == "pending"

    # The wake at/after 18:00 delivers; the ONE occurrence succeeds.
    boundary = datetime(2027, 1, 1, 18, 0, 1, tzinfo=timezone.utc)
    assert await _scheduler(repo, _registry(calls, telegram=telegram)).run_once(
        now=boundary
    ) == 1
    done = (await repo.list_occurrences(OWNER, task.id))[0]
    assert done.status == "succeeded"
    assert _names(calls) == ["web_search", "save", "send_message"]
    assert _run(done, 1)["output"] == {ANSWER_FIELD: A1}
    assert _run(done, 2)["output"] == {"top_title": "TU München", "top_url": "https://tum.de"}
    assert _run(done, 3)["output"] == {"save_code": "S0001"}
    assert _run(done, 4)["output"] == {ANSWER_FIELD: A2}
    # One task, one occurrence, ordered execution, every question asked once.
    assert len(await repo.list_occurrences(OWNER, task.id)) == 1
    assert telegram.sent == [(OWNER, Q1), (OWNER, Q2)]
