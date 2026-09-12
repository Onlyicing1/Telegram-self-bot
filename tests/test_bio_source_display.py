"""Bio source DISPLAY contract — attribution stays, rendering is opt-in.

The change under test separates two concepts that used to be one:

* source IDENTITY — a semantic generation constraint, validated
  deterministically (the generated line must open with the requested source);
* source DISPLAY — whether that validated attribution is RENDERED.

Display defaults to OFF: naming a source never implies its name appears. It is
turned on only by an explicit request ("اسمش هم اولش باشه", "منبع رو نمایش
بده", "show the source name") and can be turned off explicitly ("اسمش رو
ننویس", "don't show the source"). The deterministic ``derive_policy`` is the
authority — the provider can never turn display on merely because a source
exists.

Source-fidelity, language and length validation are unchanged; only the
presentation of an already-validated attribution changes.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from backend.ai.database.task_repository import InMemoryTaskRepository
from backend.ai.preparation_policy import (
    PreparationPolicyError,
    derive_policy,
    strip_attribution_prefix,
    validate_content,
)
from backend.ai.task_execution import TaskExecutionCoordinator, present_calls
from backend.ai.task_wizard import TaskDraft
from backend.ai.tools.base import PermissionLevel, ToolResult
from backend.ai.tools.context import ToolContext
from backend.ai.tools.executor import ToolExecutor
from backend.ai.tools.registry import ToolRegistry, create_default_registry

# ── Fixtures with MATCHING source / line scripts ─────────────────────────────
# (a Persian instruction must be paired with a Persian source line, otherwise
# the deterministic attribution check — correctly — refuses to strip it)
EN_SOURCE = "Ayanami Rei"
EN_LINE = "Don't be afraid. You are not alone."
EN_ATTRIBUTED = f"{EN_SOURCE}: {EN_LINE}"

FA_SOURCE = "آیانامی ری"
FA_LINE = "د" * 40
FA_ATTRIBUTED = f"{FA_SOURCE}: {FA_LINE}"

# Verbatim natural-language requests (what a task persists as ai_instruction).
EN_SOURCE_ONLY = (
    "every 2 minutes change my bio to a random dialogue from Ayanami Rei "
    "below 60 characters"
)
EN_SOURCE_SHOWN = EN_SOURCE_ONLY + ", show the source name"
FA_SOURCE_ONLY = (
    "هر 5 دقیقه تکست بیو من رو به یه دیالوگ رندوم از آیانامی ری تغییر بده "
    "باید زیر 60 کاراکتر باشه"
)
FA_SOURCE_SHOWN = FA_SOURCE_ONLY + " و اسمش هم اولش باشه"

OWNER = 5150
NOW = datetime(2026, 9, 12, 12, 0, 0, tzinfo=timezone.utc)


# ── deterministic policy: the default and the explicit opt-in ────────────────


@pytest.mark.parametrize(
    "instruction,source",
    [
        (EN_SOURCE_ONLY, EN_SOURCE),
        (FA_SOURCE_ONLY, FA_SOURCE),
    ],
)
def test_source_alone_never_enables_rendering(instruction, source):
    policy = derive_policy(instruction)
    assert policy.source == source  # the constraint IS derived
    assert policy.show_source is False  # ... but is never rendered


@pytest.mark.parametrize(
    "instruction,source",
    [
        (EN_SOURCE_SHOWN, EN_SOURCE),
        (FA_SOURCE_SHOWN, FA_SOURCE),
        (
            "هر 5 دقیقه بیو رو با یه دیالوگ از آیانامی ری عوض کن و منبع رو نمایش بده",
            FA_SOURCE,
        ),
        (
            "change my bio to a dialogue from Ayanami Rei with the source name",
            EN_SOURCE,
        ),
    ],
)
def test_explicit_show_language_enables_rendering(instruction, source):
    policy = derive_policy(instruction)
    assert policy.source == source
    assert policy.show_source is True


@pytest.mark.parametrize(
    "instruction",
    [
        FA_SOURCE_ONLY + "، اسمش رو ننویس",
        "هر 5 دقیقه بیو رو با یه دیالوگ از آیانامی ری عوض کن، اسم شخصیت رو توی بیو ننویس",
        EN_SOURCE_ONLY + ", don't show the source",
        EN_SOURCE_ONLY + " without the source name",
    ],
)
def test_explicit_hide_language_keeps_rendering_off(instruction):
    assert derive_policy(instruction).show_source is False


def test_hide_language_overrides_an_incidental_show_phrase():
    # Contains a show marker ("اسمش هم اول") AND a hide marker ("ننویس"): hide wins.
    policy = derive_policy(FA_SOURCE_ONLY + " و اسمش هم اولش باشه ولی اسمش رو ننویس")
    assert policy.show_source is False


def test_show_marker_without_a_source_is_inert():
    # Nothing to render: the flag only ever applies to a named source.
    policy = derive_policy("هر 5 دقیقه تسک هام رو منبع رو نمایش بده")
    assert policy.source == "" and policy.show_source is False


# ── source fidelity, language and length are unchanged ──────────────────────


def test_identity_validation_is_unchanged_by_the_display_default():
    policy = derive_policy(EN_SOURCE_ONLY)
    assert policy.show_source is False
    # The generated line must STILL self-attribute to the requested source.
    assert validate_content(EN_ATTRIBUTED, policy) == EN_ATTRIBUTED
    with pytest.raises(PreparationPolicyError):
        validate_content("Ayumi: Every star begins as a dream!", policy)
    with pytest.raises(PreparationPolicyError):
        validate_content(EN_LINE, policy)  # unattributed generic text


def test_explicit_show_is_validated_on_the_rendered_text():
    policy = derive_policy(EN_SOURCE_SHOWN)
    assert policy.show_source is True
    assert validate_content(EN_ATTRIBUTED, policy) == EN_ATTRIBUTED
    with pytest.raises(PreparationPolicyError):
        validate_content(EN_LINE, policy)


def test_length_applies_to_the_visible_text_when_the_label_is_hidden():
    instruction = (
        "every 2 minutes change my bio to a random dialogue from Ayanami Rei "
        "below 21 characters"
    )
    policy = derive_policy(instruction)
    assert policy.show_source is False and policy.max_length == 20
    visible = "x" * 20
    assert validate_content(f"{EN_SOURCE}: {visible}", policy) == f"{EN_SOURCE}: {visible}"
    with pytest.raises(PreparationPolicyError):
        validate_content(f"{EN_SOURCE}: {'x' * 21}", policy)

    # With the source rendered, the bound covers the rendered text itself.
    shown = derive_policy(instruction + ", show the source name")
    assert shown.show_source is True
    with pytest.raises(PreparationPolicyError):
        validate_content(f"{EN_SOURCE}: {visible}", shown)


def test_language_still_applies_to_the_visible_text():
    instruction = "هر 5 دقیقه بیو رو با یه دیالوگ فارسی از آیانامی ری عوض کن زیر 60 کاراکتر"
    policy = derive_policy(instruction)
    assert policy.source == FA_SOURCE and policy.max_length == 59
    assert policy.language == "persian" and policy.show_source is False
    assert validate_content(FA_ATTRIBUTED, policy) == FA_ATTRIBUTED
    # A Latin-only visible line violates the Persian requirement even though
    # the (hidden) attribution itself is Latin.
    with pytest.raises(PreparationPolicyError):
        validate_content(f"{FA_SOURCE}: hello there", policy)


# ── presentation step at the execution boundary ─────────────────────────────


def test_default_display_strips_only_the_verified_attribution():
    calls = [{"name": "bio_set_text", "arguments": {"text": EN_ATTRIBUTED}}]
    presented = present_calls(calls, EN_SOURCE_ONLY)
    assert presented[0]["arguments"]["text"] == EN_LINE
    assert calls[0]["arguments"]["text"] == EN_ATTRIBUTED  # audit copy untouched


def test_persian_source_default_display_strips_too():
    calls = [{"name": "bio_set_text", "arguments": {"text": FA_ATTRIBUTED}}]
    assert present_calls(calls, FA_SOURCE_ONLY)[0]["arguments"]["text"] == FA_LINE


def test_explicit_show_keeps_the_attribution_in_the_visible_content():
    for instruction in (EN_SOURCE_SHOWN, FA_SOURCE_SHOWN):
        calls = [{"name": "bio_set_text", "arguments": {"text": EN_ATTRIBUTED}}]
        assert present_calls(calls, instruction) is calls


def test_tasks_without_a_source_are_untouched():
    calls = [{"name": "send_message", "arguments": {"text": "hello"}}]
    assert present_calls(calls, "every 5 minutes send hello") is calls


def test_presentation_never_invents_content_for_an_unattributed_line():
    calls = [{"name": "bio_set_text", "arguments": {"text": EN_LINE}}]
    # The line cannot be stripped (no verified attribution) — it is passed
    # through unchanged; validation upstream would already have rejected it.
    assert present_calls(calls, EN_SOURCE_ONLY)[0]["arguments"]["text"] == EN_LINE
    assert strip_attribution_prefix(EN_LINE, EN_SOURCE) is None


# ── Taskloom: the Bio "Show source?" option ─────────────────────────────────


class _Event:
    pass


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _buttons(rows) -> list[str]:
    data = []
    for row in rows:
        for button in row:
            raw = getattr(button, "data", button)
            data.append(raw.decode("utf-8") if isinstance(raw, bytes) else str(raw))
    return data


def _bio_draft(**overrides) -> TaskDraft:
    base = dict(
        action="bio", content_mode="ai", source=EN_SOURCE, language="en",
        max_length=59, schedule_type="interval", interval_minutes=2,
        timezone="Asia/Tehran", step="review",
    )
    base.update(overrides)
    return TaskDraft(**base)


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


def _to_details(wizard, action: str = "bio", source: str = EN_SOURCE):
    _run(wizard._wizard_action(_Event(), f"set:action:{action}", 111))
    _run(wizard._wizard_action(_Event(), "set:mode:ai", 111))
    _run(wizard._wizard_input_handler("source")(source, 111, 9, 0, 0))


def _create_bio_task(wizard, **actions):
    _to_details(wizard)
    for extra in actions.get("before_schedule", ()):
        _run(wizard._wizard_action(_Event(), extra, 111))
    _run(wizard._wizard_input_handler("interval")("2", 111, 9, 0, 0))
    for extra in actions.get("before_create", ()):
        _run(wizard._wizard_action(_Event(), extra, 111))
    return _run(wizard._wizard_action(_Event(), "create", 111))


def test_wizard_defaults_show_source_to_no(wizard):
    _to_details(wizard)
    assert wizard._draft().show_source is False
    _title, body, rows = _run(wizard._wizard_action(_Event(), "step:details", 111))
    assert "Show source: No" in body
    assert "action:taskloom_wizard:set:show_source:1" in _buttons(rows)


def test_wizard_can_switch_no_to_yes_and_back(wizard):
    _to_details(wizard)
    _run(wizard._wizard_action(_Event(), "set:show_source:1", 111))
    assert wizard._draft().show_source is True
    _title, body, rows = _run(wizard._wizard_action(_Event(), "step:details", 111))
    assert "Show source: Yes" in body
    assert "action:taskloom_wizard:set:show_source:0" in _buttons(rows)

    _run(wizard._wizard_action(_Event(), "set:show_source:0", 111))
    assert wizard._draft().show_source is False


def test_wizard_review_reports_the_selected_value(wizard):
    _to_details(wizard)
    _run(wizard._wizard_input_handler("interval")("2", 111, 9, 0, 0))

    _title, body, _rows = _run(wizard._wizard_action(_Event(), "step:review", 111))
    assert "**Show source:** No" in body

    _run(wizard._wizard_action(_Event(), "set:show_source:1", 111))
    _title, body, _rows = _run(wizard._wizard_action(_Event(), "step:review", 111))
    assert "**Show source:** Yes" in body


def test_show_source_toggle_is_offered_for_bio_only(wizard):
    _to_details(wizard, action="username")
    _title, _body, rows = _run(wizard._wizard_action(_Event(), "step:details", 111))
    assert not any("show_source" in data for data in _buttons(rows))


def test_wizard_creation_persists_show_source_yes(wizard, repo):
    _create_bio_task(wizard, before_create=("set:show_source:1",))
    task = _run(repo.list_tasks(OWNER))[0]
    policy = derive_policy(task.ai_instruction)
    assert policy.source == EN_SOURCE and policy.show_source is True


def test_wizard_creation_persists_the_default_no(wizard, repo):
    _create_bio_task(wizard)
    task = _run(repo.list_tasks(OWNER))[0]
    policy = derive_policy(task.ai_instruction)
    assert policy.source == EN_SOURCE and policy.show_source is False


def test_edit_prefills_the_stored_display_value(wizard, repo):
    from backend.ai import task_wizard

    _create_bio_task(wizard, before_create=("set:show_source:1",))
    task = _run(repo.list_tasks(OWNER))[0]

    draft = task_wizard.draft_from_task(task)
    assert draft.show_source is True and draft.editing_task_id == task.id

    _run(wizard._start_edit(str(task.id)))
    assert wizard._draft().show_source is True
    _title, body, _rows = _run(wizard._wizard_action(_Event(), "step:details", 111))
    assert "Show source: Yes" in body


def test_edit_preserves_the_value_when_nothing_is_changed(wizard, repo):
    _create_bio_task(wizard, before_create=("set:show_source:1",))
    task = _run(repo.list_tasks(OWNER))[0]

    _run(wizard._start_edit(str(task.id)))
    _run(wizard._wizard_action(_Event(), "create", 111))
    updated = _run(repo.list_tasks(OWNER))[0]
    assert updated.id == task.id and updated.version == task.version + 1
    assert derive_policy(updated.ai_instruction).show_source is True


def test_edit_can_turn_rendering_off_through_the_cas_path(wizard, repo):
    _create_bio_task(wizard, before_create=("set:show_source:1",))
    task = _run(repo.list_tasks(OWNER))[0]

    _run(wizard._start_edit(str(task.id)))
    _run(wizard._wizard_action(_Event(), "set:show_source:0", 111))
    _run(wizard._wizard_action(_Event(), "create", 111))
    updated = _run(repo.list_tasks(OWNER))[0]
    assert updated.id == task.id and updated.version == task.version + 1
    assert derive_policy(updated.ai_instruction).show_source is False


def test_edit_can_turn_rendering_on_through_the_cas_path(wizard, repo):
    _create_bio_task(wizard)
    task = _run(repo.list_tasks(OWNER))[0]
    assert derive_policy(task.ai_instruction).show_source is False

    _run(wizard._start_edit(str(task.id)))
    _run(wizard._wizard_action(_Event(), "set:show_source:1", 111))
    _run(wizard._wizard_action(_Event(), "create", 111))
    updated = _run(repo.list_tasks(OWNER))[0]
    assert updated.version == task.version + 1
    assert derive_policy(updated.ai_instruction).show_source is True


def test_unrepresentable_display_choice_fails_closed():
    from backend.ai import task_wizard

    # No source: a stray display flag cannot be represented.
    assert task_wizard.instruction_problem(_bio_draft(source="", show_source=True)) is not None
    assert task_wizard.instruction_problem(_bio_draft(source="", show_source=False)) is None
    assert task_wizard.instruction_problem(_bio_draft(show_source=True)) is None


def test_re_entering_the_source_resets_display_to_the_safe_default(wizard):
    _to_details(wizard)
    _run(wizard._wizard_action(_Event(), "set:show_source:1", 111))
    _run(wizard._wizard_input_handler("source")(EN_SOURCE, 111, 9, 0, 0))
    assert wizard._draft().show_source is False


# ── execution: prepare-ahead, boundary and non-Bio behaviour ────────────────


class FakeTool:
    permission_level = PermissionLevel.READ_WRITE
    long_running = False
    safe = True
    return_type = "object"
    description = "test"
    parameters = {}

    def __init__(self, name, calls, success=True):
        self.name, self.calls, self.success = name, calls, success

    async def execute(self, context, arguments):
        self.calls.append((self.name, arguments, context.owner_id))
        return ToolResult(self.success, "ok" if self.success else "failed")


class ScriptedPreparator:
    def __init__(self, texts):
        self.texts = list(texts)
        self.rounds = 0

    async def prepare_validated(self, instruction, templates, *, owner_id, tz_str):
        self.rounds += 1
        text = self.texts.pop(0) if self.texts else EN_ATTRIBUTED
        return [{"name": t["name"], "arguments": {"text": text}} for t in templates]


def ai_task_data(**overrides):
    value = {
        "label": "Rei bio",
        "schedule_type": "interval",
        "schedule": {"seconds": 120},
        "timezone": "UTC",
        "next_run_at": NOW,
        "actions": [{"name": "set_bio", "arguments": {}}],
        "notification_destination": {"chat_id": 1},
        "ai_instruction": EN_SOURCE_ONLY,
    }
    value.update(overrides)
    return value


def build_coordinator(repo, preparator, calls, owner=1):
    registry = ToolRegistry()
    registry.register(FakeTool("set_bio", calls))
    ctx = ToolContext(None, owner, "UTC")
    return TaskExecutionCoordinator(
        repo, ToolExecutor(registry, ctx), owner, ctx, preparator=preparator
    )


async def make_claimed_occurrence(repo, task, boundary=NOW + timedelta(seconds=120)):
    from backend.ai.task_scheduler import occurrence_key

    return await repo.create_occurrence(1, {
        "task_id": task.id,
        "occurrence_key": occurrence_key(task.id, boundary),
        "definition_version": task.version,
        "action_snapshot": task.actions,
        "scheduled_for": boundary,
    })


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "instruction,prepared_text,expected",
    [
        (EN_SOURCE_ONLY, EN_ATTRIBUTED, EN_LINE),
        (EN_SOURCE_SHOWN, EN_ATTRIBUTED, EN_ATTRIBUTED),
        (FA_SOURCE_ONLY, FA_ATTRIBUTED, FA_LINE),
        (FA_SOURCE_SHOWN, FA_ATTRIBUTED, FA_ATTRIBUTED),
    ],
)
async def test_boundary_execution_honours_the_stored_display_choice(
    instruction, prepared_text, expected
):
    repo = InMemoryTaskRepository()
    task = await repo.create_task(1, ai_task_data(ai_instruction=instruction))
    calls: list = []
    coordinator = build_coordinator(repo, ScriptedPreparator([prepared_text]), calls)
    occurrence = await make_claimed_occurrence(repo, task)
    repo._occurrences[(task.id, occurrence.occurrence_key)].status = "running"

    result = await coordinator.execute(
        await repo.get_occurrence(1, task.id, occurrence.occurrence_key)
    )
    assert result.success is True
    assert calls == [("set_bio", {"text": expected}, 1)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "instruction,expected",
    [(EN_SOURCE_ONLY, EN_LINE), (EN_SOURCE_SHOWN, EN_ATTRIBUTED)],
)
async def test_prepare_ahead_preserves_the_display_choice(instruction, expected):
    repo = InMemoryTaskRepository()
    task = await repo.create_task(1, ai_task_data(ai_instruction=instruction))
    calls: list = []
    coordinator = build_coordinator(repo, ScriptedPreparator([EN_ATTRIBUTED]), calls)
    occurrence = await make_claimed_occurrence(repo, task)

    prepared = await coordinator.prepare_ahead(occurrence)
    assert prepared is not None and calls == []  # no side effect during preparation
    stored = await repo.get_occurrence(1, task.id, occurrence.occurrence_key)
    # The VALIDATED attributed text is what is persisted (audit copy intact).
    assert stored.preparation_metadata["action"]["arguments"]["text"] == EN_ATTRIBUTED

    repo._occurrences[(task.id, occurrence.occurrence_key)].status = "running"
    stored = await repo.get_occurrence(1, task.id, occurrence.occurrence_key)
    result = await coordinator.execute(stored)
    assert result.success is True
    assert calls == [("set_bio", {"text": expected}, 1)]


@pytest.mark.asyncio
async def test_static_non_bio_task_is_unaffected():
    repo = InMemoryTaskRepository()
    task = await repo.create_task(1, {
        "label": "hello",
        "schedule_type": "interval",
        "schedule": {"seconds": 60},
        "timezone": "UTC",
        "next_run_at": NOW,
        "actions": [{"name": "send_message", "arguments": {"text": "سلام"}}],
        "notification_destination": {"chat_id": 1},
    })
    calls: list = []
    registry = ToolRegistry()
    registry.register(FakeTool("send_message", calls))
    ctx = ToolContext(None, 1, "UTC")
    coordinator = TaskExecutionCoordinator(repo, ToolExecutor(registry, ctx), 1, ctx)
    occurrence = await make_claimed_occurrence(repo, task)
    repo._occurrences[(task.id, occurrence.occurrence_key)].status = "running"

    result = await coordinator.execute(
        await repo.get_occurrence(1, task.id, occurrence.occurrence_key)
    )
    assert result.success is True
    assert calls == [("send_message", {"text": "سلام"}, 1)]


# ── Bio Guardian still owns every Bio mutation ──────────────────────────────


@pytest.fixture
def _guardian_env():
    from backend.profile import scheduler as profile_scheduler
    from backend.services import bio_guardian

    bio_guardian.reset_window_for_tests()
    previous_client = profile_scheduler._client

    async def _rpc(request):
        return MagicMock()

    profile_scheduler._client = MagicMock(side_effect=_rpc)
    yield
    profile_scheduler._client = previous_client
    bio_guardian.reset_window_for_tests()


def _real_bio_registry(owner=9):
    ctx = ToolContext(telegram=None, owner_id=owner, tz_str="UTC", client=None, extra={})
    return ToolExecutor(create_default_registry(ctx), ctx), ctx


@pytest.mark.asyncio
async def test_label_free_bio_still_shares_the_one_guardian_window(_guardian_env):
    from backend.services import bio_guardian, bio_service

    executor, ctx = _real_bio_registry(owner=9)
    first = await bio_service.do_text(9, "manual bio", tz_str="UTC")
    assert first.startswith("✅"), first
    assert bio_guardian.seconds_until_bio_mutation_allowed() > 0

    # The label-free presentation output goes through the SAME boundary.
    presented = present_calls(
        [{"name": "bio_set_text", "arguments": {"text": EN_ATTRIBUTED}}], EN_SOURCE_ONLY
    )
    assert presented[0]["arguments"]["text"] == EN_LINE
    results = await executor.execute_calls(
        presented, owner_id=9, session_id="s", context_override=ctx,
    )
    assert results[0].success is False
    assert "NOT updated" in results[0].message
