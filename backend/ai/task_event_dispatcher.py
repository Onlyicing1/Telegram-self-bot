"""Event-driven task execution: deterministic trigger matching + occurrence handoff.

A Telegram message event enters through the existing Self Bot event path
(``backend/bot/handlers/task_events.py``), is normalized into a bounded
event-context dict, and this dispatcher:

  1. lists the owner's active ``schedule_type='event'`` tasks (bounded),
  2. evaluates each stored trigger deterministically (never an LLM call),
  3. creates ONE occurrence per (task, message) with a deterministic key,
  4. claims and executes it through the SAME TaskExecutionCoordinator the
     time scheduler uses (registry-checked, owner-scoped, CAS/retry-safe),
  5. records the outcome and applies the existing opt-in notification
     policy — silent by default.

No second scheduler, no second event loop, no second executor: this is a
synchronous responder inside the existing Telethon update path, with the
runtime as the only execution authority.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from backend.ai.database.task_repository import TaskRepository
from backend.ai.scheduling import ScheduleError, is_event_schedule, parse_schedule
from backend.ai.task_trigger import event_trigger_matches

logger = logging.getLogger(__name__)

MAX_EVENT_TASKS_PER_MESSAGE = 20
MAX_EVENT_EXECUTIONS_PER_MESSAGE = 5
_EVENT_TERMINAL_OCCURRENCE_STATUSES = frozenset(
    {"succeeded", "failed", "cancelled", "expired"}
)


def event_occurrence_key(task_id: int, chat_id: int, message_id: int) -> str:
    """Deterministic occurrence identity from trusted event attributes.

    The same (chat, message) delivered twice — Telethon redelivery,
    reconnect replay, process restart — maps to the same key, so the
    unique ``(task_id, occurrence_key)`` index prevents duplicate
    executions of the same event.
    """
    return f"{task_id}:ev:{chat_id}:{message_id}"


def extract_event_context(event: Any) -> dict[str, Any]:
    """Normalize a Telethon event into the bounded matcher context.

    Only deterministic event metadata travels forward — never message
    content beyond the bounded text used for content conditions, and never
    any raw RPC surface.
    """
    message = getattr(event, "message", None)
    text = getattr(event, "raw_text", None) or getattr(message, "message", "") or ""
    if isinstance(text, bytes):
        text = text.decode("utf-8", errors="replace")
    return {
        "chat_id": getattr(event, "chat_id", None),
        "sender_id": getattr(event, "sender_id", None),
        "message_id": getattr(message, "id", None) or getattr(event, "id", None),
        "text": str(text or "")[:8192],
        "has_media": bool(getattr(message, "media", None)),
        "is_reply": bool(getattr(message, "reply_to_msg_id", None)),
        "out": bool(getattr(event, "out", False)),
        "date": getattr(message, "date", None),
    }


class TaskEventDispatcher:
    """Owner-scoped deterministic event-trigger responder.

    Shares the single repository singleton, the single
    ``TaskExecutionCoordinator``, and the single outcome notifier with the
    time scheduler — no parallel authorities.
    """

    def __init__(
        self,
        repository: TaskRepository,
        owner_id: int,
        execution_coordinator=None,
        outcome_notifier=None,
    ) -> None:
        self.repository = repository
        self.owner_id = owner_id
        self.execution_coordinator = execution_coordinator
        self.outcome_notifier = outcome_notifier

    async def _notify_outcome(self, task_id: int, occurrence_key: str, status: str) -> None:
        notifier = self.outcome_notifier
        if notifier is None:
            return
        if status not in ("succeeded", "failed", "retry_pending", "cancelled"):
            return
        try:
            await notifier.notify_persisted(task_id, occurrence_key, status)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Task notification failed for %s", occurrence_key)

    async def _execute_one(self, task: Any, occurrence: Any) -> None:
        coordinator = self.execution_coordinator
        claimed = await self.repository.claim_occurrence(
            self.owner_id, occurrence.task_id, occurrence.occurrence_key
        )
        if claimed is None or claimed.status != "running":
            return
        if coordinator is None:
            # No execution authority wired (shutdown/tests): mark the
            # occurrence interrupted so recovery can re-own it later.
            await self.repository.transition_occurrence(
                self.owner_id, occurrence.task_id, occurrence.occurrence_key, "interrupted"
            )
            return
        result = await coordinator.execute(claimed)
        await self._notify_outcome(
            occurrence.task_id, occurrence.occurrence_key, getattr(result, "status", "unknown")
        )

    async def handle_event(self, event_context: dict[str, Any]) -> int:
        """Evaluate the event against active event tasks; return executions started.

        Never raises into the Telegram event path: every failure is logged
        with bounded structured diagnostics, and the occurrence (if any)
        follows the existing retry/interrupted semantics.
        """
        chat_id = event_context.get("chat_id")
        message_id = event_context.get("message_id")
        if not isinstance(chat_id, int) or chat_id == 0:
            return 0
        if not isinstance(message_id, int) or message_id <= 0:
            return 0

        executed = 0
        try:
            tasks = await self.repository.list_event_tasks(
                self.owner_id, MAX_EVENT_TASKS_PER_MESSAGE
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "TASK_EVENT_TRACE stage=list_failed owner_id=%s exception=%s",
                self.owner_id, type(exc).__name__,
            )
            return 0

        for task in tasks:
            if executed >= MAX_EVENT_EXECUTIONS_PER_MESSAGE:
                break
            try:
                schedule = parse_schedule(task.schedule_type, task.schedule)
                if not is_event_schedule(task.schedule_type):
                    continue
                trigger = getattr(schedule, "trigger", None)
                if trigger is None or not event_trigger_matches(trigger, event_context):
                    continue
                executed += await self._dispatch(task, event_context)
            except ScheduleError as exc:
                logger.warning(
                    "TASK_EVENT_TRACE stage=schedule_invalid task_id=%s detail=%s",
                    task.id, str(exc)[:160],
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "TASK_EVENT_TRACE stage=dispatch_failed task_id=%s "
                    "chat_id=%s message_id=%s exception=%s",
                    task.id, chat_id, message_id, type(exc).__name__,
                )
        return executed

    async def _dispatch(self, task: Any, event_context: dict[str, Any]) -> int:
        chat_id = event_context["chat_id"]
        message_id = event_context["message_id"]
        key = event_occurrence_key(task.id, chat_id, message_id)
        date = event_context.get("date")
        if not isinstance(date, datetime):
            date = datetime.now(timezone.utc)
        elif date.tzinfo is None:
            date = date.replace(tzinfo=timezone.utc)
        else:
            date = date.astimezone(timezone.utc)

        try:
            occurrence = await self.repository.create_occurrence(self.owner_id, {
                "task_id": task.id,
                "occurrence_key": key,
                "definition_version": task.version,
                "action_snapshot": task.actions,
                "scheduled_for": date,
            })
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "TASK_EVENT_TRACE stage=occurrence_create_failed task_id=%s "
                "occurrence_key=%s exception=%s",
                task.id, key, type(exc).__name__,
            )
            return 0

        # Duplicate delivery of the same (task, chat, message): a fresh
        # occurrence is created as "claimed", so any other status means the
        # key already exists and is owned by an earlier delivery (running /
        # terminal / retry-pending). The claim CAS in ``_execute_one`` is the
        # final guard for the concurrent-race window — two dispatches of the
        # same event can never both move it to running.
        status = str(getattr(occurrence, "status", "") or "")
        if status in _EVENT_TERMINAL_OCCURRENCE_STATUSES or status in ("running", "retry_pending"):
            logger.debug(
                "TASK_EVENT_TRACE stage=duplicate_skipped task_id=%s occurrence_key=%s status=%s",
                task.id, key, status,
            )
            return 0

        try:
            await self._execute_one(task, occurrence)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "TASK_EVENT_TRACE stage=execution_failed task_id=%s occurrence_key=%s exception=%s",
                task.id, key, type(exc).__name__,
            )
            return 0
        logger.info(
            "TASK_EVENT_TRACE stage=executed task_id=%s occurrence_key=%s "
            "chat_id=%s message_id=%s",
            task.id, key, chat_id, message_id,
        )
        return 1