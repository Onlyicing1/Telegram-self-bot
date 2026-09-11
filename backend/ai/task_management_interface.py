"""Presentation helpers for the user-facing task management boundary."""
from __future__ import annotations

from datetime import timezone
from zoneinfo import ZoneInfo

from backend.ai.task_management import TaskListSnapshot, TaskManagementService, TaskView

MAX_LINES = 20

# All user-facing task times are displayed in Iran/Tehran local time. The
# IANA zone (never a fixed numeric offset) keeps DST transitions correct;
# persisted instants stay timestamptz/UTC internally.
DISPLAY_TIMEZONE = "Asia/Tehran"
# ONE truthful degraded-store marker, shared by every surface that can read or
# write through the in-memory fallback: a fallback READ returns a partial view
# of the durable tasks, and a fallback WRITE is not durable at all. Either way
# the owner must never be told this state is authoritative.
FALLBACK_NOTE = (
    "⚠️ Memory fallback — Supabase unavailable "
    "(tasks may be missing, and anything created now is not durable)."
)
# Truthful counterpart for a LOCAL resource failure: the durable store may be
# perfectly healthy (a Supabase write succeeded moments earlier), so the
# surface must never claim a Supabase outage. The non-durable semantics are
# identical — only the attribution changes.
FALLBACK_RESOURCE_NOTE = (
    "⚠️ Memory fallback — a local resource error prevented the durable store "
    "from being reached (tasks may be missing, and anything created now is "
    "not durable)."
)


def fallback_note(reason: str) -> str:
    """The truthful degraded-store note for a repository fallback reason.

    Only a real durable-store failure may claim "Supabase unavailable"; any
    other recorded reason (a local OS resource error) keeps the same
    non-durable warning without the false attribution.
    """
    from backend.ai.database.task_repository import FALLBACK_REASON_LOCAL_RESOURCE

    if reason == FALLBACK_REASON_LOCAL_RESOURCE:
        return FALLBACK_RESOURCE_NOTE
    return FALLBACK_NOTE

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
    """True when the repository degraded to its in-memory fallback.

    Only used by the single-task view; the list path reads the marker from its
    own snapshot so the marker and the rendered tasks always describe the same
    read.
    """
    repository = getattr(service, "repository", None)
    return bool(getattr(repository, "fallback_active", False))


def _repository_fallback_reason(service: TaskManagementService) -> str:
    """The repository's recorded degradation reason ("" while healthy)."""
    repository = getattr(service, "repository", None)
    return str(getattr(repository, "fallback_reason", "") or "")


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


async def list_text(
    service: TaskManagementService,
    *,
    status: str | None = None,
    snapshot: TaskListSnapshot | None = None,
) -> str:
    """Render the owner's bounded task list as separated mobile-first blocks.

    Pass ``snapshot`` when the caller already read the list (a tool that also
    reports a count): the rendered tasks and the degraded marker then come
    from ONE repository read instead of two potentially inconsistent ones.
    """
    snapshot = snapshot or await service.snapshot(status=status)
    tasks = snapshot.tasks
    header = _list_header(status)
    if not tasks:
        lines = header + ["", "No tasks found."]
        if snapshot.fallback_active:
            lines.append(fallback_note(snapshot.fallback_reason))
        return "\n".join(lines)

    blocks = [_task_block(task, include_version=False) for task in tasks[:MAX_LINES]]
    if len(tasks) > MAX_LINES:
        blocks.append(f"…and {len(tasks) - MAX_LINES} more")
    rendered = "\n".join(header + ["", "\n\n".join(blocks)])
    if snapshot.fallback_active:
        rendered += "\n\n" + fallback_note(snapshot.fallback_reason)
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
        lines.append(fallback_note(_repository_fallback_reason(service)))
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
