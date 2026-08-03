"""
GeminiProvider — Google Gemini adapter (architecture only).

This provider is a placeholder. It inherits from ``BaseProvider`` and
implements the full provider contract, but currently returns
``NOT_IMPLEMENTED``. No HTTP request, no SDK, no API key, no environment
variable.

Future implementation will:
  - Read the API key from the injected ``ProviderConfig`` (not from env).
  - Convert ``PromptPackage`` into Gemini's API format.
  - Call the Gemini API via an async helper.
  - Parse the response into a ``ProviderResponse``.

Until then, this class exists solely to define the architecture and
prove the provider contract.
"""
from __future__ import annotations

from typing import Any

from backend.ai.prompt.builder import PromptPackage
from backend.ai.providers.base import (
    NOT_IMPLEMENTED,
    BaseProvider,
    ProviderConfig,
    ProviderResponse,
)

PROVIDER_NAME = "gemini"
PROVIDER_VERSION = "0.0.0"


class GeminiProvider(BaseProvider):
    """Google Gemini provider (not yet implemented).

    Returns ``NOT_IMPLEMENTED`` for every request. When a real
    implementation is added, only this file changes — the factory,
    registry, and all callers stay the same.
    """

    PROVIDER_NAME = PROVIDER_NAME
    PROVIDER_VERSION = PROVIDER_VERSION

    def __init__(self, config: ProviderConfig | None = None) -> None:
        if config is None:
            config = ProviderConfig(name=PROVIDER_NAME)
        super().__init__(config)

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

    def generate(self, prompt_package: PromptPackage) -> ProviderResponse:
        return self._not_implemented_response()

    def estimate_tokens(self, prompt_package: PromptPackage) -> int:
        try:
            return prompt_package.estimated_tokens.estimated_total
        except Exception:
            return 0

    def provider_name(self) -> str:
        return self.name

    def provider_version(self) -> str:
        return self.PROVIDER_VERSION
