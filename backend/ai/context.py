"""
AIContext — the conversation context object passed to every AI request.

A context bundles everything a provider needs to understand the
conversation: the user's message, chat metadata, optional conversation
history, and the owner's identity.

Context objects are plain data. They carry no behaviour and no I/O.
They are created by the caller (e.g. a command handler) and passed
into ``AIInterface.handle(context)``.

Future providers receive this object read-only and must never mutate it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass(frozen=True)
class AIContext:
    """Immutable bundle of conversation context for a single AI request.

    Attributes:
        owner_id:       Telegram user ID of the bot owner (for per-user state).
        chat_id:        Telegram chat ID where the request originated.
        message_id:     Telegram message ID of the triggering message.
        user_text:      The raw text the owner typed (the prompt).
        chat_title:     Human-readable chat title, if available.
        owner_name:     Display name of the owner, if available.
        timezone:       Timezone string (e.g. ``"Asia/Tehran"``).
        history:        Optional list of prior (role, text) tuples for
                        multi-turn conversations. Each tuple is
                        ``("user" | "assistant", text)``.
        metadata:       Arbitrary key-value bag for provider-specific
                        extras. Providers should not assume any keys
                        exist. Callers may use this to pass hints.
        created_at:     UTC timestamp when this context was constructed.
    """

    owner_id: int
    chat_id: int
    message_id: int
    user_text: str
    chat_title: str = ""
    owner_name: str = ""
    timezone: str = "UTC"
    history: list[tuple[str, str]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
