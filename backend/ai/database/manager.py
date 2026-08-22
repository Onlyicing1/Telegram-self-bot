"""
RepositoryManager — central owner of all AI database repositories.

The manager holds one instance of each repository and provides access
to them. When Supabase is available, concrete implementations will be
injected. When Supabase is not available, in-memory fallbacks are used.

This follows the same pattern as ``backend/db/client.py`` — the bot
works with or without Supabase. Every repository call is wrapped in
error handling that degrades gracefully.

The RepositoryManager is a singleton (one per process), accessed via
``get_repository_manager()``. It is constructed on first access.
"""
from __future__ import annotations

import logging
from typing import Any

from backend.ai.database.memory_repository import (
    InMemoryMemoryRepository,
    MemoryRepository,
    SupabaseMemoryRepository,
)
from backend.ai.database.message_repository import InMemoryMessageRepository, MessageRepository
from backend.ai.database.preferences_repository import InMemoryPreferencesRepository, PreferencesRepository
from backend.ai.database.provider_stats_repository import (
    InMemoryProviderStatsRepository,
    ProviderStatsRepository,
    SupabaseProviderStatsRepository,
)
from backend.ai.database.session_repository import InMemorySessionRepository, SessionRepository
from backend.ai.database.tool_history_repository import InMemoryToolHistoryRepository, ToolHistoryRepository
from backend.ai.database.usage_repository import (
    InMemoryUsageRepository,
    SupabaseUsageRepository,
    UsageRepository,
)

logger = logging.getLogger(__name__)


class RepositoryManager:
    """Central manager for all AI database repositories.

    Holds one instance of each repository. When Supabase-backed
    implementations are added later, they will be injected here.
    For now, all repositories use in-memory fallbacks.
    """

    __slots__ = (
        "_memory",
        "_session",
        "_message",
        "_provider_stats",
        "_usage",
        "_preferences",
        "_tool_history",
        "_supabase_available",
    )

    def __init__(self, supabase_available: bool = False) -> None:
        self._supabase_available = supabase_available
        self._memory = SupabaseMemoryRepository() if supabase_available else InMemoryMemoryRepository()
        self._session = InMemorySessionRepository()
        self._message = InMemoryMessageRepository()
        self._provider_stats = (
            SupabaseProviderStatsRepository() if supabase_available else InMemoryProviderStatsRepository()
        )
        self._usage = SupabaseUsageRepository() if supabase_available else InMemoryUsageRepository()
        self._preferences = InMemoryPreferencesRepository()
        self._tool_history = InMemoryToolHistoryRepository()

        if supabase_available:
            logger.info("RepositoryManager: Supabase available — in-memory fallbacks used until migrations are applied")
        else:
            logger.info("RepositoryManager: Supabase not available — using in-memory fallbacks")

    @property
    def memory(self) -> MemoryRepository:
        return self._memory

    @property
    def session(self) -> SessionRepository:
        return self._session

    @property
    def message(self) -> MessageRepository:
        return self._message

    @property
    def provider_stats(self) -> ProviderStatsRepository:
        return self._provider_stats

    @property
    def usage(self) -> UsageRepository:
        return self._usage

    @property
    def preferences(self) -> PreferencesRepository:
        return self._preferences

    @property
    def tool_history(self) -> ToolHistoryRepository:
        return self._tool_history

    @property
    def supabase_available(self) -> bool:
        return self._supabase_available

    def status(self) -> dict[str, Any]:
        return {
            "supabase_available": self._supabase_available,
            "repositories": [
                "memory", "session", "message",
                "provider_stats", "usage", "preferences", "tool_history",
            ],
        }


_repository_manager: RepositoryManager | None = None


def get_repository_manager() -> RepositoryManager:
    """Return the process-wide RepositoryManager instance.

    Constructs it on first call. This is the single instance — no
    duplicated managers.
    """
    global _repository_manager
    if _repository_manager is None:
        import os
        supabase_url = os.getenv("SUPABASE_URL", "")
        supabase_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
        available = bool(supabase_url and supabase_key)
        _repository_manager = RepositoryManager(supabase_available=available)
    return _repository_manager
