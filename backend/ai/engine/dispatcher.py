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

import logging
import time
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

    async def dispatch(self, request: AIRequest) -> EngineResult:
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

        # ── Stage 4: Provider ──
        try:
            messages = self._build_messages(prompt_package)
            response: ProviderResponse = await self._provider_manager.chat(messages)
            safe_call(self._hooks, "after_provider", response)
            metadata["stages"].append("provider")
        except Exception as exc:  # noqa: BLE001
            return self._fail(exc, "provider", start, errors, metadata)

        # ── Stage 4b: Tool Execution ──
        tool_results: list[dict[str, Any]] = []
        if response.tool_calls and self._tool_executor:
            try:
                exec_results = await self._tool_executor.execute_calls(
                    response.tool_calls,
                    owner_id=request.owner_id,
                    session_id=request.session_id,
                )
                for er in exec_results:
                    tool_results.append(er.as_dict())
                    if er.success:
                        self._conversation.add_tool_result(
                            owner_id=request.owner_id,
                            tool_name=er.tool_name,
                            result=er.message,
                        )
                metadata["tool_results"] = tool_results
                metadata["stages"].append("tool_execution")
            except Exception as exc:  # noqa: BLE001
                warnings.append(f"tool_execution: {exc}")

        # ── Stage 5: Conversation Update ──
        try:
            if response.text:
                self._conversation.add_assistant_message(
                    owner_id=request.owner_id, content=response.text
                )
            metadata["stages"].append("conversation_update")
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"conversation_update: {exc}")

        # ── Stage 6: Result ──
        latency = time.perf_counter() - start
        prompt_tokens = int(response.usage.get("prompt_tokens", 0)) or prompt_package.estimated_tokens.estimated_input_tokens
        completion_tokens = int(response.usage.get("completion_tokens", 0))
        total_tokens = prompt_tokens + completion_tokens
        prompt_chars = prompt_package.estimated_tokens.prompt_size_chars

        result = EngineResult(
            success=bool(response.success),
            provider=response.provider_name or provider_name,
            model=self._provider_manager.get_active().config.model or response.provider_name or provider_name,
            latency=latency,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            response=response.text,
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
        )

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
