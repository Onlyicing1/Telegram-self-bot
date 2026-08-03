"""
ToolContext — the dependency bundle injected into every tool execution.

Tools never access globals. They receive a ``ToolContext`` that carries
everything they need: the Telethon client, the owner's Telegram user ID,
and the timezone string. The context is constructed once by the runtime
supervisor and passed through the registry on each call.

This object is immutable. Tools must not mutate it.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ToolContext:
    """Immutable context injected into every ``tool.execute()`` call.

    Attributes:
        client:    The active Telethon client (for API calls via services).
        owner_id:   Telegram numeric user ID of the bot owner.
        tz_str:    Timezone string (e.g. ``"Asia/Tehran"``).
        extra:      Optional bag for future extensions (reply message,
                    chat_id, etc.). Tools should not assume any keys exist.
    """

    client: Any
    owner_id: int
    tz_str: str
    extra: dict[str, Any] = None  # type: ignore[assignment]
