# Implementation Report — Automatic Durable Retry Integration

## Stage
- **Completed stage:** Automatic execution-failure → durable retry integration
- **Previous stage:** Stage 16 — Runtime Persisted-Outcome Notification Transport
- **Next stage:** No source-justified numbered stage is established.

## Objective and scope
The source-backed gap was that `TaskExecutionCoordinator.handle_failure()` contained the existing retry classifier and persistence logic, but the normal `execute()` failure paths called `_fail()` directly. This change connects normal tool failures to that existing retry lifecycle without adding a scheduler, worker, retry implementation, table, column, or notification system.

## Exact files changed
- `backend/ai/task_execution.py`
- `backend/ai/retry.py`
- `tests/test_stage17.py`
- `IMPLEMENTATION_REPORT.md`

The unrelated pre-existing modification in `tests/test_stage13.py` was preserved and was not committed.

## Implementation details
- Exceptions raised by `ToolExecutor.execute_calls()` now flow through `handle_failure()`.
- Unsuccessful `ToolExecutionResult` values also flow through `handle_failure()` instead of directly becoming terminal failure.
- Timeout-shaped tool failures are normalized to the existing `TimeoutError` retry classification; `retry.py` also recognizes serialized `TimeoutError` text.
- Retryable failures within the existing maximum attempt policy persist `retry_pending`, a deterministic bounded `retry_at`, incremented durable `attempt`, and bounded error metadata through `TaskRepository.transition_occurrence()`.
- Exhausted retryable failures, permanent failures, and unknown failures continue through `_fail()` and persist terminal `failed`.
- `asyncio.CancelledError` remains propagated and is never converted to retry state.
- The scheduler remains the sole polling loop and continues to discover already-persisted due `retry_pending` occurrences through its existing repository API.

## Retry lifecycle now supported
```text
ToolExecutor failure result/exception
  → TaskExecutionCoordinator.execute()
  → existing classify_failure()
  → existing can_retry()/retry_delay()
  → TaskRepository.transition_occurrence()
  → durable retry_pending + retry_at + attempt increment
  → existing TaskScheduler retry pickup
  → same TaskExecutionCoordinator / ToolExecutor boundary
  → persisted terminal outcome
  → existing persisted-outcome notification path
```

Attempt semantics remain bounded by `MAX_ATTEMPTS = 3`: an occurrence at attempt 1 advances to attempt 2 for its retry; attempt 2 advances to attempt 3; attempt 3 becomes terminal `failed` without another retry. No exactly-once guarantee is claimed; external side effects remain at-least-once.

## Failure behavior
- **Retryable:** persists `retry_pending`, `retry_at`, next attempt, and bounded metadata when another attempt is allowed.
- **Retry exhaustion:** persists terminal `failed`.
- **Permanent/unknown:** persists terminal `failed` immediately using the existing policy.
- **Cancellation:** propagates cancellation and does not mutate the occurrence into retry state.
- **Owner mismatch/non-running occurrence:** existing fail-closed checks remain unchanged.

## Architecture and security preserved
- `TaskExecutionCoordinator` remains the execution and failure authority.
- `TaskScheduler` remains coordination-only and unchanged as a single polling loop.
- `TaskRepository` remains the durable state/CAS boundary.
- `ToolRegistry` and `ToolExecutor` remain the only registered action execution boundary.
- `TaskOutcomeNotifier` and `TaskNotificationService` remain the persisted-outcome notification boundaries.
- Owner identity continues to come from runtime context and repository operations remain owner-scoped.
- No arbitrary Telegram RPC, SQL/RPC, shell, provider bypass, or persisted-code execution was introduced.
- `RuntimeSupervisor` and `profile.scheduler` were not changed.

## Database/schema status
**NONE.** No database tables, columns, indexes, migrations, or SQL were changed. Existing `ai_tasks` and `ai_task_occurrences` fields (`attempt`, `retry_at`, `error_metadata`, and `status`) were sufficient. No live Supabase state was modified or verified.

## Tests and validation actually executed
- `python3 -m pytest tests/test_stage17.py tests/test_task_execution.py tests/test_task_scheduler.py tests/test_retry.py -q --no-header` — **17 passed**.
- `python3 -m pytest tests/ -q --no-header` — **1155 passed, 23 skipped, 1 warning**.
- `python3 -m compileall -q backend tests` — passed.
- `git diff --check` — passed before report update.

Focused coverage proves normal `TaskExecutionCoordinator.execute()` reaches retry handling for both tool result failures and raised executor exceptions, durable attempt advancement, `retry_at`, scheduler pickup, second-attempt execution, exhaustion, permanent/unknown failures, cancellation propagation, owner isolation, and no duplicate retry execution.

Live Telegram and live Supabase behavior were not verified.

## Delivery
- **Implementation commit:** `3c1a3c979785600aed94f59cf447ef546799dc36` (`fix: wire task execution failures into durable retries`)
- **Push:** succeeded to `origin/main`.
- **Remote HEAD:** `3c1a3c979785600aed94f59cf447ef546799dc36` (verified equal to local HEAD).
- **Final working tree:** the unrelated pre-existing `tests/test_stage13.py` modification remains intentionally uncommitted.
