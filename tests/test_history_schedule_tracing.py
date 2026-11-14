"""Focused observability tests for the Scheduled History pipeline.

These exercise the real scheduler, coordinator, registered tool, checkpoint
service, AI boundary and repository CAS paths with in-memory/test doubles.
They assert correlation, stage ordering, distinguishable outcomes and the
content-free trace contract; no Telegram or Supabase call is made.
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from backend.ai.database.task_repository import (
    DELETION_STALE,
    HistoryStorageUnavailable,
    InMemoryTaskRepository,
    TaskDeletionResult,
)
from backend.ai.retry import retry_delay
from backend.ai.task_candidate import TaskCandidate
from backend.ai.task_contract import (
    SCHEDULED_HISTORY_ACTION,
    SCHEDULED_OCCURRENCE_EXTRA,
    SCHEDULED_TASK_ID_EXTRA,
)
from backend.ai.task_creation import (
    HistoryScheduleCreationError,
    TaskCreationService,
)
from backend.ai.task_execution import TaskExecutionCoordinator
from backend.ai.task_scheduler import TaskScheduler, occurrence_key
from backend.ai.task_trace import current_occurrence, occurrence_scope
from backend.ai.tools.base import PermissionLevel, ToolResult
from backend.ai.tools.context import ToolContext
from backend.ai.tools.executor import ToolExecutor
from backend.ai.tools.history_schedule import ScheduledHistoryTool
from backend.ai.tools.registry import ToolRegistry, create_default_registry
from backend.services import history_ai_service, history_schedule_service, history_service
from backend.services.history_service import HistoryBatch, HistoryMessage
from backend.telegram_api.peers import PeerType

OWNER = 92001
PEER = -10092001
REF = datetime(2026, 11, 14, 12, 0, tzinfo=timezone.utc)
KEY = occurrence_key(77, REF)


def _message(message_id: int, text: str = "body") -> HistoryMessage:
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


def _task_payload(
    *,
    next_run: datetime | None = REF,
    actions: list[dict] | None = None,
) -> dict:
    return {
        "label": "Scheduled History trace",
        "schedule_type": "interval",
        "schedule": {"seconds": 3600},
        "timezone": "UTC",
        "actions": actions
        if actions is not None
        else [{"name": SCHEDULED_HISTORY_ACTION, "arguments": {}}],
        "notification_destination": {},
        "next_run_at": next_run,
    }


async def _task_with_state(
    repo: InMemoryTaskRepository, *, baseline: int = 10, next_run: datetime | None = REF
):
    task = await repo.create_task(OWNER, _task_payload(next_run=next_run))
    state = await repo.create_history_schedule(
        OWNER, task.id, PEER, baseline_cursor=baseline,
    )
    return task, state


def _lines(caplog, prefix: str = "SCHEDULED_OCCURRENCE_TRACE") -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if prefix in record.getMessage()
    ]


def _stages(lines: list[str]) -> list[str]:
    return [re.search(r"\bstage=(\S+)", line).group(1) for line in lines]


def _field(line: str, name: str) -> str:
    match = re.search(rf"\b{re.escape(name)}=(\S+)", line)
    return match.group(1) if match else ""


def _assert_ordered(stages: list[str], expected: list[str]) -> None:
    position = -1
    for stage in expected:
        assert stage in stages, f"missing stage {stage}; got {stages}"
        found = stages.index(stage, position + 1)
        assert found > position, f"stage {stage} was out of order: {stages}"
        position = found


class _FakeCoordinator:
    def __init__(self, repo: InMemoryTaskRepository, status: str = "succeeded") -> None:
        self.repo = repo
        self.status = status
        self.calls: list[str] = []

    async def execute(self, occurrence, *, now=None):
        self.calls.append(occurrence.occurrence_key)
        assert current_occurrence()["trace_id"] == occurrence.occurrence_key
        await self.repo.transition_occurrence(
            OWNER, occurrence.task_id, occurrence.occurrence_key, self.status
        )
        return SimpleNamespace(
            success=self.status == "succeeded", status=self.status,
            action_count=1, successful_actions=1,
        )


class _ProbeTool:
    name = "trace_probe"
    description = "probe"
    parameters = {}
    permission_level = PermissionLevel.READ_WRITE
    safe = True
    return_type = "object"

    def __init__(self) -> None:
        self.calls = 0

    async def execute(self, context, arguments):
        self.calls += 1
        return ToolResult(True, "ok")


@pytest.mark.asyncio
async def test_successful_scheduled_history_emits_ordered_major_stage_trace(monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    repo = InMemoryTaskRepository()
    task, state = await _task_with_state(repo)

    async def _classify(*args, **kwargs):
        return PeerType.PRIVATE_USER

    async def _latest(*args, **kwargs):
        return 13

    async def _fetch(*args, **kwargs):
        return HistoryBatch(
            chat_id=PEER,
            messages=(_message(11), _message(12)),
            scanned=2,
            next_cursor=13,
            has_more=False,
        )

    async def _summarize(*args, **kwargs):
        return True, "a cumulative summary", {"processed": 2, "chunks": 1}

    monkeypatch.setattr(history_service, "classify_chat", _classify)
    monkeypatch.setattr(history_service, "latest_message_id", _latest)
    monkeypatch.setattr(history_service, "fetch_oldest_history", _fetch)
    monkeypatch.setattr(
        history_schedule_service.history_ai_service,
        "summarize_incremental",
        _summarize,
    )
    monkeypatch.setattr(history_schedule_service, "get_task_repository", lambda: repo)

    context = ToolContext(None, OWNER, "UTC", extra={})
    registry = create_default_registry(context)
    coordinator = TaskExecutionCoordinator(
        repo, ToolExecutor(registry, context), OWNER, context,
    )
    scheduler = TaskScheduler(repo, OWNER, execution_coordinator=coordinator)

    assert await scheduler.run_once(REF) == 1
    rows = await repo.list_occurrences(OWNER, task.id)
    assert len(rows) == 1
    assert rows[0].occurrence_key == occurrence_key(task.id, REF)
    assert rows[0].status == "succeeded"

    lines = _lines(caplog)
    stages = _stages(lines)
    assert "occurrence_persist_result outcome=created status=claimed" in "\n".join(lines)
    _assert_ordered(stages, [
        "due_task_discovered",
        "occurrence_create_result",
        "claim_result",
        "execution_handoff",
        "execution_entered",
        "provenance_injected",
        "actions_planned",
        "validation_result",
        "execution_started",
        "action_started",
        "tool_invocation",
        "provenance_result",
        "service_invocation_started",
        "state_lookup_result",
        "peer_classification_result",
        "retrieval_started",
        "retrieval_completed",
        "ai_summarization_started",
        "ai_summarization_result",
        "commit_started",
        "repository_cas_result",
        "commit_result",
        "service_result",
        "action_result",
        "terminal_outcome",
        "execution_finished",
        "execution_result",
    ])
    assert all(_field(line, "trace_id") == occurrence_key(task.id, REF) for line in lines)
    assert all(_field(line, "task_id") == str(task.id) for line in lines)
    assert all(_field(line, "attempt") == "1" for line in lines)
    assert "outcome=summary_commit" in "\n".join(lines)


@pytest.mark.asyncio
async def test_retry_keeps_correlation_and_uses_distinct_attempt(monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    repo = InMemoryTaskRepository()
    task = await repo.create_task(OWNER, _task_payload(next_run=None))
    key = occurrence_key(task.id, REF)
    await repo.create_occurrence(OWNER, {
        "task_id": task.id,
        "occurrence_key": key,
        "definition_version": task.version,
        "action_snapshot": task.actions,
        "scheduled_for": REF,
        "status": "interrupted",
    })
    coordinator = _FakeCoordinator(repo)
    scheduler = TaskScheduler(repo, OWNER, execution_coordinator=coordinator)

    assert await scheduler.recover() == 1
    recovered = await repo.get_occurrence(OWNER, task.id, key)
    assert recovered.status == "retry_pending" and recovered.attempt == 2
    caplog.clear()
    assert await scheduler.run_once(recovered.retry_at) == 1

    lines = _lines(caplog)
    assert lines
    assert all(_field(line, "trace_id") == key for line in lines)
    assert all(_field(line, "attempt") == "2" for line in lines)
    assert "stage=recovery_result" not in "\n".join(lines)
    _assert_ordered(_stages(lines), [
        "claim_attempt", "claim_result", "execution_handoff",
        "execution_result", "outcome_notification",
    ])


@pytest.mark.asyncio
async def test_concurrent_occurrences_have_isolated_trace_contexts(caplog):
    caplog.set_level(logging.INFO)
    repo = InMemoryTaskRepository()
    observed: dict[str, str] = {}

    class _ConcurrentCoordinator:
        async def execute(self, occurrence, *, now=None):
            observed[occurrence.occurrence_key] = current_occurrence()["trace_id"]
            await asyncio.sleep(0.02)
            assert current_occurrence()["trace_id"] == occurrence.occurrence_key
            await repo.transition_occurrence(
                OWNER, occurrence.task_id, occurrence.occurrence_key, "succeeded"
            )
            return SimpleNamespace(
                success=True, status="succeeded", action_count=1, successful_actions=1,
            )

    tasks = []
    occurrences = []
    for _ in range(2):
        task = await repo.create_task(OWNER, _task_payload(next_run=None))
        key = occurrence_key(task.id, REF)
        await repo.create_occurrence(OWNER, {
            "task_id": task.id,
            "occurrence_key": key,
            "definition_version": task.version,
            "action_snapshot": task.actions,
            "scheduled_for": REF,
            "status": "claimed",
        })
        tasks.append((task, key))
        occurrences.append(await repo.get_occurrence(OWNER, task.id, key))

    scheduler = TaskScheduler(
        repo, OWNER, execution_coordinator=_ConcurrentCoordinator()
    )
    assert await scheduler._execute_claimed_batch(occurrences, REF) == 2
    assert set(observed) == {key for _task, key in tasks}
    assert len(set(observed.values())) == 2
    assert current_occurrence() is None


@pytest.mark.asyncio
async def test_trace_context_is_reset_after_success():
    repo = InMemoryTaskRepository()
    task = await repo.create_task(OWNER, _task_payload(
        next_run=None, actions=[{"name": "trace_probe", "arguments": {}}]
    ))
    key = occurrence_key(task.id, REF)
    await repo.create_occurrence(OWNER, {
        "task_id": task.id, "occurrence_key": key,
        "definition_version": task.version, "action_snapshot": task.actions,
        "scheduled_for": REF,
    })
    await repo.claim_occurrence(OWNER, task.id, key)
    registry = ToolRegistry()
    registry.register(_ProbeTool())
    ctx = ToolContext(None, OWNER, "UTC")
    coordinator = TaskExecutionCoordinator(
        repo, ToolExecutor(registry, ctx), OWNER, ctx,
    )
    result = await coordinator.execute(await repo.get_occurrence(OWNER, task.id, key))
    assert result.success
    assert current_occurrence() is None


@pytest.mark.asyncio
async def test_trace_context_is_reset_after_exception():
    repo = InMemoryTaskRepository()
    task = await repo.create_task(OWNER, _task_payload(next_run=None))
    key = occurrence_key(task.id, REF)
    await repo.create_occurrence(OWNER, {
        "task_id": task.id, "occurrence_key": key,
        "definition_version": task.version, "action_snapshot": task.actions,
        "scheduled_for": REF,
    })
    await repo.claim_occurrence(OWNER, task.id, key)

    class _ExplodingRepo(InMemoryTaskRepository):
        async def get_task(self, owner_id, task_id):
            raise RuntimeError("simulated repository exception")

    exploding = _ExplodingRepo()
    ctx = ToolContext(None, OWNER, "UTC")
    registry = ToolRegistry()
    registry.register(_ProbeTool())
    coordinator = TaskExecutionCoordinator(
        exploding, ToolExecutor(registry, ctx), OWNER, ctx,
    )
    with pytest.raises(RuntimeError):
        await coordinator.execute(await repo.get_occurrence(OWNER, task.id, key))
    assert current_occurrence() is None


@pytest.mark.asyncio
async def test_trace_context_is_reset_after_cancellation():
    repo = InMemoryTaskRepository()
    task = await repo.create_task(OWNER, _task_payload(next_run=None))
    key = occurrence_key(task.id, REF)
    await repo.create_occurrence(OWNER, {
        "task_id": task.id, "occurrence_key": key,
        "definition_version": task.version, "action_snapshot": task.actions,
        "scheduled_for": REF,
    })
    await repo.claim_occurrence(OWNER, task.id, key)

    class _CancellingRepo(InMemoryTaskRepository):
        async def get_task(self, owner_id, task_id):
            raise asyncio.CancelledError

    cancelling = _CancellingRepo()
    ctx = ToolContext(None, OWNER, "UTC")
    coordinator = TaskExecutionCoordinator(
        cancelling, ToolExecutor(ToolRegistry(), ctx), OWNER, ctx,
    )
    with pytest.raises(asyncio.CancelledError):
        await coordinator.execute(await repo.get_occurrence(OWNER, task.id, key))
    assert current_occurrence() is None


@pytest.mark.asyncio
async def test_ordinary_task_creation_does_not_emit_scheduled_trace(monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    from backend.ai.task_trace import bind_request, unbind

    token = bind_request("ordinary-request")
    try:
        repo = InMemoryTaskRepository()
        service = TaskCreationService(repo, OWNER)
        await service.create(
            {
                "label": "ordinary",
                "schedule_type": "interval",
                "schedule": {"seconds": 3600},
                "timezone": "UTC",
                "actions": [{"name": "send_message", "arguments": {"text": "hello"}}],
                "notification_destination": {},
            },
            REF,
        )
    finally:
        unbind(token)
    assert _lines(caplog) == []


@pytest.mark.asyncio
async def test_invalid_provenance_is_rejected_before_the_service(monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    called = False

    async def _must_not_run(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("service must not be reached")

    monkeypatch.setattr(history_schedule_service, "run_scheduled_history", _must_not_run)
    context = ToolContext(
        None, OWNER, "UTC",
        extra={
            SCHEDULED_TASK_ID_EXTRA: 77,
            SCHEDULED_OCCURRENCE_EXTRA: "not-a-scheduler-key",
        },
    )
    with occurrence_scope("bogus-occurrence", task_id=77, attempt=1):
        result = await ScheduledHistoryTool(context).execute(context, {})
    assert result.success is False
    assert called is False
    lines = _lines(caplog)
    assert "stage=provenance_result" in "\n".join(lines)
    assert "outcome=rejected" in "\n".join(lines)
    assert "category=invalid" in "\n".join(lines)
    assert "service_invocation_started" not in "\n".join(lines)


@pytest.mark.asyncio
async def test_missing_state_is_distinguishable_from_unavailable_storage(caplog):
    caplog.set_level(logging.INFO)

    class _MissingRepo:
        async def get_history_schedule(self, owner_id, task_id):
            return None

    class _UnavailableRepo:
        async def get_history_schedule(self, owner_id, task_id):
            raise HistoryStorageUnavailable("durable store unavailable")

    with occurrence_scope(KEY, task_id=77, attempt=1):
        ok, _message_text, data = await history_schedule_service.run_scheduled_history(
            object(), owner_id=OWNER, task_id=77, repository=_MissingRepo(),
        )
    assert ok is False and data["error"] == "scheduled_history_state_missing"
    missing_lines = _lines(caplog)
    assert "stage=state_lookup_result outcome=missing" in "\n".join(missing_lines)
    assert "stage=checkpoint_result outcome=state_missing" in "\n".join(missing_lines)

    caplog.clear()
    with occurrence_scope(KEY, task_id=77, attempt=1):
        with pytest.raises(HistoryStorageUnavailable):
            await history_schedule_service.run_scheduled_history(
                object(), owner_id=OWNER, task_id=77, repository=_UnavailableRepo(),
            )
    unavailable_lines = _lines(caplog)
    assert "stage=state_lookup_result outcome=unavailable" in "\n".join(unavailable_lines)
    assert "stage=checkpoint_result" not in "\n".join(unavailable_lines)


@pytest.mark.asyncio
async def test_peer_policy_refusal_is_distinct_from_retrieval_failure(monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    repo = InMemoryTaskRepository()
    task, _state = await _task_with_state(repo)

    async def _unknown(*args, **kwargs):
        return PeerType.UNKNOWN

    monkeypatch.setattr(history_service, "classify_chat", _unknown)
    with occurrence_scope(KEY, task_id=task.id, attempt=1):
        ok, _message_text, data = await history_schedule_service.run_scheduled_history(
            object(), owner_id=OWNER, task_id=task.id, repository=repo,
        )
    assert ok is False and data["error"] == "scheduled_history_retrieval_failed"
    policy_lines = "\n".join(_lines(caplog))
    assert "stage=policy_result outcome=refused" in policy_lines
    assert "outcome=peer_policy_refused" in policy_lines
    assert "stage=retrieval_failed failure_stage=peer_classification category=peer_policy" in policy_lines

    caplog.clear()
    repo2 = InMemoryTaskRepository()
    task2, _state2 = await _task_with_state(repo2)

    async def _private(*args, **kwargs):
        return PeerType.PRIVATE_USER

    async def _latest(*args, **kwargs):
        return 20

    async def _fetch_failed(*args, **kwargs):
        raise history_service.HistoryError("SECRET_TELEGRAM_RETRIEVAL_TEXT")

    monkeypatch.setattr(history_service, "classify_chat", _private)
    monkeypatch.setattr(history_service, "latest_message_id", _latest)
    monkeypatch.setattr(history_service, "fetch_oldest_history", _fetch_failed)
    with occurrence_scope(occurrence_key(task2.id, REF), task_id=task2.id, attempt=1):
        ok, _message_text, data = await history_schedule_service.run_scheduled_history(
            object(), owner_id=OWNER, task_id=task2.id, repository=repo2,
        )
    assert ok is False and data["error"] == "scheduled_history_retrieval_failed"
    retrieval_lines = "\n".join(_lines(caplog))
    assert "stage=retrieval_failed failure_stage=retrieval category=telegram_retrieval" in retrieval_lines
    assert "outcome=retrieval_failed" in retrieval_lines
    assert "SECRET_TELEGRAM_RETRIEVAL_TEXT" not in retrieval_lines


@pytest.mark.asyncio
async def test_retrieval_failure_is_distinct_from_ai_failure(monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    repo = InMemoryTaskRepository()
    task, _state = await _task_with_state(repo)

    async def _classify(*args, **kwargs):
        return PeerType.PRIVATE_USER

    async def _latest(*args, **kwargs):
        return 11

    async def _fetch(*args, **kwargs):
        return HistoryBatch(PEER, (_message(11),), 1, 11, False)

    async def _ai_failed(*args, **kwargs):
        return False, "provider failed", {"error": "ai_provider_failed", "chunks": 1}

    monkeypatch.setattr(history_service, "classify_chat", _classify)
    monkeypatch.setattr(history_service, "latest_message_id", _latest)
    monkeypatch.setattr(history_service, "fetch_oldest_history", _fetch)
    monkeypatch.setattr(
        history_schedule_service.history_ai_service,
        "summarize_incremental",
        _ai_failed,
    )
    with occurrence_scope(KEY, task_id=task.id, attempt=1):
        ok, _message_text, data = await history_schedule_service.run_scheduled_history(
            object(), owner_id=OWNER, task_id=task.id, repository=repo,
        )
    assert ok is False and data["error"] == "scheduled_history_ai_failed"
    lines = "\n".join(_lines(caplog))
    assert "stage=retrieval_completed" in lines
    assert "stage=ai_summarization_result outcome=provider_failed" in lines
    assert "outcome=ai_failed" in lines
    assert "outcome=retrieval_failed" not in lines


@pytest.mark.asyncio
async def test_empty_safe_skip_is_distinct_from_a_summary_commit(monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    repo = InMemoryTaskRepository()
    task, state = await _task_with_state(repo)
    state = await repo.commit_history_success(
        OWNER, PEER, expected_version=state.version, summary="old summary",
        scanned_cursor=10, summarized_cursor=10,
    )

    async def _classify(*args, **kwargs):
        return PeerType.PRIVATE_USER

    async def _latest(*args, **kwargs):
        return 20

    async def _fetch_empty(*args, **kwargs):
        return HistoryBatch(PEER, (_message(15, ""), _message(16, "  ")), 2, 20, False)

    async def _must_not_summarize(*args, **kwargs):
        raise AssertionError("safe skip must not call AI")

    monkeypatch.setattr(history_service, "classify_chat", _classify)
    monkeypatch.setattr(history_service, "latest_message_id", _latest)
    monkeypatch.setattr(history_service, "fetch_oldest_history", _fetch_empty)
    monkeypatch.setattr(
        history_schedule_service.history_ai_service,
        "summarize_incremental",
        _must_not_summarize,
    )
    with occurrence_scope(KEY, task_id=task.id, attempt=1):
        ok, _message_text, data = await history_schedule_service.run_scheduled_history(
            object(), owner_id=OWNER, task_id=task.id, repository=repo,
        )
    assert ok is True and data["scanned"] == 20
    current = await repo.get_history_schedule(OWNER, task.id)
    assert current.summary == "old summary" and current.summarized_cursor == 10
    assert current.revision == state.revision
    safe_lines = "\n".join(_lines(caplog))
    assert "stage=safe_skip_evaluated outcome=textless_batch" in safe_lines
    assert "stage=scan_advance_result outcome=committed" in safe_lines
    assert "outcome=scan_only_advanced" in safe_lines
    assert "ai_summarization_started" not in safe_lines

    caplog.clear()
    repo2 = InMemoryTaskRepository()
    task2, _state2 = await _task_with_state(repo2)

    async def _fetch_text(*args, **kwargs):
        return HistoryBatch(PEER, (_message(11),), 1, 11, False)

    async def _summarize(*args, **kwargs):
        return True, "new summary", {"processed": 1, "chunks": 1}

    monkeypatch.setattr(history_service, "fetch_oldest_history", _fetch_text)
    monkeypatch.setattr(
        history_schedule_service.history_ai_service,
        "summarize_incremental",
        _summarize,
    )
    with occurrence_scope(occurrence_key(task2.id, REF), task_id=task2.id, attempt=1):
        ok, _message_text, data = await history_schedule_service.run_scheduled_history(
            object(), owner_id=OWNER, task_id=task2.id, repository=repo2,
        )
    assert ok is True
    summary_lines = "\n".join(_lines(caplog))
    assert "stage=ai_summarization_started" in summary_lines
    assert "stage=commit_result write_kind=summary outcome=committed" in summary_lines
    assert "outcome=summary_commit" in summary_lines
    assert "outcome=scan_only_advanced" not in summary_lines


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("ai_result", "expected_event", "expected_outcome"),
    [
        ((False, "provider failed", {"error": "ai_provider_failed"}), "outcome=provider_failed", "outcome=ai_failed"),
        ((True, "", {}), "outcome=empty", "outcome=ai_output_empty"),
        ((True, "x" * 32769, {}), "outcome=oversized", "outcome=ai_output_too_large"),
    ],
)
async def test_invalid_or_oversized_ai_output_is_distinct_from_provider_failure(
    monkeypatch, caplog, ai_result, expected_event, expected_outcome,
):
    caplog.set_level(logging.INFO)
    repo = InMemoryTaskRepository()
    task, _state = await _task_with_state(repo)

    async def _classify(*args, **kwargs):
        return PeerType.PRIVATE_USER

    async def _latest(*args, **kwargs):
        return 11

    async def _fetch(*args, **kwargs):
        return HistoryBatch(PEER, (_message(11),), 1, 11, False)

    async def _summarize(*args, **kwargs):
        return ai_result

    monkeypatch.setattr(history_service, "classify_chat", _classify)
    monkeypatch.setattr(history_service, "latest_message_id", _latest)
    monkeypatch.setattr(history_service, "fetch_oldest_history", _fetch)
    monkeypatch.setattr(
        history_schedule_service.history_ai_service,
        "summarize_incremental",
        _summarize,
    )
    with occurrence_scope(KEY, task_id=task.id, attempt=1):
        ok, _message_text, _data = await history_schedule_service.run_scheduled_history(
            object(), owner_id=OWNER, task_id=task.id, repository=repo,
        )
    assert ok is False
    lines = "\n".join(_lines(caplog))
    assert expected_event in lines
    assert expected_outcome in lines
    assert "stage=ai_summarization_result" in lines


@pytest.mark.asyncio
async def test_provider_failure_timeout_and_empty_output_are_distinct_and_content_free(caplog):
    caplog.set_level(logging.INFO)

    class _Manager:
        def __init__(self, outcome: str) -> None:
            self.outcome = outcome
            self.calls = 0

        async def chat(self, messages, **kwargs):
            self.calls += 1
            if self.outcome == "timeout":
                raise asyncio.TimeoutError
            return SimpleNamespace(
                success=False if self.outcome == "failure" else True,
                text="" if self.outcome == "empty" else "SECRET_PROVIDER_BODY",
                provider_name="fake-provider",
                metadata={
                    "failure_type": "rate_limit",
                    "fallback_exhausted": True,
                    "finish_reason": "stop",
                },
            )

    async def _run(outcome: str):
        caplog.clear()
        manager = _Manager(outcome)
        with occurrence_scope(KEY, task_id=77, attempt=1):
            result = await history_ai_service.summarize_incremental(
                (_message(11, "SECRET_MESSAGE_BODY"),),
                previous_summary="SECRET_PRIOR_SUMMARY",
                instruction="SECRET_OWNER_INSTRUCTION",
                provider_manager=manager,
                timeout_s=30,
            )
        return result, "\n".join(_lines(caplog))

    _failure, failure_trace = await _run("failure")
    assert "stage=provider_call_result outcome=provider_failed" in failure_trace
    assert "provider=fake-provider" in failure_trace
    assert "failure_origin=rate_limit" in failure_trace
    assert "fallback_exhausted=True" in failure_trace
    assert "SECRET_PROVIDER_BODY" not in failure_trace
    assert "SECRET_MESSAGE_BODY" not in failure_trace
    assert "SECRET_PRIOR_SUMMARY" not in failure_trace
    assert "SECRET_OWNER_INSTRUCTION" not in failure_trace

    _timeout, timeout_trace = await _run("timeout")
    assert "stage=provider_call_result outcome=timeout" in timeout_trace
    assert "category=provider_timeout" in timeout_trace

    _empty, empty_trace = await _run("empty")
    assert "stage=provider_call_result outcome=empty_output" in empty_trace
    assert "SECRET_PROVIDER_BODY" not in empty_trace


@pytest.mark.asyncio
async def test_cas_conflict_is_distinct_from_durable_database_failure(monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    repo = InMemoryTaskRepository()
    task, _state = await _task_with_state(repo)

    async def _classify(*args, **kwargs):
        return PeerType.PRIVATE_USER

    async def _latest(*args, **kwargs):
        return 11

    async def _fetch(*args, **kwargs):
        return HistoryBatch(PEER, (_message(11),), 1, 11, False)

    async def _summarize(*args, **kwargs):
        return True, "summary", {"processed": 1, "chunks": 1}

    monkeypatch.setattr(history_service, "classify_chat", _classify)
    monkeypatch.setattr(history_service, "latest_message_id", _latest)
    monkeypatch.setattr(history_service, "fetch_oldest_history", _fetch)
    monkeypatch.setattr(
        history_schedule_service.history_ai_service,
        "summarize_incremental",
        _summarize,
    )

    original_commit = repo.commit_history_success

    async def _conflict(*args, **kwargs):
        return None

    monkeypatch.setattr(repo, "commit_history_success", _conflict)
    with occurrence_scope(KEY, task_id=task.id, attempt=1):
        ok, _message_text, data = await history_schedule_service.run_scheduled_history(
            object(), owner_id=OWNER, task_id=task.id, repository=repo,
        )
    assert ok is False and data["error"] == "scheduled_history_commit_conflict"
    conflict_lines = "\n".join(_lines(caplog))
    assert "stage=commit_result write_kind=summary outcome=cas_conflict" in conflict_lines
    assert "outcome=summary_commit_conflict" in conflict_lines

    caplog.clear()
    repo2 = InMemoryTaskRepository()
    task2, _state2 = await _task_with_state(repo2)

    async def _durable_error(*args, **kwargs):
        raise RuntimeError("SECRET_DATABASE_ERROR_TEXT")

    monkeypatch.setattr(repo2, "commit_history_success", _durable_error)
    with occurrence_scope(occurrence_key(task2.id, REF), task_id=task2.id, attempt=1):
        ok, _message_text, data = await history_schedule_service.run_scheduled_history(
            object(), owner_id=OWNER, task_id=task2.id, repository=repo2,
        )
    assert ok is False and data["error"] == "scheduled_history_commit_failed"
    durable_lines = "\n".join(_lines(caplog))
    assert "stage=commit_result write_kind=summary outcome=durable_error" in durable_lines
    assert "outcome=summary_commit_failed" in durable_lines
    assert "SECRET_DATABASE_ERROR_TEXT" not in durable_lines
    monkeypatch.setattr(repo, "commit_history_success", original_commit)


@pytest.mark.asyncio
async def test_compensation_success_and_failure_are_traceable(monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    candidate = TaskCandidate.from_untrusted({
        "label": "history",
        "schedule_type": "interval",
        "schedule": {"seconds": 3600},
        "timezone": "UTC",
        "actions": [{"name": SCHEDULED_HISTORY_ACTION, "arguments": {}}],
        "notification_destination": {},
    })

    repo = InMemoryTaskRepository()
    service = TaskCreationService(repo, OWNER)

    async def _state_failure(*args, **kwargs):
        raise RuntimeError("state write failed")

    monkeypatch.setattr(repo, "create_history_schedule", _state_failure)
    with pytest.raises(HistoryScheduleCreationError):
        await service.create_history_schedule_task(
            candidate, REF, peer_id=PEER, baseline_cursor=1,
        )
    success_lines = "\n".join(_lines(caplog, "HISTORY_SCHEDULE_TRACE"))
    assert "stage=creation_service_entered" in success_lines
    assert "stage=duplicate_check_result" in success_lines
    assert "stage=task_write_result outcome=committed" in success_lines
    assert "stage=state_write_started" in success_lines
    assert "stage=state_write_result outcome=failed" in success_lines
    assert "stage=compensation_started" in success_lines
    assert "stage=compensation_result outcome=durable_deleted" in success_lines
    assert "stage=creation_result outcome=failed" in success_lines
    assert await repo.list_tasks(OWNER) == []

    caplog.clear()
    repo2 = InMemoryTaskRepository()
    service2 = TaskCreationService(repo2, OWNER)

    async def _state_failure2(*args, **kwargs):
        raise RuntimeError("state write failed")

    async def _stale_delete(owner_id, task_id, expected_version):
        return TaskDeletionResult(DELETION_STALE, task_id)

    monkeypatch.setattr(repo2, "create_history_schedule", _state_failure2)
    monkeypatch.setattr(repo2, "delete_task", _stale_delete)
    with pytest.raises(HistoryScheduleCreationError) as raised:
        await service2.create_history_schedule_task(
            candidate, REF, peer_id=PEER, baseline_cursor=1,
        )
    assert raised.value.rollback_durable is False
    failure_lines = "\n".join(_lines(caplog, "HISTORY_SCHEDULE_TRACE"))
    assert "stage=compensation_started" in failure_lines
    assert "stage=compensation_result outcome=stale durable=False" in failure_lines
    assert "stage=creation_result outcome=failed" in failure_lines


@pytest.mark.asyncio
async def test_trace_output_contains_no_message_summary_instruction_or_provider_payload(
    monkeypatch, caplog,
):
    caplog.set_level(logging.INFO)
    repo = InMemoryTaskRepository()
    task, state = await _task_with_state(repo)
    state = await repo.commit_history_success(
        OWNER, PEER, expected_version=state.version,
        summary="SECRET_PRIOR_SUMMARY", scanned_cursor=10, summarized_cursor=10,
    )
    secret_message = "SECRET_MESSAGE_BODY"
    secret_instruction = "SECRET_OWNER_INSTRUCTION"
    secret_provider = "SECRET_PROVIDER_PAYLOAD"

    async def _classify(*args, **kwargs):
        return PeerType.PRIVATE_USER

    async def _latest(*args, **kwargs):
        return 11

    async def _fetch(*args, **kwargs):
        return HistoryBatch(PEER, (_message(11, secret_message),), 1, 11, False)

    async def _provider_failed(*args, **kwargs):
        assert kwargs["instruction"] == secret_instruction
        return False, secret_provider, {"error": "ai_provider_failed"}

    monkeypatch.setattr(history_service, "classify_chat", _classify)
    monkeypatch.setattr(history_service, "latest_message_id", _latest)
    monkeypatch.setattr(history_service, "fetch_oldest_history", _fetch)
    monkeypatch.setattr(
        history_schedule_service.history_ai_service,
        "summarize_incremental",
        _provider_failed,
    )
    with occurrence_scope(KEY, task_id=task.id, attempt=1):
        ok, _message_text, _data = await history_schedule_service.run_scheduled_history(
            object(), owner_id=OWNER, task_id=task.id,
            instruction=secret_instruction, repository=repo,
        )
    assert ok is False
    trace = "\n".join(_lines(caplog))
    assert trace
    for secret in (
        secret_message, secret_instruction, secret_provider,
        "SECRET_PRIOR_SUMMARY",
    ):
        assert secret not in trace
    assert "stage=ai_summarization_result outcome=provider_failed" in trace
