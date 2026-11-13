"""AI-scheduled bio task preparation: end-to-end diagnosis of every stage.

Reproduces the production ``Update Bio`` path (its stored instruction is
``BIO_INSTRUCTION``) through the REAL coordinator, preparator, policy and
ToolExecutor, with a scripted ProviderManager. Each preparation failure now
persists a bounded category + stage instead of the generic ``unclassified``
the retry classifier produces, and the failure record never carries generated
content.

The model still never becomes an execution authority: the task's tool name is
fixed, exactly one execution follows a successful preparation, and nothing
executes when preparation fails.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

import pytest

from backend.ai.database.task_repository import InMemoryTaskRepository
from backend.ai.providers.base import ProviderResponse
from backend.ai.task_execution import (
    AIActionPreparator,
    MAX_PREPARATION_ATTEMPTS,
    TaskExecutionCoordinator,
)
from backend.ai.tools.base import PermissionLevel, ToolResult
from backend.ai.tools.context import ToolContext
from backend.ai.tools.executor import ToolExecutor
from backend.ai.tools.registry import ToolRegistry

OWNER = 42
BIO_INSTRUCTION = (
    "update my bio, with a randomly generated dialogue, in English, "
    "at most 70 characters, from Shikamaru Nara"
)
LABEL = "Shikamaru Nara: "
DIALOGUE = "What a drag... I'd rather watch the clouds."


class RecordingBioTool:
    permission_level = PermissionLevel.READ_WRITE
    long_running = False
    safe = True
    return_type = "object"
    description = "test bio tool"
    parameters = {"text": {"type": "string"}}

    def __init__(self, calls: list) -> None:
        self.name = "bio_set_text"
        self.calls = calls

    async def execute(self, context, arguments):
        self.calls.append((self.name, dict(arguments), context.owner_id))
        return ToolResult(True, "ok")


class _ScriptedProviders:
    """ProviderManager double: one response per ``chat`` call, in order."""

    def __init__(self, *responses) -> None:
        self._responses = list(responses)
        self.calls: list[dict] = []

    async def chat(self, messages, **kwargs):
        self.calls.append({"messages": messages, "kwargs": kwargs})
        index = min(len(self.calls) - 1, len(self._responses) - 1)
        return self._responses[index]


class _RaisingProviders:
    def __init__(self, error: BaseException) -> None:
        self._error = error
        self.calls: list[dict] = []

    async def chat(self, messages, **kwargs):
        self.calls.append({"messages": messages, "kwargs": kwargs})
        raise self._error


def _response(text: str, success: bool = True, **metadata) -> ProviderResponse:
    return ProviderResponse(
        text=text, provider_name="gemini", success=success, metadata=dict(metadata),
    )


def _json_body(text: str, *, name: str = "bio_set_text", key: str = "text") -> str:
    return json.dumps({"actions": [{"name": name, "arguments": {key: text}}]})


def _task_data(actions=None, instruction: str = BIO_INSTRUCTION) -> dict:
    return {
        "label": "Update Bio",
        "schedule_type": "once",
        "schedule": {"at": "2026-09-01T12:00:00", "timezone": "UTC"},
        "timezone": "Asia/Tehran",
        "actions": actions if actions is not None else [{"name": "bio_set_text", "arguments": {"text": ""}}],
        "notification_destination": {},
        "ai_instruction": instruction,
    }


async def _run(providers, *, actions=None, instruction: str = BIO_INSTRUCTION):
    repo = InMemoryTaskRepository()
    task = await repo.create_task(OWNER, _task_data(actions, instruction))
    calls: list = []
    registry = ToolRegistry()
    registry.register(RecordingBioTool(calls))
    context = ToolContext(None, OWNER, "Asia/Tehran")
    executor = ToolExecutor(registry, context)
    coordinator = TaskExecutionCoordinator(
        repo, executor, OWNER, context, preparator=AIActionPreparator(providers),
    )
    await repo.create_occurrence(OWNER, {
        "task_id": task.id,
        "occurrence_key": "k",
        "definition_version": task.version,
        "action_snapshot": task.actions,
        "scheduled_for": datetime.now(timezone.utc),
    })
    claimed = await repo.claim_occurrence(OWNER, task.id, "k")
    result = await coordinator.execute(claimed)
    stored = await repo.get_occurrence(OWNER, task.id, "k")
    return result, stored, calls


# ── The production path: a valid Shikamaru dialogue is prepared and executed ─


@pytest.mark.asyncio
async def test_valid_dialogue_is_prepared_then_executed_exactly_once():
    providers = _ScriptedProviders(_response(_json_body(LABEL + DIALOGUE)))
    result, stored, calls = await _run(providers)

    assert result.success and result.status == "succeeded"
    assert result.action_count == 1 and result.successful_actions == 1
    # Exactly one execution, through the task's OWN fixed tool.
    assert [call[0] for call in calls] == ["bio_set_text"]
    # The validated opening label is a generation constraint only: the executed
    # text is the visible dialogue (the same content the policy measured).
    assert calls[0][1] == {"text": DIALOGUE}
    assert len(calls[0][1]["text"]) <= 70
    # The attributed line is what the occurrence audited.
    audited = stored.preparation_metadata["action"]
    assert audited["name"] == "bio_set_text"
    assert audited["arguments"]["text"] == LABEL + DIALOGUE
    assert providers.calls[0]["kwargs"] == {"tools": []}


# ── Content policy: language, visible length, attribution ───────────────────


@pytest.mark.asyncio
async def test_persian_dialogue_is_rejected_as_content_policy():
    providers = _ScriptedProviders(_response(_json_body(LABEL + "چه دردسر...")))
    result, stored, calls = await _run(providers)

    assert not result.success and result.status == "failed"
    assert calls == []
    assert result.error == "content_policy_rejected"
    assert stored.error_metadata["error_class"] == "content_policy_rejected"
    assert stored.error_metadata["failure_stage"] == "content_policy"
    assert stored.error_metadata["preparation_attempts"] == MAX_PREPARATION_ATTEMPTS


@pytest.mark.asyncio
async def test_visible_length_is_enforced_on_generated_content():
    over = _ScriptedProviders(_response(_json_body(LABEL + "a" * 71)))
    result, stored, calls = await _run(over)
    assert calls == []
    assert stored.error_metadata["error_class"] == "content_policy_rejected"

    exact = _ScriptedProviders(_response(_json_body(LABEL + "a" * 70)))
    result, stored, calls = await _run(exact)
    assert result.success
    assert calls[0][1] == {"text": "a" * 70}


@pytest.mark.asyncio
@pytest.mark.parametrize("generated", [DIALOGUE, "Shikamaru: " + DIALOGUE])
async def test_attribution_to_the_requested_source_is_required(generated):
    providers = _ScriptedProviders(_response(_json_body(generated)))
    result, stored, calls = await _run(providers)

    assert not result.success
    assert calls == []
    assert stored.error_metadata["error_class"] == "content_policy_rejected"
    assert stored.error_metadata["failure_stage"] == "content_policy"


# ── Precise violation codes through the real coordinator ──────────────────


@pytest.mark.asyncio
async def test_visible_length_code_is_persisted():
    providers = _ScriptedProviders(_response(_json_body(LABEL + "a" * 71)))
    result, stored, calls = await _run(providers)

    assert calls == []
    assert stored.error_metadata["error_class"] == "content_policy_rejected"
    assert stored.error_metadata["failure_stage"] == "content_policy"
    assert stored.error_metadata["policy_violation"] == "visible_length_exceeded"
    assert stored.error_metadata["preparation_attempts"] == MAX_PREPARATION_ATTEMPTS


@pytest.mark.asyncio
async def test_missing_and_wrong_attribution_persist_distinct_codes():
    missing = _ScriptedProviders(_response(_json_body(DIALOGUE)))
    _, stored, calls = await _run(missing)
    assert calls == []
    assert stored.error_metadata["policy_violation"] == "attribution_missing"

    wrong = _ScriptedProviders(_response(_json_body("Shikamaru: " + DIALOGUE)))
    _, stored, calls = await _run(wrong)
    assert calls == []
    assert stored.error_metadata["policy_violation"] == "attribution_mismatch"


@pytest.mark.asyncio
async def test_language_code_is_persisted():
    providers = _ScriptedProviders(_response(_json_body(LABEL + "چه دردسر...")))
    _, stored, calls = await _run(providers)

    assert calls == []
    assert stored.error_metadata["policy_violation"] == "language_mismatch"


@pytest.mark.asyncio
async def test_exact_length_and_empty_content_codes_are_persisted():
    exact = _ScriptedProviders(_response(_json_body(LABEL + "abc")))
    _, stored, _ = await _run(
        exact,
        instruction=(
            "update my bio, with a randomly generated dialogue, in English, "
            "exactly 10 characters, from Shikamaru Nara"
        ),
    )
    assert stored.error_metadata["policy_violation"] == "visible_length_mismatch"

    empty = _ScriptedProviders(_response(_json_body("")))
    _, stored, _ = await _run(empty)
    assert stored.error_metadata["policy_violation"] == "empty_content"


@pytest.mark.asyncio
async def test_policy_diagnostic_log_is_precise_and_content_free(caplog):
    import logging

    providers = _ScriptedProviders(_response(_json_body(LABEL + "a" * 71)))
    with caplog.at_level(logging.WARNING, logger="backend.ai.task_execution"):
        _, stored, calls = await _run(providers)

    assert calls == []
    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert "violation=visible_length_exceeded" in messages
    assert DIALOGUE not in messages
    assert "a" * 71 not in messages
    assert BIO_INSTRUCTION not in messages


# ── Parse / structure stages ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_prose_wrapped_json_is_invalid_json():
    providers = _ScriptedProviders(_response("Sure! " + _json_body(LABEL + DIALOGUE)))
    result, stored, calls = await _run(providers)

    assert calls == []
    assert result.error == "invalid_json"
    assert stored.error_metadata["failure_stage"] == "parse"
    assert stored.error_metadata["preparation_attempts"] == MAX_PREPARATION_ATTEMPTS


@pytest.mark.asyncio
@pytest.mark.parametrize("body, expected_calls", [
    (_json_body(LABEL + DIALOGUE, name="delete"), 1),
    (json.dumps({"actions": []}), 1),
    (_json_body(LABEL + DIALOGUE, key="content"), 1),
    (json.dumps({"actions": [{"name": "bio_set_text", "arguments": {"text": LABEL + DIALOGUE}}], "note": "x"}), 1),
])
async def test_malformed_action_structure_fails_without_regeneration(body, expected_calls):
    providers = _ScriptedProviders(_response(body))
    result, stored, calls = await _run(providers)

    assert calls == []
    assert result.error == "invalid_action_structure"
    assert stored.error_metadata["error_class"] == "invalid_action_structure"
    assert stored.error_metadata["failure_stage"] == "structure"
    # A structural violation cannot be fixed by regenerating: one provider call.
    assert len(providers.calls) == expected_calls
    assert stored.error_metadata["preparation_attempts"] == 1


# ── Provider stages ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_provider_failure_preserves_the_provider_origin():
    providers = _ScriptedProviders(
        _response("API error (404): model not found", success=False, failure_origin="model_lookup")
    )
    result, stored, calls = await _run(providers)

    assert calls == []
    assert result.error == "provider_failure"
    assert stored.error_metadata["failure_stage"] == "provider_request"
    assert stored.error_metadata["provider_failure_origin"] == "model_lookup"
    # The bounded preparation budget is preserved for a provider failure.
    assert len(providers.calls) == MAX_PREPARATION_ATTEMPTS


@pytest.mark.asyncio
async def test_empty_provider_response_is_its_own_stage():
    providers = _ScriptedProviders(_response("", success=True, finish_reason="MAX_TOKENS"))
    result, stored, calls = await _run(providers)

    assert calls == []
    assert result.error == "empty_response"
    assert stored.error_metadata["failure_stage"] == "provider_response"
    assert stored.error_metadata["provider_finish_reason"] == "max_tokens"


@pytest.mark.asyncio
async def test_provider_timeout_stays_retryable():
    providers = _RaisingProviders(asyncio.TimeoutError())
    result, stored, calls = await _run(providers)

    assert calls == []
    assert not result.success
    assert result.status == "retry_pending"
    assert stored.status == "retry_pending"
    assert stored.attempt == 2


# ── The failure record stays safe and diagnosable ───────────────────────────


@pytest.mark.asyncio
async def test_failure_record_is_safe_and_never_carries_generated_content():
    generated = LABEL + DIALOGUE
    providers = _ScriptedProviders(_response(_json_body("Shikamaru: " + DIALOGUE)))
    result, stored, calls = await _run(providers)

    persisted = json.dumps(stored.error_metadata, ensure_ascii=False)
    assert DIALOGUE not in persisted
    assert "Shikamaru" not in persisted
    assert BIO_INSTRUCTION not in persisted
    assert set(stored.error_metadata) <= {
        "error_class", "attempt", "action_count", "successful_action_count",
        "failure_stage", "preparation_attempts", "provider_failure_origin",
        "provider_finish_reason", "policy_violation", "actions",
    }
    assert stored.error_metadata["action_count"] == 1
    assert stored.error_metadata["successful_action_count"] == 0
    assert generated not in persisted
    # The failure is no longer indistinguishable from every other failure.
    assert stored.error_metadata["error_class"] != "unclassified"


@pytest.mark.asyncio
async def test_fixed_tool_name_survives_a_model_that_swaps_it():
    providers = _ScriptedProviders(_response(_json_body("anything", name="delete")))
    result, stored, calls = await _run(providers)

    assert calls == []
    assert result.error == "invalid_action_structure"
    assert stored.error_metadata["error_class"] != "content_policy_rejected"
