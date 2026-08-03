"""
Provider Layer — concrete AI provider implementations.

This package is the first layer that knows AI providers exist. The
``ProviderManager`` is the sole public entry point for the rest of
the system. The Conversation Manager, Dispatcher, and Prompt
Builder never reference a provider by name — they call
``ProviderManager.chat()``.

Architecture::

    backend/ai/providers/
        __init__.py          ← public exports
        base/                ← abstract contract
            __init__.py
            contract.py      ← BaseProvider, ProviderResponse
            capabilities.py  ← ProviderCapabilities
            config.py        ← ProviderConfig
            defaults.py      ← provider config defaults
            exceptions.py    ← ProviderError hierarchy
            validation.py    ← ValidationResult, validate_provider_config
        dummy/               ← always-on fallback
            __init__.py
            provider.py      ← DummyProvider
        registry/            ← provider storage
            __init__.py
            registry.py      ← ProviderRegistry
        manager/             ← routing + fallback + metrics + config
            __init__.py
            manager.py       ← ProviderManager
            metrics.py       ← ProviderMetrics, ProviderMetricsRegistry
            config_manager.py ← ProviderConfigManager
        factory.py           ← ProviderFactory (creates registry + manager)
        gemini.py             ← stub (NOT_IMPLEMENTED)
        openai.py             ← stub (NOT_IMPLEMENTED)
        openrouter.py         ← stub (NOT_IMPLEMENTED)

Adding a new provider (for future developers):
  1. Create ``backend/ai/providers/<name>/`` with a ``provider.py``
     containing a class inheriting from ``BaseProvider``.
  2. Set ``PROVIDER_NAME`` to a unique string.
  3. Set ``PROVIDER_VERSION``.
  4. Implement all abstract methods (initialize, shutdown, chat,
     count_tokens, health).
  5. Override ``capabilities`` to declare feature support.
  6. Add defaults to ``base/defaults.py``.
  7. Add the class to ``_PROVIDER_CLASSES`` in ``factory.py``.
  8. Import and export it in ``__init__.py``.
  9. Done.
"""
from backend.ai.providers.base.capabilities import ProviderCapabilities
from backend.ai.providers.base.config import ProviderConfig
from backend.ai.providers.base.contract import (
    AI_DISABLED,
    NOT_IMPLEMENTED,
    BaseProvider,
    ProviderResponse,
)
from backend.ai.providers.base.defaults import get_provider_default, list_provider_names
from backend.ai.providers.base.exceptions import (
    ProviderConfigurationError,
    ProviderExecutionError,
    ProviderInitializationError,
    ProviderNotFound,
    ProviderUnavailable,
)
from backend.ai.providers.base.validation import (
    ValidationIssue,
    ValidationResult,
    validate_provider_config,
)
from backend.ai.providers.dummy.provider import DummyProvider
from backend.ai.providers.factory import ProviderFactory
from backend.ai.providers.gemini import GeminiProvider
from backend.ai.providers.manager.config_manager import (
    ProviderConfigManager,
    get_provider_config_manager,
)
from backend.ai.providers.manager.manager import ProviderManager
from backend.ai.providers.manager.metrics import (
    ProviderMetrics,
    ProviderMetricsRegistry,
)
from backend.ai.providers.openai import OpenAIProvider
from backend.ai.providers.openrouter import OpenRouterProvider
from backend.ai.providers.registry.registry import ProviderRegistry

__all__ = [
    # Base
    "BaseProvider",
    "ProviderConfig",
    "ProviderResponse",
    "ProviderCapabilities",
    "NOT_IMPLEMENTED",
    "AI_DISABLED",
    # Defaults
    "get_provider_default",
    "list_provider_names",
    # Validation
    "ValidationResult",
    "ValidationIssue",
    "validate_provider_config",
    # Exceptions
    "ProviderNotFound",
    "ProviderInitializationError",
    "ProviderExecutionError",
    "ProviderUnavailable",
    "ProviderConfigurationError",
    # Registry & Manager
    "ProviderRegistry",
    "ProviderManager",
    "ProviderConfigManager",
    "get_provider_config_manager",
    "ProviderMetrics",
    "ProviderMetricsRegistry",
    "ProviderFactory",
    # Concrete providers
    "DummyProvider",
    "GeminiProvider",
    "OpenAIProvider",
    "OpenRouterProvider",
]
