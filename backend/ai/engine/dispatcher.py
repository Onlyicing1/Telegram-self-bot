"""
Dispatcher — the execution spine of the AI Engine.

The dispatcher receives an ``AIRequest`` and drives it through every
layer in the exact, fixed order:

    1. Conversation Runtime  → ConversationContext
    2. Prompt Builder        → PromptPackage
    3. Provider Manager      → active Provider name
    4. Provider              → ProviderResponse
    5. Conversation Update   → history + tokens recorded
    6. Result                → EngineResult

No layer is skipped. Any exception raised inside a layer is caught and
converted into an ``EngineResult(success=False)`` — the engine never
propagates an uncaught exception.

The dispatcher measures wall-clock latency for the whole run, invokes
hooks at each lifecycle point, and records metrics. It owns no state
of its own beyond what is injected (conversation manager, prompt
builder, provider manager, hooks, metrics).
"""
from __future__ import annotations

import json
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from backend.ai.engine.hooks import NOOP_HOOKS, EngineHooks, safe_call
from backend.ai.engine.metrics import EngineMetrics
from backend.ai.engine.result import EngineResult
from backend.ai.prompt.builder import PromptBuilder
from backend.ai.providers.base import ProviderResponse
from backend.ai.providers.manager.manager import ProviderManager
from backend.ai.providers.registry.registry import ProviderRegistry
from backend.ai.runtime.manager import ConversationManager
from backend.ai.session.request import AIRequest
from backend.ai.tools.executor import ToolExecutor
from backend.ai.tools.registry import ToolRegistry, create_default_registry
from backend.ai.tools.context import ToolContext

logger = logging.getLogger(__name__)

MAX_TOOL_ROUNDS = 3


_BLOCKED_FINISH_TOKENS = ("SAFETY", "RECITATION", "CONTENT_FILTER", "BLOCKED")
_TRUNCATED_FINISH_TOKENS = ("MAX_TOKENS", "LENGTH", "TRUNCATED")


def _is_blocked_finish(reason: str) -> bool:
    """True when the provider finish reason indicates a blocked response."""
    upper = (reason or "").upper()
    return any(tok in upper for tok in _BLOCKED_FINISH_TOKENS)


def _is_truncated_finish(reason: str) -> bool:
    """True when the provider finish reason indicates token truncation."""
    upper = (reason or "").upper()
    return any(tok in upper for tok in _TRUNCATED_FINISH_TOKENS)


class Dispatcher:
    """Drives an ``AIRequest`` through every AI layer and returns an ``EngineResult``."""

    __slots__ = (
        "_conversation",
        "_prompt_builder",
        "_providers",
        "_provider_manager",
        "_hooks",
        "_metrics",
        "_tool_registry",
        "_tool_executor",
        "_memory_manager",
    )

    def __init__(
        self,
        conversation: ConversationManager,
        prompt_builder: PromptBuilder,
        providers: ProviderRegistry | ProviderManager,
        hooks: EngineHooks | None = None,
        metrics: EngineMetrics | None = None,
        tool_registry: ToolRegistry | None = None,
        tool_executor: ToolExecutor | None = None,
        memory_manager: Any | None = None,
    ) -> None:
        self._conversation = conversation
        self._prompt_builder = prompt_builder
        if isinstance(providers, ProviderManager):
            self._provider_manager = providers
            self._providers = providers.registry
        else:
            self._provider_manager = ProviderManager(providers)
            self._providers = providers
        self._hooks = hooks or NOOP_HOOKS
        self._metrics = metrics or EngineMetrics()
        self._tool_registry = tool_registry
        self._tool_executor = tool_executor
        self._memory_manager = memory_manager

    @property
    def metrics(self) -> EngineMetrics:
        return self._metrics

    def set_tool_executor(self, executor: ToolExecutor | None) -> None:
        """Attach or replace the ToolExecutor used by the tool loop.

        Called by the Engine when tools are wired at runtime so the
        dispatcher never runs with a stale ``None`` executor reference.
        """
        self._tool_executor = executor

    async def dispatch(
        self,
        request: AIRequest,
        status_callback: Callable[[str], Awaitable[None]] | None = None,
    ) -> EngineResult:
        """Execute ``request`` through the full pipeline. Never raises."""
        start = time.perf_counter()
        warnings: list[str] = []
        errors: list[str] = []
        metadata: dict[str, Any] = {"stages": []}

        safe_call(self._hooks, "before_execution", request)

        # ── Stage 1: Conversation Runtime ──
        try:
            session = self._conversation.get_session(request.owner_id)
            if session is None:
                session = self._conversation.create_session(
                    owner_id=request.owner_id, session_id=request.session_id or None
                )
            await self._conversation.restore_history(
                owner_id=request.owner_id, session_id=session.session_id
            )
            if request.user_message:
                self._conversation.add_user_message(
                    owner_id=request.owner_id, content=request.user_message
                )
            metadata["stages"].append("conversation_runtime")
        except Exception as exc:  # noqa: BLE001
            return self._fail(exc, "conversation_runtime", start, errors, metadata)

        # ── Stage 2: Prompt Builder ──
        try:
            prompt_package = self._prompt_builder.build(self._build_context(request, session))
            # Inject tool schemas into the prompt if a registry is available
            if self._tool_registry and not self._tool_registry.is_empty():
                tool_schemas = self._tool_registry.list_schemas()
                tool_block = self._render_tool_schemas(tool_schemas)
                if tool_block:
                    prompt_package = self._inject_tool_schemas(prompt_package, tool_block)
            safe_call(self._hooks, "after_prompt", prompt_package)
            metadata["stages"].append("prompt_builder")
        except Exception as exc:  # noqa: BLE001
            return self._fail(exc, "prompt_builder", start, errors, metadata)

        # ── Stage 3: Provider Manager ──
        try:
            provider_name = self._provider_manager.get_active_name()
            metadata["stages"].append("provider_manager")
        except Exception as exc:  # noqa: BLE001
            return self._fail(exc, "provider_manager", start, errors, metadata)

        # ── Stage 4: Provider + Tool Loop ──
        try:
            messages = self._build_messages(prompt_package)
            response: ProviderResponse = await self._provider_manager.chat(messages)
            safe_call(self._hooks, "after_provider", response)
            metadata["stages"].append("provider")
        except Exception as exc:  # noqa: BLE001
            return self._fail(exc, "provider", start, errors, metadata)

        all_tool_results: list[dict[str, Any]] = []
        # Usage accumulation: initial response + every continuation round.
        # Nothing is double-counted and the initial usage is never lost.
        usage: dict[str, int] = {
            "prompt_tokens": int(response.usage.get("prompt_tokens", 0)),
            "completion_tokens": int(response.usage.get("completion_tokens", 0)),
            "total_tokens": int(response.usage.get("total_tokens", 0)),
        }
        accumulated_finish_reasons: list[str] = []
        if response.metadata.get("finish_reason"):
            accumulated_finish_reasons.append(response.metadata["finish_reason"])

        tool_rounds_executed = 0
        rounds_exhausted = False

        for round_num in range(MAX_TOOL_ROUNDS):
            if not response.success or not response.tool_calls:
                break
            if not self._tool_executor:
                warnings.append(
                    "tool_execution_unavailable: no ToolExecutor attached — "
                    "pending tool calls were not executed"
                )
                metadata["tool_executor_missing"] = True
                break

            try:
                per_request_ctx = self._build_tool_context(request)
                exec_results = await self._tool_executor.execute_calls(
                    response.tool_calls,
                    owner_id=request.owner_id,
                    session_id=request.session_id,
                    status_callback=status_callback,
                    context_override=per_request_ctx,
                )
                for er in exec_results:
                    all_tool_results.append(er.as_dict())
                    # Record EVERY tool outcome (success or failure) so tool
                    # failures never silently disappear from history.
                    marker = "✅" if er.success else "❌"
                    detail = er.message or er.error or "no message"
                    self._conversation.add_tool_result(
                        owner_id=request.owner_id,
                        tool_name=er.tool_name,
                        result=f"{marker} {detail}",
                    )
                metadata["tool_results"] = all_tool_results
                metadata["tool_rounds"] = round_num + 1
                metadata["stages"].append(f"tool_execution_round_{round_num + 1}")
                tool_rounds_executed = round_num + 1
            except Exception as exc:  # noqa: BLE001
                warnings.append(f"tool_execution_round_{round_num + 1}: {exc}")
                break

            continuation_messages = self._build_continuation_messages(messages, response, exec_results)
            try:
                cont_response: ProviderResponse = await self._provider_manager.chat(continuation_messages)
                safe_call(self._hooks, "after_provider", cont_response)
                metadata["stages"].append(f"provider_continuation_{round_num + 1}")
            except Exception as exc:  # noqa: BLE001
                warnings.append(f"provider_continuation_{round_num + 1}: {exc}")
                break

            for k in ("prompt_tokens", "completion_tokens", "total_tokens"):
                usage[k] += int(cont_response.usage.get(k, 0))
            if cont_response.metadata.get("finish_reason"):
                accumulated_finish_reasons.append(cont_response.metadata["finish_reason"])

            response = cont_response
            messages = continuation_messages

        # ── Tool-round exhaustion: pending tool calls are NEVER silently dropped ──
        if response.tool_calls:
            rounds_exhausted = True
            metadata["tool_rounds_exhausted"] = True
            metadata["tool_rounds_executed"] = tool_rounds_executed
            metadata["pending_tool_calls"] = [
                {"name": tc.get("name", ""), "id": tc.get("id", "")}
                for tc in response.tool_calls
            ]
            warnings.append(
                f"tool_round_limit_reached: {len(response.tool_calls)} pending tool call(s) "
                f"after {tool_rounds_executed} round(s) (MAX_TOOL_ROUNDS={MAX_TOOL_ROUNDS})"
            )

        if accumulated_finish_reasons:
            metadata["continuation_finish_reasons"] = accumulated_finish_reasons

        # ── Stage 5: Conversation Update ──
        try:
            if response.success and response.text:
                self._conversation.add_assistant_message(
                    owner_id=request.owner_id, content=response.text
                )
            metadata["stages"].append("conversation_update")
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"conversation_update: {exc}")

        # ── Stage 6: Result ──
        latency = time.perf_counter() - start
        prompt_tokens = usage["prompt_tokens"]
        completion_tokens = usage["completion_tokens"]
        total_tokens = usage["total_tokens"]
        if prompt_tokens == 0 and completion_tokens == 0 and total_tokens == 0:
            # Providers that omit usage: fall back to the prompt estimate.
            prompt_tokens = prompt_package.estimated_tokens.estimated_input_tokens
            total_tokens = prompt_tokens + completion_tokens
        elif total_tokens == 0:
            total_tokens = prompt_tokens + completion_tokens
        elif total_tokens < prompt_tokens + completion_tokens:
            total_tokens = prompt_tokens + completion_tokens
        prompt_chars = prompt_package.estimated_tokens.prompt_size_chars

        if not response.success and response.text:
            errors.append(response.text)

        # ── Empty-response / finish-state classification ──
        # The final result must distinguish every meaningful outcome so the
        # Telegram UI never masks a real condition behind "AI returned no
        # response."
        finish_reason = (
            (response.metadata or {}).get("finish_reason", "")
            or (accumulated_finish_reasons[-1] if accumulated_finish_reasons else "")
        )
        if not response.success:
            finish_state = "provider_failure"
        elif rounds_exhausted:
            finish_state = "tool_rounds_exhausted"
        elif response.text:
            finish_state = "text"
        elif response.tool_calls:
            finish_state = "tool_only"
        elif _is_blocked_finish(finish_reason):
            finish_state = "provider_blocked"
        elif _is_truncated_finish(finish_reason):
            finish_state = "token_truncated"
        else:
            finish_state = "empty"
        metadata["finish_state"] = finish_state
        metadata["finish_reason"] = finish_reason

        if all_tool_results:
            tool_errors = [r for r in all_tool_results if not r.get("success")]
            if tool_errors:
                for te in tool_errors:
                    err_type = te.get("error", "")
                    if err_type and err_type != "max_tools_exceeded":
                        warnings.append(f"tool_{te.get('tool_name', '?')}: {err_type}")

        result = EngineResult(
            success=bool(response.success),
            provider=response.provider_name or provider_name,
            model=self._provider_manager.get_active().config.model or response.provider_name or provider_name,
            latency=latency,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            response=response.text if response.success else "",
            warnings=warnings,
            errors=errors,
            metadata=metadata,
        )

        safe_call(self._hooks, "after_response", result)

        self._metrics.record(
            success=result.success,
            provider=result.provider,
            owner_id=request.owner_id,
            latency=latency,
            prompt_chars=prompt_chars,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            error="" if result.success else (errors[-1] if errors else "provider_failed"),
        )

        return result

    # ── internal ──

    def _build_tool_context(self, request: AIRequest) -> ToolContext:
        """Build a per-request ToolContext from the executor's base context.

        Enriches the base context's ``extra`` dict with ``chat_id`` and
        ``reply_msg`` from the current AIRequest so tools (save, delete,
        etc.) can operate on the correct chat and replied-to message.
        """
        base = self._tool_executor._context
        extra: dict[str, Any] = dict(base.extra) if base.extra else {}
        extra["chat_id"] = request.chat_id
        if request.reply_context and request.reply_context.exists:
            extra["reply_msg"] = {
                "message_id": request.reply_context.message_id,
                "sender_id": request.reply_context.sender_id,
                "sender_name": request.reply_context.sender_name,
                "chat_id": request.reply_context.chat_id,
                "chat_title": request.reply_context.chat_title,
                "media_type": request.reply_context.media_type,
                "text_preview": request.reply_context.text_preview,
                "timestamp": request.reply_context.timestamp,
            }
        return ToolContext(
            telegram=base.telegram,
            owner_id=request.owner_id,
            tz_str=request.timezone or base.tz_str,
            client=base.client,
            extra=extra,
        )

    def _render_tool_schemas(self, schemas: list[dict[str, Any]]) -> str:
        """Render tool schemas into a compact text block for the prompt."""
        if not schemas:
            return ""
        lines = ["[Available Tools]"]
        for s in schemas:
            params = s.get("parameters", {})
            param_str = ""
            if isinstance(params, dict):
                props = params.get("properties", {})
                if props:
                    parts = []
                    for pname, pinfo in props.items():
                        ptype = pinfo.get("type", "any") if isinstance(pinfo, dict) else "any"
                        parts.append(f"{pname}({ptype})")
                    param_str = ", ".join(parts)
            safe_badge = "safe" if s.get("safe") else "needs-confirm"
            lines.append(
                f"  - {s['name']}({param_str}) — {s['description']} [{safe_badge}]"
            )
        return "\n".join(lines)

    def _inject_tool_schemas(self, package: Any, tool_block: str) -> Any:
        """Return a new PromptPackage with the tool context enriched."""
        from dataclasses import replace
        existing = package.tool_context or ""
        merged = f"{existing}\n\n{tool_block}" if existing else tool_block
        return replace(package, tool_context=merged)

    def _build_messages(self, prompt_package: Any) -> list[dict[str, Any]]:
        """Convert a PromptPackage into a messages list for ProviderManager.chat()."""
        messages: list[dict[str, Any]] = []
        if prompt_package.system_prompt:
            messages.append({"role": "system", "content": prompt_package.system_prompt})
        if prompt_package.runtime_context:
            messages.append({"role": "system", "content": prompt_package.runtime_context})
        if prompt_package.conversation_context:
            messages.append({"role": "system", "content": prompt_package.conversation_context})
        if prompt_package.tool_context:
            messages.append({"role": "system", "content": prompt_package.tool_context})
        if prompt_package.user_input:
            messages.append({"role": "user", "content": prompt_package.user_input})
        return messages

    def _build_continuation_messages(
        self,
        original_messages: list[dict[str, Any]],
        response: ProviderResponse,
        exec_results: list[Any],
    ) -> list[dict[str, Any]]:
        messages = list(original_messages)

        assistant_content = response.text or ""
        assistant_msg: dict[str, Any] = {"role": "assistant", "content": assistant_content}
        if response.tool_calls:
            assistant_msg["tool_calls"] = [
                {
                    "id": tc.get("id", ""),
                    "type": "function",
                    "function": {
                        "name": tc.get("name", ""),
                        "arguments": json.dumps(tc.get("arguments", {})),
                    },
                }
                for tc in response.tool_calls
            ]
        messages.append(assistant_msg)

        for tc, er in zip(response.tool_calls, exec_results, strict=False):
            tool_name = tc.get("name", er.tool_name)
            content = json.dumps({
                "tool": tool_name,
                "success": er.success,
                "message": er.message,
                "data": er.data,
                "error": er.error,
            })
            messages.append({
                "role": "tool",
                "tool_call_id": tc.get("id", ""),
                "name": tool_name,
                "content": content,
            })

        return messages

    def _build_context(self, request: AIRequest, session: Any) -> Any:
        """Build a ConversationContext from the runtime session + request.

        Uses the Conversation Layer's ContextBuilder so the Prompt
        Builder receives the exact object type it expects.
        """
        from backend.ai.conversation.context_builder import (
            ContextBuilder,
            RuntimeContext,
            ToolContext,
        )

        history_items = self._conversation.get_history(
            owner_id=request.owner_id, n=20
        )
        from backend.ai.conversation.history import HistoryEntry
        history_entries: list[HistoryEntry] = []
        for item in history_items:
            history_entries.append(HistoryEntry(
                role=item.role,
                content=item.content,
                tool_name=item.role if item.role == "tool" else "",
            ))
        memory_data: dict[str, str] = {}
        if self._memory_manager is not None:
            try:
                memory_data = self._memory_manager.retrieve_for_prompt(request.owner_id)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Memory retrieval failed for owner %s: %r", request.owner_id, exc)
        return ContextBuilder().build(
            session=self._adapt_session(session, request),
            user_text=request.user_message,
            message_id=request.message_id,
            current_menu="main",
            reply=request.reply_context,
            tool=ToolContext(),
            runtime=RuntimeContext(
                ai_enabled=True,
                active_provider=session.active_provider,
                total_requests=self._metrics.total_executions,
                total_responses=self._metrics.successful_executions,
                turn_count=len(history_items),
            ),
            history=history_entries,
            memory=memory_data,
            preferences=self._load_preferences(request.owner_id),
        )

    def _load_preferences(self, owner_id: int) -> Any:
        """Load the owner's AI preferences from the repository manager.

        Uses ``RepositoryManager.preferences.get_or_create()`` which
        returns an in-memory ``PreferencesRecord`` with defaults when
        the ``ai_preferences`` table does not exist yet.
        """
        from backend.ai.conversation.context_builder import PreferencesContext

        try:
            from backend.ai.database.manager import get_repository_manager

            repo = get_repository_manager().preferences
            rec = repo.get_or_create(owner_id)
            return PreferencesContext(
                language=rec.language,
                personality=rec.personality,
                response_style=rec.response_style,
                custom_instructions=rec.custom_instructions,
                auto_memory=rec.auto_memory,
                auto_tools=rec.auto_tools,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Preferences load failed for owner %s: %r", owner_id, exc)
            return PreferencesContext()

    def _adapt_session(self, session: Any, request: AIRequest | None = None) -> Any:
        """Adapt a RuntimeSession to the ConversationSession shape the
        ContextBuilder expects. We build a lightweight stand-in with
        the attributes ContextBuilder reads."""
        from backend.ai.conversation.state import ConversationState

        class _SessionView:
            __slots__ = (
                "session_id", "owner_id", "chat_id", "state",
                "current_panel", "current_category", "current_flow",
                "pending_action", "language", "timezone",
                "current_tool", "last_tool",
            )

            def __init__(self, s: Any, req: AIRequest | None = None) -> None:
                self.session_id = s.session_id
                self.owner_id = s.owner_id
                self.chat_id = req.chat_id if req else 0
                self.state = ConversationState.IDLE
                self.current_panel = ""
                self.current_category = ""
                self.current_flow = ""
                self.pending_action = ""
                self.language = req.language if req else "English"
                self.timezone = req.timezone if req else "UTC"
                self.current_tool = ""
                self.last_tool = ""

        return _SessionView(session, request)

    def _fail(
        self,
        exc: BaseException,
        stage: str,
        start: float,
        errors: list[str],
        metadata: dict[str, Any],
    ) -> EngineResult:
        """Build a failure EngineResult and record metrics."""
        latency = time.perf_counter() - start
        msg = f"{stage}: {exc}"
        errors.append(msg)
        metadata.setdefault("stages", []).append(stage)
        safe_call(self._hooks, "on_error", msg, stage)
        logger.warning("Engine dispatcher failure at %s: %r", stage, exc)
        result = EngineResult(
            success=False,
            latency=latency,
            warnings=[],
            errors=errors,
            metadata=metadata,
        )
        self._metrics.record(
            success=False,
            provider="",
            owner_id=0,
            latency=latency,
            prompt_chars=0,
            prompt_tokens=0,
            completion_tokens=0,
            error=msg,
        )
        return result
