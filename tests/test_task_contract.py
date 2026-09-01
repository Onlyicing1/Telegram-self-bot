from __future__ import annotations

import pytest

from backend.ai.task_contract import (
    AIInstruction,
    PreparedAction,
    TaskContractError,
    MAX_AI_INSTRUCTION_CHARS,
)


def test_ai_instruction_is_explicit_and_round_trips():
    instruction = AIInstruction("Change my bio to a fresh short quote", version=3)
    assert instruction.as_dict() == {
        "kind": "ai_instruction",
        "version": 3,
        "text": "Change my bio to a fresh short quote",
    }


def test_ai_instruction_is_bounded():
    with pytest.raises(TaskContractError):
        AIInstruction("x" * (MAX_AI_INSTRUCTION_CHARS + 1))


def test_prepared_action_is_bounded_and_versioned():
    prepared = PreparedAction(
        definition_version=4,
        prepared_at="2026-09-01T12:00:00+00:00",
        action={"name": "send_message", "arguments": {"text": "hello"}},
    )
    value = prepared.as_dict()
    assert value["kind"] == "prepared_action"
    assert value["definition_version"] == 4
    assert value["action"]["name"] == "send_message"


def test_prepared_action_rejects_untrusted_shape():
    with pytest.raises(TaskContractError):
        PreparedAction(1, {"name": "send_message"}, "now").as_dict()
