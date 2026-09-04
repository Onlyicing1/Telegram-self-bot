# IMPLEMENTATION REPORT

## 1. IMPLEMENTATION METADATA

| Field | Value |
|---|---|
| Repository | `Onlyicing1/Telegram-self-bot` |
| Branch | `main` |
| Starting HEAD | `98150d98798efe7a6e9fb26555bac6c7f277ed27` |
| Implementation commit | `de4cde8` (`feat: add event-triggered task automation`) |
| Implementation date | 2026-09-04 |
| Task/chunk | Upgrade the AI Task system into a general-purpose automation system (event triggers + deterministic matching + Telegram message event execution) |
| Work type | implementation |
| Final implementation status | **COMPLETE** — full test suite, compile check, and diff hygiene pass; live Telegram/Supabase verification **NOT performed** (no credentials/runtime in this workspace); schema migration **NOT applied** (SQL provided, user applies in Supabase) |

This is the single current-state report. It records only behavior and
validation established from the current source, the working-tree diff, and
commands actually run.

---

## 2. OBJECTIVE

The task system was a time-only scheduler: `ai_tasks` with
`schedule_type` constrained to `once`/`interval`/`daily`/`weekly`, a single
`TaskScheduler` that polls `next_run_at`, and a `TaskExecutionCoordinator`
that replays stored action snapshots through the registered `ToolExecutor`.
The limitations to solve:

1. Natural-language scheduling ("Tomorrow at 5 PM do X", "فردا ساعت ۵ …")
   needed a precise Tehran-local schedule boundary.
2. The system behaved like a timer; it could not represent **event-driven**
   automation ("when John sends me a message, reply using X").
3. Incoming Telegram messages had no task-evaluation path — no event
   triggers, no deterministic matcher.
4. Scheduled/triggered tasks must execute only **registered tools** through
   the existing `ToolExecutor`; stored actions re-validated at execution.
5. Task detection must stay **AI-semantic** (no regex/keyword routing) while
   ordinary conversation never auto-creates a task.
6. Security boundaries (ToolRegistry → ToolExecutor → Self Bot execution
   authority; owner scoping; CAS) must remain intact.
7. A latent recovery bug: full recovery cancelled the `lifeos-task-scheduler`
   task permanently (not in the orphan-cancellation protected set).

The goal: add the smallest production-safe **event-trigger** model on top of
the existing two-table architecture, route message events through the
existing Telethon update path into deterministic trigger matching and the
existing execution authority, and keep the single scheduler/executor/
repository/supervisor model.

---

## 3. ARCHITECTURE DECISIONS (source-verified)

Inspection of the current source established the following facts that shaped
the implementation:

- **No trigger concept existed.** `ai_tasks.schedule` JSONB held only time
  schedules; the `schedule_type` CHECK constraint allowed only the four time
  types. No event representation was possible.
- **No incoming-message task path existed.** The only plain
  `events.NewMessage()` handler in the bot was a health-timestamp hook; the
  router registered only outgoing/command handlers and the AI unified
  handler.
- **Identity-resolution infrastructure existed for chats only.**
  `backend/ai/chat_resolution.py` had `resolve_chat_name` (fuzzy scoring,
  multi-match clarification, fail-closed) but no sender resolution and no
  first/last-name awareness in the dialog snapshot.
- **Latent recovery bug.** `RuntimeSupervisor._cancel_orphan_tasks` had a
  protected-name set that omitted `lifeos-task-scheduler`, so a full
  recovery cancelled the time scheduler permanently.
- **Shared execution machinery exists and is correct.** One repository
  singleton (`get_task_repository()`), one `TaskScheduler`, one
  `TaskExecutionCoordinator`, one outcome notifier; occurrence creation is
  keyed on `(task_id, occurrence_key)` and returns the existing occurrence
  when the key already exists (both in-memory and Supabase repositories) —
  which makes deterministic event dedup safe.
- **Notifications are already opt-in.** `notify_on_outcome` /
  `deliver_result` default false after the prior hardening task; Saved
  Messages is not an execution log sink.

---

## 4. IMPLEMENTATION CHANGES

### 4.1 Trigger model — `backend/ai/task_trigger.py` (new)

Bounded, two-form trigger representation (no expression language, no code):

- `validate_trigger_spec` — the **model-facing (unresolved)** form the AI
  may emit. `sender`/`chat` are semantic NAMES (never numeric ids — numeric
  ids are rejected outright because the model can never invent an identity).
  Allowed fields: `type`, `sender`, `chat`, `contains`, `text_equals`,
  `starts_with`, `has_media`, `is_reply`, `direction`. Bounded sizes and
  counts; at least one matching condition required (a trigger can never
  fire on every message by accident).
- `validate_resolved_trigger` — the **persisted (resolved)** form after
  trusted runtime resolution: adds integer `sender_id`/`chat_id` (nonzero;
  negative chat ids are legal — groups/supergroups) plus display-only
  `sender_name`/`chat_title`.
- `is_this_chat_reference` — English/Persian aliases for "this chat / همین
  چت" resolve to the trusted request `chat_id` at creation time.
- `resolve_trigger_references` — resolves names against the authenticated
  Self Bot's dialog snapshot via `resolve_chat_name` /
  `resolve_sender_name`; fails closed with numbered clarification when
  ambiguous/unresolvable; the resolved trigger never contains a
  model-invented id.
- `event_trigger_matches` — **deterministic** matcher over a bounded event
  context dict (`chat_id`, `sender_id`, `text`, `has_media`, `is_reply`,
  `out`). Direction filter (incoming/outgoing/any) + every present
  condition ANDed. No provider call, no scoring, no LLM per message.
- `trigger_summary` — bounded user-facing description of a resolved trigger.

### 4.2 Schedule model — `backend/ai/scheduling.py`, `task_candidate.py`, `task_interpreter.py`

- `SUPPORTED_TYPES` and the repository `SCHEDULE_TYPES` now include `event`.
- `EventSchedule` — frozen schedule with a single validated resolved
  `trigger`; `next_occurrence()` raises for event schedules (no wall-clock
  time); `parse_schedule` accepts only `{"trigger": ...}` payloads.
- `TaskCandidate` validates the model-facing event trigger spec through
  `validate_trigger_spec` at candidate-build time and skips the
  timezone-match rule for `event`/`interval`.
- `TaskInterpreter` CANDIDATE_SCHEMA enum + system guidance now teach the AI
  the `event` schedule shape, the allowed model-facing fields (names, never
  ids), English/Persian examples, and a hard rule that time-based requests
  must NOT become `event` tasks. No regex, no keyword lists — the provider
  interprets semantics against the schema/guidance.

### 4.3 Persistence — `backend/ai/database/task_repository.py`, `task_creation.py`, `task_management.py`

- Repository interface + in-memory + Supabase implementations gain
  `list_event_tasks(owner_id, limit)` — one indexed query
  (`owner_id` + `status='active'` + `schedule_type='event'`), bounded.
- `TaskCreationService` — event tasks persist with `next_run_at = NULL`
  (they have no wall-clock due time; the event handler drives execution);
  timezone-match rule skipped for event/interval.
- `TaskManagementService._resume_next_run` returns `None` for event tasks —
  resuming an event task never fabricates a fake run time.

### 4.4 Event execution — `backend/ai/task_event_dispatcher.py` (new)

`TaskEventDispatcher` — the single event-trigger responder, sharing the
same repository singleton, `TaskExecutionCoordinator`, and outcome notifier
as the time scheduler (no parallel authorities):

- `extract_event_context` normalizes a Telethon event into a bounded dict.
- `event_occurrence_key(task_id, chat_id, message_id)` — deterministic key
  `"<task_id>:ev:<chat_id>:<message_id>"`; duplicate delivery of the same
  event (redelivery, restart) maps to the same key, so the unique
  `(task_id, occurrence_key)` index prevents a second occurrence. The claim
  CAS is the final guard for the concurrent race window.
- `handle_event` — lists active event tasks (bounded, limit 20), parses and
  evaluates each stored trigger deterministically, skips non-matching
  tasks, dispatches up to 5 executions per message, and never raises into
  the Telegram event path. Per-message logging is bounded structured
  `TASK_EVENT_TRACE` lines; no Telegram diagnostics.
- Duplicate handling: when the occurrence key already exists
  (running/retry_pending/terminal), dispatch is skipped; a claimed
  occurrence without a wired coordinator is marked `interrupted` for
  recovery.

### 4.5 Telegram integration — `backend/bot/handlers/task_events.py` (new) + router/supervisor

- `task_events.register` hooks **the existing Telethon update path**
  (`events.NewMessage()`, both directions — the deterministic `direction`
  condition decides), normalized per event, handed to the configured
  dispatcher. No second update loop, no second event dispatcher.
- `task_events.configure(dispatcher)` — process-wide binding; handler is a
  no-op when unconfigured (startup ordering/tests).
- `backend/bot/router.py` registers `task_events`.
- `backend/runtime/supervisor.py` builds the dispatcher with the shared
  repository/coordinator/notifier and configures it during task-scheduler
  startup; **`lifeos-task-scheduler` added to the orphan-cancellation
  protected set** so full recovery no longer kills the scheduler.
  Execution always uses the current client via the existing
  client-provider/coordinator path (recovery-safe; no stale client
  captured at startup).

### 4.6 Tool-layer sender/chat resolution — `backend/ai/tools/task.py`, `backend/ai/chat_resolution.py`

- `CreateTaskTool` now loads the trusted dialog snapshot once per creation
  when the destination `chat_name` or an event trigger references a name,
  capturing user-like fields (`first_name`, `last_name`, `username`).
- After candidate validation, event triggers go through
  `resolve_trigger_references(request_chat_id=extra["chat_id"])`; failure
  returns a clarification `ToolResult` instead of creating an unsafe task.
- `resolve_sender_name` — new strict scorer against user-like entities
  (full/first/last/username): exact or containment with a real ratio, plus
  a `>=3`-char prefix rule; unrelated names score 0.0 (no loose
  character-overlap guessing), multi-match/ambiguous → fail closed with
  numbered options. Same `_MATCH_THRESHOLD`/clarification conventions as
  `resolve_chat_name`.

### 4.7 Presentation — `backend/ai/task_management_interface.py`, `backend/bot/handlers/taskloom.py`

- Task list blocks show `Next: On message event` for event tasks (never a
  fake run time); `task_inspect` shows a `Trigger:` line via
  `trigger_summary`. Time-based tasks keep the existing Tehran-local
  `Next:` formatting from the prior hardening work.
- Taskloom schedule icon map gains `event → ⚡`.

### 4.8 Database — migration + docs

- New migration `supabase/migrations/20260904000001_add_event_schedule_type.sql`
  widens the `ai_tasks.schedule_type` CHECK to include `'event'`.
  **No new tables or columns.** Trigger spec lives in the existing
  `schedule` JSONB (max 16,384 bytes unchanged).
- `DATABASE_ARCHITECTURE.md` updated with the resolved trigger spec
  structure, matching semantics, and occurrence-key identity.

---

## 5. TRIGGER / SCHEDULE / EVENT MODEL (current state)

A task is one row in `ai_tasks`; its `schedule_type` selects how it fires:

| schedule_type | schedule payload | next_run_at | execution driver |
|---|---|---|---|
| `once` | `{at, timezone}` | instant | TaskScheduler (due poll) |
| `interval` | `{seconds}` | rolling | TaskScheduler |
| `daily` | `{hour, minute, timezone}` | rolling | TaskScheduler |
| `weekly` | `{weekday, hour, timezone}` | rolling | TaskScheduler |
| `event` | `{trigger: {…resolved spec…}}` | NULL | TaskEventDispatcher (Telegram message path) |

Event trigger (resolved, persisted) — all bounded, at least one condition,
runtime-evaluated deterministically:

```json
{"trigger": {
  "type": "telegram_message",
  "sender_id": 123456, "sender_name": "John",
  "chat_id": -100123, "chat_title": "Chat",
  "contains": ["urgent"], "text_equals": null,
  "starts_with": null, "has_media": false,
  "is_reply": null, "direction": "incoming"
}}
```

Occurrence identity for event executions: `"<task_id>:ev:<chat_id>:<message_id>"`,
unique with `task_id`. Attempt limit, claim/retry/interrupted semantics, and
CAS versioning are the existing occurrence contract, unchanged. The unique
index protects the occurrence record; the claim CAS protects the
claimed→running race. **Exactly-once Telegram side effects are not claimed.**

Architecture of execution (event and time paths converge):

```
Telegram message event / scheduler due poll
  → TaskEventDispatcher.handle_event | TaskScheduler
  → occurrence create/claim (deterministic key, CAS)
  → TaskExecutionCoordinator
  → action name re-validated against ToolRegistry
  → ToolExecutor
  → registered tool → service layer → real side effect
  → occurrence finalization → opt-in notification/result delivery
```

---

## 6. TIMEZONE BEHAVIOR (current state)

- All user-facing scheduling semantics use `Asia/Tehran` via
  `ZoneInfo` — never the server local timezone, never UTC as a user
  default, never hardcoded `+03:30` arithmetic (the `tzdata` dependency was
  added in the prior hardening task so `ZoneInfo("Asia/Tehran")` resolves
  on any host).
- Instants persist as UTC/timestamptz; naive local datetimes in candidates
  are interpreted in the task's `Asia/Tehran` timezone at parse time.
- Relative expressions ("tomorrow", "next Monday", "in 2 hours", Persian
  equivalents) are interpreted by the AI/interpreter against the schema
  guidance with the current Tehran date/time as reference — no local
  parser, no regex. The deterministic tests pin the instant math for
  once/daily/weekly/interval Tehran schedules.

---

## 7. NOTIFICATION / SAVED-MESSAGES BEHAVIOR

Unchanged from the hardening task and preserved here: execution outcomes
live in `ai_task_occurrences` + structured `TASK_EVENT_TRACE` logs;
`notify_on_outcome=false` and `deliver_result=false` are the defaults, so
event/scheduled execution is silent — no Saved Messages status messages by
default. Only explicit user intent (via the existing opt-in flags) enables
notification/result delivery, and that flows through the same outcome
notifier boundary.

---

## 8. SECURITY BOUNDARIES (unchanged and enforced)

- AI proposes/selects registered capabilities; ToolRegistry validates; the
  ToolExecutor is the sole execution authority; the Self Bot owns all
  Telegram RPC. Task database never becomes a command execution system.
- Stored actions re-validated against the registry at every execution;
  unregistered actions fail closed.
- All task/occurrence mutations owner-scoped; lifecycle mutations CAS
  (version) protected.
- Model-invented Telegram ids are rejected at candidate validation; trigger
  identities come only from trusted dialog resolution, and ambiguous
  references fail closed with clarification. No arbitrary SQL/shell/HTTP/
  code predicates anywhere in the trigger path.
- The matcher is deterministic local logic — no LLM per incoming message.

---

## 9. FILES CHANGED

**New**
- `backend/ai/task_trigger.py` — trigger validation, reference resolution, deterministic matcher, summaries
- `backend/ai/task_event_dispatcher.py` — event→occurrence→execution coordinator handoff
- `backend/bot/handlers/task_events.py` — Telegram message event handler (existing update path)
- `supabase/migrations/20260904000001_add_event_schedule_type.sql` — widen `ai_tasks.schedule_type` CHECK
- `tests/test_task_trigger_events.py` — 34 regression tests

**Modified**
- `backend/ai/scheduling.py` — `event` schedule type + `EventSchedule` + `is_event_schedule`
- `backend/ai/task_candidate.py` — model-facing trigger validation; timezone rule exemption
- `backend/ai/task_interpreter.py` — candidate schema + system guidance for `event` triggers
- `backend/ai/task_creation.py` — event tasks persist `next_run_at = NULL`
- `backend/ai/task_management.py` — event tasks never fabricate a next run on resume
- `backend/ai/database/task_repository.py` — `SCHEDULE_TYPES` + `list_event_tasks` (abstract/in-memory/Supabase)
- `backend/ai/task_management_interface.py` — trigger line / `On message event` in task presentation
- `backend/ai/tools/task.py` — `CreateTaskTool` dialog snapshot + trigger reference resolution
- `backend/ai/chat_resolution.py` — `resolve_sender_name` (strict, fail-closed)
- `backend/bot/handlers/taskloom.py` — `event` schedule icon ⚡
- `backend/bot/router.py` — register `task_events`
- `backend/runtime/supervisor.py` — dispatcher configuration; `lifeos-task-scheduler` orphan protection
- `DATABASE_ARCHITECTURE.md` — event trigger spec, matching semantics, occurrence identity

**Documentation**
- `IMPLEMENTATION_REPORT.md` — this report (full replacement)

---

## 10. DATABASE IMPACT

- No new tables, no new columns, no changes to `ai_task_occurrences`.
- One CHECK-constraint widening on `ai_tasks.schedule_type`
  (`once/interval/daily/weekly` → + `event`).
- Migration file created but **NOT applied** (no live Supabase access).
  Manual application (Supabase SQL editor):

```sql
ALTER TABLE ai_tasks DROP CONSTRAINT IF EXISTS ai_tasks_schedule_type_check;
ALTER TABLE ai_tasks ADD CONSTRAINT ai_tasks_schedule_type_check
    CHECK (schedule_type IN ('once', 'interval', 'daily', 'weekly', 'event'));
```

Rollback:

```sql
ALTER TABLE ai_tasks DROP CONSTRAINT IF EXISTS ai_tasks_schedule_type_check;
ALTER TABLE ai_tasks ADD CONSTRAINT ai_tasks_schedule_type_check
    CHECK (schedule_type IN ('once', 'interval', 'daily', 'weekly'));
```

**Pre-live-verification data cleanup** (existing test/obsolete rows; the
user executes this manually — the Self Bot never runs it):

```sql
-- Dependency-safe order: occurrences first, then task definitions.
BEGIN;
DELETE FROM ai_task_occurrences WHERE task_id IN (SELECT id FROM ai_tasks);
DELETE FROM ai_tasks;
COMMIT;
```

---

## 11. TESTS

New `tests/test_task_trigger_events.py` (34 tests) covers, against real
in-memory repository + real ToolRegistry/ToolExecutor where behavior is
exercised:

- Trigger spec validation: model-facing shape, invented numeric ids
  rejected, unknown keys/empty conditions rejected, resolved nonzero
  integer ids, this-chat resolution (and fail-closed without request
  chat), sender resolution to trusted ids, ambiguous/unresolvable
  sender/chat fail closed.
- Deterministic matcher: all conditions ANDed, direction/content fields.
- Event schedule parsing, candidate acceptance/rejection of unsafe shapes.
- Event task creation persists the structured trigger with `next_run_at`
  NULL.
- Dispatcher: matching event executes once through a registered tool;
  non-matching event creates no occurrence; duplicate delivery does not
  execute twice; paused/deleted/foreign-owner tasks skipped; deterministic
  unique per-message occurrence keys; unregistered stored action fails
  closed; retry semantics preserved; missing coordinator marks
  `interrupted`; notifications require opt-in.
- Handler context extraction + unconfigured no-op.
- Tehran schedule math: once/daily/weekly instants, interval semantics.
- No-task-on-conversation behavior and equivalent NL phrasings resolving to
  the same registered create-task tool (no regex).
- Presentation: event task shows trigger and no fake next run.
- Recovery: full recovery does not cancel the task scheduler.

Prior task suites (unchanged, still green) cover: deleted excluded from
normal counts/lists, delete persistence + CAS, fallback visibility,
"no tasks" honesty, timezone display, silent-by-default notifications,
attempt limit 3, retry, and client-recovery-safe execution.

**Executed (this task):**
- Focused task suites: `tests/test_task_trigger_events.py` + scheduler/
  execution/management/repository/hardening/nl-creation/send/candidate/
  contract/taskloom/stage suites → **267 passed**
- Interpreter/candidate/capability/action suites → **143 passed**
- Full suite `python3 -m pytest tests/ -q` → **1601 passed, 23 skipped**
- `python3 -m compileall -q backend/ tests/` → **passed**
- `git diff --check` → **passed**

---

## 12. VALIDATION CLASSIFICATION

| Area | Classification |
|---|---|
| Trigger validation / reference resolution | UNIT TESTED (real resolver against synthetic trusted dialogs) |
| Deterministic event matching | UNIT TESTED |
| Event → occurrence → ToolExecutor execution | INTEGRATION TESTED (in-process, real registry/executor, in-memory repository) |
| Duplicate delivery / occurrence identity | INTEGRATION TESTED (in-memory uniqueness) |
| Supabase `list_event_tasks` query path | INTEGRATION TESTED via mocked-client repository tests from prior suite (pattern identical to `list_due_tasks`) |
| Timezone schedule math (Tehran) | UNIT TESTED |
| Recovery safety | UNIT/INTEGRATION TESTED (in-process supervisor wiring) |
| Migration applied | **NOT APPLIED** — file provided; requires manual Supabase execution |
| Live Supabase persistence round-trip | **NOT LIVE VERIFIED** |
| Live Telegram event execution | **NOT LIVE VERIFIED** |
| Live notification/result delivery | **NOT LIVE VERIFIED** |

---

## 13. LIMITATIONS & REMAINING WORK

- **Live verification not performed.** The workspace has no Telegram
  credentials or live Supabase access. The full live checklist (create a
  once-task and an event-task, observe the trigger, one occurrence, pause/
  delete semantics, no Saved Messages spam) remains for the user's
  environment, after applying the migration and clearing obsolete rows.
- Event triggers currently react to **new messages** on the existing update
  path; historical/unread catch-up and chat-listen-only modes are not
  implemented.
- The dispatcher performs one bounded indexed query per message while event
  tasks exist; that is the intended index-friendly filter (no per-message
  LLM, no unbounded scan).
- Identity resolution depends on the authenticated Self Bot's visible
  dialogs; a contact the bot cannot see cannot be referenced by name and
  fails closed with clarification (by design).

---

## 14. GIT DELIVERY

| Item | Value |
|---|---|
| Implementation commit | `de4cde8` (`feat: add event-triggered task automation`) |
| Push result | pushed to `origin/main` (`98150d9..de4cde8`), no force-push |
| Docs commit (this report) | `docs:` record — follows below |
| Branch | `main` |
| Remote HEAD after delivery | final HEAD verified with `git rev-parse HEAD` / `git rev-parse origin/main` — reported in the delivery summary |
| Working tree | only pre-existing unrelated files remain uncommitted (NaraRouter-session edits: AGENTS.md, INVESTIGATION.md, README.md, provider files/tests; untracked `telegram-self-bot/`) — all untouched by this task |
