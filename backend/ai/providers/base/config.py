"""
ProviderConfig — configuration injected into every provider instance.

A provider never reads environment variables or global state. It
receives everything it needs through this dataclass. The
``ProviderManager`` constructs a ``ProviderConfig`` from the current
``ConfigSnapshot`` before each call.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ProviderConfig:
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
