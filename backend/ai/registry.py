"""
ProviderRegistry — dependency-injected registry of AI providers.

No globals. No singletons. The registry is constructed once by the
runtime supervisor and injected wherever needed. Providers register
themselves by name; the registry tracks which provider is active.

Usage (inside the runtime, not in this file):

    registry = ProviderRegistry()
    registry.register(MyProvider())
    registry.activate("my_provider")

The ``AIInterface`` receives the registry and delegates to the active
provider. If no provider is active or the registry is empty, the
interface returns ``AI_DISABLED``.
"""
from __future__ import annotations

import logging
from typing import Protocol

from backend.ai.provider import AIProvider

logger = logging.getLogger(__name__)


class ProviderRegistry:
    """Registry of available AI providers.

    Providers are stored by their ``name`` property. Only one provider
    can be active at a time. The registry does not instantiate providers
    — it receives already-constructed instances (dependency injection).
    """

    __slots__ = ("_providers", "_active_name")

    def __init__(self) -> None:
        self._providers: dict[str, AIProvider] = {}
        self._active_name: str = ""

    def register(self, provider: AIProvider) -> None:
        """Register a provider instance by its ``name`` property."""
        name = provider.name
        if name in self._providers:
            logger.warning("ProviderRegistry: overwriting existing provider '%s'", name)
        self._providers[name] = provider
        logger.info("ProviderRegistry: registered provider '%s' (total=%d)", name, len(self._providers))

    def unregister(self, name: str) -> None:
        """Remove a provider by name. If it was active, deactivates."""
        if name == self._active_name:
            self._active_name = ""
        self._providers.pop(name, None)
        logger.info("ProviderRegistry: unregistered provider '%s'", name)

    def activate(self, name: str) -> bool:
        """Set the active provider by name. Returns True on success."""
        if name not in self._providers:
            logger.warning("ProviderRegistry: cannot activate '%s' — not registered", name)
            return False
        self._active_name = name
        logger.info("ProviderRegistry: activated provider '%s'", name)
        return True

    def deactivate(self) -> None:
        """Clear the active provider (no provider will be used)."""
        self._active_name = ""

    def get_active(self) -> AIProvider | None:
        """Return the active provider, or None if no provider is active."""
        if not self._active_name:
            return None
        return self._providers.get(self._active_name)

    def get_active_name(self) -> str:
        """Return the name of the active provider, or empty string."""
        return self._active_name

    def has_provider(self, name: str) -> bool:
        """Check if a provider is registered by name."""
        return name in self._providers

    def available_names(self) -> list[str]:
        """Return a list of all registered provider names."""
        return list(self._providers.keys())

    def is_empty(self) -> bool:
        """True if no providers are registered."""
        return len(self._providers) == 0
