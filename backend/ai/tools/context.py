"""
ToolContext — the dependency bundle injected into every tool execution.

Tools never access globals. They receive a ``ToolContext`` that carries
everything they need: the TelegramAPI facade, the owner's Telegram user
ID, and the timezone string.

The ``client`` field is retained for backward compatibility with the
existing service layer (which expects a raw Telethon client for complex
media operations). AI Tools should use ``telegram`` (the TelegramAPI
facade) for all Telegram operations — never ``client``.

This object is immutable. Tools must not mutate it.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ToolContext:
    """Immutable context injected into every ``tool.execute()`` call.

    Attributes:
        telegram:  The TelegramAPI facade — the ONLY way AI Tools
                   interact with Telegram. Clean async methods, no
                   raw Telethon objects.
        owner_id:   Telegram numeric user ID of the bot owner.
        tz_str:    Timezone string (e.g. ``"Asia/Tehran"``).
        client:    The active Telethon client. Retained for the service
                   layer's use — AI Tools must NOT use this directly.
        extra:      Optional bag for future extensions (reply message,
                    chat_id, etc.). Tools should not assume any keys exist.
    """

    telegram: Any
    owner_id: int
    tz_str: str
    client: Any = None
    extra: dict[str, Any] = None  # type: ignore[assignment]
