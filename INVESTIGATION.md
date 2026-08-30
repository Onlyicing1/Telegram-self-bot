# INVESTIGATION

## INVESTIGATION METADATA

| Field | Value |
|---|---|
| Repository | `Onlyicing1/Telegram-self-bot` |
| Branch | `main` |
| Inspected HEAD | `62e7c2589a3d328fbf39e8ba522d205c6b84bfde` |
| Date | 2026-08-30 |
| Investigation type | Source-only current-state investigation |
| Current completed stage | Stage 16 — Runtime Persisted-Outcome Notification Transport |
| Implementation performed | NO |

The working tree already contained unrelated changes to `tests/test_stage13.py`; that file was not modified by this investigation. Only this document was rewritten.

## 1. EXECUTIVE SUMMARY

The current source contains a durable, owner-scoped task model with structured creation, natural-language interpretation, Telegram creation and management commands, deterministic schedules, durable occurrence coordination, scheduled execution through `TaskExecutionCoordinator`, persisted-outcome notification verification, and runtime construction through `RuntimeSupervisor`.

The source does **not** prove that every intended retry path is operationally complete: `TaskExecutionCoordinator.handle_failure()` implements retry classification and persistence, but no production caller was found for that method, and the normal `execute()` exception path calls `_fail()` rather than `handle_failure()`. The scheduler can re-execute already-persisted due `retry_pending` occurrences. Therefore retry re-execution is implemented, while automatic conversion of execution failures into retry-pending state is only partial.

Telegram task creation and management are available through `.task`; no task-specific Glass panels or dashboard task APIs were found. These are optional interfaces unless a project requirement outside the inspected source makes them mandatory. The current source does not establish a numbered future stage. The smallest source-justified remaining implementation boundary, if core retry completeness is required, is an execution-to-retry integration stage; its exact future stage number is not established by the repository roadmap.

## 2. STAGE HISTORY

The implementation reports and current source support the following history. Earlier stages are summarized only where current source provides corresponding artifacts; implementation reports remain historical evidence, not the authority for current behavior.

| Stage | Current source-verified result |
|---|---|
| Stage 7 — Retry Classification, Backoff, and Operational Task Management | `backend/ai/retry.py` defines failure classes, retry decisions, bounded exponential delay, and `can_retry`; task repository and management boundaries provide operational state transitions. |
| Stage 8 — Task Management Interfaces and Notifications | `backend/ai/task_management.py`, `task_management_interface.py`, `notifications.py`, and `task_notifications.py` exist as service/presentation boundaries. |
| Stage 9 — User-Facing Task Creation and Scheduler-to-Notification Wiring | Historical milestone; current handler and runtime wiring provide the relevant creation and notification paths, but current source does not independently establish every historical claim. |
| Stage 10 — User-Facing Task Creation and Management Exposure | Current `backend/bot/handlers/tasks.py` exposes creation and management commands. |
| Stage 11 — Structured Task Candidate Contract | `TaskCandidate` validates bounded, owner-agnostic structured data. |
| Stage 12 — Natural-Language Task Interpretation Adapter | `TaskInterpreter` calls `ProviderManager.chat()` with an explicit schema and parses only validated JSON. |
| Stage 13 — Telegram Task Creation Interaction Boundary | `tasks.register()` handles `.task <request>` and delegates interpretation and creation. |
| Stage 14 — Telegram Task Management Interaction Boundary | `tasks._handle_management()` exposes list, inspect, pause, resume, complete, fail, expire, and delete. |
| Stage 15 — Durable Scheduled Execution and Retry Re-execution | `TaskScheduler._execute_claimed()` delegates running occurrences to `TaskExecutionCoordinator`; `_run_due_retries()` discovers and reclaims due persisted retries. |
| Stage 16 — Runtime Persisted-Outcome Notification Transport | `TaskScheduler._notify_outcome()` calls `TaskOutcomeNotifier` after execution; `RuntimeSupervisor._start_task_scheduler()` constructs the notification stack with a rebuild-safe sender. |

## 3. CURRENT ARCHITECTURE

The verified current path is:

```text
Telegram `.task <request>`
  → backend.bot.handlers.tasks.task_handler
  → TaskInterpreter.interpret
  → TaskCandidate.from_untrusted / parse_candidate_output
  → TaskCreationService.create
  → TaskRepository.create_task
  → ai_tasks
  → RuntimeSupervisor._start_task_scheduler
  → TaskScheduler.run / run_once
  → TaskRepository.list_due_tasks
  → create_occurrence / claim_occurrence
  → TaskExecutionCoordinator.execute
  → ToolExecutor.execute_calls
  → registered Tool instances from ToolRegistry
  → TaskRepository.transition_occurrence
  → TaskOutcomeNotifier.notify_persisted
  → TaskNotificationService.send
  → TelegramAPI.send_message
```

Boundaries are separated in source:

- Interpretation: `backend/ai/task_interpreter.py::TaskInterpreter.interpret`.
- Candidate validation: `backend/ai/task_candidate.py::TaskCandidate.from_untrusted`.
- Creation: `backend/ai/task_creation.py::TaskCreationService.create`.
- Persistence and state transitions: `backend/ai/database/task_repository.py`.
- Scheduling and occurrence coordination: `backend/ai/task_scheduler.py::TaskScheduler`.
- Action execution: `backend/ai/task_execution.py::TaskExecutionCoordinator.execute`.
- Registered action dispatch: `backend/ai/tools/registry.py` and `backend/ai/tools/executor.py`.
- Notification verification: `backend/ai/task_notifications.py::TaskOutcomeNotifier.notify_persisted`.
- Notification delivery: `backend/ai/notifications.py::TaskNotificationService.send`.
- Runtime lifecycle: `backend/runtime/supervisor.py::RuntimeSupervisor`.

## 4. TASK DATA MODEL

The migration `supabase/migrations/20260829000001_create_ai_tasks.sql` defines exactly the two durable task tables:

- `ai_tasks`: owner, label, lifecycle status, version, schedule type/payload, timezone, next run, bounded actions, notification destination, timestamps, and terminal timestamp.
- `ai_task_occurrences`: task/owner identity, deterministic occurrence key, definition version, immutable action snapshot, scheduled time, attempt, occurrence status, claim/start/finish timestamps, retry time, bounded error metadata, bounded result metadata, and timestamps.

`TaskRepository` defines the persistence contract. `InMemoryTaskRepository` provides a fallback; `SupabaseTaskRepository` performs owner-filtered operations and falls back on external failures. Both enforce bounded payloads and state validation.

Task transitions use expected versions through `update_task`, `advance_next_run`, and `transition_task`. Occurrence transitions enforce the current-status transition map, immutable occurrence fields, bounded metadata, and `retry_pending` requiring `retry_at`. Occurrence claiming is owner-scoped and only accepts `claimed`, `retry_pending`, or `interrupted` states.

The schema has owner/status indexes, lifecycle checks, JSON size checks, a unique `(task_id, occurrence_key)` constraint, and RLS. The source documents backend service-role access. Live Supabase state was not verified.

## 5. TASK CREATION FLOW

`backend/bot/handlers/tasks.py::task_handler` is registered by `backend/bot/router.py::register_all` for outgoing Telethon messages. It first applies `is_owner(event, owner_id)`, recognizes `.task`, and routes recognized management subcommands separately.

For creation, it:

1. Edits the source message with an interpreting/progress response.
2. Gets the configured provider through `_provider_manager()` and `get_engine()`.
3. Calls `TaskInterpreter.interpret()` inside bounded waits.
4. Receives a `TaskCandidate`, not arbitrary provider output.
5. Creates the task through `TaskCreationService(get_repository_manager().task, owner_id)`.
6. Uses `datetime.now(timezone.utc)` as the reference and edits the same message with success or bounded failure feedback.

`TaskCandidate` rejects unsupported fields, invalid schedules, invalid timezones, malformed actions, and oversized data. It does not accept owner identity. `TaskCreationService` supplies the authoritative owner ID and persists through the repository.

Natural-language management is not implemented: the interpreter is used only in the creation branch, and management uses deterministic command syntax.

## 6. TASK MANAGEMENT FLOW

`backend/bot/handlers/tasks.py::_handle_management` delegates to `TaskManagementService` and presentation helpers:

- `.task list` → `list_text(service)` → `TaskManagementService.list_tasks()`.
- `.task inspect <id>` → `inspect_text(service, task_id)` → `TaskManagementService.inspect()` with a bounded occurrence list.
- `.task pause|resume|complete|fail|expire|delete <id> <version>` → corresponding service method with the user-provided expected version.

The handler does not access Supabase directly, manipulate JSON, execute actions, or bypass CAS. It edits in place and returns a bounded error if parsing, ownership, version, or persistence fails. Non-owners return silently through `is_owner`.

Task listing, inspection, lifecycle mutation, and deletion are user-accessible through Telegram. Editing task definitions, rescheduling, notification-destination management, natural-language management, and explicit occurrence-management commands are not exposed by the current handler.

## 7. TRIGGER / SCHEDULE SUPPORT

The only schedule types established by source are `once`, `interval`, `daily`, and `weekly`. `SUPPORTED_TYPES` in `backend/ai/scheduling.py`, `SCHEDULE_TYPES` in `backend/ai/database/task_repository.py`, `TaskCandidate` schema validation, and the migration all agree on this set.

| Type | Required fields and semantics | Runtime status |
|---|---|---|
| `once` | `schedule.at` is a naive local ISO datetime and `schedule.timezone` is an IANA timezone. It resolves once to UTC. `TaskScheduler` advances the task to `next_run_at=None`. | Implemented in domain, persisted, schedulable, executable, and creatable through the interpreter/candidate path. No event trigger exists. |
| `interval` | `schedule.seconds` must be positive. It uses elapsed UTC intervals and requires a previous scheduled occurrence for calculation. | Implemented in domain, persisted, schedulable, executable, and creatable through the candidate path. |
| `daily` | `schedule.hour`, optional minute/second, and IANA `schedule.timezone`. Local time is normalized around nonexistent DST times. | Implemented in domain, persisted, schedulable, executable, and creatable through the candidate path. |
| `weekly` | `schedule.weekday` 0–6, hour/minute/second, and IANA timezone. | Implemented in domain, persisted, schedulable, executable, and creatable through the candidate path. |

`catch_up_occurrence()` provides bounded recurring catch-up for interval/daily/weekly scheduling. `occurrence_key(task_id, scheduled_for)` is deterministic and UTC-normalized. Retry is an occurrence-state event, not a fifth schedule type.

Cron, event/message, webhook, conditional, manual, and provider-triggered task schedules are **NOT PROVEN BY CURRENT SOURCE** and should not be counted as supported.

## 8. SCHEDULER AND OCCURRENCE EXECUTION

`TaskScheduler` is the single task loop. `start()` recovers interrupted occurrences and creates one `asyncio` task for `run()`. `run()` calls `run_once()` and waits for `WAKE_INTERVAL_SECONDS`.

`run_once()` first calls `_run_due_retries()`, then queries owner-scoped due active tasks. For each due task it parses the schedule, computes the scheduled and following occurrence, creates the occurrence idempotently, claims it through the repository, and calls `_execute_claimed()`.

`_execute_claimed()` refuses to proceed without an execution coordinator, calls `claim_occurrence()`, requires the returned status to be `running`, and only then invokes `coordinator.execute(claimed)`. After execution it invokes `_notify_outcome()` with the reported status. The scheduler itself does not execute persisted action JSON.

For recurring tasks, `advance_next_run()` uses the task version read before execution. This provides CAS protection against stale advancement. A once task is advanced to `None`. Duplicate wake/claim attempts are bounded by repository uniqueness and claim-state checks; the source provides at-least-once coordination, not an exactly-once external side-effect guarantee.

`recover()` marks persisted `claimed` and `running` occurrences as `interrupted` at scheduler startup. The occurrence transition map permits interrupted occurrences to become `retry_pending` or `failed`, and claiming permits interrupted occurrences. Actual interrupted-occurrence execution policy beyond this is not fully established by the source.

## 9. RETRY MODEL

`backend/ai/retry.py` defines:

- `FailureClass.RETRYABLE`, `PERMANENT`, `CANCELLED`, and `UNKNOWN`.
- Retryable timeout, temporary, and rate-limit-like failures.
- `MAX_ATTEMPTS = 3`.
- Exponential delays starting at 30 seconds, bounded at 15 minutes.
- `can_retry(attempt)` requiring an attempt below the maximum.

`TaskExecutionCoordinator.handle_failure()` calculates `retry_at` from `occurrence.updated_at + retry_delay(occurrence.attempt)` and persists `retry_pending` via `transition_occurrence`. The scheduler then uses `list_due_retry_occurrences()` and `_execute_claimed()` to reclaim due retries.

However, a production caller for `handle_failure()` was not found. In `TaskExecutionCoordinator.execute()`, exceptions from `ToolExecutor.execute_calls()` go directly to `_fail()`, which persists `failed`; the normal path does not classify those exceptions through `handle_failure()`. Also, no source path was found that increments `occurrence.attempt` before a retry. Consequently:

- Retry policy: implemented.
- Retry persistence API: implemented.
- Scheduler pickup and retry re-execution: implemented for already-persisted `retry_pending` rows.
- Automatic execution-failure → retry-pending conversion: PARTIAL / not fully wired.
- Retry exhaustion behavior: policy exists, but the complete production lifecycle is NOT PROVEN BY CURRENT SOURCE.

This is the strongest source-backed core gap found in the current tree.

## 10. NOTIFICATION MODEL

`TaskOutcomeNotifier.notify_persisted()` first reads the occurrence through the owner-scoped repository and compares its actual persisted status with the requested status. It sends nothing if the occurrence is missing or the persisted state differs.

`TaskNotificationService.send()` rejects owner mismatches and unsupported kinds, truncates messages to 1024 characters, and bounds delivery with a 10-second `asyncio.wait_for`. Sender errors return `False`; cancellation is re-raised. Notification delivery does not mutate task or occurrence state and does not execute actions.

`TaskScheduler._notify_outcome()` supports `succeeded`, `failed`, `retry_pending`, and `cancelled`, re-raises cancellation, and isolates other notifier failures. It is called after `TaskExecutionCoordinator.execute()` returns, not when execution merely starts. `RuntimeSupervisor._start_task_scheduler()` constructs the service and notifier and uses a sender closure that resolves `self.client` at call time before calling `TelegramAPI.send_message(owner, message)`, avoiding a stale client after rebuild.

The source prevents notification merely from polling a retry row: `_run_due_retries()` only executes due rows, and notification occurs after the coordinator reports a persisted outcome. There is no notification table or notification worker. Notification duplication across process crashes is not proven to be impossible; the source provides state verification and scheduler-level polling avoidance, not durable notification delivery deduplication.

## 11. SECURITY AND OWNERSHIP

Verified boundaries:

- `RuntimeSupervisor` supplies the authoritative owner ID and owns lifecycle startup/shutdown.
- `TaskCandidate` is owner-agnostic and explicitly excludes owner identity from the interpreter schema.
- `TaskCreationService` stores the constructor-provided owner ID; candidate data cannot override it.
- `TaskRepository` filters task and occurrence reads/writes by owner and validates owner fields.
- `TaskManagementService` stores an owner ID and delegates all operations owner-scoped.
- `TaskScheduler` receives one owner ID and uses it for every repository operation.
- `TaskExecutionCoordinator` rejects occurrences whose owner differs from its authoritative owner and passes that owner to `ToolExecutor`.
- `TaskNotificationService` rejects notifications whose owner differs from its configured owner and calls the configured owner in the sender boundary.
- Telegram task handlers call `is_owner` before processing.

No task-specific Glass callback or dashboard authorization path exists, so their task-specific ownership behavior is NOT PROVEN BY CURRENT SOURCE. The general existing Glass architecture has owner checks in its existing dispatch path, but no task callbacks are registered.

No source-backed violation was found for arbitrary shell execution, arbitrary SQL/RPC, arbitrary Telegram RPC, model-supplied owner identity, or direct persisted-JSON execution. Tool actions remain data and are resolved through the registered `ToolRegistry` and `ToolExecutor`.

The source does not establish exactly-once side effects: a process crash around an external tool call and durable transition could permit at-least-once behavior. This is an operational limitation, not evidence of an architecture bypass.

## 12. USER-FACING FEATURE COMPLETENESS

| Capability | Status | Evidence |
|---|---|---|
| Task creation | IMPLEMENTED | `tasks.py::task_handler`, `TaskInterpreter`, `TaskCreationService`. |
| Natural-language interpretation | IMPLEMENTED | `TaskInterpreter.interpret`, bounded provider call, schema and JSON validation. |
| Structured candidate validation | IMPLEMENTED | `TaskCandidate.from_untrusted`. |
| Durable persistence | IMPLEMENTED | `TaskRepository.create_task`, `ai_tasks` migration. |
| Task listing | IMPLEMENTED | `.task list`, `TaskManagementService.list_tasks`, `list_text`. |
| Task inspection | IMPLEMENTED | `.task inspect <id>`, `inspect_text`, bounded occurrences. |
| Pause/resume | IMPLEMENTED | `.task pause/resume <id> <version>`, CAS service methods. |
| Terminal lifecycle operations | IMPLEMENTED | complete/fail/expire/delete methods and handler commands. |
| Natural-language management | NOT IMPLEMENTED | No interpreter branch delegates management intents. |
| Task editing/rescheduling | NOT IMPLEMENTED | No Telegram command or service method exposes definition updates through the handler. |
| Task cancellation | PARTIAL | Occurrence state supports `cancelled`, but no dedicated user-facing occurrence cancellation command was found; task management has terminal statuses but no `cancel` task method. |
| Notification destination management | NOT IMPLEMENTED | Destination is stored at creation but no update/UI boundary was found. |
| Task history/occurrence history | PARTIALLY IMPLEMENTED | `inspect` displays bounded occurrence summaries; no separate history browser/export exists. |
| Retry/error visibility | PARTIALLY IMPLEMENTED | Inspection displays occurrence status/attempt; it does not display bounded error/result metadata. |
| Glass task UI | NOT IMPLEMENTED | No task panels/actions/inputs were found in `backend/helper` or handler registrations. |
| Dashboard task APIs | NOT IMPLEMENTED | `backend/web/app.py` exposes health, saves, settings, logs, AI, and diagnostics routes, but no task routes. |
| Richer management UX | PARTIALLY IMPLEMENTED | Deterministic Telegram command management exists; no Glass/dashboard/editing UX exists. |
| Automatic retry conversion | PARTIALLY IMPLEMENTED | `handle_failure` exists but no production caller was found from current source. |
| Retry re-execution | IMPLEMENTED | `TaskScheduler._run_due_retries` plus `_execute_claimed`. |
| Persisted outcome notification | IMPLEMENTED with operational limitation | `TaskOutcomeNotifier` verifies state before `TaskNotificationService`; duplicate delivery across crash boundaries is not proven impossible. |

## 13. DATABASE / SUPABASE STATUS

Repository source contains one task migration: `supabase/migrations/20260829000001_create_ai_tasks.sql`. It creates `ai_tasks` and `ai_task_occurrences`, indexes task due/status and owner/update access, enables RLS, grants read access, and intentionally leaves backend writes to the service-role path.

The current two-table model stores task definitions and occurrence history/state, including action snapshots, attempt count, retry timestamps, error metadata, and result metadata. The remaining source-backed retry gap can be addressed in application execution wiring using existing columns and transitions; no new table or column is proven necessary.

No migration, SQL, Supabase deployment, or live database state was changed or verified during this investigation.

## 14. TEST / VALIDATION STATUS

Relevant tests present in the current tree include:

- `tests/test_task_repository.py`
- `tests/test_task_scheduler.py`
- `tests/test_task_execution.py`
- `tests/test_task_management.py`
- `tests/test_retry.py`
- `tests/test_stage10.py` through `tests/test_stage16.py`
- `tests/test_stage11_candidate.py`
- `tests/test_stage12_interpreter.py`

`tests/test_stage16.py` was run read-only during this investigation: **13 passed**. The repository history/report records the broader Stage 16 validation as **1149 passed, 23 skipped, 1 warning**, plus compileall and diff checks; that historical result was not treated as proof of live integrations.

Tests cover repository state transitions, scheduling, execution boundaries, retry policy, management delegation, creation/interpreter boundaries, and persisted notification semantics. Stage 16 tests cover persisted success/failure/retry/cancelled notification, missing or mismatched persistence, sender failure/cancellation, owner mismatch, duplicate wake behavior, non-execution by notifications, and lazy runtime client wiring.

The tests are unit/in-memory or mocked boundary tests. Live Telegram, live Supabase, provider API behavior, cross-process crash recovery, and exactly-once external side effects were not verified. The absence of a production caller for `handle_failure()` is source evidence even if existing tests exercise the method directly.

## 15. REMAINING GAPS

### Gap 1 — Automatic execution failure to retry-pending integration

- **Current state:** `retry.py` and `TaskExecutionCoordinator.handle_failure()` implement the retry policy and persistence contract.
- **Missing capability:** The normal `TaskExecutionCoordinator.execute()` exception path calls `_fail()` directly; no production caller for `handle_failure()` was found, and attempt advancement is not shown.
- **Evidence:** `backend/ai/task_execution.py::execute`, `::handle_failure`, `::_fail`; repository `OccurrenceRecord.attempt` and validation in `backend/ai/database/task_repository.py`.
- **Why it matters:** A transient action failure can become terminal `failed` instead of entering the established durable retry lifecycle.
- **Existing boundary to reuse:** `TaskExecutionCoordinator` should remain the owner of classification and occurrence transitions; reuse `retry.py` and repository transitions.
- **Core status:** Required if “automatic retries” means execution failures are classified and retried; otherwise retry pickup is already available for explicitly persisted retry rows.
- **Schema impact:** None proven. Existing `attempt`, `retry_at`, status, and metadata fields are sufficient.
- **Security impact:** Preserve owner and ToolExecutor boundaries; no new execution authority is justified.

### Gap 2 — Optional Glass task interface

- **Current state:** Existing Glass infrastructure exists in `backend/helper`, but no task panel/action/input registrations were found.
- **Missing capability:** Glass task creation/management panels and callbacks.
- **Evidence:** `backend/helper/panels.py`, `panel_registry.py`, `inline_engine.py` contain generic panel dispatch; `backend/bot/handlers/tasks.py` registers only an outgoing message handler and no task panel registrations.
- **Core status:** Optional UX. Telegram commands already provide creation and management, and no inspected architecture explicitly makes Glass task panels mandatory.
- **Schema/runtime impact:** None established.

### Gap 3 — Optional dashboard task API/UI

- **Current state:** `backend/web/app.py` has read-only and settings/AI routes, and `src/` contains dashboard components, but no task routes or task UI were found.
- **Missing capability:** Dashboard read/manage endpoints and corresponding UI.
- **Evidence:** `backend/web/app.py` has no `/api/tasks` or `/api/occurrences` route; source search found no task dashboard component.
- **Core status:** Optional UX unless an external product requirement declares dashboard management mandatory.
- **Security impact:** Any future implementation would need an explicit authenticated owner boundary; the current source does not establish dashboard task authorization.
- **Schema impact:** None necessarily, but not determined until a concrete API contract exists.

No other future gap is counted. Cron, event triggers, arbitrary action types, analytics, new providers, and notification tables are not established requirements by current source.

## 16. CORE VS OPTIONAL

### Core completion checklist

The core feature is functionally complete when all of the following are true:

- A validated owner can create a task from Telegram.
- The task persists through `TaskRepository` into the two-table durable model.
- Supported once/interval/daily/weekly schedules calculate and persist due times.
- The single scheduler creates deterministic occurrences and claims them safely.
- Claimed occurrences execute only through `TaskExecutionCoordinator` → `ToolExecutor` → registered tools.
- Success, failure, retry-pending, and cancellation states are persisted through repository transitions.
- Due persisted retries can be reclaimed and re-executed under owner/state checks.
- Persisted outcomes are verified before bounded Telegram notification delivery.
- The owner can list, inspect, pause, resume, complete, fail, expire, and delete tasks.
- Runtime startup recovery and shutdown are governed by `RuntimeSupervisor`.

The current source satisfies these items except that automatic conversion of execution failures into retry-pending state is not fully proven because `handle_failure()` has no production caller. Thus the hard conclusion is: **core feature is substantially implemented but retry integration remains PARTIAL if automatic retries are required.**

### Optional UX

The following are not required by the current core architecture:

- Glass task panels and inline lifecycle callbacks.
- Dashboard task APIs and task UI.
- Natural-language management.
- Task editing/rescheduling commands.
- Rich history/error browsing beyond bounded inspection.
- Notification destination editing.

## 17. STAGE / ROADMAP CONCLUSION

The repository proves completed implementation stages through Stage 16. It does not define a post-Stage-16 numbered roadmap. Therefore:

- **Completed stages:** Stages 1–16 as the project’s established numbering, with task-system source artifacts and reports specifically verified through Stage 16.
- **Required remaining stages:** One, **if** automatic retry conversion is part of the intended core contract.
- **Optional/future stages:** Glass task UI and dashboard task APIs may be implemented as separate optional UX stages; their numbering is not established by source.
- **Next implementation stage:** No source-justified future stage number/title is currently established. The smallest source-justified boundary would be an execution-to-retry integration stage centered on `TaskExecutionCoordinator.execute()` / `handle_failure()` and existing repository attempt/retry transitions.
- **Expected final required stage:** Not established by source. If the retry boundary is implemented, the current inspected source contains no additional core gap proven by this investigation.

The retry integration is a separate boundary because it changes failure classification and durable attempt progression inside the execution contract; it should not be merged into optional Glass or dashboard work. It must not create another scheduler, retry worker, action executor, database table, or notification transport.

## 18. FINAL VERDICT

The current repository is **not conclusively complete for automatic durable retry semantics**: scheduled execution, persisted retry pickup, and retry re-execution exist, but the normal execution failure path does not demonstrably call the retry policy. All other core paths — Telegram creation, durable persistence, schedule coordination, registered-tool execution, Telegram management, persisted-outcome verification, and notification transport — are implemented in source with unit-level coverage.

A user can create and manage tasks through Telegram, and the runtime can execute supported schedules and notify verified persisted outcomes. Glass panels and dashboard task APIs are absent but are optional interfaces under the inspected architecture, not automatically core blockers. The next action should be a source-justified execution-to-retry integration review/implementation only if the product contract requires transient execution failures to retry automatically. No numbered Stage 17 is established by the current repository.

## 19. INVESTIGATION BOUNDARY

- Production code changed: NO
- Tests changed: NO
- Database changed: NO
- Migration changed: NO
- SQL executed: NO
- Supabase changed: NO
- Telegram behavior changed: NO
- Commit made for implementation: NO
- Push performed: NO
- `INVESTIGATION.md` rewritten: YES
- Implementation performed: NO

## EXACT SOURCES INSPECTED

- `AGENTS.md`
- `README.md`
- `IMPLEMENTATION_REPORT.md`
- `INVESTIGATION.md` (previous contents)
- `DATABASE_ARCHITECTURE.md`
- `backend/ai/task_candidate.py`
- `backend/ai/task_interpreter.py`
- `backend/ai/task_creation.py`
- `backend/ai/task_management.py`
- `backend/ai/task_management_interface.py`
- `backend/ai/scheduling.py`
- `backend/ai/task_scheduler.py`
- `backend/ai/task_execution.py`
- `backend/ai/retry.py`
- `backend/ai/task_notifications.py`
- `backend/ai/notifications.py`
- `backend/ai/database/task_repository.py`
- `backend/ai/database/manager.py`
- `backend/ai/tools/registry.py`
- `backend/ai/tools/executor.py`
- `backend/bot/handlers/tasks.py`
- `backend/bot/handlers/guard.py`
- `backend/bot/handlers/misc.py`
- `backend/bot/router.py`
- `backend/helper/panels.py`
- `backend/helper/panel_registry.py`
- `backend/helper/inline_engine.py`
- `backend/runtime/supervisor.py`
- `backend/profile/scheduler.py`
- `backend/telegram_api/api.py`
- `backend/telegram_api/messages.py`
- `backend/web/app.py`
- `supabase/migrations/20260829000001_create_ai_tasks.sql`
- `src/` dashboard/frontend source
- `tests/test_task_repository.py`
- `tests/test_task_scheduler.py`
- `tests/test_task_execution.py`
- `tests/test_task_management.py`
- `tests/test_retry.py`
- `tests/test_stage10.py`
- `tests/test_stage11_candidate.py`
- `tests/test_stage12_interpreter.py`
- `tests/test_stage13.py`
- `tests/test_stage14.py`
- `tests/test_stage15.py`
- `tests/test_stage16.py`

## INVESTIGATION INTEGRITY

This document is a source-only current-state report. It does not authorize or perform implementation of the retry gap, Glass UI, dashboard APIs, or any other future work.
