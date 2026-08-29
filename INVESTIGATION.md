# INVESTIGATION

## INVESTIGATION METADATA

- Repository: `https://github.com/Onlyicing1/Telegram-self-bot`
- Branch: `main`
- Investigation date: 2026-08-29
- Starting commit: `a80ef8fa619dfcd3ad60f0314201cd1e76f71ff9`
- Phase: investigation only
- Status: No production code, tests, migrations, SQL, Supabase state, or Telegram execution behavior was modified.

## 1. PROBLEM

The requested feature is a durable AI Task / Scheduler system for owner requests such as one-shot times, intervals, daily/weekly schedules, and ordered multi-action tasks. The repository currently has no task-system implementation, no generic scheduler, and no durable task or execution state. This investigation identifies the smallest compatible architecture without implementing it.

## 2. CURRENT ARCHITECTURE

The application is one Python 3.11 asyncio process. The Telegram self-client is constructed by `backend/runtime/supervisor.py::RuntimeSupervisor`, handlers are registered by `backend/bot/router.py::register_all`, and the supervisor owns startup, shutdown, reconnect, rebuild, helper lifecycle, profile resumption, and watchdog recovery.

The AI path is `ai_unified` outgoing-owner activation → `Engine.execute()` → `Dispatcher.dispatch()` → `ProviderManager.chat()` → immutable `EngineResult` → `_execute_ai()` result handling → existing tools through `ToolExecutor` → response delivery. `Engine` is the public AI entry point; `Dispatcher` owns orchestration; `ProviderManager` owns provider routing/fallback; `ToolExecutor` is the sole component that calls `Tool.execute()`.

The existing task-like state is not a task system. Conversation sessions, pending callbacks, panel timers, and profile state are separate concerns. Telegram execution remains behind the injected `TelegramAPI`/self-client and service/tool layers.

## 3. CURRENT SCHEDULER / RUNTIME

### Confirmed existing infrastructure

- `backend/profile/scheduler.py` contains one feature-specific singleton scheduler. It stores `_task`, `_client`, `_updaters`, and `_active_engines` in process memory.
- It waits for the next local minute boundary using `asyncio.sleep`, calls registered Bio/Username updater functions, merges profile fields, and sends one `UpdateProfileRequest`.
- `start_cron()` refuses duplicate active tasks; `stop_cron()` cancels and awaits with a ten-second timeout; `_supervised_cron()` restarts the loop after unexpected exceptions with backoff.
- `set_engine_active()` and `stop_if_idle()` coordinate Bio and Username without stopping one while the other remains active.
- `RuntimeSupervisor.start()` loads settings, builds/registers the client, starts helper services, resumes Bio/Username state, then starts heartbeat, keepalive, failsafe, diagnostics, web server, and the immortal self-client loop.
- `RuntimeSupervisor.stop()` cancels profile engines and all owned runtime workers. Rebuild/full recovery cancels and recreates runtime tasks and calls `_resume_bio_cron()` / `_resume_username_cron()`.
- `backend/runtime/task_guard.py` provides supervised task creation; `asyncio.Lock` is already used for supervisor recovery and save-code generation. `operation_watchdog.guarded_await` bounds DB operations.
- Heartbeat, keepalive, failsafe, and diagnostics are health/recovery loops, not business schedulers. The supervisor's `_cancel_orphan_tasks()` explicitly protects the profile scheduler and runtime worker names.

### Reusability conclusion

`profile.scheduler` is not reusable as the task scheduler: its callback signature, minute-boundary timing, shared `UpdateProfileRequest`, active-engine semantics, and client-specific behavior are profile-only. A Task System must use one new singleton task-supervisor loop, registered and started by `RuntimeSupervisor`, rather than create per-task timers or a second ad-hoc scheduler. It should use `guarded_create_task`/`immortal_create_task`, have a unique protected task name, and stop/restart through supervisor lifecycle hooks.

## 4. CURRENT DATABASE / PERSISTENCE

### Existing persistent state

Source and the existing database discovery material identify `saved_items`, `bio_state`, `username_state`, `bot_logs`, `panel_settings`, `bot_settings`, `ai_config`, `ai_sessions`, `ai_messages`, `ai_memories`, and `ai_tool_history`. AI usage/provider-stat repository interfaces also exist, while some corresponding tables are not present in the checked migration inventory. `ghost_chats` is migration-only/orphaned from the application perspective. No task or scheduler table is referenced by current code.

`backend/db/client.py` is the core Supabase singleton with service-role writes, bounded `asyncio.to_thread` calls, and in-memory fallback. `backend/ai/persistence.py` provides similar thin persistence helpers for AI sessions/messages/memories/tool history. `RepositoryManager` centralizes AI repositories; session/message/preferences/tool-history repositories are currently in-memory implementations, while memory/usage/provider-stat repositories can be Supabase-backed depending on availability. Persistence modules store data; they do not execute Telegram or AI actions.

The migration files are not modified or executed in this phase. Existing RLS and owner-scoping conventions require any future task storage to include owner scoping and policies consistent with the repository's service-role write/read architecture. Exact policy SQL must be designed and reviewed in the later schema phase, not guessed here.

### Minimum durable state required

A restart-safe scheduler cannot use only RAM. The minimum authoritative state is:

1. **Task definition/schedule record — DATABASE_REQUIRED.** It must survive process restart and contain owner scope, immutable or versioned task identity, enabled/deleted status, normalized schedule, timezone, next-run timestamp, action plan, and creation/update timestamps.
2. **Execution attempt record or equivalent durable claim/result state — DATABASE_REQUIRED for duplicate/missed-run handling.** A task definition alone cannot record whether a due occurrence was claimed, running, succeeded, failed, or retried. The implementation must durably identify each occurrence/attempt and its outcome before the scheduler advances it.

A separate action table is **DATABASE_NOT_REQUIRED for the initial safe scope** if ordered actions are stored as validated JSON in the task definition and bounded by a schema/version. A separate execution-step table is **DATABASE_USEFUL but not required initially**; it becomes required if per-step audit, resume-from-step, or large workflows are introduced. Do not add it just in case.

Proposed task-definition fields (implementation proposal, not existing schema): `id`, `owner_id`, `name/label`, `enabled`, `schedule_type`, normalized schedule payload, `timezone`, `next_run_at`, `last_scheduled_at`, `created_at`, `updated_at`, `deleted_at`/explicit inactive state, and versioned ordered `actions` JSON. Proposed execution fields: `id`, `task_id`, `owner_id`, deterministic occurrence key, `scheduled_for`, `claimed_at`, `started_at`, `finished_at`, `status`, `attempt`, bounded error/result metadata, and timestamps. Constraints must include owner/task foreign-key consistency, valid status/schedule enums, unique `(task_id, occurrence_key)` to prevent duplicate occurrence records, and indexes for enabled due tasks and task-owned execution history. Retention should be bounded and explicit in a later schema decision; task definitions are authoritative, executions are history.

Those fields are justified only by restart, duplicate prevention, retry, status, and owner-audit requirements. The database must not execute tools, Telegram calls, AI providers, or scheduling logic.

## 5. NATURAL LANGUAGE → TASK FLOW

Natural-language requests currently enter through `backend/bot/handlers/ai_unified.py` and become `AIRequest` values sent to `Engine.execute()`. The AI can reason through provider tool calls, but existing tool calls are executed immediately by `ToolExecutor`; there is no scheduled-task tool or task proposal contract.

The safe future contract is:

```text
owner outgoing message
  → ai_unified / Engine / Dispatcher / ProviderManager
  → structured task proposal tool call
  → deterministic task validator
  → owner-scoped persistence transaction
  → singleton TaskScheduler loads due records
  → deterministic action validator/dispatcher
  → ToolExecutor and existing allowed tools
  → TelegramAPI/self-client
  → durable execution result + owner notification through an approved delivery path
```

Task parsing belongs in AI reasoning only as structured proposal construction. Schedule normalization, timezone validation, action schema validation, limits, and authorization belong in deterministic application code before persistence. The AI must never instantiate timers or directly execute a proposed action.

The existing ToolRegistry/ToolExecutor can represent execution of existing safe tool calls: it already supplies schemas, permission levels, max five calls per turn, sequential execution, timeout handling, history, and confirmation handling. A scheduled executor should invoke the same validated tool contract with an explicit task execution context, not call providers or Telegram directly. Whether ToolExecutor needs a narrowly scoped batch/context API must be proven in implementation; do not bypass it.

## 6. TASK MODEL

Keep the concepts separate:

- **Task definition:** owner-owned desired action plan plus lifecycle status.
- **Schedule:** normalized one-shot or recurrence rule and timezone.
- **Task execution:** one concrete scheduled occurrence/attempt.
- **Execution result:** success/failure/cancelled/retry outcome and safe metadata.
- **Runtime scheduler state:** in-memory cache/next wakeup/task handles only; never authoritative.

Initial task definitions should use a versioned, bounded ordered action list. Each action must name an existing allowlisted tool and contain validated JSON arguments. The initial model should not persist natural-language instructions as executable authority; retain optional display text only as non-authoritative context. One-shot tasks become inactive after a terminal successful occurrence (and define behavior for terminal failure). Recurrence remains on the definition with `next_run_at` advanced deterministically.

Owner identity is the authenticated outgoing owner's numeric `owner_id`, copied into every task and execution record. Chat/message destination must be explicit in validated task context; never infer arbitrary destinations at execution time.

## 7. SCHEDULING MODEL

The scheduler should be one process-wide singleton loop owned by the RuntimeSupervisor. It should load enabled tasks from durable storage on startup/recovery, select due work using indexed `next_run_at`, and sleep until the nearest due time with a bounded wakeup so newly-created/disabled tasks are noticed.

Normalized schedule types should initially cover:

- one-shot: an absolute timezone-aware local date/time normalized to UTC plus original timezone;
- interval: a positive bounded duration with a defined anchor and next-run policy;
- daily: local wall-clock time plus timezone;
- weekly: validated weekday plus local wall-clock time plus timezone.

Timezone must come from the explicit task request/session or configured owner timezone (`cfg["TZ"]`), with one documented precedence rule. Store an IANA timezone identifier, not only an offset. Daylight-saving transitions require a documented policy for nonexistent/ambiguous local times; do not silently guess.

The implementation must define: whether missed one-shot tasks run immediately or expire; whether recurring missed occurrences coalesce, skip, or replay; maximum catch-up; whether interval recurrence is anchored to scheduled time or completion; and how DST affects next-run. A conservative initial policy is one bounded catch-up occurrence for an enabled task, then advance from the scheduled occurrence, but this is a decision blocker until explicitly accepted.

Due-task claiming must be atomic and occurrence-keyed. Compute and persist the next occurrence only after a successful claim/transition, with compare-and-set conditions on task version/status. Do not use `asyncio.sleep` per task or process-local timer handles as the source of truth.

## 8. EXECUTION MODEL

The scheduler claims one occurrence, creates/updates a durable execution attempt, validates the task is still enabled and owner-scoped, and executes actions sequentially through the existing ToolRegistry/ToolExecutor. The self-client injected into `ToolContext` remains the only Telegram authority.

Initial execution policy should be sequential and bounded. A task may be capped by action count, argument size, total runtime, and recurrence frequency. Existing ToolExecutor limits (`MAX_TOOLS_PER_TURN = 5`, default ten-second timeout, confirmation levels) must remain applicable. Scheduled tasks must not gain a bypass for dangerous/admin tools: confirmation-required actions need a defined owner confirmation workflow or are excluded from the initial scheduled scope. Never execute arbitrary shell, SQL, RPC, provider, or natural-language commands.

Persist attempt status transitions such as `claimed/running/succeeded/failed/cancelled/retry_pending`, safe error classification, attempt number, and timestamps. Do not persist secrets or full sensitive provider output unnecessarily. Notifications are a separate approved owner-only delivery action and must not be mistaken for task success.

## 9. RESTART / RECOVERY

Current RuntimeSupervisor rebuilds clients and resumes only Bio/Username; no task state is loaded because no task system exists. A future scheduler must be started after the self-client/tool context is ready and stopped before client teardown. Recovery must reload durable tasks rather than retain stale client references.

Required guarantees and limits:

- Restart before execution: durable enabled task and `next_run_at` allow reload and deterministic missed-run policy.
- Restart while executing: an attempt left `running` must be reconciled on startup as interrupted/unknown, then either retried under a bounded policy or marked failed; it cannot be assumed successful.
- Crash after Telegram execution before success recording: duplicate execution is possible in a single-instance crash window unless an idempotency mechanism exists. The initial design must state at-least-once semantics or add action-level idempotency keys where supported; exactly-once Telegram side effects cannot be claimed.
- Crash after recording success before Telegram execution: ordering must never record success before the actual ToolExecutor result; otherwise false success is possible. Use `running` until the action result is known.
- Offline past due time: apply the explicitly selected bounded catch-up/coalescing policy; never run an unbounded backlog.
- Multiple due tasks: process deterministically (scheduled time, task ID), with a global concurrency bound and no duplicate scheduler loop.

The current architecture is single-instance, so distributed leader election is not required unless deployment changes. Atomic database claiming is still required to protect against overlapping local loops/recovery races and future accidental multi-instance operation.

## 10. CONCURRENCY / LOCKING

Existing primitives are local `asyncio.Lock` for supervisor recovery and save-code generation, task guards for supervised loops, and sequential ToolExecutor calls. They do not provide task occurrence claiming.

A future TaskScheduler needs one scheduler singleton guard, one bounded execution semaphore (initially likely one), and per-task or occurrence claim checks. Disable/delete must use an atomic status/version condition so a task already claimed has a defined cancellation behavior. A recurring task must not overlap itself unless an explicit policy permits it; initial safe scope should skip or defer a due occurrence while one execution is running. Database uniqueness on `(task_id, occurrence_key)` plus conditional updates prevents duplicate claims across restart/recovery.

Do not hold a database/network lock across Telegram calls. Claim durably, execute outside the DB transaction, then finalize with conditional status update.

## 11. MULTI-ACTION TASKS

Initial safe scope: one task owns a bounded ordered list of existing allowlisted tool actions, each with a versioned action name and validated arguments. Execute sequentially and record aggregate result. A failure policy must be explicit; safest initial behavior is stop on first failure, mark the execution failed, and do not silently continue destructive or dependent steps.

Per-step durable persistence is not required for a small bounded list if the aggregate attempt stores safe step summaries. It becomes DATABASE_REQUIRED if restart resume-from-step or detailed audit is promised. Conditional actions (“if Y then Z”), loops, arbitrary workflows, and AI re-planning during execution are future scope and should be rejected or require a separate reviewed workflow model. Existing ToolExecutor's max-five limit is a useful upper bound, but scheduled-task limits should be independently validated.

## 12. SECURITY

Owner-only Telegram handlers are enforced through `is_owner`; outgoing-only events are the authenticated instruction boundary. Every task and execution must carry and verify `owner_id`. Task creation must not accept an owner ID supplied by the model or arbitrary destination without deterministic validation.

The ToolRegistry allowlist and ToolExecutor permission checks remain mandatory. Persisted task JSON is untrusted data and must be schema-validated on load, not trusted because it was previously created. Prompt injection in a saved description/action argument must not alter the action name or permission; only validated structured fields are executable.

The Self Bot remains the final Telegram authority via the injected client/TelegramAPI. No direct Telethon RPC from the AI/task parser, no shell/SQL execution, no provider bypass, and no new arbitrary command executor. Scheduled destructive/admin actions require an explicit reviewed policy and must not silently inherit immediate-message authorization.

Database access must follow service-role backend writes and owner-scoped reads/RLS conventions. Do not expose task definitions or execution history through the dashboard until an owner-scoped API contract exists.

## 13. EXISTING FEATURE INTEGRATION

- **Bio/Username:** remain on the existing shared profile scheduler. Do not merge TaskScheduler callbacks into `profile.scheduler`; generic tasks and profile updates have different semantics.
- **Ghost Seen:** no demonstrated dependency; leave separate.
- **Save/Deep Save/Delete:** can be scheduled only through existing tools after action allowlisting, argument/context validation, and explicit destructive-action policy. Deep Save is long-running and cannot be assumed safe for unattended execution.
- **AI sessions/messages:** task creation may record the originating conversation reference if needed for audit, but task execution must not depend on volatile session RAM.
- **ProviderManager/Dispatcher/Engine:** no scheduler lifecycle ownership; they only produce structured proposals or immediate AI execution.
- **ToolRegistry/ToolExecutor:** reuse for action execution and permission/timeout/history behavior.
- **RuntimeSupervisor:** owns task scheduler startup, shutdown, rebuild/reload, and health registration. No second recovery authority.
- **settings service:** may provide owner timezone/defaults only after a precise source-backed setting contract; current `ConfigManager` AI settings are RAM-only and are not a durable task store.
- **diagnostics/health:** add only bounded scheduler health/last-run telemetry in a later implementation; do not treat telemetry as authority.

## 14. DATABASE / SCHEMA REQUIREMENTS

No existing table expresses a durable scheduled task or occurrence claim. Reusing `ai_sessions`, `ai_messages`, or `ai_tool_history` would conflate conversation/audit data with task authority and cannot represent due schedules or atomic occurrence uniqueness. Therefore a new task-definition table and an execution-attempt/history table are **DATABASE_REQUIRED** for the requested restart-safe feature. This is a proposal requiring a dedicated schema review; no migration is authorized by this document.

Minimum schema review questions: UUID vs bigint IDs; owner foreign-key availability; UTC timestamp type; IANA timezone storage; JSONB validation/version; enum/check constraints; unique occurrence key; due-task and task-history indexes; RLS policies; deletion/retention; service-role access; migration compatibility with existing fallback behavior.

No database changes were made and no SQL was executed.

## 15. CONFIRMED FINDINGS

- **CONFIRMED / EXISTING:** `profile.scheduler` is a singleton, in-memory, minute-boundary scheduler specific to Bio/Username.
- **CONFIRMED / EXISTING:** RuntimeSupervisor is the single lifecycle/recovery authority and already has startup, shutdown, rebuild, and task supervision hooks.
- **CONFIRMED / EXISTING:** There is no generic task scheduler, task table, execution table, task creation handler, or scheduled-task tool in current source.
- **CONFIRMED / EXISTING:** ToolExecutor is the sole tool execution component and enforces registry lookup, permission levels, sequential calls, limits, timeout handling, and history recording.
- **CONFIRMED / EXISTING:** Current AI config/preferences/session fallback state is partly RAM-only; it is not suitable as durable scheduler authority.
- **CONFIRMED / DATABASE_REQUIRED:** Restart-safe task definitions and occurrence execution state require durable storage not represented by current tables.
- **CONFIRMED / ARCHITECTURE:** Self Bot/TelegramAPI must remain the only Telegram execution authority.

## 16. LIKELY FINDINGS

- **LIKELY / PROPOSED:** A singleton TaskScheduler loop with bounded polling/wakeup and atomic durable claims is the smallest compatible runtime design.
- **LIKELY / PROPOSED:** Two durable concepts (task definition and execution attempt) are sufficient for initial scope; action/step tables can be deferred while action lists remain small, bounded, and versioned JSON.
- **LIKELY / PROPOSED:** Initial execution should be sequential, stop-on-first-failure, bounded, and at-least-once rather than promise exactly-once Telegram side effects.
- **LIKELY / PROPOSED:** A scheduled-action tool should create validated task proposals/persisted definitions, while execution should reuse ToolExecutor rather than directly invoke services or Telegram.
- **LIKELY / BLOCKER:** DST, missed-run, retry, confirmation-required actions, notification destination, and one-shot terminal-failure semantics need product decisions before implementation.

## 17. UNKNOWN / REQUIRES SOURCE VERIFICATION

- Exact live Supabase schema/RLS state cannot be established without querying the database; no SQL or live DB access was performed.
- Whether the deployment can ever run multiple process instances is not established; current code is designed as one process.
- Exact owner-timezone setting precedence for future tasks is not currently defined.
- Exact acceptable missed-run policy, DST policy, retry backoff/max attempts, and idempotency guarantees require explicit product decisions.
- Whether unattended dangerous/confirmation-required tools should be supported at all is unresolved.
- Whether scheduled notifications should edit the originating message, reply in its chat, or use another explicitly authorized destination is unresolved.
- Whether per-step execution persistence is needed beyond initial bounded aggregate history is a product/audit decision.

## 18. RECOMMENDED IMPLEMENTATION ORDER

1. Resolve the decisions/blockers in section 21 and freeze the initial safe scope.
2. Perform a dedicated schema design/review for owner-scoped task definitions and execution attempts; add migration and matching repository/fallback only in that separate phase.
3. Implement deterministic schedule value objects/validation and next-run calculations, including timezone/DST and missed-run tests.
4. Implement task repository operations with atomic due-claim/occurrence uniqueness and bounded retry/finalization semantics.
5. Implement a singleton TaskScheduler owned by RuntimeSupervisor; load/reconcile on startup and rebuild, and cancel cleanly on shutdown.
6. Add a narrowly scoped structured task proposal/creation tool through ToolRegistry/ToolExecutor. Validate owner, schedule, actions, limits, and dangerous-action policy before persistence.
7. Implement sequential scheduled action execution through the existing ToolExecutor/TelegramAPI context, with durable attempts and owner-only notifications.
8. Add observability and owner-scoped management UI/handlers only after core correctness is tested.

## 19. TEST / VALIDATION PLAN

Future implementation tests must include:

- schedule normalization for one-shot, interval, daily, weekly, invalid dates/times, timezone IDs, DST transitions, and boundary clocks;
- deterministic next-run and missed-run policy;
- owner isolation and malformed persisted JSON rejection;
- atomic duplicate claim and unique occurrence behavior;
- restart reconciliation for pending/running/succeeded/failed attempts;
- bounded retry/backoff and cancellation;
- disable/delete races and non-overlap of recurring tasks;
- ordered multi-action success, stop-on-failure, action-count/argument limits, and confirmation-required rejection;
- ToolRegistry/ToolExecutor reuse with no direct Telegram/RPC/provider calls;
- RuntimeSupervisor start/rebuild/stop/reload and no duplicate scheduler loop;
- fake TelegramAPI integration proving Self Bot authority and honest durable result transitions;
- Supabase repository behavior plus in-memory fallback without executing SQL in unit tests;
- retention/index/RLS behavior in a separate database integration phase.

## 20. HARD CONSTRAINTS

- This document is investigation-only; no scheduler, parser, tool, handler, migration, schema, or production code was implemented.
- Do not reuse `profile.scheduler` as a generic task scheduler.
- One TaskScheduler singleton only; no per-task forever loops and no second recovery authority.
- RuntimeSupervisor owns task scheduler lifecycle and recovery.
- AI may propose structured tasks but cannot create timers or execute Telegram actions directly.
- All scheduled actions must pass deterministic validation and existing tool permission boundaries.
- Self Bot remains Telegram execution authority.
- Database stores definitions and history only; it never calls AI, tools, Telegram, SQL business logic, or timers.
- No arbitrary shell, SQL, RPC, provider, or command execution.
- No unbounded task/action/retry/backlog/resource growth.
- No exactly-once claim for Telegram side effects without proven idempotency.
- Preserve existing Bio/Username scheduler and all current features.

## 21. REMAINING DECISIONS / BLOCKERS

Before implementation, explicitly decide:

1. Missed-run policy: skip, immediate one-time catch-up, coalesce, or bounded replay.
2. DST policy for nonexistent and ambiguous local times.
3. Interval anchoring: scheduled-time anchor versus completion-time anchor.
4. Retry policy and maximum attempts for task-level and action-level failures.
5. Whether scheduled destructive/admin actions are prohibited, confirmation-gated, or supported only with a pre-authorized task approval model.
6. Notification destination and behavior after execution/failure.
7. Whether one-shot failed tasks remain enabled for retry or become terminal.
8. Whether task edits create versions and how already-claimed occurrences use old/new definitions.
9. Whether multi-action conditionals are deferred completely; recommended initial answer is yes.
10. Whether two tables are acceptable for initial durable state; recommended answer is task definitions plus execution attempts, with bounded action JSON.

## 22. PHASE BOUNDARY

This phase was investigation only. The only permitted artifact change is this handoff document. No production code, tests, database/schema/migrations, SQL, Supabase state, AI parser, task creation path, scheduler, Telegram behavior, commit delivery code, or provider/engine architecture was changed.
