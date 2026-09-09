"""Prepare-ahead execution regression tests.

Contract under test: for a recurring AI-assisted task, content preparation
(provider rounds + deterministic validation) happens BEFORE the occurrence
boundary and is persisted durably; the Telegram side effect happens exactly
once AT the boundary through the single
Scheduler -> TaskExecutionCoordinator -> ToolExecutor authority.

No Telegram mutation may occur during preparation, preparation is
idempotent across wakes/restarts, static tasks never enter the path, and a
failed preparation falls back to the existing occurrence-time contract
(retryable, bounded) instead of executing a guessed action.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from backend.ai.database.task_repository import InMemoryTaskRepository
from backend.ai.preparation_policy import (
    PreparationPolicyError,
    derive_policy,
    validate_content,
)
from backend.ai.task_execution import (
    MAX_PREPARATION_ATTEMPTS,
    AIActionPreparator,
    TaskExecutionCoordinator,
)
from backend.ai.task_scheduler import TaskScheduler, occurrence_key
from backend.ai.tools.base import PermissionLevel, ToolResult
from backend.ai.tools.context import ToolContext
from backend.ai.tools.executor import ToolExecutor
from backend.ai.tools.registry import ToolRegistry

NOW = datetime(2026, 9, 8, 12, 0, 0, tzinfo=timezone.utc)
INSTRUCTION = "Write exactly 10 characters of dialogue"


class FakeTool:
    permission_level = PermissionLevel.READ_WRITE
    long_running = False
    safe = True
    return_type = "object"
    description = "test"
    parameters = {}

    def __init__(self, name, calls, success=True):
        self.name, self.calls, self.success = name, calls, success

    async def execute(self, context, arguments):
        self.calls.append((self.name, arguments, context.owner_id))
        return ToolResult(self.success, "ok" if self.success else "failed")


class CountingPreparator:
    """Stands in for the provider round; returns round-specific content."""

    def __init__(self):
        self.rounds = 0

    async def prepare_validated(self, instruction, templates, *, owner_id, tz_str):
        self.rounds += 1
        # Exactly 10 chars, distinct per round, so a test can prove WHICH
        # round's content the tool actually received.
        content = {1: "aaaaaaaaaa", 2: "bbbbbbbbbb"}.get(self.rounds, "cccccccccc")
        return [{"name": t["name"], "arguments": {"text": content}} for t in templates]


def ai_task_data(**overrides):
    value = {
        "label": "Recurring bio",
        "schedule_type": "interval",
        "schedule": {"seconds": 60},
        "timezone": "UTC",
        "next_run_at": NOW,
        "actions": [{"name": "set_bio", "arguments": {}}],
        "notification_destination": {"chat_id": 1},
        "ai_instruction": INSTRUCTION,
    }
    value.update(overrides)
    return value


def build_coordinator(repo, preparator, owner=1, calls=None):
    registry = ToolRegistry()
    if calls is None:
        calls = []
    registry.register(FakeTool("set_bio", calls))
    ctx = ToolContext(None, owner, "UTC")
    return TaskExecutionCoordinator(repo, ToolExecutor(registry, ctx), owner, ctx, preparator=preparator)


async def drain_preparations(scheduler: TaskScheduler) -> None:
    """Wait for the scheduler's tracked preparation tasks to settle."""
    pending = [t for t in scheduler._preparations.values() if not t.done()]
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


# ── Preparation never touches Telegram and is idempotent ────────────────────


@pytest.mark.asyncio
async def test_prepare_ahead_persists_content_without_executing():
    """prepare_ahead stores a validated PreparedAction durably and NEVER
    runs the tool."""
    repo = InMemoryTaskRepository()
    task = await repo.create_task(1, ai_task_data())
    boundary = NOW + timedelta(seconds=60)
    occurrence = await repo.create_occurrence(1, {
        "task_id": task.id,
        "occurrence_key": occurrence_key(task.id, boundary),
        "definition_version": task.version,
        "action_snapshot": task.actions,
        "scheduled_for": boundary,
    })
    calls = []
    coordinator = build_coordinator(repo, CountingPreparator(), calls=calls)
    prepared = await coordinator.prepare_ahead(occurrence)

    assert prepared is not None and prepared[0]["name"] == "set_bio"
    assert calls == []  # preparation performed NO side effect
    stored = await repo.get_occurrence(1, task.id, occurrence.occurrence_key)
    assert stored.preparation_metadata["kind"] == "prepared_action"
    assert stored.preparation_metadata["definition_version"] == task.version
    assert stored.status == "claimed"  # still waiting for its boundary


@pytest.mark.asyncio
async def test_prepare_ahead_skips_static_tasks():
    repo = InMemoryTaskRepository()
    task = await repo.create_task(1, ai_task_data(ai_instruction=None))
    coordinator = build_coordinator(repo, CountingPreparator())
    # The scheduler gate short-circuits static tasks before any provider work.
    scheduler = TaskScheduler(repo, 1, execution_coordinator=coordinator)
    await scheduler._prepare_next_ahead(task, NOW + timedelta(seconds=60), NOW)
    assert scheduler._preparations == {}


@pytest.mark.asyncio
async def test_prepare_ahead_requires_coordinator_support():
    repo = InMemoryTaskRepository()
    task = await repo.create_task(1, ai_task_data())
    scheduler = TaskScheduler(repo, 1, execution_coordinator=SimpleNamespace(execute=lambda o: None))
    await scheduler._prepare_next_ahead(task, NOW + timedelta(seconds=60), NOW)
    assert scheduler._preparations == {}


@pytest.mark.asyncio
async def test_prepare_ahead_ignores_boundaries_beyond_horizon():
    repo = InMemoryTaskRepository()
    task = await repo.create_task(1, ai_task_data(schedule={"seconds": 3600}))
    coordinator = build_coordinator(repo, CountingPreparator())
    scheduler = TaskScheduler(repo, 1, execution_coordinator=coordinator)
    await scheduler._prepare_next_ahead(task, NOW + timedelta(hours=1), NOW)
    assert scheduler._preparations == {}
    assert await repo.list_occurrences(1, task.id) == []


@pytest.mark.asyncio
async def test_prepare_ahead_is_idempotent_across_wakes():
    """A second arm attempt for the same boundary never re-runs preparation."""
    repo = InMemoryTaskRepository()
    task = await repo.create_task(1, ai_task_data())
    preparator = CountingPreparator()
    coordinator = build_coordinator(repo, preparator)
    scheduler = TaskScheduler(repo, 1, execution_coordinator=coordinator)
    boundary = NOW + timedelta(seconds=60)

    await scheduler._prepare_next_ahead(task, boundary, NOW)
    await drain_preparations(scheduler)
    await scheduler._prepare_next_ahead(task, boundary, NOW)
    await drain_preparations(scheduler)

    assert preparator.rounds == 1
    stored = await repo.get_occurrence(1, task.id, occurrence_key(task.id, boundary))
    assert stored.preparation_metadata


@pytest.mark.asyncio
async def test_prepare_ahead_failure_leaves_occurrence_unprepared_and_unexecuted():
    """A failed preparation logs and returns without side effects; the
    occurrence stays claimed for the honest occurrence-time path."""
    repo = InMemoryTaskRepository()
    task = await repo.create_task(1, ai_task_data())
    occurrence = await repo.create_occurrence(1, {
        "task_id": task.id,
        "occurrence_key": "k",
        "definition_version": task.version,
        "action_snapshot": task.actions,
        "scheduled_for": NOW,
    })

    class BrokenPreparator:
        async def prepare_validated(self, *args, **kwargs):
            raise RuntimeError("provider down")

    calls = []
    coordinator = build_coordinator(repo, BrokenPreparator(), calls=calls)
    assert await coordinator.prepare_ahead(occurrence) is None
    stored = await repo.get_occurrence(1, task.id, "k")
    assert stored.preparation_metadata == {}
    assert calls == []


# ── Full lifecycle: prepare before the boundary, execute once at it ─────────


def _soon(seconds: int) -> datetime:
    """A real-clock future boundary (recovery compares against now())."""
    return datetime.now(timezone.utc) + timedelta(seconds=seconds)


@pytest.mark.asyncio
async def test_recurring_ai_task_prepares_ahead_and_executes_once_at_boundary():
    """The wake at T executes occurrence N, then prepares N+1 during the
    interval. At T+60 a FRESH scheduler (restart simulation) executes the
    pre-created, pre-prepared occurrence exactly once — with NO new provider
    round."""
    repo = InMemoryTaskRepository()
    t1 = _soon(61)
    t2 = t1 + timedelta(seconds=60)
    task = await repo.create_task(1, ai_task_data(next_run_at=t1))
    preparator = CountingPreparator()
    calls = []
    coordinator = build_coordinator(repo, preparator, calls=calls)
    scheduler = TaskScheduler(repo, 1, execution_coordinator=coordinator)

    # Wake at T: occurrence N executes; occurrence N+1 (T+60) is prepared ahead.
    assert await scheduler.run_once(t1) == 1
    await drain_preparations(scheduler)
    await scheduler.stop()

    next_key = occurrence_key(task.id, t2)
    prepared_row = await repo.get_occurrence(1, task.id, next_key)
    assert prepared_row.status == "claimed"
    assert prepared_row.preparation_metadata["kind"] == "prepared_action"
    assert [call[0] for call in calls] == ["set_bio"]  # only occurrence N ran
    rounds_after_first_wake = preparator.rounds

    # Restart: a new scheduler over the same durable state.
    restarted = TaskScheduler(repo, 1, execution_coordinator=coordinator)
    assert await restarted.recover() == 0  # future claimed occurrence untouched

    # Wake at T+60: the occurrence executes exactly once from its prepared
    # action — no new provider round at the boundary.
    assert await restarted.run_once(t2) == 1
    await drain_preparations(restarted)
    await restarted.stop()

    executed = await repo.get_occurrence(1, task.id, next_key)
    assert executed.status == "succeeded"
    assert [call[0] for call in calls].count("set_bio") == 2  # N at T, N+1 at T+60
    # Content-level proof: the boundary executed the action prepared BEFORE
    # it (round 2), not a fresh boundary-time provider round. Round 3 is the
    # legitimate prepare-ahead of occurrence N+2.
    assert calls[1][1] == {"text": "bbbbbbbbbb"}
    assert preparator.rounds == rounds_after_first_wake + 1


@pytest.mark.asyncio
async def test_duplicate_wakes_execute_prepared_occurrence_once():
    repo = InMemoryTaskRepository()
    boundary = _soon(61)
    task = await repo.create_task(1, ai_task_data(next_run_at=boundary))
    calls = []
    coordinator = build_coordinator(repo, CountingPreparator(), calls=calls)
    scheduler = TaskScheduler(repo, 1, execution_coordinator=coordinator)

    # A prior wake already prepared this boundary's occurrence durably.
    await scheduler._prepare_next_ahead(task, boundary, boundary - timedelta(seconds=60))
    await drain_preparations(scheduler)

    assert await scheduler.run_once(boundary) == 1
    assert await scheduler.run_once(boundary + timedelta(seconds=1)) == 0
    await drain_preparations(scheduler)
    await scheduler.stop()

    executed = await repo.get_occurrence(1, task.id, occurrence_key(task.id, boundary))
    assert executed.status == "succeeded"
    assert [call[0] for call in calls].count("set_bio") == 1


@pytest.mark.asyncio
async def test_stale_preparation_is_rejected_at_the_boundary():
    """A persisted prepared action from an OLD task version must never run:
    the boundary falls back to honest occurrence-time preparation."""
    repo = InMemoryTaskRepository()
    task = await repo.create_task(1, ai_task_data())
    boundary = NOW + timedelta(seconds=60)
    occurrence = await repo.create_occurrence(1, {
        "task_id": task.id,
        "occurrence_key": occurrence_key(task.id, boundary),
        "definition_version": task.version,
        "action_snapshot": task.actions,
        "scheduled_for": boundary,
    })
    calls = []
    coordinator = build_coordinator(repo, CountingPreparator(), calls=calls)
    assert await coordinator.prepare_ahead(occurrence) is not None

    # The owner edits the task after preparation -> definition version bumps.
    await repo.update_task(1, task.id, task.version, {"label": "Edited"})
    stale = await repo.get_occurrence(1, task.id, occurrence.occurrence_key)
    assert coordinator._prepared_from_metadata(stale, await repo.get_task(1, task.id)) is None


class ScriptedProviders:
    """Provider manager double returning a fixed response per round."""

    def __init__(self, texts):
        self.texts = list(texts)
        self.rounds = 0

    async def chat(self, messages, tools=None):
        from backend.ai.providers.base.contract import ProviderResponse

        text = self.texts[min(self.rounds, len(self.texts) - 1)]
        self.rounds += 1
        return ProviderResponse(text=text, provider_name="stub", success=True)


def _stub_coordinator(repo, providers, calls):
    registry = ToolRegistry()
    registry.register(FakeTool("set_bio", calls))
    ctx = ToolContext(None, 1, "UTC")
    preparator = AIActionPreparator(providers)
    return TaskExecutionCoordinator(repo, ToolExecutor(registry, ctx), 1, ctx, preparator=preparator)


@pytest.mark.asyncio
async def test_preparation_policy_rejects_invalid_content_before_execution():
    """Content violating the deterministic policy is regenerated within the
    bounded attempt count and never executed; exhaustion fails closed."""
    repo = InMemoryTaskRepository()
    task = await repo.create_task(1, ai_task_data())
    occurrence = await repo.create_occurrence(1, {
        "task_id": task.id,
        "occurrence_key": "k",
        "definition_version": task.version,
        "action_snapshot": task.actions,
        "scheduled_for": NOW,
    })
    await repo.claim_occurrence(1, task.id, "k")

    calls = []
    providers = ScriptedProviders(['{"actions": [{"name": "set_bio", "arguments": {"text": "too short"}}]}'])
    coordinator = _stub_coordinator(repo, providers, calls)
    result = await coordinator.execute(await repo.get_occurrence(1, task.id, "k"))

    assert not result.success
    assert providers.rounds == MAX_PREPARATION_ATTEMPTS  # bounded, never multiplied
    assert calls == []  # no Telegram mutation for invalid content
    stored = await repo.get_occurrence(1, task.id, "k")
    assert stored.status == "failed"  # unclassified preparation failure is permanent, never a guess


@pytest.mark.asyncio
async def test_regenerated_content_passes_policy_and_executes_once():
    """First provider round violates the policy, the regenerated round is
    valid: exactly one execution with the valid content."""
    repo = InMemoryTaskRepository()
    task = await repo.create_task(1, ai_task_data())
    occurrence = await repo.create_occurrence(1, {
        "task_id": task.id,
        "occurrence_key": "k",
        "definition_version": task.version,
        "action_snapshot": task.actions,
        "scheduled_for": NOW,
    })
    await repo.claim_occurrence(1, task.id, "k")

    calls = []
    providers = ScriptedProviders([
        '{"actions": [{"name": "set_bio", "arguments": {"text": "invalid!!"}}]}',
        '{"actions": [{"name": "set_bio", "arguments": {"text": "abcdefghij"}}]}',
    ])
    coordinator = _stub_coordinator(repo, providers, calls)
    result = await coordinator.execute(await repo.get_occurrence(1, task.id, "k"))

    assert result.success and result.status == "succeeded"
    assert [call[0] for call in calls] == ["set_bio"]
    assert calls[0][1] == {"text": "abcdefghij"}
    assert (await repo.get_occurrence(1, task.id, "k")).status == "succeeded"


# ── Deterministic content policy (units) ────────────────────────────────────


def test_policy_derives_exact_length_and_language_from_instruction():
    policy = derive_policy(INSTRUCTION)
    assert policy.exact_length == 10 and policy.language is None and policy.active

    persian = derive_policy("هر دقیقه بیو را با یک دیالوگ ۵۰ کاراکتری فارسی عوض کن")
    assert persian.exact_length == 50 and persian.language == "persian"

    assert not derive_policy("change my bio").active


def test_policy_rejects_wrong_length_and_foreign_script_without_truncation():
    policy = derive_policy(INSTRUCTION)
    with pytest.raises(PreparationPolicyError):
        validate_content("abc", policy)
    with pytest.raises(PreparationPolicyError):
        validate_content("a" * 11, policy)
    # Never truncated: invalid input raises, valid input returns unchanged.
    assert validate_content("abcdefghij", policy) == "abcdefghij"


def test_policy_language_check_ignores_digits_punctuation_and_emoji():
    policy = derive_policy("متن فارسی ۵۰ کاراکتری")
    assert policy.language == "persian"
    base = "سلام! این یک آزمایش است 🙂 (۱۲۳) "  # noqa: RUF001
    content = (base + "آ" * 50)[:50]  # exactly 50 code points, Persian + digits + emoji
    assert validate_content(content, policy) == content


@pytest.mark.asyncio
async def test_preparator_wraps_policy_violations_as_preparation_failures():
    """The real preparator fails closed when its provider output violates the
    policy — no partially valid action can leak through."""

    class StubProviders:
        def __init__(self, text):
            self.text = text

        async def chat(self, messages, tools=None):
            from backend.ai.providers.base.contract import ProviderResponse

            return ProviderResponse(
                text=self.text,
                provider_name="stub",
                success=True,
            )

    preparator = AIActionPreparator(StubProviders(
        '{"actions": [{"name": "set_bio", "arguments": {"text": "abc"}}]}'
    ))
    with pytest.raises(Exception) as excinfo:  # TaskPreparationError
        await preparator.prepare(
            INSTRUCTION,
            [{"name": "set_bio", "arguments": {}}],
            owner_id=1,
            tz_str="UTC",
        )
    assert "policy" in str(excinfo.value)
