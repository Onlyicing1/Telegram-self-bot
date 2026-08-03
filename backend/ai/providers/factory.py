"""
ProviderFactory — creates and wires providers from configuration.

The factory is the single place that knows which provider classes
exist. It builds a ``ProviderRegistry``, pre-registers the dummy
fallback, optionally creates and registers additional providers
based on config, and returns a fully wired ``ProviderManager``.

Future providers are added by:
  1. Creating a ``backend/ai/providers/<name>/`` package.
  2. Adding the class to ``_PROVIDER_CLASSES`` below.
  3. Done.
"""
from __future__ import annotations

import logging
from typing import Any, Type

from backend.ai.providers.base.config import ProviderConfig
from backend.ai.providers.base.contract import BaseProvider
from backend.ai.providers.base.exceptions import ProviderNotFound
from backend.ai.providers.dummy.provider import DummyProvider
from backend.ai.providers.gemini import GeminiProvider
from backend.ai.providers.manager.manager import ProviderManager
from backend.ai.providers.openai import OpenAIProvider
from backend.ai.providers.openrouter import OpenRouterProvider
from backend.ai.providers.registry.registry import ProviderRegistry

logger = logging.getLogger(__name__)

_PROVIDER_CLASSES: dict[str, Type[BaseProvider]] = {
    "dummy": DummyProvider,
    "gemini": GeminiProvider,
    "openai": OpenAIProvider,
    "openrouter": OpenRouterProvider,
}


class ProviderFactory:
    """Creates provider instances and wired managers from config."""

    __slots__ = ()

    @staticmethod
    def available_providers() -> list[str]:
        return list(_PROVIDER_CLASSES.keys())

    @staticmethod
    def create_provider(name: str, config: ProviderConfig | None = None) -> BaseProvider:
        cls = _PROVIDER_CLASSES.get(name)
        if cls is None:
            raise ProviderNotFound(
                f"Unknown provider '{name}'. Available: {list(_PROVIDER_CLASSES.keys())}"
            )
        if config is None:
            config = ProviderConfig(name=name)
        provider = cls(config)
        logger.info("ProviderFactory: created provider '%s'", name)
        return provider

    @staticmethod
    def create_registry(config: dict[str, Any] | None = None) -> ProviderRegistry:
        registry = ProviderRegistry()
        dummy = DummyProvider()
        registry.register(dummy)
        registry.set_fallback(dummy.name)

        if config is None:
            logger.info("ProviderFactory: created registry with default only (dummy)")
            return registry

        provider_name = config.get("provider", "")
        if provider_name and provider_name != "dummy":
            provider_config = ProviderConfig(
                name=provider_name,
                model=config.get("model", ""),
                temperature=config.get("temperature", 1.0),
                max_output_tokens=config.get("max_output_tokens", 4096),
                top_p=config.get("top_p", 1.0),
                timeout=config.get("timeout", 30.0),
                retry_count=config.get("retry_count", 3),
                enabled=config.get("enabled", False),
                api_key=config.get("api_key", ""),
                extra=config.get("extra", {}),
            )
            try:
                provider = ProviderFactory.create_provider(provider_name, provider_config)
                registry.register(provider)
            except ProviderNotFound as exc:
                logger.warning("ProviderFactory: could not create provider '%s': %s", provider_name, exc)

        logger.info("ProviderFactory: created registry with providers %s", registry.list())
        return registry

    @staticmethod
    def create_manager(config: dict[str, Any] | None = None) -> ProviderManager:
        registry = ProviderFactory.create_registry(config)
        manager = ProviderManager(registry)
        logger.info(
            "ProviderFactory: created manager (active='%s', providers=%s)",
            manager.get_active_name(),
            manager.list_providers(),
        )
        return manager
