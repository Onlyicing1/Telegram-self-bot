"""
Conversation Manager — the single source of truth for the Conversation
Runtime layer.

Owns the full session lifecycle: create, close, reset, automatic idle
cleanup, history, tool history, pending tool, and token budgeting. It
is provider-agnostic and fully offline — no AI provider is ever called.

Determinism guarantees:
  - Exactly one ``ConversationManager`` instance is intended per process
    (constructed by the caller and injected; no module-level singleton).
  - Exactly one ``ConversationRegistry`` instance is owned by the manager.
  - No global mutable state. No duplicated registries. No background
    threads: idle cleanup runs lazily on every public method, so the
    manager is deterministic and side-effect-free between calls.

Configuration (constructor):
    idle_timeout_seconds:  Idle sessions older than this are removed.
                            Default 1800 (30 minutes). <= 0 disables cleanup.
    token_budget:           Soft cap on total estimated tokens. When
                            exceeded, history is trimmed. Default 8000.
                            <= 0 disables trimming.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from backend.ai.runtime.history import (
    ROLE_ASSISTANT,
    ROLE_SYSTEM,
    ROLE_TOOL,
    ROLE_USER,
    ConversationHistory,
    HistoryItem,
)
from backend.ai.runtime.registry import ConversationRegistry
from backend.ai.runtime.session import RuntimeSession
from backend.ai.runtime.tokens import estimate_tokens

logger = logging.getLogger(__name__)

DEFAULT_IDLE_TIMEOUT_SECONDS = 1800
DEFAULT_TOKEN_BUDGET = 8000


class ConversationManager:
    """The single source of truth for conversation runtime sessions.

    Constructed once and injected wherever a future AI provider needs a
    session. Callers never touch the registry or history directly.
    """

    __slots__ = ("_registry", "_idle_timeout", "_token_budget")

    def __init__(
        self,
        idle_timeout_seconds: int = DEFAULT_IDLE_TIMEOUT_SECONDS,
        token_budget: int = DEFAULT_TOKEN_BUDGET,
    ) -> None:
        self._registry = ConversationRegistry()
        self._idle_timeout = idle_timeout_seconds
        self._token_budget = token_budget

    # ── Session lifecycle ──

    def create_session(
        self, owner_id: int, session_id: Optional[str] = None
    ) -> RuntimeSession:
        """Create a new conversation session for ``owner_id``.

        Per the single-session-per-owner invariant, if the owner already
        has an active session it is returned (and touched) instead of
        creating a duplicate.
        """
        self._cleanup_idle()
        return self._registry.create_session(owner_id=owner_id, session_id=session_id)

    def close_session(self, owner_id: int) -> bool:
        """Close and discard the owner's session. Returns True if one existed."""
        removed = self._registry.delete_session(owner_id)
        if removed:
            logger.info("ConversationManager: closed session for owner %d", owner_id)
        return removed

    def reset_session(self, owner_id: int) -> Optional[RuntimeSession]:
        """Reset the owner's session history, tool history, and pending tool.

        Returns the reset session, or None if the owner has no session.
        """
        self._cleanup_idle()
        session = self._registry.get_session(owner_id)
        if session is None:
            return None
        session.reset()
        logger.info("ConversationManager: reset session '%s'", session.session_id)
        return session

    def get_session(self, owner_id: int) -> Optional[RuntimeSession]:
        """Return the owner's active session, or None."""
        self._cleanup_idle()
        return self._registry.get_session(owner_id)

    def list_sessions(self) -> List[RuntimeSession]:
        """Return all active sessions."""
        self._cleanup_idle()
        return self._registry.list_sessions()

    # ── Conversation content ──

    def set_system_prompt(self, owner_id: int, prompt: str) -> Optional[RuntimeSession]:
        """Set the system prompt for the owner's session. Creates a session
        if one does not yet exist."""
        session = self._registry.get_session(owner_id)
        if session is None:
            session = self._registry.create_session(owner_id=owner_id)
        session.set_system_prompt(prompt)
        self._trim_if_needed(session)
        return session

    def add_user_message(self, owner_id: int, content: str) -> HistoryItem:
        return self._add_message(owner_id, ROLE_USER, content)

    def add_assistant_message(self, owner_id: int, content: str) -> HistoryItem:
        return self._add_message(owner_id, ROLE_ASSISTANT, content)

    def add_tool_result(self, owner_id: int, tool_name: str, result: str) -> HistoryItem:
        item = self._add_message(owner_id, ROLE_TOOL, result)
        session = self._require_session(owner_id)
        session.add_tool_call(name=tool_name, args={}, result=result)
        session.clear_pending_tool()
        return item

    def get_history(self, owner_id: int, n: int = 10) -> List[HistoryItem]:
        session = self._registry.get_session(owner_id)
        if session is None:
            return []
        return session.conversation_history.recent(n)

    def set_pending_tool(self, owner_id: int, name: str, args: Dict[str, Any]) -> None:
        session = self._require_session(owner_id)
        session.set_pending_tool(name=name, args=args)

    def clear_pending_tool(self, owner_id: int) -> None:
        session = self._registry.get_session(owner_id)
        if session is not None:
            session.clear_pending_tool()

    # ── Provider/model selection (no provider is called) ──

    def set_provider(self, owner_id: int, provider: str, model: str) -> None:
        session = self._require_session(owner_id)
        session.set_provider(provider=provider, model=model)

    # ── Automatic cleanup ──

    def cleanup_idle(self) -> int:
        """Public entry point for idle cleanup. Returns the number of
        sessions removed. Safe to call from a scheduler or manually."""
        return self._cleanup_idle()

    def _cleanup_idle(self) -> int:
        if self._idle_timeout <= 0:
            return 0
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=self._idle_timeout)
        removed = 0
        for session in list(self._registry.list_sessions()):
            if session.last_activity < cutoff:
                self._registry.delete_session(session.owner_id)
                removed += 1
                logger.info(
                    "ConversationManager: reaped idle session '%s' (owner %d)",
                    session.session_id,
                    session.owner_id,
                )
        return removed

    # ── History trimming ──

    def trim_history(self, owner_id: int) -> int:
        """Force a trim of the owner's history to the configured budget.
        Returns the number of items removed."""
        session = self._registry.get_session(owner_id)
        if session is None:
            return 0
        return self._trim_if_needed(session)

    def _trim_if_needed(self, session: RuntimeSession) -> int:
        if self._token_budget <= 0:
            return 0
        removed = session.conversation_history.trim_to_budget(self._token_budget)
        if removed:
            session._refresh_token_estimate()  # noqa: SLF001 — internal coordination
            logger.debug(
                "ConversationManager: trimmed %d items from session '%s'",
                removed,
                session.session_id,
            )
        return removed

    # ── Internal helpers ──

    def _add_message(self, owner_id: int, role: str, content: str) -> HistoryItem:
        session = self._registry.get_session(owner_id)
        if session is None:
            session = self._registry.create_session(owner_id=owner_id)
        item = session.add_message(role=role, content=content)
        self._trim_if_needed(session)
        return item

    def _require_session(self, owner_id: int) -> RuntimeSession:
        session = self._registry.get_session(owner_id)
        if session is None:
            raise KeyError(
                f"ConversationManager: no active session for owner_id={owner_id}"
            )
        return session

    # ── Diagnostics ──

    @property
    def idle_timeout_seconds(self) -> int:
        return self._idle_timeout

    @property
    def token_budget(self) -> int:
        return self._token_budget

    def active_count(self) -> int:
        return self._registry.count()
