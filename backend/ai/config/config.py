"""
AIConfig — the mutable, in-RAM configuration object.

``AIConfig`` holds every configuration field defined in
AI_MASTER_DESIGN.md. It is mutable so the ``ConfigManager`` can update
individual fields via ``set()``. However, the Engine never receives
an ``AIConfig`` directly — it only receives immutable
``ConfigSnapshot`` objects produced by ``ConfigManager.snapshot()``.

Provider-specific overrides are applied at snapshot time, not stored
on the ``AIConfig`` itself. This keeps ``AIConfig`` fully
provider-independent.

Fields (all have defaults from ``defaults.py``):
    enabled, provider, model, temperature, top_p, max_tokens,
    timeout, retry_count, system_prompt, history_budget,
    tool_budget, streaming_enabled, vision_enabled,
    reasoning_enabled, developer_mode
"""
from __future__ import annotations

from dataclasses import dataclass, field as dc_field

from backend.ai.config import defaults


@dataclass
class AIConfig:
    """Mutable AI configuration. Lives in RAM only — never persisted."""

    enabled: bool = defaults.DEFAULT_ENABLED
    provider: str = defaults.DEFAULT_PROVIDER
    model: str = defaults.DEFAULT_MODEL
    temperature: float = defaults.DEFAULT_TEMPERATURE
    top_p: float = defaults.DEFAULT_TOP_P
    max_tokens: int = defaults.DEFAULT_MAX_TOKENS
    timeout: int = defaults.DEFAULT_TIMEOUT
    retry_count: int = defaults.DEFAULT_RETRY_COUNT
    system_prompt: str = defaults.DEFAULT_SYSTEM_PROMPT
    history_budget: int = defaults.DEFAULT_HISTORY_BUDGET
    tool_budget: int = defaults.DEFAULT_TOOL_BUDGET
    streaming_enabled: bool = defaults.DEFAULT_STREAMING_ENABLED
    vision_enabled: bool = defaults.DEFAULT_VISION_ENABLED
    reasoning_enabled: bool = defaults.DEFAULT_REASONING_ENABLED
    developer_mode: bool = defaults.DEFAULT_DEVELOPER_MODE

    def as_dict(self) -> dict:
        """Return all fields as a plain dict (for snapshot construction)."""
        return {
            "enabled": self.enabled,
            "provider": self.provider,
            "model": self.model,
            "temperature": self.temperature,
            "top_p": self.top_p,
            "max_tokens": self.max_tokens,
            "timeout": self.timeout,
            "retry_count": self.retry_count,
            "system_prompt": self.system_prompt,
            "history_budget": self.history_budget,
            "tool_budget": self.tool_budget,
            "streaming_enabled": self.streaming_enabled,
            "vision_enabled": self.vision_enabled,
            "reasoning_enabled": self.reasoning_enabled,
            "developer_mode": self.developer_mode,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "AIConfig":
        """Build an AIConfig from a dict, ignoring unknown keys."""
        known = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        return cls(**known)
