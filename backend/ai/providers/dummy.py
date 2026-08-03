"""
DummyProvider — the default provider. Does nothing.

This provider is the safe fallback for the entire system. It is
registered as the default in ``ProviderRegistry`` and activated by
default in ``ProviderFactory``. Every request that routes through the
Provider Layer hits the DummyProvider, which returns ``AI_DISABLED``.

This guarantees zero behavior change: no network call, no model, no
API key, no environment variable. The bot continues to operate exactly
as it did before the Provider Layer was introduced.

Future: when a real provider is configured and enabled, the factory
will switch the active provider away from Dummy. Until then, everything
routes here.
"""
from __future__ import annotations

from backend.ai.prompt.builder import PromptPackage
from backend.ai.providers.base import BaseProvider, ProviderConfig, ProviderResponse

PROVIDER_NAME = "dummy"


class DummyProvider(BaseProvider):
    """The no-op default provider.

    Returns ``AI_DISABLED`` for every request. No side effects, no I/O,
    no state. This is the provider the system routes through when no
    real provider is configured.
    """

    PROVIDER_NAME = PROVIDER_NAME

    def __init__(self, config: ProviderConfig | None = None) -> None:
        if config is None:
            config = ProviderConfig(name=PROVIDER_NAME, enabled=True)
        super().__init__(config)

    def generate(self, prompt_package: PromptPackage) -> ProviderResponse:
        """Return ``AI_DISABLED`` unconditionally.

        The DummyProvider never makes any request. It exists solely to
        provide a safe, predictable default so the rest of the system
        can wire through the Provider Layer without any behavior change.
        """
        return self._disabled_response()
