"""
ProviderFactory — creates and wires AI providers from configuration.

The factory is the single place in the codebase that maps a provider
name (from configuration) to a concrete provider class. No other file
should contain provider-construction logic. This keeps provider
selection centralized and avoids switch/if-chains scattered across
the project.

The factory:
  1. Receives a configuration dict (or a ``ProviderConfig``).
  2. Maps the ``name`` field to a concrete provider class.
  3. Instantiates the provider with its config.
  4. Registers it in a ``ProviderRegistry``.
  5. Sets the default provider to Dummy (always).

Currently, every provider returns ``NOT_IMPLEMENTED`` or
``AI_DISABLED``. The factory does not enable any provider. It only
builds the architecture so future configuration can flip providers on.

Future example::

    config = {
        "provider": "gemini",
        "model": "gemini-2.0-flash",
        "temperature": 0.7,
        "max_output_tokens": 4096,
        "top_p": 1.0,
        "timeout": 30,
        "retry_count": 3,
        "enabled": False,          # stays off for now
        "api_key": "",             # placeholder, not used yet
    }

    registry = factory.create_registry(config)
    # registry.list()  → ["dummy", "gemini"]
    # registry.default_provider()  → DummyProvider
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Type

from backend.ai.providers.base import BaseProvider, ProviderConfig
from backend.ai.providers.dummy import DummyProvider
from backend.ai.providers.gemini import GeminiProvider
from backend.ai.providers.openai import OpenAIProvider
from backend.ai.providers.openrouter import OpenRouterProvider
from backend.ai.providers.registry import ProviderRegistry

logger = logging.getLogger(__name__)

# ── Provider name → class mapping ──
# This is the ONLY place that maps a string name to a concrete class.
# Adding a new provider means: (1) create the provider file, (2) add
# one entry here. Nothing else changes.
_PROVIDER_CLASSES: Dict[str, Type[BaseProvider]] = {
    "dummy": DummyProvider,
    "gemini": GeminiProvider,
    "openai": OpenAIProvider,
    "openrouter": OpenRouterProvider,
}


class ProviderFactory:
    """Factory that creates providers and registries from configuration.

    Stateless. No globals. Receives configuration, returns constructed
    objects. The factory never makes network calls — it only instantiates
    provider classes.
    """

    __slots__ = ()

    @staticmethod
    def available_providers() -> list[str]:
        """Return the list of provider names the factory can build."""
        return list(_PROVIDER_CLASSES.keys())

    @staticmethod
    def create_provider(
        name: str,
        config: ProviderConfig | None = None,
    ) -> BaseProvider:
        """Create a single provider instance by name.

        Args:
            name:   Provider identifier (e.g. ``"gemini"``).
            config: Optional ``ProviderConfig``. If ``None``, a default
                    config with the provider's name is used.

        Returns:
            A concrete ``BaseProvider`` instance.

        Raises:
            ``ValueError`` if the name is not a known provider.
        """
        cls = _PROVIDER_CLASSES.get(name)
        if cls is None:
            raise ValueError(
                f"Unknown provider '{name}'. "
                f"Available: {list(_PROVIDER_CLASSES.keys())}"
            )
        if config is None:
            config = ProviderConfig(name=name)
        provider = cls(config)
        logger.info("ProviderFactory: created provider '%s'", name)
        return provider

    @staticmethod
    def create_registry(
        config: Dict[str, Any] | None = None,
    ) -> ProviderRegistry:
        """Build a ``ProviderRegistry`` from a configuration dict.

        The registry always starts with the DummyProvider as the
        default. If ``config`` contains a ``"provider"`` key, that
        provider is also created and registered (but NOT activated —
        the default remains Dummy until explicitly changed).

        Configuration keys (all optional, all unused for now):
            provider          — str,   provider name to register
            model             — str,   model identifier
            temperature       — float, sampling temperature
            max_output_tokens — int,   max tokens to generate
            top_p             — float, nucleus sampling
            timeout           — float, request timeout in seconds
            retry_count       — int,   retries on transient failure
            enabled           — bool,  whether the provider is enabled
            api_key           — str,   API key placeholder (not used)

        Args:
            config: Configuration dict. If ``None``, only the default
                    DummyProvider is registered.

        Returns:
            A ``ProviderRegistry`` with at least the DummyProvider.
        """
        registry = ProviderRegistry()

        # Always register the DummyProvider first and set it as default.
        dummy = DummyProvider()
        registry.register(dummy)
        registry.set_default(dummy.name)

        if config is None:
            logger.info(
                "ProviderFactory: created registry with default only (dummy)"
            )
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
                provider = ProviderFactory.create_provider(
                    provider_name, provider_config
                )
                registry.register(provider)
            except ValueError as exc:
                logger.warning(
                    "ProviderFactory: could not create provider '%s': %s",
                    provider_name,
                    exc,
                )

        logger.info(
            "ProviderFactory: created registry with providers %s",
            registry.list(),
        )
        return registry
