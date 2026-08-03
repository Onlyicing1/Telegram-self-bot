"""
Database interface layer for the AI subsystem.

This package defines repository interfaces for every future AI database
table. Each repository has:
  - An abstract interface class (the contract)
  - An in-memory fallback implementation (working today, no DB needed)

When the Supabase migrations are applied (creating ai_sessions, ai_messages,
ai_memories, ai_provider_stats, ai_usage, ai_preferences, ai_tool_history),
concrete Supabase-backed implementations will be added alongside the
in-memory ones. The rest of the codebase will not change — it depends on
the interfaces, not the implementations.

Repository mapping to future tables:
  SessionRepository        → ai_sessions
  MessageRepository        → ai_messages
  MemoryRepository         → ai_memories
  ProviderStatsRepository  → ai_provider_stats
  UsageRepository          → ai_usage
  PreferencesRepository    → ai_preferences
  ToolHistoryRepository    → ai_tool_history

No migrations are created here. This is interface-only.
"""
from backend.ai.database.manager import RepositoryManager
from backend.ai.database.memory_repository import (
    InMemoryMemoryRepository,
    MemoryRepository,
)
from backend.ai.database.message_repository import (
    InMemoryMessageRepository,
    MessageRepository,
    MessageRecord,
)
from backend.ai.database.preferences_repository import (
    InMemoryPreferencesRepository,
    PreferencesRecord,
    PreferencesRepository,
)
from backend.ai.database.provider_stats_repository import (
    InMemoryProviderStatsRepository,
    ProviderStatsRecord,
    ProviderStatsRepository,
)
from backend.ai.database.session_repository import (
    InMemorySessionRepository,
    SessionRecord,
    SessionRepository,
)
from backend.ai.database.tool_history_repository import (
    InMemoryToolHistoryRepository,
    ToolHistoryRecord,
    ToolHistoryRepository,
)
from backend.ai.database.usage_repository import (
    InMemoryUsageRepository,
    UsageRecord,
    UsageRepository,
)

__all__ = [
    # Manager
    "RepositoryManager",
    # Memory
    "MemoryRepository",
    "InMemoryMemoryRepository",
    # Sessions
    "SessionRepository",
    "InMemorySessionRepository",
    "SessionRecord",
    # Messages
    "MessageRepository",
    "InMemoryMessageRepository",
    "MessageRecord",
    # Provider stats
    "ProviderStatsRepository",
    "InMemoryProviderStatsRepository",
    "ProviderStatsRecord",
    # Usage
    "UsageRepository",
    "InMemoryUsageRepository",
    "UsageRecord",
    # Preferences
    "PreferencesRepository",
    "InMemoryPreferencesRepository",
    "PreferencesRecord",
    # Tool history
    "ToolHistoryRepository",
    "InMemoryToolHistoryRepository",
    "ToolHistoryRecord",
]
