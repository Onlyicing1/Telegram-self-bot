"""
AIResponse — the immutable output object for the AI Session pipeline.

A ``AIResponse`` is produced by the pipeline after all stages complete.
It carries the final result text, the provider that produced it, timing
information, and (future) tool calls. The caller receives this object
and decides how to present it — the pipeline itself never touches
Telegram.

This object is frozen: once created, it cannot be modified.

Fields (from AI_MASTER_DESIGN.md §4.4, §25):
  success:         Whether the pipeline completed without error.
  error:           Error message if ``success`` is False, empty otherwise.
  provider:        Name of the provider that produced the response.
  text:            The generated text (or disabled/not-implemented message).
  estimated_tokens: Estimated total tokens (input + output) for this turn.
  execution_time:  Wall-clock seconds from pipeline start to finish.
  tool_calls:      List of tool-call dicts (future — empty for now).
  metadata:        Arbitrary extra metadata from the pipeline.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class AIResponse:
    """The immutable output object for the AI Session pipeline.

    Attributes:
        success:          Whether the pipeline completed without error.
        error:            Error message if ``success`` is False.
        provider:         Name of the provider that produced the response.
        text:             The generated text.
        estimated_tokens: Estimated total tokens for this turn.
        execution_time:   Wall-clock seconds from start to finish.
        tool_calls:       List of tool-call dicts (future).
        metadata:         Arbitrary extra metadata.
    """

    success: bool
    error: str = ""
    provider: str = ""
    text: str = ""
    estimated_tokens: int = 0
    execution_time: float = 0.0
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
