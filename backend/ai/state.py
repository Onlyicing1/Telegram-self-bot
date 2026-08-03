"""
AI state objects — runtime state and per-session state for the AI layer.

Two levels of state:

  AIRuntimeState  — process-wide singleton managed by the runtime
                    supervisor. Tracks whether AI is enabled, which
                    provider is active, and aggregate counters. This
                    is NOT a global: it is constructed once and injected
                    wherever needed.

  AISessionState  — per-conversation state. One instance per chat.
                    Tracks turn count, last response, and tool-call
                    history for that conversation. Stored in a dict
                    keyed by chat_id inside the runtime state.

Both objects are plain data. No I/O, no side effects.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass
class AISessionState:
    """Per-conversation state for a single chat.

    Attributes:
        chat_id:            Telegram chat ID this session belongs to.
        turn_count:         Number of AI turns completed in this chat.
        last_response_text: The text of the most recent AI response.
        last_tool_calls:    List of tool-call identifiers from the last
                            turn. Each entry is a provider-defined
                            opaque string.
        created_at:         When this session was first created.
        updated_at:         When this session was last touched.
    """

    chat_id: int
    turn_count: int = 0
    last_response_text: str = ""
    last_tool_calls: list[str] = field(default_factory=list)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def touch(self) -> None:
        """Mark this session as updated (call after any mutation)."""
        self.updated_at = datetime.now(timezone.utc)


@dataclass
class AIRuntimeState:
    """Process-wide runtime state for the AI layer.

    Constructed once and injected into the interface and providers.
    Never accessed as a global — always passed as a constructor
    argument.

    Attributes:
        enabled:            Whether AI is enabled at all. When False,
                            ``AIInterface.handle`` returns ``AI_DISABLED``
                            without consulting any provider.
        active_provider:    Name of the currently active provider, or
                            ``""`` if none is registered.
        total_requests:     Lifetime count of AI requests received.
        total_responses:    Lifetime count of AI responses returned.
        sessions:           Per-chat session state, keyed by chat_id.
    """

    enabled: bool = False
    active_provider: str = ""
    total_requests: int = 0
    total_responses: int = 0
    sessions: dict[int, AISessionState] = field(default_factory=dict)

    def get_or_create_session(self, chat_id: int) -> AISessionState:
        """Return the session for ``chat_id``, creating one if absent."""
        if chat_id not in self.sessions:
            self.sessions[chat_id] = AISessionState(chat_id=chat_id)
        return self.sessions[chat_id]
