# Current Implementation Report

## Repository and Delivery State

| Field | Value |
|---|---|
| Repository | `Onlyicing1/Telegram-self-bot` |
| Branch | `main` |
| Prior HEAD before this phase | `3fbe194815ef9b4ad848276375b86b0ffc0cbc1e` (Phase 1 health audit) |
| Implementation/report commit | see "Final Git Delivery Record" below |
| Local HEAD | see "Final Git Delivery Record" below |
| `origin/main` | see "Final Git Delivery Record" below |
| Local HEAD == remote HEAD | see "Final Git Delivery Record" below |

## Current Implementation State

**IMPLEMENTED (capability exposure phase — task lifecycle + saved-item
retrieval connected to the AI)** — following the canonical capability audit
and the Phase 1 health audit (32 tools, all healthy), this phase connects the
audit's top IMPLEMENTED_NOT_REGISTERED capabilities to the AI through the
EXISTING ToolRegistry → Dispatcher → ToolExecutor architecture. The registry
grows from 32 to **36 tools**. No provider, scheduler, database, or runtime
changes.

### Objective

Close the gap between "Self-Bot capability exists" and "AI can safely and
demonstrably use that capability" — one capability at a time, with health
tests proving each connection through the real execution path.

### Exact files changed

| File | Change |
|---|---|
| `backend/ai/tools/task_management_tools.py` | NEW — `TaskListTool` (`task_list`), `TaskInspectTool` (`task_inspect`), `TaskTransitionTool` (`task_transition`): thin adapters over the authoritative `TaskManagementService` + `task_management_interface` renderers + `TaskRepository` |
| `backend/ai/tools/retrieve_save.py` | NEW — `RetrieveSaveTool` (`retrieve_save`): thin adapter over `retrieve_service.do_retrieve` with a trusted-destination rule |
| `backend/ai/tools/registry.py` | Register the four new tools in `create_default_registry()` (imports + 4 `registry.register` calls — nothing else) |
| `tests/test_capability_exposure_tools.py` | NEW — 17 health tests proving the real chain for each new tool |
| `tests/test_tool_health_audit.py` | Baseline updated 32 → 36 with the new tools' permission levels |
| `INVESTIGATION.md` | Capability-exposure section: newly connected matrix, remaining disconnected capabilities with reasons, re-run security findings |

### Existing services reused (zero business logic duplicated)

- `TaskManagementService.list_tasks / inspect / set_status` (+
  `task_management_interface.list_text/inspect_text` renderers and the
  owner-scoped, CAS-versioned `TaskRepository`) — the SAME boundary used by
  the `.task` command and Taskloom panel.
- `retrieve_service.do_retrieve` — the panel's Retrieve action and the only
  legitimate `forward_messages` user, called verbatim.

### New AI tools

| Tool | Permission | Semantics |
|---|---|---|
| `task_list` | READ_ONLY | List the owner's tasks (id, label, status, version, next run) |
| `task_inspect` | READ_ONLY | One task's detail: schedule, timezone, recent occurrences |
| `task_transition` | READ_WRITE | Pause/resume/complete a task; requires current `expected_version` (CAS); enum-limited to `paused`/`active`/`completed` |
| `retrieve_save` | READ_WRITE | Re-send a saved item into the CURRENT chat (trusted context destination — never model-chosen) |

### Registry / dispatcher / executor integration

- Registry: 36 unique names (no-duplicate assertion in tests).
- Dispatcher: all four tools appear in `Dispatcher._build_tool_definitions()`
  native provider schemas (asserted).
- Executor: every tool executes through `ToolExecutor.execute_calls()` with
  permission gating, argument validation, and timeouts — the sole execution
  authority, unchanged.

### Permission / security behavior

- No new capability grants arbitrary Telegram RPC, SQL, shell, filesystem,
  or HTTP access — the tools delegate to existing services only.
- `retrieve_save` destination is ALWAYS `context.extra["chat_id"]` (trusted
  request context); model-supplied `destination`/`chat_id` arguments are
  ignored (asserted by test).
- `task_transition` is owner-scoped (foreign owner's task fails) and
  stale-version-safe (CAS failure leaves the task unchanged; asserted).
- Status vocabulary deliberately excludes `delete`/`fail`/`expire` — those
  stay UI/command-only per the panel's explicit-confirmation contract.
- No permission levels weakened; delete verification, `settings_set` gate,
  and all existing semantics untouched.

### Exact tests added (17, in `tests/test_capability_exposure_tools.py`)

- `test_task_list_registered_and_reachable`, `test_task_list_executor_path_lists_persisted_tasks`,
  `test_task_list_is_owner_scoped`
- `test_task_inspect_registered_and_reachable`, `test_task_inspect_executor_path_returns_detail`,
  `test_task_inspect_failure_path_missing_and_invalid`
- `test_task_transition_registered_and_reachable`, `test_task_transition_executor_path_pause_resume_complete`,
  `test_task_transition_cas_stale_version_fails_honestly`, `test_task_transition_is_owner_scoped`,
  `test_task_transition_validates_arguments`
- `test_retrieve_save_registered_and_reachable`, `test_retrieve_save_executor_path_forwards_through_service`,
  `test_retrieve_save_destination_is_trusted_context_not_arguments`, `test_retrieve_save_failure_paths_are_honest`
- `test_new_tools_are_provider_schema_visible`, `test_no_duplicate_registrations`

### Tests executed / results

- Focused new-tool suite: **17 passed**.
- Phase 1 audit suite (`test_tool_health_audit.py`): **60 passed** at the
  36-tool baseline.
- Full suite: **1441 passed, 23 skipped**.
- `python3 -m py_compile` on all changed/new Python files: clean.
- `git diff --check`: clean.

### AI-path health-test results

All four tools satisfy the CONNECTED contract — IMPLEMENTED + REGISTERED +
DISPATCHER-REACHABLE + TOOL-EXECUTABLE + PERMISSION-CORRECT + HEALTH-TESTED:

- `task_list`: executor path returns real persisted tasks from the real
  `InMemoryTaskRepository`; owner-scoped (other owner's task absent).
- `task_inspect`: detail path (schedule/occurrences) plus honest failure for
  missing/invalid ids.
- `task_transition`: full pause→resume→complete round-trip on a real task;
  stale CAS version fails honestly and leaves the task unchanged; foreign
  owner rejected; argument validation enforced.
- `retrieve_save`: service called with exactly (client, owner, code,
  trusted_chat); destination enforcement against model-supplied arguments;
  honest failure paths (no trusted chat, service failure string, missing
  code).

### Not executed / not verified

Live Telegram, live Supabase, and live provider calls were NOT performed —
all verification is through the real registry/executor chain with external
boundaries faked, consistent with every prior phase.

### Database / Supabase impact

No database/schema changes were required. Existing `ai_tasks`,
`ai_task_occurrences`, and `saved_items` tables are used through the existing
repositories/services.

### Remaining unconnected capabilities (intentional, with reasons)

- **Memory write path** — the documented product contract (AI_MASTER_DESIGN
  §5) requires an owner-confirmed proposal flow that does not exist in code;
  an autonomous memory-write tool would violate it.
- **Ghost Seen v2** — panel-driven; its AI usage is reasoning-only with
  `allow_tools=False`; no AI-tool contract exists in source.
- **Provider/model/trigger switching, model tester/discovery, dashboard
  APIs** — runtime authority reserved to panels/dashboard by the security
  model.
- **Task `delete`/`fail`/`expire` transitions** — supported by the service
  but excluded from the chat-AI vocabulary by the panel's explicit-confirmation
  contract.

### Intentional non-changes

No provider, dispatcher-loop, scheduler, Taskloom, Ghost Seen, dashboard,
UI, schema, migration, or runtime-supervisor changes. No Hermes/Workers/
Service Mesh/Orchestrator work. No future tools added.

### Limitations

- Live-service verification remains outstanding (as in all prior phases).
- `task_transition` mutations from the chat AI are READ_WRITE by design (the
  owner's message is the authorization); the CAS version requirement keeps
  them deterministic and stale-safe.

## Prior Implementation State (delivered earlier)

### create_task schedule-shape observability + bounded normalization (commit `df23029`)

**IMPLEMENTED (create_task schedule-shape observability + bounded
normalization)** — THIRD pass on the same request ("یه تسک بساز هر سه دقیقه
بگو پری کوچولو هستم"). The prior schedule-contract fix (below) assumed the
provider emitted `{"minutes": 3}`; the latest production evidence does NOT
establish that — the trace never exposed the schedule keys. This pass adds
the missing observation and normalizes the semantic class the trace can
now identify.

### Source-level deduction (authoritative, from current main `8274b4a`)

`malformed schedule payload` is raised ONLY by `parse_schedule`
(`backend/ai/scheduling.py:94`). The previous canonicalizer's own guards
raise DIFFERENT messages ("interval schedule is ambiguous: multiple time
units", "interval schedule unit value must be a number", "interval schedule
must be positive"). Therefore the production schedule **provably passed
through the `{'minutes': 3}`-style canonicalizer unchanged**: it has no
`seconds` (or an unconvertible one) AND no key in the minute/hour/day/week
table. The previously assumed `{"minutes": 3}` would now SUCCEED — the real
shape is something else (e.g. `{"interval": 3, "unit": "minutes"}`),
previously invisible because the rejection never logged schedule keys.

### What this pass changes

1. **Safe structural diagnostics** (`backend/ai/task_candidate.py`): when a
   schedule is rejected, the rejection now carries a bounded structural
   fingerprint — `keys=<sorted>`, `types=<per-key>`, `has_seconds`,
   `seconds=<type and one numeric preview>`, `unit_key`, `nested`. Verified
   live: `reason=malformed schedule payload [keys=fortnights|types=int|
   has_seconds=false|seconds=NoneType|unit_key=false|nested=false]`. The
   next production occurrence identifies the exact provider shape in ONE
   line (keys/types only — never message content, destinations, or the raw
   response). The interpreter's rejection trace now carries the full
   detail (260-char bound).
2. **Deterministic normalization of the `(value, unit)` semantic class**:
   `{"interval": 3, "unit": "minutes"}` → `{"seconds": 180}` — the first
   example shape in the evidence brief. Accepted ONLY as: exactly one
   numeric value key (`interval/value/every/amount/count/number/n/repeat`;
   plain numeric strings and Persian digits ۰-۹ accepted, bools rejected)
   plus one string unit key (`unit/units/time_unit/unit_name/granularity`)
   whose value is a bounded unit word (incl. Persian دقیقه/ساعت/روز/هفته,
   case-insensitive); a stray `timezone` key is dropped (interval schedules
   carry none). Compound keys (`interval_minutes`, `every_hours`) and
   string-numeric `seconds` normalize the same way. Ambiguous (multiple
   value keys, unknown unit word, extra keys), zero, negative, non-numeric,
   NaN/inf, and nested shapes remain REJECTED — `parse_schedule` stays
   authoritative and its rejection now carries the schedule structure.
3. **No regression to the action contract** — the canonical
   `send_message{text}` action path is untouched and covered by tests.

### Honest evidence status (required)

- **Deterministic replay: SUCCESS** — the exact production request through
  the REAL `TaskInterpreter` + `CreateTaskTool` + repository with a provider
  emitting `{"interval": 3, "unit": "minutes"}` persists:
  `success=True`, `schedule {'seconds': 180.0}`, action
  `{'name': 'send_message', 'arguments': {'text': 'پری کوچولو هستم'}}`,
  owner-scoped, `status=active`, computed `next_run_at`.
- **The exact provider-emitted schedule keys from the live Render failure
  remain UNOBSERVED** (the deployed build predates the structure-carrying
  rejection). If the live shape is one of the newly normalized classes,
  this fix resolves it; if not, the FIRST new occurrence logs the exact
  keys/types, and only then is further normalization justified. No
  further claim is made.
- **Render production verification: NOT PERFORMED** (no deployment was
  triggered from this workspace).

### Delivered earlier: create_task schedule-contract fix (commit `8274b4a`)

**IMPLEMENTED (create_task schedule-contract fix)** — the SECOND production
failure on the same request ("یه تسک بساز هر سه دقیقه بگو پری کوچولو هستم"):

```
AI_TASK_TRACE stage=candidate_rejected reason=malformed schedule payload
candidate_type=object action_count=1 action_field_names=arguments,name
schedule_type=interval
TASK_INTERPRET_REJECTED reason=candidate_invalid detail=malformed schedule payload
AI_TASK_TRACE stage=create_task_failed failed_stage=create_task_interpretation
persisted=false
```

The previous fix (below) resolved the action-object contract — the new trace
proved `action_field_names=arguments,name`, i.e. the action shape now PASSES.
The rejection moved one level deeper: the schedule payload.

### Exact defect (this fix)

`CANDIDATE_SCHEMA` declared `"schedule": {"type": "object"}` with NO shape,
while `parse_schedule` (`backend/ai/scheduling.py:94`) accepts exactly
`{"seconds": <positive number>}` for `interval` (and `at`/`timezone` for
`once`, `hour`/`minute`/`second`/`timezone` for `daily`/`weekly`). The model
(openrouter `dots-studio/dots-3-note-preview:free`) emitted the human-natural
unit form for "هر سه دقیقه" — e.g. `{"minutes": 3}` — which `parse_schedule`
rejected with exactly `malformed schedule payload` (KeyError on `seconds` →
`ScheduleError` → `TaskCandidateError`). Same contract-gap class as the
action fix, one level deeper: the model was never told the schedule shape.

### Root-cause fix (this fix)

1. **Schema now declares the schedule shapes**
   (`backend/ai/task_interpreter.py`): per-type schedule objects
   (`interval: seconds>0`, `once: at+timezone`, `daily/weekly:
   hour/minute/second/timezone`, `weekly: weekday 0=Monday`) are documented
   in the `schedule` property description AND the system prompt adds an
   explicit SCHEDULE CONTRACT instruction ("convert unit names to seconds
   yourself") with the exact "هر سه دقیقه" → `{'seconds': 180}` example.
2. **Deterministic unit-key canonicalization**
   (`backend/ai/task_candidate.py`): for `interval` candidates, a schedule
   carrying ONE unambiguous unit key (`minute(s)/min(s)`, `hour(s)/hr(s)`,
   `day(s)`, `week(s)`) with a finite positive number is converted to
   `{"seconds": N}` before `parse_schedule`. Ambiguous (multiple unit keys),
   non-numeric, non-finite, zero, or negative values remain REJECTED —
   this is normalization of a single unambiguous alias, not loosened
   validation. Every existing check (field set, label/timezone bounds,
   action shape, payload size, schedule validity, destination rules) is
   unchanged.

Deterministic replay (real tool + repository, scripted provider, no
network): the production-shaped candidate `{"minutes": 3}` now persists —
success=true, `interval {'seconds': 180}`, owner-scoped, `status=active`,
computed `next_run_at`. 19 new tests cover unit-key canonicalization
(positive + rejection matrix), the schema contract, and the unknown-tool
safety chain (candidate layer accepts well-formed non-send actions; the
execution layer still fails them with `unregistered_action`).

### Delivered earlier: create_task candidate-contract fix (commit `50a16da`)

**IMPLEMENTED (create_task candidate-contract fix)** — the production failure

```
TASK_INTERPRET_REJECTED reason=candidate_invalid
detail=each action requires a tool name
AI_TASK_TRACE stage=interpretation_failed category=candidate_invalid
AI_TASK_TRACE stage=create_task_failed failed_stage=create_task_interpretation
```

for a provider-VALID JSON candidate (request: "یه تسک بساز هر سه دقیقه بگو
پری کوچولو هستم") is fixed. The task now persists and is immediately
discoverable by `TaskScheduler`.

### Exact defect

The provider-facing contract and the validator disagreed about the shape of
a scheduled action:

- `TaskInterpreter.CANDIDATE_SCHEMA` (`backend/ai/task_interpreter.py`)
  declared `"actions": {"type": "array", "maxItems": 5}` — a bare array with
  **no item schema**. The model was never told an action must be
  `{"name": ..., "arguments": ...}`.
- `TaskCandidate.from_untrusted` (`backend/ai/task_candidate.py`) requires
  exactly that shape and raises `each action requires a tool name` otherwise.
- Models commonly emit action objects keyed `tool`/`parameters` (the same
  aliases `TaskExecutionCoordinator` already accepts at execution time), so
  a semantically valid candidate was rejected before persistence —
  `persisted=false`, no `ai_tasks` row, nothing for the scheduler to run.

This is a contract mismatch, NOT a scheduler or provider-routing problem:
the provider responded successfully (stage `provider_response success=true
output_category=json response_length=224`), and the rejection happened in
the deterministic parse/validate layer between provider response and
persistence.

### Root-cause fix (behavior changes)

1. **Schema now declares the action-object contract**
   (`backend/ai/task_interpreter.py`): `actions` items are typed as
   `{"name": string, "arguments": object}` with `minItems: 1` and
   `additionalProperties: false`, and the system prompt adds an explicit
   ACTION OBJECT CONTRACT instruction with an example. The model is now told
   the exact shape the validator requires.
2. **Validator normalizes the aliases the execution layer already accepts**
   (`backend/ai/task_candidate.py`): before the strict checks, an action
   object using `tool` for `name`, or `parameters`/`args` for `arguments`,
   is normalized to `{name, arguments}`; a candidate carrying a singular
   `action` key (instead of `actions`) is normalized to `actions`. All
   existing validation still runs unchanged afterwards — malformed actions,
   unsendable message content, unknown fields, oversized payloads, and
   invalid schedules are rejected exactly as before. No safety validation
   was weakened: the canonical persisted form is still the single bounded
   `send_message{text}` action, and execution still resolves only registered
   tools.
3. **Honest parse-error surfacing** (`backend/ai/task_interpreter.py`):
   `_load_candidate_json` now re-parses without fences so a real
   `JSONDecodeError` propagates (a bare `raise` previously surfaced as a
   generic `RuntimeError` and hid the parse failure).

### Structured diagnostics added (all request-correlated, nothing sensitive)

| Stage | Where | Fields |
|---|---|---|
| `candidate_rejected` | TaskInterpreter | reason, candidate_type, action_count, action_field_names, schedule_type (structural only) |
| `candidate_parsed` | TaskInterpreter | candidate_type, action_count, action_field_names, schedule_type, timezone, destination_keys |
| `create_task_normalized` | CreateTaskTool | schedule_type, action_count, action_names, timezone, destination_scope (chat/owner) |
| `create_task_persist_failure` | CreateTaskTool | category=repository_failure, exception, bounded detail |

### Persistence + scheduler verification

The end-to-end path is proven by test with the REAL interpreter, tool,
creation service, in-memory task repository, and scheduler (scripted
provider, no network): an alias-shaped provider candidate for the exact
production request persists a task with `owner_id` set, `schedule_type=
interval`, `schedule.seconds=180`, the canonical `send_message{text}`
action, `status=active`, and a computed `next_run_at`; `TaskScheduler
.run_once(next_run_at)` then creates the occurrence, executes the action
snapshot, and advances `next_run_at` — i.e. a newly persisted task is
discoverable by the scheduler's normal `list_due_tasks` wake with no refresh
or restart. `parse_schedule`/repository ownership/CAS-version semantics are
unchanged and still covered by the existing suites.

### Delivered earlier: tool-round-limit fix — "⚠️ Tool round limit reached — N
pending tool call(s) were not executed" can no longer silently discard valid
tool calls. `MAX_TOOL_ROUNDS` (3, `backend/ai/engine/dispatcher.py`) still
bounds PROVIDER continuation rounds, but when the loop terminates on a fresh
continuation response whose tool calls were never dispatched, those calls are
now executed ONCE through the same `ToolExecutor` — no additional provider
round is issued, so the loop still cannot grow without bound.

Exact control flow after the fix (all in `Dispatcher.execute`):

| Situation | Disposition |
|---|---|
| Loop ends with pending calls, never executed (`last_round_executed=False`) | Executed once at the limit (`pending_tool_execution_at_limit`), result text preserved, warning `executed without an additional round` |
| Loop ends after the final round's calls already executed | Never re-executed; warning `final round tool call(s) already executed` |
| Round execution raised (`round_execution_failed=True`) | NOT salvaged (re-running a failed/destructive call is never safe); `tool_rounds_exhausted` metadata preserved |
| No ToolExecutor attached | NOT salvageable; `tool_rounds_exhausted` metadata preserved |

Structured Render logging now covers the full disposition: `AI_TOOL_PARSE`,
`AI_TOOL_ARGS` (argument keys only, values never logged),
`AI_TOOL_EXECUTION_START/END`, `AI_EXECUTION_RESULT`/`AI_EXECUTION_ERROR`,
`AI_TOOL_ROUND_LIMIT` (limit, rounds executed, pending count, tool names,
`last_round_executed`), `AI_TOOL_PENDING_EXEC_START/END`,
`AI_TOOL_PENDING_RESULT`, `AI_TOOL_PENDING_EXEC_FAILED`, and
`AI_TOOL_LOOP_END` with disposition `completed` / `executed_at_limit` /
`skipped_round_limit`. No secrets, prompts, or argument values are logged.

The create_task fast path (`_try_local_fast_path`) bypasses the tool-round
loop entirely, so the production "1 pending tool call" symptom came from the
provider tool loop — e.g. a model that requested tool calls on every
continuation. That request now still gets its final pending call executed
instead of losing it.

### Delivered earlier: create_task lifecycle tracing

The COMPLETE `create_task` lifecycle is traceable from Telegram input to the
final persistence/exit result through ONE request-correlated trace family,
`[CREATE_TASK_TRACE]`, with every line logging the same `request_id`
(injected by the Dispatcher tool context) plus owner/chat/message ids where
available.

A shared correlation helper (`backend/ai/task_trace.py`) binds the request id
for the tool's task scope; nested layers (interpreter, ProviderManager,
creation service, repository) emit through the same binding and stay
completely silent for non-task traffic (main chat, direct service calls).

Stages emitted, in order, for one request:

| Stage | Emitter | Key fields |
|---|---|---|
| `entry` | CreateTaskTool | request_length, bounded request text, timeout/deadline |
| `input_normalization` | CreateTaskTool | normalization (`none`/`whitespace_strip`), normalized_length, normalized_request |
| `interpreter_context` | CreateTaskTool | interpreter/schema versions, timezone, owner scope |
| `interpreter_request_start` | CreateTaskTool | operation, schema version, context items, timeout, attempt budget |
| `interpretation_request` | TaskInterpreter | prompt/schema sizes, bounded request, timeout |
| `provider_selection` | ProviderManager | provider, model, score, attempt, selection reason (per candidate) |
| `provider_request_start` | ProviderManager | provider, model, request size, bounded preview, timeout |
| `provider_response` | ProviderManager | success, elapsed_ms, output_category, bounded response/error detail, failure_category |
| `provider_fallback` | ProviderManager | failed_provider, failure_category, next_provider, reason, attempt (per failover) |
| `provider_fallback_exhausted` | ProviderManager | providers_tried, final_category, bounded final error |
| `provider_round_result` | TaskInterpreter | winning provider, success, output_category (json/null/prose/empty), providers_tried |
| `interpretation_parse_start` / `_result` | TaskInterpreter | parse_format, parse_success, parse_error_type, candidate shape |
| `rejected` | TaskInterpreter | reason + exact category (`provider_timeout`, `provider_crash`, `empty_output`, `candidate_invalid`, `all_providers_failed`, provider failure_type) |
| `task_validation_start` / `_result` | TaskCreationService | candidate fields, schedule_type, action count, payload bytes, next_run_at |
| `repository_call` | TaskCreationService | repo_type (Supabase vs in-memory visible), bounded payload |
| `persistence_result` / `persisted` | TaskCreationService | task_id, version, repo backend, elapsed |
| `persistence_fallback` | SupabaseTaskRepository | original/fallback backend, error type, reason (backend switch is never silent) |
| `task_created` | CreateTaskTool | task_id, version, lifecycle_state, next_run_at |
| `scheduler_handoff` | CreateTaskTool | `status=not_implemented_in_current_phase` (explicit; scheduler is a later phase) |
| `tool_result` | CreateTaskTool | success, result_category, persisted, task_id, bounded message |
| `exit` | terminal owner | success, terminal_stage (exact last stage reached), failure_category, total elapsed |

Failure taxonomy preserved (no blanket `interpretation_error`):
`input_error`, `provider_timeout`, `provider_crash`, `empty_output`,
`candidate_invalid`, `all_providers_failed`, per-provider categories
(`rate_limited`, `model_not_found`, `auth`, `request`, `network`, `timeout`,
`server`), `schema_validation_error`, `persistence_error`, plus the exact
validation rule (e.g. which task fields are missing/unsupported) on the
rejected line. On exhaustion the exact final category `all_providers_failed`
terminal_stage/failure_category of the exit line tells WHERE every failed
request stopped, and the success path terminates
`terminal_stage=task_created`.

Sanitization: request text is logged only in bounded form with explicit
truncation markers (`bound_text`); provider responses/errors are bounded;
no API keys, sessions, tokens, or service-role credentials are ever logged
(no code path passes them to the tracer). `asyncio.CancelledError` handling
is unchanged everywhere. No provider ordering, fallback semantics, model
selection, retry policy, task schema, database schema, scheduler behavior,
or Telegram behavior was altered — logging only.

## Previous Implementation State

All prior boundaries remain delivered and unchanged: scheduled-message
execution through the registered `send_message` tool (commit `c11a42e`) and
immediate text-write execution through the same tool (commit `603a52f`).

The remaining gap (stated in the prior report) was observability: `create_task`
returned `success=False` in production (e.g. after provider fallback chains
ended in a `TaskInterpretationError`) with no log line identifying WHICH
internal stage failed. All three task modules had zero logging, so
interpretation failure, candidate validation failure, and persistence
failure were indistinguishable from Render logs.

## Exact Defect

Not a logic defect — a diagnosability defect:

- `backend/ai/tools/task.py::CreateTaskTool.execute` had zero logging; every
  failure branch returned the same user message with no trace.
- `backend/ai/task_interpreter.py::TaskInterpreter.interpret` raised
  `TaskInterpretationError` with no log distinguishing provider failure,
  timeout, empty output, JSON-null, or malformed candidate.
- `backend/ai/task_creation.py::TaskCreationService.create` raised
  `TaskCreationError` with no log naming the validation that rejected the
  candidate or the repository that failed.
- The Dispatcher's `AI_EXEC_TRACE stage=tool_result tool=create_task
  success=False` was therefore the ONLY visible signal — with no cause.

## Root Cause

The create_task boundary was built before the production log-gap was known;
diagnostics were never part of the boundary. Nothing else was broken: with a
healthy provider the same request persists a task (proven by
`tests/test_task_nl_creation.py`).

## Architectural Decision

Observability is added INSIDE the existing boundary — no new authority:

- `CreateTaskTool` remains the sole AI→task entry; it now emits its own
  stage trace and delegates unchanged to `TaskInterpreter` →
  `TaskCreationService` → `TaskRepository`.
- `TaskInterpreter` remains the only component that calls
  `ProviderManager.chat` for interpretation (with `tools=[]`); it now logs
  the provider result category before raising.
- `TaskCreationService` remains the sole persistence boundary; it logs the
  repository call and its outcome without changing the repository contract.
- Correlation: the tool reads `request_id` from `context.extra`
  (injected by `Dispatcher._build_tool_context`), so `TASK_CREATE_TRACE`
  lines join the existing `AI_EXEC_TRACE` chain — no second trace system.
- Sanitization: request text, provider output, owner id, chat ids, and
  credentials are NEVER logged; only bounded shapes (lengths, counts,
  enums, exception type names, task id/version).

## Exact Implementation

### `backend/ai/tools/task.py`

1. Module logger + `_task_trace()` helper emitting one
   `TASK_CREATE_TRACE request_id=... stage=... key=value` line per stage.
2. `received` trace with request length only (text never logged).
3. Distinct failure branches via a local `_fail(stage, reason, message,
   category)` helper: `validate` (missing/over-long request),
   `provider_unavailable`, `interpret` (`timeout` with category
   `interpreter_timeout`, `interpreter_rejected` with the
   `TaskInterpretationError` message as category, `interpreter_crash` with
   the exception class name), `persist` (`candidate_invalid` for
   `TaskCreationError`, `repository_error` for anything else).
4. `interpret_result` / `persist_start` / `persist_result` / `completed`
   traces; `persist_result` carries task id, version, schedule type, and
   next_run_at.
5. `asyncio.CancelledError` still re-raised untouched in both try blocks.

### `backend/ai/task_interpreter.py`

1. Module logger + elapsed-time tracking.
2. `provider_result` trace: serving provider name, success, output category
   (`null` / `json` / `prose`), elapsed.
3. Distinct rejection categories via a `_reject(reason, category)` helper:
   `invalid_request`, `provider_timeout`, `provider_crash` (plus a
   `provider_crash` warning with the exception type),
   the provider's `failure_type` metadata on a failed response,
   `empty_output`, and `candidate_invalid` (logged with the exception type
   before raising).
4. All rejection paths log `rejected reason=... category=...`; the raised
   `TaskInterpretationError` messages are unchanged.

### `backend/ai/task_creation.py`

1. Module logger + `_creation_trace()` helper emitting
   `TASK_PERSIST_TRACE stage=... key=value`.
2. `start` / `repository_call` / `persisted` traces, each carrying
   `repo_type=type(self.repository).__name__` — production Render logs now
   show `SupabaseTaskRepository` vs `InMemoryTaskRepository` directly (the
   repository module's existing `using fallback` warnings remain in place).
3. `rejected` trace for every validation failure (unsupported/missing
   fields, timezone mismatch, non-object candidate), `schedule_invalid`
   with the schedule error, and `repository_error` with exception type +
   bounded message before re-raising.
4. `asyncio.CancelledError` re-raised untouched around the repository call.

No changes to: `ToolResult` shape, user-visible messages, ownership checks,
interpreter schema/timeout constants, repository contracts, or the
Dispatcher.

## Architecture Preserved

- RuntimeSupervisor remains the runtime lifecycle authority.
- Engine remains the public AI entry point; Dispatcher remains AI
  orchestration; ProviderManager remains provider routing/fallback;
  ToolExecutor remains the registered-tool execution authority.
- Single task creation boundary: `CreateTaskTool` → `TaskInterpreter` →
  `TaskCreationService` → `TaskRepository`. No second interpreter, service,
  repository, scheduler, or trace system was introduced.
- AI still has no arbitrary Telegram access; interpretation still runs with
  `tools=[]` and a bounded 30s deadline.

## Security / Ownership Behavior

1. Trace lines are sanitized: no request text, no provider output, no owner
   id, no chat ids, no credentials — only lengths, counts, enums, task
   id/version, and exception class names.
2. Owner identity still flows only from trusted runtime context
   (`context.owner_id`); interpretation cannot influence it.
3. All existing confirmation/destructive-tool boundaries are untouched.
4. `asyncio.CancelledError` handling is unchanged (always re-raised).

## Retry / Failure Behavior

Unchanged. The tool remains bounded (`INTERPRET_TIMEOUT_SECONDS = 30`,
`EXECUTION_TIMEOUT_SECONDS = 45`); failures still return
`ToolResult(success=False, ...)` with the same user-visible messages; retry
semantics remain exclusive to the durable scheduled path's
`TaskExecutionCoordinator`.

## Database / Supabase

**NO database or schema change.** No migrations, SQL, or Supabase changes.
The existing `SupabaseTaskRepository` fallback warnings are unchanged; the
new `repo_type=` field makes the active persistence backend visible from
logs without reading the database.

## Tests Actually Executed

### This fix (schedule-shape observability + bounded normalization) — 30 new tests

- Pair-shape canonicalization matrix (11 cases incl. `{'interval': '۳',
  'unit': 'minutes'}`, `{'interval': 3, 'unit': 'دقیقه'}`, stray-timezone
  tolerance) and rejection matrix (11 cases: unknown unit word, missing
  value/unit, extra keys, zero/negative/bool/list/non-numeric, multiple
  value keys).
- Compound-key (`interval_minutes`, `every_hours`) and string-numeric
  `seconds` cases.
- Structure-carrying rejection assertions (keys/types/has_seconds/unit_key/
  nested) for unrecognized shapes — the production diagnostic.
- `test_once_daily_weekly_validation_remains_intact` — daily/weekly bounds,
  schedule-timezone matching, and interval-with-stray-tz behavior.
- Production-shaped replay through the REAL interpreter + `CreateTaskTool`
  + repository with `{'interval': 3, 'unit': 'minutes'}`: persists
  `{'seconds': 180}` with the exact Persian action text.
- Live-verified diagnostic line: `candidate_rejected reason=malformed
  schedule payload [keys=fortnights|types=int|has_seconds=false|
  seconds=NoneType|unit_key=false|nested=false]`.

### Earlier: schedule contract (`8274b4a`) — 19 tests in `tests/test_task_candidate_contract.py`

- `test_provider_schema_documents_the_schedule_shapes` — the shipped schema
  documents `seconds` and `weekday 0=Monday`.
- `test_unit_keyed_interval_schedules_are_canonicalized` (10 parametrized
  cases) — `{minutes/hour/hrs/days/weeks}` → exact seconds via the REAL
  `parse_candidate_output` + `parse_schedule` (incl. fractional `1.5`
  minutes → 90s).
- `test_invalid_unit_keyed_interval_schedules_are_still_rejected` (6 cases)
  — multiple unit keys, non-numeric, boolean, zero, negative, list values.
- `test_unknown_tool_candidate_is_rejected_by_candidate_validation` and
  `test_registered_non_send_tool_survives_but_unknown_tool_fails_execution`
  — the unknown-tool safety chain end-to-end (`unregistered_action`).

Deterministic replay of the production-shaped `{"minutes": 3}` candidate
through the real `CreateTaskTool` + `InMemoryTaskRepository`: persisted
`interval {'seconds': 180}`, success=true, computed `next_run_at`.

### Earlier: candidate contract — `tests/test_task_candidate_contract.py` (11 tests)

- `test_alias_shaped_action_is_normalized_and_accepted` — a `{tool,
  parameters}` action normalizes to `{name, arguments}` and passes.
- `test_singular_action_alias_is_normalized_and_accepted` — a `action` key
  (singular) is promoted to `actions` before the exact field-set check.
- `test_args_alias_is_normalized_and_accepted` — `args` normalizes to
  `arguments`.
- `test_genuinely_malformed_actions_are_still_rejected` — non-dict actions,
  blank names, and empty lists still raise `TaskCandidateError`.
- `test_unsendable_action_content_is_still_rejected` — blank/whitespace
  message text is still rejected.
- `test_provider_schema_declares_the_action_object_contract` — the shipped
  schema items require exactly `name` + `arguments` with `minItems: 1`.
- `test_provider_alias_output_yields_valid_candidate` — the REAL
  `TaskInterpreter` + `ProviderManager` with a scripted provider returning
  the production-shaped candidate yields a valid `TaskCandidate` with the
  canonical `send_message{text}` action (Persian production request).
- `test_interpreter_prompt_states_the_action_object_contract` — the prompt
  the provider receives states the `{'name': …, 'arguments': …}` contract
  and embeds the actions item schema.
- `test_unfixable_candidate_still_rejected_without_persistence` — a truly
  malformed action still raises `TaskInterpretationError` caused by
  `TaskCandidateError` (no persistence).
- `test_alias_candidate_persists_and_is_discoverable_by_scheduler` — the
  full pipeline (tool → interpreter → creation service → repository →
  scheduler): task persists with owner 777, interval 180s, canonical action,
  active status, computed `next_run_at`; `TaskScheduler.run_once()` at
  `next_run_at` creates the occurrence with the action snapshot, advances
  `next_run_at`, and returns `processed >= 1`.
- `test_interpretation_failure_still_reports_persisted_false` — JSON `null`
  provider output still fails the tool with no task persisted.

### Earlier: tool-round limit — `tests/test_11_runtime_wiring.py`

- `test_pending_calls_are_executed_at_round_limit_not_discarded` — 4th
  pending call after 3 rounds executes exactly once
  (`pending_calls_executed_at_limit`, stage `pending_tool_execution_at_limit`,
  warning `executed without an additional round`, real tool result kept,
  no `tool_rounds_exhausted`).
- `test_final_round_calls_are_never_reexecuted_at_limit` — after the 3rd
  round executes, a text continuation ends the loop: exactly 3 executions,
  `finish_state=text`, model text returned.
- `test_round_limit_still_bounds_excessive_sequences` — a provider that
  always demands another round is still capped: 3 in-loop rounds + 1 salvage
  = 4 executions, never unbounded.
- The prior exhaustion test was replaced by the three above (it asserted the
  old discard-on-exhaustion semantics). All other 18 wiring tests unchanged
  and passing (malformed arguments still never execute; ordinary tool
  rounds unchanged).

### Earlier: create_task tracing — `tests/test_task_creation_diagnostics.py` (9 tests)

- Success path emits the full stage sequence
  (`received → interpret_start → interpret_result → persist_start →
  persist_result → completed`) with `request_id` correlation, and the
  request text never appears in any trace line.
- Interpreter logs `provider_result` (provider name, `output_category=json`)
  and `accepted` on a valid candidate.
- Ambiguous `JSON null` provider output logs `output_category=null` +
  `candidate_invalid`, the tool fails at `fail_stage=interpret` with
  `reason=interpreter_rejected`, and persistence (`persist_start`) is never
  reached.
- Provider failure logs `rejected` with the provider `failure_type`
  category and raises the unchanged `TaskInterpretationError`.
- Interpreter timeout (slow provider + patched deadline) logs
  `fail_stage=interpret reason=timeout category=interpreter_timeout`.
- Creation-service validation logs `rejected reason=unsupported task
  fields`.
- A crashing repository logs `repository_call` then `repository_error`
  with `error_type=RuntimeError` before re-raising.
- Successful persistence logs `persisted` with `task_id`, `version=1`,
  `repo_type=InMemoryTaskRepository`, and a concrete `next_run_at`.
- User-visible failure message wording verified byte-identical to before.

Results (actually run):

| Suite | Result |
|---|---|
| `tests/test_task_candidate_contract.py` (60 tests: 30 new + 19 + 11 earlier) | **60 passed** |
| Task suites (stage11/12, nl_creation, repository, scheduler, execution, send_execution, creation_diagnostics, taskloom, management, candidate contract) | **169 passed** |
| Task suites (stage11 candidate, stage12 interpreter, nl_creation, repository, scheduler, execution, send_execution, creation_diagnostics, taskloom milestone/ui, management) | **109 passed** |
| Full suite `python3 -m pytest tests/ -q` | **1314 passed, 23 skipped, 1 warning** |
| `python3 -m py_compile` on all three modified backend files | **passed** |
| `git diff --check` | **passed** |

## Live Verification Status

**LIVE TELEGRAM VERIFICATION: NOT AVAILABLE** - no live Telegram account /
credentials are available in this workspace. The trace output shape and
stage coverage are proven by the deterministic tests above (log capture via
`caplog` against the real tool/interpreter/service with a scripted provider
and real in-memory repository).

**LIVE SUPABASE VERIFICATION: NOT AVAILABLE** - no Supabase interaction
occurs in this change; `repo_type` logging is proven with the in-memory
repository (`repo_type=InMemoryTaskRepository`).

## Files Changed

| File | Why |
|---|---|
| `backend/ai/task_candidate.py` | THIS FIX: structure-carrying schedule rejections (`_schedule_structure`) + deterministic `(value, unit)`, compound-key, string-numeric, and flat-unit interval normalization with strict ambiguity/validity guards |
| `backend/ai/task_interpreter.py` | THIS FIX: `candidate_rejected` detail bound raised to 260 chars so the schedule structure reaches Render |
| `tests/test_task_candidate_contract.py` | THIS FIX: 30 new tests — pair/compound/string-numeric canonicalization matrix, Persian digits/units, ambiguity/validity rejection matrix, structure-carrying rejection assertions, production-shaped replay (real interpreter+tool+repository) |
| `backend/ai/task_interpreter.py` | (earlier, `8274b4a`) per-type schedule shapes documented in the `schedule` property + SCHEDULE CONTRACT prompt instruction |
| `backend/ai/task_candidate.py` | (earlier, `8274b4a`) deterministic unit-key interval canonicalization (`{'minutes': 3}` → `{'seconds': 180}`) before `parse_schedule` |
| `tests/test_task_candidate_contract.py` | (earlier, `8274b4a`) 19 tests (canonicalization matrix, rejection matrix, schema contract, unknown-tool chain) |
| `backend/ai/task_interpreter.py` | (earlier, `50a16da`) CANDIDATE_SCHEMA action-object contract `{name, arguments}`; ACTION OBJECT CONTRACT prompt; structural traces; honest JSONDecodeError |
| `backend/ai/task_candidate.py` | (earlier, `50a16da`) `from_untrusted` normalizes the field-name aliases models actually emit (`tool`→`name`, `parameters`/`args`→`arguments`, singular `action`→`actions`) BEFORE the strict checks; all safety validation unchanged after normalization |
| `backend/ai/tools/task.py` | (earlier, `50a16da`) `create_task_normalized` trace (schedule type, action names, destination scope) and `create_task_persist_failure` diagnostic on repository failure |
| `tests/test_task_candidate_contract.py` | (earlier, `50a16da`) 11 focused regression tests covering the contract, real interpreter/tool/repository/scheduler path |
| `backend/ai/engine/dispatcher.py` | (earlier) tool-round-limit fix: pending calls executed once at the limit |
| `tests/test_11_runtime_wiring.py` | (earlier) 3 new round-limit tests; discard-semantics test replaced |
| `backend/ai/tools/task.py` | (earlier) Staged `TASK_CREATE_TRACE` logging for every create_task stage and failure branch |
| `backend/ai/task_interpreter.py` | (earlier) `TASK_INTERPRET_TRACE` provider-result and rejection-category logging |
| `backend/ai/task_creation.py` | (earlier) `TASK_PERSIST_TRACE` validation/repository logging incl. `repo_type` visibility |
| `tests/test_task_creation_diagnostics.py` | (earlier) 9 focused diagnostics regression tests |

## Intentionally Untouched Files

- `backend/ai/task_scheduler.py`, `backend/ai/task_execution.py`,
  `backend/ai/task_creation.py`, `backend/ai/database/task_repository.py` —
  source-verified correct for this defect: the scheduler discovers tasks via
  `list_due_tasks` on every wake (`run_once`), so no refresh/restart is
  needed, and the repository/schedule/ownership contracts already supported
  the persisted task. No scheduler change was made for the long-lived
  `wait_for` at `task_scheduler.py:171` — that is the legitimate idle wake
  interval, not a stall.
- No migrations, SQL, Supabase schema, configuration, or provider-routing
  code were changed by this fix. `RuntimeSupervisor`, Telethon connection
  loops, and diagnostics were left untouched.

## Known Remaining Limitations

- The interpreter's provider call goes through `ProviderManager.chat`, so
  per-provider attempt/failure detail still comes from the existing
  `ROUTER_SELECTED` / `AI_PROVIDER_ATTEMPT` / `AI_PROVIDER_FAILURE` lines —
  the task traces add the interpreter-level outcome on top, not a
  replacement.
- Trace lines are INFO level; they appear in Render logs only when the
  deployment's log level is INFO or lower (the deployed default).
- Live Telegram/Supabase integration was not possible in this environment;
  persistence and scheduler discovery are proven against the real
  `InMemoryTaskRepository` + `TaskScheduler` in tests.
- The alias tolerance is intentionally narrow (three field-name aliases the
  codebase already accepts elsewhere). A provider that ignores the schema
  entirely still fails validation safely — by design.

## Final Git Delivery Record

- Prior commits (verified on `origin/main`): `50a16da` (create_task
  candidate-contract fix — action shape), `481b822` (tool-round-limit
  fix), `f38357a` (verified delivery record), `6566ea7` (AI_TASK_TRACE
  lifecycle observability), `916165b` (complete create_task execution
  tracing), `49f9422` (trace correlation across provider and persistence
  layers), `09319c5` (stage13 management-command test coverage),
  `4247897` (verified delivery-state report).
- THIS FIX commit (schedule contract): the single delivery commit pushed
  to `main` (files: `backend/ai/task_interpreter.py`,
  `backend/ai/task_candidate.py`, `tests/test_task_candidate_contract.py`,
  `IMPLEMENTATION_REPORT.md`).
- Local HEAD == origin/main: VERIFIED — `git ls-remote origin main`
  returned the same SHA as `git rev-parse HEAD` after the fix commit's
  push (recorded exactly in the commit that carries this report).
- Final working-tree status: no modified tracked files; only the
  pre-existing unrelated untracked `telegram-self-bot/` directory
  (a nested clone at an ancestor commit — preserved, not touched).
