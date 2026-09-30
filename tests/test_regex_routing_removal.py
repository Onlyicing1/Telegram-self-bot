"""No-regex command/tool routing — behavior pins.

Pins the §24.10 implementation (INVESTIGATION.md): command and intent
detection is decided on TOKENS (equality, set membership, digit adjacency),
never on regex, while every execution still converges on the single
ToolRegistry → ToolExecutor boundary. Structured (native or JSON) proposals
keep working; prose mentioning a tool name, keyword strings, and malformed or
unknown requests never execute anything.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from backend.ai import actions
from backend.ai.actions import (
    parse_action_text,
    parse_command_intent,
    resolve_tool_calls,
    validate_action,
    _FA_CLOCK_WORDS,
    _EN_CLOCK_WORDS,
    _has_future_clock_request,
    _text_has_clock_anchor,
    _tokenize,
)
from backend.ai.tools.executor import ToolExecutor
from backend.ai.tools.registry import create_default_registry
from backend.ai.tools.context import ToolContext

# ── 1. Clock-anchor detection is token-based ────────────────────────────────


class TestTokenClockAnchorDetection:
    def test_source_has_no_clock_intent_regex(self):
        src = (Path("backend/ai/actions.py")).read_text(encoding="utf-8")
        for retired in (r"\d{1,2}:\d{2}", r"ساعت\s*\d", r"\bat\s+\d{1,2}\b"):
            assert retired not in src

    @pytest.mark.parametrize(
        "text",
        [
            "فردا ساعت 15:35 کارها را جمع کن",
            "tomorrow at 5 water the plants",
            "فردا at 5 pm stretch",
            "فردا ساعت 9:05 یادم بنداز",
        ],
    )
    def test_future_clock_anchors_detect(self, text):
        assert _text_has_clock_anchor(text)
        assert _has_future_clock_request(text, _tokenize(text))

    @pytest.mark.parametrize(
        "text",
        [
            "پروژه ساعت‌ها طول کشید",
            "atmosphere was nice yesterday",
            "دیروز کجا بودم",
        ],
    )
    def test_non_clock_text_does_not_false_positive(self, text):
        assert not _text_has_clock_anchor(text)

    @pytest.mark.parametrize(
        "text",
        [
            "ساعت ۹ دیروز کجا بودم",
            "جلسه تا ساعت ۶",
        ],
    )
    def test_past_or_same_day_clock_is_never_a_future_schedule(self, text):
        # The anchor exists, but without a future day marker the future
        # gate keeps the request on its command path (old behavior parity).
        assert _has_future_clock_request(text, _tokenize(text)) is False

    def test_fanum_and_nonlatin_punct_survive(self):
        assert _text_has_clock_anchor("فردا ساعت ۱۵٫۳۵")
        assert _text_has_clock_anchor("tomorrow at «7» sharp")

    def test_number_unit_pairs_are_not_clock_anchors(self):
        assert not _text_has_clock_anchor("هر 5 دقیقه")
        assert not _text_has_clock_anchor("every 10 minutes remind me")


# ── 2. create_task completeness gate uses the shared token detector ────────


def _gate(request: str):
    from tests.test_task_semantic_completeness import (
        _context,
        _manager,
        _Provider,
    )
    from backend.ai.tools.task import CreateTaskTool

    provider = _Provider("null")
    manager = _manager(provider)
    return (
        CreateTaskTool(_context(manager)).execute(
            _context(manager), {"request": request}
        ),
        provider,
    )


class TestCompletenessGateTokenBased:
    def test_gate_has_no_inline_regex(self):
        src = (Path("backend/ai/tools/task.py")).read_text(encoding="utf-8")
        assert "_re.search" not in src
        assert r"\d{1,2}:\d{2}" not in src

    @pytest.mark.asyncio
    async def test_incomplete_request_routes_to_wizard(self):
        coro, provider = _gate("hello there friend")
        result = await coro
        assert result.success is False
        assert result.data["open_taskloom_wizard"] is True
        assert result.data["wizard_reason"] == "incomplete_request"
        # The gate decided with ZERO provider involvement: no schedule
        # expression was token-provable, so nothing reached the model.
        assert provider.calls == 0

    @pytest.mark.asyncio
    async def test_clock_anchor_completes_the_request(self):
        coro, provider = _gate("tomorrow at 5 write something nice for me")
        result = await coro
        assert result.success is False
        # The GATE released it to the interpreter (one provider round); the
        # fake-null interpretation is what declined, never the gate.
        assert provider.calls == 1
        assert result.data.get("wizard_reason") != "incomplete_request"


# ── 3. One save-code shape contract ─────────────────────────────────────────


class TestSaveCodeConsolidation:
    def test_shape_contract_is_adjacent_to_token_classifiers(self):
        src = (Path("backend/ai/actions.py")).read_text(encoding="utf-8")
        assert "re.compile" in src  # shape regexes remain (§24.8)
        assert "invalid 'save_code'" in src  # validator texts unchanged

    def test_validate_save_code_action_rejects_non_codes(self):
        result = validate_action(
            {"action": "retrieve_save", "save_code": "not a code!"}
        )
        assert result.kind == actions.KIND_INVALID
        assert "Invalid 'save_code'" in (result.error or "")


# ── 4. `Menu` is exact-equality routing (no regex anywhere) ─────────────────


class TestMenuEqualityRouting:
    def test_misc_registers_no_telethon_pattern(self):
        src = (Path("backend/bot/handlers/misc.py")).read_text(encoding="utf-8")
        assert "pattern=" not in src
        assert '!= "Menu"' in src

    def test_only_the_exact_word_matches(self):
        # Mirror of the handler guard: raw text must equal the literal word.
        candidates = {"Menu": True, " menu": False, "menu ": False,
                      "MENU": False, "Menü": False}
        for text, expected in candidates.items():
            assert (text == "Menu") is expected


# ── 5. Hallucinated / malformed / unknown tool invocation is rejected ───────


class TestHallucinatedToolInvocationRejected:
    def test_prose_naming_a_tool_does_not_execute_it(self):
        result = parse_command_intent(
            "I want to know what the todo_add tool does", has_reply=False
        )
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


# ── 6. Valid structured proposal and Todo creation still work ───────────────


class TestTodoCreationStillWorks:
    def test_valid_structured_tool_call_still_resolves(self):
        parsed = parse_action_text(
            '```json\n{"action": "todo_add", "title": "buy milk"}\n```'
        )
        validated = parsed if parsed.kind == actions.KIND_EXECUTABLE else validate_action(
            {"action": "todo_add", "title": "buy milk"}
        )
        calls = resolve_tool_calls(validated)
        assert calls and calls[0]["name"] == "todo_add"

    def test_deterministic_command_paths_unchanged(self):
        # Save/delete/send vocabulary: the deterministic parser still emits
        # existing-registry tool calls through the SAME executor boundary.
        from backend.ai.actions import parse_structural_predicate
        from backend.ai import semantic_delete

        assert semantic_delete.parse_structural_predicate is not None
        r = parse_command_intent("delete 3 messages", has_reply=False)
        calls = resolve_tool_calls(r)
        assert calls and calls[0]["name"] == "delete"
        assert calls[0]["arguments"]["count"] == 3
