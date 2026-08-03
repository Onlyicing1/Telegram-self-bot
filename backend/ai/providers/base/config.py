"""
ProviderConfig — the single configuration object every provider owns.

Every provider in the system receives a ``ProviderConfig`` instance
through its constructor. A provider never reads environment variables,
never accesses global state, and never receives raw values. It owns
exactly one ``ProviderConfig`` and reads all settings from it.

The ``ProviderConfigManager`` creates, validates, and updates these
objects. The ``ProviderManager`` receives a ``ProviderConfig`` (never
raw values) and passes it to the provider on construction.

Fields:
    provider_name   — unique identifier ("gemini", "openai", "dummy")
    base_url        — API endpoint (empty for providers with fixed URLs)
    api_key         — authentication key (empty for dummy)
    default_model   — the model to use when none is specified
    temperature     — sampling temperature [0.0, 2.0]
    top_p           — nucleus sampling [0.0, 1.0]
    max_tokens      — max output tokens (positive int)
    timeout         — request timeout in seconds (positive int)
    retry_count     — retry attempts on failure (non-negative int)
    enabled         — whether this provider is active
    extra           — provider-specific overrides (dict)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ProviderConfig:
    """Configuration object owned by every provider instance."""

    provider_name: str = ""
    base_url: str = ""
    api_key: str = ""
    default_model: str = ""
    temperature: float = 1.0
    top_p: float = 1.0
    max_tokens: int = 4096
    timeout: int = 30
    retry_count: int = 3
    enabled: bool = False
    extra: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider_name": self.provider_name,
            "base_url": self.base_url,
            "api_key": self.api_key,
            "default_model": self.default_model,
            "temperature": self.temperature,
            "top_p": self.top_p,
            "max_tokens": self.max_tokens,
            "timeout": self.timeout,
            "retry_count": self.retry_count,
            "enabled": self.enabled,
            "extra": dict(self.extra),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ProviderConfig":
        known = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        return cls(**known)

    @property
    def name(self) -> str:
        return self.provider_name

    @property
    def model(self) -> str:
        return self.default_model
