"""
Dispatcher — the execution spine of the AI Engine.

The dispatcher receives an ``AIRequest`` and drives it through every
layer in the exact, fixed order:

    1. Conversation Runtime  → ConversationContext
    2. Prompt Builder        → PromptPackage
    3. Provider Factory      → active Provider
    4. Provider              → ProviderResponse
    5. Conversation Update   → history + tokens recorded
    6. Result                → EngineResult

No layer is skipped. Any exception raised inside a layer is caught and
converted into an ``EngineResult(success=False)`` — the engine never
propagates an uncaught exception.

The dispatcher measures wall-clock latency for the whole run, invokes
hooks at each lifecycle point, and records metrics. It owns no state
of its own beyond what is injected (conversation manager, prompt
builder, provider registry, hooks, metrics).
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
from backend.ai.providers.registry import ProviderRegistry
from backend.ai.runtime.manager import ConversationManager
from backend.ai.session.request import AIRequest

logger = logging.getLogger(__name__)


class Dispatcher:
    """Drives an ``AIRequest`` through every AI layer and returns an ``EngineResult``."""

    __slots__ = (
        "_conversation",
        "_prompt_builder",
        "_providers",
        "_hooks",
        "_metrics",
    )

    def __init__(
        self,
        conversation: ConversationManager,
        prompt_builder: PromptBuilder,
        providers: ProviderRegistry,
        hooks: EngineHooks | None = None,
        metrics: EngineMetrics | None = None,
    ) -> None:
        self._conversation = conversation
        self._prompt_builder = prompt_builder
        self._providers = providers
        self._hooks = hooks or NOOP_HOOKS
        self._metrics = metrics or EngineMetrics()

    @property
    def metrics(self) -> EngineMetrics:
        return self._metrics

    def dispatch(self, request: AIRequest) -> EngineResult:
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
            safe_call(self._hooks, "after_prompt", prompt_package)
            metadata["stages"].append("prompt_builder")
        except Exception as exc:  # noqa: BLE001
            return self._fail(exc, "prompt_builder", start, errors, metadata)

        # ── Stage 3: Provider Factory ──
        try:
            provider = self._providers.default_provider()
            metadata["stages"].append("provider_factory")
        except Exception as exc:  # noqa: BLE001
            return self._fail(exc, "provider_factory", start, errors, metadata)

        # ── Stage 4: Provider ──
        try:
            response: ProviderResponse = provider.generate(prompt_package)
            safe_call(self._hooks, "after_provider", response)
            metadata["stages"].append("provider")
        except Exception as exc:  # noqa: BLE001
            return self._fail(exc, "provider", start, errors, metadata)

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
            provider=provider.name,
            model=provider.config.model or provider.name,
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
        return ContextBuilder().build(
            session=self._adapt_session(session),
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
            history=[],
        )

    def _adapt_session(self, session: Any) -> Any:
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

            def __init__(self, s: Any) -> None:
                self.session_id = s.session_id
                self.owner_id = s.owner_id
                self.chat_id = 0
                self.state = ConversationState.IDLE
                self.current_panel = ""
                self.current_category = ""
                self.current_flow = ""
                self.pending_action = ""
                self.language = "English"
                self.timezone = "UTC"
                self.current_tool = ""
                self.last_tool = ""

        return _SessionView(session)

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
