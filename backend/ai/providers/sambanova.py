"""
SambaNovaProvider — SambaNova Cloud adapter (OpenAI-compatible).

SambaNova exposes an OpenAI-compatible chat-completions API at
``https://api.sambanova.ai/v1``. It is intended as an emergency backup
(limited free daily capacity), not a primary route.
"""
from __future__ import annotations

from backend.ai.providers.base.capabilities import ProviderCapabilities
from backend.ai.providers.base.config import ProviderConfig
from backend.ai.providers.base.defaults import get_provider_default
from backend.ai.providers.openai_compat import OpenAICompatProvider


class SambaNovaProvider(OpenAICompatProvider):
    PROVIDER_NAME = "sambanova"
    PROVIDER_VERSION = "1.0.0"

    def __init__(self, config: ProviderConfig | None = None) -> None:
        if config is None:
            config = get_provider_default("sambanova")
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
