"""
ReplyResolver — deterministic Telegram-message-ID → AI-message mapping.

When the AI generates a response, the handler edits the triggering Telegram
message in-place with the AI output.  The Telegram message ID of that edited
message therefore *is* the container for the AI response.  This module
records that mapping so that when the owner later replies to that message,
the full AI content can be retrieved and injected as high-priority context.

The resolver is a process-wide in-memory singleton.  It does NOT depend on
Supabase and does NOT require any schema change.  Mappings persist as long
as the bot process is running.  A bounded LRU-style cap prevents unbounded
memory growth: the oldest entries are evicted when ``_MAX_ENTRIES`` is
reached.
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

_MAX_ENTRIES = 500


@dataclass(frozen=True)
class ResolvedAIContent:
    """The full AI message associated with a Telegram message ID.

    Attributes:
        session_id:   AI session that produced this message.
        role:         Message role — ``"assistant"`` for AI responses.
        content:      Full, untruncated AI response text.
        provider:     Provider name (e.g. ``"gemini"``).
        model:        Model name.
        timestamp:    UTC ISO string when the mapping was registered.
    """

    session_id: str
    role: str
    content: str
    provider: str
    model: str
    timestamp: str = ""


class ReplyResolver:
    """Thread-safe in-memory registry mapping Telegram msg IDs to AI content.

    Public API:
        register(telegram_msg_id, session_id, role, content, provider, model)
        resolve(telegram_msg_id) → ResolvedAIContent | None
    """

    __slots__ = ("_map", "_lock")

    def __init__(self, max_entries: int = _MAX_ENTRIES) -> None:
        self._map: dict[int, ResolvedAIContent] = {}
        self._lock = threading.Lock()

    def register(
        self,
        telegram_msg_id: int,
        session_id: str,
        role: str,
        content: str,
        provider: str = "",
        model: str = "",
    ) -> None:
        """Record that ``telegram_msg_id`` now contains ``content``.

        Called after the AI response is edited into the triggering message.
        If ``telegram_msg_id`` is zero or negative, the call is a no-op
        (the handler could not determine the message ID).
        """
        if telegram_msg_id <= 0:
            return
        entry = ResolvedAIContent(
            session_id=session_id,
            role=role,
            content=content,
            provider=provider,
            model=model,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )
        with self._lock:
            if len(self._map) >= _MAX_ENTRIES and telegram_msg_id not in self._map:
                oldest_key = next(iter(self._map))
                del self._map[oldest_key]
            self._map[telegram_msg_id] = entry

    def resolve(self, telegram_msg_id: int) -> Optional[ResolvedAIContent]:
        """Look up the AI content for a Telegram message ID.

        Returns ``None`` if no AI message is registered for that ID (the
        replied message was not AI-generated, or the mapping was evicted).
        """
        if telegram_msg_id <= 0:
            return None
        with self._lock:
            return self._map.get(telegram_msg_id)

    def clear(self) -> None:
        """Remove all mappings (useful for tests)."""
        with self._lock:
            self._map.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._map)


_resolver: Optional[ReplyResolver] = None


def get_resolver() -> ReplyResolver:
    """Return the process-wide ReplyResolver singleton."""
    global _resolver
    if _resolver is None:
        _resolver = ReplyResolver()
    return _resolver
