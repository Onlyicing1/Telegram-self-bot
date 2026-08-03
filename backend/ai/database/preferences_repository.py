"""
PreferencesRepository — persistence interface for AI preferences.

Maps to the future ``ai_preferences`` table. Stores per-owner AI
preferences: language, personality, response style, custom instructions,
feature toggles.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class PreferencesRecord:
    """Per-owner AI preferences (maps to ``ai_preferences`` row)."""
    owner_id: int
    language: str = "English"
    personality: str = "default"
    response_style: str = "concise"
    custom_instructions: str = ""
    auto_memory: bool = True
    auto_tools: bool = True
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "owner_id": self.owner_id,
            "language": self.language,
            "personality": self.personality,
            "response_style": self.response_style,
            "custom_instructions": self.custom_instructions,
            "auto_memory": self.auto_memory,
            "auto_tools": self.auto_tools,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "metadata": dict(self.metadata),
        }


class PreferencesRepository:
    """Abstract interface for preferences persistence."""

    def get_or_create(self, owner_id: int) -> PreferencesRecord:
        raise NotImplementedError

    def update(self, owner_id: int, updates: dict[str, Any]) -> bool:
        raise NotImplementedError

    def get(self, owner_id: int) -> PreferencesRecord | None:
        raise NotImplementedError


class InMemoryPreferencesRepository(PreferencesRepository):
    """In-memory fallback for preferences."""

    __slots__ = ("_prefs",)

    def __init__(self) -> None:
        self._prefs: dict[int, PreferencesRecord] = {}

    def get_or_create(self, owner_id: int) -> PreferencesRecord:
        if owner_id not in self._prefs:
            self._prefs[owner_id] = PreferencesRecord(owner_id=owner_id)
        return self._prefs[owner_id]

    def update(self, owner_id: int, updates: dict[str, Any]) -> bool:
        rec = self.get_or_create(owner_id)
        for key, value in updates.items():
            if hasattr(rec, key):
                setattr(rec, key, value)
        rec.updated_at = datetime.now(timezone.utc)
        return True

    def get(self, owner_id: int) -> PreferencesRecord | None:
        return self._prefs.get(owner_id)
