"""
Conversation Runtime Layer — the single source of truth for AI runtime.

This package owns the runtime lifecycle of a conversation *as seen by an
AI provider*: the active provider/model, the system prompt, the bounded
conversation history, the tool history, the pending tool call, and a
lightweight token estimate. It is provider-agnostic and fully offline —
no Gemini, no GPT, no GLM, no network call of any kind.

It is deliberately distinct from ``backend.ai.conversation`` (which tracks
UI/panel/flow state keyed by session_id) and from ``backend.ai.session``
(which wires the execution pipeline). Those layers describe *what the
user is doing in the app*; this layer describes *what an AI provider
would need to continue a conversation*. Future providers plug into this
layer and read everything they need from the session object.

Responsibilities (per the Conversation Runtime spec):
  - Create / close / reset conversation sessions
  - Keep temporary in-memory context only — NO persistence
  - One active session per owner (ConversationRegistry)
  - Automatic cleanup of idle sessions (configurable timeout, default 30m)
  - Bounded conversation history with token estimation (len/4)
  - History trimming that preserves the system prompt and the latest
    tool result when the token budget is exceeded
  - Deterministic: a single manager instance, no duplicated registries,
    no global mutable hacks

What it does NOT do:
  - Call any LLM provider
  - Persist to any database
  - Integrate with the bot menu
  - Modify any existing feature

Public API::

    from backend.ai.runtime import (
        ConversationManager,
        ConversationRegistry,
        RuntimeSession,
        ConversationHistory,
        HistoryItem,
        estimate_tokens,
    )
"""
from backend.ai.runtime.history import (
    ConversationHistory,
    HistoryItem,
    estimate_tokens,
)
from backend.ai.runtime.manager import ConversationManager
from backend.ai.runtime.registry import ConversationRegistry
from backend.ai.runtime.session import RuntimeSession

__all__ = [
    "ConversationManager",
    "ConversationRegistry",
    "RuntimeSession",
    "ConversationHistory",
    "HistoryItem",
    "estimate_tokens",
]
