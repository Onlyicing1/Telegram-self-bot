"""
SessionManager — manages the lifecycle of AI sessions in RAM.

This is NOT the same as ``ConversationSession`` / ``SessionManager`` in
``backend.ai.conversation.session``. That manager tracks conversation
state (panels, flows, callbacks). This manager tracks *AI session*
lifecycle — whether a session exists, is active, has completed, or has
been destroyed.

Currently supports only one active AI session at a time. Future
versions may support multiple concurrent sessions.

No persistence. No database. No globals. Runtime only.
"""
from __future__ import annotations

import logging
import uuid
from typing import Dict

from backend.ai.session.state import AISessionState

logger = logging.getLogger(__name__)


class SessionManager:
    """Manages AI sessions in RAM. No database, no persistence.

    Constructed once and injected wherever needed. Currently supports
    a single active session — ``create_session`` will close any existing
    session before creating a new one.

    Methods:
      create_session()  — Create a new AI session, returns session_id.
      destroy_session() — Remove a session by ID.
      get_session()     — Retrieve session metadata by ID.
      reset_session()   — Reset a session to CREATED state.
      active_session()  — Return the current active session ID, or None.
    """

    __slots__ = ("_sessions", "_active_id")

    def __init__(self) -> None:
        self._sessions: Dict[str, dict] = {}
        self._active_id: str = ""

    def create_session(
        self,
        owner_id: int,
        chat_id: int,
        session_id: str | None = None,
        language: str = "English",
        timezone: str = "UTC",
    ) -> str:
        """Create a new AI session. Returns the session_id.

        If a session already exists, it is closed first (single-session
        constraint). If ``session_id`` is None, a UUID4 is generated.
        """
        if session_id is None:
            session_id = str(uuid.uuid4())

        if self._active_id and self._active_id in self._sessions:
            self._sessions[self._active_id]["state"] = AISessionState.CLOSED
            logger.info(
                "SessionManager: auto-closed previous session '%s'",
                self._active_id,
            )

        self._sessions[session_id] = {
            "session_id": session_id,
            "owner_id": owner_id,
            "chat_id": chat_id,
            "state": AISessionState.CREATED,
            "language": language,
            "timezone": timezone,
            "request_count": 0,
        }
        self._active_id = session_id
        logger.info(
            "SessionManager: created session '%s' for owner %d",
            session_id,
            owner_id,
        )
        return session_id

    def destroy_session(self, session_id: str) -> bool:
        """Remove a session by ID. Returns True if it existed."""
        if session_id not in self._sessions:
            return False
        self._sessions[session_id]["state"] = AISessionState.CLOSED
        del self._sessions[session_id]
        if self._active_id == session_id:
            self._active_id = ""
        logger.info("SessionManager: destroyed session '%s'", session_id)
        return True

    def get_session(self, session_id: str) -> dict | None:
        """Return session metadata dict, or None if not found."""
        return self._sessions.get(session_id)

    def reset_session(self, session_id: str) -> bool:
        """Reset a session to CREATED state. Returns True if it existed."""
        session = self._sessions.get(session_id)
        if session is None:
            return False
        session["state"] = AISessionState.CREATED
        session["request_count"] = 0
        logger.info("SessionManager: reset session '%s'", session_id)
        return True

    def active_session(self) -> str | None:
        """Return the current active session ID, or None."""
        return self._active_id or None

    def set_state(self, session_id: str, state: AISessionState) -> bool:
        """Set the state of a session. Returns True if it existed."""
        session = self._sessions.get(session_id)
        if session is None:
            return False
        session["state"] = state
        return True

    def increment_request_count(self, session_id: str) -> int:
        """Increment and return the request count for a session."""
        session = self._sessions.get(session_id)
        if session is None:
            return 0
        session["request_count"] += 1
        return session["request_count"]

    def list_sessions(self) -> list[str]:
        """Return all session IDs (including closed ones still in RAM)."""
        return list(self._sessions.keys())

    def clear_all(self) -> None:
        """Remove all sessions (used on shutdown)."""
        self._sessions.clear()
        self._active_id = ""
        logger.info("SessionManager: cleared all sessions")
