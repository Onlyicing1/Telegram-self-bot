"""
AISession — the single visible AI runtime that ties the pipeline together.

An ``AISession`` is the ONE object a future caller (command handler,
panel callback) interacts with. It owns:

  - A ``SessionManager`` for AI session lifecycle (create/destroy/reset).
  - A ``ConversationManager`` for conversation state and context building.
  - A ``ProviderRegistry`` (from the Provider Factory) for provider routing.
  - A ``Pipeline`` that wires all layers in the correct order.

The session is constructed once by the runtime supervisor and injected
wherever needed. No globals, no singletons.

Currently, only one active AI session is supported. The DummyProvider
is always the default, so every ``process()`` call returns a
deterministic response. No network call is ever made.

Usage (future, when wired into a handler)::

    ai = AISession()
    session_id = ai.create_session(owner_id=123, chat_id=456)
    request = AIRequest(
        session_id=session_id,
        user_message="Save this message",
        owner_id=123,
        chat_id=456,
        message_id=789,
    )
    response = ai.process(request)
    # response.text → "AI layer is operational.\nNo external provider configured."
"""
from __future__ import annotations

import logging

from backend.ai.conversation.conversation import ConversationManager
from backend.ai.providers.factory import ProviderFactory
from backend.ai.providers.registry import ProviderRegistry
from backend.ai.session.manager import SessionManager
from backend.ai.session.pipeline import Pipeline
from backend.ai.session.request import AIRequest
from backend.ai.session.response import AIResponse
from backend.ai.session.state import AISessionState

logger = logging.getLogger(__name__)


class AISession:
    """The single visible AI runtime.

    Owns the session manager, conversation manager, provider registry,
    and pipeline. The caller creates an ``AISession``, starts a session,
    builds ``AIRequest`` objects, and calls ``process()``.

    Currently supports one active session. The DummyProvider is always
    the default — no external model is ever contacted.
    """

    __slots__ = ("_session_mgr", "_conversation", "_registry", "_pipeline")

    def __init__(
        self,
        conversation: ConversationManager | None = None,
        registry: ProviderRegistry | None = None,
    ) -> None:
        self._session_mgr = SessionManager()
        self._conversation = conversation or ConversationManager()
        self._registry = registry or ProviderFactory.create_registry()
        self._pipeline = Pipeline(self._conversation, self._registry)

    # ── Session lifecycle ──

    def create_session(
        self,
        owner_id: int,
        chat_id: int,
        session_id: str | None = None,
        language: str = "English",
        timezone: str = "UTC",
    ) -> str:
        """Create a new AI session. Returns the session_id.

        Also creates the underlying conversation session so the
        ConversationManager has state to build context from.
        """
        sid = self._session_mgr.create_session(
            owner_id=owner_id,
            chat_id=chat_id,
            session_id=session_id,
            language=language,
            timezone=timezone,
        )
        self._conversation.start_session(
            owner_id=owner_id,
            chat_id=chat_id,
            session_id=sid,
            language=language,
            timezone=timezone,
        )
        self._session_mgr.set_state(sid, AISessionState.CREATED)
        logger.info("AISession: created session '%s'", sid)
        return sid

    def destroy_session(self, session_id: str) -> bool:
        """Destroy an AI session. Returns True if it existed."""
        self._conversation.end_session(session_id)
        return self._session_mgr.destroy_session(session_id)

    def get_session(self, session_id: str) -> dict | None:
        """Return session metadata, or None if not found."""
        return self._session_mgr.get_session(session_id)

    def reset_session(self, session_id: str) -> bool:
        """Reset a session to CREATED state. Returns True if it existed."""
        self._conversation.clear_history(session_id)
        return self._session_mgr.reset_session(session_id)

    def active_session(self) -> str | None:
        """Return the current active session ID, or None."""
        return self._session_mgr.active_session()

    # ── Request processing ──

    def process(self, request: AIRequest) -> AIResponse:
        """Process an AI request through the full pipeline.

        This is the ONE method a caller invokes. It:
          1. Sets the session state to ACTIVE.
          2. Adds the user message to conversation history.
          3. Runs the pipeline (Conversation → Prompt → Provider → Response).
          4. Adds the AI response to conversation history.
          5. Sets the session state to COMPLETED (or ERROR on failure).

        Currently always returns a deterministic DummyProvider response.
        No network call is ever made.
        """
        sid = request.session_id
        self._session_mgr.set_state(sid, AISessionState.ACTIVE)
        self._session_mgr.increment_request_count(sid)

        self._conversation.add_user_message(sid, request.user_message)

        response = self._pipeline.execute(request)

        if response.success:
            self._conversation.add_assistant_message(sid, response.text)
            self._session_mgr.set_state(sid, AISessionState.COMPLETED)
        else:
            self._session_mgr.set_state(sid, AISessionState.ERROR)

        return response

    # ── Introspection ──

    def is_active(self, session_id: str) -> bool:
        """Check whether a session is in the ACTIVE state."""
        session = self._session_mgr.get_session(session_id)
        if session is None:
            return False
        return session["state"] == AISessionState.ACTIVE

    def get_state(self, session_id: str) -> AISessionState | None:
        """Return the lifecycle state of a session, or None if not found."""
        session = self._session_mgr.get_session(session_id)
        if session is None:
            return None
        return session["state"]

    def get_conversation(self) -> ConversationManager:
        """Return the underlying ConversationManager (for advanced use)."""
        return self._conversation

    def get_registry(self) -> ProviderRegistry:
        """Return the underlying ProviderRegistry (for advanced use)."""
        return self._registry
