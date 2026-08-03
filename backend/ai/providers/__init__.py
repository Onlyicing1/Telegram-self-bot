"""
Provider Layer — concrete AI provider implementations.

This package is the first layer that knows AI providers exist. It
receives ``PromptPackage`` objects (from the Prompt Builder Layer) and
routes them to concrete provider classes.

Currently, every provider returns ``NOT_IMPLEMENTED`` or
``AI_DISABLED``. No HTTP request, no SDK, no API call, no network I/O.
This package defines the architecture only.

Public API::

    from backend.ai.providers import (
        BaseProvider,
        ProviderConfig,
        ProviderResponse,
        ProviderRegistry,
        ProviderFactory,
        DummyProvider,
        GeminiProvider,
        OpenAIProvider,
        OpenRouterProvider,
        NOT_IMPLEMENTED,
        AI_DISABLED,
    )

Provider hierarchy::

    BaseProvider (abstract)
        ├── GeminiProvider      → returns NOT_IMPLEMENTED
        ├── OpenAIProvider      → returns NOT_IMPLEMENTED
        ├── OpenRouterProvider  → returns NOT_IMPLEMENTED
        └── DummyProvider       → returns AI_DISABLED (default)

Adding a new provider (for future developers):
  1. Create ``backend/ai/providers/<name>.py``.
  2. Define a class inheriting from ``BaseProvider``.
  3. Set ``PROVIDER_NAME`` to a unique string.
  4. Implement ``generate(prompt_package) -> ProviderResponse``.
  5. Add the class to ``_PROVIDER_CLASSES`` in ``factory.py``.
  6. Done. The factory, registry, and all callers work automatically.

Future execution flow::

    User
      ↓
    Conversation
      ↓
    Prompt Builder  →  PromptPackage
      ↓
    Provider Factory  →  ProviderRegistry
      ↓
    Gemini / OpenAI / OpenRouter / Dummy
      ↓
    ProviderResponse
"""
from backend.ai.providers.base import (
    AI_DISABLED,
    NOT_IMPLEMENTED,
    BaseProvider,
    ProviderConfig,
    ProviderResponse,
)
from backend.ai.providers.dummy import DummyProvider
from backend.ai.providers.factory import ProviderFactory
from backend.ai.providers.gemini import GeminiProvider
from backend.ai.providers.openai import OpenAIProvider
from backend.ai.providers.openrouter import OpenRouterProvider
from backend.ai.providers.registry import ProviderRegistry

__all__ = [
    # Base
    "BaseProvider",
    "ProviderConfig",
    "ProviderResponse",
    "NOT_IMPLEMENTED",
    "AI_DISABLED",
    # Registry & Factory
    "ProviderRegistry",
    "ProviderFactory",
    # Concrete providers
    "DummyProvider",
    "GeminiProvider",
    "OpenAIProvider",
    "OpenRouterProvider",
]
