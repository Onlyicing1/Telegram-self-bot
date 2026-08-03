"""
DummyProvider — the default provider. Does nothing.

This provider is the safe fallback for the entire system. It is
registered as the default in ``ProviderRegistry`` and activated by
default in ``ProviderFactory``. Every request that routes through the
Provider Layer hits the DummyProvider, which returns a deterministic
``AI_DISABLED`` response.

This guarantees zero behavior change: no network call, no model, no
API key, no environment variable. The bot continues to operate exactly
as it did before the Provider Layer was introduced.

The DummyProvider returns a deterministic response with usage data
so the pipeline and inspector can exercise the full path without any
external dependency::

    {
        "success": false,
        "provider": "dummy",
        "text": "AI_DISABLED",
        "usage": {
            "prompt_tokens": 420,
            "completion_tokens": 18
        }
    }

Future: when a real provider is configured and enabled, the factory
will switch the active provider away from Dummy. Until then, everything
routes here.
"""
from __future__ import annotations

from typing import Any

from backend.ai.prompt.builder import PromptPackage
from backend.ai.providers.base import (
    AI_DISABLED,
    BaseProvider,
    ProviderConfig,
    ProviderResponse,
)

PROVIDER_NAME = "dummy"
PROVIDER_VERSION = "1.0.0"

DUMMY_TEXT = "AI pipeline operational."
DUMMY_PROMPT_TOKENS = 420
DUMMY_COMPLETION_TOKENS = 18


class DummyProvider(BaseProvider):
    """The no-op default provider.

    Returns a deterministic ``AI_DISABLED`` response for every request.
    No side effects, no I/O, no state. This is the provider the system
    routes through when no real provider is configured.

    The response includes deterministic usage data so downstream
    layers (pipeline, inspector) can exercise the full path without
    any external dependency.
    """

    PROVIDER_NAME = PROVIDER_NAME
    PROVIDER_VERSION = PROVIDER_VERSION

    def __init__(self, config: ProviderConfig | None = None) -> None:
        if config is None:
            config = ProviderConfig(name=PROVIDER_NAME, enabled=True)
        super().__init__(config)

    def initialize(self) -> None:
        """No-op initialization. Nothing to load or connect."""
        pass

    def shutdown(self) -> None:
        """No-op shutdown. Nothing to release."""
        pass

    def health(self) -> dict[str, Any]:
        """Return a deterministic health-check dict.

        The DummyProvider is always healthy — it has no external
        dependencies.
        """
        return {
            "healthy": True,
            "provider": self.name,
            "version": self.PROVIDER_VERSION,
            "enabled": self.is_enabled,
        }

    def generate(self, prompt_package: PromptPackage) -> ProviderResponse:
        """Return a deterministic ``AI_DISABLED`` response.

        The DummyProvider never makes any request. It exists solely to
        provide a safe, predictable default so the rest of the system
        can wire through the Provider Layer without any behavior change.

        The response includes deterministic usage data for downstream
        token-budget tracking.
        """
        return ProviderResponse(
            text=AI_DISABLED,
            provider_name=self.name,
            success=False,
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
        """Return a deterministic token estimate.

        Uses the prompt package's own estimate if available, otherwise
        returns the fixed dummy estimate.
        """
        try:
            return prompt_package.estimated_tokens.estimated_total
        except Exception:
            return DUMMY_PROMPT_TOKENS + DUMMY_COMPLETION_TOKENS

    def provider_name(self) -> str:
        """Return the provider's unique name."""
        return self.name

    def provider_version(self) -> str:
        """Return the provider's version string."""
        return self.PROVIDER_VERSION
