"""
DummyProvider — the always-on, zero-dependency default provider.

This provider makes no network calls, requires no API key, and never
fails. It exists so the AI pipeline always has a working provider to
fall back to. Every real provider that crashes or is unavailable is
automatically replaced by this one via the ``ProviderManager``
fallback system.

The response text is deterministic: ``"AI pipeline operational."``
Token counts are fixed constants so tests can assert exact values.
"""
from __future__ import annotations

from typing import Any, Iterator

from backend.ai.providers.base.capabilities import ProviderCapabilities
from backend.ai.providers.base.config import ProviderConfig
from backend.ai.providers.base.contract import BaseProvider, ProviderResponse

DUMMY_TEXT = "AI pipeline operational."
DUMMY_PROMPT_TOKENS = 420
DUMMY_COMPLETION_TOKENS = 18


class DummyProvider(BaseProvider):
    """Zero-dependency provider that always succeeds.

    Used as the default and as the automatic fallback when a real
    provider crashes. Capabilities are all ``False`` — it is a
    text-only, non-streaming, no-tools provider.
    """

    PROVIDER_NAME = "dummy"
    PROVIDER_VERSION = "1.0.0"

    def __init__(self, config: ProviderConfig | None = None) -> None:
        super().__init__(config)

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            supports_streaming=False,
            supports_images=False,
            supports_reasoning=False,
            supports_tools=False,
            supports_json=False,
            supports_function_call=False,
            supports_long_context=False,
        )

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

    def chat(self, messages: list[dict[str, Any]], **kwargs: Any) -> ProviderResponse:
        return ProviderResponse(
            text=DUMMY_TEXT,
            provider_name=self.name,
            success=True,
            usage={
                "prompt_tokens": DUMMY_PROMPT_TOKENS,
                "completion_tokens": DUMMY_COMPLETION_TOKENS,
            },
            metadata={"deterministic": True, "version": self.PROVIDER_VERSION},
        )

    def count_tokens(self, text: str) -> int:
        if not text:
            return 0
        return max(1, len(text) // 4)

    def provider_name(self) -> str:
        return self.name

    def provider_version(self) -> str:
        return self.PROVIDER_VERSION
