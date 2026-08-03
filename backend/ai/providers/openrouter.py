"""
OpenRouterProvider — OpenRouter adapter (architecture only).

This provider is a placeholder. It inherits from ``BaseProvider`` and
implements ``generate()``, but currently returns ``NOT_IMPLEMENTED``.
No HTTP request, no SDK, no API key, no environment variable.

Future implementation will:
  - Read the API key from the injected ``ProviderConfig`` (not from env).
  - Convert ``PromptPackage`` into OpenRouter's API format.
  - Call the OpenRouter API via an async helper.
  - Parse the response into a ``ProviderResponse``.

Until then, this class exists solely to define the architecture and
prove the provider contract.
"""
from __future__ import annotations

from backend.ai.prompt.builder import PromptPackage
from backend.ai.providers.base import BaseProvider, ProviderConfig, ProviderResponse

PROVIDER_NAME = "openrouter"


class OpenRouterProvider(BaseProvider):
    """OpenRouter provider (not yet implemented).

    Returns ``NOT_IMPLEMENTED`` for every request. When a real
    implementation is added, only this file changes — the factory,
    registry, and all callers stay the same.
    """

    PROVIDER_NAME = PROVIDER_NAME

    def __init__(self, config: ProviderConfig | None = None) -> None:
        if config is None:
            config = ProviderConfig(name=PROVIDER_NAME)
        super().__init__(config)

    def generate(self, prompt_package: PromptPackage) -> ProviderResponse:
        """Return ``NOT_IMPLEMENTED`` — no API call is made."""
        return self._not_implemented_response()
