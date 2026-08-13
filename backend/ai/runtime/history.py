"""
Conversation History — bounded runtime message log for a single session.

Each item stores:
    role:             ``"system"``, ``"user"``, ``"assistant"``, or ``"tool"``
    content:          The message text
    timestamp:        UTC datetime when the item was recorded
    estimated_tokens: ``len(content) / 4`` (see ``tokens.estimate_tokens``)

The history is in-memory-only. No database. No persistence. No
summarization. It is created per session and discarded when the session
is closed or trimmed away.

Trimming policy (``trim_to_budget``):
  If the total estimated tokens exceed the configured budget, the oldest
  user/assistant pairs are removed first. The following are always
  preserved:
    - The most recent ``"system"`` entry (the active system prompt)
    - The single most recent ``"tool"`` entry (the latest tool result)
  Trimming is deterministic and idempotent: running it twice yields the
  same result as running it once.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List

from backend.ai.runtime.tokens import estimate_tokens

ROLE_SYSTEM = "system"
ROLE_USER = "user"
ROLE_ASSISTANT = "assistant"
ROLE_TOOL = "tool"
_VALID_ROLES = frozenset({ROLE_SYSTEM, ROLE_USER, ROLE_ASSISTANT, ROLE_TOOL})


@dataclass(frozen=True)
class HistoryItem:
    """A single item in the conversation history.

    Attributes:
        role:             One of ``"system"``, ``"user"``, ``"assistant"``, ``"tool"``.
        content:          The message text.
        timestamp:        UTC datetime when this item was recorded.
        estimated_tokens: ``len(content) / 4`` (computed at creation time).
    """

    role: str
    content: str
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    estimated_tokens: int = 0


class ConversationHistory:
    """In-memory conversation history for one runtime session.

    Constructed per session and owned by the ``ConversationManager``. No
    globals, no persistence.
    """

    __slots__ = ("_items",)

    def __init__(self) -> None:
        self._items: List[HistoryItem] = []

    def add(self, role: str, content: str) -> HistoryItem:
        """Append a message. Returns the created item.

        Raises ``ValueError`` for an unknown role.
        """
        if role not in _VALID_ROLES:
            raise ValueError(
                f"ConversationHistory: unknown role '{role}'. "
                f"Expected one of {sorted(_VALID_ROLES)}."
            )
        item = HistoryItem(
            role=role,
            content=content,
            estimated_tokens=estimate_tokens(content),
        )
        self._items.append(item)
        return item

    def all_items(self) -> List[HistoryItem]:
        """Return all items (oldest first). The returned list is a copy."""
        return list(self._items)

    def recent(self, n: int = 10) -> List[HistoryItem]:
        """Return the last ``n`` items (or fewer if not enough exist)."""
        if n <= 0:
            return []
        return list(self._items[-n:])

    def restore(self, items: List[HistoryItem]) -> int:
        """Bulk-load previously persisted items into history.

        Items must be in chronological order (oldest first). System-role
        items are skipped — the system prompt is managed separately by
        ``RuntimeSession.set_system_prompt``. Returns the number loaded.
        """
        loaded = 0
        for item in items:
            if item.role == ROLE_SYSTEM:
                continue
            self._items.append(item)
            loaded += 1
        return loaded

    def clear(self) -> None:
        """Remove every item."""
        self._items.clear()

    def size(self) -> int:
        """Number of items currently stored."""
        return len(self._items)

    def total_tokens(self) -> int:
        """Sum of ``estimated_tokens`` across all items."""
        return sum(item.estimated_tokens for item in self._items)

    def is_empty(self) -> bool:
        """True if no items exist."""
        return len(self._items) == 0

    def trim_to_budget(self, token_budget: int) -> int:
        """Trim history to fit within ``token_budget``.

        Removes the oldest user/assistant pairs first, always preserving
        the most recent system entry and the single most recent tool
        entry. Returns the number of items removed. Idempotent and
        deterministic: a second call with the same budget removes 0 more.

        A ``token_budget`` <= 0 is treated as "no trimming" (returns 0)
        so callers cannot accidentally wipe the whole history.
        """
        if token_budget <= 0:
            return 0
        removed = 0
        while self.total_tokens() > token_budget and len(self._items) > 1:
            victim = self._oldest_removable_index()
            if victim is None:
                break
            del self._items[victim]
            removed += 1
        return removed

    # ── internal ──

    def _oldest_removable_index(self) -> int | None:
        """Index of the oldest item that may be trimmed, or None.

        Protected (never trimmed) items:
          - The most recent ``"system"`` entry.
          - The most recent ``"tool"`` entry.
        If removing the oldest removable item would leave the history
        with fewer than one item, returns None (refuse to empty it).
        """
        last_system_idx = self._last_index_of_role(ROLE_SYSTEM)
        last_tool_idx = self._last_index_of_role(ROLE_TOOL)
        for i, item in enumerate(self._items):
            if i == last_system_idx or i == last_tool_idx:
                continue
            if len(self._items) - 1 < 1:
                break
            return i
        return None

    def _last_index_of_role(self, role: str) -> int | None:
        for i in range(len(self._items) - 1, -1, -1):
            if self._items[i].role == role:
                return i
        return None
