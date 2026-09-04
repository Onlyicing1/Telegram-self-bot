"""Presentation helpers for the user-facing task management boundary."""
from __future__ import annotations

from datetime import timezone
from zoneinfo import ZoneInfo

from backend.ai.task_management import TaskManagementService, TaskView

MAX_LINES = 20

# All user-facing task times are displayed in Iran/Tehran local time. The
# IANA zone (never a fixed numeric offset) keeps DST transitions correct;
# persisted instants stay timestamptz/UTC internally.
DISPLAY_TIMEZONE = "Asia/Tehran"
_FALLBACK_NOTE = "⚠️ Memory fallback — Supabase unavailable (state is not durable)."

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
        if getattr(value, "tzinfo", None) is None:
            value = value.replace(tzinfo=timezone.utc)
        local = value.astimezone(ZoneInfo(DISPLAY_TIMEZONE))
        formatted = local.strftime("%Y-%m-%d %H:%M")
        timezone_name = local.tzname()
    except (AttributeError, TypeError, ValueError):
        return str(value)
    return f"{formatted} {timezone_name}" if timezone_name else formatted


def _fallback_active(service: TaskManagementService) -> bool:
    """True when the repository degraded to its in-memory fallback."""
    repository = getattr(service, "repository", None)
    return bool(getattr(repository, "fallback_active", False))


def _task_block(task: object, *, include_version: bool) -> str:
    is_event = str(getattr(task, "schedule_type", "") or "") == "event"
    next_line = (
        "Next: On message event"
        if is_event
        else f"Next: {_format_datetime(getattr(task, 'next_run_at', None), empty='Not scheduled')}"
    )
    lines = [
        f"Task #{getattr(task, 'id', '?')}",
        f"Title: {getattr(task, 'label', '')}",
        f"Status: {_status_text(getattr(task, 'status', ''))}",
        next_line,
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
        lines = header + ["", "No tasks found."]
        if _fallback_active(service):
            lines.append(_FALLBACK_NOTE)
        return "\n".join(lines)

    blocks = [_task_block(task, include_version=False) for task in tasks[:MAX_LINES]]
    if len(tasks) > MAX_LINES:
        blocks.append(f"…and {len(tasks) - MAX_LINES} more")
    rendered = "\n".join(header + ["", "\n\n".join(blocks)])
    if _fallback_active(service):
        rendered += "\n\n" + _FALLBACK_NOTE
    return rendered


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
    if str(getattr(task, "schedule_type", "") or "") == "event":
        from backend.ai.task_trigger import trigger_summary
        trigger = (getattr(task, "schedule", None) or {}).get("trigger") or {}
        try:
            lines.append(f"Trigger: {trigger_summary(trigger)}")
        except Exception:
            lines.append("Trigger: Telegram message")
    if _fallback_active(service):
        lines.append(_FALLBACK_NOTE)
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
