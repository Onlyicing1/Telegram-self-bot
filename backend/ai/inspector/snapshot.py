"""
RuntimeSnapshot — the immutable diagnostics object returned by the inspector.

This object contains ONLY diagnostic information. No secrets, no API
keys, no prompt text, no user messages. It is designed to be displayed
inside the AI menu (future) or logged for development.

The snapshot is frozen: once created, it cannot be modified.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from backend.ai.inspector.status import HealthState, LayerStatus


@dataclass(frozen=True)
class LayerInfo:
    """Diagnostic info for a single AI layer.

    Attributes:
        name:    Human-readable layer name (e.g. ``"Conversation Layer"``).
        status:  ``LayerStatus`` enum value.
        detail:  Optional extra detail (e.g. provider name, session state).
    """

    name: str
    status: LayerStatus
    detail: str = ""


@dataclass(frozen=True)
class RuntimeSnapshot:
    """Immutable diagnostics snapshot of the entire AI runtime.

    Returned by ``AIInspector.get_ai_runtime_snapshot()``. Contains
    only diagnostic data — no secrets, no prompts, no user messages.

    Attributes:
        conversation_layer:  Status of the Conversation Layer.
        prompt_builder:      Status of the Prompt Builder Layer.
        provider_registry:   Status of the Provider Registry.
        pipeline:            Status of the Pipeline.
        session:             Status of the AI Session.
        current_provider:    Name of the active provider (e.g. ``"dummy"``).
        provider_loaded:     Whether a provider is loaded (YES/NO).
        session_state:       Current session lifecycle state (or ``"Idle"``).
        context_object:      Whether a ConversationContext can be built (VALID/INVALID).
        prompt_package:      Whether a PromptPackage can be built (VALID/INVALID).
        estimated_prompt_size: Estimated tokens for the last prompt (0 if none).
        last_execution:      ISO timestamp of the last pipeline run, or ``""``.
        health:              Overall ``HealthState`` of the pipeline.
        layers:              List of all ``LayerInfo`` entries (for display).
        generated_at:        UTC timestamp when this snapshot was created.
        metadata:            Extra diagnostic metadata (no secrets).
    """

    conversation_layer: LayerInfo
    prompt_builder: LayerInfo
    provider_registry: LayerInfo
    pipeline: LayerInfo
    session: LayerInfo
    current_provider: str
    provider_loaded: str
    session_state: str
    context_object: str
    prompt_package: str
    estimated_prompt_size: int
    last_execution: str
    health: HealthState
    layers: list[LayerInfo] = field(default_factory=list)
    generated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    metadata: dict[str, Any] = field(default_factory=dict)
