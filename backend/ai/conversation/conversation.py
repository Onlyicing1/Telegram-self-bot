"""
Conversation Manager — the single source of runtime context.

The Conversation Manager owns the session lifecycle, history, and
context assembly. It is the ONE object the rest of the codebase
interacts with for AI conversations. It does NOT call models, execute
tools, generate prompts, or perform any I/O beyond what is injected.

Responsibilities (from AI_MASTER_DESIGN.md §4.2, §24, §25):
  - Create / retrieve / close conversation sessions
  - Track conversation state (deterministic state machine)
  - Maintain bounded runtime history (no DB, no persistence)
  - Build immutable ``ConversationContext`` objects via ContextBuilder
  - Know: current menu, panel, category, reply context, pending action,
    active user flow, language, timezone, runtime state, current tool,
    current callback

What it does NOT do:
  - Call LLM providers
  - Execute tools
  - Generate prompts
  - Persist to database
  - Run background loops or schedulers
  - Modify any existing feature (save, delete, bio, username, etc.)

Dependency injection:
  The manager receives ``SessionManager``, ``HistoryManager`` (factory),
  and ``ContextBuilder`` as constructor arguments. No globals.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from backend.ai.conversation.context_builder import (
    ContextBuilder,
    ConversationContext,
    ReplyContext,
    RuntimeContext,
    SettingsContext,
    ToolContext,
)
from backend.ai.conversation.history import HistoryEntry, HistoryManager
from backend.ai.conversation.session import ConversationSession, SessionManager
from backend.ai.conversation.state import ConversationState, InvalidTransition

logger = logging.getLogger(__name__)


class ConversationManager:
    """The single source of runtime context for the AI layer.

    Constructed once by the runtime supervisor and injected wherever
    needed. Callers never import SessionManager, HistoryManager, or
    ContextBuilder directly — they go through this object.

    Usage::

        mgr = ConversationManager()
        session = mgr.start_session(owner_id=123, chat_id=456)
        mgr.set_panel(session.session_id, panel="ai:new", category="ai")
        mgr.add_user_message(session.session_id, "Save this message")
        ctx = mgr.build_context(
            session_id=session.session_id,
            user_text="Save this message",
            message_id=789,
        )
        # Pass ctx to Prompt Builder (future)
    """

    __slots__ = ("_sessions", "_histories", "_context_builder", "_history_max")

    def __init__(
        self,
        context_builder: ContextBuilder | None = None,
        history_max: int = 20,
    ) -> None:
        self._sessions = SessionManager()
        self._histories: dict[str, HistoryManager] = {}
        self._context_builder = context_builder or ContextBuilder()
        self._history_max = history_max

    # ── Session lifecycle ──

    def start_session(
        self,
        owner_id: int,
        chat_id: int,
        session_id: str | None = None,
        language: str = "English",
        timezone: str = "UTC",
    ) -> ConversationSession:
        """Create a new conversation session and its history."""
        session = self._sessions.create_session(
            owner_id=owner_id,
            chat_id=chat_id,
            session_id=session_id,
            language=language,
            timezone=timezone,
        )
        self._histories[session.session_id] = HistoryManager(max_size=self._history_max)
        logger.info("ConversationManager: started session '%s'", session.session_id)
        return session

    def get_session(self, session_id: str) -> ConversationSession | None:
        """Return the session for ``session_id``, or None."""
        return self._sessions.get_session(session_id)

    def end_session(self, session_id: str) -> bool:
        """Close a session and discard its history. Returns True if existed."""
        self._histories.pop(session_id, None)
        return self._sessions.close_session(session_id)

    def list_sessions(self) -> list[str]:
        """Return all active session IDs."""
        return self._sessions.list_sessions()

    # ── State machine ──

    def transition(
        self, session_id: str, new_state: ConversationState
    ) -> ConversationState:
        """Validate and apply a state transition. Returns the new state.

        Raises ``InvalidTransition`` if the transition is not allowed.
        Raises ``KeyError`` if the session does not exist.
        """
        session = self._require_session(session_id)
        session.transition_to(new_state)
        return session.state

    def get_state(self, session_id: str) -> ConversationState:
        """Return the current state of a session."""
        session = self._require_session(session_id)
        return session.state

    def cancel(self, session_id: str) -> None:
        """Cancel a session, transitioning to CANCELLED then IDLE."""
        session = self._require_session(session_id)
        if session.state != ConversationState.CANCELLED:
            try:
                session.transition_to(ConversationState.CANCELLED)
            except InvalidTransition:
                pass
        session.reset()

    # ── Session field setters ──

    def set_panel(
        self,
        session_id: str,
        panel: str = "",
        category: str = "",
        menu: str = "",
    ) -> None:
        """Set the current panel, category, and/or menu for a session."""
        session = self._require_session(session_id)
        if panel:
            session.current_panel = panel
        if category:
            session.current_category = category
        if menu:
            session.current_menu = menu  # type: ignore[attr-defined]
        session.touch()

    def set_flow(self, session_id: str, flow: str) -> None:
        """Set the active user flow (e.g. ``"save"``, ``"bio"``)."""
        session = self._require_session(session_id)
        session.current_flow = flow
        session.touch()

    def set_pending_action(self, session_id: str, action: str) -> None:
        """Set or clear the pending action (e.g. ``"confirm_delete:5"``)."""
        session = self._require_session(session_id)
        session.pending_action = action
        session.touch()

    def set_pending_callback(self, session_id: str, callback: str) -> None:
        """Set or clear the pending callback data."""
        session = self._require_session(session_id)
        session.pending_callback = callback
        session.touch()

    def set_current_tool(self, session_id: str, tool_name: str) -> None:
        """Set the currently executing tool. Empty string to clear."""
        session = self._require_session(session_id)
        session.current_tool = tool_name
        if tool_name:
            session.last_tool = tool_name
        session.touch()

    def set_current_callback(self, session_id: str, callback: str) -> None:
        """Set the current callback being processed. Empty string to clear."""
        session = self._require_session(session_id)
        session.current_callback = callback
        session.touch()

    def set_reply_target(self, session_id: str, message_id: int | None) -> None:
        """Set the last reply target message ID. None to clear."""
        session = self._require_session(session_id)
        session.last_reply_target = message_id
        session.touch()

    def set_language(self, session_id: str, language: str) -> None:
        """Set the owner's language for this session."""
        session = self._require_session(session_id)
        session.language = language
        session.touch()

    def set_timezone(self, session_id: str, timezone: str) -> None:
        """Set the owner's timezone for this session."""
        session = self._require_session(session_id)
        session.timezone = timezone
        session.touch()

    # ── History ──

    def add_user_message(self, session_id: str, text: str) -> HistoryEntry:
        """Add a user message to the session history."""
        history = self._require_history(session_id)
        return history.add(role="user", content=text)

    def add_assistant_message(self, session_id: str, text: str) -> HistoryEntry:
        """Add an assistant message to the session history."""
        history = self._require_history(session_id)
        return history.add(role="assistant", content=text)

    def add_tool_result(self, session_id: str, tool_name: str, result: str) -> HistoryEntry:
        """Add a tool result to the session history."""
        history = self._require_history(session_id)
        return history.add(role="tool", content=result, tool_name=tool_name)

    def get_history(self, session_id: str, n: int = 10) -> list[HistoryEntry]:
        """Return the last ``n`` history entries."""
        history = self._require_history(session_id)
        return history.get_recent(n)

    def clear_history(self, session_id: str) -> None:
        """Clear all history for a session."""
        history = self._require_history(session_id)
        history.clear()

    # ── Context assembly ──

    def build_context(
        self,
        session_id: str,
        user_text: str,
        message_id: int,
        current_menu: str = "main",
        reply: ReplyContext | None = None,
        settings: SettingsContext | None = None,
        runtime: RuntimeContext | None = None,
    ) -> ConversationContext:
        """Assemble the immutable ``ConversationContext`` for the Prompt Builder.

        This is the ONE method that produces the object the Prompt
        Builder (future) will consume. It merges session state, history,
        reply context, settings, and runtime info into a single frozen
        dataclass.

        Args:
            session_id:    The active session ID.
            user_text:     The raw text the owner typed.
            message_id:    Telegram message ID of the triggering message.
            current_menu:  Current top-level menu name.
            reply:         Reply context (or None).
            settings:      Settings snapshot (or None).
            runtime:       Runtime context (or None).

        Returns:
            A frozen ``ConversationContext``.
        """
        session = self._require_session(session_id)
        history = self._require_history(session_id)
        history_entries = history.get_recent(self._history_max)

        tool_ctx = ToolContext(
            current_tool=session.current_tool,
            last_tool=session.last_tool,
        )

        return self._context_builder.build(
            session=session,
            user_text=user_text,
            message_id=message_id,
            current_menu=current_menu,
            reply=reply,
            tool=tool_ctx,
            settings=settings,
            runtime=runtime,
            history=history_entries,
        )

    # ── Internal helpers ──

    def _require_session(self, session_id: str) -> ConversationSession:
        session = self._sessions.get_session(session_id)
        if session is None:
            raise KeyError(f"No session found for session_id='{session_id}'")
        return session

    def _require_history(self, session_id: str) -> HistoryManager:
        if session_id not in self._histories:
            self._histories[session_id] = HistoryManager(max_size=self._history_max)
        return self._histories[session_id]
