"""
Taskloom — Telegram-native Task management interface (AI section).

A dedicated visual surface over the EXISTING durable task system:

    Taskloom panels (this file, Glass UI only)
      -> TaskManagementService      (backend/ai/task_management.py)
        -> TaskRepository           (owner-scoped, CAS transitions)
          -> durable ai_tasks / ai_task_occurrences

The UI never touches the database or Telegram directly: every read goes
through TaskManagementService (list/inspect), every mutation goes through
the service's CAS-guarded transitions (pause/resume/complete/delete).
No second scheduler, no second executor, no persistence layer.

Entry point: the AI panel button "Taskloom" -> panel:taskloom.
"""
from __future__ import annotations

import logging

from backend.helper import (
    InlinePanelBuilder,
    register_action,
    register_panel,
    register_inline_builder,
    render,
)

logger = logging.getLogger(__name__)

_MAX_LIST_ROWS = 4
_MAX_INSPECT_OCC = 8

_STATUS_ICON = {
    "active": "●",
    "paused": "∥",
    "completed": "✓",
    "failed": "×",
    "expired": "×",
    "deleted": "×",
}
_SCHEDULE_ICON = {
    "once": "·",
    "interval": "⟳",
    "daily": "◷",
    "weekly": "◷",
    "event": "⚡",
}


def _service(owner_id: int):
    from backend.ai.task_management import TaskManagementService
    from backend.ai.database.manager import get_repository_manager
    return TaskManagementService(get_repository_manager().task, owner_id)


def _fmt_dt(value) -> str:
    if value is None:
        return "—"
    from datetime import timezone
    from zoneinfo import ZoneInfo

    if getattr(value, "tzinfo", None) is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(ZoneInfo("Asia/Tehran")).strftime("%m-%d %H:%M")


def _task_row(task) -> tuple[str, str]:
    label = task.label[:38] + ("…" if len(task.label) > 38 else "")
    return f"Task {task.id}: {label}", f"panel:taskloom_task:{task.id}"


def _nav(builder: InlinePanelBuilder) -> None:
    builder.add_buttons(
        ("← Back", "panel:_nav:back"),
        ("⌂ Home", "panel:_nav:home"),
    )


async def _taskloom_panel(event, extra: str) -> tuple[str, str, list] | None:
    """LEVEL 1 — compact paginated task list."""
    from backend.helper.inline_engine import _owner_id
    service = _service(_owner_id)
    tasks = await service.list_tasks()
    try:
        page = max(0, int(extra or 0))
    except (TypeError, ValueError):
        page = 0
    page_count = max(1, (len(tasks) + _MAX_LIST_ROWS - 1) // _MAX_LIST_ROWS)
    page = min(page, page_count - 1)
    visible = tasks[page * _MAX_LIST_ROWS:(page + 1) * _MAX_LIST_ROWS]

    # Authoritative per-status counts from the service: the normal task
    # collection excludes terminal deleted tasks, so deleted tasks can never
    # inflate the active/paused/closed totals.
    counts = await service.counts()
    active = counts.get("active", 0)
    paused = counts.get("paused", 0)
    done = counts.get("completed", 0) + counts.get("failed", 0) + counts.get("expired", 0)

    lines = [
        "▦ **Taskloom**",
        f"● {active} active · ∥ {paused} paused · × {done} closed",
        "",
    ]
    builder = InlinePanelBuilder()

    if not tasks:
        lines.append("_No tasks yet._")
        lines.append("_Say e.g. **every minute write hello**_")
    else:
        for task in visible:
            text, cb = _task_row(task)
            builder.add_row(text, cb)
        if page_count > 1:
            builder.add_row(
                "❮" if page > 0 else "·",
                f"panel:taskloom:{page - 1}" if page > 0 else "panel:taskloom",
            )
            builder.add_row(
                f"{page + 1} / {page_count}",
                f"panel:taskloom:{page + 1}" if page + 1 < page_count else f"panel:taskloom:{page}",
            )
            if page + 1 < page_count:
                builder.add_row("❯", f"panel:taskloom:{page + 1}")

    _nav(builder)
    return "Taskloom", "\n".join(lines), builder.build()


async def _taskloom_inline_builder(event, extra: str) -> list:
    result = await _taskloom_panel(event, extra)
    if result is None:
        return [render("Taskloom", "Error loading panel.", [])]
    title, body, buttons = result
    return [render(title, body, buttons)]


async def _task_detail_panel(event, extra: str) -> tuple[str, str, list] | None:
    """LEVEL 2 — one task: metadata + last occurrences + CAS-guarded actions."""
    from backend.helper.inline_engine import _owner_id
    try:
        task_id = int(extra)
    except (TypeError, ValueError):
        return "Taskloom", "× Invalid task id.", []

    service = _service(_owner_id)
    view = await service.inspect(task_id, occurrence_limit=_MAX_INSPECT_OCC)
    if view is None:
        return "Taskloom", "× Task not found.", []

    task, occurrences = view.task, view.occurrences
    icon = _STATUS_ICON.get(task.status, "×")
    destination = task.notification_destination or {}
    destination_label = destination.get("chat_title") or (
        "Current chat" if destination.get("chat_id") else "Unavailable"
    )
    lines = [
        f"{icon} **Task {task.id}**",
        f"`{task.label[:60]}`",
        "",
        f"**Schedule:** {_SCHEDULE_ICON.get(task.schedule_type, '·')} {task.schedule_type} · v{task.version}",
        f"**Destination:** ◉ {destination_label}",
        f"**Timezone:** {task.timezone}",
        f"**Next run:** {_fmt_dt(task.next_run_at)}",
        f"**Updated:** {_fmt_dt(task.updated_at)}",
        "",
        "**Actions:**",
    ]
    for a in task.actions[:3]:
        name = a.get("name", "?")
        args = a.get("arguments", {})
        brief = ", ".join(f"{k}={str(v)[:24]}" for k, v in list(args.items())[:2])
        lines.append(f"  • `{name}`({brief})")

    if occurrences:
        lines += ["", "**Recent occurrences:**"]
        for occ in occurrences[:5]:
            oicon = _STATUS_ICON.get(occ.status, "×")
            lines.append(
                f"  {oicon} {_fmt_dt(occ.scheduled_for)} · {occ.status} · try {occ.attempt}"
            )

    builder = InlinePanelBuilder()
    if task.status == "active":
        builder.add_row("∥ Pause", f"action:taskloom_pause:{task.id}:{task.version}")
    elif task.status == "paused":
        builder.add_row("● Resume", f"action:taskloom_resume:{task.id}:{task.version}")
    if task.status not in ("completed", "deleted", "expired"):
        builder.add_row("✓ Complete", f"action:taskloom_complete:{task.id}:{task.version}")
    if task.status not in ("deleted",):
        builder.add_row("⌫ Delete", f"action:taskloom_delete:{task.id}:{task.version}")
    builder.add_row("⟳ Refresh", f"panel:taskloom_task:{task.id}")
    _nav(builder)
    return f"Task #{task.id}", "\n".join(lines), builder.build()


async def _task_detail_inline_builder(event, extra: str) -> list:
    result = await _task_detail_panel(event, extra)
    if result is None:
        return [render("Taskloom", "Error loading panel.", [])]
    title, body, buttons = result
    return [render(title, body, buttons)]


def _parse_action_extra(extra: str) -> tuple[int, int] | None:
    parts = extra.split(":")
    if len(parts) != 2:
        return None
    try:
        task_id, version = int(parts[0]), int(parts[1])
    except ValueError:
        return None
    if task_id <= 0 or version <= 0:
        return None
    return task_id, version


async def _mutate(event, extra: str, verb: str) -> tuple[str, str, list] | None:
    """Shared CAS mutation: parse -> service.<verb> -> refreshed detail panel."""
    from backend.helper.inline_engine import _owner_id
    parsed = _parse_action_extra(extra)
    if parsed is None:
        return "Taskloom", "× Invalid action arguments.", []
    task_id, version = parsed
    service = _service(_owner_id)
    try:
        task = await getattr(service, verb)(task_id, version)
    except Exception:
        logger.exception("Taskloom %s failed for task %s", verb, task_id)
        return f"Task #{task_id}", "× Operation failed; no change was confirmed.", []
    if task is None:
        return f"Task #{task_id}", "× Task not found, ownership check failed, or version is stale.", []
    notice = f"✓ {verb}d task #{task.id} · v{task.version}\n\n"
    result = await _task_detail_panel(event, str(task_id))
    if result is None:
        return f"Task #{task_id}", notice.rstrip(), []
    title, body, buttons = result
    return title, notice + body, buttons


async def _pause_action(event, extra: str, chat_id: int):
    return await _mutate(event, extra, "pause")


async def _resume_action(event, extra: str, chat_id: int):
    return await _mutate(event, extra, "resume")


async def _complete_action(event, extra: str, chat_id: int):
    return await _mutate(event, extra, "complete")


async def _delete_action(event, extra: str, chat_id: int):
    return await _mutate(event, extra, "delete")


def register(client, owner_id: int, tz_str: str) -> None:
    try:
        register_panel("taskloom", _taskloom_panel, parent="ai", title="▦ Taskloom")
        register_inline_builder("taskloom", _taskloom_inline_builder)
        register_panel("taskloom_task", _task_detail_panel, parent="taskloom", title="Task")
        register_inline_builder("taskloom_task", _task_detail_inline_builder)
        register_action("taskloom_pause", _pause_action)
        register_action("taskloom_resume", _resume_action)
        register_action("taskloom_complete", _complete_action)
        register_action("taskloom_delete", _delete_action)
        logger.info("Taskloom panels registered OK")
    except Exception as exc:
        logger.error("Taskloom registration FAILED: %s", exc)
