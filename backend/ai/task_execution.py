"""Execute claimed durable occurrences through the registered tool boundary."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import re

from backend.ai.database.task_repository import OccurrenceRecord, TaskRepository
from backend.ai.task_contract import PreparedAction, TaskContractError, validate_prepared_action
from backend.ai.tools.context import ToolContext
from backend.ai.tools.executor import ToolExecutor
from backend.ai.tools.registry import ToolRegistry
from backend.ai.retry import FailureClass, classify_failure, retry_delay, can_retry

logger = logging.getLogger(__name__)
MAX_EXECUTION_SECONDS = 60.0
MAX_METADATA_BYTES = 8192
MAX_RESULT_DELIVERY_CHARS = 4000
RESULT_DELIVERY_TIMEOUT_SECONDS = 10.0
MAX_PREPARATION_SECONDS = 45.0

_JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)


class TaskPreparationError(ValueError):
    """Occurrence-time AI argument preparation failed; the action must not run."""


def _load_preparation_json(raw: str) -> Any:
    """Parse the model's JSON, tolerating the common markdown-fence wrapper."""
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    match = _JSON_BLOCK_RE.search(raw)
    if not match:
        return json.loads(raw)
    return json.loads(match.group(1))


class AIActionPreparator:
    """Resolve AI-generated arguments for one occurrence, fail-closed.

    The model NEVER becomes an execution authority: it receives the stored
    instruction plus the task's own action templates (tool names fixed by the
    task definition) and must return complete arguments for those SAME tools.
    No tool definitions are attached (``tools=[]``), so the provider cannot
    trigger any execution — it can only emit arguments, and only the
    ToolRegistry/ToolExecutor boundary decides what actually runs.
    """

    def __init__(self, provider_manager: Any) -> None:
        self._providers = provider_manager

    async def prepare(
        self,
        instruction: str,
        templates: list[dict[str, Any]],
        *,
        owner_id: int,
        tz_str: str,
    ) -> list[dict[str, Any]]:
        from backend.ai.providers.base import ProviderResponse

        now = datetime.now(timezone.utc).isoformat()
        messages = [
            {
                "role": "system",
                "content": (
                    "You prepare final arguments for a scheduled task's actions. "
                    "Return ONLY one JSON object: "
                    '{"actions": [{"name": "<tool name>", "arguments": {...}}, ...]} — '
                    "exactly one entry per listed action, in the same order, with the "
                    "SAME tool names. Arguments must be complete and final for THIS "
                    "run. Never include owner ids, chat ids, message ids, phone "
                    "numbers, or any destination. Never invent new tools. No prose, "
                    "no markdown beyond a JSON code fence."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Task instruction: {instruction}\n"
                    f"Current time (UTC): {now}\n"
                    f"Owner timezone: {tz_str}\n"
                    f"Actions to prepare: {json.dumps(templates, ensure_ascii=False)}"
                ),
            },
        ]
        try:
            response: ProviderResponse = await asyncio.wait_for(
                self._providers.chat(messages, tools=[]),
                timeout=MAX_PREPARATION_SECONDS,
            )
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError as exc:
            # Transient provider failure — retryable under the existing contract.
            raise TimeoutError("task preparation provider timed out") from exc
        except Exception as exc:
            raise TaskPreparationError(f"task preparation provider failed: {type(exc).__name__}") from exc
        if not response.success:
            raise TaskPreparationError(
                f"task preparation provider failed: {response.provider_name or 'unknown'}"
            )
        raw = response.text
        if not isinstance(raw, str) or not raw.strip():
            raise TaskPreparationError("task preparation returned no structured output")
        try:
            value = _load_preparation_json(raw)
        except json.JSONDecodeError as exc:
            raise TaskPreparationError("task preparation returned invalid JSON") from exc
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise TaskPreparationError("task preparation returned invalid JSON") from exc
        if not isinstance(value, dict) or set(value) != {"actions"}:
            raise TaskPreparationError("task preparation output must be {'actions': [...]}")
        actions = value["actions"]
        if not isinstance(actions, list) or len(actions) != len(templates):
            raise TaskPreparationError("task preparation returned the wrong number of actions")
        prepared: list[dict[str, Any]] = []
        for template, action in zip(templates, actions, strict=True):
            if not isinstance(action, dict):
                raise TaskPreparationError("each prepared action must be an object")
            # The model may fill arguments for the task's own tools ONLY; a
            # swapped or invented tool name fails closed here.
            if action.get("name") != template["name"]:
                raise TaskPreparationError("prepared action tool name does not match the task definition")
            arguments = action.get("arguments")
            if not isinstance(arguments, dict):
                raise TaskPreparationError("prepared action arguments must be an object")
            try:
                validate_prepared_action({"name": template["name"], "arguments": arguments})
            except TaskContractError as exc:
                raise TaskPreparationError(str(exc)) from exc
            prepared.append({"name": template["name"], "arguments": dict(arguments)})
        return prepared


def _default_preparator() -> AIActionPreparator | None:
    """Resolve the process ProviderManager for the default preparator."""
    try:
        from backend.ai.engine.engine import get_engine
        return AIActionPreparator(get_engine().provider_manager)
    except Exception:
        return None


@dataclass(frozen=True)
class TaskExecutionResult:
    success: bool
    status: str
    action_count: int
    successful_actions: int
    error: str = ""
    metadata: dict[str, Any] | None = None


def _bounded_metadata(value: dict[str, Any]) -> dict[str, Any]:
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if len(encoded.encode()) > MAX_METADATA_BYTES:
        raise ValueError("execution metadata exceeds bounded size")
    return value


class TaskExecutionCoordinator:
    """Coordinates one claimed occurrence; it never owns scheduling."""

    def __init__(
        self,
        repository: TaskRepository,
        executor: ToolExecutor,
        owner_id: int,
        context: ToolContext,
        client_provider=None,
        preparator: AIActionPreparator | None = None,
    ) -> None:
        self.repository = repository
        self.executor = executor
        self.owner_id = owner_id
        self.context = context
        self.client_provider = client_provider
        self.preparator = preparator

    def _fresh_context(self) -> ToolContext:
        """Return a context bound to the CURRENT self client when a provider
        is available (the supervisor's recovery path rebuilds the client; a
        stale captured client must never execute a scheduled action)."""
        if self.client_provider is None:
            return self.context
        try:
            client = self.client_provider()
        except Exception:
            client = None
        if client is None or client is self.context.client:
            return self.context
        from backend.telegram_api import TelegramAPI
        return ToolContext(
            telegram=TelegramAPI(client),
            owner_id=self.context.owner_id,
            tz_str=self.context.tz_str,
            client=client,
            extra=self.context.extra,
        )

    async def execute(self, occurrence: OccurrenceRecord) -> TaskExecutionResult:
        if occurrence.owner_id != self.owner_id:
            return TaskExecutionResult(False, "failed", 0, 0, "owner_mismatch")
        if occurrence.status != "running":
            return TaskExecutionResult(False, occurrence.status, 0, 0, "occurrence_not_running")

        # Resolve the trusted destination for this task's actions.
        # For send_message tools, the chat_id comes from the task's
        # notification_destination (set at creation time from trusted
        # runtime context), never from the model.
        execution_context = self._fresh_context()
        task = await self.repository.get_task(self.owner_id, occurrence.task_id)
        if task is not None:
            dest = task.notification_destination or {}
            chat_id = dest.get("chat_id")
            if isinstance(chat_id, int) and chat_id != 0:
                extra = dict(execution_context.extra) if execution_context.extra else {}
                extra["chat_id"] = chat_id
                execution_context = ToolContext(
                    telegram=execution_context.telegram,
                    owner_id=execution_context.owner_id,
                    tz_str=execution_context.tz_str,
                    client=execution_context.client,
                    extra=extra,
                )

        actions = occurrence.action_snapshot
        if not isinstance(actions, list) or not actions or len(actions) > 5:
            return await self._fail(occurrence, "invalid_action_snapshot", 0, 0)
        calls: list[dict[str, Any]] = []
        for action in actions:
            if not isinstance(action, dict):
                return await self._fail(occurrence, "invalid_action", 0, 0)
            name = action.get("name") or action.get("tool")
            arguments = action.get("arguments", action.get("parameters", {}))
            if not isinstance(name, str) or not name or not isinstance(arguments, dict):
                return await self._fail(occurrence, "invalid_action", 0, 0)
            if self.executor._registry.get(name) is None:
                return await self._fail(occurrence, "unregistered_action", 0, 0)
            calls.append({"name": name, "arguments": arguments})

        # AI-assisted occurrences: only tasks that persist an ai_instruction
        # pay for a provider round. Static tasks keep the exact deterministic
        # path with zero provider calls. The prepared calls still flow through
        # the SAME ToolExecutor below; the model is never an execution
        # authority and can never change the task's tool names.
        instruction = getattr(task, "ai_instruction", None) if task is not None else None
        if isinstance(instruction, str) and instruction.strip():
            try:
                prepared = await self._prepare_calls(calls, instruction, execution_context)
                self._validate_prepared_calls(calls, prepared)
                calls = prepared
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                return await self.handle_failure(occurrence, exc, action_count=len(calls))

        started = time.perf_counter()
        try:
            result = await asyncio.wait_for(
                self.executor.execute_calls(
                    calls,
                    owner_id=self.owner_id,
                    session_id=f"task:{occurrence.task_id}:{occurrence.occurrence_key}",
                    context_override=execution_context,
                ),
                timeout=MAX_EXECUTION_SECONDS,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return await self.handle_failure(occurrence, exc, action_count=len(calls))

        successful = sum(item.success for item in result)
        failed = next((item for item in result if not item.success), None)
        duration_ms = round((time.perf_counter() - started) * 1000, 1)
        metadata = _bounded_metadata({
            "action_count": len(calls),
            "successful_action_count": successful,
            "duration_ms": duration_ms,
            "terminal_status": "succeeded" if failed is None else "failed",
        })
        if failed is not None:
            failure = failed.error or failed.message or "action_failed"
            if failure == "timeout" or failed.message.lower().startswith("timeout"):
                failure = TimeoutError(failed.message or "tool timed out")
            return await self.handle_failure(
                occurrence,
                failure,
                successful=successful,
                action_count=len(calls),
                metadata=metadata,
            )
        updated = await self.repository.transition_occurrence(
            self.owner_id, occurrence.task_id, occurrence.occurrence_key,
            "succeeded", result_metadata=metadata,
            **self._preparation_updates(task, calls),
        )
        if updated is None:
            return TaskExecutionResult(False, "unknown", len(calls), successful, "state_persist_failed", metadata)
        await self._deliver_result(task, result, execution_context, occurrence.occurrence_key)
        return TaskExecutionResult(True, "succeeded", len(calls), successful, metadata=metadata)

    async def _prepare_calls(
        self,
        templates: list[dict[str, Any]],
        instruction: str,
        execution_context: ToolContext,
    ) -> list[dict[str, Any]]:
        preparator = self.preparator
        if preparator is None:
            preparator = _default_preparator()
        if preparator is None:
            raise TaskPreparationError("task preparation authority is unavailable")
        return await preparator.prepare(
            instruction,
            [{"name": c["name"], "arguments": c["arguments"]} for c in templates],
            owner_id=self.owner_id,
            tz_str=execution_context.tz_str,
        )

    @staticmethod
    def _validate_prepared_calls(
        templates: list[dict[str, Any]],
        prepared: list[dict[str, Any]],
    ) -> None:
        """Re-validate prepared calls at the execution boundary (defense-in-depth).

        The coordinator never trusts the preparator: even a buggy or hostile
        preparator cannot swap tool names, add or drop actions, or smuggle a
        non-object argument past this check — only the task's own registered
        tools, in the task's own order, may run.
        """
        if not isinstance(prepared, list) or len(prepared) != len(templates):
            raise TaskPreparationError("prepared actions do not match the task definition")
        for template, action in zip(templates, prepared, strict=True):
            try:
                normalized = validate_prepared_action(action)
            except TaskContractError as exc:
                raise TaskPreparationError(str(exc)) from exc
            if normalized["name"] != template["name"]:
                raise TaskPreparationError("prepared action tool name does not match the task definition")

    @staticmethod
    def _preparation_updates(task: Any, calls: list[dict[str, Any]]) -> dict[str, Any]:
        """Audit record for a successful AI preparation.

        The occurrence ``preparation_metadata`` schema holds exactly one
        PreparedAction, so single-action occurrences record their prepared
        action; multi-action AI tasks keep execution results only. Diagnostic
        only — never part of the success/failure decision.
        """
        instruction = getattr(task, "ai_instruction", None) if task is not None else None
        if not isinstance(instruction, str) or not instruction.strip() or len(calls) != 1:
            return {}
        try:
            metadata = PreparedAction(
                definition_version=int(getattr(task, "version", 1) or 1),
                action=calls[0],
                prepared_at=datetime.now(timezone.utc).isoformat(),
            ).as_dict()
        except (TaskContractError, TypeError, ValueError):
            return {}
        return {"preparation_metadata": metadata}

    async def _deliver_result(
        self,
        task: Any,
        results: list[Any],
        execution_context: ToolContext,
        occurrence_key: str,
    ) -> None:
        """Deliver the execution result to the task's destination chat when
        the task definition explicitly asked for it (``deliver_result``).

        Best-effort and isolated: a delivery failure is logged and never
        changes the occurrence outcome (the actions already succeeded), and
        the destination always comes from the trusted task definition —
        never from the model.
        """
        if task is None:
            return
        destination = task.notification_destination or {}
        if destination.get("deliver_result") is not True:
            return
        chat_id = destination.get("chat_id")
        if not isinstance(chat_id, int) or chat_id == 0:
            chat_id = self.owner_id
        parts = [str(getattr(item, "message", "") or "").strip() for item in results]
        text = "\n\n".join(part for part in parts if part)
        if not text:
            return
        if len(text) > MAX_RESULT_DELIVERY_CHARS:
            text = text[: MAX_RESULT_DELIVERY_CHARS - 3] + "..."
        telegram = getattr(execution_context, "telegram", None)
        client = getattr(execution_context, "client", None)
        if telegram is None and client is not None:
            from backend.telegram_api import TelegramAPI
            telegram = TelegramAPI(client)
        if telegram is None:
            logger.warning(
                "TASK_RESULT_DELIVERY_UNAVAILABLE task_id=%s occurrence_key=%s",
                task.id, occurrence_key,
            )
            return
        try:
            await asyncio.wait_for(
                telegram.send_message(chat_id, text), timeout=RESULT_DELIVERY_TIMEOUT_SECONDS
            )
            logger.info(
                "TASK_RESULT_DELIVERED task_id=%s occurrence_key=%s chat_id=%s",
                task.id, occurrence_key, chat_id,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "TASK_RESULT_DELIVERY_FAILED task_id=%s occurrence_key=%s exception=%s",
                task.id, occurrence_key, type(exc).__name__,
            )

    async def handle_failure(
        self,
        occurrence: OccurrenceRecord,
        error: BaseException | str,
        *,
        successful: int = 0,
        action_count: int = 0,
        metadata: dict[str, Any] | None = None,
    ) -> TaskExecutionResult:
        decision = classify_failure(error)
        if decision.classification == FailureClass.RETRYABLE and can_retry(occurrence.attempt):
            retry_at = occurrence.updated_at + retry_delay(occurrence.attempt)
            updated = await self.repository.transition_occurrence(
                self.owner_id,
                occurrence.task_id,
                occurrence.occurrence_key,
                "retry_pending",
                retry_at=retry_at,
                attempt=occurrence.attempt + 1,
                error_metadata=_bounded_metadata({
                    "error_class": decision.reason,
                    "attempt": occurrence.attempt,
                    "action_count": action_count,
                    "successful_action_count": successful,
                }),
            )
            return TaskExecutionResult(
                False,
                "retry_pending" if updated else "unknown",
                action_count,
                successful,
                decision.reason,
                metadata,
            )
        return await self._fail(occurrence, decision.reason, successful, action_count, metadata)

    async def _fail(
        self,
        occurrence: OccurrenceRecord,
        error: str,
        successful: int,
        count: int,
        metadata: dict[str, Any] | None = None,
    ) -> TaskExecutionResult:
        safe_error = str(error)[:512]
        error_metadata = _bounded_metadata({
            "error_class": safe_error,
            "attempt": occurrence.attempt,
            "action_count": count,
            "successful_action_count": successful,
        })
        updated = await self.repository.transition_occurrence(
            self.owner_id, occurrence.task_id, occurrence.occurrence_key,
            "failed", error_metadata=error_metadata,
        )
        return TaskExecutionResult(False, "failed" if updated else "unknown", count, successful, safe_error, metadata or error_metadata)
