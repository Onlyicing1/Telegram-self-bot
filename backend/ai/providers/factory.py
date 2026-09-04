"""
ProviderFactory — creates and wires providers from configuration.

The factory auto-detects which providers have API keys configured in
the environment variables and registers them automatically. The dummy
provider is always registered as the fallback.

Future providers are added by:
  1. Creating a backend/ai/providers/<name>/ package or <name>.py file.
  2. Adding the class to _PROVIDER_CLASSES below.
  3. Adding defaults to base/defaults.py.
  4. Done.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Type

from backend.ai.providers.base.config import ProviderConfig
from backend.ai.providers.base.contract import BaseProvider
from backend.ai.providers.base.defaults import get_provider_default
from backend.ai.providers.base.exceptions import ProviderNotFound
from backend.ai.providers.cerebras import CerebrasProvider
from backend.ai.providers.cohere import CohereProvider
from backend.ai.providers.dummy.provider import DummyProvider
from backend.ai.providers.fireworks import FireworksProvider
from backend.ai.providers.gemini import GeminiProvider
from backend.ai.providers.groq import GroqProvider
from backend.ai.providers.manager.manager import ProviderManager
from backend.ai.providers.mistral import MistralProvider
from backend.ai.providers.nararouter import NaraRouterProvider
from backend.ai.providers.nvidia import NVIDIAProvider
from backend.ai.providers.openai import OpenAIProvider
from backend.ai.providers.openrouter import OpenRouterProvider
from backend.ai.providers.registry.registry import ProviderRegistry
from backend.ai.providers.sambanova import SambaNovaProvider
from backend.ai.providers.siliconflow import SiliconFlowProvider
from backend.ai.providers.you_search import YouSearchProvider
from backend.ai.providers.zai import ZaiProvider

logger = logging.getLogger(__name__)

_PROVIDER_CLASSES: dict[str, Type[BaseProvider]] = {
    "dummy": DummyProvider,
    "gemini": GeminiProvider,
    "openai": OpenAIProvider,
    "openrouter": OpenRouterProvider,
    "cerebras": CerebrasProvider,
    "mistral": MistralProvider,
    "groq": GroqProvider,
    "zai": ZaiProvider,
    "sambanova": SambaNovaProvider,
    "nvidia": NVIDIAProvider,
    "cohere": CohereProvider,
    "siliconflow": SiliconFlowProvider,
    "fireworks": FireworksProvider,
    "nararouter": NaraRouterProvider,
    "you": YouSearchProvider,
}

_ENV_KEY_MAP: dict[str, list[str]] = {
    "gemini": ["AI_GEMINI_API_KEY", "GEMINI_API_KEY"],
    "openai": ["AI_OPENAI_API_KEY", "OPENAI_API_KEY"],
    "openrouter": ["AI_OPENROUTER_API_KEY", "OPENROUTER_API_KEY"],
    "cerebras": ["AI_CEREBRAS_API_KEY", "CEREBRAS_API_KEY"],
    "mistral": ["AI_MISTRAL_API_KEY", "MISTRAL_API_KEY"],
    "groq": ["AI_GROQ_API_KEY", "GROQ_API_KEY"],
    "zai": ["AI_ZAI_API_KEY", "ZAI_API_KEY"],
    "sambanova": ["AI_SAMBANOVA_API_KEY", "SAMBANOVA_API_KEY"],
    "nvidia": ["AI_NVIDIA_API_KEY", "NVIDIA_API_KEY"],
    "cohere": ["AI_COHERE_API_KEY", "COHERE_API_KEY"],
    "siliconflow": ["AI_SILICONFLOW_API_KEY", "SILICONFLOW_API_KEY"],
    "fireworks": ["AI_FIREWORKS_API_KEY", "FIREWORKS_API_KEY"],
    "nararouter": ["AI_NARAROUTER_API_KEY", "NARAROUTER_API_KEY"],
    # Web-search capability (You.com Search API) — never an LLM key.
    "you": ["YDC_API_KEY"],
}

_ENV_MODEL_MAP: dict[str, str] = {
    "gemini": "AI_GEMINI_MODEL",
    "openai": "AI_OPENAI_MODEL",
    "openrouter": "AI_OPENROUTER_MODEL",
    "cerebras": "AI_CEREBRAS_MODEL",
    "mistral": "AI_MISTRAL_MODEL",
    "groq": "AI_GROQ_MODEL",
    "zai": "AI_ZAI_MODEL",
    "sambanova": "AI_SAMBANOVA_MODEL",
    "nvidia": "AI_NVIDIA_MODEL",
    "cohere": "AI_COHERE_MODEL",
    "siliconflow": "AI_SILICONFLOW_MODEL",
    "fireworks": "AI_FIREWORKS_MODEL",
    "nararouter": "AI_NARAROUTER_MODEL",
}

_ENV_BASE_URL_MAP: dict[str, str] = {
    "openai": "AI_OPENAI_BASE_URL",
    "openrouter": "AI_OPENROUTER_BASE_URL",
    "zai": "AI_ZAI_BASE_URL",
    "sambanova": "AI_SAMBANOVA_BASE_URL",
    "nvidia": "AI_NVIDIA_BASE_URL",
    "cohere": "AI_COHERE_BASE_URL",
    "siliconflow": "AI_SILICONFLOW_BASE_URL",
    "fireworks": "AI_FIREWORKS_BASE_URL",
    "nararouter": "AI_NARAROUTER_BASE_URL",
}


class ProviderFactory:
    """Creates provider instances and wired managers from config."""

    __slots__ = ()

    @staticmethod
    def available_providers() -> list[str]:
        return list(_PROVIDER_CLASSES.keys())

    @staticmethod
    def create_provider(name: str, config: ProviderConfig | None = None) -> BaseProvider:
        cls = _PROVIDER_CLASSES.get(name)
        if cls is None:
            raise ProviderNotFound(
                f"Unknown provider '{name}'. Available: {list(_PROVIDER_CLASSES.keys())}"
            )
        if config is None:
            config = get_provider_default(name)
        provider = cls(config)
        logger.info("ProviderFactory: created provider '%s'", name)
        return provider

    @staticmethod
    def create_registry(config: dict[str, Any] | None = None) -> ProviderRegistry:
        registry = ProviderRegistry()

        dummy_config = get_provider_default("dummy")
        dummy = DummyProvider(dummy_config)
        registry.register(dummy)
        registry.set_fallback(dummy.name)

        for provider_name, env_keys in _ENV_KEY_MAP.items():
            api_key = ""
            used_env_key = ""
            for ek in env_keys:
                val = os.getenv(ek, "").strip()
                if val:
                    api_key = val
                    used_env_key = ek
                    break
            if not api_key:
                continue

            provider_config = get_provider_default(provider_name)
            provider_config.api_key = api_key
            provider_config.enabled = True

            model_env = _ENV_MODEL_MAP.get(provider_name)
            if model_env:
                model = os.getenv(model_env, "").strip()
                if model:
                    provider_config.default_model = model

            base_url_env = _ENV_BASE_URL_MAP.get(provider_name)
            if base_url_env:
                base_url = os.getenv(base_url_env, "").strip()
                if base_url:
                    provider_config.base_url = base_url

            try:
                provider = ProviderFactory.create_provider(provider_name, provider_config)
                registry.register(provider)
                logger.info("ProviderFactory: auto-loaded '%s' from %s", provider_name, used_env_key)
            except ProviderNotFound as exc:
                logger.warning("ProviderFactory: could not create '%s': %s", provider_name, exc)

        active = os.getenv("AI_PROVIDER", "dummy").strip()
        if (
            active and active != "dummy" and registry.has(active)
            and _PROVIDER_CLASSES.get(active) is not None
            and getattr(_PROVIDER_CLASSES[active], "CAPABILITY_KIND", "chat") == "chat"
        ):
            # Only chat-capable providers may become the ACTIVE reasoning
            # engine — a web-search capability is never a chat fallback.
            registry.switch_provider(active)
        elif active == "dummy":
            registry.switch_provider("dummy")

        logger.info("ProviderFactory: registry ready (providers=%s, active=%s)",
                     registry.list(), registry.active_name)
        return registry

    @staticmethod
    def create_manager(config: dict[str, Any] | None = None) -> ProviderManager:
        registry = ProviderFactory.create_registry(config)
        manager = ProviderManager(registry)
        logger.info(
            "ProviderFactory: manager ready (active='%s', providers=%s)",
            manager.get_active_name(),
            manager.list_providers(),
        )
        return manager
