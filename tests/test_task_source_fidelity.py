"""Semantic source fidelity regression tests — the live Ayumi failure.

LIVE FAILURE (reproduced in production): the task
"هر 5 دقیقه تکست بیو من رو به یه دیالوگ رندوم از آیانامی ری تغییر بده باید
زیر 60 کاراکتر باشه" was created successfully, but the Telegram Bio became
"Ayumi: Every star begins as a dream!" — an unrelated character.

ROOT CAUSE (traced on the pre-fix HEAD): TaskInterpreter's system prompt has
no contract for AI-generated content, so the provider bakes a random static
dialogue into the action snapshot at CREATION time and never emits
``ai_instruction``. The coordinator runs the AI-preparation/policy path only
for tasks that persist an ``ai_instruction`` (task_execution.execute), so the
task executed as a STATIC task: the baked "Ayumi" line was applied verbatim,
every occurrence, with the source requirement silently lost.

Fix under test (two independent layers):
1. CREATION GATE (deterministic, in CreateTaskTool): when the ORIGINAL human
   request derives a content policy (source/length/language), the task MUST
   persist ``ai_instruction`` = the verbatim request. A missing or
   paraphrased instruction is repaired from the request — the model can
   neither drop the source semantics nor make the task static.
2. EXECUTION FIDELITY (deterministic, at the boundary): generated content
   must satisfy the deterministic policy before ToolExecutor sees it.
   Source requests are GENERATED in-character dialogue: the line must open
   with the requested source's full name (self-attribution) — drifted
   characters, short forms, and unattributed generic text are rejected and
   regenerated only within the bounded preparation contract; an explicitly
   requested EXACT canonical quote fails closed (no trusted corpus).

Honest limitation (documented, not faked): there is NO trusted Ayanami Rei
corpus or independent source verifier in this architecture. A self-attributed
line is enforced only as the generated line's own opening attribution — the
system never claims a line is an exact canonical quotation.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from backend.ai.database.task_repository import InMemoryTaskRepository
from backend.ai.preparation_policy import derive_policy
from backend.ai.task_execution import (
    MAX_PREPARATION_ATTEMPTS,
    AIActionPreparator,
    TaskExecutionCoordinator,
)
from backend.ai.task_scheduler import TaskScheduler, occurrence_key
from backend.ai.tools.base import PermissionLevel, ToolResult
from backend.ai.tools.context import ToolContext
from backend.ai.tools.executor import ToolExecutor
from backend.ai.tools.registry import ToolRegistry, create_default_registry

PERSIAN_TASK = (
    "هر 5 دقیقه تکست بیو من رو به یه دیالوگ رندوم از آیانامی ری تغییر بده "
    "باید زیر 60 کاراکتر باشه"
)
LIVE_DRIFT_LINE = "Ayumi: Every star begins as a dream!"
NOW = datetime(2026, 9, 9, 12, 0, 0, tzinfo=timezone.utc)


# ── Local helpers (mirroring the established test fixtures) ─────────────────


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


class ScriptedPreparator:
    """Returns one scripted candidate per preparation round."""

    def __init__(self, texts):
        self.texts = list(texts)
        self.rounds = 0
        self.instructions = []

    async def prepare_validated(self, instruction, templates, *, owner_id, tz_str):
        self.rounds += 1
        self.instructions.append(instruction)
        text = self.texts.pop(0) if self.texts else self.texts_last
        return [{"name": t["name"], "arguments": {"text": text}} for t in templates]

    @property
    def texts_last(self):
        return self.texts[-1] if self.texts else ""


def ai_task_data(**overrides):
    value = {
        "label": "Ayanami bio",
        "schedule_type": "interval",
        "schedule": {"seconds": 300},
        "timezone": "UTC",
        "next_run_at": NOW,
        "actions": [{"name": "set_bio", "arguments": {}}],
        "notification_destination": {"chat_id": 1},
        "ai_instruction": PERSIAN_TASK,
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


async def make_claimed_occurrence(repo, task, boundary=NOW + timedelta(seconds=60)):
    return await repo.create_occurrence(1, {
        "task_id": task.id,
        "occurrence_key": occurrence_key(task.id, boundary),
        "definition_version": task.version,
        "action_snapshot": task.actions,
        "scheduled_for": boundary,
    })


def _provider_manager_with(response_text: str):
    from backend.ai.providers.base.capabilities import ProviderCapabilities
    from backend.ai.providers.base.config import ProviderConfig
    from backend.ai.providers.base.contract import BaseProvider, ProviderResponse
    from backend.ai.providers.manager.manager import ProviderManager

    class _FakeProvider(BaseProvider):
        def __init__(self):
            super().__init__(ProviderConfig(provider_name="fake", enabled=True, default_model="m"))
            self.calls = 0
            self.last_messages = None

        @property
        def name(self):
            return "fake"

        @property
        def capabilities(self):
            return ProviderCapabilities(supports_tools=True, supports_function_call=True)

        async def chat(self, messages, **kwargs):
            self.calls += 1
            self.last_messages = messages
            return ProviderResponse(text=response_text, provider_name="fake", success=True)

        def initialize(self):
            return None

        def shutdown(self):
            return None

        def count_tokens(self, text):
            return max(1, len(text) // 4)

        def health(self):
            return {"healthy": True}

    pm = ProviderManager()
    provider = _FakeProvider()
    pm.register_provider(provider)
    pm.switch_provider("fake")
    pm._fallback_chain = []
    return pm, provider


def _create_context(pm, owner_id=777):
    return ToolContext(
        telegram=None, owner_id=owner_id, tz_str="UTC", client=None,
        extra={"provider_manager": pm, "chat_id": -1001},
    )


# ═══════════════════════ Part A — creation-time fidelity ════════════════════
# The exact live failure: the provider bakes "Ayumi: ..." into the candidate
# and omits ai_instruction. The task must NOT become a static Ayumi task.


BAKED_AYUMI_CANDIDATE = (
    '{"label":"Bio","schedule_type":"interval","schedule":{"seconds":300},'
    '"timezone":"UTC",'
    '"actions":[{"name":"bio_set_text","arguments":{"text":"' + LIVE_DRIFT_LINE + '"}}],'
    '"notification_destination":{}}'
)


@pytest.mark.asyncio
async def test_live_ayumi_failure_task_preserves_source_requirement():
    """THE regression: the exact live request with the exact live baked
    'Ayumi' candidate must create a task whose ai_instruction is the VERBATIM
    request — never a static Ayumi task."""
    from backend.ai.database import manager as dbm
    from backend.ai.tools.task import CreateTaskTool

    pm, provider = _provider_manager_with(BAKED_AYUMI_CANDIDATE)
    manager = dbm.RepositoryManager(supabase_available=False)
    with patch.object(dbm, "get_repository_manager", return_value=manager):
        tool = CreateTaskTool(_create_context(pm))
        result = await tool.execute(_create_context(pm), {"request": PERSIAN_TASK})

    assert result.success is True, result.message
    tasks = await manager.task.list_tasks(777)
    assert len(tasks) == 1
    task = tasks[0]
    assert task.ai_instruction == PERSIAN_TASK, (
        "the source/length semantics of the ORIGINAL request must survive "
        "task creation verbatim — this is exactly what was lost live"
    )
    policy = derive_policy(task.ai_instruction)
    assert policy.source == "آیانامی ری"
    assert policy.max_length == 59


@pytest.mark.asyncio
async def test_baked_ayumi_text_never_executes_for_source_task():
    """Even with the baked candidate persisted as the action snapshot, the
    coordinator must regenerate content under the verbatim instruction — the
    baked 'Ayumi' line can never reach the tool."""
    repo = InMemoryTaskRepository()
    task = await repo.create_task(1, ai_task_data(
        actions=[{"name": "set_bio", "arguments": {"text": LIVE_DRIFT_LINE}}],
    ))
    # A provider that keeps answering with the drifted character.
    preparator = ScriptedPreparator([LIVE_DRIFT_LINE, LIVE_DRIFT_LINE, LIVE_DRIFT_LINE])
    calls = []
    coordinator = build_coordinator(repo, preparator, calls=calls)
    occurrence = await make_claimed_occurrence(repo, task)
    repo._occurrences[(task.id, occurrence.occurrence_key)].status = "running"

    result = await coordinator.execute(
        await repo.get_occurrence(1, task.id, occurrence.occurrence_key)
    )
    assert result.success is False
    assert calls == [], "drifted content must NEVER reach the tool"


@pytest.mark.asyncio
async def test_paraphrased_instruction_is_replaced_with_verbatim_request():
    """A model that emits ai_instruction but DROPS the source (paraphrase)
    must not weaken the durable contract: the verbatim request wins."""
    from backend.ai.database import manager as dbm
    from backend.ai.tools.task import CreateTaskTool

    paraphrased = (
        '{"label":"Bio","schedule_type":"interval","schedule":{"seconds":300},'
        '"timezone":"UTC","actions":[{"name":"bio_set_text","arguments":{"text":"x"}}],'
        '"notification_destination":{},"ai_instruction":"change my bio to anime quotes"}'
    )
    pm, _ = _provider_manager_with(paraphrased)
    manager = dbm.RepositoryManager(supabase_available=False)
    with patch.object(dbm, "get_repository_manager", return_value=manager):
        tool = CreateTaskTool(_create_context(pm))
        result = await tool.execute(_create_context(pm), {"request": PERSIAN_TASK})

    assert result.success is True
    task = (await manager.task.list_tasks(777))[0]
    assert task.ai_instruction == PERSIAN_TASK


@pytest.mark.asyncio
async def test_verbatim_instruction_from_model_is_preserved():
    """When the model itself carries the full request, the gate changes
    nothing (idempotent)."""
    from backend.ai.database import manager as dbm
    from backend.ai.tools.task import CreateTaskTool

    compliant = (
        '{"label":"Bio","schedule_type":"interval","schedule":{"seconds":300},'
        '"timezone":"UTC","actions":[{"name":"bio_set_text","arguments":{"text":""}}],'
        '"notification_destination":{},"ai_instruction":' + __import__("json").dumps(PERSIAN_TASK) + '}'
    )
    pm, _ = _provider_manager_with(compliant)
    manager = dbm.RepositoryManager(supabase_available=False)
    with patch.object(dbm, "get_repository_manager", return_value=manager):
        tool = CreateTaskTool(_create_context(pm))
        result = await tool.execute(_create_context(pm), {"request": PERSIAN_TASK})

    assert result.success is True
    task = (await manager.task.list_tasks(777))[0]
    assert task.ai_instruction == PERSIAN_TASK


@pytest.mark.asyncio
async def test_unconstrained_task_stays_static():
    """A request with NO content constraints (e.g. the hello task) must not
    be forced into the AI-instruction path — static behavior is unchanged."""
    from backend.ai.database import manager as dbm
    from backend.ai.tools.task import CreateTaskTool

    hello = (
        '{"label":"Hello","schedule_type":"interval","schedule":{"seconds":60},'
        '"timezone":"UTC","actions":[{"name":"send_message","arguments":{"text":"سلام"}}],'
        '"notification_destination":{}}'
    )
    pm, _ = _provider_manager_with(hello)
    manager = dbm.RepositoryManager(supabase_available=False)
    with patch.object(dbm, "get_repository_manager", return_value=manager):
        tool = CreateTaskTool(_create_context(pm))
        result = await tool.execute(_create_context(pm), {"request": "هر 1 دقیقه یک بار بنویس سلام"})

    assert result.success is True
    task = (await manager.task.list_tasks(777))[0]
    assert task.ai_instruction is None


@pytest.mark.asyncio
async def test_interpreter_prompt_carries_ai_content_contract():
    """The interpreter's system prompt must tell the model about the
    AI-generated-content contract (ai_instruction) — the prompt-level half
    of the fix."""
    from backend.ai.task_interpreter import TaskInterpreter

    pm, provider = _provider_manager_with("null")
    interpreter = TaskInterpreter(pm)
    with pytest.raises(Exception):
        await interpreter.interpret(PERSIAN_TASK, timezone="UTC")
    system = provider.last_messages[0]["content"]
    assert "ai_instruction" in system
    assert "AI-GENERATED CONTENT" in system.upper()


# ═══════════════════════ Part B — execution fidelity ════════════════════════


@pytest.mark.asyncio
async def test_ayumi_drift_rejected_generic_text_rejected():
    """Both live failure shapes fail closed at the boundary: a DIFFERENT
    character's line, and unattributed generic/inspirational text."""
    repo = InMemoryTaskRepository()
    task = await repo.create_task(1, ai_task_data())
    for bad in (LIVE_DRIFT_LINE, "Every star begins as a dream!"):
        preparator = ScriptedPreparator([bad])
        calls = []
        coordinator = build_coordinator(repo, preparator, calls=calls)
        occurrence = await make_claimed_occurrence(repo, task)
        repo._occurrences[(task.id, occurrence.occurrence_key)].status = "running"
        result = await coordinator.execute(
            await repo.get_occurrence(1, task.id, occurrence.occurrence_key)
        )
        assert result.success is False, bad
        assert calls == [], bad


@pytest.mark.asyncio
async def test_wrong_speaker_labels_are_rejected():
    """The attribution must name the REQUESTED source exactly — not another
    character, not a short form, not a mention inside the text."""
    repo = InMemoryTaskRepository()
    task = await repo.create_task(1, ai_task_data())
    for bad in ("Rei: تست", "نوا: تست", "hello آیانامی ری: تست"):
        preparator = ScriptedPreparator([bad])
        calls = []
        coordinator = build_coordinator(repo, preparator, calls=calls)
        occurrence = await make_claimed_occurrence(repo, task)
        repo._occurrences[(task.id, occurrence.occurrence_key)].status = "running"
        result = await coordinator.execute(
            await repo.get_occurrence(1, task.id, occurrence.occurrence_key)
        )
        assert result.success is False, bad
        assert calls == []


@pytest.mark.asyncio
async def test_matching_attributed_line_executes_exactly_once():
    """A line that opens with the requested source satisfies the generated
    in-character dialogue contract: it executes exactly once through the
    ToolExecutor — self-attribution, never a canonical-quote claim."""
    repo = InMemoryTaskRepository()
    task = await repo.create_task(1, ai_task_data())
    valid = "آیانامی ری: " + "د" * 40  # 51 chars, attributed, Persian script
    preparator = ScriptedPreparator([valid])
    calls = []
    coordinator = build_coordinator(repo, preparator, calls=calls)
    occurrence = await make_claimed_occurrence(repo, task)
    repo._occurrences[(task.id, occurrence.occurrence_key)].status = "running"

    result = await coordinator.execute(
        await repo.get_occurrence(1, task.id, occurrence.occurrence_key)
    )
    assert result.success is True
    assert preparator.rounds == 1
    assert calls == [("set_bio", {"text": valid}, 1)]
    assert (await repo.get_occurrence(1, task.id, occurrence.occurrence_key)).status == "succeeded"


@pytest.mark.asyncio
async def test_regeneration_is_bounded_then_fails_closed():
    """Constantly drifted output must exhaust exactly MAX_PREPARATION_ATTEMPTS
    provider rounds, execute nothing, and fail the occurrence — never mutate
    Telegram with a guess."""
    repo = InMemoryTaskRepository()
    task = await repo.create_task(1, ai_task_data())
    preparator = ScriptedPreparator([LIVE_DRIFT_LINE] * (MAX_PREPARATION_ATTEMPTS + 2))
    calls = []
    coordinator = build_coordinator(repo, preparator, calls=calls)
    occurrence = await make_claimed_occurrence(repo, task)
    repo._occurrences[(task.id, occurrence.occurrence_key)].status = "running"

    result = await coordinator.execute(
        await repo.get_occurrence(1, task.id, occurrence.occurrence_key)
    )
    assert result.success is False
    assert preparator.rounds == MAX_PREPARATION_ATTEMPTS
    assert preparator.instructions == [PERSIAN_TASK] * MAX_PREPARATION_ATTEMPTS
    assert calls == []


@pytest.mark.asyncio
async def test_drift_then_attributed_line_succeeds_once():
    """The bounded loop regenerates after a drifted round; the first
    self-attributed line executes exactly once."""
    repo = InMemoryTaskRepository()
    task = await repo.create_task(1, ai_task_data())
    valid = "آیانامی ری: " + "د" * 40
    preparator = ScriptedPreparator([LIVE_DRIFT_LINE, valid])
    calls = []
    coordinator = build_coordinator(repo, preparator, calls=calls)
    occurrence = await make_claimed_occurrence(repo, task)
    repo._occurrences[(task.id, occurrence.occurrence_key)].status = "running"

    result = await coordinator.execute(
        await repo.get_occurrence(1, task.id, occurrence.occurrence_key)
    )
    assert result.success is True
    assert preparator.rounds == 2
    assert calls == [("set_bio", {"text": valid}, 1)]


@pytest.mark.asyncio
async def test_exactly_60_chars_rejected_under_60_accepted_at_boundary():
    """'زیر 60 کاراکتر' means maximum 59: 60 fails, 59 passes."""
    repo = InMemoryTaskRepository()
    instruction = "change my bio to random text under 60 characters"
    task = await repo.create_task(1, ai_task_data(ai_instruction=instruction))

    exactly_60 = "x" * 60
    preparator = ScriptedPreparator([exactly_60])
    calls = []
    coordinator = build_coordinator(repo, preparator, calls=calls)
    occurrence = await make_claimed_occurrence(repo, task)
    repo._occurrences[(task.id, occurrence.occurrence_key)].status = "running"
    result = await coordinator.execute(
        await repo.get_occurrence(1, task.id, occurrence.occurrence_key)
    )
    assert result.success is False and calls == []

    task = await repo.create_task(1, ai_task_data(ai_instruction=instruction))
    under_60 = "x" * 59
    preparator = ScriptedPreparator([under_60])
    calls = []
    coordinator = build_coordinator(repo, preparator, calls=calls)
    occurrence = await make_claimed_occurrence(repo, task)
    repo._occurrences[(task.id, occurrence.occurrence_key)].status = "running"
    result = await coordinator.execute(
        await repo.get_occurrence(1, task.id, occurrence.occurrence_key)
    )
    assert result.success is True and calls == [("set_bio", {"text": under_60}, 1)]


@pytest.mark.asyncio
async def test_exact_quote_task_fails_closed_zero_mutation():
    """An explicit exact-canonical-quote request cannot be satisfied without
    a trusted corpus: every round fails closed and the occurrence fails —
    zero tool calls, zero Telegram mutations."""
    repo = InMemoryTaskRepository()
    instruction = PERSIAN_TASK + "، نقل قول دقیق"
    task = await repo.create_task(1, ai_task_data(ai_instruction=instruction))
    assert derive_policy(instruction).quote_exact is True
    claimed = "آیانامی ری: " + "م" * 30  # even correctly attributed: not a verified quote
    preparator = ScriptedPreparator([claimed] * (MAX_PREPARATION_ATTEMPTS + 2))
    calls = []
    coordinator = build_coordinator(repo, preparator, calls=calls)
    occurrence = await make_claimed_occurrence(repo, task)
    repo._occurrences[(task.id, occurrence.occurrence_key)].status = "running"

    result = await coordinator.execute(
        await repo.get_occurrence(1, task.id, occurrence.occurrence_key)
    )
    assert result.success is False
    assert preparator.rounds == MAX_PREPARATION_ATTEMPTS
    assert calls == []
    assert (await repo.get_occurrence(1, task.id, occurrence.occurrence_key)).status == "failed"


@pytest.mark.asyncio
async def test_prepare_ahead_source_task_is_side_effect_free_then_executes_once():
    """Prepare-ahead never runs the tool or guardian; a validated
    self-attributed action is persisted durably, and the boundary later
    executes it exactly once from the metadata — no new provider round."""
    from backend.services import bio_guardian

    bio_guardian.reset_window_for_tests()
    try:
        repo = InMemoryTaskRepository()
        task = await repo.create_task(1, ai_task_data())
        valid = "آیانامی ری: " + "م" * 30
        preparator = ScriptedPreparator([valid])
        calls = []
        coordinator = build_coordinator(repo, preparator, calls=calls)
        occurrence = await make_claimed_occurrence(repo, task)

        prepared = await coordinator.prepare_ahead(occurrence)
        assert prepared is not None
        assert preparator.rounds == 1
        assert calls == []  # no tool execution
        assert bio_guardian.seconds_until_bio_mutation_allowed() == 0.0  # no window opened
        stored = await repo.get_occurrence(1, task.id, occurrence.occurrence_key)
        assert stored.preparation_metadata["kind"] == "prepared_action"

        # At the boundary, the durably prepared action runs exactly once
        # with ZERO additional provider rounds.
        repo._occurrences[(task.id, occurrence.occurrence_key)].status = "running"
        stored = await repo.get_occurrence(1, task.id, occurrence.occurrence_key)
        rounds_before = preparator.rounds
        result = await coordinator.execute(stored)
        assert result.success is True
        assert preparator.rounds == rounds_before  # metadata path, no provider call
        assert calls == [("set_bio", {"text": valid}, 1)]
    finally:
        bio_guardian.reset_window_for_tests()


@pytest.mark.asyncio
async def test_prepare_ahead_rejects_drifted_content():
    """Prepared-ahead drift never persists: the occurrence stays unprepared
    and the boundary will fall back to the (fail-closed) occurrence path."""
    repo = InMemoryTaskRepository()
    task = await repo.create_task(1, ai_task_data())
    preparator = ScriptedPreparator([LIVE_DRIFT_LINE])
    calls = []
    coordinator = build_coordinator(repo, preparator, calls=calls)
    occurrence = await make_claimed_occurrence(repo, task)

    prepared = await coordinator.prepare_ahead(occurrence)
    assert prepared is None
    stored = await repo.get_occurrence(1, task.id, occurrence.occurrence_key)
    assert not stored.preparation_metadata
    assert calls == []


# ═══════════════ Part C — guardian shares every Bio mutation path ═══════════


@pytest.fixture
def _guardian_env():
    from backend.profile import scheduler as profile_scheduler
    from backend.services import bio_guardian

    bio_guardian.reset_window_for_tests()
    previous_client = profile_scheduler._client

    async def _rpc(request):
        return MagicMock()

    profile_scheduler._client = MagicMock(side_effect=_rpc)
    yield
    profile_scheduler._client = previous_client
    bio_guardian.reset_window_for_tests()


def _real_bio_registry(owner=9):
    ctx = ToolContext(telegram=None, owner_id=owner, tz_str="UTC", client=None, extra={})
    registry = create_default_registry(ctx)
    return ToolExecutor(registry, ctx), ctx


@pytest.mark.asyncio
async def test_manual_bio_then_scheduled_bio_share_one_window(_guardian_env):
    """A manual/AI-tool bio mutation and a scheduled task bio mutation are
    two callers of ONE shared boundary: the second within 60s is rejected
    honestly, never reported as success."""
    from backend.services import bio_guardian, bio_service

    executor, ctx = _real_bio_registry(owner=9)
    first = await bio_service.do_text(9, "manual bio", tz_str="UTC")
    assert first.startswith("✅"), first
    assert bio_guardian.seconds_until_bio_mutation_allowed() > 0

    # The scheduled path executes the real bio_set_text tool through the
    # SAME boundary while the window is open — the tool must fail honestly.
    results = await executor.execute_calls(
        [{"name": "bio_set_text", "arguments": {"text": "scheduled bio"}}],
        owner_id=9, session_id="s", context_override=ctx,
    )
    assert results[0].success is False
    assert "NOT updated" in results[0].message


@pytest.mark.asyncio
async def test_retry_within_window_cannot_bypass_guardian(_guardian_env):
    """A retry of a bio task within the window hits the same boundary and
    fails honestly — retries never bypass the guardian."""
    from backend.services import bio_service

    executor, ctx = _real_bio_registry(owner=9)
    first = await bio_service.do_text(9, "first", tz_str="UTC")
    assert first.startswith("✅"), first

    for _ in range(2):  # simulate immediate retry attempts
        results = await executor.execute_calls(
            [{"name": "bio_set_text", "arguments": {"text": "retry"}}],
            owner_id=9, session_id="s", context_override=ctx,
        )
        assert results[0].success is False


# ═════════════ Part C — bio action fidelity at the creation boundary ═════════
# THE live failure class: a scheduled BIO update request was persisted with the
# generic message-write action (send_message), so every occurrence sent a chat
# message instead of updating the bio. Semantic interpretation stays the source
# of the action; the deterministic creation gate repairs ONLY a request that
# explicitly names the bio AND asks to change it.

PERSIAN_BIO_REQUEST = "هر ۵ دقیقه بیو پروفایلم رو آپدیت کن"
ENGLISH_BIO_REQUEST = "every 5 minutes update my bio with a new random quote"
PLAIN_SEND_REQUEST = "هر 5 دقیقه بنویس سلام"


def _message_action_candidate(text: str = "میو", ai_instruction: str | None = None) -> str:
    import json as _json
    payload: dict = {
        "label": "Bio",
        "schedule_type": "interval",
        "schedule": {"seconds": 300},
        "timezone": "UTC",
        "actions": [{"name": "send_message", "arguments": {"text": text}}],
        "notification_destination": {},
    }
    if ai_instruction is not None:
        payload["ai_instruction"] = ai_instruction
    return _json.dumps(payload, ensure_ascii=False)


async def _create_task(provider_text: str, request: str, deterministic: dict | None = None):
    from backend.ai.database import manager as dbm
    from backend.ai.tools.task import CreateTaskTool

    pm, provider = _provider_manager_with(provider_text)
    manager = dbm.RepositoryManager(supabase_available=False)
    ctx = _create_context(pm)
    if deterministic is not None:
        ctx.extra["deterministic_task_candidate"] = deterministic
    with patch.object(dbm, "get_repository_manager", return_value=manager):
        result = await CreateTaskTool(ctx).execute(ctx, {"request": request})
    return result, provider, manager


@pytest.mark.asyncio
async def test_persian_bio_update_misclassified_as_message_persists_bio_tool():
    """A clear Persian bio-update request whose candidate came back as
    send_message must persist the canonical registered bio tool."""
    result, _provider, manager = await _create_task(
        _message_action_candidate(), PERSIAN_BIO_REQUEST,
    )
    assert result.success is True, result.message
    task = (await manager.task.list_tasks(777))[0]
    assert task.actions == [
        {"name": "bio_set_text", "arguments": {"text": "میو"}}
    ], "a bio update must never persist as send_message"


@pytest.mark.asyncio
async def test_english_bio_update_misclassified_as_message_persists_bio_tool():
    result, _provider, manager = await _create_task(
        _message_action_candidate("hello"), ENGLISH_BIO_REQUEST,
    )
    assert result.success is True, result.message
    task = (await manager.task.list_tasks(777))[0]
    assert [a["name"] for a in task.actions] == ["bio_set_text"]


@pytest.mark.asyncio
async def test_plain_message_task_still_persists_send_message():
    """A request that genuinely asks to send a chat message is untouched."""
    result, _provider, manager = await _create_task(
        _message_action_candidate("سلام"), PLAIN_SEND_REQUEST,
    )
    assert result.success is True, result.message
    task = (await manager.task.list_tasks(777))[0]
    assert task.actions == [
        {"name": "send_message", "arguments": {"text": "سلام"}}
    ]


@pytest.mark.asyncio
async def test_bio_request_keeps_verbatim_ai_instruction_when_action_repaired():
    """Repairing the tool name must not weaken the AI-generated-content
    contract: the verbatim request still becomes the ai_instruction."""
    paraphrased = _message_action_candidate("Ayumi: hi", "change my bio to anime quotes")
    result, _provider, manager = await _create_task(paraphrased, PERSIAN_TASK)
    assert result.success is True, result.message
    task = (await manager.task.list_tasks(777))[0]
    assert task.ai_instruction == PERSIAN_TASK
    assert [a["name"] for a in task.actions] == ["bio_set_text"]


@pytest.mark.asyncio
async def test_deterministic_message_write_candidate_for_bio_request_is_repaired():
    """The deterministic interval+write shortcut only ever builds send_message;
    for a bio request that candidate must be repaired, with no provider call."""
    deterministic = {
        "label": "میو",
        "schedule_type": "interval",
        "schedule": {"seconds": 300},
        "timezone": "UTC",
        "actions": [{"name": "send_message", "arguments": {"text": "میو"}}],
        "notification_destination": {},
    }
    result, provider, manager = await _create_task(
        "null", "هر 5 دقیقه توی بیو بنویس میو", deterministic=deterministic,
    )
    assert result.success is True, result.message
    assert provider.calls == 0, "deterministic candidate must not hit a provider"
    task = (await manager.task.list_tasks(777))[0]
    assert task.actions == [
        {"name": "bio_set_text", "arguments": {"text": "میو"}}
    ]


@pytest.mark.asyncio
async def test_bio_occurrence_executes_bio_tool_not_message_tool():
    """The resulting scheduled occurrence reaches the bio tool through the
    existing TaskExecutionCoordinator -> ToolExecutor path, and never the
    message tool."""
    repo = InMemoryTaskRepository()
    task = await repo.create_task(1, ai_task_data(
        actions=[{"name": "bio_set_text", "arguments": {"text": "میو"}}],
        ai_instruction=None,
    ))
    calls: list = []
    registry = ToolRegistry()
    registry.register(FakeTool("bio_set_text", calls))
    registry.register(FakeTool("send_message", calls))
    ctx = ToolContext(None, 1, "UTC")
    coordinator = TaskExecutionCoordinator(repo, ToolExecutor(registry, ctx), 1, ctx)
    occurrence = await make_claimed_occurrence(repo, task)
    repo._occurrences[(task.id, occurrence.occurrence_key)].status = "running"

    result = await coordinator.execute(
        await repo.get_occurrence(1, task.id, occurrence.occurrence_key)
    )
    assert result.success is True
    assert calls == [("bio_set_text", {"text": "میو"}, 1)]
