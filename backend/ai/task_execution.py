"""Execute claimed durable occurrences through the registered tool boundary."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from typing import Any

from backend.ai.database.task_repository import OccurrenceRecord, TaskRepository
from backend.ai.tools.context import ToolContext
from backend.ai.tools.executor import ToolExecutor
from backend.ai.tools.registry import ToolRegistry
from backend.ai.retry import FailureClass, classify_failure, retry_delay, can_retry

logger = logging.getLogger(__name__)
MAX_EXECUTION_SECONDS = 60.0
MAX_METADATA_BYTES = 8192


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
    ) -> None:
        self.repository = repository
        self.executor = executor
        self.owner_id = owner_id
        self.context = context

    async def execute(self, occurrence: OccurrenceRecord) -> TaskExecutionResult:
        if occurrence.owner_id != self.owner_id:
            return TaskExecutionResult(False, "failed", 0, 0, "owner_mismatch")
        if occurrence.status != "running":
            return TaskExecutionResult(False, occurrence.status, 0, 0, "occurrence_not_running")

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

        started = time.perf_counter()
        try:
            result = await asyncio.wait_for(
                self.executor.execute_calls(
                    calls,
                    owner_id=self.owner_id,
                    session_id=f"task:{occurrence.task_id}:{occurrence.occurrence_key}",
                    context_override=self.context,
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
        )
        if updated is None:
            return TaskExecutionResult(False, "unknown", len(calls), successful, "state_persist_failed", metadata)
        return TaskExecutionResult(True, "succeeded", len(calls), successful, metadata=metadata)

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
