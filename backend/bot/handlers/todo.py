"""
Todo — the basic Todo list on the EXISTING durable task foundation.

A dedicated visual surface over the Todo rows of ``ai_tasks``:

    Todo panels (this file, Glass UI only)
      -> TaskManagementService / TaskCreationService   (backend/ai)
        -> TaskRepository                              (owner-scoped, CAS)
          -> durable ai_tasks rows (schedule_type='todo')

The UI never touches the database or Telegram directly: every read goes
through the service (one snapshot, so rows and the degraded marker agree),
every mutation goes through the service's CAS operations, and creating a todo
goes through the SAME TaskCreationService -> TaskRepository path the
natural-language tools use. No second store, no second state model.

Entry point: the mother Menu button "📋 Todo" -> panel:todo. The panel is a
basic todo list — active items, completed items, add, complete, reopen, edit,
delete. Scheduled tasks and their occurrences stay on the Taskloom surface;
a todo has no schedule and no action, so nothing here can ever be executed.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from backend.helper import (
    InlinePanelBuilder,
    register_action,
    register_input,
    register_panel,
    register_inline_builder,
    render,
)

logger = logging.getLogger(__name__)

_MAX_LIST_ROWS = 4
# The step panel shows fewer rows than the todo list: every step needs TWO rows
# (its title, then its actions), so a short page is what keeps the panel
# readable on a phone.
_MAX_STEP_ROWS = 3
_LIST_PANEL = "todo"
_DONE_PANEL = "todo_done"
_DETAIL_PANEL = "todo_task"
_STEPS_PANEL = "todo_steps"
# The owner's configured timezone (the same value the runtime registers every
# handler with). A todo has no schedule, so this only records the owner's own
# zone on the row — it is never used to invent a time.
_DEFAULT_TZ = "UTC"
_VERB_PAST = {"complete": "completed", "reopen": "reopened"}


def _service(owner_id: int):
    from backend.ai.task_management import TaskManagementService
    from backend.ai.database.manager import get_repository_manager
    return TaskManagementService(get_repository_manager().task, owner_id)


def _owner() -> int:
    from backend.helper.inline_engine import _owner_id
    return _owner_id


def _nav(builder: InlinePanelBuilder) -> None:
    builder.add_buttons(
        ("← Back", "panel:_nav:back"),
        ("⌂ Home", "panel:_nav:home"),
    )


def _row_label(task) -> str:
    label = " ".join(str(getattr(task, "label", "") or "").split()) or "Untitled"
    return f"#{getattr(task, 'id', '?')} · {label[:32] + ('…' if len(label) > 32 else '')}"


def _page_of(tasks: list, extra: str) -> tuple[list, int, int]:
    """Clamp the requested page onto the current, possibly shrunk, list."""
    try:
        page = max(0, int(extra or 0))
    except (TypeError, ValueError):
        page = 0
    page_count = max(1, (len(tasks) + _MAX_LIST_ROWS - 1) // _MAX_LIST_ROWS)
    page = min(page, page_count - 1)
    return tasks[page * _MAX_LIST_ROWS:(page + 1) * _MAX_LIST_ROWS], page, page_count


def _pager(builder: InlinePanelBuilder, panel_id: str, page: int, page_count: int) -> None:
    if page_count <= 1:
        return
    builder.add_row(
        "❮" if page > 0 else "·",
        f"panel:{panel_id}:{page - 1}" if page > 0 else f"panel:{panel_id}",
    )
    builder.add_row(
        f"{page + 1} / {page_count}",
        f"panel:{panel_id}:{page + 1}" if page + 1 < page_count else f"panel:{panel_id}:{page}",
    )
    if page + 1 < page_count:
        builder.add_row("❯", f"panel:{panel_id}:{page + 1}")


async def _todo_list_panel(event, extra: str) -> tuple[str, str, list] | None:
    """LEVEL 1 — the active todos (the completed ones live one tap away)."""
    from backend.ai.task_management_interface import fallback_note

    service = _service(_owner())
    # ONE authoritative read: the rows, the counters and the degraded marker
    # all describe the same repository call.
    snapshot = await service.snapshot(todos_only=True)
    counts = snapshot.counts()
    active = [t for t in snapshot.tasks if str(getattr(t, "status", "")) == "active"]
    completed = counts.get("completed", 0)
    visible, page, page_count = _page_of(active, extra)

    lines = [
        "📋 **Todo**",
        f"▶️ {counts.get('active', 0)} active · ✅ {completed} completed",
        "",
    ]
    builder = InlinePanelBuilder()
    builder.add_row("➕ Add todo", f"input:{_LIST_PANEL}:new")
    if not active:
        lines.append("_No active todos._")
        lines.append("_Tap ➕ Add todo, or say \"add a todo: …\"_")
    else:
        for task in visible:
            builder.add_row(_row_label(task), f"panel:{_DETAIL_PANEL}:{task.id}")
        _pager(builder, _LIST_PANEL, page, page_count)
    if snapshot.fallback_active:
        # A degraded read is not an authoritative empty list.
        lines.append("")
        lines.append(fallback_note(snapshot.fallback_reason))
    builder.add_row(f"✅ Completed ({completed})", f"panel:{_DONE_PANEL}")
    _nav(builder)
    return "Todo", "\n".join(lines), builder.build()


async def _todo_list_inline_builder(event, extra: str) -> list:
    result = await _todo_list_panel(event, extra)
    if result is None:
        return [render("Todo", "Error loading panel.", [])]
    title, body, buttons = result
    return [render(title, body, buttons)]


async def _todo_done_panel(event, extra: str) -> tuple[str, str, list] | None:
    """LEVEL 1b — the completed todos (each one can be reopened or deleted)."""
    from backend.ai.task_management_interface import fallback_note

    service = _service(_owner())
    snapshot = await service.snapshot(status="completed", todos_only=True)
    completed = snapshot.tasks
    visible, page, page_count = _page_of(completed, extra)

    lines = [
        "✅ **Completed todos**",
        f"{len(completed)} completed",
        "",
    ]
    builder = InlinePanelBuilder()
    if not completed:
        lines.append("_Nothing completed yet._")
    else:
        for task in visible:
            builder.add_row(_row_label(task), f"panel:{_DETAIL_PANEL}:{task.id}")
        _pager(builder, _DONE_PANEL, page, page_count)
    if snapshot.fallback_active:
        lines.append("")
        lines.append(fallback_note(snapshot.fallback_reason))
    builder.add_row("▶️ Active todos", f"panel:{_LIST_PANEL}")
    _nav(builder)
    return "Completed todos", "\n".join(lines), builder.build()


async def _todo_done_inline_builder(event, extra: str) -> list:
    result = await _todo_done_panel(event, extra)
    if result is None:
        return [render("Completed todos", "Error loading panel.", [])]
    title, body, buttons = result
    return [render(title, body, buttons)]


async def _todo_detail_panel(event, extra: str) -> tuple[str, str, list] | None:
    """LEVEL 2 — one todo: what it is, plus its CAS-guarded actions."""
    from backend.ai.task_management_interface import fallback_note, todo_detail_text

    try:
        task_id = int(extra)
    except (TypeError, ValueError):
        return "Todo", "× Invalid todo id.", []

    service = _service(_owner())
    view = await service.inspect(task_id, occurrence_limit=0)
    if view is None or str(getattr(view.task, "schedule_type", "")) != "todo":
        return "Todo", "× Todo not found.", []

    task = view.task
    # ONE progress read drives the summary line, the step button and the
    # completion button: the todo detail can never disagree with its own list.
    progress = await service.step_progress(task.id)
    lines = [todo_detail_text(task, progress)]
    if bool(getattr(service.repository, "fallback_active", False)):
        lines.append("")
        lines.append(fallback_note(str(getattr(service.repository, "fallback_reason", "") or "")))

    builder = InlinePanelBuilder()
    if str(task.status) == "active":
        if progress is not None and progress.remaining:
            # Plain completion is refused while a step remains; the labelled
            # button is the explicit way to finish both together.
            builder.add_row(
                f"✓ Complete all ({progress.completed}/{progress.total})",
                f"action:todo_complete_all:{task.id}:{task.version}",
            )
        else:
            builder.add_row("✓ Complete", f"action:todo_complete:{task.id}:{task.version}")
        builder.add_row("✏️ Edit", f"input:{_DETAIL_PANEL}:edit:{task.id}:{task.version}")
    elif str(task.status) == "completed":
        builder.add_row("↩️ Reopen", f"action:todo_reopen:{task.id}:{task.version}")
        builder.add_row("✏️ Edit", f"input:{_DETAIL_PANEL}:edit:{task.id}:{task.version}")
    if progress is not None and progress.total:
        builder.add_row(
            f"🧩 Steps ({progress.completed}/{progress.total})",
            f"panel:{_STEPS_PANEL}:{task.id}",
        )
    else:
        builder.add_row("🧩 Add steps", f"input:{_STEPS_PANEL}:add:{task.id}")
    builder.add_row("🗑 Delete", f"action:todo_delete:{task.id}:{task.version}")
    builder.add_row("⟳ Refresh", f"panel:{_DETAIL_PANEL}:{task.id}")
    _nav(builder)
    return f"Todo #{task.id}", "\n".join(lines), builder.build()


async def _todo_detail_inline_builder(event, extra: str) -> list:
    result = await _todo_detail_panel(event, extra)
    if result is None:
        return [render("Todo", "Error loading panel.", [])]
    title, body, buttons = result
    return [render(title, body, buttons)]


async def _todo_steps_panel(event, extra: str) -> tuple[str, str, list] | None:
    """LEVEL 3 — the ordered steps of ONE todo: complete, edit, delete.

    The panel adds no state of its own: the list, the ✓/○ marks, the progress
    line and the next step all come from the same service read the tools and
    the AI use, so the four surfaces can never disagree about a step.
    """
    from backend.ai.task_management import step_progress_of
    from backend.ai.task_management_interface import fallback_note, steps_block_text

    parts = (extra or "").split(":")
    try:
        task_id = int(parts[0])
    except (TypeError, ValueError, IndexError):
        return "Todo", "× Invalid todo id.", []

    service = _service(_owner())
    steps = await service.list_steps(task_id)
    if steps is None:
        return "Todo", "× Todo not found.", []

    try:
        page = max(0, int(parts[1])) if len(parts) > 1 else 0
    except (TypeError, ValueError):
        page = 0
    page_count = max(1, (len(steps) + _MAX_STEP_ROWS - 1) // _MAX_STEP_ROWS)
    page = min(page, page_count - 1)
    visible = steps[page * _MAX_STEP_ROWS:(page + 1) * _MAX_STEP_ROWS]
    offset = page * _MAX_STEP_ROWS
    progress = step_progress_of(steps)

    task = await service.owner_todo(task_id)
    lines = [steps_block_text(task, steps, progress)]
    if bool(getattr(service.repository, "fallback_active", False)):
        lines.append("")
        lines.append(fallback_note(str(getattr(service.repository, "fallback_reason", "") or "")))

    builder = InlinePanelBuilder()
    for index, step in enumerate(visible, start=offset + 1):
        step_title = " ".join(str(getattr(step, "title", "") or "").split()) or "Untitled"
        mark = "✓" if str(getattr(step, "status", "")) == "completed" else "○"
        label = f"{mark} {index}. {step_title[:26] + ('…' if len(step_title) > 26 else '')}"
        builder.add_row(label, f"panel:{_STEPS_PANEL}:{task_id}:{page}")
        if str(getattr(step, "status", "")) == "completed":
            builder.add_buttons(
                ("↩️ Reopen", f"action:todo_step_reopen:{step.id}:{step.version}"),
                ("✏️ Edit", f"input:{_STEPS_PANEL}:edit:{step.id}:{step.version}"),
                ("🗑", f"action:todo_step_delete:{step.id}:{step.version}"),
            )
        else:
            builder.add_buttons(
                ("✓ Complete", f"action:todo_step_complete:{step.id}:{step.version}"),
                ("✏️ Edit", f"input:{_STEPS_PANEL}:edit:{step.id}:{step.version}"),
                ("🗑", f"action:todo_step_delete:{step.id}:{step.version}"),
            )
    if page_count > 1:
        builder.add_row(
            "❮" if page > 0 else "·",
            f"panel:{_STEPS_PANEL}:{task_id}:{page - 1}" if page > 0 else f"panel:{_STEPS_PANEL}:{task_id}",
        )
        builder.add_row(
            f"{page + 1} / {page_count}",
            f"panel:{_STEPS_PANEL}:{task_id}:{page + 1}" if page + 1 < page_count else f"panel:{_STEPS_PANEL}:{task_id}:{page}",
        )
        if page + 1 < page_count:
            builder.add_row("❯", f"panel:{_STEPS_PANEL}:{task_id}:{page + 1}")
    # Adding steps is only offered while the todo is active: a completed todo
    # accepts no new work until it is reopened (the service refuses otherwise).
    if str(getattr(task, "status", "")) == "active":
        builder.add_row("➕ Add step", f"input:{_STEPS_PANEL}:add:{task_id}")
    builder.add_row("← Todo", f"panel:{_DETAIL_PANEL}:{task_id}")
    _nav(builder)
    return f"Steps of Todo #{task_id}", "\n".join(lines), builder.build()


async def _todo_steps_inline_builder(event, extra: str) -> list:
    result = await _todo_steps_panel(event, extra)
    if result is None:
        return [render("Steps", "Error loading panel.", [])]
    title, body, buttons = result
    return [render(title, body, buttons)]


def _parse_action_extra(extra: str) -> tuple[int, int] | None:
    parts = (extra or "").split(":")
    if len(parts) != 2:
        return None
    try:
        task_id, version = int(parts[0]), int(parts[1])
    except ValueError:
        return None
    if task_id <= 0 or version <= 0:
        return None
    return task_id, version


async def _mutate(extra: str, verb: str) -> tuple[str, str, list] | None:
    """Shared CAS mutation -> refreshed todo detail (nothing on a stale one)."""
    parsed = _parse_action_extra(extra)
    if parsed is None:
        return "Todo", "× Invalid action arguments.", []
    task_id, version = parsed
    service = _service(_owner())
    try:
        task = await getattr(service, verb)(task_id, version)
    except ValueError as exc:
        return f"Todo #{task_id}", f"× {exc}", []
    except Exception:
        logger.exception("Todo %s failed for todo %s", verb, task_id)
        return f"Todo #{task_id}", "× Operation failed; no change was confirmed.", []
    if task is None:
        return (
            f"Todo #{task_id}",
            "× Todo not found, or it changed since this panel was drawn "
            "(version is stale). Nothing was changed.",
            [],
        )
    notice = f"✓ Todo #{task.id} {_VERB_PAST.get(verb, verb + 'd')} · v{task.version}\n\n"
    result = await _todo_detail_panel(None, str(task_id))
    if result is None:
        return f"Todo #{task_id}", notice.rstrip(), []
    title, body, buttons = result
    return title, notice + body, buttons


async def _complete_action(event, extra: str, chat_id: int):
    return await _mutate(extra, "complete")


async def _reopen_action(event, extra: str, chat_id: int):
    return await _mutate(extra, "reopen")


async def _delete_action(event, extra: str, chat_id: int):
    """Delete a todo for real (durable row removal), then show the list.

    Deletion is not a status transition (the service removes the row, exactly
    as ``task_delete`` does), so it reports the deletion outcome instead of
    reusing the shared CAS mutation helper.
    """
    from backend.ai.database.task_repository import DELETION_STALE

    parsed = _parse_action_extra(extra)
    if parsed is None:
        return "Todo", "× Invalid action arguments.", []
    task_id, version = parsed
    service = _service(_owner())
    try:
        result = await service.delete(task_id, version)
    except Exception:
        logger.exception("Todo delete failed for todo %s", task_id)
        return f"Todo #{task_id}", "× Operation failed; no change was confirmed.", []
    if not result.deleted:
        if result.fallback_backend:
            from backend.ai.database.task_repository import FALLBACK_REASON_LOCAL_RESOURCE

            if getattr(result, "fallback_reason", "") == FALLBACK_REASON_LOCAL_RESOURCE:
                return (
                    f"Todo #{task_id}",
                    "× Local resource error; the durable store was not reached, "
                    "so nothing durable was deleted.",
                    [],
                )
            return f"Todo #{task_id}", "× Supabase unavailable; nothing durable was deleted.", []
        if result.outcome == DELETION_STALE:
            return f"Todo #{task_id}", "× Version is stale; nothing was deleted.", []
        return f"Todo #{task_id}", "× Todo not found or ownership check failed.", []
    notice = (
        f"✓ Deleted todo #{task_id}\n\n"
        if result.durable
        else f"✓ Deleted todo #{task_id} (memory only — not durable)\n\n"
    )
    refreshed = await _todo_list_panel(None, "")
    if refreshed is None:
        return "Todo", notice.rstrip(), []
    title, body, buttons = refreshed
    return title, notice + body, buttons


_STEP_VERB_PAST = {"complete_step": "completed", "reopen_step": "reopened"}


def _step_title_of(step) -> str:
    return " ".join(str(getattr(step, "title", "") or "").split()) or "Untitled"


async def _step_mutate(extra: str, verb: str) -> tuple[str, str, list] | None:
    """Shared CAS step mutation → refreshed step panel (nothing on a stale one)."""
    parsed = _parse_action_extra(extra)
    if parsed is None:
        return "Steps", "× Invalid action arguments.", []
    step_id, version = parsed
    service = _service(_owner())
    try:
        step = await getattr(service, verb)(step_id, version)
    except ValueError as exc:
        # A refusal the owner must read (a step of a completed todo cannot be
        # reopened before the todo itself is reopened).
        return "Steps", f"× {exc}", []
    except Exception:
        logger.exception("Todo step %s failed for step %s", verb, step_id)
        return "Steps", "× Operation failed; no change was confirmed.", []
    if step is None:
        return (
            "Steps",
            "× Step not found, or it changed since this panel was drawn "
            "(version is stale). Nothing was changed.",
            [],
        )
    notice = (
        f"✓ Step {_STEP_VERB_PAST.get(verb, verb)} — {_step_title_of(step)}\n\n"
    )
    result = await _todo_steps_panel(None, str(int(step.task_id)))
    if result is None:
        return "Steps", notice.rstrip(), []
    title, body, buttons = result
    return title, notice + body, buttons


async def _step_complete_action(event, extra: str, chat_id: int):
    return await _step_mutate(extra, "complete_step")


async def _step_reopen_action(event, extra: str, chat_id: int):
    return await _step_mutate(extra, "reopen_step")


async def _step_delete_action(event, extra: str, chat_id: int):
    """Remove ONE step for real — the parent todo always survives it."""
    parsed = _parse_action_extra(extra)
    if parsed is None:
        return "Steps", "× Invalid action arguments.", []
    step_id, version = parsed
    service = _service(_owner())
    step = None
    try:
        step = await service.get_step(step_id)
    except Exception:
        logger.exception("Todo step read failed for step %s", step_id)
    task_id = int(getattr(step, "task_id", 0) or 0)
    try:
        removed = await service.delete_step(step_id, version)
    except Exception:
        logger.exception("Todo step delete failed for step %s", step_id)
        return "Steps", "× Operation failed; no change was confirmed.", []
    if not removed:
        current = await service.get_step(step_id)
        if current is not None:
            return "Steps", "× Version is stale; nothing was deleted.", []
        return "Steps", "× Step not found, so nothing was deleted.", []
    notice = "🗑 Step removed — the todo itself is untouched\n\n"
    if task_id <= 0:
        return "Steps", notice.rstrip(), []
    result = await _todo_steps_panel(None, str(task_id))
    if result is None:
        return "Steps", notice.rstrip(), []
    title, body, buttons = result
    return title, notice + body, buttons


async def _complete_all_action(event, extra: str, chat_id: int):
    """Complete a todo TOGETHER with the steps it still has.

    The explicit path for finishing a multi-step todo in one action: plain
    completion refuses while a step remains, and the service completes the
    remaining steps first so the pair can never disagree.
    """
    parsed = _parse_action_extra(extra)
    if parsed is None:
        return "Todo", "× Invalid action arguments.", []
    task_id, version = parsed
    service = _service(_owner())
    try:
        task = await service.complete_todo_with_steps(task_id, version)
    except ValueError as exc:
        return f"Todo #{task_id}", f"× {exc}", []
    except Exception:
        logger.exception("Todo complete-all failed for todo %s", task_id)
        return f"Todo #{task_id}", "× Operation failed; no change was confirmed.", []
    if task is None:
        return (
            f"Todo #{task_id}",
            "× Todo not found, or it changed since this panel was drawn "
            "(version is stale). Nothing was changed.",
            [],
        )
    notice = (
        f"✓ Todo #{task.id} completed with its remaining steps · v{task.version}\n\n"
    )
    result = await _todo_detail_panel(None, str(task_id))
    if result is None:
        return f"Todo #{task_id}", notice.rstrip(), []
    title, body, buttons = result
    return title, notice + body, buttons


async def _steps_add_input_handler(
    text: str, chat_id: int, msg_id: int, inline_chat_id: int, inline_msg_id: int
) -> None:
    """Append the typed titles as ordered steps — one per line, all or nothing."""
    from backend.helper.input_state import get_pending

    pending = get_pending(_owner()) or {}
    parts = str(pending.get("extra") or "").split(":")
    task_id = 0
    for part in parts:
        try:
            task_id = int(part)
            break
        except (TypeError, ValueError):
            continue
    titles = [" ".join(line.split()) for line in str(text or "").splitlines()]
    titles = [title for title in titles if title]
    notice = ""
    if task_id <= 0:
        notice = "× This step input expired; open the todo again."
    elif not titles:
        notice = "× A step needs a title."
    else:
        try:
            added = await _service(_owner()).add_steps(task_id, titles)
        except ValueError as exc:
            notice = f"× {exc}"
        except Exception:
            logger.exception("Todo steps add failed for todo %s", task_id)
            notice = "× Steps were not added; no change was confirmed."
        else:
            if added is None:
                notice = "× That todo was not found."
            elif len(added) == 1:
                notice = f"✓ Step added — {_step_title_of(added[0])}"
            else:
                notice = f"✓ {len(added)} steps added"
    await _finish_input(
        notice,
        chat_id,
        msg_id,
        inline_chat_id,
        inline_msg_id,
        panel_id=_STEPS_PANEL,
        extra=str(task_id) if task_id > 0 else "",
    )


async def _steps_edit_input_handler(
    text: str, chat_id: int, msg_id: int, inline_chat_id: int, inline_msg_id: int
) -> None:
    """Rename ONE step under the version that opened this input (CAS).

    The todo's own title is never touched by this input: only the step row it
    was opened for changes.
    """
    from backend.helper.input_state import get_pending

    pending = get_pending(_owner()) or {}
    raw_extra = str(pending.get("extra") or "")
    if raw_extra.startswith("edit:"):
        raw_extra = raw_extra[len("edit:"):]
    parsed = _parse_action_extra(raw_extra)
    title = " ".join(str(text or "").split())
    step = None
    if parsed is None:
        notice = "× This step edit expired; open the step again."
    elif not title:
        notice = "× A step needs a title."
    else:
        step_id, version = parsed
        try:
            step = await _service(_owner()).rename_step(step_id, version, title)
        except ValueError as exc:
            notice = f"× {exc}"
        except Exception:
            logger.exception("Todo step edit failed for step %s", step_id)
            notice = "× Step was not changed; no change was confirmed."
        else:
            notice = (
                f"✓ Step is now — {_step_title_of(step)}"
                if step is not None
                else "× Step changed since this panel was drawn; open it again and retry."
            )
    await _finish_input(
        notice,
        chat_id,
        msg_id,
        inline_chat_id,
        inline_msg_id,
        panel_id=_STEPS_PANEL,
        extra=str(int(getattr(step, "task_id", 0) or 0)) if step is not None else "",
    )


async def _finish_input(
    notice: str,
    chat_id: int,
    msg_id: int,
    inline_chat_id: int,
    inline_msg_id: int,
    *,
    panel_id: str,
    extra: str = "",
) -> None:
    """Close one input: re-render the panel the input belongs to.

    Mirrors the existing Taskloom input closing: the helper bot edits the
    inline message when it exists, and the self client deletes the owner's
    typed message so the panel is the only thing left on screen.
    """
    from backend.helper import get_panel
    from backend.helper.client import get_client
    from backend.helper.inline_engine import _self_client
    from backend.helper.panel_render import render_edit

    if inline_chat_id and inline_msg_id:
        editor = get_client() or _self_client
        if editor is not None:
            try:
                handler = get_panel(panel_id)
                result = await handler(None, extra) if handler else None
                if result is None:
                    raise RuntimeError("panel unavailable")
                title, body, buttons = result
                text, built = render_edit(title, f"{notice}\n\n{body}", buttons)
                await editor.edit_message(inline_chat_id, inline_msg_id, text, buttons=built)
            except Exception:
                try:
                    await editor.edit_message(inline_chat_id, inline_msg_id, notice)
                except Exception:
                    pass
    if _self_client:
        try:
            await _self_client.delete_messages(chat_id, [msg_id])
        except Exception:
            pass


async def _add_input_handler(text: str, chat_id: int, msg_id: int, inline_chat_id: int, inline_msg_id: int) -> None:
    """Create ONE todo from the owner's typed title (never invented)."""
    from backend.ai.task_creation import TaskCreationError, TaskCreationService
    from backend.ai.database.manager import get_repository_manager

    title = " ".join(str(text or "").split())
    notice = ""
    if not title:
        notice = "× A todo needs a title."
    else:
        try:
            task = await TaskCreationService(
                get_repository_manager().task, _owner()
            ).create_todo(title, _DEFAULT_TZ, datetime.now(timezone.utc))
            notice = f"✓ Todo #{task.id} added"
        except TaskCreationError as exc:
            notice = f"× {exc}"
        except Exception:
            logger.exception("Todo creation failed")
            notice = "× Todo was not added; no change was confirmed."
    await _finish_input(
        notice, chat_id, msg_id, inline_chat_id, inline_msg_id, panel_id=_LIST_PANEL
    )


async def _edit_input_handler(text: str, chat_id: int, msg_id: int, inline_chat_id: int, inline_msg_id: int) -> None:
    """Rename ONE todo under the version that opened this input (CAS)."""
    from backend.helper.input_state import get_pending

    # The pending input's extra carries the id and the version the panel that
    # opened it was drawn with, so this rename is a real CAS update and a todo
    # that changed meanwhile is refused instead of overwritten.
    pending = get_pending(_owner()) or {}
    parsed = _parse_action_extra(str(pending.get("extra") or ""))
    title = " ".join(str(text or "").split())
    if parsed is None:
        notice = "× This edit expired; open the todo again."
    elif not title:
        notice = "× A todo needs a title."
    else:
        task_id, version = parsed
        try:
            task = await _service(_owner()).edit_todo_title(task_id, version, title)
        except ValueError as exc:
            notice = f"× {exc}"
        except Exception:
            logger.exception("Todo edit failed for todo %s", task_id)
            notice = "× Todo was not changed; no change was confirmed."
        else:
            notice = (
                f"✓ Todo #{task.id} is now — {task.label}"
                if task is not None
                else "× Todo changed since this panel was drawn; reopen it and retry."
            )
    await _finish_input(
        notice,
        chat_id,
        msg_id,
        inline_chat_id,
        inline_msg_id,
        panel_id=_DETAIL_PANEL,
        extra=str(parsed[0]) if parsed is not None else "",
    )


_INPUT_PROMPTS = {
    "new": "**New todo**\n\nSend the todo's title.\n\n_Reply below._",
    "edit": "**New title**\n\nSend what this todo should say instead.\n\n_Reply below._",
}

# The step inputs have their own prompts: adding a step accepts SEVERAL titles
# at once (one per line, appended in that order, all or nothing), while the
# edit renames exactly one step.
_STEPS_INPUT_PROMPTS = {
    "add": (
        "**New step(s)**\n\nSend the step's title — one step per line to add "
        "several at once, in order.\n\n_Reply below._"
    ),
    "edit": "**New step title**\n\nSend what this step should say instead.\n\n_Reply below._",
}


def register(client, owner_id: int, tz_str: str) -> None:
    global _DEFAULT_TZ
    _DEFAULT_TZ = str(tz_str or "").strip() or "UTC"
    try:
        register_panel(_LIST_PANEL, _todo_list_panel, parent="menu", title="📋 Todo")
        register_inline_builder(_LIST_PANEL, _todo_list_inline_builder)
        register_panel(_DONE_PANEL, _todo_done_panel, parent=_LIST_PANEL, title="✅ Completed todos")
        register_inline_builder(_DONE_PANEL, _todo_done_inline_builder)
        register_panel(_DETAIL_PANEL, _todo_detail_panel, parent=_LIST_PANEL, title="Todo")
        register_inline_builder(_DETAIL_PANEL, _todo_detail_inline_builder)
        register_panel(_STEPS_PANEL, _todo_steps_panel, parent=_DETAIL_PANEL, title="Steps")
        register_inline_builder(_STEPS_PANEL, _todo_steps_inline_builder)
        register_action("todo_complete", _complete_action)
        register_action("todo_reopen", _reopen_action)
        register_action("todo_delete", _delete_action)
        register_action("todo_complete_all", _complete_all_action)
        register_action("todo_step_complete", _step_complete_action)
        register_action("todo_step_reopen", _step_reopen_action)
        register_action("todo_step_delete", _step_delete_action)
        register_input(_LIST_PANEL, "new", {
            "handler": _add_input_handler,
            "prompt": _INPUT_PROMPTS["new"],
            "extra_rows": (("✕ Cancel", f"panel:{_LIST_PANEL}"),),
        })
        register_input(_DETAIL_PANEL, "edit", {
            "handler": _edit_input_handler,
            "prompt": _INPUT_PROMPTS["edit"],
            "extra_rows": (("✕ Cancel", f"panel:{_LIST_PANEL}"),),
        })
        register_input(_STEPS_PANEL, "add", {
            "handler": _steps_add_input_handler,
            "prompt": _STEPS_INPUT_PROMPTS["add"],
            "extra_rows": (("✕ Cancel", f"panel:{_LIST_PANEL}"),),
        })
        register_input(_STEPS_PANEL, "edit", {
            "handler": _steps_edit_input_handler,
            "prompt": _STEPS_INPUT_PROMPTS["edit"],
            "extra_rows": (("✕ Cancel", f"panel:{_LIST_PANEL}"),),
        })
        logger.info("Todo panels registered OK")
    except Exception as exc:
        logger.error("Todo registration FAILED: %s", exc)
