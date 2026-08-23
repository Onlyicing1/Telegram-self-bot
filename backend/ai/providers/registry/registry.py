"""
ProviderRegistry — the authoritative store of registered providers.

Responsibilities:
  - register(provider)       → add a provider instance
  - unregister(name)          → remove a provider (never the fallback)
  - get_active()              → return the currently active provider
  - switch_provider(name)     → set a new active provider
  - validate_provider(name)   → check health + enabled
  - health_status(name)       → dict with healthy/enabled/reason
  - list()                    → all registered provider names
  - list_metadata()           → metadata for every provider

The registry never calls ``chat()`` or ``generate()``. It only stores
instances and reports their health. The ``ProviderManager`` is
responsible for routing requests and handling fallbacks.

The registry guarantees a ``dummy`` fallback is always present. If
``get_active()`` finds the active provider unhealthy, it does NOT
fall back itself — that is the manager's job. The registry is pure
storage.
"""
from __future__ import annotations

import logging
from typing import Any

from backend.ai.providers.base.contract import BaseProvider

logger = logging.getLogger(__name__)


class ProviderRegistry:
    """Registry of provider instances with active-provider tracking."""

    __slots__ = ("_providers", "_active_name", "_fallback_name")

    def __init__(self) -> None:
        self._providers: dict[str, BaseProvider] = {}
        self._active_name: str = ""
        self._fallback_name: str = ""

    # ── Registration ──

    def register(self, provider: BaseProvider) -> bool:
        name = provider.name
        if name in self._providers:
            logger.warning("ProviderRegistry: duplicate registration '%s' rejected", name)
            return False
        self._providers[name] = provider
        if not self._active_name:
            self._active_name = name
        logger.info("ProviderRegistry: registered '%s' (total=%d)", name, len(self._providers))
        return True

    def unregister(self, name: str) -> bool:
        if name == self._fallback_name:
            logger.warning("ProviderRegistry: cannot unregister fallback '%s'", name)
            return False
        if name not in self._providers:
            logger.warning("ProviderRegistry: '%s' not registered", name)
            return False
        self._providers.pop(name)
        if self._active_name == name:
            self._active_name = self._fallback_name or next(iter(self._providers), "")
        logger.info("ProviderRegistry: unregistered '%s' (total=%d)", name, len(self._providers))
        return True

    # ── Lookup ──

    def get(self, name: str) -> BaseProvider | None:
        return self._providers.get(name)

    def has(self, name: str) -> bool:
        return name in self._providers

    def list(self) -> list[str]:
        return list(self._providers.keys())

    def list_metadata(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for name, provider in self._providers.items():
            try:
                h = provider.health()
            except Exception:
                h = {"healthy": False}
            capability_kind = getattr(type(provider), "CAPABILITY_KIND", "chat") or "chat"
            result.append({
                "name": name,
                "display_name": getattr(provider, "display_name", name),
                "version": provider.provider_version(),
                "enabled": provider.is_enabled,
                "healthy": h.get("healthy", False),
                "configured": h.get("configured", provider.is_enabled),
                "active": name == self._active_name,
                "is_fallback": name == self._fallback_name,
                "capability_kind": capability_kind,
                "capabilities": provider.capabilities.as_dict(),
            })
        return result

    # ── Active provider ──

    def get_active(self) -> BaseProvider:
        provider = self._providers.get(self._active_name)
        if provider is None:
            provider = self._ensure_fallback()
        return provider

    def switch_provider(self, name: str) -> bool:
        if name not in self._providers:
            logger.warning("ProviderRegistry: cannot switch to '%s' — not registered", name)
            return False
        self._active_name = name
        logger.info("ProviderRegistry: active provider switched to '%s'", name)
        return True

    @property
    def active_name(self) -> str:
        return self._active_name

    # ── Fallback ──

    def set_fallback(self, name: str) -> bool:
        if name not in self._providers:
            return False
        self._fallback_name = name
        logger.info("ProviderRegistry: fallback set to '%s'", name)
        return True

    @property
    def fallback_name(self) -> str:
        return self._fallback_name

    def get_fallback(self) -> BaseProvider:
        provider = self._providers.get(self._fallback_name)
        if provider is None:
            provider = self._ensure_fallback()
        return provider

    # ── Validation ──

    def validate_provider(self, name: str) -> bool:
        provider = self._providers.get(name)
        if provider is None:
            return False
        try:
            h = provider.health()
            return bool(h.get("healthy", False)) and provider.is_enabled
        except Exception:
            return False

    def health_status(self, name: str) -> dict[str, Any]:
        provider = self._providers.get(name)
        if provider is None:
            return {"name": name, "healthy": False, "enabled": False, "reason": "not registered"}
        try:
            h = provider.health()
            return {
                "name": name,
                "healthy": h.get("healthy", False),
                "enabled": provider.is_enabled,
                "version": provider.provider_version(),
                "reason": h.get("reason", ""),
            }
        except Exception as exc:
            return {"name": name, "healthy": False, "enabled": False, "reason": str(exc)}

    # ── Convenience ──

    def is_empty(self) -> bool:
        return len(self._providers) == 0

    def default_provider(self) -> BaseProvider:
        return self.get_active()

    def set_default(self, name: str) -> bool:
        return self.switch_provider(name)

    def metadata(self, name: str) -> dict[str, Any]:
        provider = self._providers.get(name)
        if provider is None:
            return {}
        try:
            h = provider.health()
        except Exception:
            h = {"healthy": False}
        return {
            "name": provider.name,
            "version": provider.provider_version(),
            "enabled": provider.is_enabled,
            "healthy": h.get("healthy", False),
        }

    # ── Internal ──

    def _ensure_fallback(self) -> BaseProvider:
        from backend.ai.providers.dummy.provider import DummyProvider
        provider = DummyProvider()
        self._providers[provider.name] = provider
        self._fallback_name = provider.name
        if not self._active_name:
            self._active_name = provider.name
        return provider
