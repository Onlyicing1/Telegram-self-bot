"""Durable question/answer continuation — the explicit Telegram correlation layer.

When a scheduled occurrence parks on ``waiting_answer``, the ONLY thing that
may resume it is the owner's reply to the EXACT question message the
``ask_owner`` tool sent. This module pairs one incoming Telegram message with
at most ONE parked occurrence:

    reply → reply_to_msg_id + chat_id + sender (trusted event context)
          → pending_question record (durable, in the occurrence's metadata)
          → exact task/occurrence/action
          → ONE CAS write (resume_waiting_for_answer) that consumes the answer
            and flips the occurrence to retry_pending(retry_at=now)
          → the EXISTING scheduler/wake loop resumes the SAME occurrence
            through claim → TaskExecutionCoordinator.

Fail closed everywhere: no reply target, a reply to a different message, a
different chat, a different owner, an over-long/blank/non-text answer, or a
park-write that loses the CAS leaves the workflow waiting and mutates nothing.
There is no "latest message" fallback, no "only active task" fallback, and no
second scheduler — the resume rides the retry path the wake loop already
polls generically.

Correlation identities are stored in the occurrence's OWN bounded metadata,
never in process memory: a restart (or a Supabase→memory degradation) cannot
forget a question, and a resumed process answers the same question without
re-asking it.

The handler that feeds this resolver is registered in the EXISTING event path
(``backend/bot/handlers/task_events.py``'s NewMessage handler) — no second
update loop, no second Telethon client.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from backend.ai.database.task_repository import TaskRepository
from backend.ai.task_contract import (
    ACTION_RUNS_KEY,
    ANSWER_FIELD,
    PENDING_QUESTION_KEY,
    QUESTION_ANSWERED_KEY,
    QUESTION_ANSWER_AT_KEY,
    QUESTION_TEXT_KEY,
    answer_run_output,
    normalize_answer,
    pending_question_from_metadata,
    pending_question_correlation_error,
    validate_action_runs,
)

logger = logging.getLogger(__name__)

MAX_RESUMES_PER_MESSAGE = 1


def extract_answer_context(event: Any) -> dict[str, Any]:
    """Normalize a Telethon event into the bounded answer-matching context.

    Only deterministic metadata travels forward — the sender, chat, message id,
    the reply target, and the bounded TEXT of the reply. No raw RPC surface,
    no chat history, no media (an answer is ordinary text by contract).
    """
    message = getattr(event, "message", None)
    text = getattr(event, "raw_text", None) or getattr(message, "message", "") or ""
    if isinstance(text, bytes):
        text = text.decode("utf-8", errors="replace")
    reply_to = getattr(getattr(message, "reply_to", None), "reply_to_msg_id", None)
    return {
        "chat_id": getattr(event, "chat_id", None),
        "sender_id": getattr(event, "sender_id", None),
        "message_id": getattr(message, "id", None) or getattr(event, "id", None),
        "reply_to_message_id": reply_to,
        "text": str(text or ""),
        "has_media": bool(getattr(message, "media", None)),
    }


class TaskAnswerResolver:
    """Owner-scoped resolver: one correlated reply resumes ONE parked occurrence.

    Shares the single repository and the single notification service with the
    scheduler and the event dispatcher — no parallel authority. It never
    executes anything itself: consuming the answer is a repository CAS, and
    the actual continuation is performed by the existing
    TaskScheduler → TaskExecutionCoordinator path on its next wake.
    """

    def __init__(
        self,
        repository: TaskRepository,
        owner_id: int,
    ) -> None:
        self.repository = repository
        self.owner_id = owner_id

    async def handle_event(self, event_context: dict[str, Any]) -> bool:
        """Match one message against parked questions; True when consumed.

        Never raises into the Telegram event path. False means "not an
        answer to any pending question" — the ordinary non-resume case, in
        which NOTHING is matched, mutated or resumed (the fail-closed rule).
        """
        chat_id = event_context.get("chat_id")
        sender_id = event_context.get("sender_id")
        reply_to = event_context.get("reply_to_message_id")
        if not isinstance(chat_id, int) or chat_id == 0:
            return False
        if not isinstance(sender_id, int) or sender_id != self.owner_id:
            # Ownership gate: a non-owner's message can never be an answer,
            # even in a shared chat where a question is visible.
            return False

        try:
            parked = await self.repository.list_waiting_answer_occurrences(
                self.owner_id, 20
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "TASK_ANSWER_TRACE stage=list_failed exception=%s", type(exc).__name__
            )
            return False

        consumed = False
        for occurrence in parked:
            if consumed:
                break
            record = self._pending_question(occurrence)
            if record is None:
                continue
            error = pending_question_correlation_error(
                record,
                owner_id=self.owner_id,
                chat_id=chat_id,
                reply_to_message_id=reply_to,
            )
            if error:
                if error not in ("answer_not_correlated",):
                    logger.info(
                        "TASK_ANSWER_TRACE stage=correlation_refused reason=%s "
                        "task_id=%s occurrence_key=%s",
                        error, occurrence.task_id, occurrence.occurrence_key,
                    )
                continue
            answer, answer_error = normalize_answer(event_context.get("text"))
            if answer_error:
                logger.info(
                    "TASK_ANSWER_TRACE stage=answer_refused reason=%s task_id=%s "
                    "occurrence_key=%s",
                    answer_error, occurrence.task_id, occurrence.occurrence_key,
                )
                # Bounded refusals (a sticker/photo instead of text, an
                # over-long reply) are NOT consumed: the question stays open.
                continue
            consumed = await self._consume(occurrence, record, answer)
        return consumed

    @staticmethod
    def _pending_question(occurrence: Any) -> dict[str, Any] | None:
        """The ONE open question of a parked occurrence, or None."""
        try:
            record = pending_question_from_metadata(occurrence.result_metadata)
            if record is None:
                record = pending_question_from_metadata(occurrence.error_metadata)
        except Exception:
            logger.warning(
                "TASK_ANSWER_TRACE stage=pending_question_invalid task_id=%s "
                "occurrence_key=%s",
                getattr(occurrence, "task_id", -1),
                getattr(occurrence, "occurrence_key", "-"),
            )
            return None
        if record is not None and record.get(QUESTION_ANSWERED_KEY):
            return None
        return record

    async def _consume(
        self,
        occurrence: Any,
        record: dict[str, Any],
        answer: str,
    ) -> bool:
        """The ONE durable consume: answer + succeeded run + resume instant.

        ``resume_waiting_for_answer`` is a CAS against ``waiting_answer``, so a
        racing duplicate reply (or a double delivery of the same reply) loses
        and returns None — the downstream chain can never be resumed twice by
        one question. On success the persisted metadata already carries the
        question run's ``succeeded`` record, which the coordinator re-proves
        on resume; nothing here executes a tool.
        """
        position = record["action"]
        runs = self._runs_of(occurrence)
        try:
            runs = validate_action_runs(runs)
        except Exception:
            logger.warning(
                "TASK_ANSWER_TRACE stage=run_record_invalid task_id=%s occurrence_key=%s",
                occurrence.task_id, occurrence.occurrence_key,
            )
            return False
        by_position = {run["position"]: run for run in runs}
        existing = by_position.get(position)
        if existing is not None and existing["status"] not in ("pending", "running"):
            # Not the state a parked question leaves: refuse rather than guess.
            logger.warning(
                "TASK_ANSWER_TRACE stage=question_run_unexpected status=%s task_id=%s "
                "occurrence_key=%s",
                existing["status"], occurrence.task_id, occurrence.occurrence_key,
            )
            return False
        if existing is None:
            runs.append({
                "position": position, "tool": "ask_owner", "status": "pending",
            })
            runs = sorted(runs, key=lambda run: run["position"])
        by_position = {run["position"]: run for run in runs}
        by_position[position] = {
            "position": position, "tool": "ask_owner", "status": "succeeded",
            "output": answer_run_output(answer),
        }
        updated_record = dict(record)
        updated_record[QUESTION_ANSWERED_KEY] = True
        updated_record[QUESTION_ANSWER_AT_KEY] = self._now_iso()
        metadata = {
            "pending_answer": answer,
            PENDING_QUESTION_KEY: updated_record,
            ACTION_RUNS_KEY: sorted(by_position.values(), key=lambda run: run["position"]),
        }
        try:
            resumed = await self.repository.resume_waiting_for_answer(
                self.owner_id, occurrence.task_id, occurrence.occurrence_key,
                answer=answer, answer_metadata=metadata,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "TASK_ANSWER_TRACE stage=resume_failed task_id=%s occurrence_key=%s "
                "exception=%s",
                occurrence.task_id, occurrence.occurrence_key, type(exc).__name__,
            )
            return False
        if resumed is None:
            # Another writer (a racing duplicate reply) consumed it first.
            logger.info(
                "TASK_ANSWER_TRACE stage=resume_race_lost task_id=%s occurrence_key=%s",
                occurrence.task_id, occurrence.occurrence_key,
            )
            return False
        logger.info(
            "TASK_ANSWER_CONSUMED task_id=%s occurrence_key=%s action=%s "
            "resume_status=%s",
            occurrence.task_id, occurrence.occurrence_key, position,
            getattr(resumed, "status", "-"),
        )
        return True

    @staticmethod
    def _runs_of(occurrence: Any) -> list[dict[str, Any]]:
        for metadata in (occurrence.result_metadata, occurrence.error_metadata):
            if isinstance(metadata, dict) and ACTION_RUNS_KEY in metadata:
                return metadata.get(ACTION_RUNS_KEY) or []
        return []

    @staticmethod
    def _now_iso() -> str:
        from datetime import datetime, timezone

        return datetime.now(timezone.utc).isoformat()


def get_answer_resolver():
    """The process answer resolver, or None before the supervisor wires it."""
    from backend.bot.handlers import task_events

    return getattr(task_events, "get_answer_resolver", lambda: None)()


__all__ = [
    "TaskAnswerResolver",
    "extract_answer_context",
    "MAX_RESUMES_PER_MESSAGE",
]
