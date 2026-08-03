"""
Runtime Session — the provider-agnostic conversation session object.

A ``RuntimeSession`` is the single object every future AI provider reads
to continue a conversation. It carries the active provider/model, the
system prompt, the bounded conversation history, the tool history, the
pending tool call, and a token estimate. It is purely in-memory: nothing
here is ever persisted to a database.

This is intentionally separate from ``ConversationSession`` in
``backend.ai.conversation.session`` (which tracks UI/panel/flow state)
and from the AI-session bookkeeping in ``backend.ai.session.session``.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from backend.ai.runtime.history import ConversationHistory, HistoryItem


@dataclass
class RuntimeSession:
    """A single conversation session as seen by an AI provider.

    Attributes:
        session_id:          Unique identifier (UUID4 string).
        owner_id:            Telegram user ID of the bot owner.
        started_at:          UTC datetime when the session was created.
        last_activity:       UTC datetime of the most recent activity.
        active_provider:     Name of the active provider (e.g. ``"dummy"``).
        active_model:        Name of the active model (e.g. ``"dummy-1"``).
        system_prompt:       The current system prompt text (may be empty).
        conversation_history: ``ConversationHistory`` of messages.
        tool_history:        List of tool-call records (name, args, result).
        pending_tool:        The tool call awaiting a result, or None.
        token_estimate:      Cached total estimated tokens for the history.
    """

    session_id: str
    owner_id: int
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    last_activity: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    active_provider: str = "dummy"
    active_model: str = "dummy-1"
    system_prompt: str = ""
    conversation_history: ConversationHistory = field(default_factory=ConversationHistory)
    tool_history: List[Dict[str, Any]] = field(default_factory=list)
    pending_tool: Optional[Dict[str, Any]] = None
    token_estimate: int = 0

    @classmethod
    def create(cls, owner_id: int, session_id: Optional[str] = None) -> "RuntimeSession":
        """Build a fresh session. A UUID4 is generated when id is omitted."""
        sid = session_id or str(uuid.uuid4())
        return cls(session_id=sid, owner_id=owner_id)

    def touch(self) -> None:
        """Mark this session as active now (UTC)."""
        self.last_activity = datetime.now(timezone.utc)

    def set_provider(self, provider: str, model: str) -> None:
        """Set the active provider and model."""
        self.active_provider = provider
        self.active_model = model
        self.touch()

    def set_system_prompt(self, prompt: str) -> None:
        """Replace the system prompt. The previous system message, if any,
        is removed from history before the new one is added so there is
        at most one system entry at a time."""
        self.system_prompt = prompt
        self._replace_system_in_history(prompt)
        self._refresh_token_estimate()
        self.touch()

    def add_message(self, role: str, content: str) -> HistoryItem:
        """Append a message to the conversation history and refresh the
        token estimate. Returns the created item."""
        item = self.conversation_history.add(role=role, content=content)
        self._refresh_token_estimate()
        self.touch()
        return item

    def add_tool_call(self, name: str, args: Dict[str, Any], result: Any = None) -> None:
        """Record a completed tool call in the tool history."""
        record: Dict[str, Any] = {
            "name": name,
            "args": dict(args) if args else {},
            "result": result,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        self.tool_history.append(record)
        self.touch()

    def set_pending_tool(self, name: str, args: Dict[str, Any]) -> None:
        """Set the tool call awaiting a result."""
        self.pending_tool = {
            "name": name,
            "args": dict(args) if args else {},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        self.touch()

    def clear_pending_tool(self) -> None:
        """Clear the pending tool call (e.g. after the result arrived)."""
        self.pending_tool = None
        self.touch()

    def reset(self) -> None:
        """Reset the conversation: clear history, tool history, pending
        tool, and token estimate. The session_id, owner_id, provider,
        model, and system prompt are preserved so the session can be
        reused for a fresh conversation."""
        self.conversation_history.clear()
        self.tool_history.clear()
        self.pending_tool = None
        self.token_estimate = 0
        if self.system_prompt:
            self.conversation_history.add(role="system", content=self.system_prompt)
            self._refresh_token_estimate()
        self.touch()

    def _replace_system_in_history(self, prompt: str) -> None:
        items = self.conversation_history.all_items()
        self.conversation_history.clear()
        if prompt:
            self.conversation_history.add(role="system", content=prompt)
        for item in items:
            if item.role == "system":
                continue
            self.conversation_history.add(role=item.role, content=item.content)

    def _refresh_token_estimate(self) -> None:
        self.token_estimate = self.conversation_history.total_tokens()
