"""
Provider config defaults — factory defaults for every supported provider.

Each entry produces a ``ProviderConfig`` with sensible defaults. The
``ProviderConfigManager`` uses these on ``load()`` and ``reset()``.

To add a new provider:
  1. Add an entry to ``_PROVIDER_DEFAULTS`` below.
  2. Add the provider class to ``_PROVIDER_CLASSES`` in ``factory.py``.
  3. Done.
"""
from __future__ import annotations

from backend.ai.providers.base.config import ProviderConfig


def _dummy_default() -> ProviderConfig:
    return ProviderConfig(
        provider_name="dummy",
        base_url="",
        api_key="",
        default_model="dummy-1",
        temperature=1.0,
        top_p=1.0,
        max_tokens=4096,
        timeout=30,
        retry_count=3,
        enabled=True,
    )


def _gemini_default() -> ProviderConfig:
    return ProviderConfig(
        provider_name="gemini",
        base_url="",
        api_key="",
        default_model="gemini-2.5-flash",
        temperature=1.0,
        top_p=1.0,
        max_tokens=8192,
        timeout=60,
        retry_count=3,
        enabled=False,
    )


def _openai_default() -> ProviderConfig:
    return ProviderConfig(
        provider_name="openai",
        base_url="https://api.openai.com/v1",
        api_key="",
        default_model="gpt-4o",
        temperature=1.0,
        top_p=1.0,
        max_tokens=4096,
        timeout=60,
        retry_count=3,
        enabled=False,
    )


def _claude_default() -> ProviderConfig:
    return ProviderConfig(
        provider_name="claude",
        base_url="https://api.anthropic.com/v1",
        api_key="",
        default_model="claude-sonnet-4-20250514",
        temperature=1.0,
        top_p=1.0,
        max_tokens=8192,
        timeout=60,
        retry_count=3,
        enabled=False,
    )


def _glm_default() -> ProviderConfig:
    return ProviderConfig(
        provider_name="glm",
        base_url="https://open.bigmodel.cn/api/paas/v4",
        api_key="",
        default_model="glm-4-flash",
        temperature=1.0,
        top_p=1.0,
        max_tokens=4096,
        timeout=60,
        retry_count=3,
        enabled=False,
    )


def _openrouter_default() -> ProviderConfig:
    return ProviderConfig(
        provider_name="openrouter",
        base_url="https://openrouter.ai/api/v1",
        api_key="",
        default_model="openrouter/auto",
        temperature=1.0,
        top_p=1.0,
        max_tokens=4096,
        timeout=60,
        retry_count=3,
        enabled=False,
    )


def _cerebras_default() -> ProviderConfig:
    return ProviderConfig(
        provider_name="cerebras",
        base_url="https://api.cerebras.ai/v1",
        api_key="",
        default_model="llama-3.3-70b",
        temperature=1.0,
        top_p=1.0,
        max_tokens=4096,
        timeout=60,
        retry_count=3,
        enabled=False,
    )


def _mistral_default() -> ProviderConfig:
    return ProviderConfig(
        provider_name="mistral",
        base_url="https://api.mistral.ai/v1",
        api_key="",
        default_model="mistral-large-latest",
        temperature=1.0,
        top_p=1.0,
        max_tokens=4096,
        timeout=60,
        retry_count=3,
        enabled=False,
    )


def _groq_default() -> ProviderConfig:
    return ProviderConfig(
        provider_name="groq",
        base_url="https://api.groq.com/openai/v1",
        api_key="",
        default_model="llama-3.3-70b-versatile",
        temperature=1.0,
        top_p=1.0,
        max_tokens=4096,
        timeout=60,
        retry_count=3,
        enabled=False,
    )


def _zai_default() -> ProviderConfig:
    return ProviderConfig(
        provider_name="zai",
        base_url="https://api.z.ai/api/paas/v4",
        api_key="",
        default_model="glm-4.5-flash",
        temperature=1.0,
        top_p=1.0,
        max_tokens=4096,
        timeout=60,
        retry_count=3,
        enabled=False,
    )


def _sambanova_default() -> ProviderConfig:
    return ProviderConfig(
        provider_name="sambanova",
        base_url="https://api.sambanova.ai/v1",
        api_key="",
        default_model="DeepSeek-V3-0324",
        temperature=1.0,
        top_p=1.0,
        max_tokens=4096,
        timeout=60,
        retry_count=3,
        enabled=False,
    )


def _nvidia_default() -> ProviderConfig:
    return ProviderConfig(
        provider_name="nvidia",
        base_url="https://integrate.api.nvidia.com/v1",
        api_key="",
        default_model="qwen/qwen2.5-72b-instruct",
        temperature=1.0,
        top_p=1.0,
        max_tokens=4096,
        timeout=60,
        retry_count=3,
        enabled=False,
    )


def _cohere_default() -> ProviderConfig:
    return ProviderConfig(
        provider_name="cohere",
        base_url="https://api.cohere.com/compatibility/v1",
        api_key="",
        default_model="command-r-plus",
        temperature=1.0,
        top_p=1.0,
        max_tokens=4096,
        timeout=60,
        retry_count=3,
        enabled=False,
    )


def _siliconflow_default() -> ProviderConfig:
    return ProviderConfig(
        provider_name="siliconflow",
        base_url="https://api.siliconflow.cn/v1",
        api_key="",
        default_model="Qwen/Qwen2.5-72B-Instruct",
        temperature=1.0,
        top_p=1.0,
        max_tokens=4096,
        timeout=60,
        retry_count=3,
        enabled=False,
    )


def _fireworks_default() -> ProviderConfig:
    return ProviderConfig(
        provider_name="fireworks",
        base_url="https://api.fireworks.ai/inference/v1",
        api_key="",
        default_model="accounts/fireworks/models/llama-v3p3-70b-instruct",
        temperature=1.0,
        top_p=1.0,
        max_tokens=4096,
        timeout=60,
        retry_count=3,
        enabled=False,
    )


def _custom_default() -> ProviderConfig:
    return ProviderConfig(
        provider_name="custom",
        base_url="",
        api_key="",
        default_model="",
        temperature=1.0,
        top_p=1.0,
        max_tokens=4096,
        timeout=30,
        retry_count=3,
        enabled=False,
    )


def _you_default() -> ProviderConfig:
    # You.com Web Search — a retrieval capability, not a chat provider.
    return ProviderConfig(
        provider_name="you",
        base_url="https://ydc-index.io",
        api_key="",
        default_model="",
        temperature=1.0,
        top_p=1.0,
        max_tokens=4096,
        timeout=20,
        retry_count=1,
        enabled=False,
    )


_PROVIDER_DEFAULTS: dict[str, callable] = {
    "dummy": _dummy_default,
    "gemini": _gemini_default,
    "openai": _openai_default,
    "claude": _claude_default,
    "glm": _glm_default,
    "openrouter": _openrouter_default,
    "custom": _custom_default,
    "cerebras": _cerebras_default,
    "mistral": _mistral_default,
    "groq": _groq_default,
    "zai": _zai_default,
    "sambanova": _sambanova_default,
    "nvidia": _nvidia_default,
    "cohere": _cohere_default,
    "siliconflow": _siliconflow_default,
    "fireworks": _fireworks_default,
    "you": _you_default,
}


def get_provider_default(name: str) -> ProviderConfig:
    """Return a fresh ProviderConfig with defaults for the named provider."""
    factory = _PROVIDER_DEFAULTS.get(name)
    if factory is None:
        return ProviderConfig(provider_name=name)
    return factory()


def list_provider_names() -> list[str]:
    """Return all provider names that have defaults."""
    return list(_PROVIDER_DEFAULTS.keys())


# ── Model resolution / deprecation ──

# Known-retired models → current replacement. Kept as data (not code) so a
# stale value in env/Supabase config never poisons the provider: it is
# substituted before the request is built. Add entries here when a provider
# retires a model — no execution logic needs to change.
DEPRECATED_MODELS: dict[str, str] = {
    "gemini-2.0-flash": "gemini-2.5-flash",
}


def resolve_model(provider_name: str, model: str) -> str:
    """Resolve a configured model, substituting known-deprecated models.

    Returns ``model`` unchanged when it is not in the deprecation map. This is
    a pure, provider-agnostic data lookup — no network, no provider coupling.
    """
    resolved = DEPRECATED_MODELS.get((model or "").strip())
    if resolved and resolved != model:
        import logging
        logging.getLogger(__name__).warning(
            "provider-model: '%s' model '%s' is deprecated — using '%s'",
            provider_name, model, resolved,
        )
        return resolved
    return model
