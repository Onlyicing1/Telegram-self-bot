# IMPLEMENTATION REPORT

## G2-A — Global, request and task language (current delivery)

### Implemented

- **One catalog/resolver** (`backend/ai/language.py`): `en`, `fa`, `ar`, `zh`
  with stable codes, labels, directionality and a provider hint, plus an
  explicit `auto` mode. All externally supplied values are validated; unknown
  values raise `LanguageError` instead of silently becoming English. The
  resolver returns the selected value, the effective code, the decision source
  (`request`/`task`/`global`/`default`/`source`/`auto_fallback`) and whether
  detection was used.
- **Precedence:** request > task > global `ai_preferences.language` > English.
  `panel_settings.language` is NOT read and remains the Glass UI language.
- **Auto:** explicit selection only; source-based summaries use the dominant
  source script when it resolves, otherwise the documented English fallback.
  Auto is not offered for translation (a no-op there) and never overrides an
  explicit task language.
- **One preference snapshot:** `Dispatcher.dispatch` reads preferences once,
  resolves the language, stores the decision on the request metadata, and uses
  it for the prompt, the session view and `ToolContext.extra["global_language"]`.
  `AIRequest.language` now defaults to empty ("not specified") and still accepts
  explicit values from direct callers.
- **History:** `summarize_history` accepts `language` + `global_language`, the
  same decision drives every map call AND the reduce call, and the result data
  carries `language`, `language_code`, `language_mode`, `language_source` and
  `language_detected`. `translate_history` validates through the same catalog
  and keeps its existing explicit-language behavior. Chunking, pacing,
  concurrency, timeouts and provider-manager use are unchanged.
- **History UI:** the panel has a request-level language selector
  (`action:history_language`, ephemeral panel state) and the result renders the
  effective language with its source (global default / auto detected / auto
  fallback). No other chrome changed.
- **Tasks:** Taskloom's language names/policy words come from the catalog. The
  wizard's generated-content instruction freezes the owner's global default at
  creation (inserted before a named source so source parsing is unchanged) and
  an explicit task language is never rewritten. `preparation_policy` is
  untouched and still rejects language mismatches; review/editor lines show the
  explicit or effective language.
- **Global setting UI:** AI → Settings shows and cycles the AI content language
  through the existing `PreferencesRepository`, and states honestly that the
  value is held in memory because `ai_preferences` has no deployed table.
- **No extraction surface exists**, so none was integrated; the shared
  resolver/catalog is the documented interface G4-A will consume.
- No schema, migration, SQL, Supabase or live Telegram change was made.

### Tests

- `tests/test_language_contract.py` (28 tests): catalog/validation, precedence,
  Auto contract and detection, AIRequest/dispatcher snapshot flow, History
  summary/translation prompts + metadata + UI rendering, task freeze/explicit
  precedence/preparation-policy rejection, and review labels.
- Updated `tests/test_task_wizard.py`'s review assertion from "Any" to the
  effective "Global default" it now reports.
- Full suite under Python 3.11.16 (isolated `uv` environment): **7899 passed,
  27 skipped, 1 failed** — the failure is the pre-existing
  `tests/test_task_scheduler.py::test_recovered_interrupted_occurrence_is_picked_up_by_run_once`.

### Limitations

- The global content language is process-memory only until an `ai_preferences`
  table is deployed; the UI and this report say so, and no durable write is
  claimed.
- Natural-language task instructions stay verbatim (source-fidelity contract),
  so the global default is frozen only for wizard-generated content. Freezing it
  for NL tasks needs a durable task-language field (schema change), which this
  phase must not add.
- Provider compliance with a requested language is never verified; only the
  requested/effective language is reported.
- No live Telegram panel, Supabase or deployment operation was performed.
- G2-B (Workspace language), G4-A (extraction/glossary), G5-A and Scheduled
  History Phase 4+ are not implemented.

### G2-A corrective pass — rejection and prompt clarity

Two confirmed defects were corrected without changing the catalog, aliases,
precedence or persistence:

- **Invalid values are rejected, not coerced.** `Dispatcher.dispatch` now
  resolves the content language through `_resolve_content_language` and returns
  the existing `EngineResult(success=False, errors=[...], metadata={...})`
  convention. An unsupported explicit `AIRequest.language` fails before any
  provider call, with a safe bounded message listing the supported languages and
  `metadata["language_rejected"] == "request"`. No English/Auto fallback decision
  is substituted and no `language` metadata is emitted for that request.
- **Invalid stored preferences fail explicitly.** `_load_preference_record` is
  the ONE preferences read; `_load_preferences` no longer converts an
  unreadable stored language into English (it preserves the raw value), and a
  request that would actually use that invalid preference is refused with
  `language_rejected == "global"` and a separate, safe configuration message.
  A valid explicit request language still wins and does not consult the broken
  lower-priority value.
- **Prompt language is unambiguous.** `PromptBuilder._render_preferences` now
  renders `Global default language: <value>`, while the runtime context renders
  `Language: <effective> (effective for this response)`. Only the runtime line is
  an output-language instruction; the global default is explicitly labelled as a
  lower-priority default. Auto keeps its existing honest wording
  (auto-detected vs. fallback). Personality, response style, auto-memory,
  auto-tools and custom instructions are unchanged, and the single
  preference-snapshot/one-resolution design is preserved.
- Added real-dispatch tests (`tests/test_language_contract.py`): invalid explicit
  request rejected with the provider never called, valid explicit over global,
  valid global alone, explicit Auto fallback, invalid stored preference refused,
  and a valid explicit language unaffected by broken stored data. Existing
  prompt/context tests continue to pass with the new labels.

## G1-A — Reusable interaction grammar (previous delivery)

### Implemented

- **UI-001 ordering rule:** documented in `AGENTS.md`. Panels order primary
  action, related controls, item-specific controls, contextual bulk actions,
  then navigation/pagination. Stash and Taskloom follow it; the existing panel
  engine, registry, callback namespaces, symbol/style registries and Appearance
  render boundary are unchanged.
- **UI-002 reusable selection:**
  `backend/helper/selection_state.py` now stores sessions by `(owner, scope)`
  while preserving the original Stash API as the default scope. Each scope keeps
  the existing 40-item cap, 900-second expiry, insertion order and exact
  identity. Taskloom stores exact task ids plus the version rendered on the
  page; display labels remain presentation only.
- **Taskloom selection:** the existing Taskloom list keeps its detail and
  pagination paths and adds a "Select tasks" view. A row toggles
  `action:taskloom_toggle:<task_id>:<version>`, selected rows show
  `sym.SELECT_MARKED`, selection survives pagination, and "Clear selection"
  drops it and returns to the list.
- **UI-003 contextual bulk actions:** Pause / Resume / Complete / Delete rows
  appear only when the current selected set has at least one applicable task.
  Every target is re-read through `TaskManagementService.inspect`, owner-scoped,
  and checked against the version stored at selection time before the existing
  CAS `pause`/`resume`/`complete`/`delete` service operation is called. Missing,
  foreign, stale and status-inapplicable targets are refused individually; the
  result view reports each item and never turns partial success into full
  success.
- **UI-004 destructive confirmation:** Taskloom bulk Delete has a dedicated
  review that names the exact operation, count and selected tasks, explains the
  irreversible consequence, provides Cancel, and puts the final confirmation in
  `styles.DANGER`. Confirmation re-runs the same full revalidation before any
  deletion. Stash's bulk delete confirm now uses `DANGER` too.
- **No second system:** no new selection store, panel renderer, task repository,
  scheduler, executor, Telegram client, listener or persistence model. No schema
  or Supabase/SQL change.

### Changed files

| File | Behavior |
|---|---|
| `backend/helper/selection_state.py` | Owner + feature-scoped sessions; Stash default preserved; cap/expiry and exact identity retained; optional selected version metadata. |
| `backend/bot/handlers/taskloom.py` | Taskloom select view, exact id/version toggles, pagination, contextual bulk actions, CAS revalidation, per-item results and delete confirmation. |
| `backend/bot/handlers/retrieve.py` | Stash bulk ordering follows the interaction rule; destructive bulk delete confirm uses `DANGER`. Existing callbacks and flows unchanged. |
| `backend/helper/styles.py` | Documents red as cancellation or a destructive final confirmation. |
| `tests/test_taskloom_selection.py` | New focused G1-A coverage. |
| `tests/test_colored_buttons.py` | Updates the style contract wording for destructive confirmations. |
| `AGENTS.md` | Documents the panel ordering rule and Taskloom selection contract. |
| `LIFEOS_IDEAS.md` | Marks UI-001/002/003/004 and G1-A only to the implemented extent. |
| `ROADMAP.md` | Adds a separated G1-A delivery entry while preserving Scheduled History history. |
| `IMPLEMENTATION_REPORT.md` | This report. |

### Tested offline

- The new `tests/test_taskloom_selection.py` covers scoped selection, cap and
  expiry, Stash default compatibility, visible marks, select/deselect,
  pagination survival, clear, foreign/missing/stale targets, stale callback
  refusal, contextual action availability, partial bulk outcomes, service
  refusal handling, confirmation scope, cancel, DANGER styling and confirmation
  revalidation.
- Existing Stash multi-select, Taskloom UI, callback latency/burst and
  colored-button suites are included in the focused regression run.

### Test results

Focused command:

```bash
.venv/bin/python -m pytest -q \
  tests/test_taskloom_selection.py tests/test_taskloom_ui.py \
  tests/test_retrieve_multi_select.py tests/test_callback_latency_burst.py \
  tests/test_colored_buttons.py
```

Result: **146 passed**.

Full suite:

```bash
.venv/bin/python -m pytest -q
```

Result: **7876 passed, 1 failed, 27 skipped** (`FULL_PYTEST_EXIT=1`).

The only failure is the known pre-existing
`tests/test_task_scheduler.py::test_recovered_interrupted_occurrence_is_picked_up_by_run_once`
(`assert 0 == 1`, log `interval advancement exceeded bound`). It is documented
in the repository as failing identically on earlier base commits; no scheduler
file is part of this delivery.

### Not live-verified

- No live Telegram callback/render, task transition, Supabase read/write,
  deployment or Render operation was performed. The tests use the in-memory task
  repository, the in-memory saved-items fallback and fake callback contexts.

### Deferred

- G1-B visual identity/accessibility, G2-A language behavior, G5-A Task Center
  beyond this narrow Taskloom selection/bulk work, Workspace features, and
  Scheduled History Phase 6/7 remain deferred. No G1-B or later stage is marked
  complete.

## Previous delivery — Scheduled History diagnostics tracing

### 0. Previous delivery scope

This previous delivery added observability only. It does not change Scheduled History
execution, scheduling, occurrence recovery/retry, cursor or CAS semantics,
retrieval limits, AI/provider behavior, task creation or compensation.

The existing `backend/ai/task_trace.py` `AI_TASK_TRACE` binding remains the
`create_task` request trace. Scheduled execution gets separate context
variables and separate log prefixes, so ordinary Telegram/AI traffic remains
silent and the existing create-task trace behavior is unchanged.

No table, column, migration, SQL, Supabase operation, deployment, scheduler,
worker, executor, listener, Telegram client or persistence path was added.

### 0.1 Source-verified starting state

- Base branch: `main`.
- Base HEAD and `origin/main`:
  `0e88c5381c52c45c06d9a92112affa47831a092a`.
- The working tree contained only the pre-existing untracked
  `tests/test_runtime_session_terminal.py`; it is not part of this delivery.
- The existing trace helper was bound to `create_task` only. Scheduler,
  occurrence lifecycle, coordinator action chain, `ScheduledHistoryTool`,
  `run_scheduled_history()`, AI/provider and repository CAS stages therefore had
  no shared occurrence correlation, and several failure paths were
  indistinguishable in production logs.

## 1. Trace contract

### 1.1 Correlation identities

| Flow | Prefix | Correlation id | Attempt |
|---|---|---|---|
| Durable occurrence | `SCHEDULED_OCCURRENCE_TRACE` | Exact Scheduler occurrence key `<task_id>:<scheduled_for UTC ISO>` | `attempt=<row attempt>` |
| Owner creation | `HISTORY_SCHEDULE_TRACE` | Short random `hist-<12 hex>` | Not applicable |
| Existing create-task request | `AI_TASK_TRACE` | Existing request id | Unchanged |

The occurrence key is stable across scheduler execution, recovery and retry.
A retry deliberately carries the same `trace_id` with a different `attempt`
value, so all attempts remain correlatable without being confusable.

`occurrence_scope()` always resets its context token in `finally`.
`history_creation_scope()` reuses an already-active outer creation scope and
resets its own token in `finally`. Separate asyncio tasks copy context, so
concurrent occurrences cannot mix fields.

The occurrence scope is opt-in. `occurrence_trace()` is silent outside it;
ordinary Telegram/AI/repository traffic therefore emits no scheduled stages.

### 1.2 Format and privacy

Every event is one bounded INFO line:

```
SCHEDULED_OCCURRENCE_TRACE trace_id=<key> task_id=<id> attempt=<n> stage=<stable-name> <safe fields>
HISTORY_SCHEDULE_TRACE trace_id=<hist-id> stage=<stable-name> <safe fields>
```

Dynamic values are flattened to one line and bounded at 160 characters (256
for the occurrence key). Fields are deliberately limited to ids, statuses,
registered action names, positions, counts, cursor/has-more booleans,
revision/version transitions, exception class names and stable categories.

The trace layer never receives or prints message text, captions, media,
cumulative summaries, owner instructions, prompts, provider request/response
bodies, Telegram session strings, bot tokens, API keys, authorization headers
or peer/chat identifiers. Raw exception messages are not used in occurrence
trace fields; only the exception class and a stable category are emitted.
Per-message and per-loop-iteration logging is not added.

Provider metadata is not traced verbatim. `failure_origin` is mapped through
an explicit allowlist (authentication/rate limit/network/server/request/
timeout/provider/model lookup), and unknown or malformed values become
`unknown`. `finish_reason` is likewise allowlisted (`stop`, `length`,
`content_filter`, `safety`, `recitation`, `tool_calls`, `blocked`), with unknown
values becoming `unknown`. `fallback_exhausted` is logged only as a real
boolean or `unknown`, never by truthiness. Provider bodies, error messages,
request ids and arbitrary metadata strings are never copied into the trace.

## 2. Instrumented stages and files

| File | Trace coverage |
|---|---|
| `backend/ai/task_trace.py` | New occurrence/creation context variables, scopes, silence outside scope, bounded formatting |
| `backend/ai/task_scheduler.py` | Recovery scan/transitions, due discovery, occurrence create/existing result, claim, handoff, retry discovery, execution result, notification, boundary advance |
| `backend/ai/task_execution.py` | Wrapper entry/reset/cancellation/exception, owner/status/action validation, provenance injection, planned actions, per-action start/result, waits, terminal outcome |
| `backend/ai/tools/history_schedule.py` | Tool invocation, provenance result, argument result, service call/result/exception |
| `backend/services/history_schedule_service.py` | Task identity, state lookup/validation, peer policy, upper bound, retrieval, safe skip, AI call, output validation, CAS commit, failure lifecycle, terminal checkpoint |
| `backend/services/history_ai_service.py` | Chunking/map capacity, provider call/timeout/failure/empty output, map/reduce result |
| `backend/ai/database/task_repository.py` | Occurrence create/existing result; `repository_fallback` for task/occurrence reads and writes that degrade to memory; narrow History read/CAS outcomes for get, commit, scan advance, failure write and reset, in both in-memory test fake and Supabase |
| `backend/ai/task_creation.py` | Creation entry/validation, duplicate check, separate task write, durability refusal, separate state write, compensation start/result, final creation result |
| `backend/bot/handlers/history.py` | Creation entry, eligibility, durable-storage availability, peer validation, baseline, definition validation, handler result |
| `tests/test_history_schedule_tracing.py` | 22 focused trace cases covering the required scenarios, including repository fallback and provider metadata allowlisting |

The service and creation public functions are thin tracing wrappers around
renamed unchanged implementations; no behavior was moved to a new execution
authority.

### 2.1 Repository fallback events

A Supabase occurrence/task operation that degrades to memory emits:

```text
repository_fallback operation=<operation> outcome=memory_fallback category=memory_fallback fallback_backend=InMemoryTaskRepository result=present|missing|hit|miss|rows count=<n>
```

If the fallback itself raises, the event is
`outcome=fallback_failed exception=<class>` and the original exception is
re-raised unchanged. Cancellation is reported as `fallback_cancelled` and
re-raised. The fallback behavior, CAS checks, retry/recovery policy and state
transitions are unchanged.

This event is the only proof that a successful-looking operation was served by
process memory rather than Supabase. It is silent outside an occurrence scope.
`repository_fallback` is emitted for `create_occurrence`, `get_occurrence`,
`claim_occurrence`, `transition_occurrence`, occurrence list/recovery/retry/
waiting-answer reads, `resume_waiting_for_answer`, and the task read/update
paths used during an occurrence. A durable operation emits no such event.

## 3. What the first live run should emit

All execution lines share one occurrence `trace_id`. A successful run should
show this order (optional branches are marked):

1. `due_task_discovered`
2. `occurrence_create_started`
3. `occurrence_persist_result outcome=created|already_exists`
4. `occurrence_create_result`
5. `claim_attempt`
6. `claim_result outcome=claimed`
7. `execution_handoff outcome=started`
8. `execution_entered`
9. `provenance_injected`
10. `actions_planned actions=scheduled_history`
11. `validation_result check=action_chain outcome=passed`
12. `execution_started`
13. `action_started position=1 action=scheduled_history`
14. `tool_invocation action=scheduled_history`
15. `provenance_result outcome=passed`
16. `arguments_result outcome=passed`
17. `service_invocation_started`
18. `task_identity_result outcome=valid`
19. `state_lookup_started`
20. `repository_read_result operation=get_history_schedule outcome=found`
21. `state_lookup_result outcome=found status=active`
22. `state_validation_result outcome=active`
23. `peer_classification_started`
24. `peer_classification_result peer_type=<safe type>`
25. `policy_result outcome=allowed`
26. `upper_bound_capture_started`
27. `upper_bound_capture_result outcome=captured`
28. `retrieval_started`
29. `retrieval_completed messages=<n> scanned=<n> has_more=<bool>`
30. `batch_classified`
31. For a content batch:
    `ai_summarization_started` → optional
    `provider_call_started`/`provider_call_result` → `ai_summarization_result`
    → `output_validation outcome=accepted`
32. `commit_started write_kind=summary`
33. `repository_cas_result operation=commit_history_success outcome=committed`
34. `commit_result write_kind=summary outcome=committed`
35. `checkpoint_result outcome=summary_commit success=True`
36. `checkpoint_completed success=True`
37. `service_result outcome=success`
38. `action_result position=1 action=scheduled_history outcome=succeeded`
39. `terminal_outcome outcome=succeeded`
40. `execution_finished success=True status=succeeded`
41. `execution_result success=True status=succeeded`
42. `outcome_notification`
43. `occurrence_boundary_advanced`

A scan-only safe skip replaces the AI/commit sequence with
`safe_skip_evaluated`, `safe_skip_result`, `scan_advance_result` and
`checkpoint_result outcome=scan_only`; `repository_cas_result
operation=advance_history_scan ... revision_unchanged=True` must show that only
the scan cursor advanced. A summary commit must show old/new revision and
version values on the repository and commit events.

On a degraded Supabase path, a `repository_fallback operation=...` event can
appear immediately after the corresponding occurrence read, claim, transition,
list or task read/write. Its `outcome=memory_fallback` means the operation
succeeded only in process memory; `outcome=fallback_failed` means the memory
fallback also failed. A durable operation emits no fallback event. Presence of
`occurrence_persist_result` or `repository_read_result` alone does not prove
durability.

To find the first failing or missing stage, filter the complete log by the
occurrence's `trace_id` and read the stages in order. The last occurrence stage
before the trace stops is the boundary that did not complete:

- No `due_task_discovered`: the scheduler did not select the task (status,
  `next_run_at`, due query) or schedule parsing rejected it; look for the
  existing `Task <id> was not scheduled` warning.
- `repository_fallback outcome=memory_fallback` after any occurrence/task
  operation: the result came from memory, not Supabase, and is not durable even
  if the operation otherwise succeeded. `outcome=fallback_failed` means both
  the durable operation and the memory fallback failed.
- `claim_result outcome=conflict|not_claimed`: another execution/state owns it.
- `execution_handoff` without `execution_entered`: coordinator invocation
  failed or no execution authority was present.
- `execution_entered` without `actions_planned`: owner/status/action-snapshot/
  registered-action validation failed; the preceding `validation_result` names
  the check and category.
- `actions_planned` without `tool_invocation`: execution stopped before the
  action (AI preparation, wait, cancellation or an exception); check
  `execution_cancelled`, `execution_exception`, `action_wait` or terminal
  outcome.
- `tool_invocation` without `provenance_result`: provenance validation did not
  return; invalid provenance emits `provenance_result outcome=rejected` and
  `service_invocation_started` must be absent.
- `state_lookup_result outcome=missing|unavailable`: missing durable row versus
  unavailable storage are distinct; `repository_read_result` distinguishes
  missing from `durable_error`.
- `policy_result outcome=refused`: peer policy refusal, before Telegram
  retrieval.
- `retrieval_started` without `retrieval_completed`: check
  `retrieval_failed failure_stage=retrieval category=telegram_retrieval`.
- `retrieval_completed` without AI start: inspect `safe_skip_result` or
  `policy_result`.
- `ai_summarization_started` without `ai_summarization_result`: inspect
  `provider_call_result` (`timeout`, `provider_failed`, `empty_output`).
- `output_validation outcome=empty|oversized`: generated output was invalid,
  distinct from provider failure.
- `commit_started` without `repository_cas_result`: service persistence call
  failed before reaching the repository result.
- `repository_cas_result outcome=cas_conflict|stale|durable_error`: CAS
  conflict/stale row versus durable database failure.
- `action_result` without terminal/execution result: coordinator failed while
  persisting occurrence state; check `execution_exception`.
- No `occurrence_boundary_advanced`: execution or notification ended before the
  scheduler advanced the task boundary.

Creation uses `HISTORY_SCHEDULE_TRACE` with the same ordered approach:
`creation_entered` → eligibility/durability → definition validation →
`creation_service_entered` → duplicate check → `task_write_result` →
`state_write_result` → `creation_result`. A state failure is followed by
`compensation_started` and `compensation_result`, with `durable=True|False` and
`outcome=durable_deleted|stale|not_found|error|cancelled`.

## 4. Tests actually run

### 4.1 Focused tracing

Command:

```
.venv/bin/python -m pytest tests/test_history_schedule_tracing.py -q
```

Result: **22 passed**, 1 warning, 0 failed.

The 22 cases cover all 18 required scenarios plus repository fallback and
provider metadata allowlisting: ordered success stages; the same
correlation id across scheduler/coordinator/tool/service; retry with distinct
attempt; concurrent isolation; reset after success, exception and cancellation;
ordinary-request silence; invalid provenance rejection before the service;
missing state versus unavailable storage; peer-policy versus retrieval failure;
retrieval versus AI failure; empty safe-skip versus summary commit; invalid/
oversized output versus provider failure; CAS conflict versus durable error;
scan-only versus summary commit; compensation success/failure; and absence of
message, summary, instruction, credential and provider-body content.

The provider-boundary tests exercise the real `summarize_incremental()` with a
fake provider response for provider failure, timeout, empty output, arbitrary
multiline metadata, and known safe metadata values. Repository fallback tests
use `SupabaseTaskRepository` with a failing fake client and an in-memory
fallback; no live Supabase call is made.

### 4.2 Focused History/scheduler/execution regression

Command:

```
.venv/bin/python -m pytest tests/test_task_repository.py tests/test_task_hardening.py \
  tests/test_task_fallback_classification.py tests/test_history_schedules.py \
  tests/test_history_schedule.py tests/test_history_schedule_ui.py \
  tests/test_task_execution.py tests/test_task_scheduler.py -q
```

Result: **139 passed, 1 failed**. The only failure is the pre-existing
`tests/test_task_scheduler.py::test_recovered_interrupted_occurrence_is_picked_up_by_run_once`
(“interval advancement exceeded bound”), reproduced before this delivery and
unrelated to tracing.

### 4.3 Full applicable suite

Command:

```
.venv/bin/python -m pytest tests -q --ignore=tests/test_runtime_session_terminal.py
```

Result: **7,856 passed, 27 skipped, 1 failed** in 144.98 s. The one failure is
the same pre-existing scheduler recovery test. `tests/test_runtime_session_terminal.py`
is the pre-existing untracked file with collection errors; it was excluded and
not modified.

### 4.4 Compilation and diff checks

```
.venv/bin/python -m compileall -q backend tests   # exit 0
git diff --check                                  # exit 0
```

## 5. Verified offline versus not verified live

Verified offline with real production functions and injected repository,
retrieval, provider and Telegram doubles:

- Correlation propagation through scheduler → coordinator → tool → service.
- Retry attempt separation and concurrent-context isolation.
- Cleanup after success, exception and cancellation.
- Every failure/skip distinction listed in the task.
- Privacy of the trace fields under content sentinels.
- Existing focused behavior and the full applicable test suite.

Not verified live:

- **No real Telegram Scheduled History occurrence was executed.**
- **No live Supabase read/write/CAS operation was performed for this task.**
- No live restart/catch-up, deleted-message gap, provider-failure or capped-peer
  run was performed.
- No deployment or schema change was made.

Passing trace tests prove the instrumentation and its offline distinctions; they
do not prove Scheduled History works end-to-end.

## 6. Remaining diagnostic gaps

- Before an occurrence exists, scheduler recovery/due/retry discovery uses
  `TASK_SCHEDULER_TRACE` debug lines rather than an occurrence-correlated event;
  there is no occurrence key to attach yet. Once a key exists, all subsequent
  stages are correlated.
- The exact event sequence is covered by tests but is not a versioned external
  telemetry schema; future stages must keep the same bounded-field discipline.
- The provider allowlist preserves only known failure origins and finish
  reasons. A provider-private string outside those sets is intentionally
  reported as `unknown`; it is not available for diagnosis. The trace does not
  invent or force-fetch cooldown/retry state.
- Per-stage duration is emitted for the checkpoint and execution result, not
  every intermediate stage. The ordered stages still localize the failing
  boundary.
- Persistent failure metadata continues to use the existing bounded repository
  error text; tracing deliberately does not add summary or message content.
- A fallback event identifies the losing durable operation but does not retry
  the durable write or change the operation's return value. The existing
  fallback/cooldown health contract remains authoritative.
- The pre-existing scheduler recovery test failure remains open and untouched.

## 7. Historical implementation state (unchanged)

The previously delivered foundation remains in place:

- Phase 3A: durable History state investigation/design, complete.
- Phase 3B: `history_schedules` migration applied remotely as
  `20261114180758`; durable-only repository, bounded oldest-first retrieval and
  identifier-free AI payload boundary.
- Phase 4: owner-only schedule wizard, deterministic cadence/baseline/focus,
  separate task and state writes, duplicate refusal and compensating deletion.
- Phase 5 prerequisite: registered `scheduled_history` action, trusted task-id
  and occurrence provenance, bounded incremental checkpoint, safe-skip scan
  advance and CAS commit.
- Phase 6 shared History admission: deferred.
- Phase 7 live validation: deferred; this trace delivery is its observability
  prerequisite.

No phase numbering changed.
