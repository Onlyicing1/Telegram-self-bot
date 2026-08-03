"""
GeminiProvider — Google Gemini adapter (architecture only).

Returns ``NOT_IMPLEMENTED`` for every request. No HTTP request, no
SDK, no API key. When a real implementation is added, only this file
changes — the factory, registry, manager, and all callers stay the
same.
"""
from __future__ import annotations

from typing import Any

from backend.ai.providers.base.capabilities import ProviderCapabilities
from backend.ai.providers.base.config import ProviderConfig
from backend.ai.providers.base.contract import BaseProvider, ProviderResponse
from backend.ai.providers.base.defaults import get_provider_default

PROVIDER_NAME = "gemini"
PROVIDER_VERSION = "0.0.0"


class GeminiProvider(BaseProvider):
    """Google Gemini provider (not yet implemented)."""

    PROVIDER_NAME = PROVIDER_NAME
    PROVIDER_VERSION = PROVIDER_VERSION

    def __init__(self, config: ProviderConfig | None = None) -> None:
        if config is None:
            config = get_provider_default(PROVIDER_NAME)
        super().__init__(config)

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            supports_streaming=True,
            supports_images=True,
            supports_reasoning=True,
            supports_tools=True,
            supports_json=True,
            supports_function_call=True,
            supports_long_context=True,
        )

    def initialize(self) -> None:
        pass

    def shutdown(self) -> None:
        pass

    def health(self) -> dict[str, Any]:
        return {
            "healthy": False,
            "provider": self.name,
            "version": self.PROVIDER_VERSION,
            "enabled": self.is_enabled,
            "reason": "not implemented",
        }

    def chat(self, messages: list[dict[str, Any]], **kwargs: Any) -> ProviderResponse:
        return self._not_implemented_response()

    def count_tokens(self, text: str) -> int:
        if not text:
            return 0
        return max(1, len(text) // 4)

    def provider_name(self) -> str:
        return self.name

    def provider_version(self) -> str:
        return self.PROVIDER_VERSION
