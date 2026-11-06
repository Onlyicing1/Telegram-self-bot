# IMPLEMENTATION_REPORT.md — AED log silence + end-to-end AI tool observability

> This file describes ONLY the current verified state of the repository for this
> task and **replaces the previous report in full**. Earlier phases' history is
> in git and their contracts are in `AGENTS.md`; nothing here restates them.

| Item | Value |
|---|---|
| Task | (1) remove every Anti-Edit/Anti-Delete log line at every level; (2) make the complete AI tool lifecycle observable from Dispatcher → ToolExecutor → Dispatcher |
| Branch / base | `main` at `6958985` (= `origin/main` before this change) |
| Date | 2026-11-06 |
| Live Telegram / Supabase / Render | **NOT touched.** No session, no RPC, no SQL, no migration, no `.env`, no deployment, no Render shell |
| New dependency | **none** |
| New production module | `backend/ai/tools/lifecycle.py` (event emitter only — no execution, no routing, no state) |
| Tool execution authority | unchanged: `ToolExecutor` remains the sole caller of `tool.execute()` |
| Interpreter / test runner | CPython 3.12 venv, `pytest` from `.venv` |

---

## 1. Anti-Edit / Anti-Delete: zero logs at every level

### 1.1 What was removed

The AED subsystem logged on nearly every event path. Every one of those calls
was **deleted**, not downgraded — no AED record is produced at `DEBUG`, `INFO`,
`WARNING`, `ERROR` or `EXCEPTION`.

`backend/bot/handlers/anti_edit_delete.py` (was 7 log sites):

| Removed | Was |
|---|---|
| `[AED] snapshot skipped: peer is not a confirmed human` | INFO, once per non-human/private-scope message |
| `[AED] edit skipped: peer is not a confirmed human` | INFO, per ineligible edit |
| `[AED] deletion skipped: peer is not a confirmed human` | INFO, per ineligible deletion |
| `[AED] peer entity unresolved` | DEBUG, per unresolved entity |
| `[AED] snapshot capture failed` | WARNING, per capture exception |
| `[AED] edit capture failed` | WARNING, per capture exception |
| `[AED] deletion capture failed` | WARNING, per capture exception |
| `aed retention inline edit failed` | WARNING, per retention-input edit failure |

`backend/services/anti_edit_delete_service.py` (was 20 log sites): configuration
store unreadable / invalid JSON, legacy-store import deferral, import success and
failure, "configuration not loaded — refusing to overwrite", Telegram flood-wait
deferral (both branches), peer resolved as bot/human, peer-resolution failure,
two "edit-shaped update ignored" DEBUG lines, archive-store-unreadable capture
and deletion skips, archive write failures, both retention write failures, media
object delete failure, media over-limit, media snapshot failure.

`import logging` and `logger = logging.getLogger(__name__)` are gone from both
modules, so a future log statement cannot be added by accident without a new
import.

### 1.2 What was deliberately preserved

* Event handlers are still registered in the same order, on the same events
  (`NewMessage`, `MessageEdited`, `MessageDeleted`), from `register()`.
* Snapshot / edit / deletion capture paths are byte-identical except for the
  removed log statement; the "honest miss" returns are unchanged.
* Fail-closed peer classification is unchanged: only a resolved Telegram `User`
  with `bot is False` may be captured; `None`, a non-`User` entity, or a missing
  flag is still UNKNOWN and dropped before the service gate. Nothing was
  loosened to avoid a log line.
* Persistence, retention, category filters, media snapshots, archive scope
  filtering and the browse/detail re-proof are untouched.
* `asyncio.CancelledError` is still re-raised in every handler and in the
  service's resolution/media paths. Exception handlers that previously only
  logged now contain `pass` or the pre-existing `return`; none swallow a
  `CancelledError`.
* No global filter, no root-logger change, no logging configuration change, no
  level downgrade.

---

## 2. AI tool lifecycle observability

### 2.1 Architecture (unchanged)

```
ProviderManager → Dispatcher → ToolRegistry/ToolExecutor → Tool → Service → Telegram/Supabase
```

No second executor, router, dispatcher, scheduler or loop was added.
`backend/ai/tools/lifecycle.py` only formats and emits bounded log events; it
holds no state and makes no decision.

### 2.2 Where each stage is instrumented

| Stage | Location | Event |
|---|---|---|
| AI request received by Dispatcher | `dispatcher.dispatch`, after tool definitions are built | `AI_TOOL_REQUEST_RECEIVED` (INFO) |
| Provider response carries tool calls | tool loop, per round | `AI_TOOL_CALLS_RECEIVED` (INFO, count + names) |
| Provider returned no tool calls | tool loop, per round | `AI_TOOL_CALLS_EMPTY` (INFO) |
| Structured action rejected / unsupported | `_apply_structured_action` | `AI_TOOL_CALL_VALIDATION_FAILED` (INFO) |
| Malformed / non-object arguments, missing name | `ToolExecutor._execute_single` | `AI_TOOL_CALL_VALIDATION_FAILED` (INFO) |
| Tool name not in `ToolRegistry` | `ToolExecutor._execute_single` | `AI_TOOL_LOOKUP_FAILED` (INFO) |
| Permission / confirmation gate | `ToolExecutor._execute_single`, `Dispatcher._gate_confirmation_results` | `AI_TOOL_CONFIRMATION_REQUIRED` (INFO) |
| Tool execution accepted + started | `ToolExecutor._execute_single` (after lookup and gate) | `AI_TOOL_EXECUTION_STARTED` (INFO, argument shape) |
| `tool.execute()` succeeded | `ToolExecutor._execute_single` | `AI_TOOL_EXECUTION_SUCCEEDED` (INFO, latency, bounded sizes) |
| `tool.execute()` returned `success=False` | `ToolExecutor._execute_single` | `AI_TOOL_EXECUTION_FAILED` (INFO, `tool_result_unsuccessful`) |
| Tool timed out | `ToolExecutor._execute_single` | `AI_TOOL_EXECUTION_TIMEOUT` (**WARNING**) |
| Tool raised | `ToolExecutor._execute_single` | `AI_TOOL_EXECUTION_FAILED` (**WARNING**, `exception:<Type>`) |
| Per-turn call limit exceeded | `ToolExecutor.execute_calls` | `AI_TOOL_EXECUTION_FAILED` (INFO, `max_tools_exceeded`) |
| Tool accepted but never executed (no executor) | tool loop | `AI_TOOL_EXECUTION_SKIPPED` (**WARNING**) |
| Result returned to the Dispatcher | tool loop and salvage path | `AI_TOOL_RESULT_RETURNED` (INFO, status, latency) |
| Continuation provider request after tools | tool loop | `AI_TOOL_CONTINUATION_STARTED` (INFO) |
| Continuation provider response | tool loop | `AI_TOOL_CONTINUATION_COMPLETED` (INFO) |

Pre-existing events that duplicated these (per-batch `AI_TOOL_EXECUTION_START` /
`AI_TOOL_EXECUTION_END`, per-result `AI_EXECUTION_RESULT` / `AI_EXECUTION_ERROR`,
`AI_EXECUTION_START`, `AI_TOOL_PARSE`, `AI_TOOL_ARGS`, `AI_TOOL_PENDING_RESULT`,
`AI_ACTION_VALIDATION result=rejected|unsupported`) were **replaced** by the
events above rather than left alongside them, so one lifecycle transition emits
one line. `AI_TOOL_LOOP_END` and the provider-level `AI_PROVIDER_*` lines remain.

### 2.3 Correlation

`request_id` already existed end-to-end: `AIRequest.request_id` → `Dispatcher`
(`rid`) → `ToolContext.extra["request_id"]` (`_build_tool_context`, the single
place a per-request context is built) → `ToolExecutor`. The executor reads it
from that context, so no new id is generated per layer.

* `round` is `1` for the initial provider round and `n+1` for the n-th
  continuation; the executor receives it as a new optional `round_no` argument
  (default `1`, so the task-execution coordinator's existing calls are
  unchanged).
* `call` is the provider's own tool-call id. When a provider omits it, a
  bounded synthetic id (`auto-r<round>-c<index>`) is used **in the log only** —
  the provider-visible call dict is never modified.
* An event for a tool call executed at the round limit reports
  `round=tool_rounds_executed+1` and `detail=at_round_limit=True`.

All events of one request therefore share `id=<request id>` and can be ordered
by `round` + `call`.

### 2.4 Security

* Arguments are never logged: `arg_count`, `arg_fields` (names) and `arg_types`
  only.
* Results are never logged: `success`/`status`, `elapsed_ms`,
  `message_chars`, `data_keys`.
* Exceptions log only the exception **type**; the message (which may carry a
  credential or user content) is discarded from the log. Internal
  `ToolExecutionResult.error` is unchanged, and the dispatcher reduces it to a
  bounded class (`error_class`) before logging.
* Every field is length-bounded and single-lined (`lifecycle.bounded`).
* No `SESSION_STRING`, `API_HASH`, `API_ID`, API key, authorization header,
  chat text, tool argument value or tool result content is emitted.

### 2.5 Levels

INFO: request/round lifecycle, call count, empty calls, validation rejection,
lookup failure, confirmation requirement, execution start/success/failure
classification, result returned, continuation start/complete.
WARNING: timeout, unexpected exception, invalid execution state
(`AI_TOOL_EXECUTION_SKIPPED`). DEBUG: not used for any lifecycle event, so no
required diagnosis is hidden behind a debug flag.

---

## 3. Exact production files changed

| File | Change |
|---|---|
| `backend/ai/tools/lifecycle.py` | **NEW.** Bounded event emitter: `emit`, `arg_shape`, `call_id`, `error_class`, `bounded`. Observability only. |
| `backend/ai/tools/executor.py` | Emits validation/lookup/confirmation/start/success/failure/timeout events for every tool call; accepts `round_no`; reads `request_id`/`provider_name`/`model_name` from `ToolContext.extra`; sanitizes the two status-callback debug lines. Execution order, timeout rules, long-running exemptions, history recording, permission gate and `execute_confirmed` semantics unchanged. |
| `backend/ai/engine/dispatcher.py` | Emits request/calls/empty/validation/confirmation/result/continuation events; passes `round_no` and provider/model labels into the per-request tool context; replaces the duplicated per-batch log lines. Tool loop, round limit, salvage, confirmation round-trip, verbatim-read rules and provider fallback unchanged. |
| `backend/bot/handlers/anti_edit_delete.py` | All logging removed (7 sites + import); capture handlers unchanged otherwise. |
| `backend/services/anti_edit_delete_service.py` | All logging removed (20 sites + import); capture/persistence/retention/scope logic unchanged. |

---

## 4. Tests added / modified

Added (no existing test modified):

| File | Covers |
|---|---|
| `tests/test_aed_silent_logging.py` (11 tests) | source guarantee that no AED log statement exists; silent snapshot/edit/deletion skips (unresolved, bot, non-`User`, disabled); silent capture failures for all three events; human snapshot/edit/deletion still captured; fail-closed classification still blocks a bot and a non-`User` entity; identity fields still captured |
| `tests/test_ai_tool_lifecycle_observability.py` (13 tests) | success start+success correlation (id/round/call/tool/provider/model); unsuccessful `ToolResult`; timeout at WARNING; raising tool sanitized (secret absent from logs); unknown tool; malformed arguments; confirmation-required vs executed (and `execute_confirmed`); argument values/result content absent, shape present; multiple calls correlated individually incl. synthetic id; dispatcher empty-tool-calls event; full dispatcher→executor→continuation path sharing one request id |

---

## 5. Test results (actually run)

| Check | Command | Result |
|---|---|---|
| Focused new tests | `.venv/bin/python -m pytest tests/test_aed_silent_logging.py tests/test_ai_tool_lifecycle_observability.py -q` | **24 passed** |
| Broader relevant suite (task tree) | AED + AI tool/dispatcher/task/todo/memory/scope suites (20 files) | **630 passed** |
| Post-merge verification (delivered HEAD) | AED + AI tool/dispatcher + provider-policy/fallback-logging/runtime-wiring suites (14 files) | **328 passed** |
| Compilation | `python -m py_compile` on all changed/new modules, then the whole `backend/` tree | passed |
| Whitespace | `git diff --check` | clean |

Not run: the complete repository suite (the only known pre-existing failure,
`tests/test_task_scheduler.py::test_recovered_interrupted_occurrence_is_picked_up_by_run_once`,
is unrelated and untouched).

---

## 6. Unimplemented / unverified

* **No live production log has been verified.** No Render log, Telegram session,
  Supabase instance or deployment exists in this workspace, so the claim is
  "the events are emitted by the tested code paths at INFO", not "these lines
  are visible on Render right now".
* **Tool-selection defects are not claimed fixed.** This task adds
  observability; it does not prove that any particular "the AI did not use the
  tool" incident is resolved. The new events make the failing stage
  identifiable (no call / rejected / unregistered / not executed / failed /
  result mishandled) but do not change model behaviour.
* The provider's own HTTP attempt/retry/fallback lines were already present and
  are unchanged; the new work starts at the Dispatcher's tool boundary.
* Task-execution (`TaskExecutionCoordinator`) tool calls emit the same executor
  events but reach the executor without a Dispatcher `request_id`; their events
  still show `round`, `tool`, `call` and status, with `id=-`. This is
  pre-existing (that path has no request id) and was not changed.

---

## 7. Behaviour that did not change

Tool registration semantics, permission/confirmation semantics, argument
contracts, execution order, timeout behaviour, long-running exemptions, tool
history persistence, provider fallback, continuation limits, Telegram execution
boundaries, AED capture/retention/scope and `CancelledError` propagation.
