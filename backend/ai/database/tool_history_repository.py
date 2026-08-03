"""
ToolHistoryRepository — persistence interface for AI tool call history.

Maps to the future ``ai_tool_history`` table. Stores a log of every
tool the AI has called: tool name, arguments, result, success, timestamp.
Used for auditing, debugging, and analytics.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class ToolHistoryRecord:
    """A single tool call record (maps to ``ai_tool_history`` row)."""
    id: str
    owner_id: int
    session_id: str = ""
    tool_name: str = ""
    arguments: dict[str, Any] = field(default_factory=dict)
    result_success: bool = False
    result_message: str = ""
    result_data: dict[str, Any] = field(default_factory=dict)
    latency_ms: float = 0.0
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "owner_id": self.owner_id,
            "session_id": self.session_id,
            "tool_name": self.tool_name,
            "arguments": dict(self.arguments),
            "result_success": self.result_success,
            "result_message": self.result_message,
            "result_data": dict(self.result_data),
            "latency_ms": self.latency_ms,
            "created_at": self.created_at.isoformat(),
        }


class ToolHistoryRepository:
    """Abstract interface for tool history persistence."""

    def create(self, record: ToolHistoryRecord) -> bool:
        raise NotImplementedError

    def recent(self, owner_id: int, limit: int = 50) -> list[ToolHistoryRecord]:
        raise NotImplementedError

    def by_tool(self, owner_id: int, tool_name: str, limit: int = 20) -> list[ToolHistoryRecord]:
        raise NotImplementedError

    def count(self, owner_id: int) -> int:
        raise NotImplementedError


class InMemoryToolHistoryRepository(ToolHistoryRepository):
    """In-memory fallback for tool history."""

    __slots__ = ("_records",)

    def __init__(self) -> None:
        self._records: list[ToolHistoryRecord] = []

    def create(self, record: ToolHistoryRecord) -> bool:
        self._records.append(record)
        return True

    def recent(self, owner_id: int, limit: int = 50) -> list[ToolHistoryRecord]:
        results = [r for r in self._records if r.owner_id == owner_id]
        results.sort(key=lambda r: r.created_at, reverse=True)
        return results[:limit]

    def by_tool(self, owner_id: int, tool_name: str, limit: int = 20) -> list[ToolHistoryRecord]:
        results = [r for r in self._records if r.owner_id == owner_id and r.tool_name == tool_name]
        results.sort(key=lambda r: r.created_at, reverse=True)
        return results[:limit]

    def count(self, owner_id: int) -> int:
        return sum(1 for r in self._records if r.owner_id == owner_id)
