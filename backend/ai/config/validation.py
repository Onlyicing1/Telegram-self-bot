"""
Validation — rejects invalid AIConfig values before they are applied.

Every ``ConfigManager.set()`` call runs through ``validate_field()``
which checks the field against its allowed range/type. Invalid values
raise ``ConfigValidationError`` with a descriptive message.

Validation rules:
  - enabled:              must be bool
  - provider:             must be a non-empty string present in the
                          provider registry (if a registry is injected)
  - model:                must be a non-empty string
  - temperature:          float in [0.0, 2.0]
  - top_p:                float in [0.0, 1.0]
  - max_tokens:           positive int
  - timeout:              positive int
  - retry_count:          non-negative int
  - system_prompt:        must be a string (may be empty)
  - history_budget:       positive int
  - tool_budget:          positive int
  - streaming_enabled:    must be bool
  - vision_enabled:       must be bool
  - reasoning_enabled:    must be bool
  - developer_mode:       must be bool
"""
from __future__ import annotations

from typing import Any, Callable

from backend.ai.config import defaults


class ConfigValidationError(ValueError):
    """Raised when a configuration value is invalid."""


def _check_bool(field: str, value: Any) -> bool:
    if not isinstance(value, bool):
        raise ConfigValidationError(f"{field} must be bool, got {type(value).__name__}")
    return value


def _check_str(field: str, value: Any, allow_empty: bool = True) -> str:
    if not isinstance(value, str):
        raise ConfigValidationError(f"{field} must be str, got {type(value).__name__}")
    if not allow_empty and not value.strip():
        raise ConfigValidationError(f"{field} must be non-empty")
    return value


def _check_float_range(field: str, value: Any, lo: float, hi: float) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ConfigValidationError(f"{field} must be float, got {type(value).__name__}")
    v = float(value)
    if v < lo or v > hi:
        raise ConfigValidationError(f"{field} must be in [{lo}, {hi}], got {v}")
    return v


def _check_positive_int(field: str, value: Any) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ConfigValidationError(f"{field} must be int, got {type(value).__name__}")
    if value <= 0:
        raise ConfigValidationError(f"{field} must be positive, got {value}")
    return value


def _check_non_negative_int(field: str, value: Any) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ConfigValidationError(f"{field} must be int, got {type(value).__name__}")
    if value < 0:
        raise ConfigValidationError(f"{field} must be non-negative, got {value}")
    return value


def _check_provider(field: str, value: Any, provider_check: Callable[[str], bool] | None) -> str:
    v = _check_str(field, value, allow_empty=False)
    if provider_check is not None and not provider_check(v):
        raise ConfigValidationError(f"{field}='{v}' is not a registered provider")
    return v


_VALIDATORS: dict[str, Callable[..., Any]] = {
    "enabled": _check_bool,
    "provider": _check_str,
    "model": _check_str,
    "temperature": _check_float_range,
    "top_p": _check_float_range,
    "max_tokens": _check_positive_int,
    "timeout": _check_positive_int,
    "retry_count": _check_non_negative_int,
    "system_prompt": _check_str,
    "history_budget": _check_positive_int,
    "tool_budget": _check_positive_int,
    "streaming_enabled": _check_bool,
    "vision_enabled": _check_bool,
    "reasoning_enabled": _check_bool,
    "developer_mode": _check_bool,
}

_FLOAT_RANGES: dict[str, tuple[float, float]] = {
    "temperature": (0.0, 2.0),
    "top_p": (0.0, 1.0),
}


def validate_field(
    field: str,
    value: Any,
    provider_check: Callable[[str], bool] | None = None,
) -> Any:
    """Validate a single config field. Returns the coerced value or raises."""
    if field not in _VALIDATORS:
        raise ConfigValidationError(f"Unknown config field: {field!r}")

    validator = _VALIDATORS[field]

    if field == "provider":
        return _check_provider(field, value, provider_check)
    if field in _FLOAT_RANGES:
        lo, hi = _FLOAT_RANGES[field]
        return _check_float_range(field, value, lo, hi)
    if field in ("max_tokens", "timeout", "history_budget", "tool_budget"):
        return _check_positive_int(field, value)
    if field == "retry_count":
        return _check_non_negative_int(field, value)
    if field == "model":
        return _check_str(field, value, allow_empty=False)

    return validator(field, value)


def validate_all(
    config_dict: dict[str, Any],
    provider_check: Callable[[str], bool] | None = None,
) -> dict[str, Any]:
    """Validate every field in a config dict. Returns validated dict."""
    validated: dict[str, Any] = {}
    for field, value in config_dict.items():
        validated[field] = validate_field(field, value, provider_check)
    return validated
