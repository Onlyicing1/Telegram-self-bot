"""AI-driven task tool selection — deterministic task vocabulary removed.

Production repro: "لیست تسک رو ببین" previously reached a retry/fallback
error because the provider returned prose. A deterministic task-show branch
was then added to ``parse_command_intent`` (commit 7aee8d1). That vocabulary
approach is deliberately REPLACED: per-phrase Persian/English task routing
cannot generalize ("completed tasks", "task 3", "pause it"), so selecting the
registered task tools (task_list / task_inspect / task_transition) is now the
AI's semantic job. The AI learns the tools from provider-visible schemas and
the prompt's JSON-action contract; the Self Bot stays the execution authority
by validating every AI output locally (validate_action → resolve_tool_calls →
the SAME ToolExecutor used by native tool calls).

These tests pin the replacement contract on both sides:
1. Task-management requests are NO LONGER captured by the deterministic
   parser — they fall through as conversational and reach the AI path.
2. The AI-selection outputs still resolve end-to-end: JSON action objects for
   task_list / task_inspect / task_transition validate and map to the
   registered tools; unknown/invalid outputs are rejected locally.
3. Extended retrieval semantics: task_list accepts an optional validated
   ``status`` filter so natural-language reads ("completed tasks") are
   expressible as a tool argument instead of new vocabulary.
"""
from __future__ import annotations

import pytest

from backend.ai.actions import parse_action_text, parse_command_intent


class TestTaskRequestsRouteToAISelection:
    """Task-management sentences must reach the AI path, not local vocabulary.

    These are the exact phrasings the removed deterministic branch used to
    resolve. They now return conversational from ``parse_command_intent`` so
    the provider sees the request and semantically selects a task tool.
    """

    @pytest.mark.parametrize(
        "text",
        [
            # The original production repro sentence.
            "لیست تسک رو ببین",
            # Persian list/show forms (ZWNJ-clitic and possessive variants).
            "لیست تسک‌ها رو ببین",
            "تسک‌هام رو لیست کن",
            "لیست کارهام رو بده",
            "تسک‌های من رو نشون بده",
            "تسک‌هام چیا هستن؟",
            "وضعیت تسک‌هام چیه",
            # English forms.
            "show my tasks",
            "list my tasks",
            # Inspect-by-id forms (the id is resolved by the model into the
            # task_inspect argument, never by a local token scan).
            "تسک ۳ رو نشون بده",
            "show me task 12",
        ],
    )
    def test_task_request_is_not_captured_by_deterministic_parser(self, text):
        result = parse_command_intent(text, has_reply=False)
        assert result.kind == "conversational", (
            f"{text!r} resolved deterministically as {result.action!r} — "
            "task tool selection must happen through the AI path"
        )


class TestTaskJSONSelectionContract:
    """AI-selection output (JSON action objects) still executes locally."""

    def test_task_list_action_resolves_to_registered_tool(self):
        result = parse_action_text('{"action":"task_list"}')
        assert result.kind == "executable"
        assert result.action == "task_list"
        assert result.tool_calls == [{"name": "task_list", "arguments": {}}]

    def test_task_inspect_action_resolves_with_task_id(self):
        result = parse_action_text('{"action":"task_inspect","task_id":3}')
        assert result.kind == "executable"
        assert result.tool_calls == [
            {"name": "task_inspect", "arguments": {"task_id": 3}}
        ]

    def test_task_transition_action_resolves_with_cas_arguments(self):
        result = parse_action_text(
            '{"action":"task_transition","task_id":7,'
            '"action_status":"paused","expected_version":2}'
        )
        assert result.kind == "executable"
        assert result.tool_calls == [{
            "name": "task_transition",
            "arguments": {"task_id": 7, "action": "paused", "expected_version": 2},
        }]

    def test_unknown_task_action_is_rejected_locally(self):
        result = parse_action_text('{"action":"task_banish"}')
        assert result.kind == "invalid"
        assert "Unknown action" in result.error


class TestTaskListStatusFilter:
    """task_list's optional status filter (extended retrieval semantics)."""

    def test_status_filter_resolves_to_task_list_argument(self):
        result = parse_action_text('{"action":"task_list","status":"completed"}')
        assert result.kind == "executable"
        assert result.tool_calls == [
            {"name": "task_list", "arguments": {"status": "completed"}}
        ]

    def test_status_filter_is_normalized_case_insensitively(self):
        result = parse_action_text('{"action":"task_list","status":"Paused"}')
        assert result.kind == "executable"
        assert result.tool_calls[0]["arguments"]["status"] == "paused"

    @pytest.mark.parametrize("status", ["failed", "deleted", "expired", "all", "42"])
    def test_status_filter_rejects_outside_lifecycle_vocabulary(self, status):
        result = parse_action_text(f'{{"action":"task_list","status":"{status}"}}')
        assert result.kind == "invalid"
        assert "status" in result.error

    def test_status_field_is_rejected_for_non_task_list_actions(self):
        result = parse_action_text('{"action":"list_saved_items","status":"completed"}')
        assert result.kind == "invalid"
        assert "task_list" in result.error


class TestNonTaskDeterministicGuards:
    """Non-task intents keep their deterministic resolution unchanged."""

    def test_scheduling_intent_still_resolves_to_create_task(self):
        result = parse_command_intent("هر 1 دقیقه یک بار بنویس سلام", has_reply=False)
        assert result.kind == "executable"
        assert result.action == "create_task"

    def test_saved_items_listing_not_affected_by_task_removal(self):
        result = parse_command_intent("لیست سیوها رو بده", has_reply=False)
        assert result.kind == "executable"
        assert result.action == "list_saved_items"

    def test_bio_status_not_affected_by_task_removal(self):
        result = parse_command_intent("وضعیت بایو چیه", has_reply=False)
        assert result.kind == "executable"
        assert result.action == "get_bio"
