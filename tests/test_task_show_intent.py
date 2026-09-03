"""Focused root-cause tests: deterministic task-show intent resolution.

Production symptom: "لیست تسک رو ببین" fell through
``parse_command_intent`` as conversational and spent provider rounds on a
deterministic READ request, ending in retry/fallback/error. These tests pin
the failure class BEFORE the fix (repro) and the fixed behavior AFTER.
"""
from __future__ import annotations

import pytest

from backend.ai.actions import parse_command_intent


class TestTaskShowIntentResolution:
    """Persian/English task-show requests resolve to task_list deterministically."""

    @pytest.mark.parametrize(
        "text",
        [
            "لیست تسک رو ببین",
            "لیست تسک‌ها رو ببین",
            "تسک‌هام رو لیست کن",
            "لیست کارهام رو بده",
            "تسک‌های من رو نشون بده",
            "تسک‌هام چیا هستن؟",
            "وضعیت تسک‌هام چیه",
        ],
    )
    def test_persian_task_show_resolves_to_task_list(self, text):
        result = parse_command_intent(text, has_reply=False)
        assert result.kind == "executable", f"{text!r} -> {result.kind}"
        assert result.action == "task_list"
        assert result.tool_calls == [{"name": "task_list", "arguments": {}}]

    def test_english_task_show_resolves_to_task_list(self):
        result = parse_command_intent("show my tasks", has_reply=False)
        assert result.kind == "executable"
        assert result.action == "task_list"
        assert result.tool_calls == [{"name": "task_list", "arguments": {}}]

    def test_english_list_my_tasks_resolves_to_task_list(self):
        result = parse_command_intent("list my tasks", has_reply=False)
        assert result.kind == "executable"
        assert result.action == "task_list"


class TestTaskShowNegativeGuards:
    """Non-task requests must NOT be captured by the task vocabulary."""

    def test_plain_task_word_without_show_verb_stays_conversational(self):
        # "تسک" alone without a list/show verb must not execute anything.
        result = parse_command_intent("تسک", has_reply=False)
        assert result.kind == "conversational"

    def test_scheduling_intent_still_wins_over_task_words(self):
        # A recurring request stays on the create_task boundary even though
        # it contains task-ish words.
        result = parse_command_intent("هر 1 دقیقه یک بار بنویس سلام", has_reply=False)
        assert result.kind == "executable"
        assert result.action == "create_task"

    def test_save_listing_not_captured_by_task_branch(self):
        result = parse_command_intent("لیست سیوها رو بده", has_reply=False)
        assert result.kind == "executable"
        assert result.action == "list_saved_items"

    def test_bio_status_not_captured_by_task_branch(self):
        result = parse_command_intent("وضعیت بایو چیه", has_reply=False)
        assert result.kind == "executable"
        assert result.action == "get_bio"


class TestTaskInspectIntent:
    """A task id present in a show/inspect request resolves to task_inspect."""

    def test_persian_inspect_with_id(self):
        result = parse_command_intent("تسک ۳ رو نشون بده", has_reply=False)
        assert result.kind == "executable"
        assert result.action == "task_inspect"
        assert result.task_id == 3
        assert result.tool_calls == [{"name": "task_inspect", "arguments": {"task_id": 3}}]

    def test_english_inspect_with_id(self):
        result = parse_command_intent("show me task 12", has_reply=False)
        assert result.kind == "executable"
        assert result.action == "task_inspect"
        assert result.task_id == 12
        assert result.tool_calls == [{"name": "task_inspect", "arguments": {"task_id": 12}}]
