"""
Conversation Registry — one active session per owner.

The registry is the authoritative map from ``owner_id`` to the active
``RuntimeSession`` for that owner. It guarantees that an owner has at
most one live session at a time: creating a new session for an owner
that already has one returns (or replaces, depending on the call) the
existing session.

The registry is in-memory only. No persistence. No globals. It is
constructed once and injected into the ``ConversationManager``.
"""
from __future__ import annotations

import logging
from typing import Dict, List, Optional

from backend.ai.runtime.session import RuntimeSession

logger = logging.getLogger(__name__)


class ConversationRegistry:
    """Owner-keyed registry of active runtime sessions.

    Methods:
      get_session(owner_id)    — Return the active session for an owner, or None.
      create_session(owner_id) — Create a new session for an owner. If one
                                 already exists, it is returned unchanged
                                 (single-session-per-owner invariant).
      delete_session(owner_id) — Remove an owner's session. Returns True if
                                 one existed.
      list_sessions()          — Return all active sessions.
    """

    __slots__ = ("_by_owner",)

    def __init__(self) -> None:
        self._by_owner: Dict[int, RuntimeSession] = {}

    def get_session(self, owner_id: int) -> Optional[RuntimeSession]:
        return self._by_owner.get(owner_id)

    def create_session(self, owner_id: int, session_id: Optional[str] = None) -> RuntimeSession:
        existing = self._by_owner.get(owner_id)
        if existing is not None:
            logger.debug(
                "ConversationRegistry: reuse existing session '%s' for owner %d",
                existing.session_id,
                owner_id,
            )
            existing.touch()
            return existing
        session = RuntimeSession.create(owner_id=owner_id, session_id=session_id)
        self._by_owner[owner_id] = session
        logger.info(
            "ConversationRegistry: created session '%s' for owner %d",
            session.session_id,
            owner_id,
        )
        return session

    def delete_session(self, owner_id: int) -> bool:
        if owner_id in self._by_owner:
            sid = self._by_owner[owner_id].session_id
            del self._by_owner[owner_id]
            logger.info(
                "ConversationRegistry: deleted session '%s' for owner %d", sid, owner_id
            )
            return True
        return False

    def list_sessions(self) -> List[RuntimeSession]:
        return list(self._by_owner.values())

    def count(self) -> int:
        return len(self._by_owner)

    def clear(self) -> None:
        self._by_owner.clear()
        logger.info("ConversationRegistry: cleared all sessions")
