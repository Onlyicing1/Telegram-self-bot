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

_MAX_LIST_ROWS = 8
_MAX_INSPECT_OCC = 8

_STATUS_ICON = {
    "active": "🟢",
    "paused": "⏸",
    "completed": "✅",
    "failed": "❌",
    "expired": "⌛",
    "deleted": "🗑",
}
_SCHEDULE_ICON = {
    "once": "1️⃣",
    "interval": "🔁",
    "daily": "📅",
    "weekly": "🗓",
}


def _service(owner_id: int):
    from backend.ai.task_management import TaskManagementService
    from backend.ai.database.manager import get_repository_manager
    return TaskManagementService(get_repository_manager().task, owner_id)


def _fmt_dt(value) -> str:
    if value is None:
        return "—"
    return value.strftime("%m-%d %H:%M")


def _task_row(task) -> tuple[str, str]:
    icon = _STATUS_ICON.get(task.status, "❓")
    sched = _SCHEDULE_ICON.get(task.schedule_type, "•")
    label = task.label[:38] + ("…" if len(task.label) > 38 else "")
    return f"{icon} #{task.id} {label}", f"panel:taskloom_task:{task.id}"


def _nav(builder: InlinePanelBuilder) -> None:
    builder.add_buttons(
        ("⬅ Back", "panel:_nav:back"),
        ("🏠 Home", "panel:_nav:home"),
    )


async def _taskloom_panel(event, extra: str) -> tuple[str, str, list] | None:
    """LEVEL 1 — task list with live counts."""
    from backend.helper.inline_engine import _owner_id
    service = _service(_owner_id)
    tasks = await service.list_tasks()

    active = sum(1 for t in tasks if t.status == "active")
    paused = sum(1 for t in tasks if t.status == "paused")
    done = sum(1 for t in tasks if t.status in ("completed", "failed", "expired", "deleted"))

    lines = [
        "**🧵 Taskloom**",
        f"🟢 {active} active · ⏸ {paused} paused · 🗄 {done} closed",
        "",
    ]
    builder = InlinePanelBuilder()

    if not tasks:
        lines.append("_No tasks yet._")
        lines.append("_Say e.g. **every minute write hello**_")
    else:
        lines.append("**Tasks**")
        for task in tasks[:_MAX_LIST_ROWS]:
            text, cb = _task_row(task)
            builder.add_row(text, cb)
        remaining = len(tasks) - _MAX_LIST_ROWS
        if remaining > 0:
            lines.append("")
            lines.append(f"_…and {remaining} more (use .task list)_")

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
        return "Taskloom", "❌ Invalid task id.", []

    service = _service(_owner_id)
    view = await service.inspect(task_id, occurrence_limit=_MAX_INSPECT_OCC)
    if view is None:
        return "Taskloom", "❌ Task not found.", []

    task, occurrences = view.task, view.occurrences
    icon = _STATUS_ICON.get(task.status, "❓")
    lines = [
        f"{icon} **#{task.id} {task.label[:60]}**",
        "",
        f"**Schedule:** {_SCHEDULE_ICON.get(task.schedule_type, '•')} {task.schedule_type} · v{task.version}",
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
            oicon = _STATUS_ICON.get(occ.status, "❓")
            lines.append(
                f"  {oicon} {_fmt_dt(occ.scheduled_for)} · {occ.status} · try {occ.attempt}"
            )

    builder = InlinePanelBuilder()
    if task.status == "active":
        builder.add_row("⏸ Pause", f"action:taskloom_pause:{task.id}:{task.version}")
    elif task.status == "paused":
        builder.add_row("▶️ Resume", f"action:taskloom_resume:{task.id}:{task.version}")
    if task.status not in ("completed", "deleted", "expired"):
        builder.add_row("✅ Complete", f"action:taskloom_complete:{task.id}:{task.version}")
    if task.status not in ("deleted",):
        builder.add_row("🗑 Delete", f"action:taskloom_delete:{task.id}:{task.version}")
    builder.add_row("🔄 Refresh", f"panel:taskloom_task:{task.id}")
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
        return "Taskloom", "❌ Invalid action arguments.", []
    task_id, version = parsed
    service = _service(_owner_id)
    try:
        task = await getattr(service, verb)(task_id, version)
    except Exception:
        logger.exception("Taskloom %s failed for task %s", verb, task_id)
        return f"Task #{task_id}", "❌ Operation failed; no change was confirmed.", []
    if task is None:
        return f"Task #{task_id}", "❌ Task not found, ownership check failed, or version is stale.", []
    notice = f"✅ {verb}d task #{task.id} · v{task.version}\n\n"
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
        register_panel("taskloom", _taskloom_panel, parent="ai", title="🧵 Taskloom")
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
