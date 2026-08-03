"""
Session Manager — runtime session state for a single conversation.

A session tracks the *runtime* state of one conversation: the current
flow, panel, pending callback, last tool, last reply target, and the
current runtime state. This is NOT long-term memory. It is NOT AI
history. It is pure runtime bookkeeping that lives in RAM and is lost
on restart.

Sessions are keyed by ``session_id`` (a string). The manager is
constructed once and injected — no globals, no singletons.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from backend.ai.conversation.state import (
    ConversationState,
    InvalidTransition,
    validate_transition,
)

logger = logging.getLogger(__name__)


@dataclass
class ConversationSession:
    """Runtime state for a single conversation session.

    Attributes:
        session_id:          Unique identifier for this session.
        owner_id:             Telegram user ID of the bot owner.
        chat_id:              Telegram chat ID where the conversation lives.
        state:                Current conversation state (enum).
        current_flow:         Active user flow name (e.g. ``"save"``, ``"bio"``).
        current_panel:        Current panel ID (e.g. ``"ai:new"``).
        current_category:     Current menu category (e.g. ``"ai"``).
        pending_callback:     Pending callback data string (e.g. ``"confirm_delete:5"``).
        pending_action:       Pending action description (e.g. ``"confirm"``).
        last_tool:            Name of the last tool called (e.g. ``"save"``).
        last_reply_target:    Message ID of the last reply target, if any.
        current_tool:         Name of the tool currently executing, if any.
        current_callback:     Current callback data being processed, if any.
        language:             Owner's language (e.g. ``"English"``).
        timezone:             Owner's timezone string (e.g. ``"Asia/Tehran"``).
        created_at:           When this session was created (UTC).
        updated_at:           When this session was last touched (UTC).
    """

    session_id: str
    owner_id: int
    chat_id: int
    state: ConversationState = ConversationState.IDLE
    current_flow: str = ""
    current_panel: str = ""
    current_category: str = ""
    pending_callback: str = ""
    pending_action: str = ""
    last_tool: str = ""
    last_reply_target: int | None = None
    current_tool: str = ""
    current_callback: str = ""
    language: str = "English"
    timezone: str = "UTC"
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def touch(self) -> None:
        """Mark this session as updated."""
        self.updated_at = datetime.now(timezone.utc)

    def transition_to(self, new_state: ConversationState) -> None:
        """Validate and apply a state transition.

        Raises ``InvalidTransition`` if the transition is not allowed.
        """
        validate_transition(self.state, new_state)
        old = self.state
        self.state = new_state
        self.touch()
        logger.debug(
            "Session %s: %s → %s", self.session_id, old.value, new_state.value
        )

    def reset(self) -> None:
        """Reset the session to IDLE, clearing transient fields."""
        self.state = ConversationState.IDLE
        self.current_flow = ""
        self.pending_callback = ""
        self.pending_action = ""
        self.current_tool = ""
        self.current_callback = ""
        self.last_reply_target = None
        self.touch()


class SessionManager:
    """Manages conversation sessions in RAM. No database, no persistence.

    Constructed once by the runtime supervisor and injected wherever
    needed. Sessions are keyed by ``session_id`` string.
    """

    __slots__ = ("_sessions",)

    def __init__(self) -> None:
        self._sessions: dict[str, ConversationSession] = {}

    def create_session(
        self,
        owner_id: int,
        chat_id: int,
        session_id: str | None = None,
        language: str = "English",
        timezone: str = "UTC",
    ) -> ConversationSession:
        """Create a new conversation session.

        If ``session_id`` is None, a UUID4 is generated.
        """
        if session_id is None:
            session_id = str(uuid.uuid4())
        session = ConversationSession(
            session_id=session_id,
            owner_id=owner_id,
            chat_id=chat_id,
            language=language,
            timezone=timezone,
        )
        self._sessions[session_id] = session
        logger.info("SessionManager: created session '%s' for chat %d", session_id, chat_id)
        return session

    def get_session(self, session_id: str) -> ConversationSession | None:
        """Return the session for ``session_id``, or None."""
        return self._sessions.get(session_id)

    def get_or_create(
        self,
        owner_id: int,
        chat_id: int,
        session_id: str | None = None,
        language: str = "English",
        timezone: str = "UTC",
    ) -> ConversationSession:
        """Return existing session or create a new one."""
        if session_id and session_id in self._sessions:
            return self._sessions[session_id]
        return self.create_session(owner_id, chat_id, session_id, language, timezone)

    def close_session(self, session_id: str) -> bool:
        """Remove a session. Returns True if it existed."""
        if session_id in self._sessions:
            del self._sessions[session_id]
            logger.info("SessionManager: closed session '%s'", session_id)
            return True
        return False

    def list_sessions(self) -> list[str]:
        """Return all active session IDs."""
        return list(self._sessions.keys())

    def active_count(self) -> int:
        """Number of active sessions."""
        return len(self._sessions)

    def clear_all(self) -> None:
        """Remove all sessions (used on shutdown)."""
        self._sessions.clear()
        logger.info("SessionManager: cleared all sessions")
