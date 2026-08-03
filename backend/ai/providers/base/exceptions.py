"""
Custom exceptions for the provider layer.

All exceptions inherit from ``ProviderError`` so callers can catch
the entire family with a single ``except ProviderError``.
"""
from __future__ import annotations


class ProviderError(Exception):
    """Base exception for all provider-layer errors."""


class ProviderNotFound(ProviderError):
    """Raised when a provider name is not in the registry."""


class ProviderInitializationError(ProviderError):
    """Raised when ``initialize()`` fails."""


class ProviderExecutionError(ProviderError):
    """Raised when ``chat()`` or ``stream()`` fails."""


class ProviderUnavailable(ProviderError):
    """Raised when a provider exists but cannot serve right now (unhealthy)."""


class ProviderConfigurationError(ProviderError):
    """Raised when a provider's configuration is invalid or incomplete."""
