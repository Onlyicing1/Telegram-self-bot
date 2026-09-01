# INVESTIGATION

## 1. CURRENT TASKLOOM EXECUTION MODEL

**Confirmed from source.** Durable task creation is routed through the existing task boundary; execution is downstream:

```text
AI request / task tool
  -> TaskInterpreter / TaskCandidate
  -> TaskCreationService.create()
  -> TaskRepository.create_task()
  -> TaskScheduler
  -> occurrence discovery and creation
  -> claim_occurrence()
  -> TaskExecutionCoordinator.execute()
  -> ToolExecutor.execute_calls()
  -> registered tool
  -> TelegramAPI / Self Bot client
```

`TaskScheduler` is the single scheduler. `TaskExecutionCoordinator` receives a claimed, `running` occurrence and executes its immutable `action_snapshot` through the existing `ToolExecutor`. It does not call the provider or interpret natural language. The current execution coordinator therefore performs action execution only; it has no AI-preparation phase.

## 2. CURRENT DURABLE TASK MODEL

**Confirmed from `DATABASE_ARCHITECTURE.md`, `backend/ai/database/task_repository.py`, and the migration contract.**

`ai_tasks` stores one owner-scoped task definition with:

- `label`
- lifecycle `status`
- integer `version`
- `schedule_type`, JSON `schedule`, IANA `timezone`
- UTC-aware `next_run_at`
- bounded JSON `actions` array
- bounded JSON `notification_destination`
- timestamps and optional `terminal_at`

`ai_task_occurrences` stores per-occurrence history with:

- `task_id`, owner, unique `occurrence_key`
- `definition_version`
- immutable bounded `action_snapshot` array
- UTC-aware `scheduled_for`
- attempt/status timestamps
- `retry_at`
- bounded `error_metadata` and `result_metadata`

The repository validates action arrays as 1–5 actions and bounds JSON payload sizes. Occurrence snapshots preserve the task definition version and action payload; later task edits do not rewrite historical snapshots.

The current `actions` and `action_snapshot` contracts represent executable registered tool calls, for example `{"name":"send_message","arguments":{"text":"..."}}`. They can safely represent a final validated action. They do not have a typed, explicit field for an AI instruction, preparation status, prepared timestamp, provider result, or readiness lease. An AI instruction could technically be embedded in an action argument or metadata JSON, but no current executor interprets such a value as a preparation request. Persisting it without an explicit consumer would therefore be inert or unsafe.

The two existing tables are sufficient as the storage boundary in principle: the task row can hold a durable instruction/definition and each occurrence can hold that occurrence's prepared/final snapshot and metadata. The current schema does not, however, explicitly model those concepts. A safe implementation would need either an additive, bounded representation within the existing JSON contracts plus corresponding validators/executors, or minimal additive columns/state approved against the existing migration contract. No third table is required by the current architecture, and no schema change is proposed here.

## 3. CURRENT AI EXECUTION TIMING

**Confirmed from source.** The current scheduler uses `next_run_at` as the due instant. `TaskScheduler` polls on `WAKE_INTERVAL_SECONDS = 60.0`, discovers due tasks, creates/claims occurrences, and hands them to `TaskExecutionCoordinator`. The coordinator starts `ToolExecutor.execute_calls()` only after the occurrence has been claimed and marked `running`.

There is no `preparation_deadline`, `prepare_at`, readiness flag, provider job, or AI work before `scheduled_for` in `TaskScheduler`, `OccurrenceRecord`, or `TaskExecutionCoordinator`. Current provider work belongs to the interactive Dispatcher/provider path, not to scheduled occurrence execution. Therefore, in the current Taskloom execution model, any AI interpretation/preparation needed for a scheduled action is not performed ahead of the due instant by the scheduler. The durable scheduler path only executes its already-present action snapshot after claim.

The scheduler has enough information to calculate a preparation start instant in a future design (`scheduled_for` / task `next_run_at`, task timezone, and the polling interval), but it does not currently do so. A preparation phase must be integrated with the existing occurrence identity and lifecycle rather than creating a second scheduler.

## 4. AI-BACKED TASK MODEL

**Proposed design, grounded in existing boundaries.** Explicit task language should create a durable task definition containing the user's bounded natural-language instruction as data, plus the resolved trusted destination and schedule. The task definition must not be treated as a preselected Telegram method or arbitrary recipient.

At execution time, an AI/provider component may transform the instruction into a candidate action. That candidate must be parsed, bounded, ownership-checked, destination-checked, and resolved to an existing registered tool before the Self Bot can perform a side effect. The provider must never receive direct Telegram, Supabase, SQL, shell, or arbitrary RPC authority.

The durable task definition is distinct from the per-occurrence executable action. For a literal request such as “every minute say X”, a deterministic final `send_message` action may be stored today. For a dynamic request such as “change my bio to a different suitable quote”, the durable instruction must remain available for each occurrence; storing one generated quote permanently as the task definition would change the user's semantics.

## 5. PER-OCCURRENCE PREPARATION

**Architectural recommendation, not implemented.** Dynamic recurring AI work must be prepared once per occurrence, not once per task. Otherwise every recurrence would reuse the same result.

The smallest compatible timing model is:

1. Keep the task's `next_run_at` / occurrence `scheduled_for` as the execution deadline.
2. Derive a preparation start/deadline from that occurrence's scheduled instant and a measured bounded AI latency budget.
3. Have the existing single `TaskScheduler` discover work early enough to invoke the provider preparation path for that occurrence.
4. Persist the prepared, validated action against the same occurrence identity.
5. At `scheduled_for`, claim/run the occurrence and execute the prepared action through `TaskExecutionCoordinator` and `ToolExecutor`.

This is a scheduler extension, not a second scheduler. Preparation should begin no earlier than necessary and must be tied to `task_id`, `occurrence_key`, and `definition_version`. A result prepared 20 seconds before the deadline is usable if it remains valid and the task definition version is unchanged. A result prepared one second before the deadline is also usable, but execution may be late because the existing scheduler wake and Telegram call have their own latency.

If preparation is still running at `scheduled_for`, the system must not execute an unvalidated or absent action. The occurrence should be recorded as late/failed/retryable using the existing failure policy, or remain in a precisely defined non-executing preparation state. The current states do not themselves distinguish this case; see §7.

## 6. PREPARATION VS TELEGRAM EXECUTION

The required boundary remains:

```text
AI/provider reasoning
  -> bounded candidate/result
  -> local validation and trusted destination enforcement
  -> existing ToolExecutor
  -> registered domain tool
  -> TelegramAPI backed by the Self Bot client
```

Preparation may call the provider, but it must not call Telethon or the Telegram API directly. The prepared value must be converted into the same registered action contract consumed by `TaskExecutionCoordinator`. For a bio task, that means the existing registered bio tool; for a message task, the existing bounded `send_message` tool. Destination and owner identity must come from the persisted trusted task context, never from provider output.

Validation belongs before persistence/use of the prepared executable snapshot and again at the existing execution boundary as defense in depth. The database stores state and snapshots; it does not execute business actions.

## 7. OCCURRENCE STATE REQUIREMENTS

**Confirmed from source.** Existing statuses are `claimed`, `running`, `succeeded`, `failed`, `retry_pending`, `cancelled`, `expired`, and `interrupted`. The repository transition table permits transitions appropriate to those states. `claimed` and `running` currently mean scheduler claim/execution lifecycle, not “AI preparation complete”.

These statuses are not sufficient to unambiguously represent “AI preparation completed, but Telegram execution has not started.” Reusing `claimed` or `running` would conflate provider work with side-effect execution and would make restart recovery ambiguous. `retry_pending` describes a failed attempt awaiting retry, not a successfully prepared action waiting for its deadline.

Minimum source-grounded requirement: introduce an explicit durable preparation/readiness distinction, either through a narrowly defined additional occurrence status/state or a bounded, validated preparation state in existing occurrence metadata with clear CAS/transition rules. The latter is only safe if every scheduler/recovery/execution query treats it consistently; the current code does not. This is an implementation decision requiring lifecycle review, not an implemented change.

## 8. PREPARED RESULT / ACTION LIFETIME

**Proposed semantics based on occurrence identity and immutable snapshots.**

- Generate a dynamic AI result once per occurrence.
- Persist only the final validated action plus bounded preparation metadata, not untrusted provider prose as executable data.
- Consume the prepared result once for that occurrence.
- Reuse it for retries of the same occurrence, because rerunning AI could produce a different side effect and violate at-most-one intended occurrence semantics.
- Never reuse it across recurring occurrences; each occurrence gets a new preparation.
- Reject or regenerate a result that is stale relative to its preparation deadline, task `definition_version`, or explicit validity policy.
- If the task definition changes while preparation runs, discard the result unless its version matches the occurrence snapshot.
- A process restart after successful preparation must recover the persisted prepared state and execute it or apply the defined stale/late policy; it must not silently regenerate or lose it.

The existing `definition_version` and unique `(task_id, occurrence_key)` identity are the correct anchors for this behavior, but the current repository has no preparation-specific fields or transitions.

## 9. FAILURE / LATENCY MODEL

**Required behavior; not implemented.**

- Early successful preparation: persist the validated action and mark the occurrence ready; execute at the scheduled deadline.
- Late successful preparation: execute only if the result is still valid and the occurrence has not been superseded; record lateness in bounded result metadata.
- Preparation still running at deadline: do not execute an incomplete result; record a bounded failure/late outcome and apply existing bounded retry policy where appropriate.
- Provider failure or timeout: no Telegram side effect; persist failure metadata and use the existing retry classification/attempt limit rather than an unbounded retry loop.
- Invalid provider candidate: reject it locally; do not fabricate success or execute it.
- Valid preparation followed by Telegram failure: preserve the prepared action for retry of the same occurrence; retry the Telegram execution, not AI generation, unless the action is explicitly marked stale/invalid.
- Pause while preparing: cancellation/commit logic must prevent a paused task from executing a prepared result.
- Delete while preparing: ownership/status/version checks must prevent any later side effect; task deletion must not bypass occurrence history constraints.
- Definition edit while preparing: version mismatch must invalidate the in-flight result.
- Restart after preparation: recovery must see the durable preparation state and resume safely.

The current `TaskExecutionCoordinator` already returns failure rather than claiming success when tool execution or occurrence-state persistence fails, and its retry behavior is bounded by `backend.ai.retry`. Those semantics should be reused rather than replaced.

## 10. SHORT-INTERVAL WARNING

The source contains timing constraints, but no existing production constant that safely defines “too short for reliable AI preparation”. Relevant values include:

- `TaskScheduler.WAKE_INTERVAL_SECONDS = 60.0`.
- `TaskExecutionCoordinator.MAX_EXECUTION_SECONDS = 60.0`.
- Repository external-operation timeout `DB_TIMEOUT = 10.0`.
- Dispatcher/provider request and handler bounds exist in the interactive AI path, but they are not a measured scheduled-AI preparation budget.
- Retry backoff and attempt limits are defined in `backend/ai/retry.py`.

These values are not interchangeable: scheduler polling latency, provider timeout/fallback behavior, validation, persistence, Telegram execution, and clock skew all consume the budget. No threshold should be invented from one constant. The minimum information needed is an observed/p99 provider preparation latency budget (including fallback), scheduler wake/jitter budget, persistence/validation budget, Telegram execution budget, and an explicit lateness tolerance.

Once measured, the warning should apply only to AI-backed tasks whose recurrence interval leaves insufficient preparation margin, and should be shown at creation as a warning without converting a failed task into a success. Persisting the warning is not required by the current contract unless product requirements later require it; the creation response is the smallest existing notification surface.

## 11. COMPLEX AI TASK STRESS TEST

The future request:

> “پری هر ۵ دقیقه تکست بیو من رو به یکی از دیالوگ های ۵۰ و زیر ۵۰ کاراکتری یکی از کاراکتر های کودره انیمه ای تغییر بده”

fits the proposed model conceptually:

- recurring interval schedule in the task definition;
- durable natural-language instruction with length/content constraints;
- one provider preparation per occurrence;
- provider returns a bounded bio candidate, not a Telegram call;
- local validation enforces the character limit and registered bio-tool schema;
- `TaskExecutionCoordinator` invokes the existing registered bio tool through `ToolExecutor`;
- the Self Bot remains the only Telegram authority;
- no third durable table is inherently required.

The exact missing capability is not scheduling or Telegram transport. It is the absence of a durable preparation/readiness contract and an occurrence-aware provider-preparation executor in the current Taskloom model. The existing `action_snapshot` can carry the final validated bio action, but it cannot by itself express the durable instruction plus an in-progress/ready preparation lifecycle to the current scheduler.

## 12. DATABASE PERSISTENCE OBSERVABILITY

The current repository catches broad external failures and falls back to memory. To diagnose missing durable `ai_tasks` inserts safely, trace points belong at these existing boundaries:

1. `TaskCreationService.create()` — creation start, owner-safe task identifier/label hash or length, schedule type, action count, and selected repository class.
2. `SupabaseTaskRepository.create_task()` — validated payload structural summary only, before `_run()`.
3. Immediately around `self._client.table("ai_tasks").insert(payload).execute()` — attempted/succeeded markers and returned row ID only.
4. The Supabase `except Exception` branch — exception class and sanitized message, with no credentials or task contents.
5. Fallback activation — explicit fallback marker and fallback repository class.
6. Final return to the tool/service boundary — persistence outcome (`supabase` vs `fallback`), task ID, and whether the durable row was confirmed.
7. `create_occurrence()` — equivalent markers for `ai_task_occurrences`, including task ID, occurrence key hash, and returned occurrence ID.

The existing `logger.warning("Supabase task create failed; using fallback")` proves fallback behavior exists but does not expose enough structured state to distinguish selected repository, insert attempt, returned row, and final persistence outcome. No live database inspection or SQL was performed here, so production row existence remains unverified.

## 13. EXISTING CONFIRMED BUGS

These are carried forward without re-investigation or implementation:

- **Delete:** deletion is a soft transition to `status="deleted"`, while `list_tasks()` includes deleted records, so deleted tasks remain visible.
- **Timezone:** storage uses UTC-aware timestamps and `taskloom._fmt_dt()` formats directly without converting to the task's IANA timezone, so Asia/Tehran can display UTC-style values.

Neither bug is part of the AI-preparation design.

## 14. CONFIRMED FINDINGS

### Confirmed from source

- There is one `TaskScheduler` and one `TaskExecutionCoordinator`.
- Scheduled execution begins after occurrence claim and uses the immutable `action_snapshot`.
- The coordinator does not currently perform provider work.
- `scheduled_for` and `next_run_at` are UTC-aware instants; no preparation deadline/readiness field exists.
- `actions`/`action_snapshot` are bounded arrays of executable tool-call-shaped JSON.
- `definition_version` and unique occurrence identity support per-occurrence version safety.
- Existing occurrence statuses do not explicitly represent prepared-but-not-executed work.
- Existing retry logic is bounded and belongs to the current execution lifecycle.
- The repository uses owner-scoped validation and Supabase-to-memory fallback.

### Confirmed from existing documentation/tests

- The two-table Taskloom persistence model and action/occurrence bounds are documented in `DATABASE_ARCHITECTURE.md` and enforced by repository validation code.
- Existing tests cover current task creation, occurrence execution, retries, and recovery boundaries, but no existing test establishes a provider-backed per-occurrence preparation lifecycle or a pre-deadline readiness state.

### Observed from user/runtime

- The user requires durable AI instructions, dynamic per-occurrence results, lower execution latency, and a warning for schedules too short for reliable preparation.
- The user reports an existing successful Taskloom execution and separately reports persistence/list/timezone concerns; those live database facts were not independently queried in this investigation.

### Not confirmed

- Actual live Supabase schema/application state and whether any particular production task row exists.
- Provider p99 latency or a safe warning threshold.
- Whether the current deployment has code or migrations different from this repository revision.
- The exact preferred occurrence-state encoding for prepared-but-not-executed work.

### Proposed design

- Persist the natural-language task instruction at task-definition level.
- Prepare dynamic AI results once per occurrence before `scheduled_for`.
- Persist the final validated action and preparation metadata against that occurrence.
- Reuse a prepared result for retries of the same occurrence only.
- Enforce version, owner, destination, schema, and freshness checks before Self Bot execution.
- Extend the existing scheduler/lifecycle minimally; do not add a scheduler, executor, Telegram path, or third table.

## 15. MINIMAL NEXT IMPLEMENTATION SCOPE

1. **Define the existing-model representation:** choose a bounded, versioned representation for a durable AI instruction and per-occurrence preparation metadata without changing unrelated tables or adding a parallel store.
2. **Add the smallest occurrence lifecycle boundary:** represent preparation-in-progress/ready distinctly from Telegram execution, with owner/version/CAS and restart-safe recovery semantics.
3. **Reuse the existing scheduler:** schedule preparation early enough using measured latency budgets, then let the existing `TaskExecutionCoordinator` execute only the persisted validated action at/after the deadline.
4. **Reuse existing tools and validation:** provider output must resolve to registered tool calls; no provider direct Telegram access; failure must remain an honest failure.
5. **Measure before warning:** instrument or derive bounded p99 preparation, scheduler, persistence, and Telegram budgets, then implement a creation-time warning only for AI-backed intervals below the measured safe margin.
6. **Add focused tests:** per-occurrence regeneration, zero Telegram execution before readiness, prepared-result reuse on same-occurrence retry, version invalidation, pause/delete cancellation safety, restart recovery, and warning behavior.

The already-confirmed Delete and timezone fixes should remain separate implementation stages.

## 16. REMAINING UNKNOWN

- Live Supabase availability/schema and the actual cause of any reported missing `ai_tasks` row.
- Provider latency distribution and fallback timing in production.
- Whether the product permits a new explicit occurrence status or requires metadata-only representation.
- Exact stale-result/lateness tolerance and whether late execution is acceptable or must be marked failed.
- Whether prepared-result metadata may safely fit current JSON byte limits for all supported AI actions.
- How an in-flight provider preparation should be cancelled and reconciled against pause/delete/version edits using the runtime's supervised task model.

No code, tests, configuration, schema, SQL, Supabase data, scheduler, UI, or Telegram behavior was modified in this investigation.
