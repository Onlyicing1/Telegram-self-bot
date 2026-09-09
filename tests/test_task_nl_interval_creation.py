"""Natural-language interval task creation — semantic, not regex-driven.

LIVE FAILURE: both requests below were rejected with
"I could not turn that into a safe, unambiguous schedule, ..." even though
each contains a clear recurring interval (هر پنج دقیقه / هر ۵ دقیقه) and a
clear action (bio update). The deterministic router correctly sent them to
create_task; the failure was inside TaskInterpreter: the action contract
never named a registered bio/profile tool (it only defines send_message), so
the model could not express "تو بیو بزارش" as a structured action and
returned null / an invalid candidate per the ambiguity rule.

Fix under test (semantic, no regex NL parsing):
1. INTERPRETER PROMPT: the action contract declares the REGISTERED profile
   tools (bio_set_text, username_set_text) with empty content arguments and
   ai_instruction verbatim, plus interval-recognition guidance covering
   digits (any script), number words, once-per-interval markers, and
   multi-line requests. Interval semantics stay the model's semantic job;
   deterministic validation still runs on the structured candidate.
2. DETERMINISTIC TOLERANCE: model-emitted interval shapes
   ({"every": "5 minutes"}, {"interval": "5 دقیقه"}, ...) normalize to
   canonical {"seconds": N} — bounded structured-output normalization,
   not natural-language parsing.
"""
from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from backend.ai.actions import (
    KIND_CONVERSATIONAL,
    KIND_EXECUTABLE,
    parse_command_intent,
)
from backend.ai.preparation_policy import (
    PreparationPolicyError,
    derive_policy,
    validate_content,
)
from backend.ai.task_candidate import TaskCandidate, TaskCandidateError
from backend.ai.tools.context import ToolContext

LIVE_REQUEST_1 = (
    "یه تسک بساز هر پنج دقیقه یه دیالوگ رندوم از آیانامی ری از انیمه "
    "نئون جنسیس انتخاب کن که زیر ۶۰ کاراکتر باشه و تو بیو بزارش"
)
LIVE_REQUEST_2 = (
    "هر ۵ دقیقه\n"
    "میخوام بیو پروفایلم رو آپدیت کنید\n"
    "یه دیالوگ رندوم از کاراکتر آیانامی ری از انیمه نئون جنسیس بزاری\n"
    "که زیر 60 کاراکتر باشه"
)

# Interval phrasings that must resolve to the same interval intent (each with
# an action verb appended where the phrasing itself carries none).
INTERVAL_PHRASINGS = [
    "هر ۵ دقیقه بیو رو عوض کن",
    "هر پنج دقیقه بیو رو عوض کن",
    "هر پنج دقیقه یکبار بیو رو عوض کن",
    "هر پنج دقیقه یک بار بیو رو عوض کن",
    "هر 5 دقیقه بیو رو عوض کن",
    "هر 5 دقیقه یک\u200cبار بیو رو عوض کن",
    "هر پنج دقیقه یه بار بیو رو عوض کن",
    "هر 300 ثانیه بیو رو عوض کن",
    "every 5 minutes send hello",
    "every five minutes send hello",
    "once every five minutes send hello",
    "every 300 seconds send hello",
    "send hello every 5 minutes",
]


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


def _tool_context(pm, owner_id=777):
    return ToolContext(
        telegram=None, owner_id=owner_id, tz_str="UTC", client=None,
        extra={"provider_manager": pm, "chat_id": -1001},
    )


def _bio_candidate(request: str, seconds: int = 300) -> str:
    """The candidate a well-instructed model emits: registered bio tool,
    EMPTY content arguments, ai_instruction = the request verbatim."""
    return json.dumps(
        {
            "label": "Ayanami Bio",
            "schedule_type": "interval",
            "schedule": {"seconds": seconds},
            "timezone": "UTC",
            "actions": [{"name": "bio_set_text", "arguments": {"text": ""}}],
            "notification_destination": {},
            "ai_instruction": request,
        },
        ensure_ascii=False,
    )


async def _create(request: str, candidate: str):
    from backend.ai.database import manager as dbm
    from backend.ai.tools.task import CreateTaskTool

    pm, provider = _provider_manager_with(candidate)
    manager = dbm.RepositoryManager(supabase_available=False)
    with patch.object(dbm, "get_repository_manager", return_value=manager):
        tool = CreateTaskTool(_tool_context(pm))
        result = await tool.execute(_tool_context(pm), {"request": request})
    return result, manager, provider


# ═══════════ Part A — the exact two live requests now create valid tasks ═══════════


@pytest.mark.asyncio
async def test_live_request_1_creates_five_minute_bio_task():
    result, manager, _ = await _create(LIVE_REQUEST_1, _bio_candidate(LIVE_REQUEST_1))
    assert result.success is True, result.message
    tasks = await manager.task.list_tasks(777)
    assert len(tasks) == 1
    task = tasks[0]
    assert task.schedule_type == "interval"
    assert task.schedule["seconds"] == 300
    # The semantic source/length requirement survives VERBATIM.
    assert task.ai_instruction == LIVE_REQUEST_1
    policy = derive_policy(task.ai_instruction)
    assert policy.source == "آیانامی ری"
    assert policy.max_length == 59
    # No baked/generated quote inside the persisted action.
    args = task.actions[0]["arguments"]
    assert args.get("text") == ""
    assert "Ayumi" not in task.actions[0]["arguments"].get("text", "")


@pytest.mark.asyncio
async def test_live_request_2_multiline_creates_five_minute_bio_task():
    result, manager, _ = await _create(LIVE_REQUEST_2, _bio_candidate(LIVE_REQUEST_2))
    assert result.success is True, result.message
    tasks = await manager.task.list_tasks(777)
    assert len(tasks) == 1
    task = tasks[0]
    assert task.schedule_type == "interval"
    assert task.schedule["seconds"] == 300
    assert task.ai_instruction == LIVE_REQUEST_2
    assert task.actions[0]["name"] == "bio_set_text"
    assert task.actions[0]["arguments"].get("text") == ""


# ═══════════ Part B — interval phrasing equivalence (semantic routing) ═══════════


@pytest.mark.parametrize("phrase", INTERVAL_PHRASINGS)
def test_interval_phrasing_routes_to_create_task(phrase):
    """Every equivalent phrasing reaches the semantic task boundary — the
    interpreter, not a phrase table, resolves the exact seconds."""
    result = parse_command_intent(phrase, has_reply=True)
    assert result.kind == KIND_EXECUTABLE, phrase
    assert result.action == "create_task", phrase


def test_exact_live_requests_route_to_create_task():
    for request in (LIVE_REQUEST_1, LIVE_REQUEST_2):
        result = parse_command_intent(request, has_reply=True)
        assert result.kind == KIND_EXECUTABLE
        assert result.action == "create_task"


def test_interval_without_intro_stays_semantic_not_rejected():
    # "پنج دقیقه یکبار" (no هر/every intro) has no deterministic marker; it
    # must NOT be force-routed — it stays conversational so the provider can
    # still interpret it semantically. Never a hard rejection.
    result = parse_command_intent("پنج دقیقه یکبار بیو رو عوض کن", has_reply=True)
    assert result.kind == KIND_CONVERSATIONAL


def test_plain_conversational_text_does_not_create_a_task():
    result = parse_command_intent("سلام خوبی امروز چیکار کردی", has_reply=True)
    assert result.kind == KIND_CONVERSATIONAL
    assert result.action == ""


# ═══════════ Part C — deterministic tolerance of model-emitted shapes ═══════════


def _candidate(schedule):
    return TaskCandidate.from_untrusted(
        {
            "label": "t",
            "schedule_type": "interval",
            "schedule": schedule,
            "timezone": "UTC",
            "actions": [{"name": "bio_set_text", "arguments": {"text": ""}}],
            "notification_destination": {},
        }
    )


@pytest.mark.parametrize(
    "schedule,expected_seconds",
    [
        ({"seconds": 300}, 300),
        ({"seconds": "300"}, 300),
        ({"interval": 5, "unit": "minutes"}, 300),
        ({"interval": "5", "unit": "minutes"}, 300),
        ({"every": 5, "unit": "minute"}, 300),
        ({"every": "5", "unit": "دقیقه"}, 300),
        ({"interval": "5 minutes"}, 300),
        ({"every": "5 دقیقه"}, 300),
        ({"minutes": 5}, 300),
        ({"interval_minutes": 5}, 300),
        ({"repeat": 5, "unit": "min"}, 300),
    ],
)
def test_model_emitted_interval_shapes_normalize_to_seconds(schedule, expected_seconds):
    parsed = _candidate(schedule).schedule
    assert parsed["seconds"] == expected_seconds


@pytest.mark.parametrize(
    "schedule",
    [
        {"when": "5 minutes"},
        {"every": "sometime later"},
        {"interval": "roughly five minutes"},
        {"seconds": 0},
        {"seconds": -5},
        {"every": 5},
        {"interval": 5, "unit": "light-years"},
    ],
)
def test_ambiguous_or_invalid_shapes_still_rejected(schedule):
    with pytest.raises(TaskCandidateError):
        _candidate(schedule)


# ═══════════ Part D — interpreter prompt carries the semantic contract ═══════════


@pytest.mark.asyncio
async def test_interpreter_prompt_names_registered_bio_tool_and_interval_contract():
    from backend.ai.task_interpreter import TaskInterpreter

    pm, provider = _provider_manager_with("null")
    interpreter = TaskInterpreter(pm)
    with pytest.raises(Exception):
        await interpreter.interpret(LIVE_REQUEST_1, timezone="UTC")
    system = provider.last_messages[0]["content"]
    assert "bio_set_text" in system, "the registered bio tool must be named"
    assert "username_set_text" in system
    # Interval recognition must be semantic (digits any script + number
    # words), not a fixed phrase list.
    assert "number word" in system
    assert "multiple lines" in system


@pytest.mark.asyncio
async def test_ambiguous_request_still_returns_honest_failure():
    """Genuinely ambiguous schedules stay rejected — the ambiguity rule is
    preserved, not weakened."""
    from backend.ai.database import manager as dbm
    from backend.ai.tools.task import CreateTaskTool

    pm, _ = _provider_manager_with("null")
    manager = dbm.RepositoryManager(supabase_available=False)
    with patch.object(dbm, "get_repository_manager", return_value=manager):
        tool = CreateTaskTool(_tool_context(pm))
        result = await tool.execute(
            _tool_context(pm), {"request": "یه وقتایی یه یادآوری بفرست"}
        )
    assert result.success is False
    assert "could not turn that into a safe, unambiguous schedule" in result.message
    tasks = await manager.task.list_tasks(777)
    assert tasks == []


# ═══════════ Part E — source fidelity, length, and guardian stay intact ═══════════


def test_max_length_semantics_still_strict():
    policy = derive_policy("یه متن زیر 60 کاراکتر بنویس")
    assert policy.max_length == 59


def test_source_fidelity_still_fails_closed():
    policy = derive_policy(LIVE_REQUEST_1)
    assert policy.source == "آیانامی ری"
    with pytest.raises(PreparationPolicyError):
        validate_content("Ayumi: Every star begins as a dream!", policy)
    with pytest.raises(PreparationPolicyError):
        validate_content("آیانامی ری: هر متنی", policy)


@pytest.mark.asyncio
async def test_bio_guardian_still_limits_mutations_to_one_per_window():
    """The shared guardian remains untouched: two rapid real bio mutations
    result in exactly one success."""
    from unittest.mock import MagicMock

    from backend.profile import scheduler as profile_scheduler
    from backend.services import bio_guardian, bio_service

    bio_guardian.reset_window_for_tests()
    previous_client = profile_scheduler._client

    async def _rpc(request):
        return MagicMock()

    profile_scheduler._client = MagicMock(side_effect=_rpc)
    try:
        first = await bio_service.do_text(9, "first", tz_str="UTC")
        assert first.startswith("✅"), first
        second = await bio_service.do_text(9, "second", tz_str="UTC")
        assert "NOT updated" in second, second
    finally:
        profile_scheduler._client = previous_client
        bio_guardian.reset_window_for_tests()