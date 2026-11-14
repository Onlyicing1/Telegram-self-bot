# IMPLEMENTATION REPORT — Scheduled History diagnostics tracing

## 0. Current delivery

This delivery adds observability only. It does not change Scheduled History
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

## 2. Instrumented stages and files

| File | Trace coverage |
|---|---|
| `backend/ai/task_trace.py` | New occurrence/creation context variables, scopes, silence outside scope, bounded formatting |
| `backend/ai/task_scheduler.py` | Recovery scan/transitions, due discovery, occurrence create/existing result, claim, handoff, retry discovery, execution result, notification, boundary advance |
| `backend/ai/task_execution.py` | Wrapper entry/reset/cancellation/exception, owner/status/action validation, provenance injection, planned actions, per-action start/result, waits, terminal outcome |
| `backend/ai/tools/history_schedule.py` | Tool invocation, provenance result, argument result, service call/result/exception |
| `backend/services/history_schedule_service.py` | Task identity, state lookup/validation, peer policy, upper bound, retrieval, safe skip, AI call, output validation, CAS commit, failure lifecycle, terminal checkpoint |
| `backend/services/history_ai_service.py` | Chunking/map capacity, provider call/timeout/failure/empty output, map/reduce result |
| `backend/ai/database/task_repository.py` | Occurrence create/existing result; narrow History read/CAS outcomes for get, commit, scan advance, failure write and reset, in both in-memory test fake and Supabase |
| `backend/ai/task_creation.py` | Creation entry/validation, duplicate check, separate task write, durability refusal, separate state write, compensation start/result, final creation result |
| `backend/bot/handlers/history.py` | Creation entry, eligibility, durable-storage availability, peer validation, baseline, definition validation, handler result |
| `tests/test_history_schedule_tracing.py` | 19 focused trace cases covering the required scenarios |

The service and creation public functions are thin tracing wrappers around
renamed unchanged implementations; no behavior was moved to a new execution
authority.

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

To find the first failing or missing stage, filter the complete log by the
occurrence's `trace_id` and read the stages in order. The last occurrence stage
before the trace stops is the boundary that did not complete:

- No `due_task_discovered`: the scheduler did not select the task (status,
  `next_run_at`, due query) or schedule parsing rejected it; look for the
  existing `Task <id> was not scheduled` warning.
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

Result: **19 passed**, 1 warning, 0 failed.

The 19 cases cover all 18 required scenarios: ordered success stages; the same
correlation id across scheduler/coordinator/tool/service; retry with distinct
attempt; concurrent isolation; reset after success, exception and cancellation;
ordinary-request silence; invalid provenance rejection before the service;
missing state versus unavailable storage; peer-policy versus retrieval failure;
retrieval versus AI failure; empty safe-skip versus summary commit; invalid/
oversized output versus provider failure; CAS conflict versus durable error;
scan-only versus summary commit; compensation success/failure; and absence of
message, summary, instruction, credential and provider-body content.

The provider-boundary test exercises the real `summarize_incremental()` with a
fake provider response for provider failure, timeout and empty output.

### 4.2 Focused History/scheduler/execution regression

Command:

```
.venv/bin/python -m pytest tests/test_history_schedule.py tests/test_history_schedules.py \
  tests/test_history_schedule_ui.py tests/test_task_execution.py \
  tests/test_task_scheduler.py tests/test_task_creation_diagnostics.py \
  tests/test_task_recursion_and_message_id_provenance.py -q
```

Result: **114 passed, 1 failed**. The only failure is the pre-existing
`tests/test_task_scheduler.py::test_recovered_interrupted_occurrence_is_picked_up_by_run_once`
(“interval advancement exceeded bound”), reproduced before this delivery and
unrelated to tracing.

### 4.3 Full applicable suite

Command:

```
.venv/bin/python -m pytest tests -q --ignore=tests/test_runtime_session_terminal.py
```

Result: **7,853 passed, 27 skipped, 1 failed** in 146.01 s. The one failure is
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
- Provider identity and fallback metadata are logged only when the existing
  provider response exposes them. The trace does not invent or force-fetch
  cooldown state.
- Per-stage duration is emitted for the checkpoint and execution result, not
  every intermediate stage. The ordered stages still localize the failing
  boundary.
- Persistent failure metadata continues to use the existing bounded repository
  error text; tracing deliberately does not add summary or message content.
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
