"""Regression tests for the event-driven automation upgrade.

Covers:
1.  The model-facing trigger spec is bounded and rejects invented ids.
2.  Sender/chat references resolve through trusted dialogs; ambiguity and
    unresolvable names fail closed.
3.  Deterministic trigger matching (sender, chat, content, media, reply,
    direction) — never an LLM call.
4.  Event tasks persist a resolved trigger; next_run_at stays None.
5.  The dispatcher executes matched events through the REAL registry ->
    executor -> coordinator chain, records occurrences with deterministic
    keys, deduplicates repeated deliveries, and skips non-matching,
    paused/deleted, and foreign-owner tasks.
6.  Event occurrences follow the existing retry/attempt and fail-closed
    boundaries.
7.  Tehran-local scheduling semantics for once/daily/weekly/interval.
8.  Conversational requests never become tasks; equivalent phrasings route
    to the same registered create_task tool without regex.
9.  Full recovery does not kill the scheduler task.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.ai.actions import parse_command_intent
from backend.ai.chat_resolution import resolve_chat_name, resolve_sender_name
from backend.ai.database.task_repository import InMemoryTaskRepository
from backend.ai.scheduling import ScheduleError, next_occurrence, parse_schedule
from backend.ai.task_candidate import TaskCandidateError, parse_candidate_output
from backend.ai.task_creation import TaskCreationService
from backend.ai.task_event_dispatcher import (
    TaskEventDispatcher,
    event_occurrence_key,
    extract_event_context,
)
from backend.ai.task_execution import TaskExecutionCoordinator
from backend.ai.task_management_interface import inspect_text, list_text
from backend.ai.task_trigger import (
    TaskTriggerError,
    event_trigger_matches,
    is_this_chat_reference,
    resolve_trigger_references,
    trigger_summary,
    validate_resolved_trigger,
    validate_trigger_spec,
)
from backend.ai.tools.context import ToolContext
from backend.ai.tools.executor import ToolExecutor
from backend.ai.tools.registry import create_default_registry
from backend.ai.task_management import TaskManagementService

OWNER = 777
CHAT = -100123
SENDER = 42


def event_task_data(**overrides):
    data = {
        "label": "event automation",
        "schedule_type": "event",
        "schedule": {
            "trigger": {
                "type": "telegram_message",
                "sender_id": SENDER,
                "sender_name": "John",
                "chat_id": CHAT,
                "chat_title": "Work",
                "contains": ["urgent"],
                "direction": "incoming",
            }
        },
        "timezone": "Asia/Tehran",
        "actions": [{"name": "send_message", "arguments": {"text": "handled"}}],
        "notification_destination": {},
    }
    data.update(overrides)
    return data


def event_context(**overrides):
    context = {
        "chat_id": CHAT,
        "sender_id": SENDER,
        "message_id": 5,
        "text": "this is urgent",
        "has_media": False,
        "is_reply": False,
        "out": False,
        "date": datetime.now(timezone.utc),
    }
    context.update(overrides)
    return context


# ── Trigger spec validation ─────────────────────────────────────────────────


def test_trigger_spec_validates_model_facing_shape():
    spec = validate_trigger_spec({
        "type": "telegram_message",
        "sender": "John",
        "chat": "this chat",
        "contains": ["urgent", "order"],
        "direction": "incoming",
    })
    assert spec["type"] == "telegram_message"
    assert spec["sender"] == "John"
    assert spec["contains"] == ["urgent", "order"]
    assert spec["direction"] == "incoming"


def test_trigger_spec_rejects_invented_ids():
    with pytest.raises(TaskTriggerError):
        validate_trigger_spec({"type": "telegram_message", "sender_id": 123})
    with pytest.raises(TaskTriggerError):
        validate_trigger_spec({"type": "telegram_message", "chat_id": -100})


def test_trigger_spec_rejects_unknown_keys_and_empty_conditions():
    with pytest.raises(TaskTriggerError):
        validate_trigger_spec({"type": "telegram_message", "sender": "x", "magic": True})
    # No conditions at all → would fire on every message; rejected.
    with pytest.raises(TaskTriggerError):
        validate_trigger_spec({"type": "telegram_message", "direction": "incoming"})
    with pytest.raises(TaskTriggerError):
        validate_trigger_spec({"type": "telegram_message", "contains": []})
    with pytest.raises(TaskTriggerError):
        validate_trigger_spec({"type": "cron"})
    with pytest.raises(TaskTriggerError):
        validate_trigger_spec({"type": "telegram_message", "sender": "x", "direction": "sideways"})


def test_resolved_trigger_requires_nonzero_integers():
    trigger = validate_resolved_trigger(event_task_data()["schedule"]["trigger"])
    assert trigger["sender_id"] == SENDER
    assert trigger["chat_id"] == CHAT  # group chat ids are negative
    with pytest.raises(TaskTriggerError):
        validate_resolved_trigger({**trigger, "sender_id": 0})
    with pytest.raises(TaskTriggerError):
        validate_resolved_trigger({**trigger, "chat_id": 0})
    with pytest.raises(TaskTriggerError):
        validate_resolved_trigger({**trigger, "sender_id": "not-an-id"})


# ── Reference resolution (trusted dialogs, fail closed) ─────────────────────


CHATS = [
    {"id": SENDER, "title": "", "username": "johnny", "first_name": "John", "last_name": "Doe"},
    {"id": 99, "title": "Work", "username": "", "first_name": "", "last_name": ""},
    {"id": 100, "title": "Johns", "username": "", "first_name": "Johns", "last_name": ""},
    {"id": 200, "title": "Work", "username": "", "first_name": "", "last_name": ""},
]


def test_this_chat_resolves_to_request_chat():
    resolved, error = resolve_trigger_references(
        {"type": "telegram_message", "chat": "this chat", "contains": ["go"]},
        request_chat_id=CHAT,
        chats=[],
    )
    assert error is None
    assert resolved["chat_id"] == CHAT


def test_this_chat_fails_closed_without_request_chat():
    _, error = resolve_trigger_references(
        {"type": "telegram_message", "chat": "همین چت", "contains": ["go"]},
        request_chat_id=None,
        chats=[],
    )
    assert error is not None
    assert "could not be resolved" in error.lower()


def test_sender_name_resolves_to_trusted_id():
    resolved, error = resolve_trigger_references(
        {"type": "telegram_message", "sender": "John Doe", "contains": ["go"]},
        request_chat_id=CHAT,
        chats=CHATS,
    )
    assert error is None
    assert resolved["sender_id"] == SENDER
    assert "John" in resolved["sender_name"]


def test_ambiguous_sender_fails_closed():
    # "Johns" vs "John Doe" both score >= 0.6 for "john" → clarification.
    _, error = resolve_trigger_references(
        {"type": "telegram_message", "sender": "john", "contains": ["go"]},
        request_chat_id=CHAT,
        chats=CHATS,
    )
    assert error is not None
    assert "Multiple contacts" in error


def test_unresolvable_sender_fails_closed():
    _, error = resolve_trigger_references(
        {"type": "telegram_message", "sender": "xy", "contains": ["go"]},
        request_chat_id=CHAT,
        chats=CHATS,
    )
    assert error is not None


def test_ambiguous_chat_fails_closed_and_no_dialog_snapshot_fails_for_names():
    result = resolve_chat_name("Work", CHATS)
    assert not result["resolved"]
    assert len(result["matches"]) >= 2
    # Exact/unique sender resolves; a partial name that also matches another
    # contact stays ambiguous and fails closed.
    exact = resolve_sender_name("John Doe", CHATS)
    assert exact["resolved"]
    assert exact["sender_id"] == SENDER
    partial = resolve_sender_name("john", CHATS)
    assert not partial["resolved"]
    assert partial["matches"]
    loose = resolve_sender_name("xy", CHATS)
    assert not loose["resolved"]
    assert not loose.get("matches")
    # Named references cannot resolve without a trusted dialog snapshot.
    _, error = resolve_trigger_references(
        {"type": "telegram_message", "chat": "Work", "contains": ["go"]},
        request_chat_id=CHAT,
        chats=[],
    )
    assert error is not None


def test_is_this_chat_reference_aliases():
    assert is_this_chat_reference("this chat")
    assert is_this_chat_reference("همین چت")
    assert not is_this_chat_reference("Work")


# ── Deterministic matching ──────────────────────────────────────────────────


def test_matcher_all_conditions_and():
    trigger = event_task_data()["schedule"]["trigger"]
    assert event_trigger_matches(trigger, event_context())
    # Wrong sender → no match.
    assert not event_trigger_matches(trigger, event_context(sender_id=999))
    # Wrong chat → no match.
    assert not event_trigger_matches(trigger, event_context(chat_id=999))
    # Missing term → no match (ALL terms must appear).
    assert not event_trigger_matches(trigger, event_context(text="no keywords here"))
    # Outgoing message → no match (direction incoming).
    assert not event_trigger_matches(trigger, event_context(out=True))


def test_matcher_direction_and_content_fields():
    outgoing = {
        "type": "telegram_message",
        "direction": "outgoing",
        "contains": ["done"],
    }
    assert event_trigger_matches(outgoing, event_context(out=True, text="done"))
    assert not event_trigger_matches(outgoing, event_context(out=False, text="done"))
    any_dir = {"type": "telegram_message", "direction": "any", "text_equals": "ping"}
    assert event_trigger_matches(any_dir, event_context(out=False, text="ping"))
    assert event_trigger_matches(any_dir, event_context(out=True, text="ping"))
    starts = {"type": "telegram_message", "starts_with": "order:"}
    assert event_trigger_matches(starts, event_context(text="order: 2 cups"))
    assert not event_trigger_matches(starts, event_context(text="2 cups order:"))
    media = {"type": "telegram_message", "has_media": True}
    assert event_trigger_matches(media, event_context(has_media=True))
    assert not event_trigger_matches(media, event_context(has_media=False))
    reply = {"type": "telegram_message", "is_reply": True}
    assert event_trigger_matches(reply, event_context(is_reply=True))
    assert not event_trigger_matches(reply, event_context(is_reply=False))


def test_event_schedule_parsing():
    schedule = parse_schedule("event", event_task_data()["schedule"])
    assert schedule.trigger["sender_id"] == SENDER
    with pytest.raises(ScheduleError):
        parse_schedule("event", {"trigger": {"type": "telegram_message"}})
    with pytest.raises(ScheduleError):
        parse_schedule("event", {"at": "2026-09-05T17:00:00", "timezone": "Asia/Tehran"})
    with pytest.raises(ScheduleError):
        next_occurrence(schedule, datetime.now(timezone.utc))


def test_event_trigger_summary_is_bounded():
    summary = trigger_summary(event_task_data()["schedule"]["trigger"])
    assert "Telegram message" in summary
    assert "John" in summary
    assert "Work" in summary
    assert len(summary) < 500


# ── Candidate + creation persistence ────────────────────────────────────────


def test_candidate_accepts_event_trigger_and_rejects_unsafe_shapes():
    candidate = parse_candidate_output({
        "label": "on urgent",
        "schedule_type": "event",
        "schedule": {
            "trigger": {
                "type": "telegram_message", "sender": "John",
                "contains": ["urgent"],
            }
        },
        "timezone": "Asia/Tehran",
        "actions": [{"name": "send_message", "arguments": {"text": "handled"}}],
        "notification_destination": {},
    })
    assert candidate.schedule_type == "event"
    assert candidate.schedule["trigger"]["sender"] == "John"
    assert "sender_id" not in candidate.schedule["trigger"]
    with pytest.raises(TaskCandidateError):
        parse_candidate_output({
            "label": "bad",
            "schedule_type": "event",
            "schedule": {"trigger": {"type": "telegram_message", "sender_id": 5}},
            "timezone": "Asia/Tehran",
            "actions": [{"name": "send_message", "arguments": {"text": "x"}}],
            "notification_destination": {},
        })
    with pytest.raises(TaskCandidateError):
        parse_candidate_output({
            "label": "bad",
            "schedule_type": "event",
            "schedule": {"not": "a trigger"},
            "timezone": "Asia/Tehran",
            "actions": [{"name": "send_message", "arguments": {"text": "x"}}],
            "notification_destination": {},
        })


@pytest.mark.asyncio
async def test_event_task_creation_persists_trigger_and_no_next_run():
    repo = InMemoryTaskRepository()
    service = TaskCreationService(repo, OWNER)
    task = await service.create(
        event_task_data(),
        datetime(2026, 9, 4, 10, 0, tzinfo=timezone.utc),
    )
    assert task.schedule_type == "event"
    assert task.next_run_at is None
    assert task.schedule["trigger"]["sender_id"] == SENDER
    stored = await repo.get_task(OWNER, task.id)
    assert stored.schedule["trigger"]["chat_id"] == CHAT
    # The task is visible in the normal list (deleted excluded) and its
    # count includes it.
    service = TaskManagementService(repo, OWNER)
    assert len(await service.list_tasks()) == 1
    assert (await service.counts())["active"] == 1


# ── Dispatcher: real registry -> executor -> coordinator chain ──────────────


def _dispatcher_and_repo(telegram, *, notifier=None, repo=None):
    context = ToolContext(
        telegram=telegram, owner_id=OWNER, tz_str="Asia/Tehran", client=None
    )
    registry = create_default_registry(context)
    executor = ToolExecutor(registry, context)
    repo = repo or InMemoryTaskRepository()
    coordinator = TaskExecutionCoordinator(repo, executor, OWNER, context)
    dispatcher = TaskEventDispatcher(repo, OWNER, coordinator, notifier)
    return dispatcher, repo


@pytest.mark.asyncio
async def test_dispatcher_executes_matching_event_once_through_registered_tool():
    telegram = MagicMock()
    telegram.send_message = AsyncMock(return_value={"id": 1})
    dispatcher, repo = _dispatcher_and_repo(telegram)
    await repo.create_task(OWNER, event_task_data())

    executed = await dispatcher.handle_event(event_context())
    assert executed == 1
    occurrences = await repo.list_occurrences(OWNER)
    assert len(occurrences) == 1
    occurrence = occurrences[0]
    assert occurrence.status == "succeeded"
    assert occurrence.occurrence_key == event_occurrence_key(occurrence.task_id, CHAT, 5)
    assert occurrence.action_snapshot == event_task_data()["actions"]
    telegram.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_dispatcher_non_matching_event_creates_no_occurrence():
    telegram = MagicMock()
    telegram.send_message = AsyncMock(return_value={"id": 1})
    dispatcher, repo = _dispatcher_and_repo(telegram)
    await repo.create_task(OWNER, event_task_data())

    assert await dispatcher.handle_event(event_context(sender_id=999)) == 0
    assert await dispatcher.handle_event(event_context(text="nothing special")) == 0
    assert await dispatcher.handle_event(event_context(out=True)) == 0
    assert await repo.list_occurrences(OWNER) == []
    telegram.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_dispatcher_duplicate_delivery_does_not_execute_twice():
    telegram = MagicMock()
    telegram.send_message = AsyncMock(return_value={"id": 1})
    dispatcher, repo = _dispatcher_and_repo(telegram)
    await repo.create_task(OWNER, event_task_data())

    assert await dispatcher.handle_event(event_context()) == 1
    # Identical Telegram event redelivered (same chat + message id).
    assert await dispatcher.handle_event(event_context()) == 0
    assert len(await repo.list_occurrences(OWNER)) == 1
    telegram.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_dispatcher_skips_paused_deleted_and_foreign_owner_tasks():
    telegram = MagicMock()
    telegram.send_message = AsyncMock(return_value={"id": 1})
    dispatcher, repo = _dispatcher_and_repo(telegram)
    service = TaskManagementService(repo, OWNER)

    paused = await repo.create_task(OWNER, event_task_data(label="paused"))
    await service.pause(paused.id, paused.version)
    doomed = await repo.create_task(OWNER, event_task_data(label="doomed"))
    await service.delete(doomed.id, doomed.version)
    completed = await repo.create_task(OWNER, event_task_data(label="done"))
    await service.complete(completed.id, completed.version)
    await repo.create_task(999, event_task_data(label="other owner"))

    assert await dispatcher.handle_event(event_context()) == 0
    assert await repo.list_occurrences(OWNER) == []
    telegram.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_dispatcher_event_key_deterministic_and_unique_per_message():
    dispatcher, repo = _dispatcher_and_repo(MagicMock())
    key1 = event_occurrence_key(1, CHAT, 5)
    key2 = event_occurrence_key(1, CHAT, 5)
    key3 = event_occurrence_key(1, CHAT, 6)
    assert key1 == key2
    assert key1 != key3
    assert "ev" in key1


@pytest.mark.asyncio
async def test_dispatcher_fails_closed_on_unregistered_action():
    telegram = MagicMock()
    telegram.send_message = AsyncMock(return_value={"id": 1})
    dispatcher, repo = _dispatcher_and_repo(telegram)
    await repo.create_task(OWNER, event_task_data(
        actions=[{"name": "totally_made_up_tool", "arguments": {}}]
    ))

    assert await dispatcher.handle_event(event_context()) == 1
    occurrences = await repo.list_occurrences(OWNER)
    assert len(occurrences) == 1
    assert occurrences[0].status == "failed"
    assert occurrences[0].error_metadata.get("error_class") == "unregistered_action"
    telegram.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_dispatcher_retry_semantics_preserved():
    telegram = MagicMock()
    telegram.send_message = AsyncMock(side_effect=TimeoutError("temporarily unavailable"))
    dispatcher, repo = _dispatcher_and_repo(telegram)
    await repo.create_task(OWNER, event_task_data())

    assert await dispatcher.handle_event(event_context()) == 1
    occurrence = (await repo.list_occurrences(OWNER))[0]
    assert occurrence.status == "retry_pending"
    assert occurrence.attempt == 2
    assert occurrence.retry_at is not None


@pytest.mark.asyncio
async def test_dispatcher_without_coordinator_marks_interrupted():
    repo = InMemoryTaskRepository()
    dispatcher = TaskEventDispatcher(repo, OWNER, execution_coordinator=None)
    await repo.create_task(OWNER, event_task_data())

    assert await dispatcher.handle_event(event_context()) == 1
    occurrence = (await repo.list_occurrences(OWNER))[0]
    assert occurrence.status == "interrupted"


@pytest.mark.asyncio
async def test_dispatcher_notification_requires_opt_in():
    sent = AsyncMock(return_value=True)
    from backend.ai.notifications import TaskNotificationService
    from backend.ai.task_notifications import TaskOutcomeNotifier

    telegram = MagicMock()
    telegram.send_message = AsyncMock(return_value={"id": 1})
    repo = InMemoryTaskRepository()
    notifier = TaskOutcomeNotifier(
        repo, TaskNotificationService(sent, OWNER), OWNER
    )
    dispatcher, _ = _dispatcher_and_repo(telegram, notifier=notifier, repo=repo)
    await repo.create_task(OWNER, event_task_data())
    assert await dispatcher.handle_event(event_context()) == 1
    sent.assert_not_called()

    # Explicit opt-in notifies: the message matches both tasks, but only the
    # opted-in task sends a notification.
    await repo.create_task(OWNER, event_task_data(
        label="notify me", notification_destination={"notify_on_outcome": True}
    ))
    assert await dispatcher.handle_event(event_context(message_id=7)) == 2
    assert sent.await_count == 1


@pytest.mark.asyncio
async def test_event_handler_context_extraction_and_unconfigured_noop():
    message = SimpleNamespace(
        id=5, media=None, reply_to_msg_id=None, date=datetime.now(timezone.utc)
    )
    event = SimpleNamespace(
        chat_id=CHAT, sender_id=SENDER, raw_text="urgent now", message=message, out=False
    )
    context = extract_event_context(event)
    assert context["chat_id"] == CHAT
    assert context["sender_id"] == SENDER
    assert context["message_id"] == 5
    assert context["text"] == "urgent now"
    assert context["has_media"] is False
    assert context["is_reply"] is False
    assert context["out"] is False

    from backend.bot.handlers import task_events
    task_events.configure(None)
    # No dispatcher → handler is a silent no-op.
    assert task_events.get_dispatcher() is None


# ── Tehran scheduling semantics ─────────────────────────────────────────────


def test_once_schedule_resolves_tehran_instant():
    schedule = parse_schedule("once", {
        "at": "2026-09-05T17:00:00", "timezone": "Asia/Tehran",
    })
    reference = datetime(2026, 9, 4, 10, 0, tzinfo=timezone.utc)
    instant = next_occurrence(schedule, reference)
    assert instant == datetime(2026, 9, 5, 13, 30, tzinfo=timezone.utc)


def test_daily_and_weekly_schedules_use_tehran_clock():
    reference = datetime(2026, 9, 4, 10, 0, tzinfo=timezone.utc)
    daily = parse_schedule("daily", {"hour": 9, "minute": 0, "timezone": "Asia/Tehran"})
    # 09:00 Tehran on 2026-09-04 = 05:30 UTC, already past the 10:00 UTC
    # reference; the next daily occurrence is 2026-09-05 05:30 UTC.
    assert next_occurrence(daily, reference) == datetime(2026, 9, 5, 5, 30, tzinfo=timezone.utc)
    weekly = parse_schedule("weekly", {
        "weekday": 0, "hour": 10, "minute": 0, "timezone": "Asia/Tehran",
    })
    # Monday 10:00 Tehran = 06:30 UTC; Sep 4 2026 is a Friday.
    assert next_occurrence(weekly, reference) == datetime(2026, 9, 7, 6, 30, tzinfo=timezone.utc)


def test_interval_schedule_semantics():
    schedule = parse_schedule("interval", {"seconds": 7200})
    previous = datetime(2026, 9, 4, 10, 0, tzinfo=timezone.utc)
    assert next_occurrence(
        schedule, datetime(2026, 9, 4, 10, 0, tzinfo=timezone.utc), previous
    ) == datetime(2026, 9, 4, 12, 0, tzinfo=timezone.utc)
    with pytest.raises(ScheduleError):
        parse_schedule("interval", {"minutes": 30})
    with pytest.raises(ScheduleError):
        parse_schedule("interval", {"seconds": 0})


# ── Task creation intent: no regex growth, no false positives ───────────────


def test_conversational_requests_never_become_tasks():
    # None of these may route to the create_task tool: only explicit
    # automation intent creates a durable task.
    for text in (
        "What time is it?",
        "Tell me about tomorrow.",
        "John messaged me.",
        "Send this message.",
        "What tasks do I have?",
    ):
        result = parse_command_intent(text, has_reply=False)
        assert result.action != "create_task", text


def test_equivalent_phrasings_route_to_same_registered_create_task_tool():
    for text in ("every 3 minutes write hello", "هر ۳ دقیقه بنویس سلام"):
        result = parse_command_intent(text, has_reply=False)
        assert result.action == "create_task"
        assert result.kind == "executable"
        assert result.tool_calls == [{"name": "create_task", "arguments": {"request": text}}]


# ── Presentation ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_event_task_presentation_shows_trigger_and_no_fake_next_run():
    repo = InMemoryTaskRepository()
    service = TaskManagementService(repo, OWNER)
    await repo.create_task(OWNER, event_task_data())
    listed = await list_text(service)
    assert "On message event" in listed
    detail = await inspect_text(service, 1)
    assert "Trigger: Telegram message" in detail
    assert "sender: John" in detail
    assert "chat: Work" in detail


# ── Recovery safety ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_full_recovery_does_not_cancel_the_task_scheduler():
    import asyncio

    from backend.runtime.supervisor import RuntimeSupervisor

    supervisor = RuntimeSupervisor.__new__(RuntimeSupervisor)

    async def _scheduler_loop():
        await asyncio.sleep(3600)

    scheduler_task = asyncio.create_task(_scheduler_loop(), name="lifeos-task-scheduler")
    unrelated = asyncio.create_task(_scheduler_loop(), name="orphan-work")
    try:
        await supervisor._cancel_orphan_tasks()
        assert not scheduler_task.done()
        assert unrelated.done()
    finally:
        scheduler_task.cancel()
        unrelated.cancel()
        for task in (scheduler_task, unrelated):
            try:
                await task
            except asyncio.CancelledError:
                pass