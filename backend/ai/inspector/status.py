"""
Status enums for the AI Runtime Inspector.

Two enums:

  - ``LayerStatus`` — the readiness state of each AI layer.
  - ``HealthState`` — the overall health of the AI pipeline.

These are diagnostic-only values. They carry no secrets, no user data,
and no prompt content.
"""
from __future__ import annotations

from enum import Enum


class LayerStatus(str, Enum):
    """Readiness state for a single AI layer.

    READY      — Layer is constructed and callable.
    NOT_READY  — Layer exists but cannot process requests.
    OFFLINE    — Layer is not constructed or not injected.
    ERROR      — Layer threw an exception during inspection.
    """

    READY = "READY"
    NOT_READY = "NOT_READY"
    OFFLINE = "OFFLINE"
    ERROR = "ERROR"


class HealthState(str, Enum):
    """Overall health of the AI pipeline.

    HEALTHY    — All layers READY, pipeline callable.
    DEGRADED   — Pipeline callable but one or more layers NOT_READY.
    OFFLINE    — Pipeline not callable (a critical layer is OFFLINE).
    ERROR      — Inspection itself encountered an error.
    """

    HEALTHY = "Healthy"
    DEGRADED = "Degraded"
    OFFLINE = "Offline"
    ERROR = "Error"
