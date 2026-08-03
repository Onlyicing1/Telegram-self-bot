"""
DummyProvider — the always-on, zero-dependency default provider.

The DummyProvider never makes any network call. It exists so the rest
of the system can wire through the Provider Layer without any external
API. It returns a deterministic, successful response with fixed token
usage, allowing the full pipeline to be tested end-to-end.

The response text is ``DUMMY_TEXT`` (``"AI pipeline operational."``).
``success`` is ``True`` so downstream layers (conversation update,
metrics) see a real successful execution.
"""
from __future__ import annotations

from typing import Any

from backend.ai.prompt.builder import PromptPackage
from backend.ai.providers.base import BaseProvider, ProviderConfig, ProviderResponse

DUMMY_TEXT = "AI pipeline operational."
DUMMY_PROMPT_TOKENS = 420
DUMMY_COMPLETION_TOKENS = 18


class DummyProvider(BaseProvider):
    """Deterministic, always-healthy provider for testing and fallback."""

    PROVIDER_NAME = "dummy"
    PROVIDER_VERSION = "1.0.0"

    def __init__(self, config: ProviderConfig | None = None) -> None:
        super().__init__(config)

    def initialize(self) -> None:
        pass

    def shutdown(self) -> None:
        pass

    def health(self) -> dict[str, Any]:
        return {
            "healthy": True,
            "provider": self.name,
            "version": self.PROVIDER_VERSION,
            "enabled": self.is_enabled,
        }

    def generate(self, prompt_package: PromptPackage) -> ProviderResponse:
        return ProviderResponse(
            text=DUMMY_TEXT,
            provider_name=self.name,
            success=True,
            usage={
                "prompt_tokens": DUMMY_PROMPT_TOKENS,
                "completion_tokens": DUMMY_COMPLETION_TOKENS,
            },
            metadata={
                "deterministic": True,
                "version": self.PROVIDER_VERSION,
            },
        )

    def estimate_tokens(self, prompt_package: PromptPackage) -> int:
        try:
            return prompt_package.estimated_tokens.estimated_total
        except Exception:
            return DUMMY_PROMPT_TOKENS + DUMMY_COMPLETION_TOKENS

    def provider_name(self) -> str:
        return self.name

    def provider_version(self) -> str:
        return self.PROVIDER_VERSION
