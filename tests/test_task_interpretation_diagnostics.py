"""Failure-layer regression tests for the exact live-rejected request.

The live symptom — the multi-line Persian bio-task request answered with the
generic "safe, unambiguous schedule" message — was diagnosed as follows
(source-traced on `6087d2e`):

- Routing: `parse_command_intent` deterministically routes the EXACT request
  to `create_task` passing the FULL multi-line text (proven below).
- Deterministic chain: a compliant candidate for the exact request creates
  the task (proven in test_task_semantic_triggers).
- Therefore the generic rejection is produced only when the interpreter
  raises, which happens for provider-output conditions: JSON null, malformed
  JSON, or a schema-violating object.
- A source-proven prompt/schema contradiction amplified the last condition:
  the old timezone instruction said "interval schedules carry no timezone
  field" while CANDIDATE_SCHEMA REQUIRES the top-level "timezone" — a model
  following the former omits the latter and the candidate is rejected.
  The instruction is reworded; the missing-timezone rejection is pinned as
  correct deterministic behavior.
- Diagnostics: the interpreter now logs a content-free `response_shape`
  (null/object/array/string/unsupported/malformed) on every candidate
  rejection and parse, so one live reproduction classifies the failure
  without exposing provider content.
"""
from __future__ import annotations

import asyncio
import json
import logging
from unittest.mock import patch

import pytest

from backend.ai.actions import ActionParseResult, KIND_CONVERSATIONAL, KIND_EXECUTABLE, parse_command_intent
from backend.ai.task_interpreter import (
    TaskInterpretationError,
    TaskInterpreter,
    TaskUnsupportedError,
)

TZ = "Asia/Tehran"

LIVE_PERSIAN_REQUEST = (
    "هر ۵ دقیقه\n"
    "میخوام بیو پروفایلم رو آپدیت کنید\n"
    "یه دیالوگ رندوم از کاراکتر آیانامی ری از انیمه نئون جنسیس بزاری\n"
    "که زیر 60 کاراکتر باشه"
)


class _Provider:
    def __init__(self, response_text: str):
        self.response_text = response_text
        self.last_messages = None

    async def chat(self, messages, tools=None):
        from backend.ai.providers.base.contract import ProviderResponse

        self.last_messages = messages
        return ProviderResponse(text=self.response_text, provider_name="stub", success=True)


async def _interpret(response_text: str):
    return await TaskInterpreter(_Provider(response_text)).interpret(
        LIVE_PERSIAN_REQUEST, timezone=TZ
    )


# ═══════════════════════ 1. ROUTING (possibility E / request-string #8) ═══════════════════════


def test_exact_request_routes_deterministically_to_create_task_with_full_text():
    """The exact multi-line request must reach create_task with the FULL
    text intact — never a fragment, never a rewritten line."""
    result: ActionParseResult = parse_command_intent(LIVE_PERSIAN_REQUEST)
    assert result.kind == KIND_EXECUTABLE
    assert result.action == "create_task"
    assert result.schedule_text == LIVE_PERSIAN_REQUEST  # multi-line preserved


def test_persian_word_variant_routes_to_create_task():
    result = parse_command_intent("هر پنج دقیقه بیو رو با یه دیالوگ رندوم از آیانامی ری عوض کن")
    assert result.kind == KIND_EXECUTABLE
    assert result.action == "create_task"


def test_ascii_digit_variant_routes_to_create_task():
    result = parse_command_intent("هر 5 دقیقه\nبیو رو آپدیت کن\nیه دیالوگ از آیانامی ری")
    assert result.kind == KIND_EXECUTABLE
    assert result.action == "create_task"


def test_english_equivalent_routes_to_create_task_with_full_text():
    """The English equivalent of the live request must reach create_task
    with the FULL text — never be captured as a bio READ (the historical
    get_bio hijack: 'my' in the read vocabulary matched 'change my bio …')."""
    request = (
        "Every 5 minutes, change my bio to a random Rei Ayanami dialogue. "
        "It must be under 60 characters."
    )
    result = parse_command_intent(request)
    assert result.kind == KIND_EXECUTABLE
    assert result.action == "create_task"
    assert result.schedule_text == request


def test_bio_change_without_schedule_is_never_captured_as_bio_read():
    """'change my bio to X' (no schedule) must NOT answer with the current
    bio: the read branch is blocked for write intents and the request stays
    conversational (the provider path owns the write semantically)."""
    for text in (
        "change my bio to something nice",
        "بیو رو عوض کن",
        "بیو رو تغییر بده",
        "update my bio please",
    ):
        result = parse_command_intent(text)
        assert result.action != "get_bio", text
        assert result.action != "bio_status", text
        assert result.kind == KIND_CONVERSATIONAL, text


def test_bio_read_queries_still_resolve_deterministically():
    for text in ("what is my bio?", "بیوم الان چیه؟", "show me my bio", "وضعیت بایو چیه"):
        result = parse_command_intent(text)
        assert result.action == "get_bio", text


def test_chit_chat_never_routes_to_create_task():
    assert parse_command_intent("سلام خوبی؟").kind == KIND_CONVERSATIONAL


# ═══════════════════════ 2. RESPONSE-SHAPE CLASSIFICATION (possibilities B/D) ═══════════════════════


@pytest.mark.asyncio
async def test_json_null_produces_interpretation_error_with_null_shape(caplog):
    with caplog.at_level(logging.INFO, logger="backend.ai.task_interpreter"):
        with pytest.raises(TaskInterpretationError):
            await _interpret("null")
    assert "response_shape=null" in caplog.text
    assert "stage=candidate_rejected" in caplog.text


@pytest.mark.asyncio
async def test_malformed_json_produces_interpretation_error_with_malformed_shape(caplog):
    with caplog.at_level(logging.INFO, logger="backend.ai.task_interpreter"):
        with pytest.raises(TaskInterpretationError):
            await _interpret('{"label": "x", "schedule_type": "interval", "actions": [')
    assert "response_shape=malformed" in caplog.text


@pytest.mark.asyncio
async def test_schema_violating_object_produces_error_with_object_shape(caplog):
    # Missing REQUIRED top-level timezone (the old prompt contradiction's
    # failure mode) — must be a distinguishable object-shaped rejection.
    bad = json.dumps({
        "label": "Bio", "schedule_type": "interval", "schedule": {"seconds": 300},
        "actions": [{"name": "bio_set_text", "arguments": {"text": ""}}],
        "notification_destination": {},
    })
    with caplog.at_level(logging.INFO, logger="backend.ai.task_interpreter"):
        with pytest.raises(TaskInterpretationError):
            await _interpret(bad)
    assert "response_shape=object" in caplog.text
    assert "candidate fields are incomplete or unsupported" in caplog.text


@pytest.mark.asyncio
async def test_array_response_produces_error_with_array_shape(caplog):
    with caplog.at_level(logging.INFO, logger="backend.ai.task_interpreter"):
        with pytest.raises(TaskInterpretationError):
            await _interpret("[1, 2, 3]")
    assert "response_shape=array" in caplog.text


@pytest.mark.asyncio
async def test_unsupported_envelope_raises_distinct_error_and_is_traced():
    from backend.ai.task_interpreter import TaskUnsupportedError

    with pytest.raises(TaskUnsupportedError) as exc_info:
        await _interpret(json.dumps({"unsupported": "monthly recurrence"}))
    assert exc_info.value.capability == "monthly recurrence"


@pytest.mark.asyncio
async def test_valid_response_is_parsed_and_traced_as_object(caplog):
    good = json.dumps({
        "label": "Bio update", "schedule_type": "interval", "schedule": {"seconds": 300},
        "timezone": TZ,
        "actions": [{"name": "bio_set_text", "arguments": {"text": ""}}],
        "notification_destination": {},
        "ai_instruction": LIVE_PERSIAN_REQUEST,
    }, ensure_ascii=False)
    with caplog.at_level(logging.INFO, logger="backend.ai.task_interpreter"):
        candidate = await _interpret(good)
    assert candidate.schedule == {"seconds": 300}
    assert candidate.ai_instruction == LIVE_PERSIAN_REQUEST
    assert "response_shape=object" in caplog.text
    assert "stage=candidate_parsed" in caplog.text


@pytest.mark.asyncio
async def test_fence_wrapped_valid_json_is_parsed():
    good = '```json\n{"label": "T", "schedule_type": "interval", "schedule": {"seconds": 60}, ' \
           '"timezone": "UTC", "actions": [{"name": "send_message", "arguments": {"text": "x"}}], ' \
           '"notification_destination": {}}\n```'
    candidate = await _interpret(good)
    assert candidate.schedule == {"seconds": 60}


# ═══════════════════════ 3. TIMECONE CONTRADICTION PIN (possibility F) ═══════════════════════


@pytest.mark.asyncio
async def test_prompt_contract_removes_timezone_contradiction():
    """The old 'interval schedules carry no timezone field' wording is gone;
    the top-level timezone is stated as REQUIRED for every schedule type."""
    provider = _Provider("null")
    with pytest.raises(TaskInterpretationError):
        await TaskInterpreter(provider).interpret(LIVE_PERSIAN_REQUEST, timezone=TZ)
    system = provider.last_messages[0]["content"]
    assert "interval schedules carry no timezone field" not in system
    assert "TOP-LEVEL 'timezone' field" in system
    assert "REQUIRED by the schema" in system
    assert '"timezone": "Asia/Tehran"' in system  # concrete example, no placeholder
    assert "<owner timezone>" not in system


@pytest.mark.asyncio
async def test_prompt_contract_teaches_valid_json_escaping_for_multiline_instruction():
    provider = _Provider("null")
    with pytest.raises(TaskInterpretationError):
        await TaskInterpreter(provider).interpret(LIVE_PERSIAN_REQUEST, timezone=TZ)
    system = provider.last_messages[0]["content"]
    assert "escape line breaks as \\n inside the JSON string" in system


@pytest.mark.asyncio
async def test_placeholder_timezone_candidate_is_rejected_deterministically():
    """A model echoing the old '<owner timezone>' placeholder (or any
    non-IANA value) for a timezone-consuming schedule fails closed."""
    from backend.ai.task_candidate import TaskCandidateError, parse_candidate_output

    bad = {
        "label": "T", "schedule_type": "daily", "schedule": {"hour": 9, "timezone": "<owner timezone>"},
        "timezone": "<owner timezone>",
        "actions": [{"name": "send_message", "arguments": {"text": "x"}}],
        "notification_destination": {},
    }
    with pytest.raises(TaskCandidateError):
        parse_candidate_output(bad)


# ═══════════════════════ 4. CREATE_TASK MESSAGE MAPPING ═══════════════════════


@pytest.mark.asyncio
async def test_create_task_maps_failure_modes_to_distinct_messages():
    """Null/malformed/schema violations keep the generic ambiguity message
    (trace classifies the shape); unsupported capabilities get the distinct
    honest message; a compliant candidate creates the task."""
    from backend.ai.database import manager as dbm
    from backend.ai.tools.context import ToolContext
    from backend.ai.tools.task import CreateTaskTool

    async def run(response_text: str):
        from tests.test_task_semantic_triggers import _FakeProvider  # noqa: PLC0415
        from backend.ai.providers.manager.manager import ProviderManager

        pm = ProviderManager()
        provider = _FakeProvider(response_text)
        pm.register_provider(provider)
        pm.switch_provider("fake")
        pm._fallback_chain = []
        ctx = ToolContext(
            telegram=None, owner_id=777, tz_str=TZ, client=None,
            extra={"provider_manager": pm, "chat_id": -1001},
        )
        manager = dbm.RepositoryManager(supabase_available=False)
        with patch.object(dbm, "get_repository_manager", return_value=manager):
            result = await CreateTaskTool(ctx).execute(ctx, {"request": LIVE_PERSIAN_REQUEST})
        return result, manager

    null_result, _ = await run("null")
    assert null_result.success is False
    assert "could not turn that into a safe, unambiguous schedule" in null_result.message

    unsupported_result, _ = await run(json.dumps({"unsupported": "monthly recurrence"}))
    assert unsupported_result.success is False
    assert "not supported yet" in unsupported_result.message
    assert "could not turn that into a safe, unambiguous schedule" not in unsupported_result.message

    good = json.dumps({
        "label": "Bio update", "schedule_type": "interval", "schedule": {"seconds": 300},
        "timezone": TZ,
        "actions": [{"name": "bio_set_text", "arguments": {"text": ""}}],
        "notification_destination": {},
        "ai_instruction": LIVE_PERSIAN_REQUEST,
    }, ensure_ascii=False)
    ok_result, manager = await run(good)
    assert ok_result.success is True, ok_result.message
    tasks = await manager.task.list_tasks(777)
    assert len(tasks) == 1
    assert tasks[0].schedule == {"seconds": 300.0}
    assert tasks[0].ai_instruction == LIVE_PERSIAN_REQUEST