"""Deterministic Todo title resolution — 0, 1 or N candidates, never a guess.

Every mutation that addresses a todo by the owner's own words goes through
``TaskManagementService.resolve_todos`` / ``resolve_todo_target``. These tests
pin the three deterministic tiers (id, all query tokens inside the title, the
whole title inside the sentence), the normalization the owner's spelling
actually needs (Persian/Arabic script variants, ZWNJ, digits), the ambiguity
refusal, and owner scoping.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from backend.ai.database.task_repository import InMemoryTaskRepository
from backend.ai.task_creation import TaskCreationService
from backend.ai.task_management import (
    MAX_TODO_QUERY_CHARS,
    TODO_RESOLUTION_AMBIGUOUS,
    TODO_RESOLUTION_NOT_FOUND,
    TODO_RESOLUTION_UNIQUE,
    TODO_TARGET_AMBIGUOUS,
    TODO_TARGET_INVALID,
    TODO_TARGET_NOT_FOUND,
    TODO_TARGET_OK,
    TaskManagementService,
    format_todo_resolution,
)

OWNER = 4242
OTHER = 9999


async def _todo(repo, label, owner=OWNER, minutes=0):
    return await TaskCreationService(repo, owner).create_todo(
        label,
        "Asia/Tehran",
        datetime(2026, 9, 26, 9, 0, tzinfo=timezone.utc) + timedelta(minutes=minutes),
    )


async def _service_with(*labels):
    repo = InMemoryTaskRepository()
    todos = []
    for index, label in enumerate(labels):
        todos.append(await _todo(repo, label, minutes=index))
    return TaskManagementService(repo, OWNER), todos


# ── single match ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_reference_matching_the_only_todo_resolves_to_it():
    service, todos = await _service_with("گزارش دانشگاه رو بنویسم")
    resolution = await service.resolve_todos("گزارش دانشگاه")
    assert resolution.status == TODO_RESOLUTION_UNIQUE
    assert [c.task_id for c in resolution.candidates] == [todos[0].id]
    assert resolution.candidates[0].version == todos[0].version
    assert resolution.candidates[0].label == "گزارش دانشگاه رو بنویسم"


@pytest.mark.asyncio
async def test_reference_matching_by_english_words_and_normalization():
    service, todos = await _service_with("Write the University Report")
    for query in ("university report", "UNIVERSITY", "  write   the  "):
        resolution = await service.resolve_todos(query)
        assert resolution.status == TODO_RESOLUTION_UNIQUE, query
        assert resolution.candidates[0].task_id == todos[0].id


@pytest.mark.asyncio
async def test_persian_spelling_variants_match_the_stored_title():
    # The stored title uses the Arabic-script yeh/kaf spelling; the owner's
    # reference is written with the Persian letters (and vice versa) — the
    # established normalization folds both to one matching form.
    service, todos = await _service_with("برنامه ريزي کاري")
    for query in ("ریزی", "ريزي کاری", "کاری", "برنامه"):
        resolution = await service.resolve_todos(query)
        assert resolution.status == TODO_RESOLUTION_UNIQUE, query
        assert resolution.candidates[0].task_id == todos[0].id


@pytest.mark.asyncio
async def test_a_title_inside_the_sentence_resolves(monkeypatch):
    service, todos = await _service_with("خرید نان")
    resolution = await service.resolve_todos("خرید نان رو انجام‌شده کن")
    assert resolution.status == TODO_RESOLUTION_UNIQUE
    assert resolution.candidates[0].task_id == todos[0].id


@pytest.mark.asyncio
async def test_todo_number_resolves_by_identity():
    service, todos = await _service_with("first", "second")
    for spelling in (str(todos[1].id), str(todos[1].id).translate(str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹"))):
        resolution = await service.resolve_todos(spelling)
        assert resolution.status == TODO_RESOLUTION_UNIQUE
        assert resolution.candidates[0].task_id == todos[1].id


# ── zero matches ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_zero_matches_is_an_honest_not_found():
    service, _todos = await _service_with("گزارش دانشگاه")
    for query in ("خرید ماشین", "unrelated", "", "   ", "x" * (MAX_TODO_QUERY_CHARS + 1)):
        resolution = await service.resolve_todos(query)
        assert resolution.status == TODO_RESOLUTION_NOT_FOUND, query
        assert resolution.candidates == ()
    target = await service.resolve_todo_target(query="خرید ماشین")
    assert target.status == TODO_TARGET_NOT_FOUND
    assert "No todo found" in target.message


@pytest.mark.asyncio
async def test_a_very_short_title_is_not_matched_inside_a_sentence():
    """A one- or two-character title must not match almost any sentence."""
    service, _todos = await _service_with("آب")
    resolution = await service.resolve_todos("آب رو بیار")
    assert resolution.status == TODO_RESOLUTION_NOT_FOUND


# ── multiple matches ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_two_matching_todos_refuse_and_list_the_candidates():
    service, todos = await _service_with(
        "گزارش دانشگاه — شنبه", "گزارش دانشگاه — پروژه پایان‌ترم"
    )
    resolution = await service.resolve_todos("گزارش دانشگاه")
    assert resolution.status == TODO_RESOLUTION_AMBIGUOUS
    assert sorted(c.task_id for c in resolution.candidates) == sorted(t.id for t in todos)
    text = format_todo_resolution(resolution)
    assert "which one do you mean?" in text
    assert f"#{todos[0].id}" in text and f"#{todos[1].id}" in text

    target = await service.resolve_todo_target(query="گزارش دانشگاه")
    assert target.status == TODO_TARGET_AMBIGUOUS
    assert target.task is None
    assert "which one do you mean?" in target.message


@pytest.mark.asyncio
async def test_the_candidate_list_is_bounded_and_flags_overflow():
    labels = [f"خرید {index}" for index in range(12)]
    service, todos = await _service_with(*labels)
    resolution = await service.resolve_todos("خرید")
    assert resolution.status == TODO_RESOLUTION_AMBIGUOUS
    assert len(resolution.candidates) <= 8
    assert resolution.overflowed is True
    assert "…and more" in format_todo_resolution(resolution)
    assert len({c.task_id for c in resolution.candidates}) == len(resolution.candidates)


# ── target resolution for a mutation ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_target_resolution_by_id_is_todo_only_and_owner_scoped():
    repo = InMemoryTaskRepository()
    todo = await _todo(repo, "mine")
    scheduled = await repo.create_task(
        OWNER,
        {
            "label": "scheduled",
            "schedule_type": "interval",
            "schedule": {"seconds": 60},
            "timezone": "UTC",
            "actions": [{"name": "send_message", "arguments": {"text": "x"}}],
            "notification_destination": {},
        },
    )
    service = TaskManagementService(repo, OWNER)
    target = await service.resolve_todo_target(task_id=todo.id)
    assert target.status == TODO_TARGET_OK and target.task.id == todo.id
    # A scheduled task is not a todo: the todo surface never mutates one.
    assert (await service.resolve_todo_target(task_id=scheduled.id)).status == TODO_TARGET_NOT_FOUND
    # A foreign todo is indistinguishable from a missing one.
    assert (await TaskManagementService(repo, OTHER).resolve_todo_target(task_id=todo.id)).status == TODO_TARGET_NOT_FOUND


@pytest.mark.asyncio
async def test_target_resolution_rejects_both_or_neither_reference():
    service, todos = await _service_with("only todo")
    both = await service.resolve_todo_target(task_id=todos[0].id, query="only")
    assert both.status == TODO_TARGET_INVALID and both.task is None
    neither = await service.resolve_todo_target(task_id=0, query="  ")
    assert neither.status == TODO_TARGET_INVALID


@pytest.mark.asyncio
async def test_resolution_is_owner_scoped():
    repo = InMemoryTaskRepository()
    await _todo(repo, "other owner report", owner=OTHER)
    service = TaskManagementService(repo, OWNER)
    assert (await service.resolve_todos("other owner report")).status == TODO_RESOLUTION_NOT_FOUND
    assert (await service.resolve_todos("report")).status == TODO_RESOLUTION_NOT_FOUND
