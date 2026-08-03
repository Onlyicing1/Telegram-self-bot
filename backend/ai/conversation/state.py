"""
Conversation State Machine — deterministic runtime states for the AI layer.

The conversation layer operates as a finite state machine. At any given
moment, a conversation is in exactly one state. Transitions are explicit
and validated — no hidden state, no implicit behavior.

States (from AI_MASTER_DESIGN.md §24):
    IDLE            — ready, no active request
    WAITING_USER    — waiting for owner input (text or confirmation)
    WAITING_REPLY   — waiting for owner to reply to a message
    WAITING_TOOL    — a tool is executing, AI is waiting for its result
    EXECUTING       — AI is processing (building prompt, calling model)
    COMPLETED       — request finished, response delivered
    CANCELLED       — owner or system cancelled the request
    ERROR           — an error occurred, recovery needed

Transitions are validated against an adjacency table. Illegal transitions
raise ``InvalidTransition`` rather than silently succeeding.
"""
from __future__ import annotations

from enum import Enum
from typing import FrozenSet


class ConversationState(str, Enum):
    """Deterministic states for the conversation state machine."""

    IDLE = "idle"
    WAITING_USER = "waiting_user"
    WAITING_REPLY = "waiting_reply"
    WAITING_TOOL = "waiting_tool"
    EXECUTING = "executing"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    ERROR = "error"


_TRANSITIONS: dict[ConversationState, frozenset[ConversationState]] = {
    ConversationState.IDLE: frozenset({
        ConversationState.WAITING_USER,
        ConversationState.WAITING_REPLY,
        ConversationState.EXECUTING,
        ConversationState.CANCELLED,
    }),
    ConversationState.WAITING_USER: frozenset({
        ConversationState.EXECUTING,
        ConversationState.CANCELLED,
        ConversationState.ERROR,
    }),
    ConversationState.WAITING_REPLY: frozenset({
        ConversationState.EXECUTING,
        ConversationState.CANCELLED,
        ConversationState.ERROR,
    }),
    ConversationState.EXECUTING: frozenset({
        ConversationState.WAITING_TOOL,
        ConversationState.WAITING_USER,
        ConversationState.COMPLETED,
        ConversationState.ERROR,
        ConversationState.CANCELLED,
    }),
    ConversationState.WAITING_TOOL: frozenset({
        ConversationState.EXECUTING,
        ConversationState.COMPLETED,
        ConversationState.ERROR,
        ConversationState.CANCELLED,
    }),
    ConversationState.COMPLETED: frozenset({
        ConversationState.IDLE,
        ConversationState.WAITING_USER,
        ConversationState.ERROR,
    }),
    ConversationState.CANCELLED: frozenset({
        ConversationState.IDLE,
    }),
    ConversationState.ERROR: frozenset({
        ConversationState.IDLE,
        ConversationState.WAITING_USER,
        ConversationState.CANCELLED,
    }),
}


class InvalidTransition(Exception):
    """Raised when a state transition is not in the adjacency table."""


def can_transition(from_state: ConversationState, to_state: ConversationState) -> bool:
    """Check whether a transition is allowed."""
    return to_state in _TRANSITIONS.get(from_state, frozenset())


def validate_transition(from_state: ConversationState, to_state: ConversationState) -> None:
    """Validate a transition. Raises ``InvalidTransition`` if not allowed."""
    if not can_transition(from_state, to_state):
        raise InvalidTransition(
            f"Cannot transition from {from_state.value} to {to_state.value}"
        )


def allowed_transitions(state: ConversationState) -> FrozenSet[ConversationState]:
    """Return the set of states reachable from ``state``."""
    return _TRANSITIONS.get(state, frozenset())
