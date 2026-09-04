"""
NaraRouterProvider — NaraRouter adapter (OpenAI-compatible gateway).

NaraRouter (https://router.bynara.id) is an OpenAI-compatible AI gateway.
The chat endpoint is ``https://router.bynara.id/v1/chat/completions`` with
Bearer-key auth; model aliases are opaque identifiers (``combo/<name>``
combos are valid and must never be rejected by validation).
"""
from __future__ import annotations

from backend.ai.providers.base.capabilities import ProviderCapabilities
from backend.ai.providers.base.config import ProviderConfig
from backend.ai.providers.base.defaults import get_provider_default
from backend.ai.providers.openai_compat import OpenAICompatProvider


class NaraRouterProvider(OpenAICompatProvider):
    """NaraRouter provider via OpenAI-compatible API."""

    PROVIDER_NAME = "nararouter"
    PROVIDER_VERSION = "1.0.0"

    def __init__(self, config: ProviderConfig | None = None) -> None:
        if config is None:
            config = get_provider_default("nararouter")
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