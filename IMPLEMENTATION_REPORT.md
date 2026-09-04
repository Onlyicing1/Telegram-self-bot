# IMPLEMENTATION REPORT

## 1. IMPLEMENTATION METADATA

| Field | Value |
|---|---|
| Repository | `Onlyicing1/Telegram-self-bot` |
| Branch | `main` |
| Starting HEAD | `9406ff391427a1e3643b7a27d7ba418f8e887013` |
| Implementation commit | `67471b5` (`fix: harden task lifecycle, scheduler execution, and notifications`) |
| Implementation date | 2026-09-04 |
| Task/chunk | Complete and harden AI task management / scheduler (six production problems) |
| Work type | implementation |
| Final implementation status | **COMPLETE** — full test suite, compile check, and diff hygiene pass; live Telegram/Supabase verification **NOT performed** (no credentials/runtime in this workspace) |

This is the single current-state report. It records only behavior and
validation established from the current source, the working-tree diff, and
commands actually run.

---

## 2. OBJECTIVE

The task system already had durable owner-scoped tasks (`ai_tasks`),
occurrence history (`ai_task_occurrences`), CAS versioning, a single
`TaskScheduler`, and a `TaskExecutionCoordinator` that replays stored action
snapshots through the registered `ToolExecutor`. Six production problems
remained: deleted tasks still counted, delete persistence appeared not to
reach Supabase, Telegram said "no tasks" while the database had tasks, tasks
could be created but could not reliably automate registered tools, task
times were displayed in UTC and `next_run_at` misrepresented lifecycle
state, and every scheduled execution flooded Saved Messages with status
messages.

The goal: fix each problem at its correct architectural layer, preserve the
existing boundaries (AI semantic tool selection → ToolRegistry → ToolExecutor
→ service → repository → Telegram), add no second scheduler/executor/
repository authority, make no schema change, and keep deterministic,
owner-scoped, restart-safe behavior.

---

## 3. ROOT CAUSES (from current source, before this task)

1. **Deleted tasks counted** — `TaskManagementService.list_tasks(status=None)`
   already excluded terminal `deleted` tasks (previous task), but counting
   was not centralized: Taskloom computed `active/paused/done` locally, and
   the `task_list` tool returned a placeholder count. There was no
   authoritative per-status count boundary that provably excluded deleted
   tasks.
2. **Delete persistence appeared not to reach Supabase** — the
   `SupabaseTaskRepository.transition_task → update_task` CAS path was
   correct, but every Supabase failure silently fell back to a *separate*
   in-memory repository: the process held TWO task-repository singletons
   (`get_repository_manager().task` for tools/handlers/Taskloom vs
   `get_task_repository()` for the scheduler), each with its own fallback.
   A Supabase outage made the UI appear successful (memory) while the
   Supabase row never changed, with no visible marker.
3. **"No tasks" while the database has tasks** — `list_tasks` on a failing
   Supabase degraded to the in-memory fallback, which is empty in a fresh
   process, and the empty result was presented as authoritative "No tasks
   found." with no indication that the durable source was unavailable.
   The two-singleton split also let tools and scheduler diverge.
4. **AI could create a task but could not reliably automate tools** — the
   scheduler and coordinator existed, but (a) the scheduler and the tool
   layer used different repository instances (divergent fallbacks), (b) a
   read/report action such as `task_list` executed and persisted its
   occurrence but its result never reached the user, and (c) after a
   supervisor rebuild/recovery the coordinator kept a stale Telethon client
   captured at startup, so scheduled Telegram actions failed after every
   recovery.
5. **Missing/wrong next execution time + UTC display** — `next_run_at` is
   stored as timestamptz (UTC) and the presentation layers
   (`task_management_interface._format_datetime`, Taskloom `_fmt_dt`)
   formatted the raw UTC instant; pause/complete/delete left `next_run_at`
   untouched, so terminal/paused tasks still advertised a future run, and
   resume did not restore a run time. No per-task timezone display existed.
6. **Saved Messages flooded with execution status** — `TaskOutcomeNotifier`
   was wired unconditionally in `RuntimeSupervisor` and sent
   "Task occurrence … succeeded/failed/…" messages to the owner's Saved
   Messages for every persisted outcome. There was no opt-in: the task
   definition never expressed whether the user wanted a notification.

Supporting environment finding: `ZoneInfo("Asia/Tehran")` raised
`ZoneInfoNotFoundError` on this workspace's Python (no system tz database,
no `tzdata` package) — the project never declared `tzdata`, so correct
IANA timezone handling was not reproducible everywhere.

---

## 4. EXACT IMPLEMENTATION CHANGES

### `backend/ai/database/task_repository.py`
- **Single repository authority**: `get_task_repository()` now returns
  `get_repository_manager().task` — the scheduler, tools, handlers, and
  Taskloom share ONE `SupabaseTaskRepository` (and one in-memory fallback).
  A Supabase outage can no longer split state between two fallback copies.
- **Fallback visibility**: `SupabaseTaskRepository.fallback_active` — set
  when any Supabase operation degrades to the in-memory fallback, cleared on
  the next successful Supabase operation; fallback-returned records from
  mutation paths carry `fallback_backend` (create already did; update paths
  now annotate too).
- **Consistency**: the terminal `terminal_at` timestamp is set only on first
  entry to a terminal state and never overwritten by an idempotent
  `deleted → deleted` self-transition (matches `InMemoryTaskRepository`).

### `backend/ai/task_management.py` (service — one authoritative rule)
- New `TaskManagementService.counts()`: per-status counts from the owner's
  full repository rows; every normal summary derives from it and the
  terminal `deleted` population is reported separately, never in normal
  totals. The normal list already excludes deleted tasks.
- `set_status` now manages `next_run_at` lifecycle semantics:
  - pause / completed / failed / expired / deleted → `next_run_at = None`
    (a paused or terminal task must not advertise another run);
  - resume (paused → active with no `next_run_at`) → recompute from the
    stored schedule (`interval`: one interval from now; once/daily/weekly:
    `next_occurrence`); if the schedule cannot be parsed, the task resumes
    but stays unscheduled (fail closed, logged) rather than lying about a
    run time.
- `set_status` keeps CAS (`expected_version` mandatory) and repository
  transition legality checks — no behavior change for invalid transitions
  (reactivation of a deleted task still raises `ValueError`).

### `backend/ai/task_management_interface.py`
- `DISPLAY_TIMEZONE = "Asia/Tehran"`: every user-facing task timestamp
  (`Next:`, occurrence `Scheduled:`) is converted with
  `ZoneInfo("Asia/Tehran")` — a real IANA zone, never a fixed `+3:30`
  offset, so DST rules stay correct. Naive datetimes are treated as UTC
  first. Persisted instants remain timestamptz/UTC.
- `list_text` / `inspect_text` append `⚠️ Memory fallback — Supabase
  unavailable (state is not durable).` whenever the repository reports
  `fallback_active` — a fallback read is never presented as durable truth.

### `backend/bot/handlers/taskloom.py`
- `_fmt_dt` renders Taskloom times in Asia/Tehran.
- The list-panel summary uses `service.counts()`; the `closed` total is
  `completed + failed + expired` — deleted tasks never inflate it.

### `backend/ai/task_execution.py`
- `TaskExecutionCoordinator` accepts a `client_provider`; each execution
  refreshes its `ToolContext` to the CURRENT self client (recovery/rebuild
  safe — scheduled Telegram actions no longer die on a stale captured
  client).
- **Result delivery**: after a successful occurrence, when the task
  definition explicitly set `notification_destination.deliver_result: true`
  (and a destination chat exists, falling back to the owner's own chat),
  the combined action-result text (bounded to 4000 chars) is sent to that
  chat through the trusted TelegramAPI facade. Delivery is best-effort and
  isolated: a failure is logged and never changes the occurrence outcome.

### `backend/ai/task_notifications.py`
- `TaskOutcomeNotifier.notify_persisted` now requires BOTH the persisted
  outcome match AND the task definition's explicit
  `notification_destination.notify_on_outcome: true`. Deleted tasks never
  notify. Without the flag the outcome stays in the durable occurrence and
  structured logs only (`TASK_OUTCOME_SILENT` debug line) — no Telegram
  message. This is the Saved Messages spam fix.

### `backend/ai/task_candidate.py` + `backend/ai/task_interpreter.py`
- `notification_destination` accepts two validated boolean flags —
  `deliver_result` (send the execution result) and `notify_on_outcome`
  (send status notifications); non-boolean values reject the candidate.
- Interpreter guidance documents when to set each flag (explicit "show me /
  نشون بده / notify me / خبرم کن" phrasing) and that both default to false —
  scheduled execution stays silent by default.

### `backend/ai/tools/task_management_tools.py`
- `task_list` now returns the real authoritative `task_count` (deleted
  excluded, via the service) and `fallback_active` in tool data, so the AI
  can see honest counts and fallback state.

### `backend/runtime/supervisor.py`
- `_start_task_scheduler` passes `client_provider=lambda: self.client` to
  the coordinator. No second scheduler; the supervisor remains the sole
  lifecycle authority.

### `backend/requirements.txt`
- Added `tzdata==2026.3` — platform-independent IANA timezone data for
  `zoneinfo` (required for correct Asia/Tehran handling on hosts without a
  system tz database).

### Tests
- `tests/test_task_hardening.py` (new, 20 tests) — full coverage of the six
  problems through the real chain (see §12).
- `tests/test_stage10.py` — presentation assertions updated to the
  Asia/Tehran display (`2026-01-02 13:00 +0330`).
- `tests/test_stage16.py`, `tests/test_stage9.py` — notification tests
  updated to the explicit `notify_on_outcome` opt-in contract; added
  `test_success_is_silent_without_notify_opt_in`.
- `tests/test_taskloom_ui.py` — added
  `test_list_panel_summary_excludes_deleted_tasks`.

---

## 5. FILES CHANGED

| File | Category | Purpose |
|---|---|---|
| `backend/ai/database/task_repository.py` | repository | Single singleton authority; fallback visibility; terminal_at consistency |
| `backend/ai/task_management.py` | service | `counts()`; next_run lifecycle (pause/terminal/resume) |
| `backend/ai/task_management_interface.py` | presentation | Asia/Tehran display; honest fallback note |
| `backend/ai/task_execution.py` | execution | client refresh per execution; opt-in result delivery |
| `backend/ai/task_notifications.py` | notifications | Outcome notifications require `notify_on_outcome` opt-in |
| `backend/ai/task_candidate.py` | contract | Boolean destination-flag validation |
| `backend/ai/task_interpreter.py` | runtime | Flag guidance for explicit result/notification requests |
| `backend/ai/tools/task_management_tools.py` | tool | Real `task_count` + `fallback_active` in task_list data |
| `backend/bot/handlers/taskloom.py` | UI | Tehran display; authoritative counts (deleted excluded) |
| `backend/runtime/supervisor.py` | runtime | Client provider for the scheduler coordinator |
| `backend/requirements.txt` | dependency | `tzdata` (IANA data for zoneinfo) |
| `tests/test_task_hardening.py` | test | New regression suite (six problems) |
| `tests/test_stage10.py` | test | Tehran display assertions |
| `tests/test_stage16.py` | test | Opt-in notification contract + silent-by-default test |
| `tests/test_stage9.py` | test | Opt-in notification contract |
| `tests/test_taskloom_ui.py` | test | Deleted tasks excluded from panel summary |
| `IMPLEMENTATION_REPORT.md` | documentation | This report |

Unrelated pre-existing working-tree changes (NaraRouter provider work from a
previous session: `AGENTS.md`, `INVESTIGATION.md`, `README.md`,
`backend/ai/discovery.py`, `backend/ai/model_discovery.py`,
`backend/ai/providers/*`, `backend/bot/handlers/ai.py`,
`tests/test_17_providers.py`, `backend/ai/providers/nararouter.py`,
`tests/test_nararouter_provider.py`, and the untracked
`telegram-self-bot/` directory) were NOT modified, staged, or committed by
this task.

---

## 6. SCHEDULER CHANGES

- No second scheduler was created. The existing `TaskScheduler` (wired by
  `RuntimeSupervisor._start_task_scheduler`, immortal-owned) is unchanged
  in its wake loop: recover → due retries → due tasks → occurrence claim →
  coordinator → advance `next_run_at`.
- The scheduler now shares the one repository singleton
  (`get_task_repository()` → `get_repository_manager().task`), so tasks
  created through the AI tool layer are guaranteed visible to the scheduler
  and vice versa, including under Supabase failure.
- The execution coordinator resolves the CURRENT self client per execution
  (`client_provider`), so a supervisor rebuild/hard reset can no longer
  leave scheduled actions pointing at a dead client.
- Restart-safety was already provided by occurrence-key uniqueness +
  recovery of `claimed/running/interrupted` occurrences; unchanged and
  re-verified by the existing recovery tests.

## 7. TASK EXECUTION ARCHITECTURE (as implemented)

```
AI semantic tool selection (provider schemas / validated JSON action)
  → ToolRegistry → ToolExecutor → service layer
  → durable ai_tasks definition (label, schedule, timezone, actions,
    notification_destination, next_run_at, version)
  → TaskScheduler (durable, owner-scoped, active-only due discovery)
  → occurrence claim (occurrence_key unique, attempt ≤ 3)
  → TaskExecutionCoordinator (stored action snapshot → registry check →
    ToolExecutor with fresh client context)
  → occurrence finalization (succeeded / retry_pending / failed)
  → advance_next_run (schedule semantics)
  → optional explicit notification (notify_on_outcome)
  → optional explicit result delivery (deliver_result)
```

A stored action is reconstructed from `action_snapshot` (`name` +
`arguments`); the coordinator validates the name against the registry and
fails closed (`unregistered_action`) before anything executes. The model
never invents task ids/versions/occurrence ids — CAS requires the version
from `task_list`/`task_inspect`.

## 8. DELETE SEMANTICS

- Deletion remains the existing terminal lifecycle state `status =
  "deleted"` — the row is NOT physically deleted (by design: `ON DELETE
  RESTRICT` from occurrences, durable history). Verified: a successful
  delete issues a PostgREST update with `status='deleted'`,
  `version = expected_version + 1`, `terminal_at` (first entry only), and
  CAS filters on `id`, `owner_id`, and `version`.
- The normal list and every count surface exclude deleted tasks; the row
  stays inspectable by id and its occurrence history is untouched.
- A stale version or foreign owner fails with no mutation.

## 9. SUPABASE PERSISTENCE BEHAVIOR

- Delete persistence path: `service.set_status → repository.update_task →
  PostgREST UPDATE ... eq(id) eq(owner_id) eq(version)` — the fake-client
  tests assert the exact payload and CAS filters, and that the stored row
  actually changes (persistence, not UI-only).
- If Supabase fails, the operation degrades to the shared in-memory
  fallback and that fact is visible: `fallback_active` on the repository,
  `fallback_backend` on mutation results, and a `⚠️ Memory fallback` note in
  the rendered task list/inspect text. Durable-looking success is never
  fabricated silently.
- **Live Supabase verification: NOT performed** (no credentials in this
  workspace). The persistence contract is proven by mocked-client tests
  (`tests/test_task_hardening.py`, `tests/test_task_repository.py`), not by
  a live round-trip.

## 10. COUNT / LIST BEHAVIOR

- One authoritative rule: `TaskManagementService` — the normal list excludes
  terminal deleted tasks; `counts()` reports per-status totals where
  deleted appears only under its own key, never in active/paused/completed/
  failed/expired totals.
- Consumers: `task_list` tool (renders list + `task_count`), `.task list`,
  Taskloom panel summary (now via `counts()`), all derive from the same
  service call. No other counting path exists in the current source (web
  API/dashboard have no task surface).

## 11. TIMEZONE & next_run_at BEHAVIOR

- Display: all user-facing task times (list, inspect, occurrences, Taskloom)
  convert to `Asia/Tehran` through `zoneinfo` — no hardcoded `+3:30`/`+4`
  arithmetic anywhere. Persisted `next_run_at` stays timestamptz/UTC.
- `next_run_at` lifecycle: cleared on pause and on every terminal
  transition; recomputed from the stored schedule on resume; advanced by
  the scheduler after each successful occurrence; the UI always renders the
  actual persisted value.
- `tzdata` is now a declared dependency so `ZoneInfo("Asia/Tehran")`
  resolves on every platform.

## 12. SAVED MESSAGES / NOTIFICATION BEHAVIOR

- Scheduled execution is silent by default: outcome → durable occurrence +
  structured logs only. No Telegram message unless the task definition
  explicitly opted in.
- `notify_on_outcome: true` → status notification on succeeded / failed /
  retry_pending / cancelled, delivered only after the repository confirms
  the persisted status; deleted tasks never notify; sender failure is
  isolated and never mutates state.
- `deliver_result: true` → the execution result text (e.g. a rendered
  `task_list`) is delivered to the task's destination chat after success;
  best-effort, bounded, failure never flips the occurrence outcome.
- The explicit `send_message` action (user-requested task content) is
  unchanged and remains the only unconditional Telegram send from a task.
- Structured diagnostics (`TASK_*` log lines, occurrence result/error
  metadata) remain in place — nothing was removed, only re-routed away from
  Telegram.

## 13. AI TOOL-SELECTION BEHAVIOR

- Unchanged: no regex task routing; task sentences resolve through provider
  schemas → native tool call or validated JSON action → `resolve_tool_calls`
  → ToolExecutor → service. The provider-visible schemas are unchanged
  except `task_list` data now carrying an honest count and fallback flag.
- The AI can now express "show me the result" / "notify me" intent through
  the creation boundary: the interpreter maps those phrases to the two
  validated destination flags, and execution honors them through the
  registered tool/service boundary.

## 14. OCCURRENCE BEHAVIOR

- Unchanged and re-verified: `(task_id, occurrence_key)` uniqueness,
  attempt limit 3, claim semantics (`claimed → running`), terminal states,
  retry_pending with `retry_at`, recovery of interrupted occurrences on
  start, and `advance_next_run` after each wake. Occurrence uniqueness
  protects the occurrence record; exactly-once Telegram delivery is not
  claimed beyond that.

## 15. SECURITY BOUNDARIES

- Owner-scoping on every repository/service call; CAS versions mandatory
  for transitions; destinations always come from trusted task definitions
  or runtime context, never from model output; action names validated
  against the registered registry before execution (fail closed); no
  arbitrary RPC/SQL/shell execution added; no secrets introduced; no
  schema/RLS changes.

---

## 16. VALIDATION

| Validation | Command | Result |
|---|---|---|
| Focused task suites (11 files) | `.venv/bin/python -m pytest tests/test_task_hardening.py tests/test_task_management.py tests/test_task_repository.py tests/test_stage10.py tests/test_taskloom_ui.py tests/test_capability_exposure_tools.py tests/test_task_scheduler.py tests/test_task_execution.py tests/test_task_send_execution.py tests/test_taskloom_milestone.py tests/test_task_nl_creation.py -q` | **139 passed** |
| Full test suite | `.venv/bin/python -m pytest tests/ -q` | **1567 passed, 23 skipped, 1 warning** |
| Compile check | `.venv/bin/python -m compileall -q backend/ tests/` | **passed** |
| Diff whitespace check | `git diff --check` | **passed** |

Coverage mapping to the task's required test list (items 1–26): deleted
excluded from list/counts (1,2,4,5,6), deleted inspectable (3), delete
persistence through repository/service incl. Supabase CAS payload (7),
owner isolation (8), stale-version CAS (9), DB failure/fallback visibility
(10), "no tasks" only when none eligible (11), tool schema/count data (12),
scheduler reconstructs and executes a stored registered action through the
real ToolExecutor (13,14), unregistered action fails closed (15 — existing
`test_unregistered_action_fails_without_execution`), deleted/paused skipped
(16), occurrence uniqueness (17), success advances next_run (18), retry
semantics (19), details expose persisted next_run (20), Asia/Tehran
conversion incl. DST-era instants (21), no hardcoded offset (22), no Saved
Messages by default (23, plus scheduler-level), explicit notification
works (24), diagnostics retained (25 — occurrence metadata assertions),
restart/recovery does not duplicate occurrences (26 — existing recovery
tests).

## 17. LIVE VERIFICATION STATUS

- **Live Telegram verification: NOT performed.** No session credentials or
  running bot were available in this workspace. The scheduler/execution
  chain is proven in-process (unit + integration tests with fake Telegram
  transports and a real registry/executor), not against live Telegram.
- **Live Supabase verification: NOT performed.** No Supabase credentials
  were available. The `SupabaseTaskRepository` behavior (delete CAS payload,
  terminal_at, fallback markers) is proven with the fake PostgREST client.
- Distinction used throughout: **UNIT TESTED** (repository/service/presenter
  contracts) and **INTEGRATION TESTED** (scheduler → coordinator → real
  ToolExecutor → real registered tools in-process). Nothing here is claimed
  as LIVE VERIFIED.

## 18. LIMITATIONS

1. Live Telegram and live Supabase verification remain unproven; the
   report proves the local contract chain only.
2. `deliver_result`/`notify_on_outcome` depend on the AI interpreter
   (or deterministic candidates) emitting the flags; a model that ignores
   the guidance yields silent tasks (safe default), not broken ones.
3. Resume recomputation of `next_run_at` fails closed for schedules that
   cannot be parsed (e.g. legacy non-canonical interval payloads) — the
   task resumes unscheduled and logs a warning.
4. Deleted tasks are excluded from normal counts/lists but remain in the
   database by design (terminal state, not physical deletion).

## 19. REMAINING WORK

- Live verification per the task's checklist (create/list/delete through a
  running bot, confirm Supabase row state, one scheduled execution, Tehran
  display, no default Saved Messages spam) once credentials are available.
- Optional: a dashboard/web read surface for tasks (out of scope here — the
  web API currently has no task endpoints).

---

## 20. DELIVERY STATE

- Implementation commit: `67471b5` (`fix: harden task lifecycle, scheduler
  execution, and notifications`) — pushed directly to `origin/main`
  (verified `git push` output `9406ff3..67471b5 main -> main`); no
  force-push, no PR.
- Branch: `main`.
- Remote HEAD: `origin/main` equals local HEAD after the final push
  (verified with `git rev-parse HEAD` / `git rev-parse origin/main`).
- Final working tree: clean except the explicitly pre-existing unrelated
  working-tree changes listed in §5 (NaraRouter session files and the
  untracked `telegram-self-bot/` directory), which were left untouched.