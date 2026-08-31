# INVESTIGATION

## Investigation Metadata

| Field | Value |
|---|---|
| Repository | `Onlyicing1/Telegram-self-bot` |
| Branch | `main` |
| Local HEAD at investigation start | `127d82e7408a37b5b6d417626b67854be27438ec` |
| Remote HEAD at investigation start | `127d82e7408a37b5b6d417626b67854be27438ec` |
| Investigation type | Source-only current-state verification and next-boundary discovery |
| Implementation performed | NO |

The working tree contained one unrelated pre-existing modification (`tests/test_stage13.py`); it was preserved exactly and not modified by this investigation. Only this document was rewritten.

## Current Repository State

- Branch `main`, synchronized with `origin/main` at `127d82e7408a37b5b6d417626b67854be27438ec` (local HEAD == remote HEAD).
- Working tree at start: only `M tests/test_stage13.py`.
- `IMPLEMENTATION_REPORT.md` is the canonical current-state report and describes the delivered interrupted-occurrence recovery.
- The previous `INVESTIGATION.md` identified `restart-safe interrupted occurrence recovery` as the remaining core gap and the next boundary. That boundary is now implemented and delivered (see below); that prior gap conclusion is therefore **SUPERSEDED** by current source and history.

## Verified Implementation History

| Boundary | Commit | Current source status |
|---|---|---|
| Durable task database foundation | covered by staged history | Present; `ai_tasks` + `ai_task_occurrences`, CAS version, RLS, unique occurrence identity |
| Scheduler runtime and restart-safe coordination | covered by staged history | Present; single polling loop, `recover()` on start, bounded per-wake limits |
| Execution and action dispatch | covered by staged history | Present; registered tools, bounded runtime and metadata |
| Retry classification and policy | covered by staged history | Present; `MAX_ATTEMPTS = 3`, bounded exponential backoff |
| Automatic execution-failure → durable retry | covered by staged history | Present and verified |
| **Restart-safe interrupted occurrence recovery** | `1e1b1212f6088c4eac6902af09f26f25418b1dbc` | Present in source (`backend/ai/task_scheduler.py`, `backend/ai/database/task_repository.py`) and on `origin/main` |
| Current-state implementation report | `cd767dffd4866c5595f57abb40dd2dd794605da4` + `127d82e7408a37b5b6d417626b67854be27438ec` | Present on `origin/main` |

## Delivered Boundary: Restart-safe Interrupted Occurrence Recovery — VERIFIED COMPLETE

Portions traced against current source:

- **Startup recovery.** `TaskScheduler.recover()` (`backend/ai/task_scheduler.py`) queries `list_recoverable_occurrences(self.owner_id, MAX_RECOVERY_PER_START)`. It first transitions persisted `claimed`/`running` rows to `interrupted` via the owner-scoped repository CAS transition, then resolves every row present in the `interrupted` state (including rows already persisted as `interrupted` by a prior process).
- **Interrupted occurrence resolution.** `_resolve_interrupted()` never strands a row. When `can_retry(attempt)` holds it transitions `interrupted → retry_pending` with `retry_at = updated_at + retry_delay(attempt)`, `attempt + 1`, `finished_at=None`, and `error_metadata.error_class="restart_interrupted"`. Otherwise it transitions `interrupted → failed` (terminal) with `retry_at=None`.
- **Retry policy reuse / attempt limits.** Recovery calls only `can_retry()` and `retry_delay()` from `backend/ai/retry.py` (`MAX_ATTEMPTS = 3`). No retry policy is duplicated in the scheduler. Attempts 1–2 may be requeued; attempt 3 fails terminal. No fourth attempt is possible.
- **retry_at handling.** Requeued retries roll their next run off the persisted `updated_at`; the existing `list_due_retry_occurrences()` (`retry_pending` where `retry_at <= now`) and `run_once()` pickup are unchanged.
- **Terminal failure behavior.** `interrupted` is removed from `_TERMINAL_OCCURRENCE_STATUSES` in `backend/ai/database/task_repository.py`, so a recovery requeue never retains terminal `finished_at`; terminal `failed` still records `finished_at`.
- **Repository implementations.** Both `InMemoryTaskRepository` and `SupabaseTaskRepository` include `interrupted` in the bounded, owner-filtered `list_recoverable_occurrences()` query; the migration `supabase/migrations/20260829000001_create_ai_tasks.sql` requires no change (existing `status`, `attempt`, `retry_at`, `error_metadata` columns suffice).
- **Owner isolation.** Every recovery query and transition filters by the scheduler's trusted `owner_id` in both repository paths.
- **CAS / duplicate protection.** Both `transition_occurrence()` implementations are status-CAS guarded (`eq("status", current.status)` in Supabase; equality check in-memory) and immutable-field guarded. A second recovery pass observes the already-requeued/failed state and does not resolve again. `claim_occurrence()` only moves eligible statuses (`claimed`/`retry_pending`) to `running`.
- **Cancellation.** `asyncio.CancelledError` is re-raised (not converted to retry/failure state) across the scheduler and coordinator boundaries.
- **Tests.** `tests/test_task_scheduler.py` and `tests/test_task_repository.py` include recovery marking, exhausted-attempt terminal failure, scheduler pickup of recovered retries, pre-existing `interrupted` recovery, owner scoping, and single execution.
- **Git delivery state.** Verified by `git fetch origin main`, `git rev-parse HEAD`, `git rev-parse origin/main`: both resolve to `127d82e7408a37b5b6d417626b67854be27438ec`. Commit `1e1b121` contains exactly `backend/ai/task_scheduler.py`, `backend/ai/database/task_repository.py`, `tests/test_task_scheduler.py`, `tests/test_task_repository.py`.

## Current Architecture

```text
Telegram `.task <request>`
  -> backend.bot.handlers.tasks.task_handler          (is_owner gate, edit-in-place)
  -> TaskInterpreter / TaskCandidate                  (bounded provider call, owner-agnostic validation)
  -> TaskCreationService.create                        (authoritative owner_id)
  -> TaskRepository.create_task                        (ai_tasks, CAS version)
  -> RuntimeSupervisor._start_task_scheduler           (single scheduler construction)
  -> TaskScheduler.recover / run_once
  -> TaskRepository occurrence transitions + claim_occurrence
  -> TaskExecutionCoordinator.execute
  -> ToolExecutor.execute_calls                        (registered tools only)
  -> TaskOutcomeNotifier.notify_persisted / TaskNotificationService
  -> TelegramAPI.send_message                          (rebuild-safe sender)

AI conversation turn
  -> backend.bot.handlers.ai_unified -> engine
  -> Dispatcher._build_context -> ConversationManager history + MemoryManager.retrieve_for_prompt
  -> ConversationManager._add_message -> persistence.add_message (ai_messages)
  -> ConversationManager.restore_history (ai_sessions/ai_messages after restart)
```

Boundaries verified in source:

- Lifecycle: `backend/runtime/supervisor.py::RuntimeSupervisor` owns the self-client, run loop, and the single task scheduler.
- Scheduling: `backend/ai/task_scheduler.py::TaskScheduler` is the only task polling loop.
- Execution: `TaskExecutionCoordinator` never schedules; the scheduler never executes actions itself.
- Persistence: `backend/ai/database/task_repository.py` enforces owner scoping, transition maps, immutable occurrence fields, bounded payloads, and CAS updates.
- Tool action boundary: actions resolve only through `ToolRegistry`/`ToolExecutor`; `TaskExecutionCoordinator` rejects unregistered actions before execution.
- Notification: `TaskNotificationService` is owner-checked, kind-bounded, and truncates delivery; outcomes are only notified after persistence-verified status.
- Telegram transport: `backend/telegram_api/` remains the only Telegram send path used by task systems.

## Capability / Feature Status

| Capability | Status | Evidence |
|---|---|---|
| Telegram task creation (natural language) | IMPLEMENTED | `backend/bot/handlers/tasks.py`, `TaskInterpreter`, `TaskCreationService` |
| Structured candidate validation | IMPLEMENTED | `TaskCandidate.from_untrusted` |
| once/interval/daily/weekly schedules | IMPLEMENTED | `backend/ai/scheduling.py`, `SCHEDULE_TYPES`, migration CHECK |
| Durable persistence with CAS | IMPLEMENTED | `TaskRepository.update_task`, occurrence transition CAS |
| Occurrence claiming | IMPLEMENTED | `claim_occurrence` (both repository implementations) |
| Registered-tool execution | IMPLEMENTED | `TaskExecutionCoordinator`, `ToolExecutor` |
| Automatic retry on failure | IMPLEMENTED | `execute()` → `handle_failure()`; durable attempt/retry_at |
| Scheduler retry pickup | IMPLEMENTED | `list_due_retry_occurrences()`, `run_once()` |
| Persisted-outcome notification | IMPLEMENTED | `TaskOutcomeNotifier.notify_persisted` |
| Startup recovery | IMPLEMENTED | `recover()` → `interrupted` |
| **Interrupted-occurrence resolution** | **IMPLEMENTED (delivered)** | `recover()` → `_resolve_interrupted()` → retry_pending/failed |
| AI memory (short/long/permanent) retrieval into context | IMPLEMENTED | `Dispatcher._build_context` calls `MemoryManager.retrieve_for_prompt` (bounded via `to_thread` + timeout); `ai_memories` migrated |
| AI session/message persistence + restore | IMPLEMENTED | `ConversationManager._add_message` → `persistence.add_message`; `restore_history`; `ai_sessions`/`ai_messages` migrated |
| AI usage / provider-stats telemetry | IMPLEMENTED | `ai_usage` migration `20260827000003`, `ai_provider_stats` migration `20260827000004`; Repos upsert and fail soft |
| Task list/inspect/lifecycle/deletion (Telegram) | IMPLEMENTED | `tasks.py::_handle_management` |
| Richer inspect (error/result metadata) | NOT IMPLEMENTED (optional UX) | `inspect_text` shows status/attempt only |
| Glass task panels | NOT IMPLEMENTED (optional UX) | no `register_panel/register_action` in `tasks.py` |
| Dashboard task APIs/UI | NOT IMPLEMENTED (optional UX) | `backend/web/app.py` has no task routes; no task-only frontend view |
| Task editing / rescheduling (user-facing) | NOT IMPLEMENTED (optional UX) | `TaskRepository.update_task` exists; no handler path exposes it |
| Notification-destination management | NOT IMPLEMENTED (optional UX) | destination set at creation only |
| `ai_preferences` durable persistence | NOT IMPLEMENTED (gated; graceful fallback) | in-memory-only repository; no `ai_preferences` migration column/table; dispatcher falls back to defaults |
| Cron / event / webhook / conditional triggers | NOT PROVEN BY CURRENT SOURCE | not in `SUPPORTED_TYPES` / scheduler |
| Exactly-once side effects | NOT PROVEN BY CURRENT SOURCE | at-least-once semantics documented and implemented |

## Core vs Optional

**Core (established by repository architecture):**

- Durable scheduled execution including restart recovery. The interrupted-recovery boundary that closed the last proven core durability gap is implemented and delivered. After that boundary, **no further core durability gap is proven by current source.** The durable task scheduler, occurrence state machine, retry policy, recovery, execution, notification, and AI persistence primitives are all present and wired.

**Optional UX / product enhancements (absent but NOT justified as implementation gaps):**

- Glass task panels
- Dashboard task APIs/UI
- Natural-language task management
- Task editing / rescheduling
- Richer history/error browsing
- Notification-destination management

None of these is mandated by AGENTS.md, README, the migrations, or the task/AI contracts. They must not be classified as core merely because they are absent.

**Distinctly gated, non-architectural:**

- Durable `ai_preferences` persistence (an in-memory fallback already exists and the missing table degrades gracefully; making it durable is additive and requires an explicit contract decision, not an architectural fix).

## Remaining Gaps

### Gap 1 — Optional Telegram/Glass task-management UX (OPTIONAL)

- **Missing behavior:** a user-facing path to edit/reschedule tasks, manage notification destinations, and richer list/inspect output, and optional inline-panel equivalents.
- **Architectural layer:** Telegram presentation + existing task management services.
- **Exact files:** `backend/bot/handlers/tasks.py`, `backend/ai/task_management*.py`, `backend/ai/task_management_interface.py`, optional `backend/helper` registrations; reuses `TaskRepository.update_task`, `transition_occurrence`, and existing management service methods.
- **Dependencies:** none outside existing task service/repository boundaries. No schema change currently justified.
- **Security/ownership:** every action remains owner-gated (`is_owner`) and owner-scoped in the repository.
- **Test requirements:** per-verb handler tests, owner isolation, transition validation.
- **Can be combined:** yes — with the other Telegram/Glass task UX items below (same boundary).

### Gap 2 — Optional dashboard task read APIs/UI (OPTIONAL)

- **Missing behavior:** read-only task/occurrence visibility in the FastAPI dashboard.
- **Architectural layer:** web transport + frontend, separate authorization surface. Requires an explicit authenticated owner contract before implementation.
- **Can be combined:** among themselves (API + UI), but must remain separate from Telegram work.

### Gap 3 — Durable `ai_preferences` persistence (GATED, non-core)

- **Missing behavior:** no `ai_preferences` migration/table; the dispatcher reads an in-memory repository (`get_or_create`) and falls back to defaults on any error.
- **Why it is NOT core:** the subsystem degrades gracefully, no product contract requires durability, and no core behavior is lost across restart (defaults apply).
- **Can be combined:** separate from task UX; a persistence change should be its own boundary.

## Tasks That Can Be Safely Combined

- Telegram/Glass task-management UX items (editing/rescheduling + destination management + richer inspect + optional panels) — one presentation/management boundary, shared service and repository methods.
- Dashboard task read API + dashboard task UI — one transport/frontend boundary (after an authorization contract exists).

## Tasks That Must Remain Separate

- Any task-system change vs a schema/persistence change (no schema change is currently justified; if `ai_preferences` durability is ever pursued it is its own boundary).
- Telegram/Glass UX vs dashboard APIs (different transports and authorization models).
- Natural-language task management vs deterministic management commands (NL must delegate to existing service methods, never become a second authority).
- Any new scheduler, worker, execution authority, or retry engine (explicitly out of scope; current architecture forbids them).

## Recommended Next Implementation Group

**Single recommended next group (optional, presentation-boundary): "Task management UX"**

- Owner-scoped Telegram verb for editing/rescheduling a task (reuse `TaskRepository.update_task` with `expected_version` CAS and `transition_task`), combined with notification-destination updates and richer `inspect`/`list` output (status, attempt, `retry_at`, `error_metadata`, `result_metadata`), exercised through the existing task management interface and handler.
- Files: `backend/bot/handlers/tasks.py`, `backend/ai/task_management*.py`, `backend/ai/task_management_interface.py`, and focused handler/management tests.
- Deterministic management verbs only; natural-language management remains a separate future boundary.
- No new scheduler, no new execution authority, no schema change.

This group is justified only if the product owner wants user-facing task management now. It is **OPTIONAL**, not a core durability gap. If no such product requirement exists, there is currently no source-mandated next implementation.

## Database / Supabase Status

- Migration inventory includes the pre-existing task migration (`20260829000001_create_ai_tasks.sql`) and the AI migrations for `ai_memories` (`20260804145402_create_ai_tables.sql`), `ai_usage` (`20260827000003_create_ai_usage_table.sql`), and `ai_provider_stats` (`20260827000004_create_ai_provider_stats_table.sql`).
- The delivered interrupted-recovery boundary required **no schema change**: existing `status`, `attempt`, `retry_at`, and transition constraints sufficed.
- Live Supabase state: NOT PROVEN BY CURRENT SOURCE (no live verification performed in this investigation).

## Security / Ownership Status

- Owner identity flows only from runtime/handler context; candidate/interpreter data cannot override it.
- All repository operations are owner-filtered in both in-memory and Supabase paths.
- No arbitrary Telegram RPC, arbitrary SQL/RPC, shell execution, persisted-code execution, or provider bypass exists in the task or AI execution paths.
- Tool actions resolve exclusively through `ToolRegistry`/`ToolExecutor`; deterministic destructive tools execute only after local ownership and (where flagged) confirmation boundaries.

## Test / Validation Status

- This investigation performed no code change; no new tests were authored.
- Full task- and repository-focused coverage exists (`tests/test_task_repository.py`, `tests/test_task_scheduler.py`, `tests/test_task_execution.py`, `tests/test_task_management.py`, `tests/test_retry.py`, and `tests/test_stage*.py`), and the recorded delivery validation in `IMPLEMENTATION_REPORT.md` states: focused recovery/repository `21 passed`, broader focused task `32 passed`, full suite `1161 passed, 23 skipped, 1 warning`, `compileall` clean, `git diff --check` clean. That validation was run during the delivered implementation, not in this investigation.
- Not re-run in this investigation (no source changed).

## What Was NOT Verified

- Live Telegram delivery and live Supabase persistence/RLS behavior.
- Real process-crash/restart behavior in a deployed environment.
- Exactly-once external side effects (not claimed; at-least-once only).
- Durable `ai_preferences` (no table/column exists).

## Final Verdict

The restart-safe interrupted-occurrence recovery boundary identified by the prior investigation is **IMPLEMENTED and DELIVERED**, verified in current source, tests, and `origin/main` (`1e1b121` implementation; local and remote both at `127d82e`). The prior gap conclusion is marked **SUPERSEDED**. No further core durability gap is proven by current source. Remaining absent capabilities are optional UX unless a product requirement outside the current source declares otherwise; the smallest justified optional boundary is deterministic **task management UX** (editing/rescheduling + destination management + richer inspect) within the existing management boundary.

## Investigation Boundary

- Production code changed: NO
- Tests changed: NO
- Database changed: NO
- Migration changed: NO
- SQL executed: NO
- Supabase changed: NO
- Telegram behavior changed: NO
- Implementation performed: NO
- `INVESTIGATION.md` rewritten: YES

## Git Delivery / Verification State

- Commit status: this documentation-only commit containing exactly `INVESTIGATION.md`.
- Push status: to be verified immediately after push via `git fetch` + `git rev-parse`.
- Local HEAD / Remote HEAD / Local==Remote: to be recorded from the verified delivery output.
- Final working-tree status: `tests/test_stage13.py` remains modified and uncommitted (pre-existing unrelated change, preserved). No other modifications remain.