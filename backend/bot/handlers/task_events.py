"""Telegram message event handler for event-triggered tasks AND answer correlation.

This handler rides the EXISTING Telethon update path (no second update
loop). Every new message is normalized into a bounded event context and:

  1. given to the supervisor-configured ``TaskEventDispatcher``, which
     performs deterministic trigger matching and hands matched occurrences
     to the shared ``TaskExecutionCoordinator``;
  2. given to the supervisor-configured ``TaskAnswerResolver``, which pairs
     an explicit reply to a SENT QUESTION MESSAGE with the one durably
     parked occurrence that asked it (Todo Part 3D). Correlation is exact
     and fail-closed: an unrelated or ambiguous message mutates nothing.

Silent by design: neither path produces diagnostic Telegram messages, and a
resolver miss is the ordinary non-resume case (the owner simply chats on).
If neither component is configured (startup ordering, tests), the handler is
a no-op.
"""
from __future__ import annotations

import asyncio
import logging

from telethon import events

logger = logging.getLogger(__name__)

_dispatcher = None
_answer_resolver = None


def configure(dispatcher) -> None:
    """Bind the process-wide event dispatcher (called by RuntimeSupervisor)."""
    global _dispatcher
    _dispatcher = dispatcher


def configure_answer_resolver(resolver) -> None:
    """Bind the process-wide answer resolver (called by RuntimeSupervisor)."""
    global _answer_resolver
    _answer_resolver = resolver


def get_dispatcher():
    return _dispatcher


def get_answer_resolver():
    return _answer_resolver


def register(client, owner_id: int, tz_str: str) -> None:
    """Register the event-trigger evaluator + answer resolver on every new
    message (both directions; each path applies its own owner gate)."""

    @client.on(events.NewMessage())
    async def _task_event_handler(event):
        dispatcher = _dispatcher
        resolver = _answer_resolver
        if dispatcher is None and resolver is None:
            return
        from backend.ai.task_event_dispatcher import extract_event_context

        context = extract_event_context(event)
        if resolver is not None:
            # Answer correlation FIRST and always: a reply to a pending
            # question must never be double-interpreted as an event trigger
            # firing on the same message, and a miss must stay silent.
            from backend.ai.task_answers import extract_answer_context

            try:
                await resolver.handle_event(extract_answer_context(event))
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — never poison the event path
                logger.warning(
                    "TASK_ANSWER_TRACE stage=handler_error chat_id=%s exception=%s",
                    context.get("chat_id"), type(exc).__name__,
                )
        if dispatcher is None:
            return
        try:
            await dispatcher.handle_event(context)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — never poison the event path
            logger.warning(
                "TASK_EVENT_TRACE stage=handler_error chat_id=%s exception=%s",
                context.get("chat_id"), type(exc).__name__,
            )
