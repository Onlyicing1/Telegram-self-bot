"""Telegram message event handler for event-triggered tasks.

This handler rides the EXISTING Telethon update path (no second update
loop): every new message is normalized into a bounded event context and
given to the supervisor-configured ``TaskEventDispatcher``, which performs
deterministic trigger matching and hands matched occurrences to the shared
``TaskExecutionCoordinator``.

Silent by design: a match never produces a diagnostic Telegram message —
execution outcomes live in ``ai_task_occurrences`` and structured logs, and
user-visible notifications only happen through the task's explicit opt-in
flags. If the dispatcher is not configured (startup ordering, tests), the
handler is a no-op.
"""
from __future__ import annotations

import asyncio
import logging

from telethon import events

logger = logging.getLogger(__name__)

_dispatcher = None


def configure(dispatcher) -> None:
    """Bind the process-wide event dispatcher (called by RuntimeSupervisor)."""
    global _dispatcher
    _dispatcher = dispatcher


def get_dispatcher():
    return _dispatcher


def register(client, owner_id: int, tz_str: str) -> None:
    """Register the event-trigger evaluator on every new message (both
    directions; the deterministic trigger ``direction`` condition decides)."""

    @client.on(events.NewMessage())
    async def _task_event_handler(event):
        dispatcher = _dispatcher
        if dispatcher is None:
            return
        from backend.ai.task_event_dispatcher import extract_event_context
        context = extract_event_context(event)
        try:
            await dispatcher.handle_event(context)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — never poison the event path
            logger.warning(
                "TASK_EVENT_TRACE stage=handler_error chat_id=%s exception=%s",
                context.get("chat_id"), type(exc).__name__,
            )