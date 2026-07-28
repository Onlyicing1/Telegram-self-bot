"""
TargetContext — abstracts how a panel resolves its target message.

Supports:
  - Reply target (a message the owner replied to)
  - Future Link target (reserved for future link-based targeting)
  - Future Forward target (reserved for future forward-based targeting)

The panel system never knows how the target was resolved — it only
requests a TargetContext and calls resolve() to get the message object.
"""
import logging
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

_EXPIRY_S = 300
_store: dict[int, "TargetContext"] = {}


@dataclass
class TargetContext:
    owner_id: int
    kind: str
    reply_chat_id: int = 0
    reply_msg_id: int = 0
    tz_str: str = "UTC"
    _ts: float = field(default_factory=time.time)

    def is_expired(self) -> bool:
        return time.time() - self._ts > _EXPIRY_S

    async def resolve(self, client: Any):
        if self.kind != "reply":
            return None
        if not self.reply_chat_id or not self.reply_msg_id:
            return None
        try:
            return await client.get_messages(self.reply_chat_id, ids=self.reply_msg_id)
        except Exception as exc:
            logger.warning("TargetContext resolve failed: %s", exc)
            return None


def set_target(owner_id: int, ctx: TargetContext) -> None:
    _store[owner_id] = ctx


def get_target(owner_id: int) -> TargetContext | None:
    ctx = _store.get(owner_id)
    if ctx is None:
        return None
    if ctx.is_expired():
        _store.pop(owner_id, None)
        return None
    return ctx


def clear_target(owner_id: int) -> TargetContext | None:
    return _store.pop(owner_id, None)


def clear_all() -> None:
    """Clear all target contexts."""
    _store.clear()
