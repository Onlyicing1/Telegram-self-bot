"""
ConfigManager — the sole owner of AIConfig in RAM.

Responsibilities:
  - ``load_default()``      — reset to factory defaults.
  - ``get(field)``          — read a single field.
  - ``set(field, value)``   — validate and update a single field.
  - ``reset()``             — same as ``load_default()``.
  - ``clone()``             — return a deep copy of the current config.
  - ``validate()``          — validate the entire config at once.
  - ``snapshot()``          — produce an immutable ``ConfigSnapshot``.
  - ``apply_overrides()``   — apply provider-specific overrides.

No persistence. Everything lives in RAM for the process lifetime.
The Engine receives only ``ConfigSnapshot`` objects — never the
mutable ``AIConfig``.

Provider-specific overrides:
  A provider can register override defaults (e.g. Gemini uses
  temperature=0.9, GPT uses timeout=60). At snapshot time, the manager
  applies the override for the currently-selected provider on top of
  the base config. The base config is never mutated — overrides are
  applied on the snapshot copy only.

  This is architecture-only for now: the override registry is empty
  by default. Future providers register overrides via
  ``register_provider_overrides()``.
"""
from __future__ import annotations

import copy
import logging
from typing import Any, Callable, Dict

from backend.ai.config.config import AIConfig
from backend.ai.config.defaults import (
    DEFAULT_DEVELOPER_MODE,
    DEFAULT_ENABLED,
    DEFAULT_HISTORY_BUDGET,
    DEFAULT_MODEL,
    DEFAULT_PROVIDER,
    DEFAULT_REASONING_ENABLED,
    DEFAULT_RETRY_COUNT,
    DEFAULT_STREAMING_ENABLED,
    DEFAULT_SYSTEM_PROMPT,
    DEFAULT_TEMPERATURE,
    DEFAULT_TIMEOUT,
    DEFAULT_TOOL_BUDGET,
    DEFAULT_TOP_P,
    DEFAULT_VISION_ENABLED,
)
from backend.ai.config.schema import ConfigSnapshot
from backend.ai.config.validation import ConfigValidationError, validate_all, validate_field

logger = logging.getLogger(__name__)

ProviderCheckFn = Callable[[str], bool] | None


def _default_config() -> AIConfig:
    return AIConfig(
        enabled=DEFAULT_ENABLED,
        provider=DEFAULT_PROVIDER,
        model=DEFAULT_MODEL,
        temperature=DEFAULT_TEMPERATURE,
        top_p=DEFAULT_TOP_P,
        max_tokens=4096,
        timeout=DEFAULT_TIMEOUT,
        retry_count=DEFAULT_RETRY_COUNT,
        system_prompt=DEFAULT_SYSTEM_PROMPT,
        history_budget=DEFAULT_HISTORY_BUDGET,
        tool_budget=DEFAULT_TOOL_BUDGET,
        streaming_enabled=DEFAULT_STREAMING_ENABLED,
        vision_enabled=DEFAULT_VISION_ENABLED,
        reasoning_enabled=DEFAULT_REASONING_ENABLED,
        developer_mode=DEFAULT_DEVELOPER_MODE,
    )


class ConfigManager:
    """RAM-only AI configuration manager.

    Owns a single ``AIConfig`` instance. All reads go through ``get()``
    or ``snapshot()``. All writes go through ``set()`` and are validated
    before being applied.
    """

    __slots__ = ("_config", "_provider_check", "_overrides")

    def __init__(
        self,
        provider_check: ProviderCheckFn = None,
    ) -> None:
        self._config: AIConfig = _default_config()
        self._provider_check: ProviderCheckFn = provider_check
        self._overrides: Dict[str, dict[str, Any]] = {}

    # ── Public API ──

    def load_default(self) -> None:
        """Reset the config to factory defaults."""
        self._config = _default_config()

    def reset(self) -> None:
        """Alias for ``load_default()``."""
        self.load_default()

    def get(self, field: str) -> Any:
        """Read a single config field by name."""
        if not hasattr(self._config, field):
            raise KeyError(f"Unknown config field: {field!r}")
        return getattr(self._config, field)

    def set(self, field: str, value: Any) -> None:
        """Validate and update a single config field.

        Raises ``ConfigValidationError`` on invalid values.
        """
        coerced = validate_field(field, value, self._provider_check)
        setattr(self._config, field, coerced)

    def set_many(self, updates: dict[str, Any]) -> None:
        """Validate and update multiple fields atomically.

        If any field is invalid, no fields are updated.
        """
        validated = validate_all(updates, self._provider_check)
        for field, value in validated.items():
            setattr(self._config, field, value)

    def clone(self) -> "ConfigManager":
        """Return a deep-copy ConfigManager with the same config."""
        new_mgr = ConfigManager(provider_check=self._provider_check)
        new_mgr._config = copy.deepcopy(self._config)
        new_mgr._overrides = copy.deepcopy(self._overrides)
        return new_mgr

    def validate(self) -> list[str]:
        """Validate the entire config. Returns a list of error messages (empty if valid)."""
        errors: list[str] = []
        for field, value in self._config.as_dict().items():
            try:
                validate_field(field, value, self._provider_check)
            except ConfigValidationError as exc:
                errors.append(str(exc))
        return errors

    def snapshot(self) -> ConfigSnapshot:
        """Produce an immutable ``ConfigSnapshot`` from the current config.

        Provider-specific overrides are applied on the snapshot — the
        base ``AIConfig`` is never mutated.
        """
        data = self._config.as_dict()

        overrides = self._overrides.get(self._config.provider, {})
        for field, value in overrides.items():
            if field in data:
                data[field] = value

        return ConfigSnapshot(**data)

    # ── Provider overrides (architecture-only, no implementations) ──

    def register_provider_overrides(self, provider_name: str, overrides: dict[str, Any]) -> None:
        """Register provider-specific override defaults.

        Example (future)::

            manager.register_provider_overrides("gemini", {"temperature": 0.9})
            manager.register_provider_overrides("openai", {"timeout": 60})

        Overrides are applied at snapshot time on top of the base config.
        They do not mutate the base config.
        """
        self._overrides[provider_name] = dict(overrides)

    def get_provider_overrides(self, provider_name: str) -> dict[str, Any]:
        """Return the registered overrides for a provider (empty dict if none)."""
        return dict(self._overrides.get(provider_name, {}))

    def clear_provider_overrides(self, provider_name: str) -> None:
        """Remove all overrides for a provider."""
        self._overrides.pop(provider_name, None)

    # ── Introspection ──

    @property
    def config(self) -> AIConfig:
        """Direct access to the mutable config (use sparingly)."""
        return self._config

    def as_dict(self) -> dict[str, Any]:
        """Return the current config as a plain dict."""
        return self._config.as_dict()


_default_manager: ConfigManager | None = None


def get_config_manager() -> ConfigManager:
    """Return the process-wide default ConfigManager instance.

    Constructs it on first call. This is the single ConfigManager —
    all runtime mutations go through this instance so changes persist
    for the process lifetime and every snapshot reflects the latest state.
    """
    global _default_manager
    if _default_manager is None:
        _default_manager = ConfigManager()
    return _default_manager
