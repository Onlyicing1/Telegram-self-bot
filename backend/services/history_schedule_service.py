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
import time
from typing import Any

from backend.ai.database.task_repository import (
    HistoryStorageUnavailable,
    get_task_repository,
)
from backend.ai.task_contract import SCHEDULED_HISTORY_ACTION
from backend.ai.task_trace import occurrence_trace
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


async def _record_failure(
    repository: Any,
    state: Any,
    exc: BaseException,
    *,
    category: str = "unknown",
) -> None:
    """Best-effort bounded error metadata; never touches summary/cursors.

    The trace records only the stage category, outcome, expected/new version
    and exception CLASS. The error payload written to the durable row keeps
    its existing bounded text; that text is never copied into a trace line.
    """
    occurrence_trace(
        "failure_metadata_write_started",
        failure_category=category,
        expected_version=getattr(state, "version", "-"),
    )
    try:
        recorded = await repository.record_history_failure(
            state.owner_id,
            state.peer_id,
            expected_version=state.version,
            error=_error_text(exc),
        )
    except asyncio.CancelledError:
        occurrence_trace(
            "failure_metadata_write_result", outcome="cancelled",
            failure_category=category,
        )
        raise
    except Exception as record_exc:  # noqa: BLE001 - failure recording is not the outcome
        occurrence_trace(
            "failure_metadata_write_result", outcome="durable_error",
            failure_category=category,
            exception=type(record_exc).__name__,
        )
        # Bounded, single-line transport/database detail: the class alone made
        # the live serialization TypeError unactionable. This text contains no
        # message body, summary, prompt or identifier.
        detail = " ".join(str(record_exc).split())[:200]
        logger.warning(
            "HISTORY_SCHEDULE_FAILURE_RECORD_FAILED task_id=%s error=%s detail=%s",
            state.task_id, type(record_exc).__name__, detail or "-",
        )
        return
    occurrence_trace(
        "failure_metadata_write_result",
        outcome="recorded" if recorded is not None else "conflict",
        failure_category=category,
        new_version=getattr(recorded, "version", "-") if recorded is not None else "-",
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


def _finish_checkpoint(
    success: bool,
    message: str,
    data: dict[str, Any],
    *,
    outcome: str,
) -> tuple[bool, str, dict[str, Any]]:
    """Emit one terminal category record without logging message content."""
    occurrence_trace(
        "checkpoint_result",
        outcome=outcome,
        success=success,
        error_category=(data or {}).get("error", "-"),
        processed=(data or {}).get("processed", "-"),
        has_more=(data or {}).get("has_more", "-"),
        revision=(data or {}).get("revision", "-"),
        version=(data or {}).get("version", "-"),
    )
    return success, message, data


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
    message so the task's delivery path does not send noise. The wrapper only
    adds terminal/cancellation tracing; execution semantics are unchanged.
    """
    started = time.perf_counter()
    try:
        result = await _run_scheduled_history(
            source,
            owner_id=owner_id,
            task_id=task_id,
            instruction=instruction,
            provider_manager=provider_manager,
            repository=repository,
            request_id=request_id,
            timeout_s=timeout_s,
        )
    except asyncio.CancelledError:
        occurrence_trace(
            "checkpoint_cancelled", category="cancelled",
            duration_ms=round((time.perf_counter() - started) * 1000, 1),
        )
        raise
    except Exception as exc:  # noqa: BLE001 - tracing must not alter semantics
        occurrence_trace(
            "checkpoint_exception", exception=type(exc).__name__,
            category="unhandled_service_exception",
            duration_ms=round((time.perf_counter() - started) * 1000, 1),
        )
        raise
    occurrence_trace(
        "checkpoint_completed", success=result[0],
        duration_ms=round((time.perf_counter() - started) * 1000, 1),
    )
    return result


async def _run_scheduled_history(
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
    """The existing bounded checkpoint pipeline, unchanged in behavior."""
    try:
        task_key = int(task_id)
    except (TypeError, ValueError):
        occurrence_trace("task_identity_result", outcome="invalid")
        return _finish_checkpoint(False, "The scheduled History task id is unavailable.", {
            "error": "scheduled_history_task_missing",
        }, outcome="invalid_task")
    if task_key <= 0:
        occurrence_trace("task_identity_result", outcome="invalid")
        return _finish_checkpoint(False, "The scheduled History task id is unavailable.", {
            "error": "scheduled_history_task_missing",
        }, outcome="invalid_task")
    occurrence_trace("task_identity_result", outcome="valid")

    repo = repository or get_task_repository()
    occurrence_trace("state_lookup_started")
    try:
        state = await repo.get_history_schedule(owner_id, task_key)
    except HistoryStorageUnavailable:
        occurrence_trace("state_lookup_result", outcome="unavailable", category="durable_store_unavailable")
        raise
    except Exception as exc:  # noqa: BLE001 - the tool reports the honest failure
        category = "invalid" if isinstance(exc, (TypeError, ValueError)) else "read_error"
        occurrence_trace(
            "state_lookup_result", outcome="failed", category=category,
            exception=type(exc).__name__,
        )
        return _finish_checkpoint(False, f"Scheduled History state could not be read: {_error_text(exc)}", {
            "error": "scheduled_history_state_unavailable",
        }, outcome="state_unavailable")
    if state is None:
        occurrence_trace("state_lookup_result", outcome="missing")
        return _finish_checkpoint(False, "No durable History schedule exists for this task.", {
            "error": "scheduled_history_state_missing",
        }, outcome="state_missing")
    occurrence_trace(
        "state_lookup_result", outcome="found", status=state.status,
        revision=state.revision, version=state.version,
    )
    if state.status != "active":
        occurrence_trace(
            "state_validation_result", outcome="inactive", status=state.status,
        )
        return _finish_checkpoint(False, f"The History checkpoint is {state.status} and cannot advance.", {
            "error": "scheduled_history_state_not_active",
        }, outcome="state_inactive")
    occurrence_trace("state_validation_result", outcome="active")

    stage = "peer_classification"
    policy_refused = False
    try:
        occurrence_trace("peer_classification_started")
        peer_type = await history_service.classify_chat(source, state.peer_id)
        occurrence_trace("peer_classification_result", peer_type=peer_type.value)
        maximum = history_policy.maximum_for(peer_type)
        if maximum <= 0:
            occurrence_trace(
                "policy_result", outcome="refused", peer_type=peer_type.value,
                maximum=maximum,
            )
            policy_refused = True
            raise history_service.HistoryError(history_policy.UNKNOWN_REFUSAL)
        occurrence_trace(
            "policy_result", outcome="allowed", peer_type=peer_type.value,
            maximum=maximum, batch_limit=min(BATCH_MESSAGES, maximum),
        )
        batch_limit = min(BATCH_MESSAGES, maximum)
        stage = "upper_bound"
        occurrence_trace("upper_bound_capture_started")
        latest = await history_service.latest_message_id(source, state.peer_id)
        pending = latest > state.scanned_cursor
        occurrence_trace(
            "upper_bound_capture_result", outcome="captured",
            pending=pending, pending_span=max(0, latest - state.scanned_cursor),
        )
        if not pending:
            return _finish_checkpoint(True, "", _result(
                processed=0,
                scanned=state.scanned_cursor,
                summarized=state.summarized_cursor,
                has_more=False,
                revision=state.revision,
                version=state.version,
            ), outcome="no_new_messages")

        stage = "retrieval"
        page_size = min(BATCH_PAGE_SIZE, batch_limit)
        occurrence_trace(
            "retrieval_started", limit=batch_limit, page_size=page_size,
            include_ai=False,
        )
        # The history window's upper bound is exclusive; +1 includes the newest
        # message that existed when this run captured its stable boundary.
        batch = await history_service.fetch_oldest_history(
            source,
            state.peer_id,
            after_cursor=state.scanned_cursor,
            upper_cursor=latest + 1,
            limit=batch_limit,
            page_size=page_size,
            include_ai=False,
        )
        occurrence_trace(
            "retrieval_completed", messages=len(batch.messages),
            scanned=batch.scanned, has_more=batch.has_more,
            next_cursor_advanced=batch.next_cursor > state.scanned_cursor,
            next_cursor_delta=max(0, batch.next_cursor - state.scanned_cursor),
        )
    except asyncio.CancelledError:
        raise
    except HistoryStorageUnavailable:
        occurrence_trace(
            "checkpoint_storage_failure", failure_stage=stage,
            category="durable_store_unavailable",
        )
        raise
    except Exception as exc:  # noqa: BLE001 - provider/Telegram boundary
        if policy_refused:
            outcome = "peer_policy_refused"
            category = "peer_policy"
        elif stage == "peer_classification":
            outcome = "peer_classification_failed"
            category = "peer_classification"
        elif stage == "upper_bound":
            outcome = "upper_bound_failed"
            category = "upper_bound"
        else:
            outcome = "retrieval_failed"
            category = "telegram_retrieval"
        occurrence_trace(
            "retrieval_failed", failure_stage=stage, category=category,
            exception=type(exc).__name__,
        )
        await _record_failure(repo, state, exc, category=category)
        return _finish_checkpoint(False, f"Scheduled History retrieval failed: {_error_text(exc)}", {
            "error": "scheduled_history_retrieval_failed",
        }, outcome=outcome)

    text_messages = tuple(message for message in batch.messages if message.text.strip())
    next_scanned = max(state.scanned_cursor, batch.next_cursor)
    occurrence_trace(
        "batch_classified", messages=len(batch.messages),
        text_messages=len(text_messages),
        skipped_without_text=len(batch.messages) - len(text_messages),
        has_more=batch.has_more,
    )

    if not text_messages:
        # A batch of AI-provenance or media-only messages is a SAFE skip: it may
        # advance the scanned cursor, but it must never replace the summary or
        # move the summarized cursor.
        occurrence_trace("safe_skip_evaluated", outcome="textless_batch")
        if next_scanned <= state.scanned_cursor:
            occurrence_trace("safe_skip_result", outcome="no_cursor_advance")
            return _finish_checkpoint(True, "", _result(
                processed=0,
                scanned=state.scanned_cursor,
                summarized=state.summarized_cursor,
                has_more=batch.has_more,
                revision=state.revision,
                version=state.version,
            ), outcome="scan_only_no_advance")
        occurrence_trace(
            "scan_advance_started", expected_version=state.version,
            scanned_delta=next_scanned - state.scanned_cursor,
            summary_unchanged=True,
        )
        try:
            committed = await repo.advance_history_scan(
                owner_id,
                state.peer_id,
                expected_version=state.version,
                scanned_cursor=next_scanned,
            )
        except HistoryStorageUnavailable:
            occurrence_trace(
                "scan_advance_result", outcome="durable_error",
                category="durable_store_unavailable",
            )
            raise
        except Exception as exc:  # noqa: BLE001
            occurrence_trace(
                "scan_advance_result", outcome="durable_error",
                exception=type(exc).__name__, category="durable_store",
            )
            await _record_failure(repo, state, exc, category="persistence")
            return _finish_checkpoint(False, f"Scheduled History checkpoint could not be saved: {_error_text(exc)}", {
                "error": "scheduled_history_commit_failed",
            }, outcome="scan_advance_failed")
        if committed is None:
            occurrence_trace("scan_advance_result", outcome="cas_conflict")
            return _finish_checkpoint(False, "The History checkpoint changed before it could be saved.", {
                "error": "scheduled_history_commit_conflict",
            }, outcome="scan_advance_conflict")
        occurrence_trace(
            "scan_advance_result", outcome="committed",
            old_version=state.version, new_version=committed.version,
            old_revision=state.revision, new_revision=committed.revision,
            scanned_delta=committed.scanned_cursor - state.scanned_cursor,
            summary_unchanged=committed.summary == state.summary,
            summarized_unchanged=committed.summarized_cursor == state.summarized_cursor,
        )
        return _finish_checkpoint(True, "", _result(
            processed=0,
            scanned=committed.scanned_cursor,
            summarized=committed.summarized_cursor,
            has_more=batch.has_more,
            revision=committed.revision,
            version=committed.version,
        ), outcome="scan_only_advanced")

    occurrence_trace(
        "ai_summarization_started", messages=len(batch.messages),
        text_messages=len(text_messages), previous_summary_bytes=len(state.summary.encode("utf-8")),
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
        occurrence_trace(
            "ai_summarization_result", outcome="storage_unavailable",
            category="durable_store_unavailable",
        )
        raise
    except Exception as exc:  # noqa: BLE001
        occurrence_trace(
            "ai_summarization_result", outcome="failed",
            exception=type(exc).__name__, category="ai_exception",
        )
        await _record_failure(repo, state, exc, category="ai_provider")
        return _finish_checkpoint(False, f"Scheduled History summarization failed: {_error_text(exc)}", {
            "error": "scheduled_history_ai_failed",
        }, outcome="ai_failed")
    occurrence_trace(
        "ai_summarization_result",
        outcome="completed" if ok else "provider_failed",
        error_code=(ai_data or {}).get("error", "-"),
        chunks=(ai_data or {}).get("chunks", "-"),
        text_messages=(ai_data or {}).get("text_messages", "-"),
        candidates_failed=(ai_data or {}).get("candidates_failed", "-"),
        failure_categories=(ai_data or {}).get("failure_categories", "-"),
    )
    if not ok:
        await _record_failure(repo, state, RuntimeError(summary), category="ai_provider")
        return _finish_checkpoint(False, summary, {**ai_data, "error": "scheduled_history_ai_failed"}, outcome="ai_failed")

    summary = str(summary).strip()
    if not summary:
        occurrence_trace("output_validation", outcome="empty")
        error = RuntimeError("the AI provider returned an empty summary")
        await _record_failure(repo, state, error, category="ai_output")
        return _finish_checkpoint(False, "The AI provider returned an empty summary.", {
            "error": "scheduled_history_ai_failed",
        }, outcome="ai_output_empty")
    summary_bytes = len(summary.encode("utf-8"))
    if summary_bytes > 32768:
        occurrence_trace(
            "output_validation", outcome="oversized", summary_bytes=summary_bytes,
            limit_bytes=32768,
        )
        error = RuntimeError("the cumulative summary exceeds the durable 32 KiB limit")
        await _record_failure(repo, state, error, category="ai_output")
        return _finish_checkpoint(False, "The cumulative summary exceeds the durable 32 KiB limit.", {
            "error": "scheduled_history_summary_too_large",
        }, outcome="ai_output_too_large")
    occurrence_trace(
        "output_validation", outcome="accepted", summary_bytes=summary_bytes,
    )

    next_summarized = max(
        state.summarized_cursor,
        max(message.message_id for message in text_messages),
    )
    if next_summarized > next_scanned:
        next_summarized = next_scanned
    occurrence_trace(
        "commit_started", write_kind="summary",
        expected_version=state.version, old_revision=state.revision,
        summary_bytes=summary_bytes,
        scanned_delta=next_scanned - state.scanned_cursor,
        summarized_delta=next_summarized - state.summarized_cursor,
    )
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
        occurrence_trace(
            "commit_result", write_kind="summary", outcome="durable_error",
            category="durable_store_unavailable",
        )
        raise
    except Exception as exc:  # noqa: BLE001
        occurrence_trace(
            "commit_result", write_kind="summary", outcome="durable_error",
            exception=type(exc).__name__, category="durable_store",
        )
        await _record_failure(repo, state, exc, category="persistence")
        return _finish_checkpoint(False, f"Scheduled History checkpoint could not be saved: {_error_text(exc)}", {
            "error": "scheduled_history_commit_failed",
        }, outcome="summary_commit_failed")
    if committed is None:
        occurrence_trace(
            "commit_result", write_kind="summary", outcome="cas_conflict",
            expected_version=state.version, old_revision=state.revision,
        )
        return _finish_checkpoint(False, "The History checkpoint changed before it could be saved.", {
            "error": "scheduled_history_commit_conflict",
        }, outcome="summary_commit_conflict")
    occurrence_trace(
        "commit_result", write_kind="summary", outcome="committed",
        old_version=state.version, new_version=committed.version,
        old_revision=state.revision, new_revision=committed.revision,
        scanned_delta=committed.scanned_cursor - state.scanned_cursor,
        summarized_delta=committed.summarized_cursor - state.summarized_cursor,
        has_more=batch.has_more,
    )

    message = summary
    if batch.has_more:
        message = f"{message}\n\nMore messages are pending; the next run will continue."
    return _finish_checkpoint(True, message, _result(
        processed=len(batch.messages),
        scanned=committed.scanned_cursor,
        summarized=committed.summarized_cursor,
        has_more=batch.has_more,
        revision=committed.revision,
        version=committed.version,
    ), outcome="summary_commit")
