"""
CohereProvider — Cohere Command adapter (OpenAI-compatible route).

Cohere's native chat API is ``/v2/chat``; it also exposes an
OpenAI-compatible endpoint at ``https://api.cohere.com/compatibility/v1``,
which this adapter uses so it shares the single OpenAI-compatible request
path. Cohere is evaluation-oriented and should not become the primary route.
"""
from __future__ import annotations

from backend.ai.providers.base.capabilities import ProviderCapabilities
from backend.ai.providers.base.config import ProviderConfig
from backend.ai.providers.base.defaults import get_provider_default
from backend.ai.providers.openai_compat import OpenAICompatProvider


class CohereProvider(OpenAICompatProvider):
    PROVIDER_NAME = "cohere"
    PROVIDER_VERSION = "1.0.0"

    def __init__(self, config: ProviderConfig | None = None) -> None:
        if config is None:
            config = get_provider_default("cohere")
        super().__init__(config)

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            supports_streaming=True,
            supports_tools=True,
            supports_json=True,
            supports_function_call=True,
        )
