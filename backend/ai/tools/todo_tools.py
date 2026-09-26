"""Basic Todo tools — the Todo lifecycle on the EXISTING task foundation.

A todo is the unscheduled kind of durable task (``schedule_type='todo'``): a
title the owner manages by hand. Nothing here introduces a second store, a
second state model or a second lifecycle: every operation goes through the
SAME ``TaskManagementService`` / ``TaskCreationService`` -> ``TaskRepository``
boundary the Taskloom surface and ``task_list``/``task_inspect``/
``task_transition``/``task_delete`` already use, with the same owner scoping
and the same version (CAS) contract.

The tools in this module cover only what that boundary did not have:

    todo_add    create one unscheduled todo (title required, never invented)
    todo_find   resolve a TITLE REFERENCE into 0, 1 or N todos
    todo_edit   rename one todo (CAS)

Completion, reopening and deletion are the existing ``task_transition`` /
``task_delete`` operations (extended to accept the SAME deterministic title
reference), so there is no parallel lifecycle implementation anywhere.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from backend.ai.tools.base import PermissionLevel, Tool, ToolResult
from backend.ai.tools.context import ToolContext


def _todo_service(owner_id: int):
    from backend.ai.database.manager import get_repository_manager
    from backend.ai.task_management import TaskManagementService

    return TaskManagementService(get_repository_manager().task, owner_id)


def _candidate_data(resolution: Any) -> list[dict[str, Any]]:
    if resolution is None:
        return []
    return [
        {
            "task_id": int(candidate.task_id),
            "label": str(candidate.label),
            "status": str(candidate.status),
            "version": int(candidate.version),
        }
        for candidate in resolution.candidates
    ]


async def resolve_todo_argument(
    owner_id: int,
    arguments: dict[str, Any],
    *,
    require_version: bool = True,
) -> tuple[Any, ToolResult | None]:
    """Bind one todo tool call to ONE owner-scoped todo, or refuse it.

    Shared by the Todo tools AND by ``task_transition``/``task_delete`` when
    they are addressed by title, so the ambiguity rule exists once:

      - 0 matching todos  -> an honest refusal, nothing is written;
      - exactly 1 match   -> that todo (with the version it currently has, so
        the caller's CAS update is still guarded);
      - 2+ matches        -> the candidate list, and NO mutation attempt. The
        model may ask the owner which one, but it can never pick for them.

    A todo addressed by id keeps the explicit-version contract; a todo
    addressed by title is resolved here, which is exactly the read the CAS
    update afterwards is based on.
    """
    from backend.ai.task_management import TODO_TARGET_OK

    task_id = arguments.get("task_id")
    query = str(arguments.get("query") or "").strip()
    if query and _present(task_id):
        return None, ToolResult(
            success=False,
            message=(
                "Provide either a todo id or a title — not both. "
                "Nothing was changed."
            ),
        )
    if not query and require_version:
        version = _positive_int(arguments.get("expected_version"))
        if _present(task_id) and version is None:
            return None, ToolResult(
                success=False,
                message=(
                    "The todo's current version is required with its id "
                    "(from todo_find or task_list). Nothing was changed."
                ),
            )
    service = _todo_service(owner_id)
    try:
        target = await service.resolve_todo_target(task_id=task_id, query=query)
    except Exception as exc:  # noqa: BLE001
        return None, ToolResult(success=False, message=f"Todo lookup failed: {exc}")
    if target.status != TODO_TARGET_OK:
        # The refusal text is the resolver's own honest rendering (0 matches,
        # a candidate list to choose from, or the missing/invalid reference).
        return None, ToolResult(
            success=False,
            message=target.message,
            data={
                "outcome": str(target.status),
                "candidates": _candidate_data(target.resolution),
            },
        )
    if require_version and not query:
        # An id-addressed call keeps the EXPLICIT version contract: the read
        # above is the current row, so a caller-supplied version that no longer
        # matches it must fail here — the same refusal a stale CAS update would
        # produce — and never be silently replaced by the fresh one.
        expected = _positive_int(arguments.get("expected_version"))
        if expected is not None and expected != int(target.task.version):
            return None, ToolResult(
                success=False,
                message=(
                    f"Todo #{target.task.id} was not changed: version {expected} is "
                    f"stale (it is now v{target.task.version}). Retry with "
                    f"expected_version={target.task.version}."
                ),
                data={
                    "task_id": int(target.task.id),
                    "current_version": int(target.task.version),
                },
            )
    return target.task, None


def _present(value: Any) -> bool:
    """True when an argument was really supplied (``0``/``""`` mean absent)."""
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    return True


def _positive_int(value: Any) -> int | None:
    from backend.ai.persian import coerce_int

    number = coerce_int(value)
    if number is None or number <= 0:
        return None
    return number


class TodoAddTool(Tool):
    """Create one basic Todo (an unscheduled item) for the owner."""

    def __init__(self, context: ToolContext) -> None:
        self._context = context

    @property
    def name(self) -> str:
        return "todo_add"

    @property
    def required_arguments(self) -> tuple[str, ...]:
        return ("title",)

    @property
    def description(self) -> str:
        return (
            "Add one basic Todo: a title the owner wants to track by hand "
            "(no schedule, no reminder, no automatic action). Use this when "
            "the owner says to add/create a todo or task WITHOUT a time, "
            "interval, or recurring cadence; a timed/recurring request is "
            "create_task instead. The title must be the owner's own words — "
            "never invented."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "title": {
                "type": "string",
                "description": "The todo's title, exactly as the owner meant it.",
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
        return "ToolResult with the created todo id/title/version in message and data"

    async def execute(self, context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
        from backend.ai.task_creation import TaskCreationError, TaskCreationService

        title = arguments.get("title")
        if not isinstance(title, str) or not title.strip():
            # The title is the one thing a todo cannot be created without, and
            # it is never invented here: ask the owner for it instead.
            return ToolResult(
                success=False,
                message="What should this todo say? Send the title and I will add it.",
            )
        owner_id = getattr(context, "owner_id", 0)
        if not isinstance(owner_id, int) or owner_id <= 0:
            return ToolResult(
                success=False, message="Owner identity is unavailable; no todo was added."
            )
        from backend.ai.database.manager import get_repository_manager

        timezone_name = str(getattr(context, "tz_str", "") or "").strip() or "UTC"
        try:
            service = TaskCreationService(get_repository_manager().task, owner_id)
            task = await service.create_todo(
                title, timezone_name, datetime.now(timezone.utc)
            )
        except TaskCreationError as exc:
            return ToolResult(success=False, message=f"Todo was not added: {exc}")
        except Exception as exc:  # noqa: BLE001
            return ToolResult(success=False, message=f"Todo was not added: {exc}")

        data: dict[str, Any] = {
            "task_id": int(task.id),
            "title": str(task.label),
            "status": str(task.status),
            "version": int(task.version),
        }
        message = f"📋 Todo #{task.id} added — {task.label}"
        fallback_backend = str(getattr(task, "fallback_backend", "") or "")
        if fallback_backend:
            # A degraded write is NOT a durable write: the todo exists in the
            # in-memory fallback only, and a restart loses it.
            from backend.ai.task_management_interface import fallback_note

            message = (
                f"{message}\n\n"
                f"{fallback_note(str(getattr(task, 'fallback_reason', '') or ''))}"
            )
            data["durable"] = False
        else:
            data["durable"] = True
        return ToolResult(success=True, message=message, data=data)


class TodoFindTool(Tool):
    """Resolve a title reference into the owner's matching todos (0/1/N)."""

    def __init__(self, context: ToolContext) -> None:
        self._context = context

    @property
    def name(self) -> str:
        return "todo_find"

    @property
    def required_arguments(self) -> tuple[str, ...]:
        return ("query",)

    @property
    def description(self) -> str:
        return (
            "Find the owner's todos by a title reference (the owner's own "
            "words, e.g. 'university report' / 'گزارش دانشگاه'), or by the "
            "todo's number given as the reference. Returns exactly one todo "
            "with its id and CURRENT version when the reference is unique, or "
            "the list of matching todos when several match — then ask the "
            "owner which one instead of choosing. Use the returned id and "
            "version for todo_edit / task_transition / task_delete."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "query": {
                "type": "string",
                "description": (
                    "The title reference from the owner's request (never the "
                    "whole command sentence), or the todo's number."
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
        return "ToolResult with the todo detail or the candidate list in message and data"

    async def execute(self, context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
        from backend.ai.task_management import (
            TODO_RESOLUTION_UNIQUE,
            format_todo_resolution,
        )
        from backend.ai.task_management_interface import todo_detail_text

        query = arguments.get("query")
        if not isinstance(query, str) or not query.strip():
            return ToolResult(
                success=False,
                message="Which todo? Send its title (or number) and I will look it up.",
            )
        owner_id = getattr(context, "owner_id", 0)
        if not isinstance(owner_id, int) or owner_id <= 0:
            return ToolResult(success=False, message="Owner identity is unavailable.")
        service = _todo_service(owner_id)
        try:
            resolution = await service.resolve_todos(query)
        except Exception as exc:  # noqa: BLE001
            return ToolResult(success=False, message=f"Todo find failed: {exc}")

        if resolution.status == TODO_RESOLUTION_UNIQUE:
            candidate = resolution.candidates[0]
            view = None
            try:
                view = await service.inspect(candidate.task_id)
            except Exception:  # noqa: BLE001
                view = None
            message = (
                todo_detail_text(view.task)
                if view is not None
                else format_todo_resolution(resolution)
            )
            return ToolResult(
                success=True,
                message=message,
                data={
                    "outcome": "unique",
                    "task_id": int(candidate.task_id),
                    "label": str(candidate.label),
                    "status": str(candidate.status),
                    "version": int(candidate.version),
                },
            )
        return ToolResult(
            success=False,
            message=format_todo_resolution(resolution),
            data={
                "outcome": str(resolution.status),
                "candidates": _candidate_data(resolution),
            },
        )


class TodoEditTool(Tool):
    """Rename one todo (its title) under the existing version (CAS) contract."""

    def __init__(self, context: ToolContext) -> None:
        self._context = context

    @property
    def name(self) -> str:
        return "todo_edit"

    @property
    def required_arguments(self) -> tuple[str, ...]:
        return ("title",)

    @property
    def description(self) -> str:
        return (
            "Change the title of ONE existing todo. Address it by its id with "
            "the CURRENT version (from todo_find or task_list), or by a title "
            "reference — then the system resolves it deterministically and "
            "refuses when several todos match (ask which one instead). A "
            "stale version fails and nothing changes."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "title": {
                "type": "string",
                "description": "The todo's new title, exactly as the owner meant it.",
            },
            "task_id": {
                "type": "integer",
                "minimum": 1,
                "description": "The todo id (from todo_find or task_list).",
            },
            "expected_version": {
                "type": "integer",
                "minimum": 1,
                "description": "The todo's current version (required with task_id).",
            },
            "query": {
                "type": "string",
                "description": (
                    "Instead of the id: the owner's own title reference; it "
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
        return "ToolResult with the updated todo id/title/version in message and data"

    async def execute(self, context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
        title = arguments.get("title")
        if not isinstance(title, str) or not title.strip():
            return ToolResult(
                success=False,
                message="What should the todo say instead? Send the new title.",
            )
        owner_id = getattr(context, "owner_id", 0)
        if not isinstance(owner_id, int) or owner_id <= 0:
            return ToolResult(
                success=False, message="Owner identity is unavailable; nothing was changed."
            )
        task, failure = await resolve_todo_argument(owner_id, arguments)
        if failure is not None:
            return failure
        service = _todo_service(owner_id)
        try:
            updated = await service.edit_todo_title(task.id, task.version, title)
        except ValueError as exc:
            return ToolResult(success=False, message=f"Todo was not changed: {exc}")
        except Exception as exc:  # noqa: BLE001
            return ToolResult(success=False, message=f"Todo edit failed: {exc}")
        if updated is None:
            current = None
            try:
                current = await service.repository.get_task(owner_id, task.id)
            except Exception:  # noqa: BLE001
                current = None
            if current is not None:
                return ToolResult(
                    success=False,
                    message=(
                        f"Todo #{task.id} was not changed: version {task.version} is "
                        f"stale (it is now v{current.version}). Retry with "
                        f"expected_version={current.version}."
                    ),
                    data={"task_id": int(task.id), "current_version": int(current.version)},
                )
            return ToolResult(
                success=False,
                message=f"Todo #{task.id} was not found. Nothing was changed.",
            )
        data: dict[str, Any] = {
            "task_id": int(updated.id),
            "title": str(updated.label),
            "status": str(updated.status),
            "version": int(updated.version),
        }
        message = f"✏️ Todo #{updated.id} is now — {updated.label}"
        fallback_backend = str(getattr(updated, "fallback_backend", "") or "")
        if fallback_backend:
            from backend.ai.task_management_interface import fallback_note

            message = (
                f"{message}\n\n"
                f"{fallback_note(str(getattr(updated, 'fallback_reason', '') or ''))}"
            )
            data["durable"] = False
        else:
            data["durable"] = True
        return ToolResult(success=True, message=message, data=data)
