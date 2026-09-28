"""Durable question / answer continuation — ONE answer resumes ONE parked chain.

The product contract under test (Todo Part 3D): a durable task's ordered
actions may carry at most ONE question — a REGISTERED ``ask_owner`` tool call
with a single bounded plain-text ``question`` argument. When the chain reaches
it the question is SENT through the existing ToolExecutor → TelegramAPI
boundary, the occurrence parks durably on ``waiting_answer`` (the ONE
non-terminal, non-retry status the scheduler, the retry query, the claim CAS
and recovery all leave alone), and the SAME occurrence resumes exactly where
it stopped when the owner's correlated reply is consumed.

The resume is ONE explicit CAS: ``owner_id + chat_id + reply_to_msg_id`` must
name EXACTLY the stored question message. The answer enters the chain through
the EXISTING Phase 3A reference mechanism — the question action's run record
carries the single declared field ``answer``, so a later action consumes it
with ``{"$ref": {"action": N, "field": "answer"}}`` and a condition reads it
with its existing source/operator/value contract.

Boundaries preserved by these tests: the ToolRegistry stays the capability
allowlist, the ToolExecutor stays the sole caller of ``tool.execute()`` (the
answer resolver never executes anything — consuming an answer is a repository
CAS), TaskScheduler stays the only scheduler, TaskExecutionCoordinator stays
the only occurrence/claim authority, and no schema change is involved beyond
the ``waiting_answer`` status value the existing status constraint now admits.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from backend.ai.database.task_repository import InMemoryTaskRepository
from backend.ai.task_answers import (
    TaskAnswerResolver,
    extract_answer_context,
)
from backend.ai.task_contract import (
    ANSWER_FIELD,
    PENDING_QUESTION_KEY,
    QUESTION_MESSAGE_ID_KEY,
    QUESTION_CHAT_ID_KEY,
    QUESTION_TEXT_KEY,
    QUESTION_TOOL,
    WAITING_ANSWER_STATUS,
    answer_run_output,
    build_pending_question,
    normalize_answer,
    pending_question_correlation_error,
    pending_question_from_metadata,
    question_chain_error,
    validate_question_text,
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
NOW = datetime(2026, 9, 26, 12, 0, tzinfo=timezone.utc)
START = datetime(2026, 9, 26, 14, 0, tzinfo=timezone.utc)
QUESTION = "آیا دانشگاه فردا تعطیل است؟"
ANSWER = "بله"


# ── Registry/executor/coordinator harness (the Phase 3A/3B/3C doubles) ──────


class ChainTool:
    """A registered tool double whose calls and results the test declares;
    the real ToolRegistry and ToolExecutor still own it."""

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
    """The minimal TelegramAPI double the AskOwnerTool needs: it records the
    send and returns a serialized-looking dict (id + chat_id)."""

    def __init__(self, chat_id=OWNER):
        self.chat_id = chat_id
        self.next_id = 1000
        self.sent = []

    async def send_message(self, chat_id, text, **kwargs):
        self.next_id += 1
        self.sent.append((chat_id, text))
        return {"id": self.next_id, "chat_id": chat_id, "text": text}


def _question_registry(calls, *, telegram=None):
    """The REAL ask_owner tool, registered beside the chain doubles.

    Only the Telegram transport is doubled — the tool itself is the real
    registered one, so the ToolExecutor stays the sole caller of
    ``tool.execute()`` end to end.
    """
    telegram = telegram if telegram is not None else _QuestionTelegram()
    context = ToolContext(telegram, OWNER, "UTC")
    registry = ToolRegistry()
    from backend.ai.tools.question import AskOwnerTool

    registry.register(AskOwnerTool(context))
    registry.register(ChainTool(
        "send_message", calls, data={"sent": True}, fields=("sent",),
    ))
    return registry


def _question_actions(*, tail_ref=True):
    """QUESTION → SEND: the send consumes the answer through the 3A reference."""
    tail_arguments = (
        {"text": {"$ref": {"action": 1, "field": ANSWER_FIELD}}} if tail_ref else {"text": "x"}
    )
    return [
        {"name": QUESTION_TOOL, "arguments": {"question": QUESTION}},
        {"name": "send_message", "arguments": tail_arguments},
    ]


def _answer_condition_actions():
    """QUESTION → CONDITION (reads the answer) → true SEND / false SEND."""
    return [
        {"name": QUESTION_TOOL, "arguments": {"question": QUESTION}},
        {
            "condition": {
                "source": {"action": 1, "field": ANSWER_FIELD},
                "operator": "equals",
                "value": ANSWER,
            }
        },
        {"name": "send_message", "arguments": {"text": "بله بود"}, "branch": "true"},
        {"name": "send_message", "arguments": {"text": "نه بود"}, "branch": "false"},
    ]


def _task_payload(actions, *, timezone_name="UTC", at="2027-01-01T09:00:00"):
    return {
        "label": "question",
        "schedule_type": "once",
        "schedule": {"at": at, "timezone": timezone_name},
        "timezone": timezone_name,
        "actions": actions,
        "notification_destination": {},
    }


async def _start(repo, actions, *, key="k", owner=OWNER):
    """Create the task + occurrence and claim it, as the scheduler would."""
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


def _run(record, position):
    return record.result_metadata["actions"][position - 1]


def _answer_event(text=ANSWER, *, chat_id=OWNER, message_id=1001, sender=OWNER,
                  reply_to=1001):
    """The bounded context one incoming Telegram message produces."""
    return {
        "chat_id": chat_id,
        "sender_id": sender,
        "message_id": message_id,
        "reply_to_message_id": reply_to,
        "text": text,
        "has_media": False,
    }


# ── 1: the question contract — bounded text, one argument, one question ─────


def test_a_question_is_normalized_bounded_plain_text():
    assert validate_question_text("  آیا   فردا  تعطیل است؟  ") == "آیا فردا تعطیل است؟"
    with pytest.raises(Exception, match="plain text"):
        validate_question_text({"text": QUESTION})
    with pytest.raises(Exception, match="nonblank"):
        validate_question_text("   ")
    with pytest.raises(Exception, match="at most"):
        validate_question_text("ط" * 513)


def test_an_answer_is_normalized_or_refused_not_silently_accepted():
    assert normalize_answer("  بله  ") == (ANSWER, "")
    assert normalize_answer("") == (None, "answer_blank")
    assert normalize_answer(123) == (None, "answer_not_text")
    assert normalize_answer("ط" * 129) == (None, "answer_too_long")


def test_the_question_takes_exactly_one_bounded_argument():
    ok = [{"name": QUESTION_TOOL, "arguments": {"question": QUESTION}}]
    assert question_chain_error(ok) is None
    assert question_chain_error(
        [{"name": QUESTION_TOOL, "arguments": {"question": QUESTION, "chat_id": 5}}]
    ) is not None
    assert question_chain_error(
        [{"name": QUESTION_TOOL, "arguments": {"text": QUESTION}}]
    ) is not None
    # Since Phase 3E a chain MAY carry several questions — each is an
    # independent checkpoint, and at most one is ever active (the chain parks
    # at the first). The exact-argument rule still holds for EVERY question.
    assert question_chain_error([
        {"name": QUESTION_TOOL, "arguments": {"question": QUESTION}},
        {"name": QUESTION_TOOL, "arguments": {"question": "دومی؟"}},
    ]) is None
    assert question_chain_error([
        {"name": QUESTION_TOOL, "arguments": {"question": QUESTION}},
        {"name": QUESTION_TOOL, "arguments": {"question": "دومی؟", "chat_id": 5}},
    ]) == "action 2: 'ask_owner' takes exactly one bounded 'question' argument"


def test_no_wait_boundary_may_sit_before_the_question():
    wait_action = {
        "name": "send_message",
        "arguments": {"text": "x"},
        "not_before": "2026-09-26T13:00:00",
    }
    plain = [
        wait_action,
        {"name": QUESTION_TOOL, "arguments": {"question": QUESTION}},
    ]
    assert "wait boundary may not sit before the question" in question_chain_error(plain)

    # The same rule holds when the condition consumes the answer (the
    # supported conditional path): the wait check is not bypassed by it.
    conditional = [
        wait_action,
        {"name": QUESTION_TOOL, "arguments": {"question": QUESTION}},
        {
            "condition": {
                "source": {"action": 2, "field": ANSWER_FIELD},
                "operator": "equals",
                "value": ANSWER,
            }
        },
        {"name": "send_message", "arguments": {"text": "t"}, "branch": "true"},
        {"name": "send_message", "arguments": {"text": "f"}, "branch": "false"},
    ]
    assert "wait boundary may not sit before the question" in question_chain_error(conditional)

    # A wait AFTER the question is fine: the chain may pause on a clock later.
    after = [
        {"name": QUESTION_TOOL, "arguments": {"question": QUESTION}},
        {"name": "send_message", "arguments": {"text": "x"}, "not_before": "2027-01-01T09:00:00"},
    ]
    assert question_chain_error(after) is None


def test_a_question_may_live_inside_a_selected_branch_since_part_3e():
    """A branch question is always AFTER the chain's condition (never its
    source), and the non-selected branch never asks — so the Part 3D refusal
    became unnecessary in Phase 3E."""
    actions = [
        {"name": "web_search", "arguments": {"query": "x"}},
        {
            "condition": {
                "source": {"action": 1, "field": "is_closed"},
                "operator": "equals",
                "value": True,
            }
        },
        {"name": QUESTION_TOOL, "arguments": {"question": QUESTION}, "branch": "true"},
        {"name": "send_message", "arguments": {"text": "t"}, "branch": "true"},
        {"name": "send_message", "arguments": {"text": "f"}, "branch": "false"},
    ]
    assert question_chain_error(actions) is None


def test_a_condition_may_consume_the_answer():
    assert question_chain_error(_answer_condition_actions()) is None


def test_the_pending_question_record_is_bounded_and_validated():
    record = build_pending_question(
        action=1, chat_id=OWNER, message_id=1001,
        question_text=QUESTION, asked_at="2026-09-26T14:00:00+00:00",
    )
    assert record["action"] == 1
    assert record[QUESTION_CHAT_ID_KEY] == OWNER
    assert record[QUESTION_MESSAGE_ID_KEY] == 1001
    assert record[QUESTION_TEXT_KEY] == QUESTION
    assert record["answered"] is False
    with pytest.raises(Exception):
        build_pending_question(
            action=1, chat_id=0, message_id=1001,
            question_text=QUESTION, asked_at="2026-09-26T14:00:00+00:00",
        )
    with pytest.raises(Exception):
        build_pending_question(
            action=1, chat_id=OWNER, message_id=0,
            question_text=QUESTION, asked_at="2026-09-26T14:00:00+00:00",
        )


def test_correlation_refuses_every_non_exact_reply():
    record = build_pending_question(
        action=1, chat_id=OWNER, message_id=1001,
        question_text=QUESTION, asked_at="2026-09-26T14:00:00+00:00",
    )
    assert pending_question_correlation_error(
        record, owner_id=OWNER, chat_id=OWNER, reply_to_message_id=1001,
    ) is None
    # A reply to a DIFFERENT message is not an answer.
    assert pending_question_correlation_error(
        record, owner_id=OWNER, chat_id=OWNER, reply_to_message_id=1002,
    ) == "answer_not_correlated"
    # No reply target at all is not an answer.
    assert pending_question_correlation_error(
        record, owner_id=OWNER, chat_id=OWNER, reply_to_message_id=None,
    ) == "answer_not_correlated"
    # Another chat is not an answer.
    assert pending_question_correlation_error(
        record, owner_id=OWNER, chat_id=777, reply_to_message_id=1001,
    ) == "answer_chat_mismatch"
    # A non-owner answerer is unverified.
    assert pending_question_correlation_error(
        record, owner_id=0, chat_id=OWNER, reply_to_message_id=1001,
    ) == "answer_owner_unverified"
    # A malformed record fails closed.
    assert pending_question_correlation_error(
        {"nope": 1}, owner_id=OWNER, chat_id=OWNER, reply_to_message_id=1001,
    ) == "pending_question_invalid"


def test_the_answer_run_output_is_the_single_declared_field():
    assert answer_run_output(ANSWER) == {ANSWER_FIELD: ANSWER}


def test_the_candidate_schema_exposes_the_bounded_question_argument():
    from backend.ai.task_interpreter import CANDIDATE_SCHEMA

    question_schema = CANDIDATE_SCHEMA["properties"]["actions"]["items"][
        "properties"
    ]["question"]
    assert question_schema["type"] == "string"
    assert "ask_owner" in question_schema["description"]


# ── 2: execution — send, park, resume, consume downstream ───────────────────


@pytest.mark.asyncio
async def test_the_chain_sends_the_question_and_parks_on_waiting_answer():
    repo = InMemoryTaskRepository()
    calls = []
    telegram = _QuestionTelegram()
    registry = _question_registry(calls, telegram=telegram)
    task, occurrence = await _start(repo, _question_actions())
    coordinator, _ = _coordinator(repo, registry)

    result = await coordinator.execute(occurrence)

    assert result.status == WAITING_ANSWER_STATUS
    assert not result.success  # a park is not a completion
    assert _names(calls) == []  # the chain double never ran — the walk stopped
    assert telegram.sent == [(OWNER, QUESTION)]
    stored = await _stored(repo, task.id)
    assert stored.status == WAITING_ANSWER_STATUS
    assert stored.attempt == 1  # a question is not a failure: no attempt spent
    record = pending_question_from_metadata(stored.result_metadata)
    assert record is not None
    assert record["action"] == 1
    assert record[QUESTION_CHAT_ID_KEY] == OWNER
    assert record[QUESTION_MESSAGE_ID_KEY] == 1001
    assert record[QUESTION_TEXT_KEY] == QUESTION
    assert record["answered"] is False
    # The question run stays PENDING: QUESTION WAITING is never QUESTION SUCCESS.
    assert _run(stored, 1)["status"] == "pending"
    assert _run(stored, 1)["tool"] == QUESTION_TOOL
    assert _run(stored, 2)["status"] == "pending"
    # The row always carries the complete per-action state on both channels.
    assert stored.result_metadata["actions"] == stored.error_metadata["actions"]


@pytest.mark.asyncio
async def test_the_owner_s_correlated_reply_resumes_and_consumes_the_answer():
    repo = InMemoryTaskRepository()
    calls = []
    registry = _question_registry(calls)
    task, occurrence = await _start(repo, _question_actions())
    coordinator, _ = _coordinator(repo, registry)

    waiting = await coordinator.execute(occurrence)
    assert waiting.status == WAITING_ANSWER_STATUS
    parked = await _stored(repo, task.id)

    resolver = TaskAnswerResolver(repo, OWNER)
    assert await resolver.handle_event(_answer_event()) is True

    resumed = await _stored(repo, task.id)
    assert resumed.status == "retry_pending"
    assert resumed.retry_at is not None
    metadata = resumed.result_metadata
    assert metadata["pending_answer"] == ANSWER
    question_run = metadata["actions"][0]
    assert question_run["status"] == "succeeded"
    assert question_run["output"] == {ANSWER_FIELD: ANSWER}
    # ...and the scheduler's next wake finishes the chain through the SAME
    # coordinator: the send consumes the answer through the existing reference.
    assert await scheduler_wake_finishes(repo, task.id, calls, registry) is True
    assert calls[0]["arguments"]["text"] == ANSWER


async def scheduler_wake_finishes(repo, task_id, calls, registry):
    """One scheduler wake AFTER the resume instant; returns True on success.

    The reference is read from the stored row's own ``retry_at`` (plus one
    second) so the wake is always genuinely due, never dependent on the
    wall clock between the park write and this call.
    """
    stored = await _stored(repo, task_id)
    assert stored.status == "retry_pending" and stored.retry_at is not None
    coordinator, _ = _coordinator(repo, registry)
    scheduler = TaskScheduler(repo, OWNER, coordinator, outcome_notifier=None)
    assert await scheduler.run_once(now=stored.retry_at + timedelta(seconds=1)) == 1
    stored = await _stored(repo, task_id)
    return stored.status == "succeeded"


@pytest.mark.asyncio
async def test_a_later_action_consumes_the_answer_through_the_existing_reference():
    repo = InMemoryTaskRepository()
    calls = []
    registry = _question_registry(calls)
    service = TaskCreationService(repo, OWNER, tool_registry=registry)
    task = await service.create(
        _task_payload(_question_actions(), at="2026-09-26T14:00:00"),
        START - timedelta(minutes=30),
    )
    coordinator, _ = _coordinator(repo, registry)
    scheduler = TaskScheduler(repo, OWNER, coordinator, outcome_notifier=None)

    assert await scheduler.run_once(now=START) == 1
    parked = (await repo.list_occurrences(OWNER, task.id))[0]
    assert parked.status == WAITING_ANSWER_STATUS

    resolver = TaskAnswerResolver(repo, OWNER)
    assert await resolver.handle_event(_answer_event()) is True
    # The wake reference is read from the stored row's own retry_at (the real
    # wall clock the resume CAS stamped), never assumed.
    parked = (await repo.list_occurrences(OWNER, task.id))[0]
    assert parked.retry_at is not None
    assert await scheduler.run_once(now=parked.retry_at + timedelta(seconds=1)) == 1

    done = (await repo.list_occurrences(OWNER, task.id))[0]
    assert done.status == "succeeded"
    assert calls[0]["arguments"]["text"] == ANSWER
    assert _run(done, 1)["status"] == "succeeded"
    assert _run(done, 1)["output"] == {ANSWER_FIELD: ANSWER}
    assert _run(done, 2)["status"] == "succeeded"
    assert json.dumps(done.result_metadata, ensure_ascii=False)  # serializable


@pytest.mark.asyncio
async def test_a_condition_reads_the_answer_and_selects_exactly_one_branch():
    repo = InMemoryTaskRepository()
    calls = []
    registry = _question_registry(calls)
    service = TaskCreationService(repo, OWNER, tool_registry=registry)
    task = await service.create(
        _task_payload(_answer_condition_actions(), at="2026-09-26T14:00:00"),
        START - timedelta(minutes=30),
    )
    coordinator, _ = _coordinator(repo, registry)
    scheduler = TaskScheduler(repo, OWNER, coordinator, outcome_notifier=None)

    assert await scheduler.run_once(now=START) == 1
    parked = (await repo.list_occurrences(OWNER, task.id))[0]
    assert parked.status == WAITING_ANSWER_STATUS

    resolver = TaskAnswerResolver(repo, OWNER)
    assert await resolver.handle_event(_answer_event(ANSWER)) is True
    parked = (await repo.list_occurrences(OWNER, task.id))[0]
    assert parked.retry_at is not None
    assert await scheduler.run_once(now=parked.retry_at + timedelta(seconds=1)) == 1

    done = (await repo.list_occurrences(OWNER, task.id))[0]
    assert done.status == "succeeded"
    assert _names(calls) == ["send_message"]  # only the TRUE branch ran
    assert calls[0]["arguments"]["text"] == "بله بود"
    assert _run(done, 2)["output"] == {"matched": True, "selected_branch": "true"}
    assert _run(done, 4)["status"] == "skipped"


# ── 3: correlation failures — nothing is consumed, nothing resumes ──────────


@pytest.mark.asyncio
async def test_a_reply_to_a_different_message_is_not_an_answer():
    repo = InMemoryTaskRepository()
    registry = _question_registry([])
    task, occurrence = await _start(repo, _question_actions())
    coordinator, _ = _coordinator(repo, registry)
    await coordinator.execute(occurrence)

    resolver = TaskAnswerResolver(repo, OWNER)
    assert await resolver.handle_event(_answer_event(reply_to=9999)) is False
    stored = await _stored(repo, task.id)
    assert stored.status == WAITING_ANSWER_STATUS
    assert _run(stored, 1)["status"] == "pending"


@pytest.mark.asyncio
async def test_an_answer_from_another_owner_is_never_consumed():
    repo = InMemoryTaskRepository()
    registry = _question_registry([])
    task, occurrence = await _start(repo, _question_actions())
    coordinator, _ = _coordinator(repo, registry)
    await coordinator.execute(occurrence)

    resolver = TaskAnswerResolver(repo, OWNER)
    assert await resolver.handle_event(_answer_event(sender=OTHER_OWNER)) is False
    stored = await _stored(repo, task.id)
    assert stored.status == WAITING_ANSWER_STATUS


@pytest.mark.asyncio
async def test_an_answer_from_another_chat_is_not_consumed():
    repo = InMemoryTaskRepository()
    registry = _question_registry([])
    task, occurrence = await _start(repo, _question_actions())
    coordinator, _ = _coordinator(repo, registry)
    await coordinator.execute(occurrence)

    resolver = TaskAnswerResolver(repo, OWNER)
    assert await resolver.handle_event(_answer_event(chat_id=777)) is False
    stored = await _stored(repo, task.id)
    assert stored.status == WAITING_ANSWER_STATUS


@pytest.mark.asyncio
async def test_a_media_or_overlong_reply_is_refused_not_consumed():
    repo = InMemoryTaskRepository()
    registry = _question_registry([])
    task, occurrence = await _start(repo, _question_actions())
    coordinator, _ = _coordinator(repo, registry)
    await coordinator.execute(occurrence)

    resolver = TaskAnswerResolver(repo, OWNER)
    # An over-long text is a refusal: the question stays open.
    assert await resolver.handle_event(_answer_event("ط" * 129)) is False
    stored = await _stored(repo, task.id)
    assert stored.status == WAITING_ANSWER_STATUS
    # A blank answer is a refusal too.
    assert await resolver.handle_event(_answer_event("   ")) is False
    assert (await _stored(repo, task.id)).status == WAITING_ANSWER_STATUS


@pytest.mark.asyncio
async def test_a_plain_message_that_replies_to_nothing_is_ignored():
    repo = InMemoryTaskRepository()
    registry = _question_registry([])
    task, occurrence = await _start(repo, _question_actions())
    coordinator, _ = _coordinator(repo, registry)
    await coordinator.execute(occurrence)

    resolver = TaskAnswerResolver(repo, OWNER)
    assert await resolver.handle_event(_answer_event(reply_to=None)) is False
    assert (await _stored(repo, task.id)).status == WAITING_ANSWER_STATUS


# ── 4: durability — the resume CAS, restart safety, scheduler wake ──────────


@pytest.mark.asyncio
async def test_a_second_reply_cannot_resume_a_resumed_occurrence():
    repo = InMemoryTaskRepository()
    registry = _question_registry([])
    task, occurrence = await _start(repo, _question_actions())
    coordinator, _ = _coordinator(repo, registry)
    await coordinator.execute(occurrence)

    resolver = TaskAnswerResolver(repo, OWNER)
    assert await resolver.handle_event(_answer_event()) is True
    # The occurrence is now retry_pending: a duplicate reply finds nothing
    # parked and must not resume anything.
    assert await resolver.handle_event(_answer_event(message_id=2002)) is False
    stored = await _stored(repo, task.id)
    assert stored.status == "retry_pending"
    assert stored.result_metadata["pending_answer"] == ANSWER


@pytest.mark.asyncio
async def test_a_parked_question_survives_a_restart_and_resumes_without_re_asking():
    """The correlation identity lives in the occurrence's OWN durable metadata,
    so a fresh process (new resolver, fresh coordinator) resumes the SAME
    occurrence and never sends the question again."""
    repo = InMemoryTaskRepository()
    calls = []
    telegram = _QuestionTelegram()
    registry = _question_registry(calls, telegram=telegram)
    task, occurrence = await _start(repo, _question_actions())
    coordinator, _ = _coordinator(repo, registry)
    await coordinator.execute(occurrence)

    # "Restart": brand-new resolver + coordinator over the SAME repository.
    fresh_registry = _question_registry(calls, telegram=telegram)
    fresh_coordinator, _ = _coordinator(repo, fresh_registry)
    fresh_resolver = TaskAnswerResolver(repo, OWNER)
    assert await fresh_resolver.handle_event(_answer_event()) is True
    assert await scheduler_wake_finishes(repo, task.id, calls, fresh_registry) is True

    assert telegram.sent == [(OWNER, QUESTION)]  # asked exactly ONCE
    stored = await _stored(repo, task.id)
    assert stored.status == "succeeded"
    assert calls[0]["arguments"]["text"] == ANSWER


@pytest.mark.asyncio
async def test_recovery_leaves_a_parked_question_alone():
    repo = InMemoryTaskRepository()
    registry = _question_registry([])
    task, occurrence = await _start(repo, _question_actions())
    coordinator, _ = _coordinator(repo, registry)
    await coordinator.execute(occurrence)

    recoverable = await repo.list_recoverable_occurrences(OWNER)
    assert all(
        item.occurrence_key != occurrence.occurrence_key for item in recoverable
    )
    due = await repo.list_due_retry_occurrences(OWNER, NOW)
    assert all(
        item.occurrence_key != occurrence.occurrence_key for item in due
    )
    # ...and the claim CAS never accepts the parked status.
    assert await repo.claim_occurrence(OWNER, task.id, occurrence.occurrence_key) is None


@pytest.mark.asyncio
async def test_the_scheduler_wakes_a_resumed_occurrence_exactly_once():
    repo = InMemoryTaskRepository()
    calls = []
    registry = _question_registry(calls)
    task, occurrence = await _start(repo, _question_actions())
    coordinator, _ = _coordinator(repo, registry)
    await coordinator.execute(occurrence)

    resolver = TaskAnswerResolver(repo, OWNER)
    assert await resolver.handle_event(_answer_event()) is True

    stored = await _stored(repo, task.id)
    assert stored.status == "retry_pending" and stored.retry_at is not None
    coordinator2, _ = _coordinator(repo, registry)
    scheduler = TaskScheduler(repo, OWNER, coordinator2, outcome_notifier=None)
    now = stored.retry_at + timedelta(seconds=1)
    assert await scheduler.run_once(now=now) == 1
    assert await scheduler.run_once(now=now + timedelta(minutes=1)) == 0

    stored = await _stored(repo, task.id)
    assert stored.status == "succeeded"
    assert _names(calls) == ["send_message"]


@pytest.mark.asyncio
async def test_an_unsent_question_never_parks_and_retries_normally():
    """A Telegram send failure is an ordinary action failure: the occurrence
    retries through the existing bounded contract, nothing is parked."""
    repo = InMemoryTaskRepository()
    calls = []

    class _BrokenTelegram:
        async def send_message(self, chat_id, text, **kwargs):
            raise RuntimeError("transport down")

    context = ToolContext(_BrokenTelegram(), OWNER, "UTC")
    registry = ToolRegistry()
    from backend.ai.tools.question import AskOwnerTool

    registry.register(AskOwnerTool(context))
    registry.register(ChainTool("send_message", calls, data={"sent": True}))

    task, occurrence = await _start(repo, _question_actions(tail_ref=False))
    coordinator, _ = _coordinator(repo, registry)
    result = await coordinator.execute(occurrence)

    assert result.status == "failed"
    stored = await _stored(repo, task.id)
    assert stored.status != WAITING_ANSWER_STATUS
    # The failure record rides the error channel (the existing mid-chain
    # failure semantics); the result channel keeps the last running write.
    assert stored.error_metadata["actions"][0]["status"] == "failed"
    assert "Telegram send failed" in stored.error_metadata["actions"][0]["error"]


# ── 5: repository — the park validation and the resume CAS in both stores ───


@pytest.mark.asyncio
async def test_the_repository_requires_a_pending_question_for_the_parked_status():
    repo = InMemoryTaskRepository()
    task = await repo.create_task(OWNER, _task_payload(_question_actions()))
    with pytest.raises(ValueError, match="pending_question"):
        await repo.create_occurrence(OWNER, {
            "task_id": task.id,
            "occurrence_key": "k",
            "definition_version": task.version,
            "action_snapshot": _question_actions(),
            "scheduled_for": NOW,
            "status": WAITING_ANSWER_STATUS,
        })


@pytest.mark.asyncio
async def test_the_resume_cas_refuses_anything_but_the_parked_row():
    repo = InMemoryTaskRepository()
    task = await repo.create_task(OWNER, _task_payload(_question_actions()))
    record = build_pending_question(
        action=1, chat_id=OWNER, message_id=1001,
        question_text=QUESTION, asked_at="2026-09-26T14:00:00+00:00",
    )
    metadata = {
        PENDING_QUESTION_KEY: record,
        "actions": [
            {"position": 1, "tool": QUESTION_TOOL, "status": "pending"},
        ],
    }
    # No such occurrence: refused.
    assert await repo.resume_waiting_for_answer(
        OWNER, task.id, "missing", answer=ANSWER, answer_metadata=metadata,
    ) is None
    await repo.create_occurrence(OWNER, {
        "task_id": task.id,
        "occurrence_key": "k",
        "definition_version": task.version,
        "action_snapshot": _question_actions(),
        "scheduled_for": NOW,
    })
    # Still "claimed", not parked: refused.
    assert await repo.resume_waiting_for_answer(
        OWNER, task.id, "k", answer=ANSWER, answer_metadata=metadata,
    ) is None


@pytest.mark.asyncio
async def test_list_waiting_answer_returns_only_parked_rows_in_order():
    repo = InMemoryTaskRepository()
    registry = _question_registry([])
    coordinator, _ = _coordinator(repo, registry)
    keys = []
    for index in (1, 2):
        task, occurrence = await _start(repo, _question_actions(), key=f"k{index}")
        await coordinator.execute(occurrence)
        keys.append((task.id, occurrence.occurrence_key))
    parked = await repo.list_waiting_answer_occurrences(OWNER, 20)
    assert {(item.task_id, item.occurrence_key) for item in parked} == set(keys)
    # ...and the other owner's parked rows never leak.
    assert all(item.owner_id == OWNER for item in parked)


# ── 6: the answer event boundary — bounded context extraction ───────────────


def test_extract_answer_context_carries_only_deterministic_metadata():
    class _Msg:
        id = 55
        media = None
        message = "بله"

        class reply_to:
            reply_to_msg_id = 1001

    class _Event:
        chat_id = OWNER
        sender_id = OWNER
        raw_text = "بله"
        message = _Msg()

    context = extract_answer_context(_Event())
    assert context["chat_id"] == OWNER
    assert context["sender_id"] == OWNER
    assert context["message_id"] == 55
    assert context["reply_to_message_id"] == 1001
    assert context["text"] == "بله"
    assert context["has_media"] is False


def test_extract_answer_context_survives_a_media_reply():
    class _Msg:
        id = 56
        media = object()
        message = ""

    class _Event:
        chat_id = OWNER
        sender_id = OWNER
        raw_text = ""
        message = _Msg()

    context = extract_answer_context(_Event())
    assert context["has_media"] is True
    assert context["text"] == ""
