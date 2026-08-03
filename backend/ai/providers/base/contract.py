"""
BaseProvider — the abstract contract every AI provider must implement.

Every provider in the system inherits from this class and implements
the same interface. The Conversation Manager and Dispatcher never
know which provider is active — they call ``ProviderManager.chat()``
and receive a ``ProviderResponse``.

The contract:

    initialize()          → None  (setup: validate config, open clients)
    shutdown()            → None  (cleanup: close clients)
    chat(messages)        → ProviderResponse   (single-shot completion)
    vision(messages, img)→ ProviderResponse   (image + text)
    stream(messages)     → Iterator[ProviderResponse]  (token-by-token)
    count_tokens(text)   → int
    supports_tools()     → bool
    supports_vision()    → bool
    supports_reasoning() → bool
    health()             → dict[str, Any]

``chat()`` and ``vision()`` are the primary entry points. ``stream()``
yields incremental responses. ``count_tokens()`` gives a fast estimate
without making an API call.

Capabilities are exposed both as individual ``supports_*()`` methods
(for convenience) and as a ``capabilities`` property returning a
frozen ``ProviderCapabilities`` object (for UI/feature-gating).
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Iterator

from backend.ai.providers.base.capabilities import ProviderCapabilities
from backend.ai.providers.base.config import ProviderConfig

NOT_IMPLEMENTED = "NOT_IMPLEMENTED"
AI_DISABLED = "AI_DISABLED"


@dataclass(frozen=True)
class ProviderResponse:
    text: str
    provider_name: str
    success: bool = False
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    usage: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)


class BaseProvider(ABC):
    """Abstract base class for every AI provider.

    A provider instance is cheap to create and holds no external
    resources until ``initialize()`` is called. After ``shutdown()``
    the instance is inert and may be re-initialized.
    """

    __slots__ = ("_config",)
    PROVIDER_NAME: str = ""
    PROVIDER_VERSION: str = "0.0.0"

    def __init__(self, config: ProviderConfig | None = None) -> None:
        self._config: ProviderConfig = config or ProviderConfig()

    @property
    def name(self) -> str:
        return self.PROVIDER_NAME

    @property
    def config(self) -> ProviderConfig:
        return self._config

    @property
    def is_enabled(self) -> bool:
        return self._config.enabled

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities()

    # ── Lifecycle ──

    @abstractmethod
    def initialize(self) -> None: ...

    @abstractmethod
    def shutdown(self) -> None: ...

    # ── Core operations ──

    @abstractmethod
    def chat(self, messages: list[dict[str, Any]], **kwargs: Any) -> ProviderResponse: ...

    def vision(self, messages: list[dict[str, Any]], images: list[bytes], **kwargs: Any) -> ProviderResponse:
        return ProviderResponse(
            text=NOT_IMPLEMENTED,
            provider_name=self.name,
            success=False,
            metadata={"reason": "vision not supported"},
        )

    def stream(self, messages: list[dict[str, Any]], **kwargs: Any) -> Iterator[ProviderResponse]:
        yield ProviderResponse(
            text=NOT_IMPLEMENTED,
            provider_name=self.name,
            success=False,
            metadata={"reason": "streaming not supported"},
        )

    @abstractmethod
    def count_tokens(self, text: str) -> int: ...

    # ── Capability queries ──

    def supports_tools(self) -> bool:
        return self.capabilities.supports_tools

    def supports_vision(self) -> bool:
        return self.capabilities.supports_images

    def supports_reasoning(self) -> bool:
        return self.capabilities.supports_reasoning

    # ── Health ──

    @abstractmethod
    def health(self) -> dict[str, Any]: ...

    # ── Introspection ──

    def provider_name(self) -> str:
        return self.name

    def provider_version(self) -> str:
        return self.PROVIDER_VERSION

    # ── Helpers ──

    def _disabled_response(self) -> ProviderResponse:
        return ProviderResponse(text=AI_DISABLED, provider_name=self.name, success=False)

    def _not_implemented_response(self) -> ProviderResponse:
        return ProviderResponse(text=NOT_IMPLEMENTED, provider_name=self.name, success=False)
