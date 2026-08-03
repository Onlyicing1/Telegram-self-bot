"""
History Manager — lightweight runtime conversation history.

Keeps a bounded list of (role, content) messages for the current
session. No database. No vector storage. No summaries. Pure runtime.

The history is capped at ``max_size`` entries (default 20). When the
cap is exceeded, the oldest entries are dropped (FIFO eviction).

Each entry is a ``HistoryEntry`` dataclass with:
    role:      ``"user"``, ``"assistant"``, or ``"tool"``
    content:   The message text
    tool_name:  For tool entries, the tool that produced this result
    timestamp: When the entry was recorded (UTC)
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Deque


@dataclass(frozen=True)
class HistoryEntry:
    """A single entry in the conversation history.

    Attributes:
        role:      ``"user"``, ``"assistant"``, or ``"tool"``.
        content:   The message text (truncated to 2000 chars on creation).
        tool_name:  For tool entries, the name of the tool that produced this.
        timestamp: UTC datetime when this entry was recorded.
    """

    role: str
    content: str
    tool_name: str = ""
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class HistoryManager:
    """Bounded runtime history for a single conversation session.

    Constructed per session (or per session_id) and injected. No
    globals, no persistence, no summarization.
    """

    __slots__ = ("_entries", "_max_size")

    def __init__(self, max_size: int = 20) -> None:
        if max_size < 1:
            raise ValueError("HistoryManager: max_size must be >= 1")
        self._max_size = max_size
        self._entries: Deque[HistoryEntry] = deque(maxlen=max_size)

    def add(self, role: str, content: str, tool_name: str = "") -> HistoryEntry:
        """Add a message to the history. Returns the created entry.

        Content is truncated to 2000 characters to bound memory usage.
        """
        truncated = content[:2000]
        entry = HistoryEntry(role=role, content=truncated, tool_name=tool_name)
        self._entries.append(entry)
        return entry

    def get_recent(self, n: int = 10) -> list[HistoryEntry]:
        """Return the last ``n`` entries (or fewer if not enough exist)."""
        if n <= 0:
            return []
        return list(self._entries)[-n:]

    def get_all(self) -> list[HistoryEntry]:
        """Return all entries as a list (oldest first)."""
        return list(self._entries)

    def clear(self) -> None:
        """Remove all entries."""
        self._entries.clear()

    def size(self) -> int:
        """Number of entries currently stored."""
        return len(self._entries)

    def is_empty(self) -> bool:
        """True if no entries exist."""
        return len(self._entries) == 0

    @property
    def max_size(self) -> int:
        """The maximum number of entries this manager can hold."""
        return self._max_size
