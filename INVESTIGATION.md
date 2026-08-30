# INVESTIGATION

## Investigation Metadata

| Field | Value |
|---|---|
| Repository | `Onlyicing1/Telegram-self-bot` |
| Branch | `main` |
| Local HEAD at investigation start | `7b8adc83dd315f44ab343ab14ef01f33a7ea2419` |
| Remote HEAD at investigation start | `7b8adc83dd315f44ab343ab14ef01f33a7ea2419` |
| Date | 2026-08-30 |
| Investigation type | Source-only current-state investigation and next-boundary discovery |
| Implementation performed | NO |

The working tree contained one unrelated pre-existing modification (`tests/test_stage13.py`); it was not modified by this investigation. Only this document was rewritten.

## Current Repository State

- Branch `main`, synchronized with `origin/main` at investigation start.
- Latest implementation commit in history: `da021b2ae1e48e02277cf00c160607fbd5a6734b` (`fix: wire task execution failures into durable retries`), followed by documentation commits `4043bde`, `4ba68d9`, and `7b8adc8`.
- Working tree at start: only `M tests/test_stage13.py`.
- `IMPLEMENTATION_REPORT.md` is the canonical current-state report and describes the durable task system through the automatic retry integration.
- The prior `INVESTIGATION.md` was stale: it concluded that `TaskExecutionCoordinator.handle_failure()` had no production caller and that attempts were never advanced. Current source contradicts that: `backend/ai/task_execution.py::execute` routes executor exceptions and unsuccessful tool results through `handle_failure()`, which persists `retry_pending` with `retry_at` and `attempt=occurrence.attempt + 1`. That work is delivered and synchronized.

## Executive Summary

The durable Telegram task lifecycle is implemented in source: owner-scoped creation with natural-language interpretation, bounded candidate validation, durable two-table persistence with CAS versioning, once/interval/daily/weekly scheduling, deterministic occurrence creation, claiming, registered-tool execution, automatic retry classification with durable retry progression, scheduler retry pickup, and persisted-outcome notification verification.

One remaining durability gap is proven by current source: **interrupted-occurrence recovery is incomplete**. On scheduler start, `recover()` marks persisted `claimed`/`running` occurrences as `interrupted`, but nothing ever resolves them afterward. `run_once()` only picks up due `retry_pending` rows and due active tasks. `interrupted` occurrences therefore remain stranded permanently after any process restart that interrupts in-flight work. This is a core durability boundary, not optional UX: the repository already defines `interrupted` as a non-terminal status and its state machine permits `interrupted → retry_pending` or `interrupted → failed`, but no production path exercises either transition.

After the interrupted-recovery boundary, no further core gap is proven. Remaining absent capabilities (Glass task panels, dashboard task APIs, natural-language management, task editing/rescheduling, richer history browsing, notification-destination management) are optional UX with no source-established necessity.

## Verified Implementation History

| Boundary | Evidence | Current source status |
|---|---|---|
| Durable task database foundation | `supabase/migrations/20260829000001_create_ai_tasks.sql`, `backend/ai/database/task_repository.py` | Present; two tables, bounded payloads, CAS version, RLS, unique occurrence identity |
| Supabase repository integration with fallback | `backend/ai/database/task_repository.py::SupabaseTaskRepository` | Present; owner-filtered, `asyncio.to_thread` bounded, falls back on external failure |
| Scheduler runtime and restart-safe coordination | `backend/ai/task_scheduler.py`, `backend/runtime/supervisor.py` | Present; single polling loop, recovery on start, bounded per-wake limits |
| Execution and action dispatch | `backend/ai/task_execution.py::TaskExecutionCoordinator`, `backend/ai/tools/registry.py`, `backend/ai/tools/executor.py` | Present; registered tools only, bounded runtime and metadata |
| Retry classification and policy | `backend/ai/retry.py` | Present; `MAX_ATTEMPTS = 3`, exponential backoff bounded at 15 minutes |
| Management interfaces and notifications | `backend/ai/task_management.py`, `backend/ai/notifications.py`, `backend/ai/task_notifications.py` | Present; owner-scoped service and verified notification path |
| Telegram creation and management commands | `backend/bot/handlers/tasks.py` | Present; owner-gated, edit-in-place, deterministic management verbs |
| Candidate contract and interpreter | `backend/ai/task_candidate.py`, `backend/ai/task_interpreter.py` | Present; owner-agnostic validation, provider-mediated JSON interpretation |
| Persisted-outcome notification transport | `TaskScheduler._notify_outcome`, `TaskOutcomeNotifier.notify_persisted` | Present; verifies persisted status before delivery, lazy rebuild-safe sender |
| Automatic execution-failure → durable retry | `backend/ai/task_execution.py::execute` + `::handle_failure` | Present and verified; retryable failures persist `retry_pending`, `retry_at`, incremented `attempt` |

Historical report numbering (Stages 4–17) is retained in `IMPLEMENTATION_REPORT.md` as delivery history; current source is authoritative for behavior.

## Current Architecture

```text
Telegram `.task <request>`
  → backend.bot.handlers.tasks.task_handler          (is_owner gate, edit-in-place)
  → TaskInterpreter.interpret                         (bounded provider call)
  → TaskCandidate.from_untrusted                      (owner-agnostic validation)
  → TaskCreationService.create                        (authoritative owner_id)
  → TaskRepository.create_task                        (ai_tasks, CAS version)
  → RuntimeSupervisor._start_task_scheduler           (single scheduler construction)
  → TaskScheduler.run_once                            (retries first, then due tasks)
  → TaskRepository.create_occurrence / claim_occurrence
  → TaskExecutionCoordinator.execute
  → ToolExecutor.execute_calls                        (registered tools only)
  → TaskRepository.transition_occurrence              (succeeded / failed / retry_pending)
  → TaskOutcomeNotifier.notify_persisted              (persisted-state verification)
  → TaskNotificationService.send                      (bounded, owner-checked)
  → TelegramAPI.send_message                          (rebuild-safe sender)
```

Boundaries verified in source:

- Lifecycle: `backend/runtime/supervisor.py::RuntimeSupervisor` constructs and owns the task scheduler.
- Scheduling: `backend/ai/task_scheduler.py::TaskScheduler` is the only task polling loop.
- Execution: `TaskExecutionCoordinator` never schedules; the scheduler never executes actions itself.
- Persistence: `backend/ai/database/task_repository.py` enforces owner scoping, transition maps, immutability of occurrence identity fields, bounded payloads, and CAS updates.
- Notification: `backend/ai/notifications.py::TaskNotificationService` rejects owner mismatch and unsupported kinds, truncates to 1024 characters, and bounds delivery at 10 seconds.
- Transport: `backend/telegram_api/` remains the only Telegram send path used by the task system.

## Current Feature / Capability Status

| Capability | Status | Evidence |
|---|---|---|
| Telegram task creation (natural language) | IMPLEMENTED | `backend/bot/handlers/tasks.py::task_handler`, `TaskInterpreter`, `TaskCreationService` |
| Structured candidate validation | IMPLEMENTED | `backend/ai/task_candidate.py::TaskCandidate.from_untrusted` |
| Durable persistence with CAS | IMPLEMENTED | `TaskRepository.update_task`, occurrence transition CAS |
| once/interval/daily/weekly schedules | IMPLEMENTED | `backend/ai/scheduling.py`, `SCHEDULE_TYPES`, migration CHECK |
| Occurrence claiming | IMPLEMENTED | `claim_occurrence` in both repository implementations |
| Registered-tool execution | IMPLEMENTED | `TaskExecutionCoordinator.execute`, `ToolExecutor` |
| Automatic retry on failure | IMPLEMENTED | `execute()` → `handle_failure()`; durable attempt/retry_at |
| Scheduler retry pickup | IMPLEMENTED | `_run_due_retries`, `list_due_retry_occurrences` |
| Persisted-outcome notification | IMPLEMENTED | `TaskOutcomeNotifier.notify_persisted`, scheduler hook |
| Startup recovery marking | IMPLEMENTED | `TaskScheduler.recover()` → `interrupted` |
| **Interrupted-occurrence resolution** | **NOT IMPLEMENTED** | No production path transitions `interrupted` to any further status; `run_once()` never queries `interrupted` rows |
| Telegram list/inspect/lifecycle/deletion | IMPLEMENTED | `backend/bot/handlers/tasks.py::_handle_management` |
| Retry/error visibility in inspect | PARTIAL | `inspect_text` shows status/attempt only, not error/result metadata |
| Glass task panels | NOT IMPLEMENTED | No `register_panel/register_action` calls in `backend/bot/handlers/tasks.py` |
| Dashboard task APIs/UI | NOT IMPLEMENTED | `backend/web/app.py` has no task routes; `src/` has no task views |
| Natural-language task management | NOT IMPLEMENTED | Interpreter used only in creation branch |
| Task editing/rescheduling | NOT IMPLEMENTED (user-facing) | Repository supports `update_task`; no Telegram/handler path exposes it |
| Notification destination management | NOT IMPLEMENTED | Destination set at creation only |
| Cron/event/webhook/conditional triggers | NOT PROVEN BY CURRENT SOURCE | Not present in `SUPPORTED_TYPES` or scheduler |
| Exactly-once side effects | NOT PROVEN BY CURRENT SOURCE | At-least-once semantics documented and implemented |

## Source Evidence (key excerpts)

- `backend/ai/task_scheduler.py::recover` transitions every `claimed`/`running` occurrence to `interrupted` at start.
- `backend/ai/task_scheduler.py::run_once` processes only `list_due_retry_occurrences()` (status `retry_pending`) and `list_due_tasks()` (task status `active`). No query returns `interrupted` occurrences.
- `backend/ai/database/task_repository.py::_ALLOWED_OCCURRENCE_TRANSITIONS` permits `interrupted → {retry_pending, failed}` and `claim_occurrence` accepts `interrupted`, proving the state machine anticipates reclaiming interrupted work.
- `tests/test_task_scheduler.py::test_recovery_marks_claimed_and_running_interrupted_without_execution` asserts the marking only; no test asserts later resolution because none exists.
- `backend/ai/task_execution.py::handle_failure` persists `retry_pending` with `attempt + 1` and bounded metadata; `backend/ai/retry.py` bounds attempts at 3.

## Core vs Optional

**Core (established by repository architecture):**

- Durable scheduled execution including restart recovery. The repository defines `interrupted` as a non-terminal status with explicit recovery transitions; leaving rows stranded contradicts the durable-scheduling objective and the existing state machine. This is the remaining core boundary.

**Optional UX (absent but not required by source):**

- Glass task panels
- Dashboard task APIs/UI
- Natural-language task management
- Task editing/rescheduling commands
- Richer history/error browsing
- Notification-destination management

None of these is mandated by AGENTS.md, README, the migration, or the task contracts. They must not be classified as core merely because they are absent.

## Remaining Gaps

### Gap 1 — Interrupted-occurrence recovery resolution (CORE)

- **Missing behavior:** After `recover()` marks occurrences `interrupted`, no code path ever resolves them. They are never retried, failed, cancelled, or re-claimed, so restart-interrupted work is silently lost while remaining non-terminal in the database.
- **Why it matters:** Durable scheduling must survive process restarts. The state machine already authorizes `interrupted → retry_pending` (respecting `MAX_ATTEMPTS` and `retry_at`) or `interrupted → failed`, and `claim_occurrence` accepts `interrupted`, showing reclaim was the intended design.
- **Exact files/components:** `backend/ai/task_scheduler.py` (recovery resolution in `recover()` or a bounded recovery sweep in `run_once()`), `backend/ai/database/task_repository.py` (a bounded query for recoverable/resumable occurrences reusing the existing transition map), reuse of `TaskExecutionCoordinator.handle_failure()` semantics for attempt/retry_at consistency, and `tests/test_task_scheduler.py` / `tests/test_task_repository.py` for coverage.
- **Dependencies:** None outside the existing scheduler/repository/coordinator boundaries. No schema change: existing `status`, `retry_at`, and `attempt` columns are sufficient.
- **Security/ownership:** All operations remain owner-scoped via `self.owner_id`; no new authorization surface.
- **Test requirements:** recovery → retry_pending with correct attempt/retry_at; recovery → terminal failed at attempt limit; no duplicate execution after reclaim; owner isolation; cancellation propagation.
- **Can be safely combined:** No other task shares this boundary. Keep it standalone.

### Gap 2 — Optional Telegram/Glass management UX (OPTIONAL)

- Glass panels, richer inspect output, editing/rescheduling, destination management share the Telegram presentation boundary and could be grouped if explicitly requested. Not required by source.

### Gap 3 — Optional dashboard task APIs/UI (OPTIONAL)

- Separate transport and authorization boundary. Requires an explicit authenticated owner contract before any implementation. Must remain separate from Telegram work.

## Dependencies / Boundaries

- The interrupted-recovery boundary must not create a second scheduler, worker, or execution path; it extends the existing single `TaskScheduler` loop and existing repository transitions.
- Retry semantics must reuse `retry_delay`, `can_retry`, and `MAX_ATTEMPTS` from `backend/ai/retry.py` so attempts remain bounded across restarts.
- Notification behavior must remain post-execution and persistence-verified; recovery itself should not fabricate outcomes.
- Optional Telegram UX work shares `backend/bot/handlers/tasks.py` and `backend/ai/task_management*` and may be grouped; it must not touch scheduler/execution.
- Dashboard work is a separate boundary and authorization surface.

## Recommended Next Implementation Group(s)

**Single implementation group (standalone): "Restart-safe interrupted occurrence recovery"**

- Objective: at scheduler start (and only there), resolve persisted `interrupted` occurrences deterministically: reclaim-and-continue within the existing retry policy (`interrupted → running` via existing claim semantics when attempts remain, otherwise `interrupted → failed`), using existing repository transitions and owner scoping, with bounded limits and full test coverage.
- Files: `backend/ai/task_scheduler.py`, optionally a small bounded query addition in `backend/ai/database/task_repository.py` (interface + both implementations), and focused tests.
- Must NOT include: Glass UI, dashboard APIs, notification changes, schema changes, new workers, or interpreter changes.

## Tasks That Can Be Safely Combined

- (Only if explicitly requested later) Telegram/Glass task UX extensions with each other — they share the presentation and management boundary.
- Dashboard task read APIs with dashboard task UI — same transport and frontend boundary, after an authorization contract exists.

## Tasks That Must Remain Separate

- Interrupted-recovery work vs any UX work (different architectural layers; recovery changes durable semantics and needs isolated validation).
- Telegram/Glass UX vs dashboard APIs (different transports and authorization models).
- Natural-language management vs deterministic management changes (NL must delegate to existing service methods, never become a second authority).
- Any schema-affecting work vs everything else (no schema change is currently justified; if one ever is, it must be its own boundary).

## Database / Supabase Status

- Migration inventory: exactly one task migration, `supabase/migrations/20260829000001_create_ai_tasks.sql`, defining `ai_tasks` and `ai_task_occurrences` with CHECK constraints, attempt bounds, payload bounds, retry-state constraint, unique occurrence identity, indexes, and RLS with SELECT-only anon access.
- The interrupted-recovery boundary requires **no schema change**: existing `status`, `attempt`, and `retry_at` columns plus the existing transition map suffice.
- Live Supabase state: NOT PROVEN BY CURRENT SOURCE (no live verification performed in this investigation).

## Security / Ownership Status

- Owner identity flows only from `RuntimeSupervisor`/handler context; candidate data cannot override it.
- All repository operations are owner-filtered in both implementations.
- No arbitrary Telegram RPC, SQL/RPC, shell execution, persisted-code execution, or provider bypass exists in the task system.
- Tool actions are data resolved exclusively through `ToolRegistry`/`ToolExecutor`.
- Model-supplied owner identity: not present; interpreter schema excludes owner fields.

## Test / Validation Status

- Task-system tests present and green in the tree: `tests/test_task_repository.py`, `tests/test_task_scheduler.py`, `tests/test_task_execution.py`, `tests/test_task_management.py`, `tests/test_retry.py`, `tests/test_stage13.py` … `tests/test_stage17.py`.
- Most recent full-suite run recorded for this tree: **1155 passed, 23 skipped, 1 warning**; focused task tests: **17 passed** (recorded in `IMPLEMENTATION_REPORT.md`).
- Not verified in this investigation: live Telegram, live Supabase, cross-process crash behavior, external tool side effects.

## What Was NOT Verified

- Live Telegram delivery and live Supabase persistence/RLS behavior.
- Real process-crash/restart behavior in a deployed environment.
- Exactly-once external side effects (not claimed; at-least-once only).
- Whether any deployed database already applied the task migration.

## Final Verdict

The previously delivered retry integration is complete and synchronized; the stale prior investigation claiming otherwise is superseded by current source. One genuine core gap remains: interrupted occurrences are marked but never resolved, violating restart-safe durability. The next implementation boundary is therefore **"Restart-safe interrupted occurrence recovery"**, implemented standalone within the existing scheduler/repository boundaries, with no schema change. All other absent capabilities are optional UX unless a product requirement outside the current source declares otherwise.

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

- Commit status: `7dfeb69b43ac3ebabb45ee5047938e31ef493453` — documentation-only commit containing exactly `INVESTIGATION.md`.
- Push status: SUCCESS — pushed to `origin/main` and verified via `git fetch` + `git rev-parse origin/main`.
- Local HEAD: `7dfeb69b43ac3ebabb45ee5047938e31ef493453`
- Remote HEAD: `7dfeb69b43ac3ebabb45ee5047938e31ef493453`
- Local/remote equality: YES
- Final working-tree status: `tests/test_stage13.py` remains modified and uncommitted (pre-existing unrelated change, preserved). No other modifications remain.
