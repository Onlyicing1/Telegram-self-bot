# IMPLEMENTATION REPORT — CURRENT STATE

> **This is a CURRENT-STATE document.** It describes the repository as it
> exists at the tip of THIS phase. It replaced the previous phase reports
> rather than appending to them. If code changes invalidate any section,
> update this document in the same commit.

---

## 1. Objective

Repair seven independently reported Taskloom/task-system defects from the
actual source, without redesigning the architecture:

1. scheduled tasks execute late / irregularly / not at all;
2. natural-language task management (create/pause/resume/delete/list/inspect)
   does not work reliably from the first message;
3. Taskloom wizard **Back** leaves the wizard and jumps to Taskloom home;
4. manually opened tasks have no **Edit** option;
5. send-message editing must keep the project's existing Unicode/font
   capability;
6. all recurring Bio task executions fail with a PostgREST schema error
   (`PGRST204 … 'preparation_metadata' column …`);
7. the long-lived runtime coroutines in the watchdog dumps must not be
   reported as starvation.

## 2. Implementation phase

`Phase: task-scheduler / first-message task management / Taskloom edit repair`
(starting HEAD `40b3fb7d3a0e79f44d7cf435d0238777083e168d`).

## 3. Root causes (each traced from source)

### 3.1 Scheduling latency, missing runs, starvation (problem 1)

`TaskScheduler.run_once()` processed due tasks **sequentially**, and each
task's `next_run_at` was advanced only **after** its full execution
(`TaskExecutionCoordinator.execute` = up to `MAX_EXECUTION_SECONDS = 60 s`,
plus AI preparation of up to `3 × MAX_PREPARATION_SECONDS = 45 s`). So:

* a slow task blocked every other due task in the same wake (head-of-line
  blocking → multi-minute lateness, and the recurring boundaries of the
  blocked tasks were served late);
* `run()` slept a fixed `WAKE_INTERVAL_SECONDS = 60 s` *after* the sweep, so
  even a fast sweep could not serve a boundary that landed just after it;
* `MAX_TASKS_PER_WAKE = 10` with a single batch per wake meant a task due
  behind ten others waited a whole poll interval (starvation);
* a task was re-selected while its execution was still running because its
  boundary had not moved yet.

The advance itself was already **correct**: `catch_up_occurrence()` advances
from the persisted SCHEDULED boundary, never from the execution finish time,
so the cadence was not being shifted — it was being *delayed*.

### 3.2 First-message task management (problem 2)

`Dispatcher._read_results_authoritative()` treated a round that executed only
`task_list` as final: it replaced the response with the verbatim tool output
and **broke the tool loop immediately** (`_VERBATIM_READ_TOOLS = {get_bio,
task_list}`). But `task_transition` / `task_delete` require the task's CURRENT
version, which only `task_list`/`task_inspect` can supply. Therefore
"pause task 11" resolved to a single `task_list` round, printed the list, and
the mutation was never requested again — while "show my tasks" (the reason the
verbatim rule exists) must still deliver the tool output untouched.

A second, smaller defect: `TaskTransitionTool` only read the `action`
argument, while the JSON-action contract (and the prompt's own examples) use
`action_status`; and a stale version produced no current version for the
follow-up round.

### 3.3 Wizard Back (problem 3)

`_wizard_render()` appended the generic panel navigation (`_nav(builder)` →
`panel:_nav:back` / `panel:_nav:home`) to **every** wizard step, and
`panels._finalize_panel()` injects those same buttons whenever a panel
supplies none. `panel:_nav:back` pops the panel nav stack, whose previous
frame is the Taskloom list — so the wizard's own "← Back" and the shared
footer Back both jumped out of the wizard (and `_handle_input`'s prompt
offered the same stack-popping Back while a field was being entered).

### 3.4 No task editing (problem 4)

The Taskloom detail panel had pause/resume/complete/delete/refresh only, and
`TaskManagementService` had no definition-edit operation — `set_status()`
writes status, never schedule/actions/instruction.

### 3.5 Unicode/font capability (problem 5)

The canonical display-font registry is `backend/helper/font_style.py`
(`FONT_KEYS`, `apply_font`), used by the Glass UI/dashboard only. Nothing
carried a font choice into a scheduled `send_message` definition, and
`TaskCandidate._canonicalize_action()` deliberately dropped every argument
except `text`.

### 3.6 `PGRST204 'preparation_metadata'` (problem 6)

`20260829000001_create_ai_tasks.sql` creates `ai_task_occurrences` with
`CREATE TABLE IF NOT EXISTS`, and `preparation_metadata` was added to **that
same file** in commit `164ccc2`. On the production database the table already
existed, so the modified `CREATE TABLE IF NOT EXISTS` was a no-op and the
column was never created. The application then failed every occurrence
transition that carried the column.

That is not cosmetic:

* `TaskExecutionCoordinator.execute()` writes the terminal state together
  with the (diagnostic) `preparation_metadata` audit record on **one** update;
* with the column missing, the durable write of the *status* failed too, the
  occurrence stayed `running`, recovery converted it to `interrupted →
  retry_pending`, and the already-completed Telegram side effect could be
  executed again;
* `prepare_ahead()` could not persist its prepared action either, so every
  prepared boundary was re-prepared (or replayed), generating the repeated
  warning pairs in the Render logs.

### 3.7 Watchdog classification (problem 7)

Already correct at HEAD: `backend/runtime/diagnostics.py` classifies
`lifeos-task-scheduler`, `lifeos-profile-scheduler`, `lifeos-run`,
`lifeos-helper` and the Telethon `_update_loop` / `_recv_loop` / `_send_loop` /
`mtprotosender` coroutines as PERMANENT and excludes them from
`TASK_NO_PROGRESS` / `TASK_STARVATION`, and `backend/health.py::set_heartbeat`
is the live writer of `_last_heartbeat` (called every heartbeat tick). **No
change was needed**; the existing tests (`tests/test_07_diagnostics.py`,
`tests/test_runtime_diagnostics_classification.py`) cover it and pass.

## 4. Files changed

| File | Change |
|---|---|
| `backend/ai/task_scheduler.py` | bounded concurrent execution, batch sweeps, sleep-until-nearest-due |
| `backend/ai/database/task_repository.py` | `next_run_hint()`, `discard_unstarted_occurrences()`, schema-drift-safe occurrence transition |
| `backend/ai/engine/dispatcher.py` | defer the verbatim short-circuit for CAS-read rounds |
| `backend/ai/tools/task_management_tools.py` | accept `action_status`, report the current version on a stale CAS |
| `backend/bot/handlers/taskloom.py` | wizard-owned navigation footer, Edit entry + edit mode, font field |
| `backend/helper/panels.py` | input prompt no longer offers a panel-stack Back |
| `backend/ai/task_wizard.py` | edit-mode draft (`draft_from_task`), font field + preview |
| `backend/ai/task_management.py` | `update_definition()` (CAS definition edit + future-occurrence invalidation) |
| `backend/ai/task_creation.py` | shared `initial_next_run()` (one boundary calculation for create + edit) |
| `backend/ai/task_candidate.py` | allow-list-validated `font` on the message action |
| `backend/ai/tools/message.py` | bounded `font` parameter; canonical transform at send time |
| `supabase/migrations/20260912000001_add_ai_task_occurrences_preparation_metadata.sql` | **new** idempotent column repair |
| `DATABASE_ARCHITECTURE.md` | records the migration and the drift |
| `tests/test_task_reliability_repair.py` | **new** 57 focused regressions |
| `tests/test_20_advanced_execution.py`, `tests/test_task_list_consistency.py` | updated to the repaired, still-safety-preserving contract |

## 5. Behaviour changed

### 5.1 Scheduler timing semantics

* Every due task in a wake is served: sweeps repeat in batches of
  `MAX_TASKS_PER_WAKE` up to `MAX_SWEEPS_PER_WAKE` (200 due tasks/wake), so a
  task due behind ten others is no longer deferred a poll interval.
* Within a sweep, tasks run with bounded concurrency
  (`MAX_CONCURRENT_EXECUTIONS = 4`) via the existing per-occurrence claim CAS
  — one slow execution no longer serializes the others.
* `run()` sleeps `min(WAKE_INTERVAL_SECONDS, max(MIN_WAKE_SECONDS, time to the
  nearest known boundary))` using the new advisory `next_run_hint()`; failures
  of the hint (or no active task) fall back to the plain 60 s poll, so retries
  can never be starved.
* The boundary advance keeps its exact previous position in the per-task
  sequence (create occurrence → execute → advance → prepare-ahead). **This is
  load-bearing**: `advance_next_run` bumps the task's CAS version, and a
  durably prepared action is stamped with the version that must still be
  current when the boundary executes.
* `catch_up_occurrence()` remains the only boundary calculation; a delayed
  wake never shifts the cadence to the execution time.

### 5.2 First-message task-management semantics

* A round that executed **only** `task_list`/`task_inspect` now gets one
  continuation round instead of the immediate verbatim short-circuit, so
  `task_list → task_transition` / `task_delete` can complete in one request.
  If that continuation produces no tool call, the verbatim tool output is
  delivered exactly as before (the anti-paraphrase guarantee is unchanged;
  `tests/test_task_list_consistency.py` still proves the fabricated narration
  never reaches the owner).
* `TaskTransitionTool` accepts `action` **and** `action_status`.
* A stale CAS now reports the task's current version
  (`current_version` in `data`, `… retry with expected_version=N` in the
  message) so the next round can finish deterministically.
* Destructive safety is unchanged: owner scoping, required `task_id` +
  `expected_version`, `CAS` transitions, real row-removal deletion, and
  rejection of a missing/ambiguous target all still hold.

### 5.3 Taskloom Back / input semantics

* The wizard renders its **own** footer (`✕ Cancel` → Taskloom,
  `❌ Close` → close panel) and never emits `panel:_nav:back`, so the shared
  finalizer cannot inject a stack-popping Back.
* Back is always the wizard's previous step, rendered as a draft step change:

  | Step | Back goes to |
  |---|---|
  | Action | *(no Back — Cancel is the explicit exit)* |
  | Content | Action |
  | Content details | Content (AI) / Action (static) |
  | Schedule | Content details |
  | Review | Schedule |

* Submitting an input updates only that field, keeps `draft.step`, and
  re-renders the same step; unrelated draft values survive.
* The shared input prompt no longer offers a panel-stack Back (its `Cancel`
  already returns to the owning panel).
* `Cancel` still returns to Taskloom; `Close` closes; **Back never does
  either**.

### 5.4 Edit semantics

* The task detail panel has `✎ Edit` → the SAME wizard, prefilled from the
  STORED definition only (`draft_from_task`), then persisted through the SAME
  CAS update path: `TaskManagementService.update_definition()` →
  `repository.update_task(expected_version=…)`.
* Only representable definitions can be edited; event-triggered or
  unknown-action tasks are refused with an explicit message (no silent
  conversion).
* Every successful edit: uses `expected_version`, increments the version
  exactly once (`update_task` is the only writer), recomputes `next_run_at`
  from the NEW schedule through the shared `initial_next_run()`, keeps the
  task id and the stored destination when the edit chose none, and discards
  **future, never-started** occurrences (`discard_unstarted_occurrences`)
  so the next boundary runs the new definition instead of a stale snapshot.
* Started/terminal occurrences are never touched: history stays an immutable
  snapshot of its own `definition_version` / `action_snapshot`.
* A stale form (the task changed after the wizard opened) writes nothing and
  says so.

### 5.5 Unicode/font behaviour

* The message action carries an optional `font` key validated against the
  canonical registry (`is_valid_font`); the stored text stays RAW.
* The send tool applies `font_style.apply_font(text, key)` at execution time,
  so scheduled execution is deterministic and re-editing a styled task never
  double-styles it. No second Unicode subsystem was introduced.

### 5.6 Bio execution path and schema honesty

* The durable status transition is retried **without** the optional
  `preparation_metadata` audit field when (and only when) PostgREST reports an
  unknown-column/PGRST204 error **and** the status actually changes. The
  durable state is then persisted truthfully and the dropped diagnostics field
  is reported once per episode
  (`TASK_OCCURRENCE_AUDIT_FIELD_DROPPED … durable_state_transition=persisted`).
* The repository is *not* marked degraded by that case, so a healthy store is
  not reported as unavailable.
* A drift on any other (required) column is never stripped: the transition
  degrades honestly (`fallback_active=True`, non-durable) exactly as before.
* `prepare_ahead()`'s same-status write (which carries only the prepared
  action) is never retried without the field — dropping it would claim a
  durable preparation that never happened.
* Bio Guardian policy, preparation policy, source attribution, language and
  length validation, attempt limits, occurrence uniqueness and the fallback
  contract are unchanged.

## 6. `preparation_metadata` schema status

* The repository migration **does** declare the column
  (`20260829000001_create_ai_tasks.sql`), but only inside
  `CREATE TABLE IF NOT EXISTS`; the production table predates it, so the live
  database is **behind** the repository schema. This was **not** verified
  against the live database from this workspace.
* Repository-side repair added (idempotent, safe to apply twice):
  `supabase/migrations/20260912000001_add_ai_task_occurrences_preparation_metadata.sql`.
* **Manual SQL the operator must apply** (Supabase SQL editor):

```sql
ALTER TABLE ai_task_occurrences
    ADD COLUMN IF NOT EXISTS preparation_metadata jsonb NOT NULL DEFAULT '{}';

ALTER TABLE ai_task_occurrences
    DROP CONSTRAINT IF EXISTS ai_task_occurrences_preparation_metadata_object;
ALTER TABLE ai_task_occurrences
    ADD CONSTRAINT ai_task_occurrences_preparation_metadata_object
    CHECK (jsonb_typeof(preparation_metadata) = 'object');

ALTER TABLE ai_task_occurrences
    DROP CONSTRAINT IF EXISTS ai_task_occurrences_preparation_size;
ALTER TABLE ai_task_occurrences
    ADD CONSTRAINT ai_task_occurrences_preparation_size
    CHECK (octet_length(preparation_metadata::text) <= 8192);

NOTIFY pgrst, 'reload schema';
```

* **Rollback** (only if the column is truly unused):

```sql
ALTER TABLE ai_task_occurrences DROP CONSTRAINT IF EXISTS ai_task_occurrences_preparation_size;
ALTER TABLE ai_task_occurrences DROP CONSTRAINT IF EXISTS ai_task_occurrences_preparation_metadata_object;
ALTER TABLE ai_task_occurrences DROP COLUMN IF EXISTS preparation_metadata;
```

* Database/migration impact: **one additive migration**, no schema
  restructuring, no new table, no index change, no RLS change, no data
  mutation. No SQL was executed against the user's Supabase instance from
  this workspace.

## 7. Tests

Added `tests/test_task_reliability_repair.py` — **57 focused behavioural
tests**:

* scheduler: no early execution, cadence preserved after a late wake, 25 due
  tasks in one wake, concurrency proof (measured overlap + elapsed bound), a
  definition edit that changes only the future schedule, `retry_at` honoured,
  bounded sleep-until-nearest-boundary;
* first-message management: read→mutate continuation executes the requested
  transition, read-only request still verbatim, JSON `action_status` mapping,
  missing/ambiguous targets rejected, stale CAS reports the current version
  (transition + delete), `action_status` accepted by the tool;
* wizard: no `panel:_nav:back` on any step, exactly one Back per step with the
  correct target, Cancel/Close differ from Back, every input keeps its step
  and the rest of the draft, Back from Review preserves the draft, input
  prompt has no stack-popping Back;
* edit: `✎ Edit` entry present, end-to-end wizard edit updates the SAME task
  (version +1, no second task), stale form refused, future unstarted
  occurrences discarded while history is preserved, review shows the font,
  raw text + font preserved, invalid font rejected, canonical transform
  applied at send time, `draft_from_task` prefill + refusal of
  unrepresentable schedules;
* schema/fallback: missing audit column keeps the durable transition (and is
  logged), missing required column is never stripped, same-status write is not
  stripped, genuine store failure still classifies as `unavailable`, local
  resource failure still classifies as `local_resource`, `next_run_hint` is
  advisory and never degrades.

Test results actually executed:

* `pytest tests/test_task_reliability_repair.py -q` → **57 passed**
* `pytest tests -q` → **2300 passed, 24 skipped, 0 failed** (67 s)
* `py_compile` on every changed Python file → **OK**
* `git diff --check` → **clean**

Two existing tests were updated, both because the *contract they assert was
deliberately repaired* (not weakened):

* `tests/test_task_list_consistency.py` — the task-list round now costs one
  continuation round; the assertion still proves the fabricated narration
  never reaches the owner.
* `tests/test_20_advanced_execution.py` — the `send_message` guard now expects
  `{text, font}` and additionally asserts the font enum is EXACTLY the
  canonical registry, i.e. no arbitrary value can reach Telegram.

## 8. Live verification status

**Live Telegram / Render verification was NOT performed.** There is no
production session available in this workspace. Everything above was verified
in-process (unit/behavioural tests) against the real dispatcher, scheduler,
repository, wizard and tool boundaries. The production Supabase schema is
**not** verified; the manual SQL in §6 remains outstanding, and the
application-side schema-drift tolerance is what keeps task execution honest
until it is applied.

## 9. Remaining limitations

1. A pure "list my tasks" request now costs one extra provider round (the
   deferral is the price of read→mutate requests finishing in one message).
2. Edit mode covers Bio / Username / send-message definitions with
   once/interval/daily/weekly schedules. Event-triggered tasks and
   unknown-action tasks must still be recreated (and say so honestly).
3. An interval whose stored value is not a whole number of minutes must be
   re-entered during an edit (the wizard's unit is minutes).
4. If the durable store is genuinely unreachable at the moment a Telegram
   action has already succeeded, the terminal state still cannot be persisted
   and recovery may retry that occurrence; this phase removes the *observed*
   cause (the audit column) but cannot make a two-system commit atomic.
5. Preparing ahead keeps the previous per-task order (execute → advance →
   prepare) because the prepared-action version contract depends on it; a very
   slow task can therefore still delay the *next batch* of the same wake, but
   no longer the tasks in its own batch, and no longer the following wake.

## 10. Delivery

| Item | Value |
|---|---|
| Starting HEAD | `40b3fb7d3a0e79f44d7cf435d0238777083e168d` |
| Final HEAD | see the delivery commit recorded below |
| Push result | recorded below |
| Remote HEAD | recorded below |
| Working tree | recorded below |
