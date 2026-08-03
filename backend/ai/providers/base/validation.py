"""
Validation — structured validation for ProviderConfig objects.

Every ``ProviderConfigManager.update()`` call runs through
``validate_provider_config()`` which checks every field and returns
a ``ValidationResult`` with a list of structured issues.

Validation checks:
  - Missing API Key      → provider needs an api_key (except dummy)
  - Missing Base URL     → providers that require a base_url must have one
  - Invalid Model        → default_model must be non-empty (except dummy)
  - Disabled Provider    → enabled=False is valid but flagged as informational
  - Temperature range    → [0.0, 2.0]
  - Top P range          → [0.0, 1.0]
  - Max tokens           → positive int
  - Timeout              → positive int
  - Retry count          → non-negative int

The result is always a ``ValidationResult`` — never an exception.
Callers inspect ``result.valid`` and ``result.issues`` to decide
what to do.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from backend.ai.providers.base.config import ProviderConfig


@dataclass
class ValidationIssue:
    """A single validation problem."""
    field: str
    code: str
    message: str
    severity: str = "error"

    def as_dict(self) -> dict[str, str]:
        return {
            "field": self.field,
            "code": self.code,
            "message": self.message,
            "severity": self.severity,
        }


@dataclass
class ValidationResult:
    """Structured result of validating a ProviderConfig."""
    valid: bool
    provider_name: str
    issues: list[ValidationIssue] = field(default_factory=list)

    @property
    def errors(self) -> list[ValidationIssue]:
        return [i for i in self.issues if i.severity == "error"]

    @property
    def warnings(self) -> list[ValidationIssue]:
        return [i for i in self.issues if i.severity == "warning"]

    def add(self, field_name: str, code: str, message: str, severity: str = "error") -> None:
        self.issues.append(ValidationIssue(field=field_name, code=code, message=message, severity=severity))
        if severity == "error":
            self.valid = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "valid": self.valid,
            "provider_name": self.provider_name,
            "issues": [i.as_dict() for i in self.issues],
            "error_count": len(self.errors),
            "warning_count": len(self.warnings),
        }


# Providers that don't need an API key
_PROVIDERS_NO_KEY = {"dummy"}

# Providers that need a base_url
_PROVIDERS_NEED_URL = {"openrouter", "custom"}

# Providers that need a default_model
_PROVIDERS_NEED_MODEL = {"gemini", "openai", "openrouter", "glm", "claude", "custom"}


def validate_provider_config(config: ProviderConfig) -> ValidationResult:
    """Validate a ProviderConfig. Returns a structured ValidationResult."""
    result = ValidationResult(valid=True, provider_name=config.provider_name)

    name = config.provider_name

    # ── Missing API Key ──
    if name not in _PROVIDERS_NO_KEY and not config.api_key.strip():
        result.add("api_key", "MISSING_API_KEY", f"Provider '{name}' requires an API key")

    # ── Missing Base URL ──
    if name in _PROVIDERS_NEED_URL and not config.base_url.strip():
        result.add("base_url", "MISSING_BASE_URL", f"Provider '{name}' requires a base URL")

    # ── Invalid Model ──
    if name in _PROVIDERS_NEED_MODEL and not config.default_model.strip():
        result.add("default_model", "INVALID_MODEL", f"Provider '{name}' requires a default model")

    # ── Disabled Provider (warning, not error) ──
    if not config.enabled:
        result.add("enabled", "DISABLED_PROVIDER", f"Provider '{name}' is disabled", severity="warning")

    # ── Temperature range ──
    if not isinstance(config.temperature, (int, float)) or isinstance(config.temperature, bool):
        result.add("temperature", "INVALID_TYPE", "temperature must be a number")
    elif config.temperature < 0.0 or config.temperature > 2.0:
        result.add("temperature", "OUT_OF_RANGE", f"temperature must be in [0.0, 2.0], got {config.temperature}")

    # ── Top P range ──
    if not isinstance(config.top_p, (int, float)) or isinstance(config.top_p, bool):
        result.add("top_p", "INVALID_TYPE", "top_p must be a number")
    elif config.top_p < 0.0 or config.top_p > 1.0:
        result.add("top_p", "OUT_OF_RANGE", f"top_p must be in [0.0, 1.0], got {config.top_p}")

    # ── Max tokens ──
    if not isinstance(config.max_tokens, int) or isinstance(config.max_tokens, bool):
        result.add("max_tokens", "INVALID_TYPE", "max_tokens must be int")
    elif config.max_tokens <= 0:
        result.add("max_tokens", "OUT_OF_RANGE", f"max_tokens must be positive, got {config.max_tokens}")

    # ── Timeout ──
    if not isinstance(config.timeout, int) or isinstance(config.timeout, bool):
        result.add("timeout", "INVALID_TYPE", "timeout must be int")
    elif config.timeout <= 0:
        result.add("timeout", "OUT_OF_RANGE", f"timeout must be positive, got {config.timeout}")

    # ── Retry count ──
    if not isinstance(config.retry_count, int) or isinstance(config.retry_count, bool):
        result.add("retry_count", "INVALID_TYPE", "retry_count must be int")
    elif config.retry_count < 0:
        result.add("retry_count", "OUT_OF_RANGE", f"retry_count must be non-negative, got {config.retry_count}")

    return result
