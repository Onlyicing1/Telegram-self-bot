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
import hashlib
import logging
import time
from datetime import datetime, timezone
from typing import Any

from backend.ai.task_trace import bind_request, unbind
from backend.ai.tools.base import PermissionLevel, Tool, ToolResult
from backend.ai.tools.context import ToolContext
from backend.ai.task_candidate import TaskCandidate

logger = logging.getLogger(__name__)

MAX_REQUEST_CHARS = 2000
INTERPRET_TIMEOUT_SECONDS = 30.0
# Interpreter (30s) plus creation + persistence margin, still bounded.
EXECUTION_TIMEOUT_SECONDS = 45.0


def _classify_interpretation_failure(exc: Exception) -> str:
    """Map an interpretation failure to a bounded, sanitized category."""
    from backend.ai.task_candidate import TaskCandidateError
    from backend.ai.task_interpreter import TaskInterpretationError

    if isinstance(exc, asyncio.TimeoutError):
        return "timeout"
    if isinstance(exc, TaskInterpretationError):
        text = str(exc)
        if text.startswith("task interpretation provider failed: "):
            # The interpreter already normalized the concrete provider
            # failure (provider/category/detail); carry it verbatim so the
            # tool trace preserves the rate-limit/404/exhausted reason.
            return text.removeprefix("task interpretation provider failed: ")
        cause = exc.__cause__
        cause_name = type(cause).__name__ if cause is not None else ""
        if cause_name == "JSONDecodeError":
            return "candidate_invalid_json"
        if cause_name == "TaskCandidateError":
            return "candidate_invalid"
        return "interpretation_error"
    if isinstance(exc, TaskCandidateError):
        return "candidate_invalid"
    return "unexpected_exception"


class CreateTaskTool(Tool):
    """Create a durable scheduled/reminder task from a natural-language request."""

    def __init__(self, context: ToolContext) -> None:
        self._context = context

    @staticmethod
    def _label_hash(label: str) -> str:
        """Non-reversible label fingerprint for logs — never raw task content."""
        return hashlib.sha256(label.encode("utf-8")).hexdigest()[:12]

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
        from backend.ai.task_interpreter import TaskInterpreter
        from backend.ai.task_creation import TaskCreationService

        extra0 = getattr(context, "extra", None) or {}
        # Bind the request correlation so the interpreter, provider manager,
        # creation service, and repository layers emit correlated
        # AI_TASK_TRACE records for this request (silent outside create_task).
        bind_token = bind_request(
            str(extra0.get("request_id") or "-"),
            _owns_exit=False,
            owner_id=getattr(context, "owner_id", 0),
            chat_id=extra0.get("chat_id", "-"),
            request_message_id=extra0.get("request_message_id", "-"),
        )
        try:
            return await self._execute(context, arguments)
        finally:
            unbind(bind_token)

    async def _execute(self, context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
        from backend.ai.task_interpreter import TaskInterpreter
        from backend.ai.task_creation import TaskCreationService

        started = time.perf_counter()
        request = arguments.get("request")
        if not isinstance(request, str) or not request.strip():
            return ToolResult(success=False, message="Missing task request argument.")
        request = request.strip()
        if len(request) > MAX_REQUEST_CHARS:
            return ToolResult(success=False, message="Task request is too long.")

        owner_id = getattr(context, "owner_id", 0)
        if not isinstance(owner_id, int) or owner_id <= 0:
            return ToolResult(success=False, message="Owner identity is unavailable; task was not created.")

        extra = getattr(context, "extra", None) or {}
        request_id = str(extra.get("request_id") or "-")
        chat_id = extra.get("chat_id")

        def _trace(stage: str, **fields: Any) -> None:
            """Emit one AI_TASK_TRACE lifecycle record for this request."""
            rendered = " ".join(f"{k}={v}" for k, v in fields.items() if v is not None)
            logger.info(
                "AI_TASK_TRACE request_id=%s stage=%s%s",
                request_id, stage, (" " + rendered) if rendered else "",
            )

        def _fail(stage: str, category: str, exc: Exception | None = None) -> ToolResult:
            """Single terminal failure record — last stage + safe detail."""
            logger.warning(
                "AI_TASK_TRACE request_id=%s stage=create_task_failed failed_stage=%s "
                "category=%s exception=%s detail=%s elapsed_ms=%s persisted=false",
                request_id, stage, category,
                type(exc).__name__ if exc is not None else "-",
                str(exc)[:200] if exc is not None else "-",
                int((time.perf_counter() - started) * 1000),
            )
            return ToolResult(success=False, message=(
                "I could not turn that into a safe, unambiguous schedule, so I "
                "did not create any task. Restate it as an interval (e.g. 'every "
                "X minutes'), a time, or a daily/weekly cadence with a clear action."
            ))

        _trace(
            "create_task_received", owner_scope=owner_id,
            chat_id=chat_id if isinstance(chat_id, int) else "-",
            request_len=len(request),
            preview=" ".join(request.split())[:80],
        )
        _trace("create_task_validation_start")
        tz_str = getattr(context, "tz_str", "UTC") or "UTC"
        provider_manager = extra.get("provider_manager")
        deterministic_candidate = extra.get("deterministic_task_candidate")
        if deterministic_candidate is not None:
            _trace("create_task_validation_end", success=True, mode="deterministic")
            candidate = deterministic_candidate
        else:
            _trace("create_task_validation_end", success=True, mode="nl_interpretation")
            if provider_manager is None:
                try:
                    from backend.ai.engine.engine import get_engine
                    provider_manager = get_engine().provider_manager
                except Exception:
                    provider_manager = None
            if provider_manager is None:
                return _fail(
                    "create_task_provider_resolution", "provider_manager_unavailable",
                    RuntimeError("provider manager is unavailable"),
                )

            _trace(
                "create_task_interpretation_start", mode="provider",
                request_len=len(request),
            )
            try:
                candidate = await asyncio.wait_for(
                    TaskInterpreter(provider_manager).interpret(
                        request, timezone=tz_str, request_id=request_id,
                    ),
                    timeout=INTERPRET_TIMEOUT_SECONDS,
                )
            except asyncio.CancelledError:
                raise
            except asyncio.TimeoutError as exc:
                logger.warning(
                    "AI_TASK_TRACE request_id=%s stage=interpretation_failed category=timeout "
                    "exception=TimeoutError all_providers_exhausted=false",
                    request_id,
                )
                return _fail("create_task_interpretation", "timeout", exc)
            except Exception as exc:  # noqa: BLE001
                category = _classify_interpretation_failure(exc)
                logger.warning(
                    "AI_TASK_TRACE request_id=%s stage=interpretation_failed category=%s "
                    "exception=%s detail=%s all_providers_exhausted=%s",
                    request_id, category,
                    type(exc).__name__, str(exc)[:200],
                    "category=all_providers_failed" in str(exc),
                )
                return _fail("create_task_interpretation", category, exc)

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
            _trace(
                "create_task_destination_resolution", mode="chat_name",
                requested=" ".join(str(chat_name).split())[:64],
            )
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
                _trace(
                    "create_task_destination_resolution", mode="chat_name",
                    success=False, reason=str(resolved.get("reason") or "ambiguous")[:64],
                )
                options = format_clarification_options(resolved)
                return ToolResult(
                    success=False,
                    message=f"Could not resolve chat destination:\n{options}",
                )
            destination["chat_id"] = resolved["chat_id"]
            destination["chat_title"] = resolved["chat_title"]
            _trace(
                "create_task_destination_resolution", mode="chat_name", success=True,
                resolved_chat_title=" ".join(str(resolved.get("chat_title") or "").split())[:64],
            )
        else:
            # No explicit chat name — use the current request chat as the
            # destination for scheduled sends. This comes from trusted runtime
            # context (the AIRequest.chat_id), never from the model.
            request_chat_id = extra.get("chat_id")
            if isinstance(request_chat_id, int) and request_chat_id != 0:
                destination["chat_id"] = request_chat_id
            _trace(
                "create_task_destination_resolution", mode="current_chat", success=True,
                resolved_chat_id=request_chat_id if isinstance(request_chat_id, int) else "default",
            )

        if isinstance(candidate, TaskCandidate):
            candidate = candidate.as_creation_candidate()
        candidate["notification_destination"] = destination

        _trace(
            "create_task_definition_validation_start", schedule_type=candidate.get("schedule_type"),
            action_count=len(candidate.get("actions") or []),
        )
        repository = None
        fallback_used = False
        task = None
        try:
            from backend.ai.database.manager import get_repository_manager
            service = TaskCreationService(get_repository_manager().task, owner_id)
            repository = type(service.repository).__name__
            _trace(
                "create_task_repository_create_start", repository=repository,
                definition_version_expected=1,
            )
            task = await service.create(candidate, datetime.now(timezone.utc))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            _trace(
                "create_task_repository_create_result", success=False,
                repository=repository or "unresolved", exception=type(exc).__name__,
                detail=str(exc)[:120],
            )
            logger.warning(
                "AI_EXEC_TRACE request_id=%s stage=create_task_persist_failed "
                "exception=%s detail=%s repository=%s owner_scope=%s",
                request_id, type(exc).__name__, str(exc)[:200],
                repository, owner_id,
            )
            return _fail("create_task_persistence", "repository_failure", exc)

        fallback_backend = getattr(task, "fallback_backend", None)
        if fallback_backend:
            fallback_used = True
        _trace(
            "create_task_repository_create_result", success=True,
            repository=repository, task_id=task.id, version=task.version,
            fallback_backend=fallback_backend,
        )
        _trace(
            "create_task_definition_validation_end", success=True,
            action_count=len(candidate.get("actions") or []),
            persisted=True, definition_version=task.version,
        )
        logger.info(
            "AI_TASK_TRACE request_id=%s stage=create_task_success task_id=%s "
            "task_version=%s fallback_used=%s final_backend=%s elapsed_ms=%s "
            "schedule_type=%s action_count=%s occurrence=not_applicable",
            request_id, task.id, task.version, fallback_used,
            fallback_backend or repository,
            int((time.perf_counter() - started) * 1000),
            task.schedule_type, len(candidate.get("actions") or []),
        )
        logger.info(
            "AI_EXEC_TRACE request_id=%s stage=create_task_persisted task_id=%s "
            "repository=%s schedule_type=%s action_count=%s label_len=%s "
            "label_hash=%s owner_scope=%s",
            request_id, task.id,
            repository, task.schedule_type, len(candidate.get("actions") or []),
            len(task.label), self._label_hash(task.label), owner_id,
        )

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