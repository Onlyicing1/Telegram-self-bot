"""One scheduled History checkpoint.

The registered ``scheduled_history`` tool delegates here. This service owns the
deterministic side of a scheduled run:

* load the ONE durable ``history_schedules`` row by the TRUSTED task id;
* re-classify the stored peer and enforce the existing History policy;
* capture a stable upper boundary;
* read one bounded oldest-first batch from the durable lower cursor;
* extend the cumulative summary through the existing History AI boundary;
* commit the summary, both cursors, revision and CAS version in ONE write.

It never creates a task or schedule, never reads a peer/task id from arguments,
and never substitutes process memory when durable storage is unavailable.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from backend.ai.database.task_repository import (
    HistoryStorageUnavailable,
    get_task_repository,
)
from backend.ai.task_contract import SCHEDULED_HISTORY_ACTION
from backend.services import history_ai_service, history_policy, history_service

logger = logging.getLogger(__name__)

#: The provider work is deliberately kept to one bounded checkpoint per run.
#: A large backlog catches up over successive occurrences instead of holding
#: one occurrence open for an unbounded time.
BATCH_MESSAGES = 100
BATCH_PAGE_SIZE = 100
#: Kept below the coordinator's 240s History backstop; the AI service derives
#: its own paced provider budget from this value.
RUN_TIMEOUT_S = 220.0


def _error_text(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"[:2000]


async def _record_failure(repository: Any, state: Any, exc: BaseException) -> None:
    """Best-effort bounded error metadata; never touches summary/cursors."""
    try:
        await repository.record_history_failure(
            state.owner_id,
            state.peer_id,
            expected_version=state.version,
            error=_error_text(exc),
        )
    except asyncio.CancelledError:
        raise
    except Exception as record_exc:  # noqa: BLE001 - failure recording is not the outcome
        logger.warning(
            "HISTORY_SCHEDULE_FAILURE_RECORD_FAILED task_id=%s error=%s",
            state.task_id, type(record_exc).__name__,
        )


def _result(
    *, processed: int, scanned: int, summarized: int, has_more: bool,
    revision: int, version: int,
) -> dict[str, Any]:
    return {
        "operation": SCHEDULED_HISTORY_ACTION,
        "processed": processed,
        "scanned": scanned,
        "summarized": summarized,
        "has_more": has_more,
        "revision": revision,
        "version": version,
    }


async def run_scheduled_history(
    source: Any,
    *,
    owner_id: int,
    task_id: int,
    instruction: Any = "",
    provider_manager: Any = None,
    repository: Any = None,
    request_id: str = "",
    timeout_s: Any = RUN_TIMEOUT_S,
) -> tuple[bool, str, dict[str, Any]]:
    """Run one bounded incremental checkpoint for ``task_id``.

    Returns ``(success, message, data)``. ``message`` is the cumulative summary
    only when new text was actually committed; a no-op run returns an empty
    message so the task's delivery path does not send noise.
    """
    try:
        task_key = int(task_id)
    except (TypeError, ValueError):
        return False, "The scheduled History task id is unavailable.", {
            "error": "scheduled_history_task_missing",
        }
    if task_key <= 0:
        return False, "The scheduled History task id is unavailable.", {
            "error": "scheduled_history_task_missing",
        }

    repo = repository or get_task_repository()
    try:
        state = await repo.get_history_schedule(owner_id, task_key)
    except HistoryStorageUnavailable:
        raise
    except Exception as exc:  # noqa: BLE001 - the tool reports the honest failure
        return False, f"Scheduled History state could not be read: {_error_text(exc)}", {
            "error": "scheduled_history_state_unavailable",
        }
    if state is None:
        return False, "No durable History schedule exists for this task.", {
            "error": "scheduled_history_state_missing",
        }
    if state.status != "active":
        return False, f"The History checkpoint is {state.status} and cannot advance.", {
            "error": "scheduled_history_state_not_active",
        }

    try:
        peer_type = await history_service.classify_chat(source, state.peer_id)
        maximum = history_policy.maximum_for(peer_type)
        if maximum <= 0:
            raise history_service.HistoryError(history_policy.UNKNOWN_REFUSAL)
        batch_limit = min(BATCH_MESSAGES, maximum)
        latest = await history_service.latest_message_id(source, state.peer_id)
        if latest <= state.scanned_cursor:
            return True, "", _result(
                processed=0,
                scanned=state.scanned_cursor,
                summarized=state.summarized_cursor,
                has_more=False,
                revision=state.revision,
                version=state.version,
            )

        # The history window's upper bound is exclusive; +1 includes the newest
        # message that existed when this run captured its stable boundary.
        batch = await history_service.fetch_oldest_history(
            source,
            state.peer_id,
            after_cursor=state.scanned_cursor,
            upper_cursor=latest + 1,
            limit=batch_limit,
            page_size=min(BATCH_PAGE_SIZE, batch_limit),
            include_ai=False,
        )
    except asyncio.CancelledError:
        raise
    except HistoryStorageUnavailable:
        raise
    except Exception as exc:  # noqa: BLE001 - provider/Telegram boundary
        await _record_failure(repo, state, exc)
        return False, f"Scheduled History retrieval failed: {_error_text(exc)}", {
            "error": "scheduled_history_retrieval_failed",
        }

    text_messages = tuple(message for message in batch.messages if message.text.strip())
    next_scanned = max(state.scanned_cursor, batch.next_cursor)

    if not text_messages:
        # A batch of AI-provenance or media-only messages is a SAFE skip: it may
        # advance the scanned cursor, but it must never replace the summary or
        # move the summarized cursor.
        if next_scanned <= state.scanned_cursor:
            return True, "", _result(
                processed=0,
                scanned=state.scanned_cursor,
                summarized=state.summarized_cursor,
                has_more=batch.has_more,
                revision=state.revision,
                version=state.version,
            )
        try:
            committed = await repo.commit_history_success(
                owner_id,
                state.peer_id,
                expected_version=state.version,
                summary=state.summary,
                scanned_cursor=next_scanned,
                summarized_cursor=state.summarized_cursor,
            )
        except HistoryStorageUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001
            await _record_failure(repo, state, exc)
            return False, f"Scheduled History checkpoint could not be saved: {_error_text(exc)}", {
                "error": "scheduled_history_commit_failed",
            }
        if committed is None:
            return False, "The History checkpoint changed before it could be saved.", {
                "error": "scheduled_history_commit_conflict",
            }
        return True, "", _result(
            processed=0,
            scanned=committed.scanned_cursor,
            summarized=committed.summarized_cursor,
            has_more=batch.has_more,
            revision=committed.revision,
            version=committed.version,
        )

    try:
        ok, summary, ai_data = await history_ai_service.summarize_incremental(
            batch.messages,
            previous_summary=state.summary,
            instruction=instruction,
            provider_manager=provider_manager,
            request_id=request_id,
            timeout_s=timeout_s,
        )
    except asyncio.CancelledError:
        raise
    except HistoryStorageUnavailable:
        raise
    except Exception as exc:  # noqa: BLE001
        await _record_failure(repo, state, exc)
        return False, f"Scheduled History summarization failed: {_error_text(exc)}", {
            "error": "scheduled_history_ai_failed",
        }
    if not ok:
        await _record_failure(repo, state, RuntimeError(summary))
        return False, summary, {**ai_data, "error": "scheduled_history_ai_failed"}

    summary = str(summary).strip()
    if not summary:
        error = RuntimeError("the AI provider returned an empty summary")
        await _record_failure(repo, state, error)
        return False, "The AI provider returned an empty summary.", {
            "error": "scheduled_history_ai_failed",
        }
    if len(summary.encode("utf-8")) > 32768:
        error = RuntimeError("the cumulative summary exceeds the durable 32 KiB limit")
        await _record_failure(repo, state, error)
        return False, "The cumulative summary exceeds the durable 32 KiB limit.", {
            "error": "scheduled_history_summary_too_large",
        }

    next_summarized = max(
        state.summarized_cursor,
        max(message.message_id for message in text_messages),
    )
    if next_summarized > next_scanned:
        next_summarized = next_scanned
    try:
        committed = await repo.commit_history_success(
            owner_id,
            state.peer_id,
            expected_version=state.version,
            summary=summary,
            scanned_cursor=next_scanned,
            summarized_cursor=next_summarized,
        )
    except HistoryStorageUnavailable:
        raise
    except Exception as exc:  # noqa: BLE001
        await _record_failure(repo, state, exc)
        return False, f"Scheduled History checkpoint could not be saved: {_error_text(exc)}", {
            "error": "scheduled_history_commit_failed",
        }
    if committed is None:
        return False, "The History checkpoint changed before it could be saved.", {
            "error": "scheduled_history_commit_conflict",
        }

    message = summary
    if batch.has_more:
        message = f"{message}\n\nMore messages are pending; the next run will continue."
    return True, message, _result(
        processed=len(batch.messages),
        scanned=committed.scanned_cursor,
        summarized=committed.summarized_cursor,
        has_more=batch.has_more,
        revision=committed.revision,
        version=committed.version,
    )
