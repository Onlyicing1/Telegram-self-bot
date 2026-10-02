"""Prompt / tool contract for the semantic intent boundary.

There is no deterministic intent parser any more: the AI is the only component
that decides what the owner means. That makes the PROMPT CONTRACT the primary
place where the immediate-workflow / durable-task distinction is expressed, so
these tests pin that contract and the two remaining authorization vocabularies.

They assert what the model is TOLD and what capabilities it is OFFERED. They do
not claim the model always complies — that is model behaviour, not a code
invariant — and they never assert that a local function recognises a phrase.

End-to-end proof that no local code selects a capability lives in
``tests/test_semantic_intent_boundary.py``.
"""
from __future__ import annotations

import pytest

from backend.ai.prompt.template import (
    OUTPUT_INSTRUCTIONS_TEMPLATE,
    RUNTIME_RULES_TEMPLATE,
    SYSTEM_RULES_TEMPLATE,
)
from backend.ai.proactive import (
    PROACTIVE_AUTHORIZED_RULES,
    has_proactive_authorization,
)
from backend.ai.tools.context import ToolContext
from backend.ai.tools.registry import create_default_registry


def _registry_context() -> ToolContext:
    """A minimal context: no Telegram, no network, no provider."""
    return ToolContext(telegram=None, owner_id=1, tz_str="UTC", extra={})

# ── Prompt contract: immediate workflow vs durable task ──────────────────

# ─────────────────────────────────────────────────────────────────────────────
# 2. Explicit single actions keep deterministic routing (the guardrail)
# ─────────────────────────────────────────────────────────────────────────────


# ─────────────────────────────────────────────────────────────────────────────
# 3. Immediate multi-action request must NOT be stored as a task
# ─────────────────────────────────────────────────────────────────────────────


def test_prompt_contract_separates_immediate_workflow_from_durable_task():
    """The prompt must define the two concepts and forbid the substitution."""
    system = SYSTEM_RULES_TEMPLATE
    assert "Immediate workflows vs durable tasks" in system
    assert "create_task" in system
    # The forbidden substitution must be stated in the system rules...
    assert "NEVER turn an immediate workflow into create_task" in system
    # ...and an immediate ordered workflow must be shown as consecutive calls.
    assert "consecutive real tool calls" in system
    # A durable task is the right answer only for real scheduling wording.
    assert "schedule" in system and "interval" in system and "cadence" in system


def test_output_instructions_forbid_storing_work_the_owner_wanted_now():
    output = OUTPUT_INSTRUCTIONS_TEMPLATE
    assert "one tool call per action" in output
    assert "stores work for later" in output
    assert "call no tool" in output  # explain-a-capability = no tool


def test_prompt_multi_step_todo_sentence_is_scoped_to_durable_todos():
    """The old "multi-step request" rule taught ordered-multi-part -> one stored
    object, which is the exact shape the model transferred to create_task."""
    assert "unrelated to an immediate workflow" in SYSTEM_RULES_TEMPLATE


def test_runtime_rules_do_not_gate_the_owners_own_sequence_on_authorization():
    runtime = RUNTIME_RULES_TEMPLATE
    assert "Running the owner's own ordered sequence needs NO proactive authorization" in runtime
    assert "never required for the actions the owner themselves requested" in runtime
    # The old wording implied multi-call planning was authorization-only.
    assert "One tool at a time. Max 5 tools per turn." not in runtime


# ─────────────────────────────────────────────────────────────────────────────
# 4. Durable / scheduled request — create_task IS correct here
# ─────────────────────────────────────────────────────────────────────────────


def test_durable_task_wording_is_what_selects_create_task():
    """The prompt contract is what separates this from an immediate request."""
    assert "cadence" in SYSTEM_RULES_TEMPLATE
    assert "create_task" in SYSTEM_RULES_TEMPLATE
    create_task = create_default_registry(_registry_context()).get("create_task")
    assert create_task is not None
    described = create_task.description.lower()
    assert "interval" in described and "cadence" in described
    assert "schedul" in described


# ─────────────────────────────────────────────────────────────────────────────
# 5. Proactive authorized immediate workflow
# ─────────────────────────────────────────────────────────────────────────────


def test_proactive_authorization_is_fail_closed_and_requires_explicit_phrase():
    assert has_proactive_authorization("اول سرچ کن، بعد نتیجه رو سیو کن، بعد تگ بزن.") is False
    assert has_proactive_authorization("هر کاری لازمه خودت انجام بده") is True
    assert has_proactive_authorization("") is False
    assert has_proactive_authorization(None) is False


def test_proactive_rules_bound_extra_work_and_do_not_enable_task_storage():
    assert "AUTHORIZED" in PROACTIVE_AUTHORIZED_RULES
    assert "at most 5 tool calls" in PROACTIVE_AUTHORIZED_RULES
    assert "never call create_task repeatedly" in PROACTIVE_AUTHORIZED_RULES


# ─────────────────────────────────────────────────────────────────────────────
# Boundary integrity: no second dispatcher / executor / routing surface
# ─────────────────────────────────────────────────────────────────────────────


def test_the_whole_tool_surface_still_terminates_at_one_registry():
    """create_task, web_search, save and the tag tool are all in the ONE
    registry, so the immediate workflow has every tool it needs available."""
    registry = create_default_registry(_registry_context())
    for name in ("create_task", "web_search", "save", "update_save_tags", "todo_add"):
        assert registry.get(name) is not None, name
