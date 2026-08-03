"""
Conversation & Context Layer — deterministic runtime context for the AI.

This package is the single source of runtime context for the AI layer.
It tracks conversation state, session metadata, and lightweight history,
and assembles one immutable ``ConversationContext`` object that the
future Prompt Builder will consume.

What this package does:
  - Manages conversation sessions (create, get, close)
  - Tracks deterministic runtime state (finite state machine)
  - Maintains bounded in-memory history (no DB, no persistence)
  - Builds immutable ``ConversationContext`` objects

What this package does NOT do:
  - Call LLM providers
  - Execute tools
  - Generate prompts
  - Persist to database
  - Run background loops or schedulers
  - Modify any existing feature (save, delete, bio, username, settings, etc.)

Public API::

    from backend.ai.conversation import (
        ConversationManager,
        ConversationContext,
        ConversationState,
        ContextBuilder,
        SessionManager,
        HistoryManager,
        ReplyContext,
        SettingsContext,
        ToolContext,
        RuntimeContext,
    )

Architecture (from AI_MASTER_DESIGN.md §4.2, §24, §25)::

    ┌──────────────────────────────────────────────┐
    │             ConversationManager               │
    │  ├─ SessionManager   (session lifecycle)      │
    │  ├─ HistoryManager    (bounded runtime history)│
    │  └─ ContextBuilder    (assembles ConversationContext)│
    └───────────────────────┬──────────────────────┘
                            │
                            ▼
    ┌──────────────────────────────────────────────┐
    │          ConversationContext (frozen)         │
    │  ├─ session_id, owner_id, chat_id, message_id  │
    │  ├─ state (ConversationState enum)            │
    │  ├─ current_menu, panel, category, flow        │
    │  ├─ pending_action, language, timezone          │
    │  ├─ current_time, user_text                     │
    │  ├─ ReplyContext   (replied message metadata)   │
    │  ├─ ToolContext    (current/last tool)          │
    │  ├─ SettingsContext (owner settings snapshot)   │
    │  ├─ RuntimeContext  (AI state, counters)        │
    │  └─ history: list[HistoryEntry]                 │
    └───────────────────────┬──────────────────────┘
                            │
                            ▼ (future)
    ┌──────────────────────────────────────────────┐
    │             Prompt Builder                    │
    │  (receives ConversationContext, builds prompt) │
    └──────────────────────────────────────────────┘
"""
from backend.ai.conversation.context_builder import (
    ContextBuilder,
    ConversationContext,
    ReplyContext,
    RuntimeContext,
    SettingsContext,
    ToolContext,
)
from backend.ai.conversation.conversation import ConversationManager
from backend.ai.conversation.history import HistoryEntry, HistoryManager
from backend.ai.conversation.session import ConversationSession, SessionManager
from backend.ai.conversation.state import (
    ConversationState,
    InvalidTransition,
    allowed_transitions,
    can_transition,
    validate_transition,
)

__all__ = [
    "ConversationManager",
    "ConversationContext",
    "ConversationState",
    "ConversationSession",
    "SessionManager",
    "HistoryManager",
    "HistoryEntry",
    "ContextBuilder",
    "ReplyContext",
    "SettingsContext",
    "ToolContext",
    "RuntimeContext",
    "InvalidTransition",
    "can_transition",
    "validate_transition",
    "allowed_transitions",
]
