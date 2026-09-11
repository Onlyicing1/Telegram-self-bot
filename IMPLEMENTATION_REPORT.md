# IMPLEMENTATION REPORT — CURRENT STATE

> **This is a CURRENT-STATE document.** It describes the repository as it
> exists at the tip of this phase. If code changes invalidate any section,
> update this document in the same commit.

---

## 1. Implementation metadata

| Item | Value |
|---|---|
| Repository | `Onlyicing1/Telegram-self-bot` |
| Branch | `main` |
| Starting HEAD | `9b4af95e2b74c249bd0771515585ae90cb1f9cb1` (== `origin/main` at phase start) |
| Phase | **Degraded task-transition honesty** — a `task_transition` whose Supabase update degraded into the in-memory fallback was still reported as a plain durable success (the same false-persistence class fixed for `create_task` in the previous phase) |
| Status | **IMPLEMENTED — full suite green (2103 passed, 24 skipped, 0 failed)** |
| Database impact | **NO DATABASE / SCHEMA CHANGE** |
| Live verification | **NOT performed** (no credentials in this workspace) — see §7 |
| Delivery record | see §8 |

---

## 2. Exact root cause (source-verified)

### 2.1 The decisive evidence in the live symptom

The creation was reported as **`Task #3`**, while the task list — two minutes
later — contained **`Task #27`**. Those two ids cannot both come from the
durable store: `ai_tasks.id` is `bigserial` (monotonic), so a table that
already holds `#27` can never hand out `#3`. A fresh low id is the signature of
the **process-local in-memory fallback**, whose counter starts at 1.

So the created task was written to the in-memory fallback, and the list was
served from the durable store — the two stores diverge by design, and the
divergence was never reported.

### 2.2 The failing layer: `SupabaseTaskRepository` write degradation

`backend/ai/database/task_repository.py::SupabaseTaskRepository.create_task`
catches **any** Supabase insert failure and re-creates the task in the shared
in-memory fallback:

```python
except Exception as exc:
    self._mark_fallback()
    record = await self._fallback.create_task(owner_id, data)   # process-local id
    record.fallback_backend = type(self._fallback).__name__
    return record
```

`CreateTaskTool` (`backend/ai/tools/task.py`) then returned a plain, durable
looking result:

```
✅ Task #3 created — Bio update        data={task_id, label, schedule_type, timezone, owner_id, status}
```

`fallback_backend` was only logged/traced — never surfaced in the result. The
owner was therefore told a **durable** task had been created while the task
existed only in process memory. The next list (a *healthy* durable read)
cannot see it: the created active task "disappeared" exactly as reported.

### 2.3 Two further defects on the same request path

**(a) Double read in `TaskListTool`** — the tool read the list twice:

```python
tasks  = await service.list_tasks(status=...)   # read 1 → used for task_count
result = await list_text(service, status=...)   # read 2 → used for the rendered text
```

The two reads can hit different stores and the later one can **clear** the
`fallback_active` marker. Pre-fix reproduction (now a regression test): read 1
fails (marker `True`, `task_count == 0`) → read 2 succeeds (durable content
rendered) → the owner sees a real task beside an empty count and **no**
degraded marker at all. That is precisely "a transient read failure
masquerading as an authoritative list".

**(b) `task_list` was not response-authoritative** — `_VERBATIM_READ_TOOLS`
contained only `get_bio`, so a native `task_list` round still got a
continuation provider round, where the model could answer from stale
conversation context. Task-management requests are deliberately absent from
the deterministic command vocabulary (`backend/ai/actions.py` documents this),
so the **native provider round is the live path** for "list my active tasks" —
and the model could (and live, did) produce "*…it may have completed or been
removed*" over a fresh tool result that said otherwise.

### 2.4 Ruled out (checked in source, not assumed)

| Hypothesis | Verdict |
|---|---|
| Owner scope mismatch | **No.** Creation (`CreateTaskTool`) and listing (`TaskListTool`) both use `context.owner_id`; `TaskManagementService` filters by owner on every call. |
| Different repository instance | **No.** Both use the process-wide singleton `get_repository_manager().task`. |
| Unexpected status transition | **No.** The unfiltered list excludes only `deleted`; a created task is `active` (DB default and in-memory default agree). |
| Supabase schema/payload mismatch | **Not the cause.** `ai_instruction` and every payload field, bound, and CHECK constraint exist in `supabase/migrations/20260829000001_create_ai_tasks.sql` and match `_validate_task_input`. The insert failure is transient (read succeeded moments later). |
| Parser / structured output | **Unrelated** to this path. |

---

## 3. Exact fix (four minimal, source-consistent changes)

### 3.1 One authoritative snapshot (`backend/ai/task_management.py`, `..._interface.py`, `tools/task_management_tools.py`)

- New `TaskListSnapshot(tasks, fallback_active)` and
  `TaskManagementService.snapshot(status)`: the tasks and the degraded marker
  come from the **same** repository read with no `await` between them.
- `list_text(..., snapshot=None)` renders the supplied snapshot when given
  (otherwise it takes exactly one itself).
- `TaskListTool.execute` now performs **one** read and reports
  `task_count`, `task_ids`, and `fallback_active` from that same snapshot —
  identical content, count, and marker by construction.

### 3.2 A non-durable creation can no longer look durable (`backend/ai/tools/task.py`) — and neither can a non-durable transition (`.../task_management_tools.py`)

When the created record came from the in-memory fallback
(`fallback_backend` set), the tool result now says so:

```
✅ Task #N created — <label>

⚠️ Memory fallback — Supabase unavailable (tasks may be missing, and
   anything created now is not durable).
```

plus `data = {..., "durable": false, "fallback_backend": "InMemoryTaskRepository"}`.
A durable creation carries `"durable": true` and no note. The graceful
fallback architecture is preserved — creation still succeeds — but the owner
is never told a memory-only task is persisted.

**Current-phase extension (same class, sibling path):** the sweep for further
instances of this defect class found `TaskTransitionTool` (pause / resume /
complete / delete) reporting a degraded update as a plain durable success:
`SupabaseTaskRepository.update_task` degrades through `_annotate_fallback`
exactly like `create_task`, but the tool never checked the annotation. The
tool now applies the identical contract: `fallback_backend` set → append the
shared `FALLBACK_NOTE` and report `"durable": false`; otherwise
`"durable": true` with no note. The honest-failure semantics the tests
exposed are preserved: when the durable task is not (yet) in the fallback
(a durable task whose update fails outright cannot be mirrored into an empty
fallback), the repository returns `None` and the tool still says "nothing
was changed" — that was already correct and is unchanged.

Verified non-defects on the same class: `TaskInspectTool`/`inspect_text`
render the degraded marker from the live repository state at render time
(single read, no second store possible); `TaskManagementService.counts()` is
diagnostic-only with no user-facing durability claim; `advance_next_run`
(scheduler-internal) degrades inside the same repository and is re-proven by
the restart-safe occurrence contract rather than by a user-facing message.

### 3.3 `task_list` is now response-authoritative (`backend/ai/engine/dispatcher.py`)

`_VERBATIM_READ_TOOLS = frozenset({"get_bio", "task_list"})`. A successful
`task_list`-only round skips the continuation provider round and returns the
real tool result to the owner, so the model can no longer replace or
contradict the authoritative list. Mixed rounds and failures keep the normal
continuation behavior (unchanged).

### 3.4 One truthful degraded-store marker (`backend/ai/task_management_interface.py`)

`_FALLBACK_NOTE` → public `FALLBACK_NOTE`, reworded to be accurate for both
directions: *"Memory fallback — Supabase unavailable (tasks may be missing,
and anything created now is not durable)."* The list view and the creation
result share the exact same wording.

---

## 4. Why a created task can no longer vanish from the next list

- **If the creation degraded**, the task is genuinely not in the durable store
  — and the owner is told so at creation time (`durable: false` + note)
  instead of being handed a durable-looking success. No durable success is
  ever claimed for a task the durable store never received.
- **If the creation was durable**, the list is now a single snapshot and, for
  the provider path, a verbatim-authoritative result: the count, the rendered
  tasks, and the degraded marker can no longer disagree, and no model prose can
  replace the list.
- **If the read degraded**, the empty/partial view is explicitly marked as a
  memory-fallback view rather than presented as the durable list, and the
  marker can no longer be cleared out from under the content it describes.

**Honest limitation:** the fix does not (and cannot, without a schema or a
second store — both forbidden) make a task that was written only to memory
reappear in the durable store. What it removes is the *silent* part of that
divergence. The live id evidence (`#3` beside a durable `#27`) is the reason
this is the diagnosed cause; a live reproduction is still required to confirm
it end-to-end (§7).

---

## 5. Tests added — `tests/test_task_list_consistency.py` (11 tests)

| Requirement | Test |
|---|---|
| A — create then list (same owner) shows the task | `test_created_task_appears_in_the_next_list` |
| B — multiple active tasks all appear | `test_multiple_active_tasks_all_appear` |
| C — deleted tasks stay excluded | `test_deleted_task_stays_out_of_the_next_list` |
| D — owner isolation | `test_owner_isolation_holds_for_created_tasks` |
| E — a failed durable read is flagged, never authoritative | `test_failed_durable_read_is_flagged_not_authoritative` |
| E/I — one snapshot; degraded→healthy cannot split it | `test_degraded_then_healthy_reads_cannot_split_the_snapshot`, `test_task_list_reads_the_repository_once` |
| F — `task_list` result cannot be replaced by model prose | `test_task_list_result_is_verbatim_authoritative` (contract + real Dispatcher end-to-end; the narrating continuation provider is never consulted) |
| G — `task_count` matches the returned/rendered ids | `test_task_count_matches_returned_ids_in_one_snapshot` |
| Live root cause — a memory-only creation is reported honestly | `test_non_durable_creation_is_reported_honestly` (drives the REAL `CreateTaskTool` with a degrading repo: `durable=False`, note present, fresh local id ≠ durable `#27`, and the durable read cannot see the task) |
| Healthy path — a durable creation is reported and listed as durable | `test_durable_creation_is_reported_and_listed_as_durable` |
| H — a degraded transition is honest, never durable-looking | `test_non_durable_transition_is_reported_honestly` (task created during a degraded window lives only in the fallback; a further degraded pause reports `durable=False` + note; after recovery the durable store proves the pause never landed) |
| H — a healthy transition reports durable | `test_durable_transition_is_reported_as_durable` |

Test-double update: `tests/test_stage10.py::PresentationService` gained
`snapshot()` to mirror the real service contract (presentation assertions
unchanged).

---

## 6. Preserved architecture from the previous phases (still current)

- **Production fallback** — model-level, bounded, deterministic: active
  provider/model first, then the complete discovery-fed eligible real/free
  model pool (no per-provider cap), health/cooldown/quarantine, model-not-found
  TTL, one bounded retry per candidate, structured-contract failover; total
  real-candidate exhaustion returns an honest `success=False` (the Dummy
  provider is **never** in production routing or fallback).
- **Discovery feed** — one discovery pass feeds the complete production
  candidate pool independently of the `_MODELS_IN_RESPONSE` display cap and of
  `MODEL_TEST_GLOBAL_TEST_BUDGET`.
- **Test Modules** — non-blocking launch, `_test_running` concurrency guard,
  `_PanelEditGuardian` coalescing (12 s window, newest state, dedupe,
  serialized, terminal retry), exact five-segment progress, pagination,
  diagnostics budget, canonical `_render_test_results`.
- **Bio fidelity** — an explicit bio change persists the registered
  `bio_set_text` action, never `send_message`.

---

## 7. Database impact and live verification

**Database impact: none.** No schema, table, migration, RLS, or configuration
change. The only code touching persistence semantics reports the *existing*
degradation truthfully; the payload shape is unchanged.

**Live Telegram verification: NOT performed** (no credentials in this
workspace). The end-to-end probe remains: send a bio-task creation request,
then immediately ask for the active task list, and confirm that (a) the
creation either reports `durable` or carries the memory-fallback note, and
(b) the list contains the created task — or is explicitly marked as a
memory-fallback view.

---

## 8. Verification and delivery record

- Focused suites: task-list consistency, stage10 presentation, task hardening,
  task management, task repository, taskloom UI/milestone, tool health audit,
  current-bio determinism — **all green**.
- **Full suite: `2103 passed, 24 skipped, 0 failed`** (previous tip: 2101
  passed, +2 new tests for the transition-honesty contract).
- `python -m py_compile` on every changed Python file — OK.
- `git diff --check` — clean.

| Item | Value |
|---|---|
| Starting HEAD | `9b4af95e2b74c249bd0771515585ae90cb1f9cb1` (== origin/main at start) |
| Change commit | _this phase_ — `fix: report a degraded task transition as non-durable` (2 files: `backend/ai/tools/task_management_tools.py`, `tests/test_task_list_consistency.py`) |
| Previous phase (record) | `56b5e2c36c7c98ba33a8078640fa000519c6643f` — `fix: keep the task list authoritative and never report a memory-only creation as durable` (8 files); pushed `bf1eee5..56b5e2c`, remote HEAD verified |
| Report commit | _this commit_ (delivery record update) |
| Working tree | pre-existing untracked stray clone `telegram-self-bot/` deliberately left untouched |
| Live Telegram verification | **NOT performed** (no credentials in this workspace) |
