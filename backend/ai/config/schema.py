"""
Schema — immutable configuration snapshot for the AI Engine.

``ConfigSnapshot`` is the only configuration object the Engine and
Dispatcher ever receive. It is a frozen dataclass — once created, it
cannot be mutated. The ``ConfigManager`` produces snapshots from the
mutable ``AIConfig`` and hands them to the engine.

This separation guarantees that no downstream layer can accidentally
modify configuration mid-execution. The conversation layer, prompt
builder, and provider factory never see a mutable config object.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ConfigSnapshot:
    """Immutable snapshot of the active AI configuration.

    Produced by ``ConfigManager.snapshot()``. Passed to the Engine
    and downstream layers. Cannot be modified after creation.
    """

    enabled: bool
    provider: str
    model: str
    temperature: float
    top_p: float
    max_tokens: int
    timeout: int
    retry_count: int
    system_prompt: str
    history_budget: int
    tool_budget: int
    streaming_enabled: bool
    vision_enabled: bool
    reasoning_enabled: bool
    developer_mode: bool
