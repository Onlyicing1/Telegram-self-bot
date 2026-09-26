"""Ordered step tools — the multi-step Todo surface on the EXISTING foundation.

A step belongs to exactly ONE todo (``ai_tasks`` row with ``schedule_type='todo'``)
and is ordered inside it. Nothing here introduces a second store, a second
lifecycle or a second resolver:

    parent todo   -> ``resolve_todo_argument``  (the Part 1 deterministic resolver)
    step          -> ``TaskManagementService.resolve_step`` (0/1/N, same tiers)
    every mutation -> ``TaskManagementService`` -> ``TaskRepository`` (owner-scoped, CAS)

The tools cover the whole step surface and nothing else:

    todo_step_add         append one or more steps, in order
    todo_step_list        the ordered steps with progress and the next step
    todo_step_transition  complete or reopen ONE step
    todo_step_edit        rename ONE step (never the todo)
    todo_step_delete      remove ONE step (never the todo)

Creating a todo TOGETHER with its steps is the existing ``todo_add`` (its
optional ``steps`` argument), so a multi-step request is one atomic operation
rather than a create followed by several appends. Completing a todo that still
has steps is ``task_transition`` (``complete_steps``), the only operation that
ever touches the parent and its steps together.
"""
from __future__ import annotations

from typing import Any

from backend.ai.tools.base import PermissionLevel, Tool, ToolResult
from backend.ai.tools.context import ToolContext
from backend.ai.tools.todo_tools import _present, resolve_todo_argument


def _todo_service(owner_id: int):
    from backend.ai.database.manager import get_repository_manager
    from backend.ai.task_management import TaskManagementService

    return TaskManagementService(get_repository_manager().task, owner_id)


def _step_candidate_data(target: Any) -> list[dict[str, Any]]:
    return [
        {
            "step_id": int(candidate.step_id),
            "step": int(candidate.ordinal),
            "title": str(candidate.title),
            "status": str(candidate.status),
            "version": int(candidate.version),
        }
        for candidate in getattr(target, "candidates", ())
    ]


def _fallback_note_for(service: Any, records: list[Any]) -> str:
    """The truthful degraded-store note for a write, or "" when durable.

    The repository annotates a record exactly when ITS write degraded; a
    repository-level ``fallback_active`` marker covers a write whose record
    carries no annotation. A degraded write is never presented as durable.
    """
    annotated = next(
        (r for r in records if str(getattr(r, "fallback_backend", "") or "")),
        None,
    )
    if annotated is None and not bool(
        getattr(getattr(service, "repository", None), "fallback_active", False)
    ):
        return ""
    from backend.ai.task_management_interface import fallback_note

    reason = str(getattr(annotated, "fallback_reason", "") or "") or str(
        getattr(getattr(service, "repository", None), "fallback_reason", "") or ""
    )
    return fallback_note(reason)


async def resolve_step_argument(
    owner_id: int, arguments: dict[str, Any]
) -> tuple[tuple[Any, Any, int] | None, ToolResult | None]:
    """Bind one step tool call to ONE owner-scoped step, or refuse it.

    Two deterministic resolutions happen, in this order, and neither of them
    ever guesses:

      1. the PARENT todo — by ``task_id`` or by the owner's own words
         (``query``), through the shared Part 1 resolver: 0 matches is an
         honest not-found, 2 or more is the candidate list;
      2. the STEP inside that todo — by its 1-based ORDINAL (``step``) or by a
         title reference (``step_query``), scoped to that parent only, so no
         step of another todo or another owner is ever in scope.

    Returns ``((task, step, ordinal), None)`` on success and ``(None, refusal)``
    otherwise. Nothing is written here.
    """
    task, failure = await resolve_todo_argument(
        owner_id,
        {"task_id": arguments.get("task_id"), "query": arguments.get("query")},
        require_version=False,
    )
    if failure is not None:
        return None, failure

    from backend.ai.task_management import STEP_TARGET_OK, format_step_resolution

    service = _todo_service(owner_id)
    try:
        target = await service.resolve_step(
            task.id, number=arguments.get("step"), query=arguments.get("step_query")
        )
    except Exception as exc:  # noqa: BLE001
        return None, ToolResult(success=False, message=f"Step lookup failed: {exc}")
    if target.status != STEP_TARGET_OK or target.step is None:
        return None, ToolResult(
            success=False,
            message=format_step_resolution(target),
            data={
                "outcome": str(target.status),
                "task_id": int(target.task_id or task.id),
                "candidates": _step_candidate_data(target),
            },
        )
    return (task, target.step, int(target.ordinal)), None


def _step_text(step: Any, ordinal: int) -> str:
    """One step rendered for a result message (title never truncated)."""
    title = " ".join(str(getattr(step, "title", "") or "").split()) or "Untitled"
    return f"{ordinal}. {title}"


class TodoStepAddTool(Tool):
    """Append one or more steps to ONE todo, in the order given."""

    def __init__(self, context: ToolContext) -> None:
        self._context = context

    @property
    def name(self) -> str:
        return "todo_step_add"

    @property
    def required_any_arguments(self) -> tuple[str, ...]:
        return ("title", "steps")

    @property
    def description(self) -> str:
        return (
            "Add one or more ordered steps to an existing Todo. Address the todo "
            "by its id (task_id) or by the owner's own words (query). Give the "
            "steps with 'steps' (a list) for several at once — they are appended "
            "in that order, all or nothing — or with 'title' for a single step. "
            "Step titles are the owner's own words and are never invented. The "
            "steps are appended at the end of the current list; existing step "
            "numbers never change."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "title": {
                "type": "string",
                "description": "The title of ONE step to append.",
            },
            "steps": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Several step titles to append, in order (an alternative to "
                    "'title')."
                ),
            },
            "task_id": {
                "type": "integer",
                "minimum": 1,
                "description": "The todo id (from todo_find or task_list).",
            },
            "query": {
                "type": "string",
                "description": (
                    "Instead of the id: the owner's own words for the todo; it "
                    "resolves only when exactly one todo matches."
                ),
            },
        }

    @property
    def permission_level(self) -> PermissionLevel:
        return PermissionLevel.READ_WRITE

    @property
    def safe(self) -> bool:
        return True

    @property
    def return_type(self) -> str:
        return "ToolResult with the added steps (id, number, title, version) in message and data"

    async def execute(self, context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
        owner_id = getattr(context, "owner_id", 0)
        if not isinstance(owner_id, int) or owner_id <= 0:
            return ToolResult(
                success=False, message="Owner identity is unavailable; no step was added."
            )
        titles: list[Any] = []
        if _present(arguments.get("title")):
            titles.append(arguments.get("title"))
        raw_steps = arguments.get("steps")
        if isinstance(raw_steps, (list, tuple)):
            titles.extend(raw_steps)
        elif raw_steps is not None:
            return ToolResult(
                success=False, message="'steps' must be a list of step titles."
            )
        if not titles:
            return ToolResult(
                success=False,
                message="Which step should I add? Send the step's title.",
            )

        task, failure = await resolve_todo_argument(
            owner_id,
            {"task_id": arguments.get("task_id"), "query": arguments.get("query")},
            require_version=False,
        )
        if failure is not None:
            return failure

        service = _todo_service(owner_id)
        try:
            added = await service.add_steps(task.id, titles)
        except ValueError as exc:
            return ToolResult(success=False, message=f"Steps were not added: {exc}")
        except Exception as exc:  # noqa: BLE001
            return ToolResult(success=False, message=f"Steps were not added: {exc}")
        if added is None:
            return ToolResult(
                success=False, message="That todo was not found. Nothing was changed."
            )

        first_ordinal = len(await service.list_steps(task.id) or []) - len(added) + 1
        entries = [
            {
                "step_id": int(step.id),
                "step": first_ordinal + index,
                "title": str(step.title),
                "status": str(step.status),
                "version": int(step.version),
            }
            for index, step in enumerate(added)
        ]
        listing = "\n".join(
            f"{entry['step']}. {entry['title']}" for entry in entries
        )
        title = " ".join(str(task.label).split())
        if len(entries) == 1:
            message = f"➕ Step {entries[0]['step']} added to «{title}» — {entries[0]['title']}"
        else:
            message = (
                f"➕ {len(entries)} steps added to «{title}»:\n{listing}"
            )
        note = _fallback_note_for(service, list(added))
        data: dict[str, Any] = {
            "task_id": int(task.id),
            "steps": entries,
            "durable": not note,
        }
        if note:
            message = f"{message}\n\n{note}"
        return ToolResult(success=True, message=message, data=data)


class TodoStepListTool(Tool):
    """List ONE todo's ordered steps with progress and the next step."""

    def __init__(self, context: ToolContext) -> None:
        self._context = context

    @property
    def name(self) -> str:
        return "todo_step_list"

    @property
    def description(self) -> str:
        return (
            "Show the ordered steps of ONE Todo: how many are completed, the "
            "numbered list (✓ completed, ○ remaining) and which step is next. "
            "Address the todo by its id (task_id) or by the owner's own words "
            "(query). Use the returned step numbers with "
            "todo_step_transition / todo_step_edit / todo_step_delete."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "task_id": {
                "type": "integer",
                "minimum": 1,
                "description": "The todo id (from todo_find or task_list).",
            },
            "query": {
                "type": "string",
                "description": (
                    "Instead of the id: the owner's own words for the todo; it "
                    "resolves only when exactly one todo matches."
                ),
            },
        }

    @property
    def permission_level(self) -> PermissionLevel:
        return PermissionLevel.READ_ONLY

    @property
    def safe(self) -> bool:
        return True

    @property
    def return_type(self) -> str:
        return "ToolResult with the steps block (progress, ordered steps, next step) in message and data"

    async def execute(self, context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
        from backend.ai.task_management import step_progress_of
        from backend.ai.task_management_interface import steps_block_text

        owner_id = getattr(context, "owner_id", 0)
        if not isinstance(owner_id, int) or owner_id <= 0:
            return ToolResult(success=False, message="Owner identity is unavailable.")
        task, failure = await resolve_todo_argument(
            owner_id,
            {"task_id": arguments.get("task_id"), "query": arguments.get("query")},
            require_version=False,
        )
        if failure is not None:
            return failure

        service = _todo_service(owner_id)
        try:
            steps = await service.list_steps(task.id)
        except Exception as exc:  # noqa: BLE001
            return ToolResult(success=False, message=f"Step list failed: {exc}")
        steps = list(steps or [])
        progress = step_progress_of(steps)
        message = steps_block_text(task, steps, progress)
        return ToolResult(
            success=True,
            message=message,
            data={
                "task_id": int(task.id),
                "title": str(task.label),
                "status": str(task.status),
                "version": int(task.version),
                "total": progress.total,
                "completed": progress.completed,
                "remaining": progress.remaining,
                "next_step": (
                    int(self._ordinal(steps, progress.next_step))
                    if progress.next_step is not None
                    else None
                ),
                "steps": [
                    {
                        "step_id": int(step.id),
                        "step": index,
                        "title": str(step.title),
                        "status": str(step.status),
                        "version": int(step.version),
                    }
                    for index, step in enumerate(steps, start=1)
                ],
            },
        )

    @staticmethod
    def _ordinal(steps: list, step: Any) -> int:
        from backend.ai.task_management_interface import step_ordinal

        return step_ordinal(steps, step)


class TodoStepTransitionTool(Tool):
    """Complete or reopen ONE step of ONE todo."""

    def __init__(self, context: ToolContext) -> None:
        self._context = context

    @property
    def name(self) -> str:
        return "todo_step_transition"

    @property
    def description(self) -> str:
        return (
            "Mark ONE step of a Todo completed (action 'completed') or return it "
            "to remaining (action 'active'). Address the todo by task_id or query, "
            "and the step by its number ('step', 1-based, as shown by "
            "todo_step_list) or by its own words ('step_query'). A step of a "
            "completed todo cannot be reopened before the todo itself is "
            "reopened. An ambiguous step reference is refused and the candidates "
            "are returned — never guessed."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "action": {
                "type": "string",
                "enum": ["completed", "active"],
                "description": (
                    "'completed' marks the step done; 'active' returns it to "
                    "remaining."
                ),
            },
            "step": {
                "type": "integer",
                "minimum": 1,
                "description": "The step number (1-based, from todo_step_list).",
            },
            "step_query": {
                "type": "string",
                "description": (
                    "Instead of the number: the step's own words; it resolves "
                    "only when exactly one step of that todo matches."
                ),
            },
            "task_id": {
                "type": "integer",
                "minimum": 1,
                "description": "The todo id (from todo_find or task_list).",
            },
            "query": {
                "type": "string",
                "description": (
                    "Instead of the todo id: the owner's own words for the todo."
                ),
            },
        }

    @property
    def permission_level(self) -> PermissionLevel:
        return PermissionLevel.READ_WRITE

    @property
    def safe(self) -> bool:
        return True

    @property
    def return_type(self) -> str:
        return "ToolResult with the step's new status and the todo's progress in message and data"

    async def execute(self, context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
        status = str(arguments.get("action") or "").strip().lower()
        if status not in ("completed", "active"):
            return ToolResult(
                success=False,
                message="Use action 'completed' to finish a step or 'active' to reopen it.",
            )
        binding, failure = await resolve_step_argument(
            getattr(context, "owner_id", 0), arguments
        )
        if failure is not None:
            return failure
        task, step, ordinal = binding

        service = _todo_service(context.owner_id)
        try:
            updated = await service.set_step_status(step.id, status, int(step.version))
        except ValueError as exc:
            return ToolResult(success=False, message=f"The step was not changed: {exc}")
        except Exception as exc:  # noqa: BLE001
            return ToolResult(success=False, message=f"Step transition failed: {exc}")
        if updated is None:
            current = await service.get_step(step.id)
            if current is not None:
                return ToolResult(
                    success=False,
                    message=(
                        f"Step {ordinal} was not changed: version {step.version} is "
                        f"stale (it is now v{current.version}). Retry."
                    ),
                    data={"task_id": int(task.id), "current_version": int(current.version)},
                )
            return ToolResult(
                success=False,
                message=f"Step {ordinal} was not found. Nothing was changed.",
            )

        progress = await service.step_progress(task.id)
        verb = "completed" if status == "completed" else "reopened"
        mark = "✓" if status == "completed" else "↩️"
        message = (
            f"{mark} Step {ordinal} {verb} — {_step_text(updated, ordinal)}"
        )
        if progress is not None:
            message = f"{message}\n{progress.label}"
            if progress.remaining and progress.next_step is not None:
                next_title = " ".join(str(progress.next_step.title).split())
                message = f"{message}\nNext: {next_title}"
        data: dict[str, Any] = {
            "task_id": int(task.id),
            "step_id": int(updated.id),
            "step": int(ordinal),
            "status": str(updated.status),
            "version": int(updated.version),
            "completed": int(progress.completed) if progress is not None else 0,
            "total": int(progress.total) if progress is not None else 0,
        }
        note = _fallback_note_for(service, [updated])
        if note:
            message = f"{message}\n\n{note}"
            data["durable"] = False
        else:
            data["durable"] = True
        return ToolResult(success=True, message=message, data=data)


class TodoStepEditTool(Tool):
    """Rename ONE step (the todo's own title is never touched)."""

    def __init__(self, context: ToolContext) -> None:
        self._context = context

    @property
    def name(self) -> str:
        return "todo_step_edit"

    @property
    def required_arguments(self) -> tuple[str, ...]:
        return ("title",)

    @property
    def description(self) -> str:
        return (
            "Change the title of ONE step of a Todo. The todo keeps its own "
            "title — this never renames the todo. Address the todo by task_id or "
            "query, and the step by its number ('step') or its own words "
            "('step_query'). A stale step version fails and nothing changes."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "title": {
                "type": "string",
                "description": "The step's new title, exactly as the owner meant it.",
            },
            "step": {
                "type": "integer",
                "minimum": 1,
                "description": "The step number (1-based, from todo_step_list).",
            },
            "step_query": {
                "type": "string",
                "description": "Instead of the number: the step's own words.",
            },
            "task_id": {
                "type": "integer",
                "minimum": 1,
                "description": "The todo id (from todo_find or task_list).",
            },
            "query": {
                "type": "string",
                "description": "Instead of the todo id: the owner's own words.",
            },
        }

    @property
    def permission_level(self) -> PermissionLevel:
        return PermissionLevel.READ_WRITE

    @property
    def safe(self) -> bool:
        return True

    @property
    def return_type(self) -> str:
        return "ToolResult with the updated step (number, title, version) in message and data"

    async def execute(self, context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
        title = arguments.get("title")
        if not isinstance(title, str) or not title.strip():
            return ToolResult(
                success=False, message="What should the step say instead? Send the new title."
            )
        binding, failure = await resolve_step_argument(
            getattr(context, "owner_id", 0), arguments
        )
        if failure is not None:
            return failure
        task, step, ordinal = binding

        service = _todo_service(context.owner_id)
        try:
            updated = await service.rename_step(step.id, int(step.version), title)
        except ValueError as exc:
            return ToolResult(success=False, message=f"The step was not changed: {exc}")
        except Exception as exc:  # noqa: BLE001
            return ToolResult(success=False, message=f"Step edit failed: {exc}")
        if updated is None:
            current = await service.get_step(step.id)
            if current is not None:
                return ToolResult(
                    success=False,
                    message=(
                        f"Step {ordinal} was not changed: version {step.version} is "
                        f"stale (it is now v{current.version}). Retry."
                    ),
                    data={"task_id": int(task.id), "current_version": int(current.version)},
                )
            return ToolResult(
                success=False, message=f"Step {ordinal} was not found. Nothing was changed."
            )
        message = f"✏️ Step {ordinal} of «{str(task.label).strip()}» is now — {updated.title}"
        data: dict[str, Any] = {
            "task_id": int(task.id),
            "step_id": int(updated.id),
            "step": int(ordinal),
            "title": str(updated.title),
            "status": str(updated.status),
            "version": int(updated.version),
        }
        note = _fallback_note_for(service, [updated])
        if note:
            message = f"{message}\n\n{note}"
            data["durable"] = False
        else:
            data["durable"] = True
        return ToolResult(success=True, message=message, data=data)


class TodoStepDeleteTool(Tool):
    """Remove ONE step (the todo always survives it)."""

    def __init__(self, context: ToolContext) -> None:
        self._context = context

    @property
    def name(self) -> str:
        return "todo_step_delete"

    @property
    def description(self) -> str:
        return (
            "Remove ONE step from a Todo. The Todo itself is NOT deleted: only "
            "the step disappears from its list. Address the todo by task_id or "
            "query, and the step by its number ('step') or its own words "
            "('step_query'). A stale step version fails and nothing is removed."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "step": {
                "type": "integer",
                "minimum": 1,
                "description": "The step number (1-based, from todo_step_list).",
            },
            "step_query": {
                "type": "string",
                "description": "Instead of the number: the step's own words.",
            },
            "task_id": {
                "type": "integer",
                "minimum": 1,
                "description": "The todo id (from todo_find or task_list).",
            },
            "query": {
                "type": "string",
                "description": "Instead of the todo id: the owner's own words.",
            },
        }

    @property
    def permission_level(self) -> PermissionLevel:
        return PermissionLevel.READ_WRITE

    @property
    def safe(self) -> bool:
        return True

    @property
    def return_type(self) -> str:
        return "ToolResult with the removed step number and the remaining progress in data"

    async def execute(self, context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
        binding, failure = await resolve_step_argument(
            getattr(context, "owner_id", 0), arguments
        )
        if failure is not None:
            return failure
        task, step, ordinal = binding

        service = _todo_service(context.owner_id)
        try:
            removed = await service.delete_step(step.id, int(step.version))
        except Exception as exc:  # noqa: BLE001
            return ToolResult(success=False, message=f"Step delete failed: {exc}")
        if not removed:
            current = await service.get_step(step.id)
            if current is not None:
                return ToolResult(
                    success=False,
                    message=(
                        f"Step {ordinal} was not deleted: version {step.version} is "
                        f"stale (it is now v{current.version}). Retry."
                    ),
                    data={"task_id": int(task.id), "current_version": int(current.version)},
                )
            return ToolResult(
                success=False,
                message=f"Step {ordinal} was not found, so nothing was deleted.",
            )

        remaining = list(await service.list_steps(task.id) or [])
        from backend.ai.task_management import step_progress_of

        progress = step_progress_of(remaining)
        message = (
            f"🗑 Step {ordinal} ({_step_text(step, ordinal)}) removed from "
            f"«{str(task.label).strip()}»\n{progress.label}"
        )
        data: dict[str, Any] = {
            "task_id": int(task.id),
            "step_id": int(step.id),
            "step": int(ordinal),
            "removed": True,
            "total": int(progress.total),
            "completed": int(progress.completed),
            "remaining": int(progress.remaining),
            "durable": not bool(
                getattr(getattr(service, "repository", None), "fallback_active", False)
            ),
        }
        note = _fallback_note_for(service, [])
        if note:
            message = f"{message}\n\n{note}"
        return ToolResult(success=True, message=message, data=data)


__all__ = [
    "TodoStepAddTool",
    "TodoStepListTool",
    "TodoStepTransitionTool",
    "TodoStepEditTool",
    "TodoStepDeleteTool",
    "resolve_step_argument",
]