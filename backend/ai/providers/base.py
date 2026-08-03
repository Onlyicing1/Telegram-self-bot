"""
BaseProvider — the abstract base class every AI provider must inherit.

This is the Provider Layer: the first layer that knows AI providers exist.
It receives a ``PromptPackage`` (produced by the Prompt Builder Layer) and
returns a ``ProviderResponse``.

Every concrete provider inherits from ``BaseProvider`` and implements the
full provider contract:

    initialize()        — start the provider (load SDK, test connection)
    shutdown()          — release resources (close connections)
    health()            — return a health-check dict
    generate()          — process a PromptPackage, return a ProviderResponse
    estimate_tokens()   — estimate token count for a PromptPackage
    provider_name()     — return the provider's unique name
    provider_version()  — return the provider's version string

Currently, no provider makes any network call. All concrete
implementations return ``NOT_IMPLEMENTED`` or ``AI_DISABLED``.

Provider contract (for future developers adding a new provider):
  1. Subclass ``BaseProvider``.
  2. Set the class-level ``PROVIDER_NAME`` attribute to a unique identifier.
  3. Set the class-level ``PROVIDER_VERSION`` attribute.
  4. Implement all abstract methods.
  5. Add the class to ``_PROVIDER_CLASSES`` in ``factory.py``.
  6. No other file needs to change. The factory and registry handle the rest.

Architecture (from AI_MASTER_DESIGN.md §4.5)::

    PromptPackage (from Prompt Builder)
           │
           ▼
    ┌──────────────────────────────────────────────┐
    │                BaseProvider                    │
    │  ├─ name: str           (unique identifier)   │
    │  ├─ config: ProviderConfig  (model, temp, etc) │
    │  └─ generate(prompt_package) → ProviderResponse│
    └──────────────────────┬───────────────────────┘
                           │
           ┌───────────────┼───────────────┐
           ▼               ▼               ▼
    ┌──────────┐  ┌──────────┐  ┌──────────────┐
    │ Gemini   │  │ OpenAI   │  │ OpenRouter    │
    │ Provider │  │ Provider │  │ Provider      │
    └──────────┘  └──────────┘  └──────────────┘
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from backend.ai.prompt.builder import PromptPackage

logger = logging.getLogger(__name__)

NOT_IMPLEMENTED = "NOT_IMPLEMENTED"
AI_DISABLED = "AI_DISABLED"


@dataclass(frozen=True)
class ProviderResponse:
    """The response object returned by every provider's ``generate`` method.

    Attributes:
        text:           The natural-language response text. When the
                        provider is not implemented, this is
                        ``"NOT_IMPLEMENTED"`` or ``"AI_DISABLED"``.
        provider_name:  Name of the provider that produced this response.
        success:        Whether the provider actually produced a real
                        response. ``False`` for disabled/unimplemented
                        providers.
        tool_calls:     List of tool-call dicts the provider wants the
                        caller to resolve. Empty if none.
        usage:          Token usage dict for billing/budgeting. Empty
                        when no model was called.
        metadata:       Arbitrary provider-specific metadata.
    """

    text: str
    provider_name: str
    success: bool = False
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    usage: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ProviderConfig:
    """Configuration for a single AI provider instance.

    All fields are placeholders for future use. None of them are read
    or used yet — they exist so configuration can be structured now and
    populated later without schema changes.

    Attributes:
        name:            Unique provider identifier (e.g. ``"gemini"``).
        model:           Model identifier (e.g. ``"gemini-2.0-flash"``).
        temperature:     Sampling temperature (0.0–2.0).
        max_output_tokens: Maximum tokens the model may generate.
        top_p:           Nucleus sampling probability (0.0–1.0).
        timeout:         Request timeout in seconds.
        retry_count:     Number of retries on transient failure.
        enabled:         Whether this provider is enabled. When False,
                         the provider returns ``AI_DISABLED``.
        api_key:          Placeholder for the provider's API key.
                         Never read from environment variables in this
                         layer — populated by the runtime from secrets.
        extra:           Arbitrary provider-specific configuration.
    """

    name: str = ""
    model: str = ""
    temperature: float = 1.0
    max_output_tokens: int = 4096
    top_p: float = 1.0
    timeout: float = 30.0
    retry_count: int = 3
    enabled: bool = False
    api_key: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


class BaseProvider(ABC):
    """Abstract base class for all AI providers.

    Every concrete provider (Gemini, OpenAI, OpenRouter, Dummy, etc.)
    inherits from this class. The class defines the full provider
    contract that subclasses must implement:

      - ``initialize()``        — start the provider
      - ``shutdown()``          — release resources
      - ``health()``            — return health-check dict
      - ``generate()``          — process a PromptPackage
      - ``estimate_tokens()``   — estimate token count
      - ``provider_name()``     — return the provider name
      - ``provider_version()``  — return the provider version

    The ``generate`` method is the primary entry point. It receives a
    ``PromptPackage`` (the output of the Prompt Builder Layer) and
    returns a ``ProviderResponse``.

    Currently, no provider makes any network call. All concrete
    implementations return ``NOT_IMPLEMENTED`` or ``AI_DISABLED``.
    """

    __slots__ = ("_config",)

    PROVIDER_NAME: str = ""
    PROVIDER_VERSION: str = "0.0.0"

    def __init__(self, config: ProviderConfig | None = None) -> None:
        self._config: ProviderConfig = config or ProviderConfig()

    @property
    def name(self) -> str:
        """Unique provider identifier (e.g. ``"gemini"``, ``"openai"``).

        Defaults to the class-level ``PROVIDER_NAME`` attribute.
        Subclasses must set ``PROVIDER_NAME``.
        """
        return self.PROVIDER_NAME

    @property
    def config(self) -> ProviderConfig:
        """The provider's configuration object."""
        return self._config

    @property
    def is_enabled(self) -> bool:
        """Whether this provider is enabled in its configuration."""
        return self._config.enabled

    @abstractmethod
    def initialize(self) -> None:
        """Start the provider.

        Called once before the provider is used. Future real providers
        will load SDKs, test connections, and validate credentials here.

        Raises:
            ``ProviderInitializationError`` if startup fails.
        """
        ...

    @abstractmethod
    def shutdown(self) -> None:
        """Release all resources.

        Called when the provider is being removed or the system is
        shutting down. Must be safe to call even if ``initialize()``
        was never called or failed.
        """
        ...

    @abstractmethod
    def health(self) -> dict[str, Any]:
        """Return a health-check dict.

        Contains at minimum:
            ``"healthy"``: bool
            ``"provider"``: str

        Future real providers will also include latency, model status,
        rate-limit info, etc.
        """
        ...

    @abstractmethod
    def generate(self, prompt_package: PromptPackage) -> ProviderResponse:
        """Process a ``PromptPackage`` and return a ``ProviderResponse``.

        This is the primary entry point. Subclasses must implement it.

        Contract:
          - Receives an immutable ``PromptPackage``.
          - Returns a ``ProviderResponse`` — never raises.
          - Must not perform any network I/O until the provider is
            fully implemented in a future phase.

        Currently all implementations return ``NOT_IMPLEMENTED`` or
        ``AI_DISABLED``.
        """
        ...

    @abstractmethod
    def estimate_tokens(self, prompt_package: PromptPackage) -> int:
        """Estimate the total token count for a ``PromptPackage``.

        Returns the estimated number of tokens (input + output) that
        this provider would consume if ``generate()`` were called with
        the same package. This is used for budgeting and prompt-size
        display — not for billing.

        The DummyProvider returns a deterministic estimate based on
        the prompt package's own estimate.
        """
        ...

    @abstractmethod
    def provider_name(self) -> str:
        """Return the provider's unique name (e.g. ``"dummy"``, ``"gemini"``).

        This is the canonical accessor — ``name`` property delegates here.
        """
        ...

    @abstractmethod
    def provider_version(self) -> str:
        """Return the provider's version string (e.g. ``"1.0.0"``).

        Used for diagnostics and logging.
        """
        ...

    def _disabled_response(self) -> ProviderResponse:
        """Helper: build a standard AI_DISABLED response."""
        return ProviderResponse(
            text=AI_DISABLED,
            provider_name=self.name,
            success=False,
        )

    def _not_implemented_response(self) -> ProviderResponse:
        """Helper: build a standard NOT_IMPLEMENTED response."""
        return ProviderResponse(
            text=NOT_IMPLEMENTED,
            provider_name=self.name,
            success=False,
        )
