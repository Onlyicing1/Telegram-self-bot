"""
Custom exceptions for the AI Provider Framework.

These exceptions are raised by the provider layer only. They give
callers a single, well-defined set of error types to catch — no
need to catch ``ValueError``, ``RuntimeError``, or other generic
builtins.

Exception hierarchy::

    ProviderError (base)
        ├── ProviderNotFound          — unknown provider name in factory
        ├── ProviderInitializationError — provider failed to start
        ├── ProviderExecutionError     — provider failed during generate()
        ├── ProviderUnavailable        — provider exists but can't be used
        └── ProviderConfigurationError — config is invalid or incomplete
"""
from __future__ import annotations


class ProviderError(Exception):
    """Base exception for all provider-layer errors.

    All custom provider exceptions inherit from this. Callers can
    catch ``ProviderError`` to handle any provider failure, or catch
    a specific subclass for finer control.
    """


class ProviderNotFound(ProviderError):
    """Raised when the factory cannot find a provider by name.

    Example: ``ProviderFactory.create_provider("unknown")`` raises
    this because ``"unknown"`` is not in ``_PROVIDER_CLASSES``.
    """


class ProviderInitializationError(ProviderError):
    """Raised when a provider fails to initialize.

    Future real providers will raise this if their SDK cannot be
    loaded, credentials are malformed, or a connection test fails
    during ``initialize()``.
    """


class ProviderExecutionError(ProviderError):
    """Raised when a provider fails during ``generate()``.

    Future real providers will raise this if the API returns an
    error, the response cannot be parsed, or a timeout occurs.
    """


class ProviderUnavailable(ProviderError):
    """Raised when a provider exists but cannot be used right now.

    Example: the provider is disabled in configuration, or it has
    been temporarily taken offline by the runtime supervisor.
    """


class ProviderConfigurationError(ProviderError):
    """Raised when a provider's configuration is invalid or incomplete.

    Example: a required field (like ``model``) is missing, or the
    temperature value is out of range.
    """
