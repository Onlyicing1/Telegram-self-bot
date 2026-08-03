"""Base package — abstract provider contract, capabilities, config, exceptions, validation, defaults."""
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
    "ValidationResult",
    "ValidationIssue",
    "validate_provider_config",
    "get_provider_default",
    "list_provider_names",
]
