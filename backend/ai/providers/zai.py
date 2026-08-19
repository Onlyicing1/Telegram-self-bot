"""
ZaiProvider — Z.ai / Zhipu AI GLM adapter (OpenAI-compatible).

Z.ai exposes an OpenAI-compatible chat-completions API. The free Flash
model family is the preferred route; the exact model name is configurable
via ``ZAI_MODEL`` / ``AI_ZAI_MODEL``.
"""
from __future__ import annotations

from backend.ai.providers.base.capabilities import ProviderCapabilities
from backend.ai.providers.base.config import ProviderConfig
from backend.ai.providers.base.defaults import get_provider_default
from backend.ai.providers.openai_compat import OpenAICompatProvider


class ZaiProvider(OpenAICompatProvider):
    PROVIDER_NAME = "zai"
    PROVIDER_VERSION = "1.0.0"

    def __init__(self, config: ProviderConfig | None = None) -> None:
        if config is None:
            config = get_provider_default("zai")
        super().__init__(config)

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            supports_streaming=True,
            supports_tools=True,
            supports_json=True,
            supports_function_call=True,
            supports_long_context=True,
        )
