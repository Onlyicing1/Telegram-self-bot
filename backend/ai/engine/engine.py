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
selected from the environment (``AI_PROVIDER`` + configured API keys);
the DummyProvider is only the automatic fallback and never reports
fake success.

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
        if memory_manager is not None:
            self._memory_manager = memory_manager
        else:
            try:
                # Wire the shared repository into the normal execution path:
                # Supabase-backed when available, in-memory otherwise. Memory
                # retrieval stays behind the MemoryManager abstraction — never
                # direct DB calls from the dispatcher.
                from backend.ai.database.manager import get_repository_manager
                repo = get_repository_manager().memory
                self._memory_manager = MemoryManager(
                    long_repository=repo, permanent_repository=repo,
                )
            except Exception as exc:
                logger.warning("MemoryManager default construction failed: %s", exc)
                self._memory_manager = MemoryManager()
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

    async def execute(
        self,
        user_request: AIRequest,
        status_callback: "Callable[[str], Awaitable[None]] | None" = None,
    ) -> EngineResult:
        """Execute a request through the full AI pipeline.

        This is the ONLY public execution method. Returns an immutable
        ``EngineResult``. Never raises.
        """
        return await self._dispatcher.dispatch(user_request, status_callback=status_callback)

    def engine_health(self) -> str:
        """Return ``"READY" or "FAILED: <reason>``."""
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

    def attach_tools(
        self,
        registry: ToolRegistry,
        context: "ToolContext | None" = None,
        owner_id: int = 0,
        tz_str: str = "UTC",
    ) -> None:
        """Attach or replace the tool registry and executor at runtime.

        ``context`` carries the REAL runtime ToolContext (TelegramAPI
        facade + Telethon client) built by the supervisor. The executor
        base context is created from it so per-request contexts inherit
        ``ctx.telegram`` and ``ctx.client`` at execution time.

        The executor is propagated to the Dispatcher so the tool
        continuation loop actually runs — there is exactly ONE
        authoritative tool execution path.
        """
        from backend.ai.tools.context import ToolContext
        base_ctx = context if context is not None else ToolContext(
            telegram=None, owner_id=owner_id, tz_str=tz_str,
        )
        self._tool_registry = registry
        self._tool_executor = ToolExecutor(registry, base_ctx)
        self._dispatcher.set_tool_registry(registry)
        self._dispatcher.set_tool_executor(self._tool_executor)


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


def apply_runtime_selection(provider: str, model: str = "") -> bool:
    """Apply a (provider, model) selection to the runtime engine.

    This is the ONE authoritative path used by the web API, the glass
    actions, and the chat entry points: it switches the active provider
    and updates the registered provider instance's config so the runtime
    sends exactly the (provider, model) the user selected. Never raises.
    """
    try:
        return get_engine().provider_manager.apply_selection(provider, model)
    except Exception as exc:
        logger.warning("engine.apply_runtime_selection failed for provider=%s: %s", provider, exc)
        return False


async def _heal_phantom_config(
    engine: Engine, owner_id: int, config: dict, persisted_provider: str,
) -> tuple[str, str] | None:
    """Rewrite the persisted provider/model to the ACTIVE runtime pair.

    Called when ``apply_runtime_selection`` failed — the persisted provider
    is not registered in this process's ProviderManager. The ProviderManager
    is the single authoritative runtime state, so the persisted config is
    healed to match it (provider + effective model) and the restore
    continues with the effective pair. Never raises; returns the effective
    (provider, model) or None when no heal is possible.
    """
    try:
        active = engine.provider_manager.get_active_name()
        active_name = active if isinstance(active, str) and active else ""
        if not active_name or active_name == persisted_provider:
            return None
        active_model = ""
        try:
            pconfig = engine.provider_manager.get_provider_config(active_name)
            if pconfig is not None:
                m = getattr(pconfig, "default_model", "") or ""
                active_model = m if isinstance(m, str) else ""
        except Exception:  # noqa: BLE001
            pass
        healed = dict(config)
        healed["provider"] = active_name
        healed["model"] = active_model
        from backend.ai import config_store
        await config_store.save_config(owner_id, healed)
        logger.warning(
            "apply_persisted_config: persisted provider '%s' is not registered at "
            "runtime; healed persisted config to active provider '%s' (model '%s')",
            persisted_provider, active_name, active_model,
        )
        return active_name, active_model
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "apply_persisted_config: phantom-config heal failed for owner=%s: %s",
            owner_id, exc,
        )
        return None


async def apply_persisted_config(owner_id: int) -> bool:
    """Apply the persisted AI configuration to the live runtime.

    This is the ONE shared restore, used at boot (``RuntimeSupervisor``)
    and before every chat request (``ai_unified._restore_config``), so
    every surface reads the same runtime state:

      - provider/model  → ``apply_runtime_selection`` (switches the
        registry's active provider and writes the model onto the live
        provider instance);
      - temperature/max_tokens → written onto the active provider's OWN
        config object (the one the provider reads at request time);
      - the owner's conversation session is synced (``set_provider``);
      - the system prompt is applied.

    The persisted ``config_store`` remains the source of truth. Failures
    are logged, never raised.
    """
    try:
        from backend.ai.config_store import get_config
        config = await get_config(owner_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("apply_persisted_config: config load failed for owner=%s: %s", owner_id, exc)
        return False

    try:
        engine = get_engine()
    except Exception as exc:  # noqa: BLE001
        logger.warning("apply_persisted_config: engine unavailable for owner=%s: %s", owner_id, exc)
        return False

    provider = str(config.get("provider", "") or "").strip()
    model = str(config.get("model", "") or "").strip()

    if provider:
        applied = apply_runtime_selection(provider, model)
        if not applied:
            # Phantom persisted selection: the provider is NOT registered in
            # this process (its API key is absent from ENV), so the runtime
            # keeps serving the previous active provider while every
            # config-reading surface (AI menu, settings_get) reports the
            # unappliable pair. Heal the persisted config to the ACTIVE
            # runtime pair — the ProviderManager is the single authoritative
            # runtime state — so the two can never diverge silently.
            healed = await _heal_phantom_config(engine, owner_id, config, provider)
            if healed is not None:
                provider, model = healed
        try:
            engine.conversation_manager.create_session(owner_id)
            engine.conversation_manager.set_provider(owner_id, provider, model)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "apply_persisted_config: session sync failed for owner=%s: %s", owner_id, exc
            )
        try:
            pconfig = engine.provider_manager.get_provider_config(provider)
            if pconfig is not None:
                try:
                    pconfig.temperature = float(config.get("temperature"))
                except (TypeError, ValueError):
                    pass
                try:
                    pconfig.max_tokens = int(config.get("max_tokens"))
                except (TypeError, ValueError):
                    pass
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "apply_persisted_config: provider config sync failed for owner=%s: %s",
                owner_id, exc,
            )

    try:
        engine.conversation_manager.set_system_prompt(
            owner_id,
            str(config.get("system_prompt", "") or "") or "You are LifeOS Assistant.",
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "apply_persisted_config: system prompt apply failed for owner=%s: %s", owner_id, exc
        )
    return True
