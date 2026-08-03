"""
ProviderRegistry — registry of AI providers in the Provider Layer.

This registry is separate from the interface-level ``ProviderRegistry``
in ``backend/ai/registry.py``. That registry tracks the abstract
``AIProvider`` protocol used by ``AIInterface``. This registry tracks
concrete ``BaseProvider`` subclasses that accept ``PromptPackage``
objects.

The registry supports:
  - ``register(provider)``      — add a provider by its ``name``.
  - ``unregister(name)``         — remove a provider by name.
  - ``list()``                   — list all registered provider names.
  - ``get(name)``                — retrieve a provider by name.
  - ``default_provider()``       — return the default provider (Dummy).

No globals, no singletons. The registry is constructed once and
injected wherever needed. The DummyProvider is pre-registered as the
default so the system always has a safe fallback.
"""
from __future__ import annotations

import logging
from typing import Dict, List

from backend.ai.providers.base import BaseProvider

logger = logging.getLogger(__name__)


class ProviderRegistry:
    """Registry of concrete AI providers.

    Providers are stored by their ``name`` property. The registry always
    has the DummyProvider pre-registered under the name ``"dummy"`` so
    that ``default_provider()`` never returns ``None``.

    Usage::

        registry = ProviderRegistry()
        registry.register(GeminiProvider(config))
        registry.list()               # ["dummy", "gemini"]
        provider = registry.get("gemini")
    """

    __slots__ = ("_providers", "_default_name")

    def __init__(self) -> None:
        self._providers: Dict[str, BaseProvider] = {}
        self._default_name: str = ""

    def register(self, provider: BaseProvider) -> None:
        """Register a provider instance by its ``name`` property.

        If a provider with the same name already exists, it is
        overwritten with a warning.
        """
        name = provider.name
        if name in self._providers:
            logger.warning(
                "ProviderRegistry: overwriting existing provider '%s'", name
            )
        self._providers[name] = provider
        logger.info(
            "ProviderRegistry: registered '%s' (total=%d)",
            name,
            len(self._providers),
        )

    def unregister(self, name: str) -> bool:
        """Remove a provider by name.

        The default provider (``"dummy"``) cannot be unregistered — it
        is the system's safety net.

        Returns:
            ``True`` if the provider was removed, ``False`` if it was
            not found or is the default.
        """
        if name == self._default_name:
            logger.warning(
                "ProviderRegistry: cannot unregister default provider '%s'",
                name,
            )
            return False
        if name not in self._providers:
            logger.warning(
                "ProviderRegistry: cannot unregister '%s' — not registered",
                name,
            )
            return False
        self._providers.pop(name)
        logger.info(
            "ProviderRegistry: unregistered '%s' (total=%d)",
            name,
            len(self._providers),
        )
        return True

    def list(self) -> List[str]:
        """Return a list of all registered provider names."""
        return list(self._providers.keys())

    def get(self, name: str) -> BaseProvider | None:
        """Return the provider with the given name, or ``None``."""
        return self._providers.get(name)

    def has(self, name: str) -> bool:
        """Check whether a provider is registered by name."""
        return name in self._providers

    def default_provider(self) -> BaseProvider:
        """Return the default provider.

        The default is always the DummyProvider. It is guaranteed to
        exist — if it was somehow removed, a fresh instance is created
        and registered.
        """
        provider = self._providers.get(self._default_name)
        if provider is None:
            from backend.ai.providers.dummy import DummyProvider

            provider = DummyProvider()
            self.register(provider)
            self._default_name = provider.name
        return provider

    def set_default(self, name: str) -> bool:
        """Set the default provider by name.

        Returns ``True`` on success, ``False`` if the name is not
        registered.
        """
        if name not in self._providers:
            logger.warning(
                "ProviderRegistry: cannot set default to '%s' — not registered",
                name,
            )
            return False
        self._default_name = name
        logger.info("ProviderRegistry: default provider set to '%s'", name)
        return True

    def is_empty(self) -> bool:
        """True if no providers are registered (excluding the default)."""
        return len(self._providers) == 0
