"""
AI Session State — lifecycle states for an AI session.

An AI session progresses through a simple lifecycle:

    CREATED → ACTIVE → COMPLETED → CLOSED
                        ↓
                      ERROR → CLOSED

These states are distinct from the ``ConversationState`` state machine
in ``backend.ai.conversation.state``. That state machine tracks the
*conversation* (IDLE, WAITING_USER, EXECUTING, etc.). This enum tracks
the *AI session* lifecycle — whether a session exists, is running, has
finished, or has been destroyed.

States:
  CREATED:   Session has been created but no request has been processed.
  ACTIVE:    A request is currently being processed through the pipeline.
  COMPLETED: The last request finished successfully.
  ERROR:     The last request failed.
  CLOSED:    Session has been destroyed and can no longer be used.
"""
from __future__ import annotations

from enum import Enum


class AISessionState(str, Enum):
    """Lifecycle states for an AI session."""

    CREATED = "created"
    ACTIVE = "active"
    COMPLETED = "completed"
    ERROR = "error"
    CLOSED = "closed"
