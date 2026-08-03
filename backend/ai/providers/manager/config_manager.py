"""
ProviderConfigManager — the sole owner of provider configuration.

Responsibilities:
  - load()         — load configs from ConfigManager snapshot
  - validate()     — validate a specific provider's config
  - update()       — update a provider's config field
  - reset()        — reset a provider to factory defaults
  - export()       — export a snapshot of all provider configs
  - get_config()   — get a ProviderConfig by provider name

This manager bridges the existing ConfigManager (which owns the
top-level AI settings) and the provider layer (which needs
ProviderConfig objects). It reads the active provider name from
ConfigManager and produces ProviderConfig objects on demand.

No persistence. Everything lives in RAM.
"""
from __future__ import annotations

import copy
import logging
from typing import Any

from backend.ai.providers.base.config import ProviderConfig
from backend.ai.providers.base.defaults import get_provider_default, list_provider_names
from backend.ai.providers.base.validation import ValidationResult, validate_provider_config

logger = logging.getLogger(__name__)


class ProviderConfigManager:
    """Manages ProviderConfig objects for all registered providers.

    Holds one ProviderConfig per provider name in RAM. The
    ``ProviderManager`` calls ``get_config(name)`` to get the config
    for a specific provider — it never receives raw values.
    """

    __slots__ = ("_configs", "_active_name")

    def __init__(self) -> None:
        self._configs: dict[str, ProviderConfig] = {}
        self._active_name: str = "dummy"
        self._load_all_defaults()

    # ── Load ──

    def _load_all_defaults(self) -> None:
        """Load factory defaults for every known provider."""
        for name in list_provider_names():
            self._configs[name] = get_provider_default(name)

    def load(self, config_manager: Any) -> None:
        """Load provider configs from a ConfigManager snapshot.

        Reads the active provider name and top-level settings from the
        ConfigManager and applies them to the matching ProviderConfig.
        """
        snap = config_manager.snapshot()
        self._active_name = snap.provider

        active = self._configs.get(self._active_name)
        if active is not None:
            active.temperature = snap.temperature
            active.top_p = snap.top_p
            active.max_tokens = snap.max_tokens
            active.timeout = snap.timeout
            active.retry_count = snap.retry_count
            active.enabled = snap.enabled
            if snap.model:
                active.default_model = snap.model

    # ── Get ──

    def get_config(self, name: str) -> ProviderConfig:
        """Return the ProviderConfig for ``name``. Creates one if missing."""
        if name not in self._configs:
            self._configs[name] = get_provider_default(name)
        return self._configs[name]

    def get_active_config(self) -> ProviderConfig:
        """Return the ProviderConfig for the currently active provider."""
        return self.get_config(self._active_name)

    @property
    def active_name(self) -> str:
        return self._active_name

    # ── Update ──

    def update(self, name: str, field: str, value: Any) -> ValidationResult:
        """Update a single field on a provider's config.

        Returns the ValidationResult after the update. If the update
        is invalid, the field is NOT changed.
        """
        config = self.get_config(name)
        if not hasattr(config, field):
            result = ValidationResult(valid=False, provider_name=name)
            result.add(field, "UNKNOWN_FIELD", f"Unknown config field: {field!r}")
            return result

        old_value = getattr(config, field)
        setattr(config, field, value)
        result = validate_provider_config(config)
        if not result.valid:
            setattr(config, field, old_value)
            logger.warning("ProviderConfigManager: rejected update %s.%s=%r — %s", name, field, value, result.errors)
        else:
            logger.info("ProviderConfigManager: updated %s.%s → %r", name, field, value)
        return result

    def update_many(self, name: str, updates: dict[str, Any]) -> ValidationResult:
        """Update multiple fields at once. All-or-nothing."""
        config = self.get_config(name)
        old_values = {}
        for field, value in updates.items():
            if hasattr(config, field):
                old_values[field] = getattr(config, field)
                setattr(config, field, value)
        result = validate_provider_config(config)
        if not result.valid:
            for field, old_value in old_values.items():
                setattr(config, field, old_value)
            logger.warning("ProviderConfigManager: rejected batch update for '%s' — %s", name, result.errors)
        else:
            logger.info("ProviderConfigManager: batch update '%s' — %d fields", name, len(updates))
        return result

    # ── Reset ──

    def reset(self, name: str) -> ProviderConfig:
        """Reset a provider's config to factory defaults."""
        fresh = get_provider_default(name)
        self._configs[name] = fresh
        logger.info("ProviderConfigManager: reset '%s' to defaults", name)
        return fresh

    def reset_all(self) -> None:
        """Reset all provider configs to factory defaults."""
        self._configs.clear()
        self._load_all_defaults()
        logger.info("ProviderConfigManager: reset all providers to defaults")

    # ── Validate ──

    def validate(self, name: str) -> ValidationResult:
        """Validate a provider's current config."""
        return validate_provider_config(self.get_config(name))

    def validate_all(self) -> dict[str, ValidationResult]:
        """Validate all provider configs. Returns name→result dict."""
        return {name: validate_provider_config(config) for name, config in self._configs.items()}

    # ── Export ──

    def export(self) -> dict[str, dict[str, Any]]:
        """Export a snapshot of all provider configs as plain dicts."""
        return {name: config.as_dict() for name, config in self._configs.items()}

    def export_active(self) -> dict[str, Any]:
        """Export only the active provider's config as a dict."""
        return self.get_active_config().as_dict()

    # ── List ──

    def list_providers(self) -> list[str]:
        return list(self._configs.keys())

    def list_provider_names(self) -> list[str]:
        return list_provider_names()

    # ── Sync ──

    def sync_to_config_manager(self, config_manager: Any) -> None:
        """Push the active provider's config back to ConfigManager.

        Called after a provider config update that should be reflected
        in the top-level ConfigManager (e.g. temperature, max_tokens).
        """
        config = self.get_active_config()
        snap = config_manager.snapshot()
        if config.temperature != snap.temperature:
            config_manager.set("temperature", config.temperature)
        if config.top_p != snap.top_p:
            config_manager.set("top_p", config.top_p)
        if config.max_tokens != snap.max_tokens:
            config_manager.set("max_tokens", config.max_tokens)
        if config.timeout != snap.timeout:
            config_manager.set("timeout", config.timeout)
        if config.retry_count != snap.retry_count:
            config_manager.set("retry_count", config.retry_count)
        if config.enabled != snap.enabled:
            config_manager.set("enabled", config.enabled)
        if config.default_model and config.default_model != snap.model:
            config_manager.set("model", config.default_model)


# ── Module-level singleton ──

_default_manager: ProviderConfigManager | None = None


def get_provider_config_manager() -> ProviderConfigManager:
    """Return the process-wide default ProviderConfigManager."""
    global _default_manager
    if _default_manager is None:
        _default_manager = ProviderConfigManager()
    return _default_manager
