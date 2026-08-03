"""
MessageRepository — persistence interface for AI messages.

Maps to the future ``ai_messages`` table. Stores individual messages
within a session: role, content, token count, tool calls, timestamps.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class MessageRecord:
    """A single AI message record (maps to ``ai_messages`` row)."""
    id: str
    session_id: str
    owner_id: int
    role: str = "user"
    content: str = ""
    token_count: int = 0
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    provider: str = ""
    model: str = ""
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "session_id": self.session_id,
            "owner_id": self.owner_id,
            "role": self.role,
            "content": self.content,
            "token_count": self.token_count,
            "tool_calls": list(self.tool_calls),
            "provider": self.provider,
            "model": self.model,
            "created_at": self.created_at.isoformat(),
            "metadata": dict(self.metadata),
        }


class MessageRepository:
    """Abstract interface for message persistence."""

    def create(self, record: MessageRecord) -> bool:
        raise NotImplementedError

    def list_messages(self, session_id: str, limit: int = 100) -> list[MessageRecord]:
        raise NotImplementedError

    def delete_session_messages(self, session_id: str) -> int:
        raise NotImplementedError

    def count(self, session_id: str) -> int:
        raise NotImplementedError


class InMemoryMessageRepository(MessageRepository):
    """In-memory fallback for message persistence."""

    __slots__ = ("_messages",)

    def __init__(self) -> None:
        self._messages: list[MessageRecord] = []

    def create(self, record: MessageRecord) -> bool:
        self._messages.append(record)
        return True

    def list_messages(self, session_id: str, limit: int = 100) -> list[MessageRecord]:
        results = [m for m in self._messages if m.session_id == session_id]
        return results[-limit:]

    def delete_session_messages(self, session_id: str) -> int:
        before = len(self._messages)
        self._messages = [m for m in self._messages if m.session_id != session_id]
        return before - len(self._messages)

    def count(self, session_id: str) -> int:
        return sum(1 for m in self._messages if m.session_id == session_id)
