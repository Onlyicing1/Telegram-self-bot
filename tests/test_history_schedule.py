"""Phase 4/5 boundary: owner History schedule creation and bounded execution."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from backend.ai.database.task_repository import (
    DELETION_DELETED,
    DELETION_NOT_FOUND,
    DELETION_STALE,
    InMemoryTaskRepository,
    TaskDeletionResult,
)
from backend.ai.task_candidate import TaskCandidate
from backend.ai.task_contract import (
    OWNER_ONLY_TASK_ACTIONS,
    SCHEDULED_HISTORY_ACTION,
    SCHEDULED_OCCURRENCE_EXTRA,
    SCHEDULED_TASK_ID_EXTRA,
    scheduled_occurrence_provenance,
)
from backend.ai.task_creation import (
    HistoryScheduleCreationError,
    TaskCreationError,
    TaskCreationService,
)
from backend.ai.task_execution import TaskExecutionCoordinator
from backend.ai.task_scheduler import occurrence_key
from backend.ai.tools.base import PermissionLevel, ToolResult
from backend.ai.tools.context import ToolContext
from backend.ai.tools.executor import ToolExecutor
from backend.ai.tools.history_schedule import ScheduledHistoryTool
from backend.ai.tools.registry import ToolRegistry, create_default_registry
from backend.services import history_ai_service, history_schedule_service, history_service
from backend.services.history_service import HistoryBatch, HistoryMessage
from backend.telegram_api.peers import PeerType

OWNER = 7001
PEER = -1007001
REF = datetime(2026, 11, 14, 12, 0, tzinfo=timezone.utc)
VALID_SCHEDULED_KEY = occurrence_key(77, REF)


def _message(message_id: int, text: str = "hello") -> HistoryMessage:
    return HistoryMessage(
        message_id=message_id,
        chat_id=PEER,
        sender_id=1,
        out=False,
        date=REF,
        text=text,
        reply_to_msg_id=None,
        has_media=False,
        ai_provenance=False,
    )


def _candidate() -> dict:
    return {
        "label": "History checkpoint",
        "schedule_type": "interval",
        "schedule": {"seconds": 3600},
        "timezone": "UTC",
        "actions": [{"name": SCHEDULED_HISTORY_ACTION, "arguments": {}}],
        "notification_destination": {},
    }


def _validated() -> TaskCandidate:
    return TaskCandidate.from_untrusted(_candidate())


async def _task_with_state(repo: InMemoryTaskRepository, *, baseline: int = 10):
    task = await repo.create_task(OWNER, _candidate())
    state = await repo.create_history_schedule(
        OWNER, task.id, PEER, baseline_cursor=baseline,
    )
    return task, state


def test_the_scheduled_history_action_is_registered_once():
    registry = create_default_registry(ToolContext(None, OWNER, "UTC"))
    assert registry.has(SCHEDULED_HISTORY_ACTION)
    assert isinstance(registry.get(SCHEDULED_HISTORY_ACTION), ScheduledHistoryTool)


@pytest.mark.asyncio
async def test_untrusted_creation_rejects_the_owner_only_action():
    repo = InMemoryTaskRepository()
    service = TaskCreationService(repo, OWNER)

    with pytest.raises(TaskCreationError, match="owner-wizard-only"):
        await service.create(_validated(), REF)
    assert await repo.list_tasks(OWNER) == []

    task = await service.create(
        _validated(), REF, trusted_owner_actions=OWNER_ONLY_TASK_ACTIONS,
    )
    assert task.actions[0]["name"] == SCHEDULED_HISTORY_ACTION


@pytest.mark.asyncio
async def test_owner_creation_writes_the_task_and_one_state_row():
    repo = InMemoryTaskRepository()
    service = TaskCreationService(repo, OWNER)

    task, state = await service.create_history_schedule_task(
        _validated(), REF, peer_id=PEER, baseline_cursor=55,
    )

    assert task.id > 0
    assert task.actions == [{"name": SCHEDULED_HISTORY_ACTION, "arguments": {}}]
    assert state.task_id == task.id
    assert state.peer_id == PEER
    assert state.baseline_cursor == state.scanned_cursor == state.summarized_cursor == 55
    assert (await repo.get_history_schedule_by_peer(OWNER, PEER)).task_id == task.id


@pytest.mark.asyncio
async def test_a_live_owner_peer_state_is_refused_before_a_second_task():
    repo = InMemoryTaskRepository()
    service = TaskCreationService(repo, OWNER)
    await service.create_history_schedule_task(
        _validated(), REF, peer_id=PEER, baseline_cursor=1,
    )

    with pytest.raises(HistoryScheduleCreationError, match="already has"):
        await service.create_history_schedule_task(
            _validated(), REF, peer_id=PEER, baseline_cursor=2,
        )
    assert len(await repo.list_tasks(OWNER)) == 1


@pytest.mark.asyncio
async def test_a_fallback_task_is_removed_and_never_gets_state(monkeypatch):
    repo = InMemoryTaskRepository()
    service = TaskCreationService(repo, OWNER)
    original_create = repo.create_task

    async def _fallback_create(owner_id, data):
        task = await original_create(owner_id, data)
        task.fallback_backend = "InMemoryTaskRepository"
        return task

    monkeypatch.setattr(repo, "create_task", _fallback_create)
    with pytest.raises(HistoryScheduleCreationError) as raised:
        await service.create_history_schedule_task(
            _validated(), REF, peer_id=PEER, baseline_cursor=1,
        )
    assert raised.value.rollback_durable is True
    assert await repo.list_tasks(OWNER) == []
    assert await repo.get_history_schedule_by_peer(OWNER, PEER) is None


@pytest.mark.asyncio
async def test_state_failure_compensates_the_exact_created_task(monkeypatch):
    repo = InMemoryTaskRepository()
    service = TaskCreationService(repo, OWNER)

    async def _fail(*args, **kwargs):
        raise RuntimeError("state write failed")

    monkeypatch.setattr(repo, "create_history_schedule", _fail)
    with pytest.raises(HistoryScheduleCreationError) as raised:
        await service.create_history_schedule_task(
            _validated(), REF, peer_id=PEER, baseline_cursor=1,
        )
    assert raised.value.rollback_durable is True
    assert raised.value.task_id == 0
    assert await repo.list_tasks(OWNER) == []
    assert await repo.get_history_schedule_by_peer(OWNER, PEER) is None


@pytest.mark.asyncio
async def test_compensation_deletes_the_exact_created_task_version(monkeypatch):
    repo = InMemoryTaskRepository()
    service = TaskCreationService(repo, OWNER)
    created: dict[str, int] = {}
    original_create = repo.create_task

    async def _capture_create(owner_id, data):
        task = await original_create(owner_id, data)
        created.update(task_id=task.id, version=task.version)
        return task

    async def _fail_state(*args, **kwargs):
        raise RuntimeError("state write failed")

    deletes: list[tuple[int, int]] = []
    original_delete = repo.delete_task

    async def _capture_delete(owner_id, task_id, expected_version):
        deletes.append((task_id, expected_version))
        return await original_delete(owner_id, task_id, expected_version)

    monkeypatch.setattr(repo, "create_task", _capture_create)
    monkeypatch.setattr(repo, "create_history_schedule", _fail_state)
    monkeypatch.setattr(repo, "delete_task", _capture_delete)

    with pytest.raises(HistoryScheduleCreationError) as raised:
        await service.create_history_schedule_task(
            _validated(), REF, peer_id=PEER, baseline_cursor=1,
        )

    assert raised.value.rollback_durable is True
    assert raised.value.task_id == 0
    assert deletes == [(created["task_id"], created["version"])]
    assert await repo.list_tasks(OWNER) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", [DELETION_STALE, DELETION_NOT_FOUND, DELETION_DELETED])
async def test_compensation_must_prove_a_durable_deletion(monkeypatch, outcome):
    repo = InMemoryTaskRepository()
    service = TaskCreationService(repo, OWNER)
    created: dict[str, int] = {}
    original_create = repo.create_task

    async def _capture_create(owner_id, data):
        task = await original_create(owner_id, data)
        created.update(task_id=task.id, version=task.version)
        return task

    async def _fail_state(*args, **kwargs):
        raise RuntimeError("state write failed")

    async def _compensate(owner_id, task_id, expected_version):
        if outcome == DELETION_DELETED:
            # A deletion that happened in the degraded store is not durable.
            return TaskDeletionResult(
                DELETION_DELETED, task_id,
                fallback_backend="InMemoryTaskRepository",
            )
        return TaskDeletionResult(outcome, task_id)

    monkeypatch.setattr(repo, "create_task", _capture_create)
    monkeypatch.setattr(repo, "create_history_schedule", _fail_state)
    monkeypatch.setattr(repo, "delete_task", _compensate)

    with pytest.raises(HistoryScheduleCreationError) as raised:
        await service.create_history_schedule_task(
            _validated(), REF, peer_id=PEER, baseline_cursor=1,
        )

    assert raised.value.rollback_durable is False
    assert raised.value.task_id == created["task_id"]


@pytest.mark.asyncio
async def test_cancellation_at_the_state_boundary_compensates_and_propagates(monkeypatch):
    repo = InMemoryTaskRepository()
    service = TaskCreationService(repo, OWNER)

    async def _cancel_state(*args, **kwargs):
        raise asyncio.CancelledError

    monkeypatch.setattr(repo, "create_history_schedule", _cancel_state)

    with pytest.raises(asyncio.CancelledError):
        await service.create_history_schedule_task(
            _validated(), REF, peer_id=PEER, baseline_cursor=1,
        )

    assert await repo.list_tasks(OWNER) == []
    assert await repo.get_history_schedule_by_peer(OWNER, PEER) is None


@pytest.mark.asyncio
async def test_cancellation_during_compensation_is_not_swallowed(monkeypatch):
    repo = InMemoryTaskRepository()
    service = TaskCreationService(repo, OWNER)
    started = asyncio.Event()
    original_delete = repo.delete_task

    async def _fail_state(*args, **kwargs):
        raise RuntimeError("state write failed")

    async def _slow_delete(owner_id, task_id, expected_version):
        started.set()
        await asyncio.sleep(0.05)
        return await original_delete(owner_id, task_id, expected_version)

    monkeypatch.setattr(repo, "create_history_schedule", _fail_state)
    monkeypatch.setattr(repo, "delete_task", _slow_delete)

    running = asyncio.create_task(
        service.create_history_schedule_task(
            _validated(), REF, peer_id=PEER, baseline_cursor=1,
        )
    )
    await asyncio.wait_for(started.wait(), timeout=1.0)
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running

    # The shield lets the already-started bounded compensation finish; it does
    # not start an unbounded background worker.
    await asyncio.sleep(0.1)
    assert await repo.list_tasks(OWNER) == []


@pytest.mark.asyncio
async def test_one_run_extends_the_summary_and_commits_both_cursors(monkeypatch):
    repo = InMemoryTaskRepository()
    task, _state = await _task_with_state(repo, baseline=10)
    calls: list[dict] = []

    async def _classify(*args, **kwargs):
        return PeerType.PRIVATE_USER

    async def _latest(*args, **kwargs):
        return 13

    async def _fetch(*args, **kwargs):
        calls.append(dict(kwargs))
        return HistoryBatch(
            chat_id=PEER,
            messages=(_message(11), _message(12, "newer")),
            scanned=2,
            next_cursor=13,
            has_more=False,
        )

    async def _summarize(messages, **kwargs):
        calls.append({"summary_messages": [m.message_id for m in messages], **kwargs})
        return True, "cumulative summary", {"processed": 2}

    monkeypatch.setattr(history_service, "classify_chat", _classify)
    monkeypatch.setattr(history_service, "latest_message_id", _latest)
    monkeypatch.setattr(history_service, "fetch_oldest_history", _fetch)
    monkeypatch.setattr(
        history_schedule_service.history_ai_service,
        "summarize_incremental",
        _summarize,
    )

    ok, message, data = await history_schedule_service.run_scheduled_history(
        object(), owner_id=OWNER, task_id=task.id, repository=repo,
    )

    assert ok is True
    assert message == "cumulative summary"
    state = await repo.get_history_schedule(OWNER, task.id)
    assert state.summary == "cumulative summary"
    assert state.scanned_cursor == 13
    assert state.summarized_cursor == 12
    assert state.version == 2 and state.revision == 1
    assert calls[0]["after_cursor"] == 10
    assert calls[0]["upper_cursor"] == 14
    assert calls[1]["summary_messages"] == [11, 12]


@pytest.mark.asyncio
async def test_a_no_new_messages_run_does_not_touch_the_checkpoint(monkeypatch):
    repo = InMemoryTaskRepository()
    task, state = await _task_with_state(repo, baseline=10)

    async def _classify(*args, **kwargs):
        return PeerType.PRIVATE_USER

    async def _latest(*args, **kwargs):
        return state.scanned_cursor

    monkeypatch.setattr(history_service, "classify_chat", _classify)
    monkeypatch.setattr(history_service, "latest_message_id", _latest)

    ok, message, _data = await history_schedule_service.run_scheduled_history(
        object(), owner_id=OWNER, task_id=task.id, repository=repo,
    )

    assert ok is True and message == ""
    current = await repo.get_history_schedule(OWNER, task.id)
    assert current.version == state.version
    assert current.scanned_cursor == current.summarized_cursor == 10


@pytest.mark.asyncio
async def test_ai_failure_records_error_without_advancing_the_summary(monkeypatch):
    repo = InMemoryTaskRepository()
    task, _state = await _task_with_state(repo, baseline=10)
    await repo.commit_history_success(
        OWNER, PEER, expected_version=1, summary="old summary",
        scanned_cursor=10, summarized_cursor=10,
    )

    async def _classify(*args, **kwargs):
        return PeerType.PRIVATE_USER

    async def _latest(*args, **kwargs):
        return 11

    async def _fetch(*args, **kwargs):
        return HistoryBatch(
            chat_id=PEER, messages=(_message(11),), scanned=1,
            next_cursor=11, has_more=False,
        )

    async def _summarize(*args, **kwargs):
        return False, "provider failed", {"error": "ai_provider_failed"}

    monkeypatch.setattr(history_service, "classify_chat", _classify)
    monkeypatch.setattr(history_service, "latest_message_id", _latest)
    monkeypatch.setattr(history_service, "fetch_oldest_history", _fetch)
    monkeypatch.setattr(
        history_schedule_service.history_ai_service,
        "summarize_incremental",
        _summarize,
    )

    ok, message_text, data = await history_schedule_service.run_scheduled_history(
        object(), owner_id=OWNER, task_id=task.id, repository=repo,
    )

    assert ok is False
    current = await repo.get_history_schedule(OWNER, task.id)
    assert current.summary == "old summary"
    assert current.scanned_cursor == current.summarized_cursor == 10
    assert current.last_error
    assert data["error"] == "scheduled_history_ai_failed", message_text


@pytest.mark.asyncio
async def test_a_safe_skip_advances_only_the_scan_cursor(monkeypatch):
    repo = InMemoryTaskRepository()
    task, state = await _task_with_state(repo, baseline=10)
    state = await repo.commit_history_success(
        OWNER, PEER, expected_version=state.version,
        summary="durable summary", scanned_cursor=10, summarized_cursor=10,
    )
    committed_at = state.last_committed_at
    revision = state.revision
    version = state.version

    async def _classify(*args, **kwargs):
        return PeerType.PRIVATE_USER

    async def _latest(*args, **kwargs):
        return 20

    async def _fetch(*args, **kwargs):
        return HistoryBatch(
            chat_id=PEER,
            messages=(_message(15, ""), _message(16, "   ")),
            scanned=2,
            next_cursor=20,
            has_more=False,
        )

    monkeypatch.setattr(history_service, "classify_chat", _classify)
    monkeypatch.setattr(history_service, "latest_message_id", _latest)
    monkeypatch.setattr(history_service, "fetch_oldest_history", _fetch)

    ok, message, data = await history_schedule_service.run_scheduled_history(
        object(), owner_id=OWNER, task_id=task.id, repository=repo,
    )

    assert ok is True and message == ""
    current = await repo.get_history_schedule(OWNER, task.id)
    assert current.scanned_cursor == 20
    assert current.summarized_cursor == state.summarized_cursor
    assert current.summary == state.summary
    assert current.revision == revision
    assert current.last_committed_at == committed_at
    assert current.version == version + 1
    assert data["scanned"] == 20 and data["revision"] == revision


@pytest.mark.asyncio
async def test_the_tool_requires_both_trusted_scheduled_markers(monkeypatch):
    seen: list[int] = []

    async def _run(source, **kwargs):
        seen.append(kwargs["task_id"])
        return True, "ok", {}

    monkeypatch.setattr(
        history_schedule_service, "run_scheduled_history", _run,
    )

    missing = await ScheduledHistoryTool(
        ToolContext(None, OWNER, "UTC")
    ).execute(ToolContext(None, OWNER, "UTC"), {})
    assert missing.success is False
    assert missing.data["error"] == "scheduled_history_provenance_missing"

    task_only = ToolContext(
        None, OWNER, "UTC", extra={SCHEDULED_TASK_ID_EXTRA: 77},
    )
    result = await ScheduledHistoryTool(task_only).execute(task_only, {})
    assert result.success is False
    assert result.data["error"] == "scheduled_history_provenance_missing"

    occurrence_only = ToolContext(
        None, OWNER, "UTC",
        extra={SCHEDULED_OCCURRENCE_EXTRA: VALID_SCHEDULED_KEY},
    )
    result = await ScheduledHistoryTool(occurrence_only).execute(occurrence_only, {})
    assert result.success is False
    assert result.data["error"] == "scheduled_history_provenance_missing"

    arbitrary = ToolContext(
        None, OWNER, "UTC",
        extra={
            SCHEDULED_TASK_ID_EXTRA: 77,
            SCHEDULED_OCCURRENCE_EXTRA: "k",
        },
    )
    result = await ScheduledHistoryTool(arbitrary).execute(arbitrary, {})
    assert result.success is False
    assert result.data["error"] == "scheduled_history_provenance_invalid"

    assert seen == []

    context = ToolContext(
        None, OWNER, "UTC",
        extra={
            SCHEDULED_TASK_ID_EXTRA: 77,
            SCHEDULED_OCCURRENCE_EXTRA: VALID_SCHEDULED_KEY,
        },
    )
    result = await ScheduledHistoryTool(context).execute(context, {})
    assert result.success is True
    assert seen == [77]


@pytest.mark.parametrize(
    ("task_id", "marker", "expected_error"),
    [
        (77, VALID_SCHEDULED_KEY, ""),
        (77, "k", "invalid"),
        (77, "77", "invalid"),
        (77, "77:", "invalid"),
        (77, "77:invalid", "invalid"),
        (77, "77:2026-11-14T12:00:00", "invalid"),
        (77, "77:2026-11-14T12:00:00Z", "invalid"),
        (77, "77:2026-11-14T12:00:00+01:00", "invalid"),
        (77, "78:2026-11-14T12:00:00+00:00", "inconsistent"),
        (True, VALID_SCHEDULED_KEY, "invalid"),
        (0, VALID_SCHEDULED_KEY, "invalid"),
        (-1, VALID_SCHEDULED_KEY, "invalid"),
        (77, " " + VALID_SCHEDULED_KEY, "invalid"),
        (77, VALID_SCHEDULED_KEY + " ", "invalid"),
        (77, "x" * 257, "invalid"),
    ],
)
def test_occurrence_provenance_requires_the_exact_scheduler_key(
    task_id, marker, expected_error,
):
    task_key, occurrence, error = scheduled_occurrence_provenance({
        SCHEDULED_TASK_ID_EXTRA: task_id,
        SCHEDULED_OCCURRENCE_EXTRA: marker,
    })
    assert error == expected_error
    if expected_error:
        assert (task_key, occurrence) == (0, "")
    else:
        assert (task_key, occurrence) == (77, VALID_SCHEDULED_KEY)


def test_a_generated_key_with_microseconds_is_valid():
    key = occurrence_key(77, REF.replace(microsecond=123456))
    assert scheduled_occurrence_provenance({
        SCHEDULED_TASK_ID_EXTRA: 77,
        SCHEDULED_OCCURRENCE_EXTRA: key,
    }) == (77, key, "")


@pytest.mark.asyncio
async def test_model_supplied_identity_arguments_are_refused_before_any_lookup(monkeypatch):
    calls: list[dict] = []

    async def _run(source, **kwargs):
        calls.append(kwargs)
        return True, "ok", {}

    monkeypatch.setattr(
        history_schedule_service, "run_scheduled_history", _run,
    )
    context = ToolContext(
        None, OWNER, "UTC",
        extra={
            SCHEDULED_TASK_ID_EXTRA: 77,
            SCHEDULED_OCCURRENCE_EXTRA: VALID_SCHEDULED_KEY,
        },
    )
    for arguments in (
        {"task_id": 999},
        {"occurrence_key": "999:x"},
        {"peer_id": PEER},
        {"scanned_cursor": 10},
        {"instruction": "focus", "task_id": 999},
    ):
        result = await ScheduledHistoryTool(context).execute(context, arguments)
        assert result.success is False
        assert result.data["error"] == "scheduled_history_arguments_invalid"
    assert calls == []


class _FakeProviderManager:
    def __init__(self) -> None:
        self.messages: list[list[dict]] = []

    async def chat(self, messages, **kwargs):
        self.messages.append(messages)
        return SimpleNamespace(success=True, text="cumulative summary")


@pytest.mark.asyncio
async def test_incremental_summary_sends_only_text_and_owner_focus(monkeypatch):
    monkeypatch.setattr(history_ai_service, "call_spacing", lambda calls, budget: 0.0)
    manager = _FakeProviderManager()

    ok, summary, data = await history_ai_service.summarize_incremental(
        (_message(11, "first body"), _message(12, "second body")),
        previous_summary="prior summary",
        instruction="owner focus",
        provider_manager=manager,
        timeout_s=30,
    )

    assert ok is True
    assert summary == "cumulative summary"
    assert data["text_messages"] == 2
    assert len(manager.messages) == 2
    for messages in manager.messages:
        prompt = "\n".join(message["content"] for message in messages)
        assert str(PEER) not in prompt
        assert str(OWNER) not in prompt
        assert "[11]" not in prompt and "[12]" not in prompt
        assert "owner focus" in prompt
    assert "first body" in manager.messages[0][1]["content"]
    assert "prior summary" in manager.messages[1][1]["content"]


@pytest.mark.asyncio
async def test_incremental_summary_is_a_no_op_for_non_text_messages():
    manager = _FakeProviderManager()
    ok, summary, data = await history_ai_service.summarize_incremental(
        (HistoryMessage(11, PEER, 1, False, REF, "", None, True, False),),
        provider_manager=manager,
    )
    assert ok is True and summary == ""
    assert data["text_messages"] == 0
    assert manager.messages == []


class _ProbeTool:
    name = SCHEDULED_HISTORY_ACTION
    description = "probe"
    parameters = {}
    permission_level = PermissionLevel.READ_WRITE
    safe = True
    return_type = "object"

    def __init__(self) -> None:
        self.extra: dict | None = None

    async def execute(self, context, arguments):
        self.extra = dict(context.extra or {})
        return ToolResult(True, "ok")


@pytest.mark.asyncio
async def test_the_coordinator_injects_the_occurrence_task_id():
    repo = InMemoryTaskRepository()
    task = await repo.create_task(OWNER, {
        "label": "probe", "schedule_type": "once",
        "schedule": {"at": "2026-11-14T12:00:00", "timezone": "UTC"},
        "timezone": "UTC",
        "actions": [{"name": SCHEDULED_HISTORY_ACTION, "arguments": {}}],
        "notification_destination": {},
    })
    key = occurrence_key(task.id, REF)
    await repo.create_occurrence(OWNER, {
        "task_id": task.id, "occurrence_key": key, "definition_version": task.version,
        "action_snapshot": [
            {"name": SCHEDULED_HISTORY_ACTION, "arguments": {}}
        ],
        "scheduled_for": REF,
    })
    await repo.claim_occurrence(OWNER, task.id, key)
    probe = _ProbeTool()
    registry = ToolRegistry()
    registry.register(probe)
    context = ToolContext(None, OWNER, "UTC")
    coordinator = TaskExecutionCoordinator(
        repo, ToolExecutor(registry, context), OWNER, context,
    )
    result = await coordinator.execute(
        await repo.get_occurrence(OWNER, task.id, key),
    )
    assert result.success
    assert probe.extra is not None
    assert probe.extra[SCHEDULED_TASK_ID_EXTRA] == task.id
    assert probe.extra[SCHEDULED_OCCURRENCE_EXTRA] == key
