"""Task tools — create durable scheduled tasks from natural language.

The AI never controls execution or owner identity. An AI-originated task
request flows through the SAME canonical boundary as the ``.task`` command:

    TaskInterpreter              (bounded provider call, tools=[], validated candidate)
      -> TaskCreationService     (authoritative owner_id)
        -> TaskRepository.create_task  (ai_tasks, CAS version)

The interpreter fabricates nothing when a request is ambiguous (it returns
JSON null), so this tool never invents a task. The created task's actions are
executed later by the single TaskScheduler through the registered ToolExecutor
— no new executor, no second scheduler.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

from backend.ai.tools.base import PermissionLevel, Tool, ToolResult
from backend.ai.tools.context import ToolContext
from backend.ai.task_candidate import TaskCandidate

MAX_REQUEST_CHARS = 2000
INTERPRET_TIMEOUT_SECONDS = 30.0
# Interpreter (30s) plus creation + persistence margin, still bounded.
EXECUTION_TIMEOUT_SECONDS = 45.0


class CreateTaskTool(Tool):
    """Create a durable scheduled/reminder task from a natural-language request."""

    def __init__(self, context: ToolContext) -> None:
        self._context = context

    @property
    def name(self) -> str:
        return "create_task"

    @property
    def description(self) -> str:
        return (
            "Create a durable scheduled task or reminder from a natural-language "
            "request describing an interval, daily/weekly cadence, or one-time "
            "time and the action to run. Persists the schedule and actions under "
            "the owner; later execution goes through the normal scheduler."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "request": {
                "type": "string",
                "description": (
                    "The full natural-language task request, e.g. 'every 30 minutes "
                    "send a reminder to drink water' or 'every morning write hello'."
                ),
            },
        }

    @property
    def permission_level(self) -> PermissionLevel:
        # The owner's message IS the authorization in this single-owner self-bot.
        # Task creation itself persists a schedule; the actions execute later
        # through the registered ToolExecutor under owner scope.
        return PermissionLevel.READ_WRITE

    @property
    def safe(self) -> bool:
        return True

    @property
    def return_type(self) -> str:
        return "ToolResult with created task id/label in message and data"

    @property
    def timeout_seconds(self) -> int:
        return EXECUTION_TIMEOUT_SECONDS

    async def execute(self, context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
        from backend.ai.task_interpreter import TaskInterpretationError, TaskInterpreter
        from backend.ai.task_creation import TaskCreationError, TaskCreationService

        request = arguments.get("request")
        if not isinstance(request, str) or not request.strip():
            return ToolResult(success=False, message="Missing task request argument.")
        request = request.strip()
        if len(request) > MAX_REQUEST_CHARS:
            return ToolResult(success=False, message="Task request is too long.")

        owner_id = getattr(context, "owner_id", 0)
        tz_str = getattr(context, "tz_str", "UTC") or "UTC"
        provider_manager = (context.extra or {}).get("provider_manager")
        if provider_manager is None:
            try:
                from backend.ai.engine.engine import get_engine
                provider_manager = get_engine().provider_manager
            except Exception:
                provider_manager = None
        if provider_manager is None:
            return ToolResult(success=False, message="AI provider manager is unavailable; task was not created.")

        try:
            candidate = await asyncio.wait_for(
                TaskInterpreter(provider_manager).interpret(request, timezone=tz_str),
                timeout=INTERPRET_TIMEOUT_SECONDS,
            )
        except asyncio.CancelledError:
            raise
        except (TaskInterpretationError, asyncio.TimeoutError, Exception):  # noqa: BLE001
            return ToolResult(
                success=False,
                message=(
                    "I could not turn that into a safe, unambiguous schedule, so I "
                    "did not create any task. Restate it as an interval (e.g. 'every "
                    "X minutes'), a time, or a daily/weekly cadence with a clear action."
                ),
            )

        # Resolve destination: the model may express a chat_name (never a
        # numeric chat_id). Resolve it against the authenticated Self Bot's
        # chats. If no chat_name is given, the destination is the current
        # request chat (for immediate/scheduled sends from this chat).
        if isinstance(candidate, TaskCandidate):
            destination = dict(candidate.notification_destination)
            chat_name = destination.pop("chat_name", None)
        else:
            destination = dict(candidate.get("notification_destination", {}))
            chat_name = destination.pop("chat_name", None)
        if chat_name:
            from backend.ai.chat_resolution import format_clarification_options, resolve_chat_name
            client = getattr(context, "client", None)
            if client is not None:
                try:
                    dialogs = await client.get_dialogs()
                except Exception:
                    dialogs = []
            else:
                dialogs = []
            chats = []
            for dialog in dialogs:
                if isinstance(dialog, dict):
                    chat_id = dialog.get("id")
                    title = dialog.get("title") or dialog.get("name") or ""
                    username = dialog.get("username") or ""
                else:
                    entity = getattr(dialog, "entity", dialog)
                    chat_id = getattr(entity, "id", None) or getattr(dialog, "id", None)
                    title = (
                        getattr(dialog, "title", None)
                        or getattr(entity, "title", None)
                        or getattr(entity, "first_name", None)
                        or getattr(entity, "name", None)
                        or ""
                    )
                    username = getattr(entity, "username", None) or ""
                chats.append({"id": chat_id, "title": title, "username": username})
            resolved = resolve_chat_name(chat_name, chats, owner_id=owner_id)
            if not resolved["resolved"]:
                options = format_clarification_options(resolved)
                return ToolResult(
                    success=False,
                    message=f"Could not resolve chat destination:\n{options}",
                )
            destination["chat_id"] = resolved["chat_id"]
            destination["chat_title"] = resolved["chat_title"]
        else:
            # No explicit chat name — use the current request chat as the
            # destination for scheduled sends. This comes from trusted runtime
            # context (the AIRequest.chat_id), never from the model.
            extra = getattr(context, "extra", None) or {}
            request_chat_id = extra.get("chat_id")
            if isinstance(request_chat_id, int) and request_chat_id != 0:
                destination["chat_id"] = request_chat_id

        if isinstance(candidate, TaskCandidate):
            candidate = candidate.as_creation_candidate()
        candidate["notification_destination"] = destination

        try:
            from backend.ai.database.manager import get_repository_manager
            service = TaskCreationService(get_repository_manager().task, owner_id)
            task = await service.create(candidate, datetime.now(timezone.utc))
        except asyncio.CancelledError:
            raise
        except (TaskCreationError, Exception):  # noqa: BLE001
            return ToolResult(success=False, message="The task could not be persisted; nothing was created.")

        return ToolResult(
            success=True,
            message=f"✅ Task #{task.id} created — {task.label}",
            data={
                "task_id": int(task.id),
                "label": str(task.label),
                "schedule_type": str(task.schedule_type),
                "timezone": str(task.timezone),
                "owner_id": int(task.owner_id),
                "status": str(task.status),
            },
        )