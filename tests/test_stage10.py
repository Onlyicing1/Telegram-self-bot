from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from backend.ai.database.task_repository import InMemoryTaskRepository
from backend.ai.task_creation import TaskCreationService
from backend.ai.task_management import TaskManagementService, TaskView
from backend.ai.task_management_interface import inspect_text, list_text


REF = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
RUN_AT = datetime(2026, 1, 2, 9, 30, tzinfo=timezone.utc)


class PresentationService:
    def __init__(self, tasks, occurrences=None):
        self.tasks = list(tasks)
        self.occurrences = list(occurrences or [])

    async def list_tasks(self, status=None):
        if status is None:
            return list(self.tasks)
        return [task for task in self.tasks if task.status == status]

    async def inspect(self, task_id, occurrence_limit=100):
        task = next((task for task in self.tasks if task.id == task_id), None)
        return TaskView(task, self.occurrences[:occurrence_limit]) if task else None


def render_task(task_id, label, status="active", next_run_at=RUN_AT, version=1, **extra):
    return SimpleNamespace(
        id=task_id,
        label=label,
        status=status,
        next_run_at=next_run_at,
        version=version,
        schedule_type=extra.get("schedule_type", "daily"),
        timezone=extra.get("timezone", "UTC"),
        updated_at=extra.get("updated_at", RUN_AT),
    )


def candidate(label="task"):
    return {
        "label": label,
        "schedule_type": "daily",
        "schedule": {"hour": 13, "minute": 0, "timezone": "UTC"},
        "timezone": "UTC",
        "actions": [{"name": "account_show", "arguments": {}}],
        "notification_destination": {"kind": "owner"},
    }


@pytest.mark.asyncio
async def test_management_presentation_is_owner_scoped_and_bounded():
    repo = InMemoryTaskRepository()
    task = await TaskCreationService(repo, 42).create(candidate("visible"), REF)
    await TaskCreationService(repo, 7).create(candidate("hidden"), REF)
    service = TaskManagementService(repo, 42)
    text = await list_text(service)
    assert "visible" in text and "hidden" not in text
    detail = await inspect_text(service, task.id)
    assert f"#{task.id}" in detail
    assert await inspect_text(TaskManagementService(repo, 7), task.id) == "Task not found."


@pytest.mark.asyncio
async def test_stale_management_mutation_does_not_report_success():
    repo = InMemoryTaskRepository()
    task = await TaskCreationService(repo, 42).create(candidate(), REF)
    service = TaskManagementService(repo, 42)
    changed = await service.pause(task.id, task.version)
    assert changed is not None and changed.status == "paused"
    assert await service.resume(task.id, task.version) is None


@pytest.mark.asyncio
async def test_task_list_uses_vertical_mobile_blocks_for_mixed_content():
    tasks = [
        render_task(6, "Morning workout", status="active", version=28),
        render_task(5, "یادآوری جلسه تیم / Sprint review", status="paused", next_run_at=None),
        render_task(4, "Release notes", status="completed"),
    ]

    text = await list_text(PresentationService(tasks))

    assert text.startswith("Tasks\n\nTask #6\n")
    assert "Title: Morning workout" in text
    assert "Status: ▶️ Active" in text
    assert "Status: ⏸ Paused" in text
    assert "Status: ✅ Completed" in text
    assert "Next: 2026-01-02 13:00 +0330" in text
    assert "Next: Not scheduled" in text
    assert "یادآوری جلسه تیم / Sprint review" in text
    assert "\n\nTask #5\n" in text
    assert "\n\nTask #4\n" in text
    assert "Version:" not in text
    assert "|" not in text
    assert " · " not in text


@pytest.mark.asyncio
async def test_task_list_preserves_long_titles_without_silent_truncation():
    title = "این یک عنوان طولانی برای بررسی خوانایی در تلگرام است " + "with mixed English " * 8
    text = await list_text(PresentationService([render_task(9, title)]))

    assert title in text
    assert "Title: " + title in text
    assert "…" not in text


@pytest.mark.asyncio
async def test_task_list_filter_is_visible_without_reintroducing_table_layout():
    tasks = [
        render_task(1, "Active task"),
        render_task(2, "Paused task", status="paused"),
    ]

    text = await list_text(PresentationService(tasks), status="paused")

    assert text == (
        "Tasks\n\nFilter: Paused\n\n"
        "Task #2\n"
        "Title: Paused task\n"
        "Status: ⏸ Paused\n"
        "Next: 2026-01-02 13:00 +0330"
    )
    assert "Task #1" not in text
    assert "|" not in text


@pytest.mark.asyncio
async def test_empty_task_list_keeps_clear_user_facing_message():
    assert await list_text(PresentationService([])) == "Tasks\n\nNo tasks found."


@pytest.mark.asyncio
async def test_task_detail_moves_technical_fields_below_the_primary_block():
    task = render_task(
        6,
        "بررسی گزارش / Review report",
        status="paused",
        version=28,
        schedule_type="weekly",
        timezone="Asia/Tehran",
    )
    occurrence = SimpleNamespace(
        occurrence_key="2026-W01",
        status="succeeded",
        scheduled_for=RUN_AT,
        attempt=2,
    )

    text = await inspect_text(PresentationService([task], [occurrence]), task.id)

    assert text.startswith(
        "Task #6\n"
        "Title: بررسی گزارش / Review report\n"
        "Status: ⏸ Paused\n"
        "Next: 2026-01-02 13:00 +0330\n"
        "Version: v28"
    )
    assert "\n\nSchedule: weekly\nTimezone: Asia/Tehran" in text
    assert "\n\nRecent occurrences:\n" in text
    assert "• 2026-W01\n  Status: ✅ Succeeded\n  Scheduled: 2026-01-02 13:00 +0330\n  Attempt: 2" in text
    assert "|" not in text


@pytest.mark.asyncio
async def test_task_detail_without_occurrences_is_explicit():
    task = render_task(3, "One-time task", status="active")

    text = await inspect_text(PresentationService([task]), task.id)

    assert text.endswith("Recent occurrences:\nNo occurrences yet.")
