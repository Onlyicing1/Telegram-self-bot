"""Presentation helpers for the user-facing task management boundary."""
from __future__ import annotations

from backend.ai.task_management import TaskManagementService, TaskView

MAX_LINES = 20


def _task_line(task) -> str:
    next_run = task.next_run_at.isoformat() if task.next_run_at else "none"
    return f"#{task.id} {task.label[:80]} · {task.status} · v{task.version} · next {next_run}"


async def list_text(service: TaskManagementService) -> str:
    tasks = await service.list_tasks()
    if not tasks:
        return "Tasks\n\nNo tasks found."
    lines = ["Tasks", ""] + [_task_line(task) for task in tasks[:MAX_LINES]]
    if len(tasks) > MAX_LINES:
        lines.append(f"…and {len(tasks) - MAX_LINES} more")
    return "\n".join(lines)


async def inspect_text(service: TaskManagementService, task_id: int) -> str:
    view: TaskView | None = await service.inspect(task_id, occurrence_limit=10)
    if view is None:
        return "Task not found."
    lines = [_task_line(view.task), f"schedule: {view.task.schedule_type}", f"timezone: {view.task.timezone}", "occurrences:"]
    lines.extend(f"- {item.occurrence_key} · {item.status} · attempt {item.attempt}" for item in view.occurrences[:10])
    return "\n".join(lines)
