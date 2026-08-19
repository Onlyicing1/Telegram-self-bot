"""
FireworksProvider — Fireworks AI adapter (OpenAI-compatible).

Fireworks exposes an OpenAI-compatible chat-completions API at
``https://api.fireworks.ai/inference/v1``. Only usable when the account has
genuine recurring access — promotional credits are NOT equivalent to a free
tier.
"""
from __future__ import annotations

from backend.ai.providers.base.capabilities import ProviderCapabilities
from backend.ai.providers.base.config import ProviderConfig
from backend.ai.providers.base.defaults import get_provider_default
from backend.ai.providers.openai_compat import OpenAICompatProvider


class FireworksProvider(OpenAICompatProvider):
    PROVIDER_NAME = "fireworks"
    PROVIDER_VERSION = "1.0.0"

    def __init__(self, config: ProviderConfig | None = None) -> None:
        if config is None:
            config = get_provider_default("fireworks")
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
