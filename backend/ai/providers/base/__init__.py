"""Base package — abstract provider contract, capabilities, config, exceptions."""
from backend.ai.providers.base.capabilities import ProviderCapabilities
from backend.ai.providers.base.config import ProviderConfig
from backend.ai.providers.base.contract import (
    AI_DISABLED,
    NOT_IMPLEMENTED,
    BaseProvider,
    ProviderResponse,
)
from backend.ai.providers.base.exceptions import (
    ProviderConfigurationError,
    ProviderExecutionError,
    ProviderInitializationError,
    ProviderNotFound,
    ProviderUnavailable,
)

__all__ = [
    "BaseProvider",
    "ProviderConfig",
    "ProviderResponse",
    "ProviderCapabilities",
    "NOT_IMPLEMENTED",
    "AI_DISABLED",
    "ProviderNotFound",
    "ProviderInitializationError",
    "ProviderExecutionError",
    "ProviderUnavailable",
    "ProviderConfigurationError",
]
