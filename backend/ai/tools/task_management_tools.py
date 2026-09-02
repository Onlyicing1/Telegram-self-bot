"""Task lifecycle tools — wrap ``TaskManagementService``.

The AI can now see and manage the tasks it creates through ``create_task``.
Every tool is a thin adapter over the SAME authoritative service the ``.task``
command and Taskloom panel use — no second management system.

Semantics preserved from the existing boundary:
  - owner-scoped: the service filters every operation by ``owner_id``;
  - CAS transitions: mutations require the current ``expected_version``, a
    stale version fails honestly instead of overwriting;
  - the tool layer adds NO new persistence or status logic.
"""
from __future__ import annotations

from typing import Any

from backend.ai.tools.base import PermissionLevel, Tool, ToolResult
from backend.ai.tools.context import ToolContext

_MUTABLE_STATUSES = ("paused", "active", "completed")


class TaskListTool(Tool):
    """List the owner's durable tasks."""

    def __init__(self, context: ToolContext) -> None:
        self._context = context

    @property
    def name(self) -> str:
        return "task_list"

    @property
    def description(self) -> str:
        return (
            "List the owner's scheduled tasks (id, label, status, version, "
            "next run). Use the returned id and version for task_transition."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {}

    @property
    def permission_level(self) -> PermissionLevel:
        return PermissionLevel.READ_ONLY

    @property
    def safe(self) -> bool:
        return True

    @property
    def return_type(self) -> str:
        return "ToolResult with the task list text in message"

    async def execute(self, context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
        from backend.ai.database.manager import get_repository_manager
        from backend.ai.task_management import TaskManagementService
        from backend.ai.task_management_interface import list_text

        try:
            service = TaskManagementService(get_repository_manager().task, context.owner_id)
            result = await list_text(service)
        except Exception as exc:  # noqa: BLE001
            return ToolResult(success=False, message=f"Task list failed: {exc}")
        return ToolResult(
            success=True,
            message=result,
            data={"count": 0 if "No tasks found" in result else -1},
        )


class TaskInspectTool(Tool):
    """Inspect one task: schedule, timezone, recent occurrences."""

    def __init__(self, context: ToolContext) -> None:
        self._context = context

    @property
    def name(self) -> str:
        return "task_inspect"

    @property
    def description(self) -> str:
        return (
            "Inspect one scheduled task by id: label, status, version, "
            "schedule, timezone, and recent occurrences."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "task_id": {
                "type": "integer",
                "minimum": 1,
                "description": "The task id (from task_list or create_task).",
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
        return "ToolResult with the task detail text in message"

    async def execute(self, context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
        from backend.ai.database.manager import get_repository_manager
        from backend.ai.task_management import TaskManagementService
        from backend.ai.task_management_interface import inspect_text

        task_id = _coerce_positive_int(arguments.get("task_id"))
        if task_id is None:
            return ToolResult(success=False, message="A positive task_id is required.")
        try:
            service = TaskManagementService(get_repository_manager().task, context.owner_id)
            result = await inspect_text(service, task_id)
        except Exception as exc:  # noqa: BLE001
            return ToolResult(success=False, message=f"Task inspect failed: {exc}")
        if result == "Task not found.":
            return ToolResult(success=False, message=f"Task #{task_id} not found.")
        return ToolResult(success=True, message=result)


class TaskTransitionTool(Tool):
    """Pause / resume / complete a task (owner-scoped, CAS version check)."""

    def __init__(self, context: ToolContext) -> None:
        self._context = context

    @property
    def name(self) -> str:
        return "task_transition"

    @property
    def description(self) -> str:
        return (
            "Change a scheduled task's status: pause, resume (set active), or "
            "complete. Requires the task's CURRENT version (from task_list or "
            "task_inspect) — a stale version fails and nothing changes."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "task_id": {
                "type": "integer",
                "minimum": 1,
                "description": "The task id to transition.",
            },
            "action": {
                "type": "string",
                "enum": list(_MUTABLE_STATUSES),
                "description": "Target status: paused, active (resume), or completed.",
            },
            "expected_version": {
                "type": "integer",
                "minimum": 1,
                "description": "The task's current version (optimistic concurrency).",
            },
        }

    @property
    def permission_level(self) -> PermissionLevel:
        # Pausing/resuming/completing the owner's own task is a benign,
        # owner-authorized lifecycle operation; the CAS version check keeps
        # it deterministic and stale-safe.
        return PermissionLevel.READ_WRITE

    @property
    def safe(self) -> bool:
        return True

    @property
    def return_type(self) -> str:
        return "ToolResult with the new task id/status/version in message and data"

    async def execute(self, context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
        from backend.ai.database.manager import get_repository_manager
        from backend.ai.task_management import TaskManagementService

        task_id = _coerce_positive_int(arguments.get("task_id"))
        version = _coerce_positive_int(arguments.get("expected_version"))
        status = str(arguments.get("action") or "").strip().lower()
        if task_id is None:
            return ToolResult(success=False, message="A positive task_id is required.")
        if version is None:
            return ToolResult(
                success=False,
                message=(
                    "The task's current version is required (from task_list or "
                    "task_inspect). Nothing was changed."
                ),
            )
        if status not in _MUTABLE_STATUSES:
            return ToolResult(
                success=False,
                message=f"Unsupported status '{status}'. Allowed: paused, active, completed.",
            )

        try:
            service = TaskManagementService(get_repository_manager().task, context.owner_id)
            task = await service.set_status(task_id, status, expected_version=version)
        except Exception as exc:  # noqa: BLE001
            return ToolResult(success=False, message=f"Task transition failed: {exc}")
        if task is None:
            return ToolResult(
                success=False,
                message=(
                    f"Task #{task_id} not found, ownership check failed, or "
                    f"version {version} is stale. Nothing was changed."
                ),
            )
        return ToolResult(
            success=True,
            message=f"✅ Task #{task.id} is now {task.status} · version {task.version}.",
            data={
                "task_id": int(task.id),
                "status": str(task.status),
                "version": int(task.version),
            },
        )


def _coerce_positive_int(value: Any) -> int | None:
    from backend.ai.persian import coerce_int

    number = coerce_int(value)
    if number is None or number <= 0:
        return None
    return number
