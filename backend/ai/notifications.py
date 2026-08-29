"""Structured task notifications; notification failure never changes state."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Awaitable, Callable

MAX_NOTIFICATION_CHARS = 1024


@dataclass(frozen=True)
class TaskNotification:
    owner_id: int
    task_id: int
    occurrence_key: str
    kind: str
    message: str


class TaskNotificationService:
    """Formats state notifications and delegates delivery to an injected sender."""

    def __init__(self, sender: Callable[[int, str], Awaitable[object]], owner_id: int) -> None:
        self._sender = sender
        self._owner_id = owner_id

    async def send(self, notification: TaskNotification) -> bool:
        if notification.owner_id != self._owner_id:
            return False
        if notification.kind not in {"succeeded", "failed", "retry_pending", "cancelled"}:
            return False
        message = notification.message[:MAX_NOTIFICATION_CHARS]
        try:
            await asyncio.wait_for(self._sender(self._owner_id, message), timeout=10.0)
            return True
        except asyncio.CancelledError:
            raise
        except Exception:
            return False
