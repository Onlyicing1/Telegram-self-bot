# Implementation Report — Reduce Redundant AI Logging

Logging/observability cleanup only. No AI behavior, provider routing, tool
execution, retry, fallback, diagnostics scheduling or request-handling logic was
changed. The temporary continuation investigation is over, so its per-stage
success-path output no longer belongs at `INFO`.

## Logs retained at INFO

These are the concise, request-level records production operations actually
needs:

- `AI_RESULT` — the primary final request outcome record (success, provider,
  model, finish state, fallback flags, failure type, latency). Unchanged.
- `AI_PROVIDER_EMPTY_RESPONSE_RETRY`, `AI_ACTION_RECOVERY_RETRY` — the request
  only continued because a retry fired; worth seeing without debug enabled.
- `AI_CONTINUATION_FALLBACK` — a continuation round changed provider/model.
- `AI_TOOL_ROUND_LIMIT`, `AI_TOOL_PENDING_EXEC_START`, `AI_TOOL_PENDING_EXEC_END`
  — the bounded-round limit was hit and pending calls were executed once.
- `AI_CONFIRMATION_PENDING`, `AI_CONFIRMATION_EXPIRED`,
  `AI_CONFIRMATION_CONSUMED`, `AI_CONFIRMATION_RESULT` — confirmation lifecycle.
- `AI_PROVIDER_FAILURE_CATEGORY`, provider call failures and exhausted fallbacks
  (WARNING), `AI_TOOL_EXECUTION_FAILED` for an unsuccessful result (WARNING),
  `AI_TOOL_LOOKUP_FAILED` / `AI_TOOL_CALL_VALIDATION_FAILED` (WARNING).

## Logs downgraded or removed

`backend/ai/engine/dispatcher.py`:

- `AI_PROVIDER_CALL_START` → DEBUG; `AI_PROVIDER_CALL_COMPLETE` → DEBUG (failures
  are still reported at WARNING by the provider-failure path).
- `AI_PROVIDER_REQUEST_START` (initial and continuation rounds) → DEBUG.
- `AI_PROVIDER_RESPONSE` (initial and continuation rounds) → DEBUG.
- `AI_PROMPT_BUILD` → DEBUG.
- `AI_TOOL_AVAILABILITY` → DEBUG, and the per-request `names=[...]` list of all
  registered tool names was **removed** from the message (enabled flag and tool
  count kept).
- `AI_TOOL_REQUEST_RECEIVED` → DEBUG.
- `AI_TOOL_CALLS_EMPTY`, `AI_TOOL_CALLS_RECEIVED`, `AI_TOOL_CONTINUATION_STARTED`
  → DEBUG.
- `AI_TOOL_RESULT_RETURNED` (both emit sites) → DEBUG on success, WARNING on an
  unsuccessful result or one that needs confirmation.
- `AI_TOOL_CONTINUATION_COMPLETED` → DEBUG on success, WARNING on failure.
- `AI_TOOL_LOOP_END` → DEBUG.
- Action parsing/validation: `AI_ACTION_PARSE_START`, `AI_ACTION_PARSE_RESULT`
  (both branches), `AI_ACTION_VALIDATION result=clarify|ok`,
  `AI_TARGET_RESOLUTION`, `AI_PROVIDER_EMPTY_RESPONSE_RETRY_RECOVERED`,
  `AI_ACTION_RECOVERY_RETRY_RECOVERED` (both sites) → DEBUG.
- `AI_TOOL_CALL_VALIDATION_FAILED` in the structured-action path (rejected and
  unsupported) → WARNING (raised, not silenced).
- `AI_EXEC_TRACE` routine stages — `provider_selected`, `proactive_authorized`,
  `provider_request`, `provider_response`, `verbatim_tool_result_deferred`,
  `verbatim_tool_result_delivered`, `media_route_skipped`, `media_request`,
  `media_completed`, `telegram_response` → DEBUG.

`backend/ai/tools/executor.py`:

- `AI_TOOL_EXECUTION_STARTED` and `AI_TOOL_EXECUTION_SUCCEEDED` → DEBUG.
- Unsuccessful `AI_TOOL_EXECUTION_FAILED`, `AI_TOOL_CALL_VALIDATION_FAILED`
  (missing name, malformed arguments, non-object arguments) and
  `AI_TOOL_LOOKUP_FAILED` → WARNING.

`backend/ai/continuation_diagnostic.py`:

- `PROVIDER_CONTINUATION_DIAGNOSTIC` level is now classification-driven:
  `CONTINUATION_ACCEPTED`, `CONTINUATION_INCONCLUSIVE`, `UNSUPPORTED` and
  `SKIPPED` are DEBUG; `CONTINUATION_REJECTED`, `UNKNOWN`, timeouts, auth and
  rate-limit outcomes stay WARNING. The probe itself, its scheduling, its budget
  and its request shape are unchanged; no synthetic Gemini continuation request
  is generated without a native model-issued signature.

No other message text changed, so correlation ids and failure evidence are
preserved — the records are simply no longer emitted at INFO.

## Runtime diagnostics intentionally preserved

Inspected and left untouched, because these are runtime safety signals rather
than AI success-path noise:

- `backend/runtime/heartbeat.py` — `READY_BUT_DISCONNECTED`,
  `EVENT_DISPATCH_STALLED`, heartbeat snapshots, 30s interval, recovery triggers.
- `backend/runtime/diagnostics.py` — runtime diagnostics helpers.
- `backend/runtime/tracer.py` — `tracer.trace()` remains WARNING;
  `EVENT_LOOP_STARVATION`, `RUNTIME_HEARTBEAT`, `ASYNC_TASK_DUMP`, task-crash
  records unchanged.
- Task crash handling, supervision, keepalive and recovery paths: not modified.

## Tests and validation

Policy tests updated/added in `tests/test_ai_tool_lifecycle_observability.py`:
success-path lifecycle records are asserted at DEBUG, `AI_RESULT` is asserted to
remain at INFO, and a dedicated test asserts that request/tool/provider failures
remain visible at INFO+ (WARNING). Existing assertions were tightened, not
weakened.

Supporting test changes required by the new severities:

- `tests/test_continuation_diagnostic.py` — accepted/inconclusive probe records
  asserted at DEBUG.
- `tests/test_media_ai_integration.py` — media success traces asserted at DEBUG.
- `tests/test_gemini_thought_signature.py` — the UNSUPPORTED probe record
  asserted at DEBUG.
- `tests/test_ai_fallback_logging.py` — added an autouse fixture that restores
  the `backend` logger level after tests that call `_configure_logging()`. That
  call sets process-wide namespace state; without the restore it silently
  suppressed DEBUG records in later tests of the same run (order-dependent
  failures, not product behavior).

Commands and results:

| Check | Result |
|---|---|
| Focused suites (`test_ai_tool_lifecycle_observability`, `test_continuation_diagnostic`, `test_ai_fallback_logging`, `test_media_ai_integration`, `test_gemini_thought_signature`, `test_tool_calling_root_cause_fixes`, `test_10_tool_calls`, `test_history_ai_tools`, `test_aed_silent_logging`, `test_runtime_diagnostics_classification`) | pass |
| `.venv/bin/python -m pytest -q` (full) | **7110 passed, 27 skipped, 1 failed** |
| `py_compile` on edited modules | pass |
| `git diff --check` | pass |

The single full-suite failure is the pre-existing baseline failure
`tests/test_task_scheduler.py::test_recovered_interrupted_occurrence_is_picked_up_by_run_once`
(`assert 0 == 1`, interval advancement bound) — unrelated to logging and present
before this change.

Log content review: the changed messages carry only ids, counts, booleans, tool
names, error classes and character counts. No argument values, user content,
credentials or message bodies were added to any log record.

## Commit and deployment state

Commit: see the `chore: reduce redundant AI success-path logging` commit on
`main`. Push/remote verification is recorded in the final message of the task.

This change is **not yet deployed**. Render production logging behavior is
unchanged until the new commit is deployed and its logs are inspected there.
