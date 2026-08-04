"""
AI Runtime Layer — in-memory conversation state for AI providers.

This package owns the runtime lifecycle of a conversation as seen by
an AI provider: the active provider/model, the system prompt, bounded
conversation history, tool history, pending tool calls, and token
estimates. It is provider-agnostic and fully offline — no network.

Responsibilities:
  - Create / close / reset conversation sessions (one per owner)
  - Bounded conversation history with token estimation
  - History trimming that preserves system prompt and latest tool result
  - Automatic cleanup of idle sessions (configurable timeout)

What it does NOT do:
  - Call any LLM provider
  - Persist to any database (persistence is handled by persistence.py)
  - Modify any existing bot feature

Public API:
    ConversationManager — central manager for all conversations
    ConversationRegistry — owner→session mapping
    RuntimeSession — a single conversation session
    ConversationHistory — bounded message log
    HistoryItem — a single history entry
    estimate_tokens — coarse token count heuristic
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
