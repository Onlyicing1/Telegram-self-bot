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

    async def chat(self, messages, **kwargs):
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
    """Every rejection layer now carries a bounded, content-free failure
    category in the user-facing message, so one live reproduction identifies
    the exact layer (provider-null vs malformed JSON vs schema violation vs
    provider failure) instead of one indistinguishable generic text.
    Unsupported capabilities keep the distinct honest message; a compliant
    candidate creates the task."""
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
    assert "[failure category: candidate_invalid:null]" in null_result.message

    malformed_result, _ = await run("{\"label\": \"x\"")
    assert malformed_result.success is False
    assert "[failure category: candidate_invalid_json]" in malformed_result.message

    schema_result, _ = await run(json.dumps({
        "label": "T", "schedule_type": "interval", "schedule": {"seconds": 300},
        "actions": [{"name": "bio_set_text", "arguments": {"text": ""}}],
        "notification_destination": {},
    }))  # missing required top-level timezone
    assert schema_result.success is False
    assert "[failure category: candidate_invalid:object]" in schema_result.message

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


# ═══════════════════════ 5. JSON-EXTRACTION TOLERANCE & DIAGNOSTICS ═══════════════════════
#
# Live evidence: the exact multi-line Persian request reached
# `[failure category: candidate_invalid_json]` — a JSONDecodeError BEFORE
# candidate validation. Source contract: provider adapters (Gemini text-part
# join, OpenAI-compat message content) deliver ONE plain text string; the
# interpreter prompt does not forbid prose around the object. The tolerated
# wrapper shapes below are deterministic parse tolerances only — every parsed
# result still passes the FULL parse_candidate_output validation.


def _good_candidate_json() -> str:
    return json.dumps({
        "label": "Bio update", "schedule_type": "interval", "schedule": {"seconds": 300},
        "timezone": TZ,
        "actions": [{"name": "bio_set_text", "arguments": {"text": ""}}],
        "notification_destination": {},
        "ai_instruction": LIVE_PERSIAN_REQUEST,
    }, ensure_ascii=False)


@pytest.mark.parametrize("wrapped", [
    _good_candidate_json(),
    "```json\n" + _good_candidate_json() + "\n```",
    "Here is the JSON:\n```json\n" + _good_candidate_json() + "\n```\nHope this helps!",
    "Here is the JSON:\n" + _good_candidate_json() + "\nHope this helps!",
    json.dumps(_good_candidate_json()),
    _good_candidate_json().replace("\\n", "\n"),
], ids=["direct", "fenced", "prose_plus_fenced", "prose_unfenced", "double_encoded", "raw_newlines"])
@pytest.mark.asyncio
async def test_json_extraction_tolerates_contract_permitted_wrappers(wrapped):
    """Every wrapper shape the provider contract permits must parse and then
    pass the FULL candidate validation — the live multi-line request is the
    raw-newlines case (unescaped control characters)."""
    candidate = await _interpret(wrapped)
    assert candidate.schedule_type == "interval"
    assert candidate.schedule == {"seconds": 300.0}
    assert candidate.actions == [{"name": "bio_set_text", "arguments": {"text": ""}}]
    assert candidate.ai_instruction == LIVE_PERSIAN_REQUEST


@pytest.mark.parametrize("bad, category", [
    (_good_candidate_json()[:-40], "candidate_invalid_json:truncated"),
    ("{\"label\": 12x3}", "candidate_invalid_json"),
    ("", "candidate_invalid_json:empty"),
    ("Sure, here is the JSON you asked for!", "candidate_invalid_json"),
], ids=["truncated", "malformed", "empty", "prose_only"])
@pytest.mark.asyncio
async def test_json_extraction_fails_closed_with_distinct_categories(bad, category):
    """Genuinely unparseable output is rejected (never fabricated into a
    candidate) and the user-facing category distinguishes truncated JSON from
    other malformed shapes."""
    from backend.ai.database import manager as dbm
    from backend.ai.tools.context import ToolContext
    from backend.ai.tools.task import CreateTaskTool
    from tests.test_task_semantic_triggers import _FakeProvider  # noqa: PLC0415
    from backend.ai.providers.manager.manager import ProviderManager

    pm = ProviderManager()
    provider = _FakeProvider(bad)
    pm.register_provider(provider)
    pm.switch_provider("fake")
    pm._fallback_chain = []
    ctx = ToolContext(
        telegram=None, owner_id=778, tz_str=TZ, client=None,
        extra={"provider_manager": pm, "chat_id": -1001},
    )
    manager = dbm.RepositoryManager(supabase_available=False)
    with patch.object(dbm, "get_repository_manager", return_value=manager):
        result = await CreateTaskTool(ctx).execute(ctx, {"request": LIVE_PERSIAN_REQUEST})
    assert result.success is False
    assert f"[failure category: {category}]" in result.message
    tasks = await manager.task.list_tasks(778)
    assert tasks == []  # zero persistence on unparseable output


@pytest.mark.asyncio
async def test_json_diagnostics_log_parser_metadata_without_content(caplog):
    """The candidate_rejected trace for malformed JSON carries content-free
    parser metadata (error type, line, column, position, bounded length,
    truncation flag) and NEVER the raw response or the user request."""
    from backend.ai.providers.base.contract import ProviderResponse

    truncated_raw = _good_candidate_json()[:-40]

    class _MetaProvider:
        async def chat(self, messages, **kwargs):
            return ProviderResponse(
                text=truncated_raw, provider_name="stub", success=True,
                metadata={"finish_reason": "MAX_TOKENS"},
            )

    with caplog.at_level(logging.INFO):
        with pytest.raises(TaskInterpretationError):
            await TaskInterpreter(_MetaProvider()).interpret(
                LIVE_PERSIAN_REQUEST, timezone=TZ, request_id="req-42"
            )
    line = next(
        r.getMessage() for r in caplog.records
        if "AI_TASK_TRACE" in r.getMessage() and "stage=candidate_parse_error" in r.getMessage()
    )
    assert "response_shape=malformed" in line
    assert "json_error=JSONDecodeError" in line
    assert "line=" in line and "col=" in line and "pos=" in line
    assert "raw_len=" in line
    assert "truncated=True" in line
    assert "provider_finish_reason=MAX_TOKENS" in line
    assert "request_id=req-42" in line
    assert LIVE_PERSIAN_REQUEST not in line  # never the user's request
    assert "bio_set_text" not in line  # never the candidate content


@pytest.mark.asyncio
async def test_finish_reason_truncation_classifies_even_when_json_parses(caplog):
    """A provider finish_reason of MAX_TOKENS/length is a truncation signal
    the trace must surface even if the partial JSON happens to parse."""
    from backend.ai.providers.base.contract import ProviderResponse

    partial = _good_candidate_json()
    partial = partial[: partial.rfind("}")] + "}"  # structurally valid, semantically cut

    class _CutProvider:
        async def chat(self, messages, **kwargs):
            return ProviderResponse(
                text=partial, provider_name="stub", success=True,
                metadata={"finish_reason": "length"},
            )

    with caplog.at_level(logging.INFO):
        await TaskInterpreter(_CutProvider()).interpret(
            LIVE_PERSIAN_REQUEST, timezone=TZ, request_id="req-43"
        )
    line = next(
        r.getMessage() for r in caplog.records
        if "AI_TASK_TRACE" in r.getMessage() and "stage=candidate_parsed" in r.getMessage()
    )
    assert "request_id=req-43" in line
    assert "provider_finish_reason=length" in line


# ═══════════════ 6. PROVIDER-RESPONSE INSTRUMENTATION (behavior-unchanged) ═══════════════
#
# Instrumentation-only phase: the raw_response_shape trace classifies
# the REAL provider response content-free BEFORE parsing; the
# candidate_parse_error trace enriches the JSON failure with provider/model/
# structural metadata. NO parser, prompt, or validation behavior is changed
# — the tolerance matrix above still passes unchanged with instrumentation
# in place.


def _meta_provider(text: str):
    from backend.ai.providers.base.contract import ProviderResponse

    class _MetaProvider:
        async def chat(self, messages, **kwargs):
            return ProviderResponse(
                text=text, provider_name="stub", success=True,
                metadata={"model": "stub-model", "finish_reason": "stop"},
            )

    return _MetaProvider()


@pytest.mark.parametrize("wrapped, first, extra", [
    (_good_candidate_json(), "object", {}),
    ("```json\n" + _good_candidate_json() + "\n```", "fence", {"contains_fence": "True"}),
    ("Here is the JSON:\n" + _good_candidate_json(), "other", {"leading_prose": "True"}),
    (json.dumps(_good_candidate_json()), "quote", {}),
    (_good_candidate_json().replace("\\n", "\n"), "object", {"has_control_chars": "True"}),
    (_good_candidate_json()[:-30], "object", {"trailing_prose": "True", "object_span": "False"}),
    ("", "empty", {}),
    ("[1,2,3]", "array", {}),
    ("Sure thing!", "other", {"trailing_prose": "True"}),
], ids=["direct", "fenced", "prose_unfenced", "double_encoded", "raw_newlines", "truncated", "empty", "array", "prose_only"])
@pytest.mark.asyncio
async def test_provider_response_shape_trace_classifies_structure(wrapped, first, extra, caplog):
    """The raw_response_shape trace carries content-free structural
    categories (never request/candidate content) for every synthetic
    ProviderResponse shape the provider contract permits."""
    with caplog.at_level(logging.INFO):
        try:
            await TaskInterpreter(_meta_provider(wrapped)).interpret(
                LIVE_PERSIAN_REQUEST, timezone=TZ, request_id="req-shape"
            )
        except TaskInterpretationError:
            pass
    shape_line = next(
        r.getMessage() for r in caplog.records
        if "AI_TASK_TRACE" in r.getMessage() and "stage=raw_response_shape" in r.getMessage()
    )
    assert "request_id=req-shape" in shape_line
    assert "provider=stub" in shape_line
    assert "model=stub-model" in shape_line
    assert "success=True" in shape_line
    assert f"first_non_ws={first}" in shape_line
    for key, value in extra.items():
        assert f"{key}={value}" in shape_line
    assert "raw_len=" in shape_line
    assert "finish_reason=stop" in shape_line
    # Content-free: never the user's request, the action name, or config values
    assert LIVE_PERSIAN_REQUEST not in shape_line
    assert "bio_set_text" not in shape_line
    assert "Asia/Tehran" not in shape_line


@pytest.mark.asyncio
async def test_parse_error_trace_carries_provider_and_json_metadata(caplog):
    """The candidate_parse_error trace identifies provider, model, JSON error
    position metadata, and the structural classification — content-free."""
    truncated = _good_candidate_json()[:-30]
    with caplog.at_level(logging.INFO):
        with pytest.raises(TaskInterpretationError):
            await TaskInterpreter(_meta_provider(truncated)).interpret(
                LIVE_PERSIAN_REQUEST, timezone=TZ, request_id="req-perr"
            )
    err_line = next(
        r.getMessage() for r in caplog.records
        if "AI_TASK_TRACE" in r.getMessage() and "stage=candidate_parse_error" in r.getMessage()
    )
    assert "request_id=req-perr" in err_line
    assert "category=candidate_invalid_json" in err_line
    assert "provider=stub" in err_line
    assert "model=stub-model" in err_line
    assert "json_error=JSONDecodeError" in err_line
    assert "line=" in err_line and "col=" in err_line and "pos=" in err_line
    assert "raw_len=" in err_line
    assert "first_non_ws=object" in err_line
    assert "object_span=False" in err_line
    assert LIVE_PERSIAN_REQUEST not in err_line
    assert "bio_set_text" not in err_line


@pytest.mark.asyncio
async def test_schema_invalid_json_is_not_labeled_a_parse_error(caplog):
    """Valid JSON that fails candidate schema validation reaches schema
    validation (response_shape=object) and is never mislabeled
    candidate_invalid_json."""
    from backend.ai.tools.context import ToolContext
    from backend.ai.tools.task import CreateTaskTool
    from tests.test_task_semantic_triggers import _FakeProvider  # noqa: PLC0415
    from backend.ai.providers.manager.manager import ProviderManager

    schema_bad = json.dumps({
        "label": "T", "schedule_type": "interval", "schedule": {"seconds": 300},
        "actions": [{"name": "bio_set_text", "arguments": {"text": ""}}],
        "notification_destination": {},
    })  # missing required top-level timezone
    pm = ProviderManager()
    provider = _FakeProvider(schema_bad)
    pm.register_provider(provider)
    pm.switch_provider("fake")
    pm._fallback_chain = []
    ctx = ToolContext(
        telegram=None, owner_id=779, tz_str=TZ, client=None,
        extra={"provider_manager": pm, "chat_id": -1001},
    )
    from backend.ai.database import manager as dbm
    manager = dbm.RepositoryManager(supabase_available=False)
    with caplog.at_level(logging.INFO):
        with patch.object(dbm, "get_repository_manager", return_value=manager):
            result = await CreateTaskTool(ctx).execute(ctx, {"request": LIVE_PERSIAN_REQUEST})
    assert result.success is False
    assert "candidate_invalid:object" in result.message
    assert "candidate_invalid_json" not in result.message
    trace_lines = [r.getMessage() for r in caplog.records if "AI_TASK_TRACE" in r.getMessage()]
    assert any("response_shape=object" in line for line in trace_lines)
    assert not any("stage=candidate_parse_error" in line for line in trace_lines)
    # The shape trace ran on this path too
    assert any("stage=raw_response_shape" in line for line in trace_lines)


@pytest.mark.asyncio
async def test_raw_response_shape_reached_before_json_parsing_on_malformed_path(caplog):
    """Control-flow guarantee: the boundary shape record executes BEFORE the
    parser — even on the malformed path where parsing fails, both records
    appear, so the shape classification can never be skipped by a parse
    failure."""
    malformed = "{\"label\": 12x3}"
    with caplog.at_level(logging.INFO):
        with pytest.raises(TaskInterpretationError):
            await TaskInterpreter(_meta_provider(malformed)).interpret(
                LIVE_PERSIAN_REQUEST, timezone=TZ, request_id="req-order"
            )
    messages = [r.getMessage() for r in caplog.records if "AI_TASK_TRACE" in r.getMessage()]
    shape_idx = next(i for i, m in enumerate(messages) if "stage=raw_response_shape" in m)
    err_idx = next(i for i, m in enumerate(messages) if "stage=candidate_parse_error" in m)
    assert shape_idx < err_idx
    assert "provider=stub" in messages[shape_idx]
    assert "raw_len=15" in messages[err_idx]  # len('{"label": 12x3}')


@pytest.mark.asyncio
async def test_missing_optional_metadata_does_not_crash_instrumentation(caplog):
    """A ProviderResponse with NO model/finish_reason/http_status/failure_type
    metadata still produces the complete shape record with '-' placeholders."""
    from backend.ai.providers.base.contract import ProviderResponse

    class _BareProvider:
        async def chat(self, messages, **kwargs):
            return ProviderResponse(
                text=_good_candidate_json(), provider_name="bare", success=True,
            )  # metadata defaults to {}

    with caplog.at_level(logging.INFO):
        await TaskInterpreter(_BareProvider()).interpret(
            LIVE_PERSIAN_REQUEST, timezone=TZ, request_id="req-bare"
        )
    line = next(
        r.getMessage() for r in caplog.records
        if "AI_TASK_TRACE" in r.getMessage() and "stage=raw_response_shape" in r.getMessage()
    )
    assert "provider=bare" in line
    assert "model=-" in line
    assert "finish_reason=-" in line
    assert "http_status=-" in line
    assert "failure_type=-" in line