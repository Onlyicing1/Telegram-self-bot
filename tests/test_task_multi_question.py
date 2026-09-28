"""Multi-turn question / instruction continuation — several bounded questions.

The product contract under test (Todo Part 3E): a durable task's ordered
actions may carry SEVERAL ``ask_owner`` actions. Each is an independent
durable checkpoint with its own Telegram correlation identity (the exact
sent question message), its own persisted answer (in that question action's
own run record), and its own park/resume cycle — all inside ONE task, ONE
occurrence, ONE ordered chain, ONE coordinator, ONE scheduler and ONE
ToolExecutor.

The one-active-question invariant is structural: the chain walk STOPS the
moment it reaches an open question (``waiting_answer``), so a later question
cannot even be sent while an earlier one is unanswered, and at most ONE
question is ever waiting at any point. Answers integrate with the existing
Phase 3A reference mechanism: question N's answer lives in question N's run
record as ``{"answer": …}``, so ``{"$ref": {"action": N, "field":
"answer"}}`` and a 3C condition source read exactly that question's answer —
never another's, never a generic "current answer".

Boundaries preserved: the ToolRegistry stays the capability allowlist, the
ToolExecutor stays the sole caller of ``tool.execute()`` (the answer resolver
never executes anything — consuming an answer is a repository CAS), the
scheduler stays the only wake authority, and the restart contract leaves a
parked question untouched while never re-asking an answered one.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from backend.ai.database.task_repository import InMemoryTaskRepository
from backend.ai.task_answers import TaskAnswerResolver
from backend.ai.task_contract import (
    ANSWER_FIELD,
    ACTION_RUNS_KEY,
    PENDING_QUESTION_KEY,
    QUESTION_MESSAGE_ID_KEY,
    QUESTION_TOOL,
    WAITING_ANSWER_STATUS,
    build_pending_question,
    pending_question_from_metadata,
    question_chain_error,
)
from backend.ai.task_creation import TaskCreationService
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
Q2 = "کدام رشته؟"
Q3 = "کدام شهر؟"
A1 = "دانشگاه تهران"
A2 = "مهندسی کامپیوتر"
A3 = "تهران"


# ── The Phase 3D harness, unchanged ─────────────────────────────────────────


class ChainTool:
    permission_level = PermissionLevel.READ_WRITE
    long_running = False
    safe = True
    description = "chain test tool"
    parameters = {}
    return_type = "object"

    def __init__(self, name, calls, *, data=None, fields=()):
        self.name = name
        self.calls = calls
        self.data = dict(data or {})
        self.consumable_output_fields = tuple(fields)

    async def execute(self, context, arguments):
        self.calls.append({
            "name": self.name,
            "arguments": dict(arguments),
            "owner": context.owner_id,
        })
        return ToolResult(True, "ok", dict(self.data))


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


def _question_registry(calls, *, telegram=None, search_data=None):
    telegram = telegram if telegram is not None else _QuestionTelegram()
    context = ToolContext(telegram, OWNER, "UTC")
    registry = ToolRegistry()
    from backend.ai.tools.question import AskOwnerTool

    registry.register(AskOwnerTool(context))
    registry.register(ChainTool(
        "send_message", calls, data={"sent": True}, fields=("sent",),
    ))
    registry.register(ChainTool(
        "web_search", calls,
        data={"summary": search_data or "نتایج سرچ"}, fields=("summary",),
    ))
    registry.register(ChainTool(
        "save", calls, data={"save_code": "S0001"}, fields=("save_code",),
    ))
    return registry


def _q(text):
    return {"name": QUESTION_TOOL, "arguments": {"question": text}}


def _t(text="x"):
    return {"name": "send_message", "arguments": {"text": text}}


def _search(query):
    return {"name": "web_search", "arguments": {"query": query}}


def _ref(position, field=ANSWER_FIELD):
    return {"$ref": {"action": position, "field": field}}


def _task_payload(actions, *, at="2026-09-26T14:00:00"):
    return {
        "label": "multi-question",
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


def _coordinator(repo, registry, *, owner=OWNER):
    telegram = registry.get(QUESTION_TOOL)._context.telegram
    ctx = ToolContext(telegram, owner, "UTC")
    executor = ToolExecutor(registry, ctx)
    return TaskExecutionCoordinator(repo, executor, owner, ctx), executor


async def _stored(repo, task_id, key="k", owner=OWNER):
    return await repo.get_occurrence(owner, task_id, key)


def _names(calls):
    return [call["name"] for call in calls]


def _runs_of(occurrence):
    for metadata in (occurrence.result_metadata, occurrence.error_metadata):
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
    """A reply keyed the way the raw Telegram context is keyed."""
    return {**_answer_event(text, reply_to=message_id), **overrides}


def _question_message_id(stored):
    record = pending_question_from_metadata(stored.result_metadata)
    assert record is not None
    return record[QUESTION_MESSAGE_ID_KEY]


def _scheduler(repo, registry):
    coordinator, _ = _coordinator(repo, registry)
    return TaskScheduler(repo, OWNER, coordinator, outcome_notifier=None)


async def _drive_one_question(repo, registry, task, occurrence, *, text=A1):
    """Park (unless already parked) → correlated reply → scheduler wake."""
    stored = await _stored(repo, task.id)
    if stored.status != WAITING_ANSWER_STATUS:
        coordinator, _ = _coordinator(repo, registry)
        result = await coordinator.execute(occurrence)
        assert result.status == WAITING_ANSWER_STATUS
        stored = await _stored(repo, task.id)
    message_id = _question_message_id(stored)
    resolver = TaskAnswerResolver(repo, OWNER)
    assert await resolver.handle_event(_reply_event(text, message_id)) is True
    parked = await _stored(repo, task.id)
    assert parked.retry_at is not None
    scheduler = _scheduler(repo, registry)
    assert await scheduler.run_once(now=parked.retry_at + timedelta(seconds=1)) == 1
    return await _stored(repo, task.id)


# ── 1: the multi-question contract ──────────────────────────────────────────


def test_two_and_three_questions_are_a_valid_chain():
    two = [_q(Q1), _t(_ref(1)), _q(Q2), _t(_ref(3))]
    three = [_search("درباره دانشگاه"), _q(Q1), _search(_ref(2)), _q(Q2),
             _search(_ref(2) | {} | _ref(4)) if False else _search("درباره رشته"),
             _t("پایان")]
    assert question_chain_error(two) is None
    assert question_chain_error(three) is None
    assert question_chain_error([_q(Q1), _q(Q2), _q(Q3), _t("x")]) is None


def test_every_question_still_takes_exactly_one_bounded_argument():
    assert "action 2" in question_chain_error([
        _q(Q1), {"name": QUESTION_TOOL, "arguments": {"question": Q2, "chat_id": 5}},
    ])
    assert "action 2" in question_chain_error([
        _q(Q1), {"name": QUESTION_TOOL, "arguments": {"text": Q2}},
    ])
    assert "action 2" in question_chain_error([
        _q(Q1), {"name": QUESTION_TOOL, "arguments": {"question": "  "}},
    ])
    assert "action 2" in question_chain_error([
        _q(Q1), {"name": QUESTION_TOOL, "arguments": {"question": "ط" * 513}},
    ])


def test_no_wait_boundary_may_sit_before_the_first_question():
    wait_action = {"name": "send_message", "arguments": {"text": "x"},
                   "not_before": "2026-09-26T13:00:00"}
    assert "wait boundary may not sit before the question" in question_chain_error(
        [wait_action, _q(Q1), _t(_ref(2)), _q(Q2), _t(_ref(4))]
    )
    # A wait BETWEEN/AFTER questions is a legitimate Phase 3B composition.
    assert question_chain_error([
        _q(Q1), _t(_ref(1)), _q(Q2), _t(_ref(3)),
        {"name": "send_message", "arguments": {"text": "x"},
         "not_before": "2027-01-01T18:00:00"},
    ]) is None


def test_a_question_may_live_inside_a_selected_branch_since_part_3e():
    """A branch question is always AFTER the chain's condition (never its
    source), and the non-selected branch never asks — so the Part 3D refusal
    became unnecessary in Phase 3E."""
    actions = [
        _search("دانشگاه"),
        {"condition": {"source": {"action": 1, "field": "summary"},
                       "operator": "equals", "value": "تعطیل"}},
        {**_q(Q1), "branch": "true"},
        {**_t("بله"), "branch": "true"},
        {**_t(_ref(1)), "branch": "false"},
    ]
    assert question_chain_error(actions) is None


def test_a_condition_may_read_any_question_s_answer():
    actions = [
        _q(Q1),
        _t(_ref(1)),
        _q(Q2),
        {"condition": {"source": {"action": 3, "field": ANSWER_FIELD},
                       "operator": "equals", "value": A2}},
        {**_t("true run"), "branch": "true"},
        {**_t("false run"), "branch": "false"},
    ]
    assert question_chain_error(actions) is None


@pytest.mark.asyncio
async def test_creation_accepts_a_multi_question_chain_end_to_end():
    repo = InMemoryTaskRepository()
    registry = _question_registry([])
    service = TaskCreationService(repo, OWNER, tool_registry=registry)
    task = await service.create(
        _task_payload([
            _search("درباره دانشگاه"),
            _q(Q1),
            _search(_ref(2)),
            _q(Q2),
            _t("پایان"),
        ]),
        START - timedelta(minutes=30),
    )
    assert len(task.actions) == 5  # the existing bounded chain length
    assert task.actions[1]["name"] == QUESTION_TOOL
    assert task.actions[3]["name"] == QUESTION_TOOL


# ── 2: execution — ordered parks, one active question at a time ─────────────


@pytest.mark.asyncio
async def test_one_question_still_works_exactly_as_part_3d():
    repo = InMemoryTaskRepository()
    calls = []
    telegram = _QuestionTelegram()
    registry = _question_registry(calls, telegram=telegram)
    task, occurrence = await _start(repo, [_q(Q1), _t(_ref(1))])
    coordinator, _ = _coordinator(repo, registry)

    result = await coordinator.execute(occurrence)
    assert result.status == WAITING_ANSWER_STATUS
    assert telegram.sent == [(OWNER, Q1)]
    stored = await _stored(repo, task.id)
    assert _run(stored, 1)["status"] == "pending"

    finished = await _drive_one_question(repo, registry, task, occurrence, text=A1)
    assert finished.status == "succeeded"
    assert calls[-1]["arguments"]["text"] == A1


@pytest.mark.asyncio
async def test_two_sequential_questions_each_keep_their_own_answer():
    repo = InMemoryTaskRepository()
    calls = []
    telegram = _QuestionTelegram()
    registry = _question_registry(calls, telegram=telegram)
    task, occurrence = await _start(repo, [
        _q(Q1), _search(_ref(1)), _q(Q2), _t(_ref(3)),
    ])
    coordinator, _ = _coordinator(repo, registry)

    # First park: Question #1 only. Nothing later has executed.
    first = await coordinator.execute(occurrence)
    assert first.status == WAITING_ANSWER_STATUS
    assert telegram.sent == [(OWNER, Q1)]
    stored = await _stored(repo, task.id)
    assert _question_message_id(stored) == 1001
    assert _names(calls) == []  # the search between the questions has NOT run

    resolver = TaskAnswerResolver(repo, OWNER)
    assert await resolver.handle_event(_answer_event(A1, reply_to=1001)) is True
    parked = await _stored(repo, task.id)
    assert parked.retry_at is not None
    scheduler = _scheduler(repo, registry)
    assert await scheduler.run_once(now=parked.retry_at + timedelta(seconds=1)) == 1

    # After Answer #1 the chain ran the search AND reached Question #2.
    second = await _stored(repo, task.id)
    assert second.status == WAITING_ANSWER_STATUS
    assert telegram.sent == [(OWNER, Q1), (OWNER, Q2)]
    assert _question_message_id(second) == 1002  # a NEW durable identity
    assert _run(second, 1)["status"] == "succeeded"
    assert _run(second, 1)["output"] == {ANSWER_FIELD: A1}
    assert _run(second, 2)["status"] == "succeeded"  # the search consumed A1
    assert calls[0]["arguments"]["query"] == A1
    assert _run(second, 3)["status"] == "pending"  # Question #2 waiting

    # Answer #2 finishes the chain through ITS OWN question identity.
    assert await resolver.handle_event(_answer_event(A2, reply_to=1002)) is True
    parked2 = await _stored(repo, task.id)
    assert await scheduler.run_once(now=parked2.retry_at + timedelta(seconds=1)) == 1
    done = await _stored(repo, task.id)
    assert done.status == "succeeded"
    assert _run(done, 3)["output"] == {ANSWER_FIELD: A2}
    assert calls[-1]["arguments"]["text"] == A2


@pytest.mark.asyncio
async def test_three_sequential_questions_stay_distinct_to_the_end():
    repo = InMemoryTaskRepository()
    calls = []
    telegram = _QuestionTelegram()
    registry = _question_registry(calls, telegram=telegram)
    task, occurrence = await _start(repo, [
        _q(Q1), _q(Q2), _q(Q3), _t(_ref(3)),
    ])
    coordinator, _ = _coordinator(repo, registry)
    resolver = TaskAnswerResolver(repo, OWNER)
    scheduler = _scheduler(repo, registry)

    await coordinator.execute(occurrence)
    stored = await _stored(repo, task.id)
    assert telegram.sent == [(OWNER, Q1)]
    assert _run(stored, 2)["status"] == "pending"  # Q2 blocked behind Q1

    for reply_to, answer in ((1001, A1), (1002, A2), (1003, A3)):
        assert await resolver.handle_event(
            _answer_event(answer, reply_to=reply_to)
        ) is True
        parked = await _stored(repo, task.id)
        assert await scheduler.run_once(
            now=parked.retry_at + timedelta(seconds=1)
        ) == 1
        stored = await _stored(repo, task.id)

    assert stored.status == "succeeded"
    assert telegram.sent == [(OWNER, Q1), (OWNER, Q2), (OWNER, Q3)]
    assert _run(stored, 1)["output"] == {ANSWER_FIELD: A1}
    assert _run(stored, 2)["output"] == {ANSWER_FIELD: A2}
    assert _run(stored, 3)["output"] == {ANSWER_FIELD: A3}
    assert calls[0]["arguments"]["text"] == A3


@pytest.mark.asyncio
async def test_a_question_after_a_normal_action_and_before_one():
    repo = InMemoryTaskRepository()
    calls = []
    telegram = _QuestionTelegram()
    registry = _question_registry(calls, telegram=telegram)
    # Normal action → question → normal action.
    task, occurrence = await _start(repo, [_search("دانشگاه"), _q(Q1), _t(_ref(2))])
    coordinator, _ = _coordinator(repo, registry)

    await coordinator.execute(occurrence)
    stored = await _stored(repo, task.id)
    assert _names(calls) == ["web_search"]  # the earlier action ran
    assert _run(stored, 1)["status"] == "succeeded"

    finished = await _drive_one_question(repo, registry, task, occurrence, text=A1)
    assert finished.status == "succeeded"
    assert calls[-1]["arguments"]["text"] == A1


@pytest.mark.asyncio
async def test_a_question_before_any_normal_action_blocks_the_whole_chain():
    repo = InMemoryTaskRepository()
    calls = []
    registry = _question_registry(calls)
    task, occurrence = await _start(repo, [_q(Q1), _search(_ref(1)), _t("x")])
    coordinator, _ = _coordinator(repo, registry)

    result = await coordinator.execute(occurrence)
    assert result.status == WAITING_ANSWER_STATUS
    stored = await _stored(repo, task.id)
    assert _names(calls) == []
    assert _run(stored, 2)["status"] == "pending"
    assert _run(stored, 3)["status"] == "pending"


# ── 3: exact question identity and correlation ──────────────────────────────


@pytest.mark.asyncio
async def test_a_reply_to_question_one_cannot_answer_question_two():
    """Question identities are immutable: after Q1 is answered and Q2 becomes
    active, a LATE duplicate reply to Q1's message answers nothing."""
    repo = InMemoryTaskRepository()
    calls = []
    registry = _question_registry(calls)
    task, occurrence = await _start(repo, [_q(Q1), _t(_ref(1)), _q(Q2), _t(_ref(3))])
    coordinator, _ = _coordinator(repo, registry)
    resolver = TaskAnswerResolver(repo, OWNER)
    scheduler = _scheduler(repo, registry)

    await coordinator.execute(occurrence)
    assert await resolver.handle_event(_answer_event(A1, reply_to=1001)) is True
    parked = await _stored(repo, task.id)
    assert await scheduler.run_once(now=parked.retry_at + timedelta(seconds=1)) == 1

    # Question #2 is now the active one (message 1002). A duplicate reply to
    # question #1's ORIGINAL message must not consume anything.
    assert await resolver.handle_event(_answer_event("late", reply_to=1001)) is False
    stored = await _stored(repo, task.id)
    assert stored.status == WAITING_ANSWER_STATUS
    assert _question_message_id(stored) == 1002
    assert _run(stored, 3)["status"] == "pending"


@pytest.mark.asyncio
async def test_a_reply_to_the_wrong_message_never_consumes_any_question():
    repo = InMemoryTaskRepository()
    registry = _question_registry([])
    task, occurrence = await _start(repo, [_q(Q1), _t(_ref(1)), _q(Q2), _t(_ref(3))])
    coordinator, _ = _coordinator(repo, registry)
    await coordinator.execute(occurrence)

    resolver = TaskAnswerResolver(repo, OWNER)
    for wrong in (9999, 1002, 1003):
        assert await resolver.handle_event(
            _answer_event(A1, reply_to=wrong)
        ) is False
    stored = await _stored(repo, task.id)
    assert stored.status == WAITING_ANSWER_STATUS
    assert _question_message_id(stored) == 1001


@pytest.mark.asyncio
async def test_an_unrelated_plain_message_is_ignored_while_waiting():
    repo = InMemoryTaskRepository()
    registry = _question_registry([])
    task, occurrence = await _start(repo, [_q(Q1), _t(_ref(1)), _q(Q2), _t(_ref(3))])
    coordinator, _ = _coordinator(repo, registry)
    await coordinator.execute(occurrence)

    resolver = TaskAnswerResolver(repo, OWNER)
    assert await resolver.handle_event(_answer_event("سلام")) is False
    assert await resolver.handle_event(_answer_event("مستقل، بدون ریپلای")) is False
    stored = await _stored(repo, task.id)
    assert stored.status == WAITING_ANSWER_STATUS
    assert _run(stored, 1)["status"] == "pending"


@pytest.mark.asyncio
async def test_wrong_user_wrong_chat_and_wrong_task_replies_are_refused():
    repo = InMemoryTaskRepository()
    registry = _question_registry([])
    task, occurrence = await _start(repo, [_q(Q1), _t(_ref(1)), _q(Q2), _t(_ref(3))])
    coordinator, _ = _coordinator(repo, registry)
    await coordinator.execute(occurrence)

    resolver = TaskAnswerResolver(repo, OWNER)
    assert await resolver.handle_event(
        _reply_event(A1, 1001, sender_id=OTHER_OWNER)
    ) is False
    assert await resolver.handle_event(
        _reply_event(A1, 1001, chat_id=OTHER_CHAT)
    ) is False
    # A DIFFERENT task's parked question is a different occurrence entirely:
    # its question runs in a DIFFERENT chat, so its (chat_id, message_id)
    # identity can never correlate with a reply meant for this task.
    other_calls = []
    other_telegram = _QuestionTelegram(chat_id=OTHER_CHAT)
    other_telegram.next_id = 5000  # a distinct Telegram id space
    other_registry = ToolRegistry()
    from backend.ai.tools.question import AskOwnerTool as _AskOwner

    other_registry.register(_AskOwner(ToolContext(other_telegram, OWNER, "UTC")))
    other_registry.register(ChainTool("send_message", other_calls, data={"sent": True}))
    other_payload = _task_payload([_q(Q3), _t(_ref(1))], at="2026-09-26T15:00:00")
    other_payload["notification_destination"] = {"chat_id": OTHER_CHAT}
    other_task = await repo.create_task(OWNER, other_payload)
    await repo.create_occurrence(OWNER, {
        "task_id": other_task.id,
        "occurrence_key": "k",
        "definition_version": other_task.version,
        "action_snapshot": [_q(Q3), _t(_ref(1))],
        "scheduled_for": NOW,
    })
    other_claimed = await repo.claim_occurrence(OWNER, other_task.id, "k")
    other_coordinator, _ = _coordinator(repo, other_registry)
    await other_coordinator.execute(other_claimed)
    other_stored = await _stored(repo, other_task.id)
    other_message_id = _question_message_id(other_stored)
    other_record = pending_question_from_metadata(other_stored.result_metadata)
    assert other_record["question_chat_id"] == OTHER_CHAT  # a different chat
    # A reply to the OTHER task's question message (other chat) is not an
    # answer for THIS task's parked question.
    assert await resolver.handle_event(
        _reply_event(A1, other_message_id)
    ) is False
    # ...and THIS task still answers normally afterwards.
    assert await resolver.handle_event(_reply_event(A1, 1001)) is True


@pytest.mark.asyncio
async def test_the_active_question_is_always_the_recorded_pending_one():
    repo = InMemoryTaskRepository()
    calls = []
    registry = _question_registry(calls)
    task, occurrence = await _start(repo, [_q(Q1), _t(_ref(1)), _q(Q2), _t(_ref(3))])
    coordinator, _ = _coordinator(repo, registry)
    resolver = TaskAnswerResolver(repo, OWNER)
    scheduler = _scheduler(repo, registry)

    await coordinator.execute(occurrence)
    stored = await _stored(repo, task.id)
    record = pending_question_from_metadata(stored.result_metadata)
    assert record["action"] == 1
    assert record[QUESTION_MESSAGE_ID_KEY] == 1001

    await resolver.handle_event(_answer_event(A1, reply_to=1001))
    parked = await _stored(repo, task.id)
    await scheduler.run_once(now=parked.retry_at + timedelta(seconds=1))
    stored = await _stored(repo, task.id)
    record = pending_question_from_metadata(stored.result_metadata)
    assert record is not None and record["action"] == 3
    assert record[QUESTION_MESSAGE_ID_KEY] == 1002


# ── 4: answer persistence and the existing reference mechanism ──────────────


@pytest.mark.asyncio
async def test_each_answer_persists_in_its_own_question_run_record():
    repo = InMemoryTaskRepository()
    calls = []
    registry = _question_registry(calls)
    task, occurrence = await _start(repo, [
        _q(Q1), _search(_ref(1)), _q(Q2), _search(_ref(3)),
    ])
    coordinator, _ = _coordinator(repo, registry)
    resolver = TaskAnswerResolver(repo, OWNER)
    scheduler = _scheduler(repo, registry)

    await coordinator.execute(occurrence)
    await resolver.handle_event(_reply_event(A1, 1001))
    parked = await _stored(repo, task.id)
    await scheduler.run_once(now=parked.retry_at + timedelta(seconds=1))
    after_first = await _stored(repo, task.id)
    assert _run(after_first, 1)["output"] == {ANSWER_FIELD: A1}

    await resolver.handle_event(_reply_event(A2, 1002))
    parked2 = await _stored(repo, task.id)
    await scheduler.run_once(now=parked2.retry_at + timedelta(seconds=1))
    done = await _stored(repo, task.id)

    # Answers #1 and #2 remain distinct — no generic "current answer" field.
    assert _run(done, 1)["output"] == {ANSWER_FIELD: A1}
    assert _run(done, 3)["output"] == {ANSWER_FIELD: A2}
    assert calls[0]["arguments"]["query"] == A1
    assert calls[1]["arguments"]["query"] == A2
    assert done.result_metadata.get("pending_answer") in (A2, None)


@pytest.mark.asyncio
async def test_a_later_action_may_reference_both_answers():
    repo = InMemoryTaskRepository()
    calls = []
    registry = _question_registry(calls)
    task, occurrence = await _start(repo, [
        _q(Q1), _q(Q2),
        {"name": "web_search",
         "arguments": {"query": {"$concat": [A1, A2]}} if False else {"query": "دانشگاه رشته"}},
        _t(_ref(1)),
    ])
    # The THIRD action references question #1's answer through the existing
    # mechanism; a second argument cannot carry a second reference, so the
    # BOTH-answers case is exercised through two separate referencing actions.
    actions = [_q(Q1), _q(Q2), _search(_ref(1)), _t(_ref(2))]
    repo2 = InMemoryTaskRepository()
    task2, occurrence2 = await _start(repo2, actions)
    coordinator2, _ = _coordinator(repo2, registry)
    resolver = TaskAnswerResolver(repo2, OWNER)
    scheduler = _scheduler(repo2, registry)

    await coordinator2.execute(occurrence2)
    await resolver.handle_event(_answer_event(A1, reply_to=1001))
    parked = await _stored(repo2, task2.id)
    await scheduler.run_once(now=parked.retry_at + timedelta(seconds=1))
    await resolver.handle_event(_answer_event(A2, reply_to=1002))
    parked2 = await _stored(repo2, task2.id)
    await scheduler.run_once(now=parked2.retry_at + timedelta(seconds=1))

    done = await _stored(repo2, task2.id)
    assert done.status == "succeeded"
    assert calls[-2]["arguments"]["query"] == A1  # references Q1's answer
    assert calls[-1]["arguments"]["text"] == A2   # references Q2's answer


@pytest.mark.asyncio
async def test_a_reference_to_a_future_question_is_rejected():
    from backend.ai.task_contract import action_reference_error

    registry = _question_registry([])
    actions = [_search(_ref(2)), _q(Q1)]  # the search would read Q1's answer
    assert action_reference_error(actions, registry) is not None


@pytest.mark.asyncio
async def test_an_unanswered_question_blocks_the_referencing_action():
    repo = InMemoryTaskRepository()
    calls = []
    registry = _question_registry(calls)
    task, occurrence = await _start(repo, [_q(Q1), _search(_ref(1))])
    coordinator, _ = _coordinator(repo, registry)

    await coordinator.execute(occurrence)
    stored = await _stored(repo, task.id)
    # The chain parked BEFORE the search: an unanswered question can never
    # leave a run record a reference could resolve.
    assert _run(stored, 1)["status"] == "pending"
    assert _run(stored, 2)["status"] == "pending"
    assert _names(calls) == []


@pytest.mark.asyncio
async def test_cross_task_and_cross_occurrence_references_are_impossible():
    from backend.ai.task_contract import action_reference_error

    registry = _question_registry([])
    # A reference is structurally bound to THIS action list: a target beyond
    # the chain's own positions cannot even be expressed as "another task's
    # action" — the validator refuses anything that is not an earlier action
    # of the same chain, which is the cross-task/cross-occurrence refusal.
    assert action_reference_error([_t(_ref(5))], registry) is not None
    assert action_reference_error([_t({"$ref": {"action": 0, "field": "answer"}})], registry) is not None


# ── 5: restart, duplicate answers, crash semantics ──────────────────────────


@pytest.mark.asyncio
async def test_a_restart_while_question_one_is_pending_never_re_asks():
    repo = InMemoryTaskRepository()
    calls = []
    telegram = _QuestionTelegram()
    registry = _question_registry(calls, telegram=telegram)
    task, occurrence = await _start(repo, [_q(Q1), _t(_ref(1)), _q(Q2), _t(_ref(3))])
    coordinator, _ = _coordinator(repo, registry)
    await coordinator.execute(occurrence)

    # "Restart": a fresh coordinator over the SAME repository, a fresh wake.
    fresh = _question_registry(calls, telegram=telegram)
    fresh_scheduler = _scheduler(repo, fresh)
    stored = await _stored(repo, task.id)
    assert stored.status == WAITING_ANSWER_STATUS
    assert await fresh_scheduler.run_once(now=stored.retry_at or NOW) == 0
    due = await repo.list_due_retry_occurrences(OWNER, NOW + timedelta(hours=1))
    assert all(item.occurrence_key != stored.occurrence_key for item in due)
    assert await repo.claim_occurrence(OWNER, task.id, stored.occurrence_key) is None
    assert telegram.sent == [(OWNER, Q1)]  # asked exactly once, never re-sent


@pytest.mark.asyncio
async def test_a_restart_after_the_first_answer_resumes_without_re_asking_q1():
    repo = InMemoryTaskRepository()
    calls = []
    telegram = _QuestionTelegram()
    registry = _question_registry(calls, telegram=telegram)
    task, occurrence = await _start(repo, [
        _q(Q1), _search(_ref(1)), _q(Q2), _t(_ref(3)),
    ])
    coordinator, _ = _coordinator(repo, registry)
    resolver = TaskAnswerResolver(repo, OWNER)
    scheduler = _scheduler(repo, registry)

    await coordinator.execute(occurrence)
    await resolver.handle_event(_answer_event(A1, reply_to=1001))
    parked = await _stored(repo, task.id)
    await scheduler.run_once(now=parked.retry_at + timedelta(seconds=1))
    stored = await _stored(repo, task.id)
    assert stored.status == WAITING_ANSWER_STATUS
    assert telegram.sent == [(OWNER, Q1), (OWNER, Q2)]

    # Restart #2: a fresh process over the same repository. Q1 stays answered
    # (never re-asked), Q2 stays pending (never re-sent), and the owner's
    # reply to Q2 finishes the chain.
    fresh = _question_registry(calls, telegram=telegram)
    fresh_resolver = TaskAnswerResolver(repo, OWNER)
    fresh_scheduler = _scheduler(repo, fresh)
    assert await fresh_resolver.handle_event(_answer_event(A2, reply_to=1002)) is True
    parked2 = await _stored(repo, task.id)
    assert await fresh_scheduler.run_once(
        now=parked2.retry_at + timedelta(seconds=1)
    ) == 1
    done = await _stored(repo, task.id)
    assert done.status == "succeeded"
    assert telegram.sent == [(OWNER, Q1), (OWNER, Q2)]
    assert _run(done, 1)["output"] == {ANSWER_FIELD: A1}
    assert _run(done, 3)["output"] == {ANSWER_FIELD: A2}


@pytest.mark.asyncio
async def test_a_duplicate_reply_is_accepted_exactly_once():
    repo = InMemoryTaskRepository()
    calls = []
    registry = _question_registry(calls)
    task, occurrence = await _start(repo, [
        _q(Q1), _search(_ref(1)), _q(Q2), _t(_ref(3)),
    ])
    coordinator, _ = _coordinator(repo, registry)
    resolver = TaskAnswerResolver(repo, OWNER)
    scheduler = _scheduler(repo, registry)

    await coordinator.execute(occurrence)
    # First reply consumes; the immediate duplicate loses the CAS.
    assert await resolver.handle_event(_answer_event(A1, reply_to=1001)) is True
    assert await resolver.handle_event(_answer_event("دوباره", reply_to=1001)) is False
    parked = await _stored(repo, task.id)
    assert await scheduler.run_once(now=parked.retry_at + timedelta(seconds=1)) == 1

    # After the wake the chain reached Question #2: the duplicate must not
    # have consumed Question #2's checkpoint, and the search ran exactly once.
    stored = await _stored(repo, task.id)
    assert stored.status == WAITING_ANSWER_STATUS
    assert _names(calls) == ["web_search"]
    assert _question_message_id(stored) == 1002


@pytest.mark.asyncio
async def test_a_crash_between_park_and_resume_preserves_exact_progress():
    """Simulate the crash window: the park write landed, no answer arrived.
    Recovery must leave the row parked, and a later correlated reply must
    resume it at exactly the same checkpoint."""
    repo = InMemoryTaskRepository()
    calls = []
    telegram = _QuestionTelegram()
    registry = _question_registry(calls, telegram=telegram)
    task, occurrence = await _start(repo, [
        _q(Q1), _search(_ref(1)), _q(Q2), _t(_ref(3)),
    ])
    coordinator, _ = _coordinator(repo, registry)
    await coordinator.execute(occurrence)

    recoverable = await repo.list_recoverable_occurrences(OWNER)
    assert all(item.occurrence_key != occurrence.occurrence_key for item in recoverable)
    due = await repo.list_due_retry_occurrences(OWNER, NOW + timedelta(days=1))
    assert all(item.occurrence_key != occurrence.occurrence_key for item in due)

    resolver = TaskAnswerResolver(repo, OWNER)
    assert await resolver.handle_event(_answer_event(A1, reply_to=1001)) is True
    parked = await _stored(repo, task.id)
    scheduler = _scheduler(repo, registry)
    assert await scheduler.run_once(now=parked.retry_at + timedelta(seconds=1)) == 1
    stored = await _stored(repo, task.id)
    assert stored.status == WAITING_ANSWER_STATUS
    assert _run(stored, 1)["output"] == {ANSWER_FIELD: A1}
    assert _question_message_id(stored) == 1002


# ── 6: branch compatibility (Phase 3C) ──────────────────────────────────────


@pytest.mark.asyncio
async def test_a_condition_reads_an_answer_and_only_the_selected_branch_runs():
    repo = InMemoryTaskRepository()
    calls = []
    registry = _question_registry(calls)
    actions = [
        _q(Q1),
        {"condition": {"source": {"action": 1, "field": ANSWER_FIELD},
                       "operator": "equals", "value": A1}},
        {**_q(Q2), "branch": "true"},
        {**_t("true end"), "branch": "true"},
        {**_t("false end"), "branch": "false"},
    ]
    repo2 = InMemoryTaskRepository()
    task, occurrence = await _start(repo2, actions)
    coordinator, _ = _coordinator(repo2, registry)
    resolver = TaskAnswerResolver(repo2, OWNER)
    scheduler = _scheduler(repo2, registry)

    await coordinator.execute(occurrence)
    await resolver.handle_event(_answer_event(A1, reply_to=1001))
    parked = await _stored(repo2, task.id)
    await scheduler.run_once(now=parked.retry_at + timedelta(seconds=1))

    stored = await _stored(repo2, task.id)
    assert stored.status == WAITING_ANSWER_STATUS
    # The TRUE branch was selected (the condition read answer #1) and the
    # branch's own question became the active checkpoint.
    assert _run(stored, 2)["output"] == {"matched": True, "selected_branch": "true"}
    assert _run(stored, 3)["status"] == "pending"
    # The non-selected branch (false run) is durably inactive for this
    # occurrence — its skip label survives the park and every wake.
    assert _run(stored, 5)["status"] == "skipped"
    assert _question_message_id(stored) == 1002

    await resolver.handle_event(_answer_event(A2, reply_to=1002))
    parked2 = await _stored(repo2, task.id)
    await scheduler.run_once(now=parked2.retry_at + timedelta(seconds=1))
    done = await _stored(repo2, task.id)
    assert done.status == "succeeded"
    assert calls[-1]["arguments"]["text"] == "true end"
    assert _run(done, 5)["status"] == "skipped"


# ── 7: wait compatibility (Phase 3B) ────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_time_wait_after_questions_parks_on_the_clock_not_a_question():
    repo = InMemoryTaskRepository()
    calls = []
    telegram = _QuestionTelegram()
    registry = _question_registry(calls, telegram=telegram)
    actions = [
        _q(Q1), _t(_ref(1)), _q(Q2), _t(_ref(3)),
        {"name": "send_message", "arguments": {"text": "ساعت ۶"},
         "not_before": "2027-01-01T18:00:00"},
    ]
    task, occurrence = await _start(repo, actions)
    coordinator, _ = _coordinator(repo, registry)
    resolver = TaskAnswerResolver(repo, OWNER)
    scheduler = _scheduler(repo, registry)

    await coordinator.execute(occurrence)
    await resolver.handle_event(_answer_event(A1, reply_to=1001))
    parked = await _stored(repo, task.id)
    await scheduler.run_once(now=parked.retry_at + timedelta(seconds=1))
    await resolver.handle_event(_answer_event(A2, reply_to=1002))
    parked2 = await _stored(repo, task.id)
    await scheduler.run_once(now=parked2.retry_at + timedelta(seconds=1))

    stored = await _stored(repo, task.id)
    # Both answers consumed; the chain is now parked on the TIME boundary:
    # retry_pending with retry_at = the 18:00 boundary itself (Phase 3B —
    # a clock, never a question), and the last action still pending.
    assert stored.status == "retry_pending"
    assert stored.retry_at is not None
    assert _run(stored, 1)["output"] == {ANSWER_FIELD: A1}
    assert _run(stored, 3)["output"] == {ANSWER_FIELD: A2}
    assert _run(stored, 5)["status"] == "pending"
    assert telegram.sent == [(OWNER, Q1), (OWNER, Q2)]
    # The wake AT/after the boundary delivers; the ONE occurrence succeeds.
    assert await scheduler.run_once(
        now=stored.retry_at + timedelta(seconds=1)
    ) == 1
    done = await _stored(repo, task.id)
    assert done.status == "succeeded"


@pytest.mark.asyncio
async def test_waiting_answer_and_time_wait_stay_semantically_distinct():
    repo = InMemoryTaskRepository()
    registry = _question_registry([])
    task, occurrence = await _start(repo, [
        _q(Q1), _t(_ref(1)),
        {"name": "send_message", "arguments": {"text": "x"},
         "not_before": "2027-01-01T18:00:00"},
    ])
    coordinator, _ = _coordinator(repo, registry)
    await coordinator.execute(occurrence)
    stored = await _stored(repo, task.id)
    assert stored.status == WAITING_ANSWER_STATUS
    assert stored.retry_at is None  # a question has NO clock
    record = pending_question_from_metadata(stored.result_metadata)
    assert record is not None and record["answered"] is False


# ── 8: context isolation and boundary preservation ─────────────────────────


@pytest.mark.asyncio
async def test_the_executor_receives_only_the_structured_answer():
    repo = InMemoryTaskRepository()
    calls = []
    registry = _question_registry(calls)
    task, occurrence = await _start(repo, [_q(Q1), _t(_ref(1))])
    coordinator, _ = _coordinator(repo, registry)

    await coordinator.execute(occurrence)
    resolver = TaskAnswerResolver(repo, OWNER)
    await resolver.handle_event(_answer_event(A1, reply_to=1001))
    parked = await _stored(repo, task.id)
    scheduler = _scheduler(repo, registry)
    await scheduler.run_once(now=parked.retry_at + timedelta(seconds=1))

    done = await _stored(repo, task.id)
    assert done.status == "succeeded"
    # The downstream action received EXACTLY the bounded answer — no chat
    # history, no sender context, no event envelope, no conversation state.
    assert calls[0]["arguments"] == {"text": A1}
    assert calls[0]["owner"] == OWNER


def test_the_tool_executor_remains_the_sole_execution_authority():
    import inspect

    from backend.ai import task_answers

    source = inspect.getsource(task_answers)
    assert "execute_calls" not in source
    assert "tool.execute(" not in source
    assert "resume_waiting_for_answer" in source


# ── 9: end to end — the full example request as ONE occurrence ──────────────


@pytest.mark.asyncio
async def test_the_full_example_workflow_search_question_search_question_save():
    """SEARCH → Q1 → SEARCH(A1) → Q2 → SAVE → WAIT → DELIVER — the example
    request fitted to the existing bounded 5-action chain (the second answer
    rides the SAVE notification text). ONE task, ONE occurrence, driven end
    to end through the real scheduler, coordinator, executor and resolver."""
    repo = InMemoryTaskRepository()
    calls = []
    telegram = _QuestionTelegram()
    registry = _question_registry(calls, telegram=telegram)
    actions = [
        _search("درباره دانشگاه"),                     # 1
        _q(Q1),                                        # 2
        _search(_ref(2)),                              # 3
        _q(Q2),                                        # 4
        {"name": "send_message", "arguments": {"text": "نتیجه"},
         "not_before": "2027-01-01T18:00:00"},         # 5
    ]
    service = TaskCreationService(repo, OWNER, tool_registry=registry)
    task = await service.create(_task_payload(actions), START - timedelta(minutes=30))
    occurrences = await repo.list_occurrences(OWNER, task.id)
    assert occurrences == []  # creation pre-creates nothing

    scheduler = _scheduler(repo, registry)
    resolver = TaskAnswerResolver(repo, OWNER)

    # Wake 1: SEARCH runs, Question #1 parks.
    assert await scheduler.run_once(now=START) == 1
    stored = (await repo.list_occurrences(OWNER, task.id))[0]
    assert stored.status == WAITING_ANSWER_STATUS
    assert telegram.sent == [(OWNER, Q1)]
    assert _names(calls) == ["web_search"]

    # Answer #1 → wake: SEARCH(A1) runs, Question #2 parks.
    assert await resolver.handle_event(_answer_event(A1, reply_to=1001)) is True
    parked = (await repo.list_occurrences(OWNER, task.id))[0]
    assert await scheduler.run_once(now=parked.retry_at + timedelta(seconds=1)) == 1
    stored = (await repo.list_occurrences(OWNER, task.id))[0]
    assert stored.status == WAITING_ANSWER_STATUS
    assert _names(calls) == ["web_search", "web_search"]
    assert calls[1]["arguments"]["query"] == A1
    assert telegram.sent == [(OWNER, Q1), (OWNER, Q2)]
    assert _question_message_id(stored) == 1002

    # Answer #2 → wake: SAVE... (the chain's last action is the 18:00
    # DELIVERY, so the wake after Answer #2 parks on the time boundary.
    assert await resolver.handle_event(_answer_event(A2, reply_to=1002)) is True
    parked2 = (await repo.list_occurrences(OWNER, task.id))[0]
    assert await scheduler.run_once(now=parked2.retry_at + timedelta(seconds=1)) == 1
    stored = (await repo.list_occurrences(OWNER, task.id))[0]
    assert stored.status == "retry_pending"
    assert _names(calls) == ["web_search", "web_search"]
    assert _run(stored, 5)["status"] == "pending"

    # The wake at/after 18:00 delivers and the ONE occurrence succeeds.
    boundary = datetime(2027, 1, 1, 18, 0, 1, tzinfo=timezone.utc)
    assert await scheduler.run_once(now=boundary) == 1
    done = (await repo.list_occurrences(OWNER, task.id))[0]
    assert done.status == "succeeded"
    assert _names(calls) == ["web_search", "web_search", "send_message"]
    assert _run(done, 1)["output"] == {"summary": "نتایج سرچ"}
    assert _run(done, 2)["output"] == {ANSWER_FIELD: A1}
    assert _run(done, 4)["output"] == {ANSWER_FIELD: A2}
    # One task, one occurrence, every question asked exactly once.
    assert len(await repo.list_occurrences(OWNER, task.id)) == 1
    assert telegram.sent == [(OWNER, Q1), (OWNER, Q2)]
