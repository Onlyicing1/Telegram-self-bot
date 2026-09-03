"""Presentation helpers for the user-facing task management boundary."""
from __future__ import annotations

from backend.ai.task_management import TaskManagementService, TaskView

MAX_LINES = 20

_STATUS_ICONS = {"active": "▶️", "paused": "⏸", "completed": "✅", "failed": "❌", "expired": "⌛", "deleted": "🗑"}


def _task_line(task) -> str:
    next_run = task.next_run_at.isoformat() if task.next_run_at else "none"
    icon = _STATUS_ICONS.get(str(task.status), "•")
    return f"{icon} #{task.id} {task.label[:80]} · {task.status} · v{task.version} · next {next_run}"


async def list_text(service: TaskManagementService, *, status: str | None = None) -> str:
    """Render the owner's task list, optionally filtered by *status*.

    The status label (paused / active / completed) comes from the AI tool's
    validated argument — never free text. The empty text keeps the exact
    "No tasks found." phrasing so existing consumers can detect emptiness.
    """
    tasks = await service.list_tasks(status=status)
    title = f"Tasks · {status}" if status else "Tasks"
    if not tasks:
        return f"{title}\n\nNo tasks found."
    lines = [title, ""] + [_task_line(task) for task in tasks[:MAX_LINES]]
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
