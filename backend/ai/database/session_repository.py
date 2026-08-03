"""
SessionRepository — persistence interface for AI conversation sessions.

Maps to the future ``ai_sessions`` table. Stores session metadata:
session ID, owner ID, provider, model, status, token counts, timestamps.

Interface-only. In-memory fallback provided.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class SessionRecord:
    """A single AI session record (maps to ``ai_sessions`` row)."""
    session_id: str
    owner_id: int
    provider: str = ""
    model: str = ""
    status: str = "active"
    total_tokens: int = 0
    message_count: int = 0
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "owner_id": self.owner_id,
            "provider": self.provider,
            "model": self.model,
            "status": self.status,
            "total_tokens": self.total_tokens,
            "message_count": self.message_count,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "metadata": dict(self.metadata),
        }


class SessionRepository:
    """Abstract interface for session persistence."""

    def create(self, record: SessionRecord) -> bool:
        raise NotImplementedError

    def get(self, session_id: str) -> SessionRecord | None:
        raise NotImplementedError

    def update(self, session_id: str, updates: dict[str, Any]) -> bool:
        raise NotImplementedError

    def list_sessions(self, owner_id: int, limit: int = 50) -> list[SessionRecord]:
        raise NotImplementedError

    def delete(self, session_id: str) -> bool:
        raise NotImplementedError


class InMemorySessionRepository(SessionRepository):
    """In-memory fallback for session persistence."""

    __slots__ = ("_sessions",)

    def __init__(self) -> None:
        self._sessions: dict[str, SessionRecord] = {}

    def create(self, record: SessionRecord) -> bool:
        self._sessions[record.session_id] = record
        return True

    def get(self, session_id: str) -> SessionRecord | None:
        return self._sessions.get(session_id)

    def update(self, session_id: str, updates: dict[str, Any]) -> bool:
        rec = self._sessions.get(session_id)
        if rec is None:
            return False
        for key, value in updates.items():
            if hasattr(rec, key):
                setattr(rec, key, value)
        rec.updated_at = datetime.now(timezone.utc)
        return True

    def list_sessions(self, owner_id: int, limit: int = 50) -> list[SessionRecord]:
        results = [s for s in self._sessions.values() if s.owner_id == owner_id]
        results.sort(key=lambda s: s.updated_at, reverse=True)
        return results[:limit]

    def delete(self, session_id: str) -> bool:
        return self._sessions.pop(session_id, None) is not None
