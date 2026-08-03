"""
AI Runtime Inspector — temporary developer-only diagnostics layer.

This package provides read-only inspection of the AI runtime. It
produces one immutable ``RuntimeSnapshot`` containing the status of
every AI layer, the current provider, session state, and overall
pipeline health.

Constraints:
  - NEVER calls Telegram APIs.
  - No polling, no loops, no scheduler.
  - No network, no provider execution, no SDK.
  - No secrets, no API keys, no prompts, no user messages.
  - Does NOT modify any existing runtime.

Public API::

    from backend.ai.inspector import (
        AIInspector,
        RuntimeSnapshot,
        LayerInfo,
        LayerStatus,
        HealthState,
    )

    inspector = AIInspector(ai_session)
    snapshot = inspector.get_ai_runtime_snapshot()
    # snapshot.health → HealthState.HEALTHY
    # snapshot.current_provider → "dummy"
    # snapshot.session_state → "Idle"

This inspector is temporary. After the first real provider is
integrated and verified, it can be disabled or hidden behind
Developer Mode.
"""
from backend.ai.inspector.health import HealthChecker
from backend.ai.inspector.inspector import AIInspector
from backend.ai.inspector.snapshot import LayerInfo, RuntimeSnapshot
from backend.ai.inspector.status import HealthState, LayerStatus

__all__ = [
    "AIInspector",
    "RuntimeSnapshot",
    "LayerInfo",
    "LayerStatus",
    "HealthState",
    "HealthChecker",
]
