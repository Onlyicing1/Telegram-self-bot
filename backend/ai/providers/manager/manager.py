"""
ProviderManager — the sole intermediary between the AI Engine and
the provider layer.

The Conversation Manager, Dispatcher, and Prompt Builder NEVER call
a provider directly. They call ``ProviderManager.chat()`` and receive
a ``ProviderResponse``. The manager:

  1. Gets the active provider from the registry.
  2. Validates it (health + enabled).
  3. If unhealthy → uses the fallback provider.
  4. Calls ``provider.chat()`` inside a guarded await.
  5. If the call crashes OR returns ``success=False`` (429/500/quota/
     invalid request/...), the configured fallback chain is tried, then
     the emergency fallback. The emergency fallback ALWAYS returns
     ``success=False`` with the original failures preserved — never fake
     success.
  6. Records latency + success/failure in per-provider metrics.

The manager also exposes:
  - ``switch_provider(name)``  → switch the active provider
  - ``register_provider(p)``   → add a new provider at runtime
  - ``unregister_provider(name)``→ remove a provider
  - ``get_active_name()``      → current provider name
  - ``list_providers()``       → all registered names
  - ``provider_health(name)``  → health dict
  - ``metrics_snapshot()``     → all provider metrics
  - ``capabilities(name)``     → ProviderCapabilities for a provider
  - ``get_provider_config(name)`` → ProviderConfig for a provider
  - ``update_provider_config(name, field, value)`` → update + validate
  - ``reset_provider_config(name)`` → reset to defaults
  - ``validate_provider(name)`` → ValidationResult
  - ``export_configs()``       → all configs as dicts

The manager never crashes. Every exception is caught and converted
into a fallback response.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Iterator

from backend.ai.providers.base.config import ProviderConfig
from backend.ai.providers.base.contract import BaseProvider, ProviderResponse
from backend.ai.providers.base.exceptions import ProviderUnavailable
from backend.ai.providers.manager.config_manager import ProviderConfigManager
from backend.ai.providers.manager.metrics import ProviderMetricsRegistry
from backend.ai.providers.registry.registry import ProviderRegistry
from backend.runtime.operation_watchdog import guarded_await

logger = logging.getLogger(__name__)

_PROVIDER_RPC_TIMEOUT = 30.0


class ProviderManager:
    """Manages provider lifecycle, routing, fallback, and metrics.

    The manager is the ONLY object that calls ``provider.chat()``.
    All other layers call ``manager.chat()``.
    """

    __slots__ = ("_registry", "_metrics", "_config_mgr", "_fallback_chain")

    def __init__(self, registry: ProviderRegistry | None = None) -> None:
        self._registry = registry or ProviderRegistry()
        self._metrics = ProviderMetricsRegistry()
        self._config_mgr = ProviderConfigManager()
        self._fallback_chain: list[str] = []
        self._ensure_dummy_fallback()
        self._load_env_fallback_chain()

    # ── Public API ──

    async def chat(self, messages: list[dict[str, Any]], **kwargs: Any) -> ProviderResponse:
        """Send a chat request to the active provider. Never raises.

        Fallback is entered on BOTH hard exceptions AND structured
        ``success=False`` responses (429/500/quota/invalid request/...):
        the configured fallback chain is tried, then the emergency
        fallback. The dummy provider is terminal — it never reports
        fake success and is never used as a fallback source of success.
        """
        provider = self._get_healthy_provider()
        provider_name = provider.name
        start = time.perf_counter()
        try:
            response = await guarded_await(
                provider.chat(messages, **kwargs),
                name=f"provider:chat:{provider_name}",
                timeout=_PROVIDER_RPC_TIMEOUT,
            )
            latency = time.perf_counter() - start
            self._metrics.record(provider_name, latency=latency, error="")
            if response.success or provider_name == "dummy":
                # A dummy response is already terminal (success=False with
                # diagnostics when nothing is configured) — never fall back
                # from the fallback itself.
                return response
            logger.warning(
                "ProviderManager: '%s' returned success=False — attempting fallback",
                provider_name,
            )
            return await self._try_fallback_chain(
                messages,
                original_provider=provider_name,
                initial_failure=f"{provider_name}: success=False ({response.text[:200]})",
                **kwargs,
            )
        except Exception as exc:
            latency = time.perf_counter() - start
            error_msg = f"{type(exc).__name__}: {exc}"
            self._metrics.record(provider_name, latency=latency, error=error_msg)
            logger.warning("ProviderManager: '%s' crashed during chat: %s", provider_name, exc)
            return await self._try_fallback_chain(
                messages,
                original_provider=provider_name,
                initial_failure=f"{provider_name}: {type(exc).__name__}: {exc}",
                **kwargs,
            )

    def vision(self, messages: list[dict[str, Any]], images: list[bytes], **kwargs: Any) -> ProviderResponse:
        """Send a vision request. Never raises."""
        provider = self._get_healthy_provider()
        provider_name = provider.name
        start = time.perf_counter()
        try:
            response = provider.vision(messages, images, **kwargs)
            latency = time.perf_counter() - start
            self._metrics.record(provider_name, latency=latency, error="")
            return response
        except Exception as exc:
            latency = time.perf_counter() - start
            self._metrics.record(provider_name, latency=latency, error=str(exc))
            logger.warning("ProviderManager: '%s' crashed during vision: %s", provider_name, exc)
            return self._fallback_vision(messages, images, **kwargs)

    def stream(self, messages: list[dict[str, Any]], **kwargs: Any) -> Iterator[ProviderResponse]:
        """Stream a chat response. Falls back to dummy on error."""
        provider = self._get_healthy_provider()
        try:
            yield from provider.stream(messages, **kwargs)
        except Exception as exc:
            logger.warning("ProviderManager: '%s' crashed during stream: %s", provider.name, exc)
            self._metrics.record(provider.name, latency=0.0, error=str(exc))
            yield from self._fallback_stream(messages, **kwargs)

    def count_tokens(self, text: str) -> int:
        """Estimate token count using the active provider. Never raises."""
        try:
            return self._get_healthy_provider().count_tokens(text)
        except Exception:
            return max(1, len(text) // 4)

    # ── Provider management ──

    def register_provider(self, provider: BaseProvider) -> bool:
        return self._registry.register(provider)

    def unregister_provider(self, name: str) -> bool:
        return self._registry.unregister(name)

    def switch_provider(self, name: str) -> bool:
        if not self._registry.has(name):
            logger.warning("ProviderManager: cannot switch to '%s' — not registered", name)
            return False
        return self._registry.switch_provider(name)

    def get_active_name(self) -> str:
        return self._registry.active_name

    def get_active(self) -> BaseProvider:
        return self._registry.get_active()

    def list_providers(self) -> list[str]:
        return self._registry.list()

    def list_metadata(self) -> list[dict[str, Any]]:
        return self._registry.list_metadata()

    def provider_health(self, name: str) -> dict[str, Any]:
        return self._registry.health_status(name)

    def capabilities(self, name: str | None = None) -> Any:
        from backend.ai.providers.base.capabilities import ProviderCapabilities
        provider = self._registry.get(name) if name else self._get_healthy_provider()
        if provider is None:
            return ProviderCapabilities()
        return provider.capabilities

    def metrics_snapshot(self) -> dict[str, dict[str, Any]]:
        return self._metrics.snapshot()

    @property
    def registry(self) -> ProviderRegistry:
        return self._registry

    @property
    def config_manager(self) -> ProviderConfigManager:
        return self._config_mgr

    def get_provider_config(self, name: str | None = None) -> ProviderConfig:
        """Return the ProviderConfig for a provider (active if name is None)."""
        if name is None:
            return self._config_mgr.get_active_config()
        return self._config_mgr.get_config(name)

    def update_provider_config(self, name: str, field: str, value: Any) -> Any:
        """Update a provider config field. Returns the ValidationResult."""
        return self._config_mgr.update(name, field, value)

    def reset_provider_config(self, name: str) -> ProviderConfig:
        """Reset a provider's config to factory defaults."""
        return self._config_mgr.reset(name)

    def validate_provider(self, name: str) -> Any:
        """Validate a provider's config. Returns ValidationResult."""
        return self._config_mgr.validate(name)

    def export_configs(self) -> dict[str, dict[str, Any]]:
        """Export all provider configs as dicts."""
        return self._config_mgr.export()

    # ── Internal ──

    def _get_healthy_provider(self) -> BaseProvider:
        """Return the active provider if healthy, else the fallback."""
        provider = self._registry.get_active()
        try:
            h = provider.health()
            if h.get("healthy", False):
                return provider
        except Exception:
            pass
        logger.warning("ProviderManager: active provider '%s' unhealthy, falling back", provider.name)
        return self._registry.get_fallback()

    async def _fallback(
        self,
        messages: list[dict[str, Any]],
        failures: list[str] | None = None,
        **kwargs: Any,
    ) -> ProviderResponse:
        """Emergency fallback — always returns success=False with diagnostics.

        Even though the dummy provider "succeeds" deterministically, a
        fallback after real provider failures must NEVER look like a
        successful AI answer. The original failure information is preserved.
        """
        fallback = self._registry.get_fallback()
        errors = list(failures or [])
        detail = "; ".join(errors) if errors else "no additional provider error information"
        try:
            await guarded_await(
                fallback.chat(messages, **kwargs),
                name=f"provider:fallback:{fallback.name}",
                timeout=_PROVIDER_RPC_TIMEOUT,
            )
        except Exception as exc:
            logger.error("ProviderManager: FALLBACK CRASHED: %s", exc)
            errors.append(f"fallback: {type(exc).__name__}: {exc}")
        return ProviderResponse(
            text=f"All AI providers failed. Last error: {detail}",
            provider_name=fallback.name,
            success=False,
            metadata={
                "fallback": True,
                "emergency": True,
                "fallback_exhausted": True,
                "errors": errors,
                "fallback_chain_tried": list(self._fallback_chain),
            },
        )

    def _fallback_vision(self, messages: list[dict[str, Any]], images: list[bytes], **kwargs: Any) -> ProviderResponse:
        fallback = self._registry.get_fallback()
        try:
            return fallback.vision(messages, images, **kwargs)
        except Exception as exc:
            logger.error("ProviderManager: FALLBACK VISION CRASHED: %s", exc)
            return ProviderResponse(
                text=f"All AI providers failed. Last error: {exc}",
                provider_name=fallback.name,
                success=False,
                metadata={"fallback": True, "emergency": True, "fallback_exhausted": True},
            )

    def _fallback_stream(self, messages: list[dict[str, Any]], **kwargs: Any) -> Iterator[ProviderResponse]:
        fallback = self._registry.get_fallback()
        try:
            yield from fallback.stream(messages, **kwargs)
        except Exception as exc:
            logger.error("ProviderManager: FALLBACK STREAM CRASHED: %s", exc)
            yield ProviderResponse(
                text=f"All AI providers failed. Last error: {exc}",
                provider_name=fallback.name,
                success=False,
                metadata={"fallback": True, "emergency": True, "fallback_exhausted": True},
            )

    def _ensure_dummy_fallback(self) -> None:
        from backend.ai.providers.dummy.provider import DummyProvider
        if not self._registry.has("dummy"):
            dummy = DummyProvider()
            self._registry.register(dummy)
        self._registry.set_fallback("dummy")

    def _load_env_fallback_chain(self) -> None:
        import os
        chain_str = os.getenv("AI_PROVIDER_FALLBACK", "")
        if chain_str:
            self._fallback_chain = [p.strip() for p in chain_str.split(",") if p.strip()]
            logger.info("ProviderManager: fallback chain = %s", self._fallback_chain)

    async def _try_fallback_chain(
        self,
        messages: list[dict[str, Any]],
        original_provider: str = "",
        initial_failure: str = "",
        **kwargs: Any,
    ) -> ProviderResponse:
        """Try each provider in the fallback chain before the emergency fallback.

        Both hard exceptions and ``success=False`` responses from a chain
        member are treated as failures and advance the chain. The provider
        that already failed in this request is skipped so a failure can
        never loop back into itself.
        """
        failures: list[str] = []
        if initial_failure:
            failures.append(initial_failure)
        for name in self._fallback_chain:
            if name == original_provider:
                continue
            if not self._registry.has(name):
                continue
            provider = self._registry.get(name)
            start = time.perf_counter()
            try:
                h = provider.health()
                if not h.get("healthy", False):
                    failures.append(f"{name}: unhealthy (skipped)")
                    continue
                response = await guarded_await(
                    provider.chat(messages, **kwargs),
                    name=f"provider:fallback_chain:{name}",
                    timeout=_PROVIDER_RPC_TIMEOUT,
                )
                latency = time.perf_counter() - start
                if response.success:
                    self._metrics.record(name, latency=latency, error="")
                    merged = dict(response.metadata or {})
                    merged["fallback"] = True
                    merged["fallback_from"] = original_provider or None
                    logger.info("ProviderManager: fallback chain succeeded with '%s'", name)
                    from dataclasses import replace
                    return replace(response, metadata=merged)
                self._metrics.record(name, latency=latency, error=response.text[:200])
                failures.append(f"{name}: success=False ({response.text[:200]})")
                logger.warning(
                    "ProviderManager: fallback chain provider '%s' returned success=False",
                    name,
                )
                continue
            except Exception as exc:
                latency = time.perf_counter() - start
                self._metrics.record(name, latency=latency, error=str(exc))
                failures.append(f"{name}: {type(exc).__name__}: {exc}")
                logger.warning("ProviderManager: fallback chain provider '%s' failed: %s", name, exc)
                continue
        return await self._fallback(messages, failures=failures, **kwargs)
