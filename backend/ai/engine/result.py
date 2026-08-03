"""
EngineResult — the immutable output of every AI Engine execution.

Returned by ``Engine.execute()`` and ``engine_health()``. Once created,
it cannot be modified. Every field has a default so a result can be
constructed even when the engine fails mid-execution.

Attributes:
    success:           Whether the full pipeline completed without error.
    provider:          Name of the provider that ran (e.g. ``"dummy"``).
    model:             Name of the model that ran (e.g. ``"dummy-1"``).
    latency:           Wall-clock seconds from dispatch start to finish.
    prompt_tokens:     Estimated tokens consumed by the prompt.
    completion_tokens: Estimated tokens produced by the provider.
    total_tokens:      ``prompt_tokens + completion_tokens``.
    response:          The provider's response text (may be ``"AI_DISABLED"``).
    warnings:          Non-fatal warnings collected during execution.
    errors:            Fatal error messages (empty when ``success`` is True).
    metadata:          Arbitrary extra metadata from the pipeline stages.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class EngineResult:
    """The immutable result of a single AI Engine execution."""

    success: bool = False
    provider: str = ""
    model: str = ""
    latency: float = 0.0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    response: str = ""
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
