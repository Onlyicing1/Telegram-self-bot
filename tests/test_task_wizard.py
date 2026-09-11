"""Taskloom structured creation-wizard tests.

Contract under test:

- The wizard is a CREATION UX, not a second task system: structured choices
  are mapped to the SAME ``TaskCandidate`` the natural-language path produces
  and persisted through the SAME ``TaskCreationService`` -> ``TaskRepository``.
- Scheduling is REQUIRED; the wizard cannot produce a candidate without a
  valid schedule, and a once schedule must be in the future.
- Optional constraints (source, language, maximum length) are enforced by the
  EXISTING deterministic preparation policy: the review screen shows only
  constraints that ``derive_policy`` actually derives from the composed
  ``ai_instruction``.
- Generated content is never produced or stored at creation time; the
  occurrence path generates and validates fresh content per run.
- Named-source attribution is validated deterministically, and the owner may
  request a LABEL-FREE presentation: identity validation is unchanged while
  the visible bio drops the validated speaker label.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from backend.ai.database.task_repository import InMemoryTaskRepository
from backend.ai.preparation_policy import (
    PreparationPolicyError,
    derive_policy,
    strip_attribution_prefix,
    validate_content,
)
from backend.ai.task_candidate import TaskCandidate
from backend.ai.task_execution import present_calls
from backend.ai.task_wizard import TaskDraft, TaskWizardError

OWNER = 5150
OTHER = 41
NOW = datetime(2026, 9, 11, 12, 0, 0, tzinfo=timezone.utc)

REQUESTED = (
    "هر ۲ دقیقه بیو رو آپدیت کن به یه دیالوگ رندوم از آیانامی ری که زیر 60 کاراکتر باشه"
)
LABELED_LINE = "Ayanami Rei: Don't be afraid. You are not alone."


def _draft(**overrides) -> TaskDraft:
    base = dict(
        action="bio",
        content_mode="ai",
        schedule_type="interval",
        interval_minutes=2,
        timezone="Asia/Tehran",
        step="review",
    )
    base.update(overrides)
    return TaskDraft(**base)


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ── candidate construction ──────────────────────────────────────────────────


def test_bio_wizard_happy_path_produces_the_shared_candidate_contract():
    draft = _draft(
        source="Ayanami Rei", language="en", max_length=59, hide_speaker_label=True,
    )
    candidate = TaskCandidate.from_untrusted(task_wizard_candidate(draft))

    assert candidate.schedule_type == "interval"
    assert candidate.schedule == {"seconds": 120.0}
    assert candidate.timezone == "Asia/Tehran"
    assert candidate.label == "Update Bio"
    assert candidate.actions == [{"name": "bio_set_text", "arguments": {"text": ""}}]
    policy = derive_policy(candidate.ai_instruction)
    assert policy.source == "Ayanami Rei"
    assert policy.language == "english"
    assert policy.max_length == 59
    assert policy.speaker_label is False


def task_wizard_candidate(draft: TaskDraft) -> dict:
    from backend.ai import task_wizard
    return task_wizard.build_candidate(draft, chat_id=0, reference=NOW)


def test_schedule_is_required():
    from backend.ai import task_wizard

    draft = _draft(schedule_type="")
    assert task_wizard.missing_requirement(draft, reference=NOW) is not None
    with pytest.raises(TaskWizardError, match="schedule"):
        task_wizard.build_candidate(draft, reference=NOW)


def test_interval_parameter_is_required():
    from backend.ai import task_wizard

    draft = _draft(interval_minutes=None)
    with pytest.raises(TaskWizardError, match="interval"):
        task_wizard.build_candidate(draft, reference=NOW)


def test_once_schedule_must_be_in_the_future():
    from backend.ai import task_wizard

    future = _draft(schedule_type="once", once_at="2030-01-01 08:00")
    assert task_wizard.build_candidate(future, reference=NOW)["schedule"]["at"] == (
        "2030-01-01T08:00:00"
    )
    past = _draft(schedule_type="once", once_at="2020-01-01 08:00")
    with pytest.raises(TaskWizardError, match="future"):
        task_wizard.build_candidate(past, reference=NOW)


def test_daily_and_weekly_schedules_carry_the_task_timezone():
    from backend.ai import task_wizard

    daily = task_wizard.build_candidate(_draft(schedule_type="daily", clock="09:30"))
    assert daily["schedule"] == {"hour": 9, "minute": 30, "timezone": "Asia/Tehran"}
    weekly = task_wizard.build_candidate(
        _draft(schedule_type="weekly", clock="09:30", weekday=0)
    )
    assert weekly["schedule"] == {
        "weekday": 0, "hour": 9, "minute": 30, "timezone": "Asia/Tehran",
    }
    with pytest.raises(TaskWizardError, match="weekday"):
        task_wizard.build_candidate(_draft(schedule_type="weekly", clock="09:30"))


def test_invalid_timezone_is_rejected():
    from backend.ai import task_wizard

    with pytest.raises(TaskWizardError, match="IANA"):
        task_wizard.build_candidate(_draft(timezone="Mars/Olympus"))


# ── language contract ───────────────────────────────────────────────────────


def test_omitted_language_imposes_no_language_constraint():
    candidate = task_wizard_candidate(_draft(source="Ayanami Rei"))
    policy = derive_policy(candidate["ai_instruction"])
    assert policy.language is None
    assert policy.active  # the source constraint is still enforced


@pytest.mark.parametrize(
    "code,expected",
    [("en", "english"), ("fa", "persian"), ("ar", "arabic"), ("zh", "chinese")],
)
def test_selected_language_survives_into_the_durable_instruction(code, expected):
    candidate = task_wizard_candidate(_draft(language=code))
    policy = derive_policy(candidate["ai_instruction"])
    assert policy.language == expected


def test_english_language_is_enforced_deterministically():
    policy = derive_policy(task_wizard_candidate(_draft(language="en"))["ai_instruction"])
    assert policy.language == "english"
    assert validate_content("Don't be afraid.", policy) == "Don't be afraid."
    with pytest.raises(PreparationPolicyError):
        validate_content("نترس", policy)
    with pytest.raises(PreparationPolicyError):
        validate_content("恐れるな", policy)


def test_unrepresentable_language_choice_fails_closed():
    from backend.ai import task_wizard

    draft = _draft(language="en", source="English Rose")
    # The source itself names the language, so the round-trip still holds.
    assert task_wizard.instruction_problem(draft) is None
    broken = _draft(language="")
    # A source containing the language word would silently impose English.
    assert task_wizard.instruction_problem(broken.updated(source="English Rose")) is not None


# ── length / source fidelity ────────────────────────────────────────────────


def test_maximum_length_is_inclusive_and_survives():
    candidate = task_wizard_candidate(_draft(max_length=59))
    policy = derive_policy(candidate["ai_instruction"])
    assert policy.max_length == 59 and policy.exact_length is None
    assert validate_content("a" * 59, policy) == "a" * 59
    with pytest.raises(PreparationPolicyError):
        validate_content("a" * 60, policy)


def test_selected_source_survives_into_the_instruction():
    candidate = task_wizard_candidate(_draft(source="Ayanami Rei"))
    assert derive_policy(candidate["ai_instruction"]).source == "Ayanami Rei"


def test_source_input_is_bounded():
    from backend.ai import task_wizard

    with pytest.raises(TaskWizardError):
        task_wizard.clean_source("x" * (task_wizard.MAX_SOURCE_CHARS + 1))
    assert task_wizard.clean_source("  Ayanami   Rei ") == "Ayanami Rei"
    assert task_wizard.clean_source("none") == ""


# ── review accuracy / fresh content ─────────────────────────────────────────


def test_review_reflects_the_candidate_that_will_be_persisted():
    from backend.ai import task_wizard

    draft = _draft(source="Ayanami Rei", language="en", max_length=59, hide_speaker_label=True)
    rows = dict(task_wizard.review_lines(draft, reference=NOW))
    candidate = task_wizard.build_candidate(draft, 0, NOW)
    policy = derive_policy(candidate["ai_instruction"])

    assert rows["Action"] == "Update Bio"
    assert rows["Source"] == policy.source == "Ayanami Rei"
    assert rows["Language"] == "English" and policy.language == "english"
    assert rows["Maximum length"] == f"at most {policy.max_length} characters"
    assert policy.max_length == 59
    assert rows["Speaker label"] == "Hidden" and policy.speaker_label is False
    assert rows["Schedule"] == "Every 2 minutes"
    assert rows["Timezone"] == candidate["timezone"] == "Asia/Tehran"


def test_review_lists_nothing_that_is_not_in_the_candidate():
    from backend.ai import task_wizard

    rows = dict(task_wizard.review_lines(_draft(), reference=NOW))
    candidate = task_wizard.build_candidate(_draft(), 0, NOW)
    policy = derive_policy(candidate["ai_instruction"])
    assert rows["Source"] == "Any" and policy.source == ""
    assert rows["Language"] == "Any" and policy.language is None
    assert rows["Maximum length"] == "Any" and policy.max_length is None
    assert rows["Schedule"] == task_wizard.schedule_summary(
        candidate["schedule_type"], candidate["schedule"]
    )


def test_candidate_never_carries_generated_content():
    candidate = task_wizard_candidate(_draft(source="Ayanami Rei", language="en", max_length=59))
    assert candidate["actions"] == [{"name": "bio_set_text", "arguments": {"text": ""}}]
    assert LABELED_LINE not in str(candidate)


def test_static_content_is_kept_verbatim_without_an_instruction():
    candidate = task_wizard_candidate(
        _draft(action="message", content_mode="static", text="hello there")
    )
    assert candidate["actions"] == [{"name": "send_message", "arguments": {"text": "hello there"}}]
    assert "ai_instruction" not in candidate
    assert candidate["label"] == "hello there"


def test_message_action_cannot_be_ai_generated():
    from backend.ai import task_wizard

    with pytest.raises(TaskWizardError, match="static text"):
        task_wizard.build_candidate(_draft(action="message", content_mode="ai"))


# ── named-source presentation (identity validation vs visible format) ───────


def test_validator_never_rewrites_the_generated_line():
    policy = derive_policy(task_wizard_candidate(_draft(
        source="Ayanami Rei", hide_speaker_label=True,
    ))["ai_instruction"])
    assert policy.speaker_label is False
    assert validate_content(LABELED_LINE, policy) == LABELED_LINE
    with pytest.raises(PreparationPolicyError):
        validate_content("Ayumi: Every star begins as a dream!", policy)


def test_label_free_presentation_strips_only_the_verified_label():
    instruction = task_wizard_candidate(_draft(
        source="Ayanami Rei", hide_speaker_label=True,
    ))["ai_instruction"]
    calls = [{"name": "bio_set_text", "arguments": {"text": LABELED_LINE}}]
    presented = present_calls(calls, instruction)
    assert presented[0]["arguments"]["text"] == "Don't be afraid. You are not alone."
    assert presented[0]["name"] == "bio_set_text"
    # the validated calls are untouched (they are what gets persisted/audited)
    assert calls[0]["arguments"]["text"] == LABELED_LINE


def test_default_presentation_keeps_the_speaker_label():
    instruction = task_wizard_candidate(_draft(source="Ayanami Rei"))["ai_instruction"]
    calls = [{"name": "bio_set_text", "arguments": {"text": LABELED_LINE}}]
    assert present_calls(calls, instruction) is calls


def test_strip_attribution_prefix_refuses_unattributed_text():
    assert strip_attribution_prefix("Ayanami Rei: hello", "Ayanami Rei") == "hello"
    assert strip_attribution_prefix("hello", "Ayanami Rei") is None


def test_label_free_length_applies_to_the_visible_content():
    policy = derive_policy(task_wizard_candidate(_draft(
        source="Ayanami Rei", hide_speaker_label=True, max_length=20,
    ))["ai_instruction"])
    visible = "x" * 20
    assert validate_content(f"Ayanami Rei: {visible}", policy) == f"Ayanami Rei: {visible}"
    with pytest.raises(PreparationPolicyError):
        validate_content(f"Ayanami Rei: {'x' * 21}", policy)


# ── wizard UI wiring ────────────────────────────────────────────────────────


class _Event:
    pass


@pytest.fixture()
def repo():
    return InMemoryTaskRepository()


@pytest.fixture()
def wizard(repo, monkeypatch):
    import backend.ai.database.manager as manager
    import backend.bot.handlers.taskloom as handler

    class _Manager:
        task = repo

    monkeypatch.setattr(manager, "get_repository_manager", lambda: _Manager())
    from backend.helper import inline_engine
    inline_engine.set_owner_id(OWNER)
    handler._drafts.clear()
    handler.register(client=None, owner_id=OWNER, tz_str="Asia/Tehran")
    yield handler
    handler._drafts.clear()


def _buttons(rows) -> list[str]:
    data = []
    for row in rows:
        for button in row:
            raw = getattr(button, "data", button)
            data.append(raw.decode("utf-8") if isinstance(raw, bytes) else str(raw))
    return data


def test_wizard_is_registered_and_reachable_from_the_task_list(wizard, repo):
    from backend.helper.panel_registry import registry as get_registry

    assert get_registry().get_handler("taskloom_new") is not None
    _title, _body, rows = _run(wizard._taskloom_panel(_Event(), ""))
    assert "panel:taskloom_new" in _buttons(rows)


def test_wizard_walks_action_to_review_and_creates_through_the_shared_service(wizard, repo):
    _run(wizard._wizard_action(_Event(), "set:action:bio", 111))
    _run(wizard._wizard_action(_Event(), "set:mode:ai", 111))
    _run(wizard._wizard_action(_Event(), "set:lang:en", 111))
    _run(wizard._wizard_action(_Event(), "set:maxlen:59", 111))
    _run(wizard._wizard_input_handler("source")("Ayanami Rei", 111, 9, 0, 0))
    _run(wizard._wizard_input_handler("interval")("2", 111, 9, 0, 0))

    title, body, rows = _run(wizard._wizard_action(_Event(), "step:review", 111))
    assert title == "＋ New task"
    assert "**Action:** Update Bio" in body
    assert "action:taskloom_wizard:create" in _buttons(rows)

    title, body, rows = _run(wizard._wizard_action(_Event(), "create", 111))
    assert "✓ Task #" in body
    tasks = _run(repo.list_tasks(OWNER))
    assert len(tasks) == 1
    task = tasks[0]
    assert task.label == "Update Bio"
    assert task.schedule_type == "interval"
    assert task.schedule == {"seconds": 120.0}
    assert task.actions == [{"name": "bio_set_text", "arguments": {"text": ""}}]
    policy = derive_policy(task.ai_instruction)
    assert policy.source == "Ayanami Rei"
    assert policy.language == "english"
    assert policy.max_length == 59
    assert wizard._drafts == {}  # the draft is consumed by the creation


def test_wizard_creation_is_owner_scoped(wizard, repo):
    _run(wizard._wizard_action(_Event(), "set:action:message", 111))
    _run(wizard._wizard_input_handler("text")("mine", 111, 9, 0, 0))
    _run(wizard._wizard_input_handler("interval")("60", 111, 9, 0, 0))
    _run(wizard._wizard_action(_Event(), "create", 111))
    assert len(_run(repo.list_tasks(OWNER))) == 1
    assert _run(repo.list_tasks(OTHER)) == []


def test_wizard_refuses_to_create_without_a_schedule(wizard, repo):
    _run(wizard._wizard_action(_Event(), "set:action:bio", 111))
    _run(wizard._wizard_action(_Event(), "set:mode:ai", 111))
    title, body, rows = _run(wizard._wizard_action(_Event(), "create", 111))
    assert "×" in body
    assert "action:taskloom_wizard:create" not in _buttons(rows)
    assert _run(repo.list_tasks(OWNER)) == []


def test_wizard_schedule_step_blocks_review_until_complete(wizard, repo):
    _run(wizard._wizard_action(_Event(), "set:action:bio", 111))
    _run(wizard._wizard_action(_Event(), "set:mode:ai", 111))
    _run(wizard._wizard_action(_Event(), "step:schedule", 111))
    _title, body, rows = _run(wizard._wizard_action(_Event(), "step:review", 111))
    assert "Incomplete" in body and "schedule" in body
    assert "action:taskloom_wizard:create" not in _buttons(rows)
    _title, body, rows = _run(wizard._wizard_action(_Event(), "create", 111))
    assert "×" in body
    assert _run(repo.list_tasks(OWNER)) == []


def test_wizard_rejects_an_unrepresentable_source_without_corrupting_the_draft(wizard):
    _run(wizard._wizard_action(_Event(), "set:action:bio", 111))
    _run(wizard._wizard_action(_Event(), "set:mode:ai", 111))
    _run(wizard._wizard_input_handler("source")("Ayanami Rei", 111, 9, 0, 0))
    _run(wizard._wizard_input_handler("source")("x" * 200, 111, 9, 0, 0))
    assert wizard._draft().source == "Ayanami Rei"


def test_wizard_static_message_flow_stores_the_text_and_chat_destination(wizard, repo):
    _run(wizard._wizard_action(_Event(), "set:action:message", 111))
    _run(wizard._wizard_input_handler("text")("hello there", 111, 9, 0, 0))
    _run(wizard._wizard_input_handler("daily")("09:30", 111, 9, 0, 0))
    _run(wizard._wizard_action(_Event(), "create", 111))

    task = _run(repo.list_tasks(OWNER))[0]
    assert task.actions == [{"name": "send_message", "arguments": {"text": "hello there"}}]
    assert task.schedule == {"hour": 9, "minute": 30, "timezone": "Asia/Tehran"}
    assert task.notification_destination == {"chat_id": 111}
    assert task.ai_instruction is None


def test_wizard_adds_no_second_creation_path(wizard, monkeypatch, repo):
    """The wizard persists through TaskCreationService -> TaskRepository only."""
    import backend.ai.task_creation as creation

    calls = []
    original = creation.TaskCreationService.create

    async def _spy(self, candidate, reference):
        calls.append(candidate)
        return await original(self, candidate, reference)

    monkeypatch.setattr(creation.TaskCreationService, "create", _spy)
    _run(wizard._wizard_action(_Event(), "set:action:bio", 111))
    _run(wizard._wizard_action(_Event(), "set:mode:ai", 111))
    _run(wizard._wizard_input_handler("interval")("1", 111, 9, 0, 0))
    _run(wizard._wizard_action(_Event(), "create", 111))
    assert len(calls) == 1
    assert isinstance(calls[0], TaskCandidate)
