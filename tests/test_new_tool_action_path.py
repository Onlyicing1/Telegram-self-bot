"""Live-path regression tests for the four newly connected AI tools.

Phase-1 health tests proved registration/executor reachability. Production
evidence then showed the tools still fell back with no action taken: the
deterministic JSON-action path (Dispatcher._apply_structured_action →
parse_action_text → resolve_tool_calls) REJECTED the new actions because the
vocabulary in backend/ai/actions.py never learned them, and the prompt's
JSON fallback schema never advertised them.

These tests drive the REAL parse → resolve → tool-call shape contract end to
end so the model's JSON output can actually reach the registered tools.
"""
from __future__ import annotations

import pytest

from backend.ai.actions import (
    ACTION_NAMES,
    EXECUTABLE_ACTION_NAMES,
    parse_action_text,
)


# ── Vocabulary registration ──────────────────────────────────────────────────


def test_new_actions_are_in_the_action_vocabulary():
    for name in ("task_list", "task_inspect", "task_transition", "retrieve_save"):
        assert name in ACTION_NAMES
        assert name in EXECUTABLE_ACTION_NAMES


def test_prompt_output_contract_advertises_new_actions():
    from backend.ai.prompt.template import OUTPUT_INSTRUCTIONS_TEMPLATE

    for name in ("task_list", "task_inspect", "task_transition", "retrieve_save"):
        assert name in OUTPUT_INSTRUCTIONS_TEMPLATE


def test_action_nudge_advertises_task_and_retrieve_actions():
    from backend.ai.engine.dispatcher import _ENFORCE_ACTION_NUDGE

    assert "task" in _ENFORCE_ACTION_NUDGE
    assert "retrieve a saved item" in _ENFORCE_ACTION_NUDGE


# ── task_list ────────────────────────────────────────────────────────────────


def test_task_list_action_resolves_to_tool_call():
    result = parse_action_text('{"action":"task_list"}')
    assert result.kind == "executable"
    assert result.tool_calls == [{"name": "task_list", "arguments": {}}]


def test_task_list_rejects_stray_fields():
    result = parse_action_text('{"action":"task_list","task_id":5}')
    assert result.kind == "invalid"
    assert "task_id" in result.error


def test_task_list_status_filter_resolves_to_tool_argument():
    result = parse_action_text('{"action":"task_list","status":"completed"}')
    assert result.kind == "executable"
    assert result.tool_calls == [
        {"name": "task_list", "arguments": {"status": "completed"}}
    ]


@pytest.mark.parametrize("payload", [
    '{"action":"task_list","status":"failed"}',
    '{"action":"task_list","status":""}',
    '{"action":"task_list","status":42}',
])
def test_task_list_rejects_invalid_status_filters(payload):
    result = parse_action_text(payload)
    assert result.kind == "invalid"


def test_task_list_status_field_rejected_for_other_actions():
    result = parse_action_text('{"action":"retrieve_save","save_code":"S1","status":"active"}')
    assert result.kind == "invalid"
    assert "status" in result.error


# ── task_inspect ─────────────────────────────────────────────────────────────


def test_task_inspect_action_resolves_with_task_id():
    result = parse_action_text('{"action":"task_inspect","task_id":3}')
    assert result.kind == "executable"
    assert result.tool_calls == [
        {"name": "task_inspect", "arguments": {"task_id": 3}}
    ]


@pytest.mark.parametrize("payload", [
    '{"action":"task_inspect"}',
    '{"action":"task_inspect","task_id":0}',
    '{"action":"task_inspect","task_id":-2}',
    '{"action":"task_inspect","task_id":"abc"}',
])
def test_task_inspect_rejects_invalid_task_ids(payload):
    result = parse_action_text(payload)
    assert result.kind == "invalid"


def test_task_inspect_rejects_unexpected_fields():
    result = parse_action_text(
        '{"action":"task_inspect","task_id":3,"expected_version":1}'
    )
    assert result.kind == "invalid"
    assert "expected_version" in result.error


# ── task_transition ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("status,verb", [
    ("paused", "paused"),
    ("active", "active"),
    ("completed", "completed"),
])
def test_task_transition_resolves_full_cas_arguments(status, verb):
    result = parse_action_text(
        '{"action":"task_transition","task_id":7,'
        f'"action_status":"{status}","expected_version":2}}'
    )
    assert result.kind == "executable"
    assert result.tool_calls == [{
        "name": "task_transition",
        "arguments": {
            "task_id": 7,
            "action": verb,
            "expected_version": 2,
        },
    }]


def test_task_transition_normalizes_status_case():
    result = parse_action_text(
        '{"action":"task_transition","task_id":7,'
        '"action_status":"Paused","expected_version":1}'
    )
    assert result.kind == "executable"
    assert result.tool_calls[0]["arguments"]["action"] == "paused"


def test_task_transition_rejects_nonlifecycle_status():
    result = parse_action_text(
        '{"action":"task_transition","task_id":7,'
        '"action_status":"delete","expected_version":1}'
    )
    assert result.kind == "invalid"
    assert "action_status" in result.error


def test_task_transition_requires_expected_version():
    result = parse_action_text(
        '{"action":"task_transition","task_id":7,"action_status":"paused"}'
    )
    assert result.kind == "invalid"
    assert "expected_version" in result.error


def test_task_transition_rejects_invalid_version():
    result = parse_action_text(
        '{"action":"task_transition","task_id":7,'
        '"action_status":"paused","expected_version":0}'
    )
    assert result.kind == "invalid"


# ── retrieve_save ────────────────────────────────────────────────────────────


def test_retrieve_save_resolves_and_canonicalizes_code():
    result = parse_action_text('{"action":"retrieve_save","save_code":"s0012"}')
    assert result.kind == "executable"
    assert result.tool_calls == [
        {"name": "retrieve_save", "arguments": {"save_code": "S0012"}}
    ]


def test_retrieve_save_accepts_random_alphabet_code():
    result = parse_action_text('{"action":"retrieve_save","save_code":"AB12"}')
    assert result.kind == "executable"
    assert result.tool_calls[0]["arguments"]["save_code"] == "AB12"


@pytest.mark.parametrize("payload", [
    '{"action":"retrieve_save"}',
    '{"action":"retrieve_save","save_code":""}',
    '{"action":"retrieve_save","save_code":"S-123"}',
    '{"action":"retrieve_save","save_code":"S 1"}',
    '{"action":"retrieve_save","save_code":123}',
])
def test_retrieve_save_rejects_invalid_codes(payload):
    result = parse_action_text(payload)
    assert result.kind == "invalid"


def test_save_code_field_is_rejected_for_other_actions():
    result = parse_action_text('{"action":"save","save_code":"S1"}')
    assert result.kind == "invalid"
    assert "save_code" in result.error


# ── No regression to the existing vocabulary ─────────────────────────────────


@pytest.mark.parametrize("payload,expected_tool", [
    ('{"action":"save","target":"replied_message"}', "save"),
    ('{"action":"delete_messages","target":"last_message","count":1}', "delete"),
    ('{"action":"create_task","request":"هر ۵ دقیقه بگو سلام"}', "create_task"),
    ('{"action":"list_saved_items"}', "list_saves"),
    ('{"action":"search_saved_items","query":"invoice"}', "search"),
    ('{"action":"get_bio"}', "get_bio"),
    ('{"action":"account_status","fields":["username"]}', "account_show"),
    ('{"action":"send","text":"hello"}', "send_message"),
])
def test_existing_actions_still_resolve(payload, expected_tool):
    result = parse_action_text(payload)
    assert result.kind == "executable"
    assert result.tool_calls[0]["name"] == expected_tool


def test_unknown_action_still_rejected():
    result = parse_action_text('{"action":"forward_to_friend"}')
    assert result.kind == "invalid"
