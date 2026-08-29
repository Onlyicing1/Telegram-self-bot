# INVESTIGATION

## INVESTIGATION METADATA

- Repository: `https://github.com/Onlyicing1/Telegram-self-bot`
- Branch: `main`
- Date: 2026-08-29
- Starting commit: `e594a497a8f5b552f4a8244dc69aa4b0d89a1988`
- Phase: investigation and product/architecture decision freeze only

## 1. PROBLEM

LifeOS has no durable AI Task/Scheduler system. The requested feature must turn owner-approved natural-language requests into durable, restart-safe scheduled plans without bypassing the existing AI, tool, owner, or Telegram boundaries.

## 2. CURRENT ARCHITECTURE

The application is a single Python 3.11 asyncio process. `RuntimeSupervisor` owns the self-client lifecycle, recovery, helper, profile resumption, watchdogs, and shutdown. The AI path is `ai_unified` → `Engine` → `Dispatcher` → `ProviderManager` → `EngineResult`; registered tools are executed only by `ToolExecutor`, and Telegram remains behind the self-client/TelegramAPI authority.

There is no task parser, task creation tool, task repository, generic scheduler, execution-attempt model, or scheduled-task handler. `IMPLEMENTATION_REPORT.md` confirms the unrelated table-delivery work and was not changed.

## 3. CURRENT SCHEDULER / RUNTIME

`backend/profile/scheduler.py` is a process-memory singleton dedicated to Bio and Username. It keeps an asyncio task, client, updater callbacks, and active-engine flags; waits for minute boundaries; updates profile fields; and has cancellation/restart behavior specific to profile updates. It is not generic and must remain separate.

`RuntimeSupervisor` provides the correct lifecycle authority and supervised task primitives. Existing heartbeat, keepalive, failsafe, diagnostics, and retry/watchdog loops are health/recovery infrastructure, not business scheduling. A future TaskScheduler must be one supervisor-owned process-wide loop, with no per-task forever loops and no second recovery authority.

## 4. CURRENT DATABASE / PERSISTENCE

`DATABASE_ARCHITECTURE.md` documents owner-scoped core and AI tables, service-role backend writes, RLS for client-facing reads, and in-memory fallbacks. Existing state covers saves, profile engines, settings, AI configuration, sessions/messages, memories, usage, provider statistics, and tool history. No existing table represents a durable schedule, due time, occurrence key, claim, retry, or task lifecycle.

Conversation/session and tool-history persistence cannot be repurposed as task authority: they do not define schedule semantics or atomic occurrence uniqueness. No SQL was executed and no schema was changed.

## 5. NATURAL LANGUAGE → TASK FLOW

Current owner requests enter through `ai_unified` and the existing Engine/Dispatcher/provider path. The future contract is:

```text
owner message → AI structured proposal → deterministic validator
→ owner-scoped task persistence → TaskScheduler due claim
→ deterministic action validation → ToolExecutor → TelegramAPI/self-client
→ durable execution result → explicit owner notification
```

AI may construct a structured proposal, but it must not create timers, persist unvalidated executable JSON, call Telegram directly, or bypass ToolExecutor. Schedule parsing/normalization, limits, timezone validation, action allowlisting, destination validation, and ownership checks belong to deterministic application code.

## 6. TASK MODEL

Keep these concepts distinct:

- **Definition:** owner-owned, versioned desired plan and lifecycle state.
- **Schedule:** normalized recurrence/one-shot rule and IANA timezone.
- **Occurrence/execution:** one deterministic scheduled occurrence and its attempt state.
- **Result:** success, failure, cancellation, retry state, timestamps, and bounded safe metadata.
- **Runtime state:** transient wakeup/cache/semaphore state only.

Initial definitions contain a bounded ordered JSON action list. Every action names an existing allowlisted tool and validated arguments. The authoritative owner ID is obtained from the authenticated owner context, never from model-provided identity. Destinations are explicit and validated.

## 7. SCHEDULING MODEL

One process-wide `TaskScheduler` is owned and started/stopped/rebuilt by `RuntimeSupervisor`. It loads enabled definitions, wakes for the nearest due task with bounded polling, atomically claims occurrences, and executes outside database transactions.

### Frozen decisions

1. **Missed runs — FROZEN:** for recurring tasks, coalesce all overdue occurrences into at most one catch-up occurrence on restart/recovery, then advance from the latest scheduled occurrence. One-shot tasks that are overdue but still within the configured grace window run once; expired one-shots become terminal `expired`. No unbounded replay or backlog.

2. **DST — FROZEN:** store an IANA timezone and normalize persisted execution timestamps to UTC. A nonexistent local wall-clock time is shifted forward to the first valid local instant after the gap. An ambiguous local time chooses the earlier UTC occurrence (`fold=0`). The policy is deterministic and never uses the machine timezone implicitly. The chosen normalized instant is persisted as the occurrence identity.

3. **Interval anchoring — FROZEN:** intervals anchor to the scheduled occurrence timeline, not completion time. The next occurrence is calculated from the prior scheduled timestamp and advances by the interval until it is in the future or reaches the bounded catch-up rule. Slow execution therefore does not silently drift the schedule.

4. **Retries — FROZEN:** an occurrence has at most three total attempts, including the initial attempt. Retry only deterministic/transient execution failures classified by the execution layer; validation, authorization, unsupported-tool, and malformed-data failures are terminal and not retried. Backoff is bounded and deterministic: 1 minute after attempt 1 and 5 minutes after attempt 2, capped by the next scheduled occurrence. No infinite retry.

5. **Destructive/admin actions — FROZEN:** confirmation-required, admin-only, and destructive unattended actions are excluded from initial scheduled scope. Immediate-message owner authorization is not treated as standing authorization. Supporting them later requires an explicit reviewed pre-authorization model and separate tests.

6. **Notifications — FROZEN:** each task stores or references an explicit owner-scoped destination validated at creation. Default destination may be the authenticated owner's configured Saved/notification chat only if an existing setting contract is later approved; otherwise creation requires an explicit destination. Task outcome and notification outcome are separate: a failed notification never changes action success, and a successful notification never masks action failure. No arbitrary destination inference.

7. **One-shot failure — FROZEN:** after the bounded retry budget is exhausted, a one-shot enters terminal `failed` with its execution history retained and the definition disabled/completed according to the task lifecycle. It is never silently deleted. Manual retry, if later added, creates a new explicit occurrence rather than silently replaying an old one.

8. **Versioning/edit behavior — FROZEN:** definitions have a monotonic version. Editing creates the next version through compare-and-set. An already-claimed occurrence snapshots the definition version/action JSON used for that occurrence; later edits affect only unclaimed occurrences. Disable/delete prevents future claims but does not rewrite history or silently cancel an already-running occurrence.

9. **Conditional workflows — FROZEN:** deferred. Initial tasks are fixed, bounded, ordered action lists only. No conditional branches, loops, model re-planning, or arbitrary workflow language.

10. **Two durable tables — FROZEN:** sufficient initially: task definitions plus execution occurrences/attempt history. Actions remain bounded versioned JSON in the definition; a step table is deferred until resume-from-step or detailed per-step audit is a demonstrated requirement.

## 8. EXECUTION MODEL

The scheduler claims an occurrence with a unique `(task_id, occurrence_key)`, validates the immutable snapshot and owner, then invokes bounded actions sequentially through ToolRegistry/ToolExecutor. Initial multi-action behavior is stop-on-first-failure. The scheduler never calls providers, arbitrary Telethon RPC, SQL, shell, or natural-language commands.

Execution state must distinguish claimed/running/succeeded/failed/retry-pending/cancelled/expired. Result and error metadata are bounded and must not contain secrets. Notification is an owner-only, separately observable operation.

## 9. RESTART / RECOVERY MODEL

Durable definitions and occurrences are authoritative. On startup/recovery, the scheduler reloads enabled definitions and reconciles `running` attempts left by a crash as interrupted/unknown; they are retried only under the frozen bounded policy or terminally failed. Success is recorded only after ToolExecutor reports completion. A crash after Telegram side effects but before durable success can duplicate effects; the system therefore promises at-least-once behavior, not exactly-once Telegram side effects.

Offline due work follows the one-catch-up/expiry policy. Multiple due tasks are ordered deterministically by scheduled time then task ID and bounded by a global execution limit. No distributed leader election is required for the current single-process deployment, though atomic claims remain mandatory.

## 10. CONCURRENCY / LOCKING MODEL

Reuse supervisor recovery locks/task guards and ToolExecutor's sequential behavior, but add a dedicated scheduler singleton guard, bounded execution semaphore (initially one), and atomic repository claim operations. A task cannot overlap itself in the initial scope. Disable/delete and finalization use owner/version/status compare-and-set. Database locks are not held during Telegram calls.

## 11. MULTI-ACTION MODEL

A task contains a bounded ordered list of existing safe tool calls. Actions execute sequentially and stop on the first failure. Aggregate occurrence history stores bounded per-action summaries; a separate step table is not initially required. Conditional actions, loops, dynamic branching, and AI replanning are explicitly deferred.

## 12. SECURITY MODEL

Owner-only outgoing handling remains the authorization boundary. Persisted owner IDs, destinations, schedules, action names, arguments, and JSON versions are validated on creation and again on load. ToolRegistry/ToolExecutor permissions, limits, timeouts, and confirmation semantics remain mandatory. Prompt-injected text in labels or arguments cannot alter the validated action name or privilege. Self Bot/TelegramAPI remains the only Telegram execution authority.

Scheduled dangerous/admin operations are rejected initially. No arbitrary shell, SQL, RPC, provider, or command executor is permitted. Database writes use existing backend service-role conventions; owner-scoped RLS/read rules must be designed in the schema phase.

## 13. EXISTING FEATURE INTEGRATION

Bio/Username remain on `profile.scheduler`. Save, Deep Save, Delete, Ghost Seen, and other features have no automatic scheduler dependency; only individually reviewed safe tools may be scheduled. Deep Save and destructive tools require separate policy review. ProviderManager, Dispatcher, Engine, and EngineResult remain proposal/immediate-execution infrastructure, not scheduler owners. RuntimeSupervisor owns scheduler lifecycle. Existing settings may supply a timezone default only after explicit precedence is implemented. Health/diagnostics may expose bounded telemetry without becoming authority.

## 14. DATABASE / SCHEMA REQUIREMENTS

The next schema phase requires exactly two new durable concepts:

1. **Task definitions:** owner ID, stable task ID, label, enabled/lifecycle status, monotonic version, normalized schedule payload/type, IANA timezone, next scheduled UTC instant, bounded versioned action JSON, explicit notification destination, created/updated timestamps, and optional terminal timestamp.
2. **Execution occurrences/attempts:** task ID/owner ID, immutable occurrence key, definition version and action snapshot/reference, scheduled UTC instant, attempt number, status, claim/start/finish timestamps, bounded error/result metadata, and retry timing.

Required constraints include owner/task consistency, valid status/schedule values, bounded JSON/action size, unique `(task_id, occurrence_key)`, and indexes for enabled due definitions plus owner/task history. Exact SQL types, RLS, retention, foreign keys, and fallback behavior require schema review. The database stores state/history only and never executes business logic.

## 15. CONFIRMED FINDINGS

- `RuntimeSupervisor` is the single lifecycle/recovery authority.
- `profile.scheduler` is a separate in-memory Bio/Username scheduler and is not generic.
- No generic AI task scheduler, task persistence, occurrence history, or task creation path exists.
- ToolExecutor is the sole registered-tool execution component and must remain in the scheduled path.
- Existing persistence conventions provide service-role writes/fallback patterns but no suitable task authority.
- Restart-safe scheduling requires durable definitions and occurrence/attempt state.
- The Self Bot remains the final Telegram execution authority.

## 16. LIKELY / PROPOSED FINDINGS

- A singleton supervisor-owned loop with atomic database claims is the smallest compatible design.
- Two tables with bounded versioned action JSON are sufficient initially.
- Initial semantics should be bounded, sequential, stop-on-first-failure, at-least-once, and exclude dangerous actions.
- The task proposal should be a structured ToolExecutor-mediated operation followed by deterministic validation and persistence.

## 17. UNKNOWN / REQUIRES IMPLEMENTATION VERIFICATION

- Exact live Supabase schema/RLS state and available owner foreign-key relationships.
- Exact existing configured notification destination setting; no current source proves a task-specific default.
- Whether production will ever run multiple instances.
- Exact transient/permanent error classification exposed by every existing tool.
- Whether aggregate action snapshots provide enough audit detail in practice.
- DST library behavior at all historical timezone transition anomalies requires focused implementation tests.

## 18. FINAL FROZEN INITIAL SCOPE

Implement only owner-scoped one-shot, positive interval, daily, and weekly tasks; explicit IANA timezone; bounded catch-up/expiry; at-most-three-attempt retry; versioned bounded ordered safe-tool JSON actions; explicit destination; sequential stop-on-first-failure execution through ToolExecutor; durable claims and history; supervisor-owned lifecycle; and owner-only outcome notification.

No conditionals, loops, dynamic workflows, dangerous/admin actions, arbitrary commands, unbounded replay/retry, or exactly-once guarantee.

## 19. IMPLEMENTATION PHASE ORDER

1. Review and approve the frozen decisions and exact schema contract.
2. Add only the two task/occurrence migrations and owner-scoped repositories with in-memory fallback.
3. Implement schedule value objects and deterministic validation/next-run/DST/missed-run calculations.
4. Implement atomic occurrence creation/claim/finalization and bounded retry transitions.
5. Implement the singleton TaskScheduler and RuntimeSupervisor lifecycle/recovery hooks.
6. Add a structured task proposal/creation tool with deterministic validation and safe-tool allowlist.
7. Execute versioned action snapshots sequentially through ToolExecutor and persist outcomes.
8. Add owner-only management handlers/notifications and bounded diagnostics.
9. Add integration and restart/concurrency tests before enabling production behavior.

## 20. TEST / VALIDATION PLAN

Future tests must cover schedule normalization, timezone/DST boundaries, missed-run coalescing/expiry, interval anchoring, version compare-and-set, duplicate occurrence claims, restart reconciliation, retry cap/backoff, cancellation/disable races, owner isolation, malformed JSON rejection, action limits, dangerous-tool rejection, ordered stop-on-failure execution, ToolExecutor reuse, supervisor lifecycle without duplicate loops, fake Telegram authority, notification independence, Supabase fallback, and bounded retention.

## 21. HARD CONSTRAINTS

No second recovery authority; no per-task forever loops; no direct Telegram RPC from scheduler/AI; no provider/Engine/Dispatcher bypass; no arbitrary shell/SQL/RPC/natural-language execution; no unvalidated persisted JSON; no unbounded backlog/actions/retries/resources; no exactly-once side-effect claim; profile scheduler remains separate; database stores state/history only; owner scope is authoritative.

## 22. PHASE BOUNDARY

This phase was investigation and decision freeze only. No production code, tests, database schema, migrations, SQL, Supabase state, providers, Engine, Dispatcher, ToolExecutor, handlers, scheduler, or Telegram execution behavior was modified.
