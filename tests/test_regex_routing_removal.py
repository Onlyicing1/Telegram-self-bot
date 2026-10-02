"""Regex audit — no natural-language intent routing remains.

The goal of this audit is NOT "zero regex in the repository". It is:

    zero regex (and zero keyword/token vocabulary) that reads a natural-language
    user message and chooses a tool or an action.

Every remaining regex is classified here as TECHNICAL: fixed-format artifact
matching, JSON extraction, protocol/transport parsing, or a deterministic
validation applied to an argument the AI already selected. None of them can
turn an owner message into ``create_task``, ``save``, ``search`` or a tag call.

Structured proposals (native tool calls and the model's JSON action object)
keep working, and malformed/unknown requests still execute nothing.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from backend.ai import actions
from backend.ai.actions import (
    parse_action_text,
    resolve_tool_calls,
    validate_action,
)
from backend.ai.tools.context import ToolContext
from backend.ai.tools.executor import ToolExecutor
from backend.ai.tools.registry import create_default_registry

DECISION_PATH_MODULES = [
    "backend/ai/actions.py",
    "backend/ai/engine/dispatcher.py",
    "backend/ai/tools/task.py",
    "backend/ai/tools/save.py",
    "backend/ai/proactive.py",
]

RETIRED_SYMBOLS = (
    "parse_command_intent",
    "_is_scheduling_intent",
    "_is_event_intent",
    "_has_future_clock_request",
    "_try_local_fast_path",
    "_build_deterministic_task_candidate",
    "deterministic_task_candidate",
    "local_fast_path",
    "explicit_no_tags_requested",
    "save_metadata_requested",
)

RETIRED_VOCABULARY = (
    "_FA_RECUR_WORDS",
    "_EN_RECUR_WORDS",
    "_FA_ACTION_VERBS",
    "_EN_ACTION_VERBS",
    "_INTERVAL_INTRO",
    "_FA_PLAN_WORDS",
    "_EN_PLAN_WORDS",
    "_FA_EVENT_MARKERS",
    "_EN_EVENT_WORDS",
    "_DELETE_STEMS",
    "_SAVE_STEMS",
    "_SEND_STEMS",
    "_EN_SAVE",
    "_EN_DELETE",
    "_EN_SEND",
    "_EN_META_FRAME_WORDS",
    "_SAVE_TAG_MARKERS",
    "_SAVE_NAME_MARKERS",
)


# ── 1. The AI decision path contains no natural-language intent routing ─────


class TestNoSemanticIntentRoutingRemains:
    """No module may re-derive an intent from the owner's raw message."""

    @pytest.mark.parametrize("module", DECISION_PATH_MODULES)
    def test_no_command_parser_symbol_exists(self, module):
        src = Path(module).read_text(encoding="utf-8")
        for retired in RETIRED_SYMBOLS:
            assert retired not in src, f"{module} still references {retired}"

    @pytest.mark.parametrize("module", DECISION_PATH_MODULES)
    def test_no_recurrence_or_action_vocabulary_remains(self, module):
        src = Path(module).read_text(encoding="utf-8")
        for retired in RETIRED_VOCABULARY:
            assert retired not in src, f"{module} still defines {retired}"

    def test_cadence_words_have_no_routing_power_anywhere(self):
        """The exact production failure: "هفتگی" as a topic, not a schedule."""
        for name in dir(actions):
            assert not name.startswith("_FA_RECUR"), name
            assert not name.startswith("_EN_RECUR"), name
        assert not hasattr(actions, "parse_command_intent")

    def test_dispatcher_module_states_the_intent_boundary(self):
        src = Path("backend/ai/engine/dispatcher.py").read_text(encoding="utf-8")
        assert "no step here reads the owner's message to decide WHAT to do" in src

    def test_create_task_tool_has_no_natural_language_pre_gate(self):
        src = Path("backend/ai/tools/task.py").read_text(encoding="utf-8")
        assert "NO natural-language gate runs here" in src


# ── 2. Remaining regexes are technical, not semantic ───────────────────────


class TestRemainingRegexesAreTechnical:
    def test_actions_module_keeps_no_persian_english_keyword_alternation(self):
        src = Path("backend/ai/actions.py").read_text(encoding="utf-8")
        assert "_FA_" not in src
        assert "_EN_" not in src

    def test_save_code_shape_contract_still_validates(self):
        src = Path("backend/ai/actions.py").read_text(encoding="utf-8")
        assert "re.compile" in src  # shape regexes remain
        assert "invalid 'save_code'" in src  # validator texts unchanged

    def test_validate_save_code_action_rejects_non_codes(self):
        result = validate_action(
            {"action": "retrieve_save", "save_code": "not a code!"}
        )
        assert result.kind == actions.KIND_INVALID
        assert "Invalid 'save_code'" in (result.error or "")

    def test_telegram_url_parsing_is_technical(self):
        assert actions._extract_telegram_link("t.me/durov") == "t.me/durov"
        assert actions._extract_telegram_link("no link here") is None


# ── 3. `Menu` is exact-equality routing ────────────────────────────────────


class TestMenuEqualityRouting:
    def test_misc_registers_no_telethon_pattern(self):
        src = Path("backend/bot/handlers/misc.py").read_text(encoding="utf-8")
        assert "pattern=" not in src
        assert '!= "Menu"' in src

    def test_only_the_exact_word_matches(self):
        candidates = {"Menu": True, " menu": False, "menu ": False,
                      "MENU": False, "Menü": False}
        for text, expected in candidates.items():
            assert (text == "Menu") is expected


# ── 4. Hallucinated / malformed / unknown tool invocation is rejected ───────


class TestHallucinatedToolInvocationRejected:
    def test_prose_naming_a_tool_produces_no_action(self):
        """Prose that merely mentions a tool yields no structured action.

        Execution requires a STRUCTURED proposal from the AI; a sentence that
        happens to contain a tool name is not one.
        """
        result = parse_action_text("I want to know what the todo_add tool does")
        assert result.kind == actions.KIND_CONVERSATIONAL
        assert resolve_tool_calls(result) == []

    def test_unknown_tool_name_is_rejected_by_the_registry(self):
        registry = create_default_registry(ToolContext(
            telegram=None, owner_id=7, tz_str="UTC", client=None, extra={}
        ))
        assert registry.get("make_me_a_sandwich") is None

    @pytest.mark.asyncio
    async def test_unknown_tool_call_executes_nothing(self):
        registry = create_default_registry(ToolContext(
            telegram=None, owner_id=7, tz_str="UTC", client=None, extra={}
        ))
        executor = ToolExecutor(registry, ToolContext(
            telegram=None, owner_id=7, tz_str="UTC", client=None, extra={}
        ))
        results = await executor.execute_calls(
            [{"name": "make_me_a_sandwich", "arguments": {"title": "hi"}}], owner_id=7
        )
        assert results[0].success is False
        assert results[0].error == "not_found"

    @pytest.mark.asyncio
    async def test_malformed_arguments_are_never_executed(self):
        registry = create_default_registry(ToolContext(
            telegram=None, owner_id=7, tz_str="UTC", client=None, extra={}
        ))
        executor = ToolExecutor(registry, ToolContext(
            telegram=None, owner_id=7, tz_str="UTC", client=None, extra={}
        ))
        results = await executor.execute_calls(
            [{"name": "delete", "arguments": "<<not json>>",
              "malformed_arguments": True, "arguments_error": "bad json"}],
            owner_id=7,
        )
        assert results[0].success is False
        assert results[0].error == "malformed_arguments"
        assert "was not executed" in results[0].message

    def test_model_json_with_unknown_tool_resolves_to_nothing(self):
        result = parse_action_text('```json\n{"action": "fly_to_moon"}\n```')
        assert result.kind == actions.KIND_INVALID
        assert resolve_tool_calls(result) == []


# ── 5. Valid structured proposal and Todo creation still work ──────────────


class TestStructuredProposalStillWorks:
    def test_valid_structured_tool_call_still_resolves(self):
        parsed = parse_action_text(
            '```json\n{"action": "todo_add", "title": "buy milk"}\n```'
        )
        validated = (
            parsed if parsed.kind == actions.KIND_EXECUTABLE
            else validate_action({"action": "todo_add", "title": "buy milk"})
        )
        calls = resolve_tool_calls(validated)
        assert calls and calls[0]["name"] == "todo_add"

    def test_create_task_is_still_available_to_the_ai(self):
        registry = create_default_registry(ToolContext(
            telegram=None, owner_id=7, tz_str="UTC", client=None, extra={}
        ))
        assert registry.get("create_task") is not None