"""
Taskloom — Telegram-native Task management interface (AI section).

A dedicated visual surface over the EXISTING durable task system:

    Taskloom panels (this file, Glass UI only)
      -> TaskManagementService      (backend/ai/task_management.py)
        -> TaskRepository           (owner-scoped, CAS transitions)
          -> durable ai_tasks / ai_task_occurrences

The UI never touches the database or Telegram directly: every read goes
through TaskManagementService (list/inspect), every mutation goes through
the service's CAS-guarded operations (pause/resume/complete) and its real
deletion (delete removes the durable task row). No second scheduler, no
second executor, no persistence layer.

Entry point: the AI panel button "Taskloom" -> panel:taskloom.

The "＋ New task" wizard is a CREATION UX over the SAME boundary: structured
choices are mapped to the same TaskCandidate the natural-language path
produces (``backend.ai.task_wizard``) and persisted through the SAME
TaskCreationService -> TaskRepository. No second scheduler, executor, or
persistence path.
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
from backend.ai import task_wizard
from backend.ai.task_wizard import (
    ACTION_DEFINITIONS,
    ACTION_ORDER,
    AI_MODE,
    LANGUAGE_NAMES,
    SCHEDULE_BUTTONS,
    STATIC_MODE,
    STEP_ACTION,
    STEP_CONTENT,
    STEP_DETAILS,
    STEP_REVIEW,
    STEP_SCHEDULE,
    WEEKDAYS,
    TaskDraft,
    TaskWizardError,
)

logger = logging.getLogger(__name__)

_MAX_LIST_ROWS = 4
_MAX_INSPECT_OCC = 8

# In-progress wizard drafts, keyed by owner. Per-owner UI state only — the
# draft carries no execution authority and is persisted nowhere until the
# owner confirms the review, at which point it becomes the SAME task
# candidate contract the natural-language path produces.
_drafts: dict[int, TaskDraft] = {}
_DEFAULT_TZ = "UTC"
_WIZARD_PANEL = "taskloom_new"

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
    builder.add_row("＋ New task", f"panel:{_WIZARD_PANEL}")

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
    """Delete a task for real (durable row removal), then show the list.

    Deletion is not a status transition, so it cannot reuse the shared
    ``_mutate`` verb helper: the service returns a deletion outcome that
    distinguishes a real removal from a missing task, a stale version, and a
    degraded (non-durable) deletion.
    """
    from backend.helper.inline_engine import _owner_id
    from backend.ai.database.task_repository import DELETION_STALE

    parsed = _parse_action_extra(extra)
    if parsed is None:
        return "Taskloom", "× Invalid action arguments.", []
    task_id, version = parsed
    service = _service(_owner_id)
    try:
        result = await service.delete(task_id, version)
    except Exception:
        logger.exception("Taskloom delete failed for task %s", task_id)
        return f"Task #{task_id}", "× Operation failed; no change was confirmed.", []
    if not result.deleted:
        if result.fallback_backend:
            from backend.ai.database.task_repository import FALLBACK_REASON_LOCAL_RESOURCE

            if getattr(result, "fallback_reason", "") == FALLBACK_REASON_LOCAL_RESOURCE:
                return (
                    f"Task #{task_id}",
                    "× Local resource error; the durable store was not reached, "
                    "so nothing durable was deleted.",
                    [],
                )
            return f"Task #{task_id}", "× Supabase unavailable; nothing durable was deleted.", []
        if result.outcome == DELETION_STALE:
            return f"Task #{task_id}", "× Version is stale; nothing was deleted.", []
        return f"Task #{task_id}", "× Task not found or ownership check failed.", []
    notice = (
        f"✓ Deleted task #{task_id}\n\n"
        if result.durable
        else f"✓ Deleted task #{task_id} (memory only — not durable)\n\n"
    )
    refreshed = await _taskloom_panel(event, "")
    if refreshed is None:
        return "Taskloom", notice.rstrip(), []
    title, body, buttons = refreshed
    return title, notice + body, buttons


# ── Task creation wizard (deterministic candidate -> TaskCreationService) ──


def _owner(owner_id: int | None = None) -> int:
    if owner_id is not None:
        return owner_id
    from backend.helper.inline_engine import _owner_id
    return _owner_id


def _draft(owner_id: int | None = None) -> TaskDraft:
    owner = _owner(owner_id)
    draft = _drafts.get(owner)
    if draft is None:
        draft = TaskDraft(timezone=_DEFAULT_TZ, step=STEP_ACTION)
        _drafts[owner] = draft
    return draft


def _store(draft: TaskDraft, owner_id: int | None = None) -> None:
    _drafts[_owner(owner_id)] = draft


def _step_position(step: str) -> tuple[int, str]:
    order = {
        STEP_ACTION: (1, "Action"),
        STEP_CONTENT: (2, "Content"),
        STEP_DETAILS: (3, "Content details"),
        STEP_SCHEDULE: (4, "Schedule"),
        STEP_REVIEW: (4, "Review"),
    }
    index, label = order.get(step, (1, "Action"))
    return index, label


def _wizard_render(draft: TaskDraft, owner_id: int | None = None) -> tuple[str, str, list]:
    """Render the current wizard step. Every value shown is derived from the
    wizard draft / the candidate it produces — never invented here."""
    step = draft.step or STEP_ACTION
    index, label = _step_position(step)
    lines = [f"**Step {index}/4 · {label}**", ""]
    if draft.notice:
        lines.insert(0, draft.notice)
        lines.insert(1, "")
    builder = InlinePanelBuilder()

    if step == STEP_ACTION:
        lines.append("What should this task do?")
        for key in ACTION_ORDER:
            definition = ACTION_DEFINITIONS[key]
            builder.add_row(definition.button, f"action:taskloom_wizard:set:action:{key}")
        builder.add_row("✕ Cancel", "panel:taskloom")
    elif step == STEP_CONTENT:
        lines.append("How should the content be produced each run?")
        builder.add_row("✨ AI-generated (fresh each run)", "action:taskloom_wizard:set:mode:ai")
        builder.add_row("✍ Static text (same every run)", "action:taskloom_wizard:set:mode:static")
        builder.add_row("← Back", "action:taskloom_wizard:step:action")
    elif step == STEP_DETAILS:
        definition = ACTION_DEFINITIONS.get(draft.action)
        if definition is not None and draft.content_mode == AI_MODE:
            lines.append(f"{definition.title} · generated content. Optional constraints;")
            lines.append("anything left unset imposes no constraint.")
            lines.append("")
            lines.append(f"Source: {draft.source or 'Any'}")
            lines.append(f"Language: {task_wizard.LANGUAGE_WORDS.get(draft.language) or 'Any'}")
            lines.append(
                f"Maximum length: {f'at most {draft.max_length}' if draft.max_length else 'Any'}"
            )
            if draft.source:
                lines.append(f"Speaker label: {'Hidden' if draft.hide_speaker_label else 'Shown'}")
            builder.add_row("Character / source…", f"input:{_WIZARD_PANEL}:source")
            if draft.source:
                builder.add_row("Clear source", "action:taskloom_wizard:set:source:none")
            for code, word in LANGUAGE_NAMES:
                builder.add_row(f"Language: {word}", f"action:taskloom_wizard:set:lang:{code}")
            builder.add_row("Language: any", "action:taskloom_wizard:set:lang:none")
            builder.add_row("Maximum length…", f"input:{_WIZARD_PANEL}:maxlen")
            if draft.max_length:
                builder.add_row("Clear maximum length", "action:taskloom_wizard:set:maxlen:none")
            if draft.source:
                builder.add_row(
                    "Speaker label: " + ("Show" if draft.hide_speaker_label else "Hide"),
                    "action:taskloom_wizard:set:label:"
                    + ("0" if draft.hide_speaker_label else "1"),
                )
        else:
            lines.append("Text to use every run:")
            lines.append(f"`{draft.text.strip() or '— not set —'}`")
            builder.add_row("Enter text…", f"input:{_WIZARD_PANEL}:text")
        builder.add_row("Next: schedule →", f"action:taskloom_wizard:step:{STEP_SCHEDULE}")
        back_step = (
            STEP_CONTENT
            if (definition is not None and definition.supports_ai and draft.content_mode)
            else STEP_ACTION
        )
        builder.add_row("← Back", f"action:taskloom_wizard:step:{back_step}")
    elif step == STEP_SCHEDULE:
        lines.append("Scheduling is required.")
        lines.append("")
        lines.append(f"Schedule: {_schedule_line(draft)}")
        lines.append(f"Timezone: {draft.timezone}")
        for kind, button in SCHEDULE_BUTTONS:
            builder.add_row(button, f"input:{_WIZARD_PANEL}:{kind}")
        builder.add_row("Timezone…", f"input:{_WIZARD_PANEL}:tz")
        problem = task_wizard.missing_requirement(draft, reference=datetime.now(timezone.utc))
        if problem is None:
            builder.add_row("Review →", f"action:taskloom_wizard:step:{STEP_REVIEW}")
        else:
            lines.append("")
            lines.append(f"_Cannot continue yet: {problem}_")
        builder.add_row("← Back", f"action:taskloom_wizard:step:{STEP_DETAILS}")
    elif step == STEP_REVIEW:
        lines.append("Create this task?")
        lines.append("")
        try:
            rows = task_wizard.review_lines(draft, reference=datetime.now(timezone.utc))
        except TaskWizardError as exc:
            lines.append(f"_Incomplete: {exc}_")
            rows = []
        for key, value in rows:
            lines.append(f"**{key}:** {value}")
        if rows:
            builder.add_row("✔ Create task", "action:taskloom_wizard:create")
        builder.add_row("← Back", f"action:taskloom_wizard:step:{STEP_SCHEDULE}")
        builder.add_row("✕ Cancel", "panel:taskloom")

    _nav(builder)
    return "＋ New task", "\n".join(lines), builder.build()


def _schedule_line(draft: TaskDraft) -> str:
    kind = draft.schedule_type
    if kind == "interval" and draft.interval_minutes:
        return task_wizard.schedule_summary("interval", {"seconds": draft.interval_minutes * 60})
    if kind == "daily" and draft.clock:
        try:
            hour, minute = task_wizard.parse_clock(draft.clock)
        except TaskWizardError:
            return "_not set_"
        return task_wizard.schedule_summary("daily", {"hour": hour, "minute": minute})
    if kind == "weekly" and draft.clock and draft.weekday is not None:
        try:
            hour, minute = task_wizard.parse_clock(draft.clock)
        except TaskWizardError:
            return "_not set_"
        return task_wizard.schedule_summary(
            "weekly", {"weekday": draft.weekday, "hour": hour, "minute": minute}
        )
    if kind == "once" and draft.once_at:
        return task_wizard.schedule_summary("once", {"at": draft.once_at})
    return "_not set_"


async def _wizard_panel(event, extra: str) -> tuple[str, str, list] | None:
    return _wizard_render(_draft())


async def _wizard_inline_builder(event, extra: str) -> list:
    title, body, buttons = _wizard_render(_draft())
    return [render(title, body, buttons)]


def _wizard_set_action(draft: TaskDraft, key: str) -> TaskDraft:
    definition = ACTION_DEFINITIONS.get(key)
    if definition is None:
        return draft.updated(notice="× Unknown action.")
    return draft.updated(
        action=key,
        content_mode="" if definition.supports_ai else STATIC_MODE,
        step=STEP_CONTENT if definition.supports_ai else STEP_DETAILS,
        source="", language="", max_length=None, hide_speaker_label=False,
        text="", notice="",
    )


def _wizard_apply(draft: TaskDraft, field: str, raw: str) -> TaskDraft:
    """Apply one wizard field deterministically; reject unrepresentable values."""
    try:
        if field == "action":
            return _wizard_set_action(draft, raw)
        if field == "mode":
            if raw not in (AI_MODE, STATIC_MODE):
                raise TaskWizardError("choose a content mode")
            definition = ACTION_DEFINITIONS.get(draft.action)
            if raw == AI_MODE and (definition is None or not definition.supports_ai):
                raise TaskWizardError("this action requires static text")
            return draft.updated(content_mode=raw, step=STEP_DETAILS, notice="")
        if field == "lang":
            code = "" if raw in ("none", "any", "") else raw
            if code and code not in task_wizard.LANGUAGE_POLICY:
                raise TaskWizardError("unknown language")
            trial = draft.updated(language=code)
            problem = task_wizard.instruction_problem(trial)
            if problem:
                raise TaskWizardError(problem)
            return trial.updated(notice="")
        if field == "maxlen":
            value = None if raw in ("none", "") else task_wizard.parse_max_length(raw)
            trial = draft.updated(max_length=value)
            problem = task_wizard.instruction_problem(trial)
            if problem:
                raise TaskWizardError(problem)
            return trial.updated(notice="")
        if field == "label":
            trial = draft.updated(hide_speaker_label=raw == "1")
            problem = task_wizard.instruction_problem(trial)
            if problem:
                raise TaskWizardError(problem)
            return trial.updated(notice="")
        if field == "source":
            return draft.updated(source="", hide_speaker_label=False, notice="")
        raise TaskWizardError("unknown setting")
    except TaskWizardError as exc:
        return draft.updated(notice=f"× {exc}")


async def _wizard_create(draft: TaskDraft, chat_id: int) -> tuple[str, str, list]:
    owner = _owner()
    now = datetime.now(timezone.utc)
    problem = task_wizard.missing_requirement(draft, chat_id, now)
    if problem is not None:
        return _wizard_render(draft.updated(step=STEP_REVIEW, notice=f"× {problem}"))
    try:
        from backend.ai.task_candidate import TaskCandidate
        from backend.ai.task_creation import TaskCreationService
        from backend.ai.database.manager import get_repository_manager

        candidate = task_wizard.build_candidate(draft, chat_id, now)
        validated = TaskCandidate.from_untrusted(candidate)
        service = TaskCreationService(get_repository_manager().task, owner)
        task = await service.create(validated, now)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Taskloom wizard creation failed")
        return _wizard_render(
            draft.updated(step=STEP_REVIEW, notice=f"× Creation failed ({type(exc).__name__}); nothing durable was created.")
        )
    _drafts.pop(owner, None)
    note = f"✓ Task #{task.id} created — {task.label}"
    fallback_backend = getattr(task, "fallback_backend", None)
    if fallback_backend:
        from backend.ai.task_management_interface import fallback_note
        note += f" (memory only — not durable)\n\n{fallback_note(str(getattr(task, 'fallback_reason', '') or ''))}"
    refreshed = await _taskloom_panel(None, "")
    if refreshed is None:
        return "Taskloom", note, []
    title, body, buttons = refreshed
    return title, f"{note}\n\n{body}", buttons


async def _wizard_action(event, extra: str, chat_id: int):
    """One action for every wizard mutation (set/step/create/cancel)."""
    verb, _, value = (extra or "").partition(":")
    draft = _draft()
    if verb == "set":
        field, _, raw = value.partition(":")
        draft = _wizard_apply(draft, field, raw)
        _store(draft)
        return _wizard_render(draft)
    if verb == "step":
        draft = draft.updated(step=value, notice="")
        _store(draft)
        return _wizard_render(draft)
    if verb == "create":
        return await _wizard_create(draft, chat_id)
    return _wizard_render(draft)


async def _wizard_finish(notice: str, chat_id: int, msg_id: int, inline_chat_id: int, inline_msg_id: int) -> None:
    """Close one wizard input with a single edit: notice + refreshed wizard."""
    from backend.helper.client import get_client
    from backend.helper.inline_engine import _self_client
    from backend.helper.panel_render import render_edit

    helper = get_client()
    if helper and inline_chat_id and inline_msg_id:
        try:
            title, body, buttons = _wizard_render(_draft())
            text, built = render_edit(title, f"{notice}\n\n{body}", buttons)
            await helper.edit_message(inline_chat_id, inline_msg_id, text, buttons=built)
        except Exception:
            try:
                await helper.edit_message(inline_chat_id, inline_msg_id, notice)
            except Exception:
                pass
    if _self_client:
        try:
            await _self_client.delete_messages(chat_id, [msg_id])
        except Exception:
            pass


def _wizard_input_handler(field: str):
    """Build the input handler for one bounded wizard field."""

    async def _handler(text: str, chat_id: int, msg_id: int, inline_chat_id: int, inline_msg_id: int) -> None:
        draft = _draft()
        notice = ""
        try:
            if field == "source":
                source = task_wizard.clean_source(text)
                trial = draft.updated(source=source, hide_speaker_label=False)
                problem = task_wizard.instruction_problem(trial)
                if problem:
                    raise TaskWizardError(problem)
                draft = trial
                notice = f"✓ Source: {source}" if source else "✓ Source cleared"
            elif field == "maxlen":
                value = task_wizard.parse_max_length(text)
                trial = draft.updated(max_length=value)
                problem = task_wizard.instruction_problem(trial)
                if problem:
                    raise TaskWizardError(problem)
                draft = trial
                notice = f"✓ Maximum length: at most {value} characters" if value else "✓ Maximum length cleared"
            elif field == "text":
                value = text.strip()
                if not value:
                    raise TaskWizardError("text cannot be empty")
                if len(value) > task_wizard.MAX_TEXT_CHARS:
                    raise TaskWizardError("text is too long")
                draft = draft.updated(text=value)
                notice = "✓ Text saved"
            elif field == "interval":
                minutes = task_wizard.parse_interval_minutes(text)
                draft = draft.updated(schedule_type="interval", interval_minutes=minutes)
                notice = f"✓ Every {minutes} minutes"
            elif field == "daily":
                hour, minute = task_wizard.parse_clock(text)
                draft = draft.updated(schedule_type="daily", clock=f"{hour:02d}:{minute:02d}")
                notice = f"✓ Daily at {hour:02d}:{minute:02d}"
            elif field == "weekly":
                weekday, clock = _parse_weekly(text)
                draft = draft.updated(schedule_type="weekly", weekday=weekday, clock=clock)
                notice = f"✓ Weekly on {WEEKDAYS[weekday]} at {clock}"
            elif field == "once":
                at = task_wizard.parse_once_at(text)
                draft = draft.updated(schedule_type="once", once_at=at)
                notice = f"✓ Once at {at.replace('T', ' ')[:16]}"
            elif field == "tz":
                zone = " ".join((text or "").split())
                if not task_wizard.valid_timezone(zone):
                    raise TaskWizardError("enter a valid IANA timezone like Asia/Tehran")
                draft = draft.updated(timezone=zone)
                notice = f"✓ Timezone: {zone}"
            else:
                raise TaskWizardError("unknown input")
        except TaskWizardError as exc:
            notice = f"× {exc}"
        draft = draft.updated(notice=notice)
        _store(draft)
        await _wizard_finish(notice, chat_id, msg_id, inline_chat_id, inline_msg_id)

    return _handler


def _parse_weekly(text: str) -> tuple[int, str]:
    raw = " ".join((text or "").split())
    if not raw:
        raise TaskWizardError("enter a weekday and time, e.g. Monday 09:00")
    head, _, tail = raw.partition(" ")
    weekday: int | None = None
    for index, name in enumerate(WEEKDAYS):
        if head.casefold() == name.casefold():
            weekday = index
            break
    if weekday is None and head.isdigit() and 0 <= int(head) <= 6:
        weekday = int(head)
    if weekday is None:
        raise TaskWizardError("enter a weekday name (Monday–Sunday) followed by a time")
    hour, minute = task_wizard.parse_clock(tail)
    return weekday, f"{hour:02d}:{minute:02d}"


_INPUT_PROMPTS = {
    "source": "**Character / source**\n\nSend the exact name, or `none` to clear.\n\n_Reply below._",
    "maxlen": "**Maximum length**\n\nSend a whole number of characters (inclusive maximum), or `none` to clear.\n\n_Reply below._",
    "text": "**Content text**\n\nSend the exact text this task should use.\n\n_Reply below._",
    "interval": "**Interval**\n\nSend the number of minutes between runs (e.g. `2`).\n\n_Reply below._",
    "daily": "**Daily time**\n\nSend the local time as HH:MM (e.g. `09:30`).\n\n_Reply below._",
    "weekly": "**Weekly time**\n\nSend a weekday and time (e.g. `Monday 09:30`).\n\n_Reply below._",
    "once": "**Start time**\n\nSend the local start as YYYY-MM-DD HH:MM (e.g. `2026-09-20 09:30`).\n\n_Reply below._",
    "tz": "**Timezone**\n\nSend an IANA timezone (e.g. `Asia/Tehran`).\n\n_Reply below._",
}


def register(client, owner_id: int, tz_str: str) -> None:
    global _DEFAULT_TZ
    _DEFAULT_TZ = tz_str if task_wizard.valid_timezone(tz_str or "") else "UTC"
    try:
        register_panel("taskloom", _taskloom_panel, parent="ai", title="▦ Taskloom")
        register_inline_builder("taskloom", _taskloom_inline_builder)
        register_panel("taskloom_task", _task_detail_panel, parent="taskloom", title="Task")
        register_inline_builder("taskloom_task", _task_detail_inline_builder)
        register_action("taskloom_pause", _pause_action)
        register_action("taskloom_resume", _resume_action)
        register_action("taskloom_complete", _complete_action)
        register_action("taskloom_delete", _delete_action)
        register_panel(_WIZARD_PANEL, _wizard_panel, parent="taskloom", title="＋ New task")
        register_inline_builder(_WIZARD_PANEL, _wizard_inline_builder)
        register_action("taskloom_wizard", _wizard_action)
        for field, prompt in _INPUT_PROMPTS.items():
            register_input(_WIZARD_PANEL, field, {
                "handler": _wizard_input_handler(field),
                "prompt": prompt,
            })
        logger.info("Taskloom panels registered OK")
    except Exception as exc:
        logger.error("Taskloom registration FAILED: %s", exc)
