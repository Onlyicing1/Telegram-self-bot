"""Intent-routing boundary regression tests.

Proves the boundary that failed in production:

  1. conversational mention of a capability  -> NO executable intent
  2. explicit single action                  -> deterministic command kept
  3. explicit IMMEDIATE multi-action request -> must reach the model as
                                                several tool calls, never
                                                stored as a task
  4. durable / scheduled request             -> create_task is the right tool
  5. proactive authorized immediate workflow -> bounded extra work, no store

These tests exercise the DECISION BOUNDARY (what the local parser resolves,
and what the prompt contract tells the model the tool surface means). They
never inject a correct tool call and assert the executor runs it.
"""
from __future__ import annotations

import pytest

from backend.ai import actions
from backend.ai.actions import parse_command_intent
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

# ─────────────────────────────────────────────────────────────────────────────
# 1. Conversational mention of a capability must never become executable intent
# ─────────────────────────────────────────────────────────────────────────────

CONVERSATIONAL_MENTIONS_PERSIAN = [
    "سیو یعنی چی؟",
    "چرا سیستم سیو اینطوری کار می‌کنه؟",
    "درباره قابلیت سیو صحبت کنیم.",
    "فقط درباره این workflow توضیح بده، چیزی اجرا نکن.",
    "ذخیره کردن چطور کار می‌کنه؟",
    "تگ چیه؟",
]

CONVERSATIONAL_MENTIONS_ENGLISH = [
    "what does save mean?",
    "why does the save system work this way?",
    "let's talk about the save capability.",
    "what does the save tool do?",
    "explain how saving works",
    "how does this bot store things?",
    "what does delete mean?",
]


@pytest.mark.parametrize("text", CONVERSATIONAL_MENTIONS_PERSIAN)
def test_persian_capability_mention_is_conversational_without_reply(text):
    result = parse_command_intent(text, has_reply=False, reply_text="")
    assert result.kind == actions.KIND_CONVERSATIONAL
    assert not result.tool_calls


@pytest.mark.parametrize("text", CONVERSATIONAL_MENTIONS_ENGLISH)
def test_english_capability_mention_is_conversational_without_reply(text):
    result = parse_command_intent(text, has_reply=False, reply_text="")
    assert result.kind == actions.KIND_CONVERSATIONAL
    assert not result.tool_calls


@pytest.mark.parametrize("text", CONVERSATIONAL_MENTIONS_PERSIAN + CONVERSATIONAL_MENTIONS_ENGLISH)
def test_capability_mention_is_conversational_while_replying(text):
    """The regression: a REPLY is context, not a verb target.

    While replying to an unrelated message, the bare capability word used to
    satisfy the English verb gate, so meta-discussion became a real save and
    ran through the executor with no provider round at all.
    """
    result = parse_command_intent(text, has_reply=True, reply_text="some unrelated reply")
    assert result.kind == actions.KIND_CONVERSATIONAL
    assert not result.tool_calls


# ─────────────────────────────────────────────────────────────────────────────
# 2. Explicit single actions keep deterministic routing (the guardrail)
# ─────────────────────────────────────────────────────────────────────────────

EXPLICIT_SINGLE_ACTIONS = [
    ("این رو سیو کن.", ["save"]),
    ("این پیام رو سیو کن", ["save"]),
    ("اینو سیو کن", ["save"]),
    ("save this", ["save"]),
    ("save it", ["save"]),
    ("save this message", ["save"]),
    ("save the message", ["save"]),
    ("save this message with deep mode", ["save"]),
    ("delete this", ["delete_replied"]),
    ("delete this message", ["delete_replied"]),
]


@pytest.mark.parametrize("text,expected_tools", EXPLICIT_SINGLE_ACTIONS)
def test_explicit_single_action_stays_deterministic_while_replying(text, expected_tools):
    """Fix 1 must not cost us deterministic routing of real commands."""
    result = parse_command_intent(text, has_reply=True, reply_text="a message")
    assert result.kind == actions.KIND_EXECUTABLE
    assert [tc["name"] for tc in result.tool_calls] == expected_tools


def test_english_meta_frame_words_never_open_an_english_command():
    """A question/meta frame zeroes the English verb gate even with a target."""
    for frame in actions._EN_META_FRAME_WORDS:
        result = parse_command_intent(
            f"save this {frame} the system", has_reply=True, reply_text="a message",
        )
        assert result.kind == actions.KIND_CONVERSATIONAL, frame


def test_semantic_delete_and_event_frames_still_work():
    """The meta guard must not swallow the two frames that are real commands.

    "about" is a semantic-delete frame and "when" is an event frame; both keep
    their own pre-existing routing, and neither is treated as meta-talk.
    """
    semantic = parse_command_intent(
        "delete messages about the contract", has_reply=False,
    )
    assert semantic.kind == actions.KIND_EXECUTABLE
    assert [tc["name"] for tc in semantic.tool_calls] == ["delete"]
    assert "about" not in actions._EN_META_FRAME_WORDS
    assert "when" not in actions._EN_META_FRAME_WORDS


# ─────────────────────────────────────────────────────────────────────────────
# 3. Immediate multi-action request must NOT be stored as a task
# ─────────────────────────────────────────────────────────────────────────────

IMMEDIATE_MULTI_ACTION = [
    "این رو سیو کن و بعد تگش کن.",
    "اول سرچ کن، بعد نتیجه رو سیو کن، بعد تگ بزن.",
]


@pytest.mark.parametrize("text", IMMEDIATE_MULTI_ACTION)
def test_immediate_multi_action_is_not_routed_to_create_task(text):
    """The reported live bug: "Creating task..." for an immediate workflow.

    The local parser must never turn an owner's ordered action sequence into
    the durable create_task boundary. Sequencing words mark ORDER within this
    turn, never a schedule.
    """
    result = parse_command_intent(text, has_reply=True, reply_text="a message")
    tool_names = [tc["name"] for tc in (result.tool_calls or [])]
    assert "create_task" not in tool_names
    assert result.action != "create_task"


def test_immediate_workflow_tags_make_the_parser_yield_to_the_model():
    """A tag request is semantic, so the parser abstains and the model runs.

    This is the exact path the anime request took: not a deterministic command
    and not proactive — it reaches the provider, so the PROMPT contract (not
    this parser) is what must steer it to web_search -> save -> update_save_tags.
    """
    text = "اول سرچ کن، بعد نتیجه رو سیو کن، بعد تگ بزن."
    assert actions.save_metadata_requested(text) is True
    result = parse_command_intent(text, has_reply=True, reply_text="a message")
    assert result.kind == actions.KIND_CONVERSATIONAL
    assert not result.tool_calls
    assert has_proactive_authorization(text) is False


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


def test_durable_weekly_request_is_left_to_the_provider_not_invented_locally():
    """A weekly cadence request must not be handled by the local parser.

    ``_is_scheduling_intent`` fails closed here ("انجام بده" is not in the
    action-verb vocabulary), so the request reaches the provider instead of
    being resolved locally. That is the architecturally correct outcome: the
    local parser never fabricates a schedule, and the PROMPT contract (not this
    parser) is what points the model at create_task for cadence wording.
    """
    text = "هر هفته این کار رو انجام بده."
    assert actions._is_scheduling_intent(actions._tokenize(text)) is False
    result = parse_command_intent(text, has_reply=True, reply_text="a message")
    assert result.kind == actions.KIND_CONVERSATIONAL
    assert not result.tool_calls


def test_durable_task_wording_is_what_selects_create_task():
    """The prompt contract is what separates this from an immediate request."""
    assert "cadence" in SYSTEM_RULES_TEMPLATE
    assert "create_task" in SYSTEM_RULES_TEMPLATE
    create_task = create_default_registry(_registry_context()).get("create_task")
    assert create_task is not None
    described = create_task.description.lower()
    assert "interval" in described and "cadence" in described
    assert "schedul" in described


def test_immediate_request_is_not_a_scheduling_intent():
    """The mirror image: the same vocabulary without cadence is immediate."""
    for text in IMMEDIATE_MULTI_ACTION:
        assert actions._is_scheduling_intent(actions._tokenize(text)) is False


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
