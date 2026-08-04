"""
OpenAIProvider — OpenAI GPT adapter (real implementation).

Uses the OpenAI-compatible chat completions API via httpx async.
Supports custom base URLs for OpenAI-compatible endpoints.
"""
from __future__ import annotations

from typing import Any

from backend.ai.providers.base.capabilities import ProviderCapabilities
from backend.ai.providers.base.config import ProviderConfig
from backend.ai.providers.base.contract import ProviderResponse
from backend.ai.providers.base.defaults import get_provider_default
from backend.ai.providers.openai_compat import OpenAICompatProvider


class OpenAIProvider(OpenAICompatProvider):
    """OpenAI GPT provider via chat completions API."""

    PROVIDER_NAME = "openai"
    PROVIDER_VERSION = "1.0.0"

    def __init__(self, config: ProviderConfig | None = None) -> None:
        if config is None:
            config = get_provider_default("openai")
        super().__init__(config)

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            supports_streaming=True,
            supports_images=True,
            supports_tools=True,
            supports_json=True,
            supports_function_call=True,
            supports_long_context=True,
        )
