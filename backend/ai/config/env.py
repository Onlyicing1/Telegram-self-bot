"""
ENV-based AI configuration.

All AI-related environment variables are loaded here and fed into the
existing ConfigManager and ProviderConfigManager. Nothing is hardcoded —
API keys, model names, and provider names all come from environment
variables with sensible defaults (empty string / disabled).

Environment variables (all optional — AI is off by default):
    AI_ENABLED                → "true"/"false" (default: false)
    AI_PROVIDER               → provider name (default: "dummy")
    AI_MODEL                  → model name (default: provider-specific)
    AI_TEMPERATURE            → float (default: 1.0)
    AI_TOP_P                  → float (default: 1.0)
    AI_MAX_TOKENS             → int (default: 4096)
    AI_TIMEOUT                → int seconds (default: 30)
    AI_RETRY_COUNT            → int (default: 3)

    # Per-provider API keys (empty = not configured)
    # Both AI_*_API_KEY and bare *_API_KEY forms are accepted.
    AI_GEMINI_API_KEY         → Gemini API key
    GEMINI_API_KEY            → Gemini API key (alternative)
    AI_OPENAI_API_KEY         → OpenAI API key
    OPENAI_API_KEY            → OpenAI API key (alternative)
    AI_OPENROUTER_API_KEY     → OpenRouter API key
    OPENROUTER_API_KEY        → OpenRouter API key (alternative)
    AI_CLAUDE_API_KEY         → Anthropic/Claude API key
    CLAUDE_API_KEY            → Anthropic/Claude API key (alternative)
    AI_GLM_API_KEY            → GLM/ChatGLM API key
    GLM_API_KEY               → GLM/ChatGLM API key (alternative)
    AI_GROQ_API_KEY           → Groq API key
    GROQ_API_KEY              → Groq API key (alternative)
    AI_CEREBRAS_API_KEY       → Cerebras API key
    CEREBRAS_API_KEY          → Cerebras API key (alternative)
    AI_MISTRAL_API_KEY        → Mistral API key
    MISTRAL_API_KEY           → Mistral API key (alternative)

    # Per-provider model overrides (empty = use provider default)
    AI_GEMINI_MODEL           → Gemini model name
    AI_OPENAI_MODEL           → OpenAI model name
    AI_OPENROUTER_MODEL       → OpenRouter model name
    AI_CLAUDE_MODEL           → Claude model name
    AI_GLM_MODEL              → GLM model name
    AI_GROQ_MODEL             → Groq model name
    AI_CEREBRAS_MODEL         → Cerebras model name
    AI_MISTRAL_MODEL          → Mistral model name

    # Per-provider base URL overrides (empty = use provider default)
    AI_OPENAI_BASE_URL        → OpenAI-compatible base URL
    AI_OPENROUTER_BASE_URL    → OpenRouter base URL

    # Memory settings
    AI_MEMORY_RETENTION_DAYS → int (default: 90)

    # Fallback chain (comma-separated provider names, tried in order)
    AI_PROVIDER_FALLBACK      → e.g. "gemini,openai,openrouter" (default: "")
"""
from __future__ import annotations

import logging
import os
from typing import Any

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

logger = logging.getLogger(__name__)


def _get_bool(key: str, default: bool = False) -> bool:
    val = os.getenv(key, "")
    if not val:
        return default
    return val.lower() in ("true", "1", "yes", "on")


def _get_float(key: str, default: float) -> float:
    try:
        return float(os.getenv(key, "") or default)
    except (ValueError, TypeError):
        return default


def _get_int(key: str, default: int) -> int:
    try:
        return int(os.getenv(key, "") or default)
    except (ValueError, TypeError):
        return default


def load_ai_env() -> dict[str, Any]:
    """Load all AI-related env vars into a config dict.

    This dict is consumed by ConfigManager and ProviderConfigManager
    to initialize the AI subsystem at startup.
    """
    return {
        "enabled": _get_bool("AI_ENABLED", DEFAULT_ENABLED),
        "provider": os.getenv("AI_PROVIDER", DEFAULT_PROVIDER),
        "model": os.getenv("AI_MODEL", DEFAULT_MODEL),
        "temperature": _get_float("AI_TEMPERATURE", DEFAULT_TEMPERATURE),
        "top_p": _get_float("AI_TOP_P", DEFAULT_TOP_P),
        "max_tokens": _get_int("AI_MAX_TOKENS", 4096),
        "timeout": _get_int("AI_TIMEOUT", DEFAULT_TIMEOUT),
        "retry_count": _get_int("AI_RETRY_COUNT", DEFAULT_RETRY_COUNT),
        "streaming_enabled": _get_bool("AI_STREAMING_ENABLED", DEFAULT_STREAMING_ENABLED),
        "vision_enabled": _get_bool("AI_VISION_ENABLED", DEFAULT_VISION_ENABLED),
        "reasoning_enabled": _get_bool("AI_REASONING_ENABLED", DEFAULT_REASONING_ENABLED),
        "developer_mode": _get_bool("AI_DEVELOPER_MODE", DEFAULT_DEVELOPER_MODE),
        "history_budget": _get_int("AI_HISTORY_BUDGET", DEFAULT_HISTORY_BUDGET),
        "tool_budget": _get_int("AI_TOOL_BUDGET", DEFAULT_TOOL_BUDGET),
        "system_prompt": os.getenv("AI_SYSTEM_PROMPT", DEFAULT_SYSTEM_PROMPT),
        "memory_retention_days": _get_int("AI_MEMORY_RETENTION_DAYS", 90),
        "fallback_chain": [
            p.strip() for p in os.getenv("AI_PROVIDER_FALLBACK", "").split(",")
            if p.strip()
        ],
    }


def load_provider_env_configs() -> dict[str, dict[str, Any]]:
    """Load per-provider env-based config overrides.

    Returns a dict mapping provider name → config dict with keys:
    api_key, default_model, base_url, enabled.

    Both AI_*_API_KEY and bare *_API_KEY env vars are checked.
    """
    configs: dict[str, dict[str, Any]] = {}

    gemini_key = os.getenv("AI_GEMINI_API_KEY", "") or os.getenv("GEMINI_API_KEY", "")
    if gemini_key:
        configs["gemini"] = {
            "api_key": gemini_key,
            "default_model": os.getenv("AI_GEMINI_MODEL", "") or os.getenv("GEMINI_MODEL", ""),
            "enabled": True,
        }

    openai_key = os.getenv("AI_OPENAI_API_KEY", "") or os.getenv("OPENAI_API_KEY", "")
    if openai_key:
        configs["openai"] = {
            "api_key": openai_key,
            "default_model": os.getenv("AI_OPENAI_MODEL", "") or os.getenv("OPENAI_MODEL", ""),
            "base_url": os.getenv("AI_OPENAI_BASE_URL", "") or os.getenv("OPENAI_BASE_URL", ""),
            "enabled": True,
        }

    openrouter_key = os.getenv("AI_OPENROUTER_API_KEY", "") or os.getenv("OPENROUTER_API_KEY", "")
    if openrouter_key:
        configs["openrouter"] = {
            "api_key": openrouter_key,
            "default_model": os.getenv("AI_OPENROUTER_MODEL", "") or os.getenv("OPENROUTER_MODEL", ""),
            "base_url": os.getenv("AI_OPENROUTER_BASE_URL", "") or os.getenv("OPENROUTER_BASE_URL", ""),
            "enabled": True,
        }

    claude_key = os.getenv("AI_CLAUDE_API_KEY", "") or os.getenv("CLAUDE_API_KEY", "")
    if claude_key:
        configs["claude"] = {
            "api_key": claude_key,
            "default_model": os.getenv("AI_CLAUDE_MODEL", "") or os.getenv("CLAUDE_MODEL", ""),
            "enabled": True,
        }

    glm_key = os.getenv("AI_GLM_API_KEY", "") or os.getenv("GLM_API_KEY", "")
    if glm_key:
        configs["glm"] = {
            "api_key": glm_key,
            "default_model": os.getenv("AI_GLM_MODEL", "") or os.getenv("GLM_MODEL", ""),
            "enabled": True,
        }

    groq_key = os.getenv("AI_GROQ_API_KEY", "") or os.getenv("GROQ_API_KEY", "")
    if groq_key:
        configs["groq"] = {
            "api_key": groq_key,
            "default_model": os.getenv("AI_GROQ_MODEL", "") or os.getenv("GROQ_MODEL", ""),
            "enabled": True,
        }

    cerebras_key = os.getenv("AI_CEREBRAS_API_KEY", "") or os.getenv("CEREBRAS_API_KEY", "")
    if cerebras_key:
        configs["cerebras"] = {
            "api_key": cerebras_key,
            "default_model": os.getenv("AI_CEREBRAS_MODEL", "") or os.getenv("CEREBRAS_MODEL", ""),
            "enabled": True,
        }

    mistral_key = os.getenv("AI_MISTRAL_API_KEY", "") or os.getenv("MISTRAL_API_KEY", "")
    if mistral_key:
        configs["mistral"] = {
            "api_key": mistral_key,
            "default_model": os.getenv("AI_MISTRAL_MODEL", "") or os.getenv("MISTRAL_MODEL", ""),
            "enabled": True,
        }

    zai_key = os.getenv("AI_ZAI_API_KEY", "") or os.getenv("ZAI_API_KEY", "")
    if zai_key:
        configs["zai"] = {
            "api_key": zai_key,
            "default_model": os.getenv("AI_ZAI_MODEL", "") or os.getenv("ZAI_MODEL", ""),
            "base_url": os.getenv("AI_ZAI_BASE_URL", "") or os.getenv("ZAI_BASE_URL", ""),
            "enabled": True,
        }

    sambanova_key = os.getenv("AI_SAMBANOVA_API_KEY", "") or os.getenv("SAMBANOVA_API_KEY", "")
    if sambanova_key:
        configs["sambanova"] = {
            "api_key": sambanova_key,
            "default_model": os.getenv("AI_SAMBANOVA_MODEL", "") or os.getenv("SAMBANOVA_MODEL", ""),
            "base_url": os.getenv("AI_SAMBANOVA_BASE_URL", "") or os.getenv("SAMBANOVA_BASE_URL", ""),
            "enabled": True,
        }

    nvidia_key = os.getenv("AI_NVIDIA_API_KEY", "") or os.getenv("NVIDIA_API_KEY", "")
    if nvidia_key:
        configs["nvidia"] = {
            "api_key": nvidia_key,
            "default_model": os.getenv("AI_NVIDIA_MODEL", "") or os.getenv("NVIDIA_MODEL", ""),
            "base_url": os.getenv("AI_NVIDIA_BASE_URL", "") or os.getenv("NVIDIA_BASE_URL", ""),
            "enabled": True,
        }

    cohere_key = os.getenv("AI_COHERE_API_KEY", "") or os.getenv("COHERE_API_KEY", "")
    if cohere_key:
        configs["cohere"] = {
            "api_key": cohere_key,
            "default_model": os.getenv("AI_COHERE_MODEL", "") or os.getenv("COHERE_MODEL", ""),
            "base_url": os.getenv("AI_COHERE_BASE_URL", "") or os.getenv("COHERE_BASE_URL", ""),
            "enabled": True,
        }

    siliconflow_key = os.getenv("AI_SILICONFLOW_API_KEY", "") or os.getenv("SILICONFLOW_API_KEY", "")
    if siliconflow_key:
        configs["siliconflow"] = {
            "api_key": siliconflow_key,
            "default_model": os.getenv("AI_SILICONFLOW_MODEL", "") or os.getenv("SILICONFLOW_MODEL", ""),
            "base_url": os.getenv("AI_SILICONFLOW_BASE_URL", "") or os.getenv("SILICONFLOW_BASE_URL", ""),
            "enabled": True,
        }

    fireworks_key = os.getenv("AI_FIREWORKS_API_KEY", "") or os.getenv("FIREWORKS_API_KEY", "")
    if fireworks_key:
        configs["fireworks"] = {
            "api_key": fireworks_key,
            "default_model": os.getenv("AI_FIREWORKS_MODEL", "") or os.getenv("FIREWORKS_MODEL", ""),
            "base_url": os.getenv("AI_FIREWORKS_BASE_URL", "") or os.getenv("FIREWORKS_BASE_URL", ""),
            "enabled": True,
        }

    return configs


def apply_env_to_config_manager(config_manager: Any) -> None:
    """Apply ENV-based AI config to a ConfigManager instance.

    Called once at startup to seed the ConfigManager with env values.
    """
    env = load_ai_env()
    try:
        config_manager.set_many({
            "enabled": env["enabled"],
            "provider": env["provider"],
            "model": env["model"],
            "temperature": env["temperature"],
            "top_p": env["top_p"],
            "max_tokens": env["max_tokens"],
            "timeout": env["timeout"],
            "retry_count": env["retry_count"],
            "streaming_enabled": env["streaming_enabled"],
            "vision_enabled": env["vision_enabled"],
            "reasoning_enabled": env["reasoning_enabled"],
            "developer_mode": env["developer_mode"],
            "history_budget": env["history_budget"],
            "tool_budget": env["tool_budget"],
        })
        if env["system_prompt"] != DEFAULT_SYSTEM_PROMPT:
            config_manager.set("system_prompt", env["system_prompt"])
    except Exception as exc:
        logger.warning("AI env config: failed to apply to ConfigManager: %s", exc)


def apply_env_to_provider_configs(provider_config_manager: Any) -> None:
    """Apply per-provider ENV config to a ProviderConfigManager instance.

    Called once at startup to seed provider configs with env-based
    API keys and model names.
    """
    env_configs = load_provider_env_configs()
    for provider_name, overrides in env_configs.items():
        try:
            for field, value in overrides.items():
                if value:
                    provider_config_manager.update(provider_name, field, value)
        except Exception as exc:
            logger.warning("AI env config: failed to apply provider '%s': %s", provider_name, exc)
