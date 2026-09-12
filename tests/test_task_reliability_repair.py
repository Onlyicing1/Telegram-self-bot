"""Regressions for the task-reliability repair phase.

Covers the source-proven defects:

A. Scheduler timing — a delayed wake must not shift the cadence, a slow task
   must not serialize the sweep, more due tasks than one batch must still run
   in the same wake, retry_at must be honoured, and the sleep is bounded.
B. First-message task management — a read-then-mutate request (task_list ->
   task_transition) must be able to finish, while a pure read request still
   delivers the tool result verbatim.
C. Taskloom wizard — Back is always the wizard's previous step (never a panel
   stack pop) and an input submission preserves the current step + draft.
D. Task editing — definition edits use the existing CAS update, bump the
   version exactly once, discard only FUTURE unstarted occurrences, keep
   history immutable, and preserve the canonical display font.
E. Schema contract — a missing OPTIONAL audit column never costs the durable
   state transition, while a missing required column and a genuine store
   failure are still reported honestly.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from backend.ai.database.task_repository import (
    FALLBACK_REASON_LOCAL_RESOURCE,
    FALLBACK_REASON_UNAVAILABLE,
    InMemoryTaskRepository,
    SupabaseTaskRepository,
)
from backend.ai.task_scheduler import (
    MIN_WAKE_SECONDS,
    WAKE_INTERVAL_SECONDS,
    TaskScheduler,
    occurrence_key,
)

OWNER = 4242
BASE = datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc)


def task_data(**overrides):
    value = {
        "label": "Recurring",
        "schedule_type": "interval",
        "schedule": {"seconds": 300},
        "timezone": "UTC",
        "next_run_at": BASE,
        "actions": [{"name": "send_message", "arguments": {"text": "hello"}}],
        "notification_destination": {"chat_id": 1},
    }
    value.update(overrides)
    return value


class CountingCoordinator:
    """Records executions; never touches Telegram."""

    def __init__(self, repo, owner_id=OWNER, delay=0.0):
        self.repo, self.owner_id, self.delay = repo, owner_id, delay
        self.executed: list[str] = []
        self.peak = 0
        self._live = 0

    async def execute(self, occurrence):
        self._live += 1
        self.peak = max(self.peak, self._live)
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
            self.executed.append(occurrence.occurrence_key)
            await self.repo.transition_occurrence(
                self.owner_id, occurrence.task_id, occurrence.occurrence_key, "succeeded"
            )
            return SimpleNamespace(status="succeeded")
        finally:
            self._live -= 1


# ── A. scheduler timing ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_future_boundary_is_never_executed_early():
    repo = InMemoryTaskRepository()
    await repo.create_task(OWNER, task_data(next_run_at=BASE + timedelta(minutes=5)))
    scheduler = TaskScheduler(repo, OWNER, execution_coordinator=CountingCoordinator(repo))
    assert await scheduler.run_once(BASE) == 0
    assert await repo.list_occurrences(OWNER) == []


@pytest.mark.asyncio
async def test_delayed_wake_preserves_the_original_cadence():
    """A wake 3 minutes late executes the 12:00 occurrence and advances the
    boundary to 12:05 — anchored to the SCHEDULE, never to the wake time."""
    repo = InMemoryTaskRepository()
    task = await repo.create_task(OWNER, task_data())
    coordinator = CountingCoordinator(repo)
    scheduler = TaskScheduler(repo, OWNER, execution_coordinator=coordinator)

    late = BASE + timedelta(minutes=3)
    assert await scheduler.run_once(late) == 1
    assert coordinator.executed == [occurrence_key(task.id, BASE)]
    assert (await repo.get_task(OWNER, task.id)).next_run_at == BASE + timedelta(minutes=5)


@pytest.mark.asyncio
async def test_more_due_tasks_than_one_batch_all_run_in_the_same_wake():
    repo = InMemoryTaskRepository()
    for index in range(25):
        await repo.create_task(OWNER, task_data(label=f"t{index}"))
    coordinator = CountingCoordinator(repo)
    scheduler = TaskScheduler(repo, OWNER, execution_coordinator=coordinator)

    assert await scheduler.run_once(BASE) == 25
    assert len(coordinator.executed) == 25
    assert await repo.list_due_tasks(OWNER, BASE) == []


@pytest.mark.asyncio
async def test_one_slow_task_does_not_serialize_the_sweep():
    repo = InMemoryTaskRepository()
    for index in range(4):
        await repo.create_task(OWNER, task_data(label=f"slow{index}"))
    coordinator = CountingCoordinator(repo, delay=0.1)
    scheduler = TaskScheduler(repo, OWNER, execution_coordinator=coordinator)

    started = time.perf_counter()
    assert await scheduler.run_once(BASE) == 4
    elapsed = time.perf_counter() - started
    assert len(coordinator.executed) == 4
    assert coordinator.peak > 1, "executions did not overlap — the sweep serialized"
    assert elapsed < 0.35, f"one slow task delayed the sweep ({elapsed:.2f}s)"


@pytest.mark.asyncio
async def test_due_retry_honours_retry_at():
    repo = InMemoryTaskRepository()
    task = await repo.create_task(OWNER, task_data(next_run_at=None))
    row = await repo.create_occurrence(OWNER, {
        "task_id": task.id, "occurrence_key": "k", "definition_version": 1,
        "action_snapshot": task.actions, "scheduled_for": BASE,
    })
    await repo.claim_occurrence(OWNER, task.id, row.occurrence_key)
    retry_at = BASE + timedelta(minutes=2)
    await repo.transition_occurrence(
        OWNER, task.id, "k", "retry_pending", retry_at=retry_at, attempt=2
    )
    coordinator = CountingCoordinator(repo)
    scheduler = TaskScheduler(repo, OWNER, execution_coordinator=coordinator)

    assert await scheduler.run_once(retry_at - timedelta(seconds=1)) == 0
    assert coordinator.executed == []
    assert await scheduler.run_once(retry_at) == 1
    assert coordinator.executed == ["k"]


@pytest.mark.asyncio
async def test_sleep_until_nearest_boundary_is_bounded():
    repo = InMemoryTaskRepository()
    scheduler = TaskScheduler(repo, OWNER)
    assert await scheduler._sleep_seconds() == WAKE_INTERVAL_SECONDS

    await repo.create_task(OWNER, task_data(next_run_at=datetime.now(timezone.utc) + timedelta(seconds=3600)))
    assert await scheduler._sleep_seconds() == WAKE_INTERVAL_SECONDS

    await repo.create_task(OWNER, task_data(label="soon", next_run_at=datetime.now(timezone.utc) + timedelta(seconds=20)))
    assert MIN_WAKE_SECONDS <= await scheduler._sleep_seconds() <= WAKE_INTERVAL_SECONDS


@pytest.mark.asyncio
async def test_edit_of_a_task_changes_only_future_schedule():
    """A definition edit changes the next boundary and never rewrites the
    occurrence that already ran."""
    repo = InMemoryTaskRepository()
    task = await repo.create_task(OWNER, task_data())
    coordinator = CountingCoordinator(repo)
    scheduler = TaskScheduler(repo, OWNER, execution_coordinator=coordinator)
    assert await scheduler.run_once(BASE) == 1
    past = await repo.list_occurrences(OWNER, task.id)
    assert len(past) == 1 and past[0].scheduled_for == BASE

    current = await repo.get_task(OWNER, task.id)
    await repo.update_task(OWNER, task.id, current.version, {"schedule": {"seconds": 600}})
    before = (await repo.get_task(OWNER, task.id)).version
    later = BASE + timedelta(minutes=10)
    assert await scheduler.run_once(later) == 1
    rows = await repo.list_occurrences(OWNER, task.id)
    assert [r.scheduled_for for r in rows] == [BASE, BASE + timedelta(minutes=5)]
    # The already-executed occurrence keeps its original definition version;
    # the one created after the edit snapshots the version current at creation.
    assert rows[0].definition_version == 1
    assert rows[1].definition_version == before


# ── B. first-message task management ────────────────────────────────────────

def _dispatcher_with(responses, exec_results):
    from unittest.mock import AsyncMock, MagicMock

    from backend.ai.engine.dispatcher import Dispatcher
    from backend.ai.engine.hooks import NOOP_HOOKS
    from backend.ai.engine.metrics import EngineMetrics
    from backend.ai.tools.executor import ToolExecutionResult

    pm = MagicMock()
    pm.get_active_name.return_value = "test"
    pm.get_active.return_value.config.model = "m"
    pm.get_active.return_value.health.return_value = {"healthy": True}
    pm.get_active.return_value.chat = AsyncMock(side_effect=responses)

    conv = MagicMock()
    session = MagicMock()
    session.session_id, session.active_provider = "s", "test"
    conv.get_session.return_value = session
    conv.restore_history = AsyncMock()
    conv.get_history.return_value = []

    pb = MagicMock()
    package = MagicMock()
    package.system_prompt = "sys"
    package.runtime_context = package.conversation_context = package.tool_context = ""
    package.user_input = "pause task 11"
    package.estimated_tokens.estimated_input_tokens = 10
    package.estimated_tokens.prompt_size_chars = 10
    pb.build.return_value = package

    executor = MagicMock()
    executor.execute_calls = AsyncMock(side_effect=exec_results)
    executor._context = MagicMock()
    executor._context.extra = {}
    return Dispatcher(conv, pb, pm, NOOP_HOOKS, EngineMetrics(), tool_executor=executor), executor


def test_read_round_may_continue_classification():
    from backend.ai.engine.dispatcher import _read_round_may_continue

    assert _read_round_may_continue([{"name": "task_list"}]) is True
    assert _read_round_may_continue([{"name": "task_inspect"}]) is True
    assert _read_round_may_continue([{"name": "get_bio"}]) is False
    assert _read_round_may_continue([{"name": "task_list"}, {"name": "get_bio"}]) is False
    assert _read_round_may_continue([]) is False


@pytest.mark.asyncio
async def test_task_list_round_continues_into_the_requested_transition():
    from backend.ai.providers.base import ProviderResponse
    from backend.ai.session.request import AIRequest

    ToolExecutionResult = _import_exec_result()

    listing = ProviderResponse(
        text="", provider_name="test", success=True,
        tool_calls=[{"id": "c1", "name": "task_list", "arguments": {}}],
        usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        metadata={"finish_reason": "tool_calls"},
    )
    transition = ProviderResponse(
        text="", provider_name="test", success=True,
        tool_calls=[{"id": "c2", "name": "task_transition", "arguments": {
            "task_id": 11, "action": "paused", "expected_version": 3,
        }}],
        usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        metadata={"finish_reason": "tool_calls"},
    )
    final = ProviderResponse(
        text="Task #11 paused.", provider_name="test", success=True,
        usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        metadata={"finish_reason": "stop"},
    )
    results = [
        [ToolExecutionResult(tool_name="task_list", success=True, message="Task #11 · v3")],
        [ToolExecutionResult(tool_name="task_transition", success=True, message="paused")],
    ]
    dispatcher, executor = _dispatcher_with([listing, transition, final], results)

    result = await dispatcher.dispatch(
        AIRequest(session_id="s1", message_id=1, owner_id=OWNER, user_message="pause task 11", chat_id=1)
    )

    assert executor.execute_calls.await_count == 2
    second = executor.execute_calls.await_args_list[1].args[0]
    assert second == [{"id": "c2", "name": "task_transition", "arguments": {
        "task_id": 11, "action": "paused", "expected_version": 3,
    }}]
    assert result.response == "Task #11 paused."


@pytest.mark.asyncio
async def test_read_only_request_still_delivers_the_verbatim_tool_result():
    from backend.ai.providers.base import ProviderResponse
    from backend.ai.session.request import AIRequest

    ToolExecutionResult = _import_exec_result()
    listing = ProviderResponse(
        text="", provider_name="test", success=True,
        tool_calls=[{"id": "c1", "name": "task_list", "arguments": {}}],
        usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        metadata={"finish_reason": "tool_calls"},
    )
    paraphrase = ProviderResponse(
        text="You seem to have no tasks at all.", provider_name="test", success=True,
        usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        metadata={"finish_reason": "stop"},
    )
    results = [[ToolExecutionResult(tool_name="task_list", success=True, message="Task #11 · v3")]]
    dispatcher, executor = _dispatcher_with([listing, paraphrase], results)

    result = await dispatcher.dispatch(
        AIRequest(session_id="s1", message_id=1, owner_id=OWNER, user_message="show my tasks", chat_id=1)
    )

    assert executor.execute_calls.await_count == 1
    assert result.response == "Task #11 · v3"


# ── C. Taskloom wizard navigation ───────────────────────────────────────────

@pytest.fixture()
def registered(monkeypatch):
    from backend.bot.handlers import taskloom
    from backend.helper import inline_engine

    inline_engine.set_owner_id(OWNER)
    taskloom.register(client=None, owner_id=OWNER, tz_str="UTC")
    return taskloom


def _rows(buttons) -> list[list[tuple[str, str]]]:
    rows = []
    for row in buttons:
        rendered = row if isinstance(row, list) else [row]
        pairs = []
        for button in rendered:
            raw = getattr(button, "data", button)
            data = raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)
            pairs.append((str(getattr(button, "text", "")), data))
        rows.append(pairs)
    return rows


def _render(taskloom, **changes):
    draft = taskloom.TaskDraft(timezone="UTC", **changes)
    return taskloom._wizard_render(draft)


def _step_draft(taskloom, step):
    draft = taskloom.TaskDraft(
        timezone="UTC", step=step, action="bio", content_mode=taskloom.AI_MODE
    )
    return draft


def _import_exec_result():
    from backend.ai.tools.executor import ToolExecutionResult

    return ToolExecutionResult


def test_wizard_back_is_always_a_step_action_and_never_a_panel_pop(registered):
    taskloom = registered
    for step in (taskloom.STEP_ACTION, taskloom.STEP_CONTENT, taskloom.STEP_DETAILS,
                 taskloom.STEP_SCHEDULE, taskloom.STEP_REVIEW):
        title, body, buttons = taskloom._wizard_render(_step_draft(taskloom, step))
        rows = _rows(buttons)
        data = [value for row in rows for _, value in row]
        assert "panel:_nav:back" not in data, f"{step} exposes a panel-stack Back"
        back_rows = [pairs for pairs in rows if pairs and pairs[0][0].startswith("← Back")]
        if step == taskloom.STEP_ACTION:
            # The first step has no Back at all: Cancel is the explicit exit.
            assert back_rows == []
            assert "panel:taskloom" in data
        else:
            assert len(back_rows) == 1, f"{step} must expose exactly one wizard Back"
            assert back_rows[0][0][1].startswith("action:taskloom_wizard:step:")


def test_wizard_back_targets_the_previous_step(registered):
    taskloom = registered
    expected = {
        taskloom.STEP_CONTENT: taskloom.STEP_ACTION,
        taskloom.STEP_DETAILS: taskloom.STEP_DETAILS,  # AI mode: back to content
        taskloom.STEP_SCHEDULE: taskloom.STEP_DETAILS,
        taskloom.STEP_REVIEW: taskloom.STEP_SCHEDULE,
    }
    for step, target in expected.items():
        _, _, buttons = taskloom._wizard_render(_step_draft(taskloom, step))
        data = [value for row in _rows(buttons) for _, value in row]
        if step == taskloom.STEP_DETAILS:
            assert f"action:taskloom_wizard:step:{taskloom.STEP_CONTENT}" in data
        else:
            assert f"action:taskloom_wizard:step:{target}" in data


def test_wizard_cancel_differs_from_back(registered):
    taskloom = registered
    _, _, buttons = taskloom._wizard_render(_step_draft(taskloom, taskloom.STEP_REVIEW))
    data = [value for row in _rows(buttons) for _, value in row]
    assert "panel:taskloom" in data  # Cancel returns to Taskloom
    assert "panel:_nav:close" in data  # Close closes the panel


@pytest.mark.asyncio
async def test_input_submission_preserves_the_current_step_and_draft(registered, monkeypatch):
    taskloom = registered
    from backend.helper import panels

    draft = taskloom.TaskDraft(
        timezone="UTC", step=taskloom.STEP_DETAILS, action="bio", content_mode=taskloom.AI_MODE
    )
    taskloom._store(draft, OWNER)

    async def _no_edit(*args, **kwargs):
        return None

    monkeypatch.setattr(taskloom, "_wizard_finish", _no_edit)
    handler = panels.get_input("taskloom_new", "source")["handler"]
    await handler("Ayanami Rei", 1, 2, 1, 2)

    stored = taskloom._draft(OWNER)
    assert stored.source == "Ayanami Rei"
    assert stored.step == taskloom.STEP_DETAILS
    assert stored.action == "bio" and stored.content_mode == taskloom.AI_MODE


@pytest.mark.asyncio
async def test_interval_input_preserves_the_schedule_step(registered, monkeypatch):
    taskloom = registered
    from backend.helper import panels

    taskloom._store(
        taskloom.TaskDraft(timezone="UTC", step=taskloom.STEP_SCHEDULE, action="bio",
                           content_mode=taskloom.AI_MODE),
        OWNER,
    )

    async def _no_edit(*args, **kwargs):
        return None

    monkeypatch.setattr(taskloom, "_wizard_finish", _no_edit)
    handler = panels.get_input("taskloom_new", "interval")["handler"]
    await handler("2", 1, 2, 1, 2)

    stored = taskloom._draft(OWNER)
    assert stored.interval_minutes == 2
    assert stored.schedule_type == "interval"
    assert stored.step == taskloom.STEP_SCHEDULE


@pytest.mark.asyncio
async def test_input_prompt_offers_no_panel_stack_back(monkeypatch):
    """The shared input prompt must not offer a Back that pops the panel
    stack (live: a wizard input's Back jumped to the Taskloom list)."""
    from backend.helper import panels

    captured = {}

    class _Event:
        async def answer(self):
            return None

        async def edit(self, text, buttons=None):
            captured["buttons"] = buttons

    monkeypatch.setattr(panels, "_safe_answer", lambda event: asyncio.sleep(0))
    monkeypatch.setattr(panels, "_sync_timer", lambda *a, **k: None)
    panels.register_input("_test_prompt", "field", {"handler": lambda *a: None, "prompt": "p"})
    await panels._handle_input(_Event(), "_test_prompt:field:", OWNER, 1, 2)

    data = []
    for row in captured["buttons"]:
        for button in row:
            raw = getattr(button, "data", button)
            data.append(raw.decode("utf-8") if isinstance(raw, bytes) else str(raw))
    assert "panel:_nav:back" not in data
    assert any(value.startswith("panel:_test_prompt") for value in data)


# ── D. task editing ─────────────────────────────────────────────────────────

def _bio_task_data(**overrides):
    value = task_data(
        label="Update Bio",
        schedule={"seconds": 120},
        actions=[{"name": "bio_set_text", "arguments": {"text": ""}}],
        ai_instruction="update my bio, with a randomly generated dialogue",
    )
    value.update(overrides)
    return value


@pytest.mark.asyncio
async def test_edit_updates_definition_and_bumps_version_once():
    from backend.ai.task_management import TaskManagementService

    repo = InMemoryTaskRepository()
    task = await repo.create_task(OWNER, _bio_task_data())
    service = TaskManagementService(repo, OWNER)
    candidate = {
        "label": "Update Bio", "schedule_type": "interval", "schedule": {"seconds": 600},
        "timezone": "UTC", "actions": [{"name": "bio_set_text", "arguments": {"text": ""}}],
        "notification_destination": {}, "ai_instruction": "update my bio, in English",
    }
    updated = await service.update_definition(task.id, task.version, candidate, BASE)

    assert updated is not None
    assert updated.version == task.version + 1
    assert updated.schedule == {"seconds": 600}
    assert updated.ai_instruction == "update my bio, in English"
    assert updated.next_run_at == BASE + timedelta(minutes=10)


@pytest.mark.asyncio
async def test_edit_rejects_a_stale_version():
    from backend.ai.task_management import TaskManagementService

    repo = InMemoryTaskRepository()
    task = await repo.create_task(OWNER, _bio_task_data())
    service = TaskManagementService(repo, OWNER)
    candidate = {
        "label": "Update Bio", "schedule_type": "interval", "schedule": {"seconds": 600},
        "timezone": "UTC", "actions": [{"name": "bio_set_text", "arguments": {"text": ""}}],
        "notification_destination": {}, "ai_instruction": None,
    }
    assert await service.update_definition(task.id, task.version + 7, candidate, BASE) is None
    current = await repo.get_task(OWNER, task.id)
    assert current.version == task.version
    assert current.schedule == task.schedule


@pytest.mark.asyncio
async def test_edit_discards_only_future_unstarted_occurrences():
    from backend.ai.task_management import TaskManagementService

    repo = InMemoryTaskRepository()
    task = await repo.create_task(OWNER, _bio_task_data())
    history = await repo.create_occurrence(OWNER, {
        "task_id": task.id, "occurrence_key": "past", "definition_version": 1,
        "action_snapshot": task.actions, "scheduled_for": BASE - timedelta(minutes=2),
        "status": "succeeded",
    })
    prepared = await repo.create_occurrence(OWNER, {
        "task_id": task.id, "occurrence_key": "future", "definition_version": 1,
        "action_snapshot": task.actions, "scheduled_for": BASE + timedelta(minutes=2),
    })
    service = TaskManagementService(repo, OWNER)
    candidate = {
        "label": "Update Bio", "schedule_type": "interval", "schedule": {"seconds": 600},
        "timezone": "UTC", "actions": [{"name": "bio_set_text", "arguments": {"text": ""}}],
        "notification_destination": {},
    }
    updated = await service.update_definition(task.id, task.version, candidate, BASE)
    assert updated is not None

    assert await repo.get_occurrence(OWNER, task.id, "future") is None
    kept = await repo.get_occurrence(OWNER, task.id, "past")
    assert kept is not None and kept.status == "succeeded"
    assert kept.action_snapshot == history.action_snapshot
    assert kept.definition_version == 1


def test_wizard_review_shows_the_selected_font(registered):
    taskloom = registered
    draft = taskloom.TaskDraft(
        timezone="UTC", step=taskloom.STEP_REVIEW, action="message",
        content_mode=taskloom.STATIC_MODE, text="Hello", font="script",
        schedule_type="interval", interval_minutes=5,
    )
    _, body, _ = taskloom._wizard_render(draft)
    assert "Font" in body


def test_send_message_edit_keeps_the_raw_text_and_the_font(registered):
    taskloom = registered
    draft = taskloom.TaskDraft(
        timezone="UTC", action="message", content_mode=taskloom.STATIC_MODE,
        text="Hello", font="script", schedule_type="interval", interval_minutes=5,
    )
    candidate = taskloom.task_wizard.build_candidate(draft, 0, BASE)
    assert candidate["actions"] == [
        {"name": "send_message", "arguments": {"text": "Hello", "font": "script"}}
    ]


def test_font_is_applied_by_the_canonical_registry_at_send_time():
    from backend.helper.font_style import apply_font, is_valid_font

    assert is_valid_font("script") and not is_valid_font("not-a-font")
    assert apply_font("Hello", "script") != "Hello"
    assert apply_font("Hello", "default") == "Hello"


def test_invalid_font_is_rejected_by_the_candidate_contract():
    from backend.ai.task_candidate import TaskCandidate, TaskCandidateError

    with pytest.raises(TaskCandidateError):
        TaskCandidate.from_untrusted({
            "label": "msg", "schedule_type": "interval", "schedule": {"seconds": 60},
            "timezone": "UTC",
            "actions": [{"name": "send_message", "arguments": {"text": "hi", "font": "comic"}}],
            "notification_destination": {},
        })


@pytest.mark.asyncio
async def test_send_tool_applies_the_stored_font(monkeypatch):
    from backend.ai.tools.context import ToolContext
    from backend.ai.tools.message import SendMessageTool

    sent = {}

    class _Telegram:
        async def send_message(self, chat_id, text):
            sent["chat_id"], sent["text"] = chat_id, text

    tool = SendMessageTool(ToolContext(None, OWNER, "UTC"))
    result = await tool.execute(
        ToolContext(telegram=_Telegram(), owner_id=OWNER, tz_str="UTC", extra={"chat_id": 5}),
        {"text": "Hello", "font": "script"},
    )
    assert result.success is True
    assert sent["text"] != "Hello"

    bad = await tool.execute(
        ToolContext(telegram=_Telegram(), owner_id=OWNER, tz_str="UTC", extra={"chat_id": 5}),
        {"text": "Hello", "font": "comic"},
    )
    assert bad.success is False


@pytest.mark.asyncio
async def test_draft_from_task_prefills_and_rejects_unrepresentable_schedules():
    from backend.ai.task_wizard import TaskWizardError, draft_from_task, review_lines

    repo = InMemoryTaskRepository()
    bio = await repo.create_task(OWNER, _bio_task_data())
    draft = draft_from_task(bio)
    assert draft.editing_task_id == bio.id and draft.editing_version == bio.version
    assert draft.action == "bio" and draft.content_mode == "ai"
    assert draft.schedule_type == "interval" and draft.interval_minutes == 2
    assert review_lines(draft, 0, BASE)

    message = await repo.create_task(
        OWNER,
        task_data(
            label="msg",
            actions=[{"name": "send_message", "arguments": {"text": "Hello", "font": "script"}}],
            ai_instruction=None,
        ),
    )
    message_draft = draft_from_task(message)
    assert message_draft.content_mode == "static"
    assert message_draft.text == "Hello" and message_draft.font == "script"

    event = await repo.create_task(
        OWNER,
        task_data(
            label="event", schedule_type="event",
            schedule={"trigger": {"kind": "message", "chat": "this_chat"}},
            ai_instruction=None,
        ),
    )
    with pytest.raises(TaskWizardError):
        draft_from_task(event)


# ── E. schema contract / fallback honesty ───────────────────────────────────

PREPARED = {
    "kind": "prepared_action",
    "definition_version": 1,
    "prepared_at": "2026-09-12T00:00:00+00:00",
    "action": {"name": "bio_set_text", "arguments": {"text": "hi"}},
}


class _DriftError(RuntimeError):
    code = "PGRST204"

    def __init__(self) -> None:
        super().__init__(
            "Could not find the 'preparation_metadata' column of "
            "'ai_task_occurrences' in the schema cache"
        )


class _FakeQuery:
    def __init__(self, client, table):
        self.client, self.table_name = client, table
        self.filters, self.payload, self.operation, self.single = [], None, None, False

    def select(self, *a, **k):
        self.operation = "select"
        return self

    def insert(self, payload):
        self.payload, self.operation = payload, "insert"
        return self

    def update(self, payload):
        self.payload, self.operation = payload, "update"
        return self

    def delete(self):
        self.operation = "delete"
        return self

    def eq(self, key, value):
        self.filters.append((key, value))
        return self

    def gt(self, key, value):
        self.filters.append(("__gt__" + key, value))
        return self

    def in_(self, key, values):
        self.filters.append((key, set(values)))
        return self

    def order(self, *a, **k):
        return self

    def limit(self, value):
        self.limit_value = value
        return self

    def maybe_single(self):
        self.single = True
        return self

    def _matches(self):
        rows = self.client.rows[self.table_name]
        kept = []
        for row in rows:
            ok = True
            for key, value in self.filters:
                if key.startswith("__gt__"):
                    if not (row.get(key[6:]) > value):
                        ok = False
                elif isinstance(value, set):
                    if row.get(key) not in value:
                        ok = False
                elif row.get(key) != value:
                    ok = False
            if ok:
                kept.append(row)
        return kept

    def execute(self):
        if self.client.error:
            raise self.client.error
        if self.payload and self.client.drift_column in self.payload:
            raise _DriftError()
        matches = self._matches()
        if self.operation == "update":
            for row in matches:
                row.update(self.payload)
            return SimpleNamespace(data=(matches[0] if matches else None))
        if self.operation == "delete":
            for row in matches:
                self.client.rows[self.table_name].remove(row)
            return SimpleNamespace(data=list(matches))
        return SimpleNamespace(data=(matches[0] if self.single and matches else matches))


class _FakeClient:
    def __init__(self, occurrence_rows=None, error=None, drift_column="preparation_metadata"):
        self.rows = {"ai_tasks": [], "ai_task_occurrences": [dict(r) for r in (occurrence_rows or [])]}
        self.error, self.drift_column = error, drift_column

    def table(self, name):
        return _FakeQuery(self, name)


def row_occurrence(**overrides):
    row = {
        "id": 9, "task_id": 7, "owner_id": 10, "occurrence_key": "k1",
        "definition_version": 1, "action_snapshot": [{"name": "bio_set_text", "arguments": {}}],
        "scheduled_for": "2026-09-12T12:00:00+00:00", "attempt": 1, "status": "running",
        "claimed_at": None, "started_at": None, "finished_at": None, "retry_at": None,
        "error_metadata": {}, "result_metadata": {},
        "created_at": "2026-09-12T12:00:00+00:00", "updated_at": "2026-09-12T12:00:00+00:00",
    }
    row.update(overrides)
    return row


@pytest.mark.asyncio
async def test_missing_audit_column_does_not_lose_the_durable_transition(caplog):
    client = _FakeClient([row_occurrence()])
    repo = SupabaseTaskRepository(client, InMemoryTaskRepository())
    with caplog.at_level(logging.WARNING):
        updated = await repo.transition_occurrence(
            10, 7, "k1", "succeeded",
            result_metadata={"terminal_status": "succeeded"},
            preparation_metadata=PREPARED,
        )

    assert updated is not None, "the durable state transition was lost to a schema drift"
    assert updated.status == "succeeded"
    assert updated.result_metadata == {"terminal_status": "succeeded"}
    assert repo.fallback_active is False, "the durable store was wrongly marked degraded"
    assert "TASK_OCCURRENCE_AUDIT_FIELD_DROPPED" in caplog.text
    assert "preparation_metadata" in caplog.text


@pytest.mark.asyncio
async def test_missing_required_column_is_never_silently_stripped():
    client = _FakeClient([row_occurrence()], drift_column="status")
    repo = SupabaseTaskRepository(client, InMemoryTaskRepository())
    updated = await repo.transition_occurrence(10, 7, "k1", "succeeded")
    # An update that cannot be written without a REQUIRED column is not
    # reported as a durable success: the repository degrades honestly.
    assert updated is None
    assert repo.fallback_active is True


@pytest.mark.asyncio
async def test_same_status_write_is_not_retried_without_the_audit_field():
    """prepare_ahead's claim-status write carries ONLY the prepared action:
    dropping it would claim a durable preparation that never happened."""
    client = _FakeClient([row_occurrence(status="claimed")])
    repo = SupabaseTaskRepository(client, InMemoryTaskRepository())
    result = await repo.transition_occurrence(
        10, 7, "k1", "claimed", preparation_metadata=PREPARED
    )
    assert result is None
    stored = client.rows["ai_task_occurrences"][0]
    assert stored.get("preparation_metadata") is None


@pytest.mark.asyncio
async def test_genuine_store_failure_still_classifies_as_unavailable():
    repo = SupabaseTaskRepository(
        _FakeClient([row_occurrence()], error=RuntimeError("connection refused")),
        InMemoryTaskRepository(),
    )
    await repo.get_occurrence(10, 7, "k1")
    assert repo.fallback_active is True
    assert repo.fallback_reason == FALLBACK_REASON_UNAVAILABLE


@pytest.mark.asyncio
async def test_local_resource_failure_still_classifies_as_local_resource():
    repo = SupabaseTaskRepository(
        _FakeClient([row_occurrence()], error=OSError(11, "Resource temporarily unavailable")),
        InMemoryTaskRepository(),
    )
    await repo.get_occurrence(10, 7, "k1")
    assert repo.fallback_reason == FALLBACK_REASON_LOCAL_RESOURCE


@pytest.mark.asyncio
async def test_next_run_hint_is_advisory_and_never_degrades():
    repo = SupabaseTaskRepository(
        _FakeClient([], error=RuntimeError("connection refused")),
        InMemoryTaskRepository(),
    )
    assert await repo.next_run_hint(10) is None
    assert repo.fallback_active is False


# ── D.2 edit ergonomics (panel entry + end-to-end wizard edit) ──────────────

@pytest.fixture()
def wizard(monkeypatch):
    import backend.ai.database.manager as manager
    import backend.bot.handlers.taskloom as handler
    from backend.helper import inline_engine

    repo = InMemoryTaskRepository()

    class _Manager:
        task = repo

    monkeypatch.setattr(manager, "get_repository_manager", lambda: _Manager())
    inline_engine.set_owner_id(OWNER)
    handler._drafts.clear()
    handler.register(client=None, owner_id=OWNER, tz_str="Asia/Tehran")
    yield handler, repo
    handler._drafts.clear()


def test_task_detail_panel_offers_the_edit_entry(wizard):
    taskloom, repo = wizard

    async def _run():
        task = await repo.create_task(OWNER, _bio_task_data())
        _title, _body, rows = await taskloom._task_detail_panel(None, str(task.id))
        return task, [value for row in _rows(rows) for _, value in row]

    task, data = asyncio.new_event_loop().run_until_complete(_run())
    assert f"panel:taskloom_new:edit:{task.id}" in data


def test_wizard_edit_updates_the_same_task_definition(wizard):
    taskloom, repo = wizard

    async def _run():
        task = await repo.create_task(OWNER, _bio_task_data(schedule={"seconds": 120}))
        title, _body, _rows = await taskloom._wizard_panel(None, f"edit:{task.id}")
        draft = taskloom._draft(OWNER)
        opened = (title, draft.editing_task_id, draft.editing_version,
                  draft.interval_minutes, draft.action)
        await taskloom._wizard_input_handler("interval")("10", 1, 2, 0, 0)
        await taskloom._wizard_action(None, "step:review", 1)
        _t, body, _r = await taskloom._wizard_action(None, "create", 1)
        tasks = await repo.list_tasks(OWNER)
        return opened, body, tasks

    opened, body, tasks = asyncio.new_event_loop().run_until_complete(_run())
    assert opened[0] == f"✎ Edit task #1"
    assert opened[1] == 1 and opened[3] == 2 and opened[4] == "bio"
    assert "Task #1 updated" in body
    # The SAME task row was edited (no second task created) and the version
    # incremented exactly once.
    assert len(tasks) == 1
    assert tasks[0].schedule == {"seconds": 600}
    assert tasks[0].version == 2


def test_wizard_edit_refuses_a_stale_form(wizard):
    taskloom, repo = wizard

    async def _run():
        task = await repo.create_task(OWNER, _bio_task_data())
        await taskloom._wizard_panel(None, f"edit:{task.id}")
        # A concurrent writer changes the task after the form was opened.
        await repo.update_task(OWNER, task.id, task.version, {"label": "changed elsewhere"})
        _t, body, _r = await taskloom._wizard_action(None, "create", 1)
        current = await repo.get_task(OWNER, task.id)
        return body, current

    body, current = asyncio.new_event_loop().run_until_complete(_run())
    assert "stale version" in body
    assert current.label == "changed elsewhere"
    assert current.schedule == {"seconds": 120}


# ── B.2 deterministic task-management action contract ───────────────────────

def test_json_task_transition_maps_to_the_registered_tool_contract():
    from backend.ai.actions import KIND_EXECUTABLE, resolve_tool_calls, validate_action

    result = validate_action({
        "action": "task_transition", "task_id": 11,
        "action_status": "paused", "expected_version": 3,
    })
    assert result.kind == KIND_EXECUTABLE
    assert resolve_tool_calls(result) == [{
        "name": "task_transition",
        "arguments": {"task_id": 11, "action": "paused", "expected_version": 3},
    }]


def test_json_task_lifecycle_without_a_target_is_rejected():
    from backend.ai.actions import KIND_EXECUTABLE, validate_action

    assert validate_action({"action": "task_delete", "expected_version": 3}).kind != KIND_EXECUTABLE
    assert validate_action({"action": "task_transition", "task_id": 11}).kind != KIND_EXECUTABLE
    assert validate_action({"action": "task_list", "status": "deleted"}).kind != KIND_EXECUTABLE


# ── C.2 every wizard input keeps its step and the rest of the draft ─────────

@pytest.mark.parametrize(
    "field,text,step,action,mode,check",
    [
        ("source", "Ayanami Rei", "details", "bio", "ai", lambda d: d.source == "Ayanami Rei"),
        ("maxlen", "59", "details", "bio", "ai", lambda d: d.max_length == 59),
        ("text", "Hello there", "details", "message", "static", lambda d: d.text == "Hello there"),
        ("interval", "7", "schedule", "message", "static", lambda d: d.interval_minutes == 7),
        ("daily", "09:30", "schedule", "message", "static", lambda d: d.clock == "09:30"),
        ("weekly", "Monday 09:30", "schedule", "message", "static",
         lambda d: d.weekday == 0 and d.clock == "09:30"),
        ("once", "2027-01-05 09:30", "schedule", "message", "static",
         lambda d: d.once_at.startswith("2027-01-05")),
        ("tz", "Asia/Tehran", "schedule", "message", "static",
         lambda d: d.timezone == "Asia/Tehran"),
        ("font", "script", "details", "message", "static", lambda d: d.font == "script"),
    ],
)
def test_every_input_preserves_its_step_and_the_unrelated_draft(
    registered, monkeypatch, field, text, step, action, mode, check
):
    taskloom = registered
    from backend.helper import panels

    step_name = taskloom.STEP_DETAILS if step == "details" else taskloom.STEP_SCHEDULE
    content_mode = taskloom.AI_MODE if mode == "ai" else taskloom.STATIC_MODE
    taskloom._store(
        taskloom.TaskDraft(
            timezone="UTC", step=step_name, action=action, content_mode=content_mode,
        ),
        OWNER,
    )

    async def _no_edit(*args, **kwargs):
        return None

    monkeypatch.setattr(taskloom, "_wizard_finish", _no_edit)
    handler = panels.get_input("taskloom_new", field)["handler"]
    asyncio.new_event_loop().run_until_complete(handler(text, 1, 2, 1, 2))

    stored = taskloom._draft(OWNER)
    assert stored.step == step_name
    assert check(stored)
    # The unrelated draft state survives an input.
    assert stored.action == action and stored.content_mode == content_mode


def test_back_from_review_keeps_every_entered_value(registered):
    taskloom = registered
    draft = taskloom.TaskDraft(
        timezone="UTC", step=taskloom.STEP_REVIEW, action="bio",
        content_mode=taskloom.AI_MODE, source="Ayanami Rei", language="en",
        max_length=59, schedule_type="interval", interval_minutes=2,
        editing_task_id=0,
    )
    taskloom._store(draft, OWNER)
    _title, _body, _rows = asyncio.new_event_loop().run_until_complete(
        taskloom._wizard_action(None, "step:schedule", 1)
    )
    stored = taskloom._draft(OWNER)
    assert stored.step == taskloom.STEP_SCHEDULE
    assert (stored.source, stored.language, stored.max_length) == ("Ayanami Rei", "en", 59)
    assert stored.interval_minutes == 2


# ── B.3 stale CAS feedback (so the next round can actually finish) ──────────

@pytest.mark.asyncio
async def test_stale_transition_reports_the_current_version(monkeypatch):
    import backend.ai.database.manager as manager
    from backend.ai.tools.context import ToolContext
    from backend.ai.tools.task_management_tools import TaskTransitionTool

    repo = InMemoryTaskRepository()
    task = await repo.create_task(OWNER, task_data())

    class _Manager:
        task = repo

    monkeypatch.setattr(manager, "get_repository_manager", lambda: _Manager())
    tool = TaskTransitionTool(ToolContext(None, OWNER, "UTC"))
    result = await tool.execute(
        ToolContext(None, OWNER, "UTC"),
        {"task_id": task.id, "action": "paused", "expected_version": task.version + 5},
    )
    assert result.success is False
    assert f"expected_version={task.version}" in result.message
    assert result.data.get("current_version") == task.version
    assert (await repo.get_task(OWNER, task.id)).status == "active"


@pytest.mark.asyncio
async def test_stale_delete_reports_the_current_version(monkeypatch):
    import backend.ai.database.manager as manager
    from backend.ai.tools.context import ToolContext
    from backend.ai.tools.task_management_tools import TaskDeleteTool

    repo = InMemoryTaskRepository()
    task = await repo.create_task(OWNER, task_data())

    class _Manager:
        task = repo

    monkeypatch.setattr(manager, "get_repository_manager", lambda: _Manager())
    tool = TaskDeleteTool(ToolContext(None, OWNER, "UTC"))
    result = await tool.execute(
        ToolContext(None, OWNER, "UTC"),
        {"task_id": task.id, "expected_version": task.version + 5},
    )
    assert result.success is False
    assert result.data.get("current_version") == task.version
    assert (await repo.get_task(OWNER, task.id)) is not None


@pytest.mark.asyncio
async def test_transition_accepts_the_json_action_field_name(monkeypatch):
    import backend.ai.database.manager as manager
    from backend.ai.tools.context import ToolContext
    from backend.ai.tools.task_management_tools import TaskTransitionTool

    repo = InMemoryTaskRepository()
    task = await repo.create_task(OWNER, task_data())

    class _Manager:
        task = repo

    monkeypatch.setattr(manager, "get_repository_manager", lambda: _Manager())
    tool = TaskTransitionTool(ToolContext(None, OWNER, "UTC"))
    result = await tool.execute(
        ToolContext(None, OWNER, "UTC"),
        {"task_id": task.id, "action_status": "paused", "expected_version": task.version},
    )
    assert result.success is True
    assert (await repo.get_task(OWNER, task.id)).status == "paused"
