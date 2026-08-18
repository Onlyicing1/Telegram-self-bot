"""
DummyProvider — the always-on, zero-dependency default provider.

This provider makes no network calls and requires no API key. It
exists so the AI pipeline always has a provider instance available
when no real provider is configured or every real provider fails.

It NEVER reports fake success: when used as the active provider with
no keys configured it returns ``success=False`` with a clear
"not configured" diagnostic, and the ``ProviderManager`` converts
emergency-fallback dummy responses into ``success=False`` results
that preserve the original provider failures.

Token counts are fixed constants so tests can assert exact values.
"""
from __future__ import annotations

from typing import Any, Iterator

from backend.ai.providers.base.capabilities import ProviderCapabilities
from backend.ai.providers.base.config import ProviderConfig
from backend.ai.providers.base.contract import BaseProvider, ProviderResponse
from backend.ai.providers.base.defaults import get_provider_default

DUMMY_TEXT = (
    "AI is not configured. Set an API key for a supported provider "
    "(e.g. AI_OPENAI_API_KEY, AI_GEMINI_API_KEY, AI_GROQ_API_KEY, "
    "AI_OPENROUTER_API_KEY) to enable AI."
)
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
        if config is None:
            config = get_provider_default("dummy")
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

    async def chat(self, messages: list[dict[str, Any]], **kwargs: Any) -> ProviderResponse:
        """Deterministic response — always ``success=False``.

        A real AI answer was never produced, so a success flag would be
        fake. The ProviderManager surfaces this message as the final
        failure when no provider is configured.
        """
        return ProviderResponse(
            text=DUMMY_TEXT,
            provider_name=self.name,
            success=False,
            usage={
                "prompt_tokens": DUMMY_PROMPT_TOKENS,
                "completion_tokens": DUMMY_COMPLETION_TOKENS,
            },
            metadata={
                "deterministic": True,
                "version": self.PROVIDER_VERSION,
                "reason": "no_provider_configured",
            },
        )

    def count_tokens(self, text: str) -> int:
        if not text:
            return 0
        return max(1, len(text) // 4)

    def provider_name(self) -> str:
        return self.name

    def provider_version(self) -> str:
        return self.PROVIDER_VERSION
