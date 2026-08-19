"""
ToolExecutor — executes tool calls from provider responses.

When a provider returns ``tool_calls`` in its ``ProviderResponse``, the
ToolExecutor is responsible for:

  1. Looking up each tool in the ToolRegistry.
  2. Checking permission level (DANGEROUS/ADMIN_ONLY require confirmation).
  3. Executing the tool with the provided arguments.
  4. Recording the result in tool history.
  5. Returning a list of ``ToolExecutionResult`` objects.

The executor is the SOLE component that calls ``tool.execute()``. The
Engine and Dispatcher never call tools directly.

Safety:
  - READ_ONLY, READ_WRITE, and DANGEROUS tools execute directly. This is
    a single-owner self-bot: the owner's outgoing message is the explicit
    instruction and authorization. Destructive tools enforce deterministic
    arguments in their own ``execute()``.
  - ADMIN_ONLY and CONFIRMATION_REQUIRED tools return a "confirmation
    required" result instead of executing. The caller (Engine) must
    surface this to the owner for confirmation.
  - Unknown tools return an error result.
  - Every execution is wrapped in try/except — the executor never raises.
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from backend.ai.tools.base import PermissionLevel, ToolResult
from backend.ai.tools.context import ToolContext
from backend.ai.tools.registry import ToolRegistry
from backend.runtime.task_guard import guarded_create_task

logger = logging.getLogger(__name__)

MAX_TOOLS_PER_TURN = 5
TOOL_TIMEOUT_SECONDS = 10

_STATUS_LABELS: dict[str, str] = {
    "save": "💾 Saving...",
    "save_by_link": "💾 Saving from link...",
    "search": "🔎 Searching saved items...",
    "list_saves": "📋 Loading saved items...",
    "database_stats": "📊 Checking database...",
    "delete": "🗑️ Deleting...",
    "delete_by_id": "🗑️ Deleting saved item...",
    "delete_replied": "🗑️ Deleting...",
    "delete_message_by_id": "🗑️ Deleting...",
    "delete_messages_by_ids": "🗑️ Deleting...",
    "list_recent_messages": "📋 Reading messages...",
    "settings_get": "⚙️ Checking settings...",
    "settings_set": "⚙️ Updating settings...",
    "get_bio": "👤 Reading bio...",
    "bio_show": "👤 Checking bio...",
    "bio_template": "👤 Updating bio...",
    "bio_text": "👤 Updating bio...",
    "bio_mood": "👤 Updating bio...",
    "bio_on": "👤 Updating bio...",
    "bio_off": "👤 Updating bio...",
    "username_show": "📛 Checking username...",
    "username_set": "📛 Updating username...",
    "organize_list": "🗂️ Checking organization...",
    "organize_clean": "🧹 Cleaning organization...",
}


@dataclass(frozen=True)
class ToolExecutionResult:
    """The result of a single tool execution within a turn."""
    tool_name: str
    success: bool
    message: str
    data: dict[str, Any] = field(default_factory=dict)
    latency_ms: float = 0.0
    needs_confirmation: bool = False
    error: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "tool_name": self.tool_name,
            "success": self.success,
            "message": self.message,
            "data": dict(self.data),
            "latency_ms": self.latency_ms,
            "needs_confirmation": self.needs_confirmation,
            "error": self.error,
        }


class ToolExecutor:
    """Executes tool calls from provider responses.

    The executor is the sole component that calls ``tool.execute()``.
    It enforces permission levels, rate limits, and error handling.
    """

    __slots__ = ("_registry", "_context", "_history_repo")

    def __init__(
        self,
        registry: ToolRegistry,
        context: ToolContext,
        history_repo: Any | None = None,
    ) -> None:
        self._registry = registry
        self._context = context
        self._history_repo = history_repo

    async def execute_calls(
        self,
        tool_calls: list[dict[str, Any]],
        owner_id: int = 0,
        session_id: str = "",
        status_callback: Callable[[str], Awaitable[None]] | None = None,
        context_override: ToolContext | None = None,
    ) -> list[ToolExecutionResult]:
        """Execute a batch of tool calls from a provider response.

        Enforces MAX_TOOLS_PER_TURN. Tools requiring confirmation are
        returned as "needs_confirmation" without executing.

        If ``context_override`` is provided, it replaces the executor's
        base context for this batch — this is how per-request runtime
        context (chat_id, reply_msg, etc.) reaches the tools.
        """
        ctx = context_override or self._context
        results: list[ToolExecutionResult] = []

        for i, call in enumerate(tool_calls):
            if i >= MAX_TOOLS_PER_TURN:
                logger.warning("ToolExecutor: hit max %d tools per turn, skipping remaining", MAX_TOOLS_PER_TURN)
                results.append(ToolExecutionResult(
                    tool_name="(overflow)",
                    success=False,
                    message="Tool call limit reached for this turn.",
                    error="max_tools_exceeded",
                ))
                break

            tool_name = call.get("name", "") or call.get("tool", "")
            if status_callback and tool_name:
                label = _STATUS_LABELS.get(tool_name, f"⏳ Running {tool_name}...")
                try:
                    await status_callback(label)
                except Exception as exc:
                    logger.debug("ToolExecutor: status callback failed for '%s': %s", tool_name, exc)

            result = await self._execute_single(call, owner_id, session_id, ctx)
            results.append(result)

        return results

    async def _execute_single(
        self,
        call: dict[str, Any],
        owner_id: int,
        session_id: str,
        context: ToolContext | None = None,
    ) -> ToolExecutionResult:
        """Execute a single tool call. Never raises."""
        ctx = context or self._context
        tool_name = call.get("name", "") or call.get("tool", "")
        arguments = call.get("arguments", {}) or call.get("parameters", {})

        if not tool_name:
            return ToolExecutionResult(
                tool_name="(unknown)",
                success=False,
                message="Tool call missing 'name' field.",
                error="missing_name",
            )

        if call.get("malformed_arguments"):
            # Malformed arguments are a STRUCTURED failure: the tool is
            # never executed with fake {} arguments.
            detail = call.get("arguments_error") or "arguments could not be parsed"
            return ToolExecutionResult(
                tool_name=tool_name,
                success=False,
                message=f"Tool '{tool_name}' was not executed: {detail}.",
                error="malformed_arguments",
            )

        if not isinstance(arguments, dict):
            return ToolExecutionResult(
                tool_name=tool_name,
                success=False,
                message=f"Tool '{tool_name}' received non-object arguments.",
                error="malformed_arguments",
            )

        tool = self._registry.get(tool_name)
        if tool is None:
            return ToolExecutionResult(
                tool_name=tool_name,
                success=False,
                message=f"Tool '{tool_name}' is not registered.",
                error="not_found",
            )

        if not self._is_auto_executable(tool):
            return ToolExecutionResult(
                tool_name=tool_name,
                success=False,
                message=(
                    f"Tool '{tool_name}' requires owner confirmation before it "
                    "can be executed."
                ),
                needs_confirmation=True,
                error="confirmation_required",
            )

        start = time.perf_counter()
        try:
            # Long-running tools (e.g. Deep Save media transfer) must not be
            # cancelled by the generic short tool timeout. Other tools keep the
            # bounded timeout so a stuck tool cannot hang a request forever.
            if getattr(tool, "long_running", False):
                tool_result: ToolResult = await tool.execute(ctx, arguments)
            else:
                tool_result: ToolResult = await asyncio.wait_for(
                    tool.execute(ctx, arguments),
                    timeout=TOOL_TIMEOUT_SECONDS,
                )
            latency_ms = (time.perf_counter() - start) * 1000

            self._record_history(owner_id, session_id, tool_name, arguments, tool_result, latency_ms)

            from backend.ai import persistence
            guarded_create_task(
                persistence.record_tool_call(
                    owner_id, session_id, tool_name, arguments,
                    tool_result.success, tool_result.message, latency_ms,
                ),
                name="ai:record-tool-call",
            )

            return ToolExecutionResult(
                tool_name=tool_name,
                success=tool_result.success,
                message=tool_result.message,
                data=tool_result.data,
                latency_ms=latency_ms,
            )
        except asyncio.TimeoutError:
            latency_ms = (time.perf_counter() - start) * 1000
            logger.warning("ToolExecutor: tool '%s' timed out", tool_name)
            return ToolExecutionResult(
                tool_name=tool_name,
                success=False,
                message=f"Tool '{tool_name}' timed out.",
                latency_ms=latency_ms,
                error="timeout",
            )
        except Exception as exc:
            latency_ms = (time.perf_counter() - start) * 1000
            error_msg = f"{type(exc).__name__}: {exc}"
            logger.warning("ToolExecutor: tool '%s' failed: %s", tool_name, exc)
            return ToolExecutionResult(
                tool_name=tool_name,
                success=False,
                message=f"Tool execution error: {error_msg}",
                latency_ms=latency_ms,
                error=error_msg,
            )

    def _is_auto_executable(self, tool: Any) -> bool:
        """Check if a tool can execute without a confirmation round-trip.

        READ_ONLY, READ_WRITE, and DANGEROUS tools execute directly: the
        owner's message IS the authorization in this single-owner self-bot,
        and destructive tools validate deterministic arguments themselves.
        ADMIN_ONLY and CONFIRMATION_REQUIRED still require confirmation.
        """
        level = tool.permission_level
        return level in (
            PermissionLevel.READ_ONLY,
            PermissionLevel.READ_WRITE,
            PermissionLevel.DANGEROUS,
        )

    def _record_history(
        self,
        owner_id: int,
        session_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        result: ToolResult,
        latency_ms: float,
    ) -> None:
        """Record a tool execution in history (if repository available)."""
        if self._history_repo is None:
            return
        try:
            from backend.ai.database.tool_history_repository import ToolHistoryRecord
            record = ToolHistoryRecord(
                id=str(uuid.uuid4()),
                owner_id=owner_id,
                session_id=session_id,
                tool_name=tool_name,
                arguments=arguments,
                result_success=result.success,
                result_message=result.message,
                result_data=result.data,
                latency_ms=latency_ms,
            )
            self._history_repo.create(record)
        except Exception as exc:
            logger.warning("ToolExecutor: failed to record history: %s", exc)
