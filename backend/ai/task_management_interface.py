"""Presentation helpers for the user-facing task management boundary."""
from __future__ import annotations

from backend.ai.task_management import TaskManagementService, TaskView

MAX_LINES = 20

_STATUS_LABELS = {
    "active": ("▶️", "Active"),
    "paused": ("⏸", "Paused"),
    "completed": ("✅", "Completed"),
    "failed": ("❌", "Failed"),
    "expired": ("⌛", "Expired"),
    "deleted": ("🗑", "Deleted"),
    "claimed": ("◷", "Claimed"),
    "running": ("⟳", "Running"),
    "succeeded": ("✅", "Succeeded"),
    "retry_pending": ("↻", "Retry pending"),
    "cancelled": ("⊘", "Cancelled"),
    "interrupted": ("⚠️", "Interrupted"),
}


def _status_text(status: object) -> str:
    value = str(status or "").strip()
    icon, label = _STATUS_LABELS.get(
        value.casefold(), ("•", value.replace("_", " ").title() or "Unknown")
    )
    return f"{icon} {label}"


def _status_label(status: object) -> str:
    value = str(status or "").strip()
    return _STATUS_LABELS.get(
        value.casefold(), ("", value.replace("_", " ").title() or "Unknown")
    )[1]


def _format_datetime(value: object, *, empty: str) -> str:
    if value is None:
        return empty
    try:
        formatted = value.strftime("%Y-%m-%d %H:%M")
        timezone_name = value.tzname()
    except (AttributeError, TypeError, ValueError):
        return str(value)
    return f"{formatted} {timezone_name}" if timezone_name else formatted


def _task_block(task: object, *, include_version: bool) -> str:
    lines = [
        f"Task #{getattr(task, 'id', '?')}",
        f"Title: {getattr(task, 'label', '')}",
        f"Status: {_status_text(getattr(task, 'status', ''))}",
        f"Next: {_format_datetime(getattr(task, 'next_run_at', None), empty='Not scheduled')}",
    ]
    if include_version:
        lines.append(f"Version: v{getattr(task, 'version', '?')}")
    return "\n".join(lines)


def _list_header(status: str | None) -> list[str]:
    lines = ["Tasks"]
    if status:
        lines.extend(["", f"Filter: {_status_label(status)}"])
    return lines


async def list_text(service: TaskManagementService, *, status: str | None = None) -> str:
    """Render the owner's bounded task list as separated mobile-first blocks."""
    tasks = await service.list_tasks(status=status)
    header = _list_header(status)
    if not tasks:
        return "\n".join(header + ["", "No tasks found."])

    blocks = [_task_block(task, include_version=False) for task in tasks[:MAX_LINES]]
    if len(tasks) > MAX_LINES:
        blocks.append(f"…and {len(tasks) - MAX_LINES} more")
    return "\n".join(header + ["", "\n\n".join(blocks)])


async def inspect_text(service: TaskManagementService, task_id: int) -> str:
    view: TaskView | None = await service.inspect(task_id, occurrence_limit=10)
    if view is None:
        return "Task not found."

    task = view.task
    lines = [
        _task_block(task, include_version=True),
        "",
        f"Schedule: {task.schedule_type}",
        f"Timezone: {task.timezone}",
    ]
    if view.occurrences:
        occurrence_blocks = []
        for item in view.occurrences[:10]:
            occurrence_blocks.append(
                "\n".join(
                    [
                        f"• {item.occurrence_key}",
                        f"  Status: {_status_text(item.status)}",
                        f"  Scheduled: {_format_datetime(item.scheduled_for, empty='Not scheduled')}",
                        f"  Attempt: {item.attempt}",
                    ]
                )
            )
        lines.extend(["", "Recent occurrences:", "\n\n".join(occurrence_blocks)])
    else:
        lines.extend(["", "Recent occurrences:", "No occurrences yet."])
    return "\n".join(lines)
