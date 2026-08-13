"""
Engine — the ONLY public entry point for AI execution.

Public API:
    execute(user_request) → EngineResult
    engine_health()        → "READY" or "FAILED: <reason>"

Nobody calls providers, prompt builders, or conversation managers
directly anymore. The engine owns the dispatcher, which drives the
request through every layer in the fixed order:

    Conversation Runtime → Prompt Builder → Provider Factory
    → Provider → Response → Conversation Update → Result

The engine is constructed once and injected wherever needed. No
globals, no duplicated managers, no singletons. The active provider is
always the DummyProvider — no HTTP, no SDK, no external API.

Failure handling:
    Any exception inside any layer is caught by the dispatcher and
    converted into ``EngineResult(success=False)``. The engine never
crashes and never propagates uncaught exceptions.
"""
from __future__ import annotations

import logging
from typing import Any

from backend.ai.engine.dispatcher import Dispatcher
from backend.ai.engine.hooks import NOOP_HOOKS, EngineHooks
from backend.ai.engine.metrics import EngineMetrics
from backend.ai.engine.result import EngineResult
from backend.ai.memory.manager import MemoryManager
from backend.ai.prompt.builder import PromptBuilder
from backend.ai.providers.factory import ProviderFactory
from backend.ai.providers.manager.manager import ProviderManager
from backend.ai.providers.registry.registry import ProviderRegistry
from backend.ai.runtime.manager import ConversationManager
from backend.ai.session.request import AIRequest
from backend.ai.tools.executor import ToolExecutor
from backend.ai.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)


class Engine:
    """The single public entry point for AI execution.

    Constructed once and injected. Owns the conversation manager,
    prompt builder, provider registry, dispatcher, hooks, and metrics.
    """

    __slots__ = (
        "_conversation",
        "_prompt_builder",
        "_provider_manager",
        "_providers",
        "_dispatcher",
        "_hooks",
        "_metrics",
        "_tool_registry",
        "_tool_executor",
        "_memory_manager",
    )

    def __init__(
        self,
        conversation: ConversationManager | None = None,
        prompt_builder: PromptBuilder | None = None,
        providers: ProviderRegistry | ProviderManager | None = None,
        hooks: EngineHooks | None = None,
        tool_registry: ToolRegistry | None = None,
        memory_manager: MemoryManager | None = None,
    ) -> None:
        self._conversation = conversation or ConversationManager()
        self._prompt_builder = prompt_builder or PromptBuilder()
        if providers is None:
            self._provider_manager = ProviderFactory.create_manager()
            self._providers = self._provider_manager.registry
        elif isinstance(providers, ProviderManager):
            self._provider_manager = providers
            self._providers = providers.registry
        else:
            self._provider_manager = ProviderManager(providers)
            self._providers = providers
        self._hooks = hooks or NOOP_HOOKS
        self._metrics = EngineMetrics()
        self._tool_registry = tool_registry
        self._tool_executor: ToolExecutor | None = None
        if tool_registry is not None:
            from backend.ai.tools.context import ToolContext
            ctx = ToolContext(telegram=None, owner_id=0, tz_str="UTC")
            self._tool_executor = ToolExecutor(tool_registry, ctx)
        self._memory_manager = memory_manager or MemoryManager()
        self._dispatcher = Dispatcher(
            conversation=self._conversation,
            prompt_builder=self._prompt_builder,
            providers=self._provider_manager,
            hooks=self._hooks,
            metrics=self._metrics,
            tool_registry=self._tool_registry,
            tool_executor=self._tool_executor,
            memory_manager=self._memory_manager,
        )
        logger.info(
            "Engine initialized (provider=%s, providers=%s)",
            self._provider_manager.get_active_name(),
            self._provider_manager.list_providers(),
        )

    # ── Public API ──

    async def execute(self, user_request: AIRequest) -> EngineResult:
        """Execute a request through the full AI pipeline.

        This is the ONLY public execution method. Returns an immutable
        ``EngineResult``. Never raises.
        """
        return await self._dispatcher.dispatch(user_request)

    def engine_health(self) -> str:
        """Return ``"READY"`` or ``"FAILED: <reason>``."""
        try:
            provider = self._provider_manager.get_active()
            health = provider.health()
            if not health.get("healthy", False):
                return f"FAILED: provider {provider.name} unhealthy"
            if not self._conversation or not self._prompt_builder:
                return "FAILED: missing dependencies"
            return "READY"
        except Exception as exc:  # noqa: BLE001
            return f"FAILED: {exc}"

    # ── Diagnostics (not part of the public execution API) ──

    def metrics_snapshot(self) -> dict[str, Any]:
        """Return a snapshot of aggregate engine metrics. RAM-only."""
        return self._metrics.snapshot()

    @property
    def conversation_manager(self) -> ConversationManager:
        return self._conversation

    @property
    def provider_registry(self) -> ProviderRegistry:
        return self._providers

    @property
    def provider_manager(self) -> ProviderManager:
        return self._provider_manager

    @property
    def tool_registry(self) -> ToolRegistry | None:
        return self._tool_registry

    @property
    def memory_manager(self) -> MemoryManager:
        return self._memory_manager

    def attach_tools(self, registry: ToolRegistry, owner_id: int = 0, tz_str: str = "UTC") -> None:
        """Attach or replace the tool registry and executor at runtime."""
        from backend.ai.tools.context import ToolContext
        self._tool_registry = registry
        self._tool_executor = ToolExecutor(registry, ToolContext(telegram=None, owner_id=owner_id, tz_str=tz_str))


# ── Module-level convenience ──

_default_engine: Engine | None = None


def get_engine() -> Engine:
    """Return the process-wide default Engine instance.

    Constructs it on first call. This is the single Engine instance —
    there are no duplicated managers or registries.
    """
    global _default_engine
    if _default_engine is None:
        _default_engine = Engine()
    return _default_engine


def engine_health() -> str:
    """Module-level health check — delegates to the default engine."""
    return get_engine().engine_health()
