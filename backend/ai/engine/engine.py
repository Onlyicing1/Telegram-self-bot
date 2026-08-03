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
from backend.ai.prompt.builder import PromptBuilder
from backend.ai.providers.factory import ProviderFactory
from backend.ai.providers.registry import ProviderRegistry
from backend.ai.runtime.manager import ConversationManager
from backend.ai.session.request import AIRequest

logger = logging.getLogger(__name__)


class Engine:
    """The single public entry point for AI execution.

    Constructed once and injected. Owns the conversation manager,
    prompt builder, provider registry, dispatcher, hooks, and metrics.
    """

    __slots__ = (
        "_conversation",
        "_prompt_builder",
        "_providers",
        "_dispatcher",
        "_hooks",
        "_metrics",
    )

    def __init__(
        self,
        conversation: ConversationManager | None = None,
        prompt_builder: PromptBuilder | None = None,
        providers: ProviderRegistry | None = None,
        hooks: EngineHooks | None = None,
    ) -> None:
        self._conversation = conversation or ConversationManager()
        self._prompt_builder = prompt_builder or PromptBuilder()
        self._providers = providers or ProviderFactory.create_registry()
        self._hooks = hooks or NOOP_HOOKS
        self._metrics = EngineMetrics()
        self._dispatcher = Dispatcher(
            conversation=self._conversation,
            prompt_builder=self._prompt_builder,
            providers=self._providers,
            hooks=self._hooks,
            metrics=self._metrics,
        )
        logger.info(
            "Engine initialized (provider=%s, providers=%s)",
            self._providers.default_provider().name,
            self._providers.list(),
        )

    # ── Public API ──

    def execute(self, user_request: AIRequest) -> EngineResult:
        """Execute a request through the full AI pipeline.

        This is the ONLY public execution method. Returns an immutable
        ``EngineResult``. Never raises.
        """
        return self._dispatcher.dispatch(user_request)

    def engine_health(self) -> str:
        """Return ``"READY"`` or ``"FAILED: <reason>"``."""
        try:
            provider = self._providers.default_provider()
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
