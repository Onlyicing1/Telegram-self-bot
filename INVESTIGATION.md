# INVESTIGATION

## PROBLEM

The exact request `هر 1 دقیقه یک بار برای من بنویس سلام` does not create a durable Taskloom task in the deployed service. No task row is inserted, while the user observes `Tool round limit reached`. The immediate request `بنویس سلام` can execute, and the Taskloom UI can render.

## ROOT CAUSE

**CONFIRMED:** The deterministic scheduling parser exists in `backend/ai/actions.py`, but the normal natural-language AI path is not guaranteed to invoke it before provider execution. The incoming message is routed by `backend/bot/handlers/ai_unified.py` into the AI dispatcher/provider flow. In that flow, scheduling is represented as a provider-selected `create_task` action/tool rather than as an unconditional deterministic pre-provider classification. A provider/tool-round exhaustion therefore returns `Tool round limit reached` before `create_task` reaches task creation or persistence.

**LIKELY:** The reported failure is caused by provider routing/tool-round exhaustion consuming the request before the provider emits a valid `create_task` call. This explains the absence of a database row more directly than a Persian interval parsing defect.

**UNKNOWN:** The exact production provider response and number of rounds for this specific Telegram message are not available in the repository. Source inspection cannot prove whether the provider emitted malformed output, selected another tool repeatedly, or exhausted rounds for another provider-specific reason.

## RELEVANT FILES

- `backend/bot/handlers/ai_unified.py`
- `backend/ai/engine/dispatcher.py`
- `backend/ai/actions.py`
- `backend/ai/task_interpreter.py`
- `backend/ai/task_candidate.py`
- `backend/ai/tools/task.py`
- `backend/ai/task_creation.py`
- `backend/ai/database/task_repository.py`
- `backend/ai/task_scheduler.py`
- `backend/ai/task_execution.py`
- `backend/runtime/supervisor.py`
- `backend/runtime/task_guard.py`
- `backend/health.py`
- relevant tests under `tests/`

## RELEVANT FUNCTIONS / CLASSES

- `ai_unified` outgoing-message handler and its AI request construction
- `Dispatcher.dispatch()` / provider execution and local action handling
- `parse_command_intent()` and scheduling-intent helpers in `backend/ai/actions.py`
- `parse_action_text()` / `validate_action()` / `resolve_tool_calls()`
- `TaskInterpreter.interpret()`
- `CreateTaskTool.execute()`
- `TaskCreationService.create()`
- `TaskRepository.create_task()` implementations
- `TaskScheduler.start()`, `recover()`, `run_once()`
- `TaskExecutionCoordinator.execute()`
- runtime supervisor scheduler startup
- task snapshot/diagnostic logic in `backend/runtime/task_guard.py` and health diagnostics

## CURRENT EXECUTION PATH

For `هر 1 دقیقه یک بار برای من بنویس سلام`, the source-supported path is:

```text
Telegram outgoing NewMessage
  -> backend/bot/handlers/ai_unified.py
  -> AI request with original message text and runtime context
  -> backend/ai/engine/dispatcher.py
  -> provider/tool loop
  -> provider must return a structured create_task action, or provider text must be parsed
  -> backend/ai/actions.py validates create_task and builds {name: create_task, request: ...}
  -> backend/ai/tools/task.py::CreateTaskTool.execute
  -> TaskInterpreter.interpret(request)
  -> TaskCreationService.create(...)
  -> TaskRepository.create_task(...)
  -> persisted task/occurrence rows
  -> RuntimeSupervisor-owned TaskScheduler
  -> TaskScheduler.recover()/run_once()
  -> TaskExecutionCoordinator
  -> registered ToolExecutor action
```

The critical observed branch ends earlier:

```text
provider/tool loop
  -> tool rounds exhausted
  -> `Tool round limit reached`
  -> no create_task tool call
  -> no TaskInterpreter call
  -> no TaskCreationService call
  -> no TaskRepository insert
```

The repository proves that `create_task` is an executable action and has a service/repository path, but it does not prove that every natural-language request is deterministically classified as `create_task` before provider dispatch.

## ROUTING FINDINGS

**CONFIRMED:** `create_task` is present in the action vocabulary, executable action set, and tool resolution path in `backend/ai/actions.py`. It requires a non-empty `request` field and resolves to the registered `create_task` tool.

**CONFIRMED:** The provider is involved in the ordinary AI request path. The dispatcher performs provider/tool rounds and has a maximum-round failure represented by `Tool round limit reached`.

**CONFIRMED:** Immediate write handling has a deterministic local path for `بنویس سلام` / `write hello`, which explains why that comparison case can work without the same scheduling classification.

**CONFIRMED:** The scheduling helpers and deterministic command-intent machinery are not equivalent to a universal pre-provider classifier. Their existence does not establish that the unified natural-language handler always invokes them before provider dispatch.

**LIKELY:** The provider is currently allowed to decide whether a natural-language request becomes a scheduled task by emitting `create_task`. Therefore the provider can consume or exhaust the request before deterministic scheduling gets a chance to create a task.

## TASK INTERPRETER FINDINGS

`TaskInterpreter` is a bounded provider-backed interpreter for a task request. Its contract validates a structured schedule/action candidate; it is not itself the first Telegram-message router.

The current candidate normalization supports canonical `send_message` action aliases and interval schedule fields. The surrounding source and tests cover Persian and English interval forms, including forms equivalent to `هر 1 دقیقه`, `هر یک دقیقه`, `هر 5 دقیقه`, `every minute`, and `every 5 minutes`.

**CONFIRMED:** The exact Persian phrase is recognized by the deterministic scheduling-intent logic in `actions.py` based on normalized digits, interval introduction, and a time-unit token. The phrase includes `هر`, `1`, `دقیقه`, and `یک بار`; the tokenizer normalizes the numeric digit and preserves the Persian time unit.

**CONFIRMED:** If `TaskInterpreter.interpret()` is reached and the provider returns a valid candidate, the candidate can represent an interval schedule and a message action.

**UNKNOWN:** No live provider response for this exact phrase is available, so the exact candidate produced in production cannot be proven. The source also does not prove that the interpreter is reached in the failing run.

## PERSISTENCE FINDINGS

**CONFIRMED:** Task creation persists through `CreateTaskTool` -> `TaskCreationService.create()` -> owner-scoped `TaskRepository.create_task()` in the task database repository. The repository handles task ownership, initial status/version, and durable task/occurrence persistence according to its existing schema contract.

**CONFIRMED:** The failing request does not reach persistence if `Tool round limit reached` is returned first. Therefore the missing database row is a consequence of the earlier routing/provider failure, not evidence that the database insert itself is the primary failure.

**UNKNOWN:** Live Supabase availability, RLS behavior, and production insert errors were not inspected or exercised. No SQL was executed.

## SCHEDULER FINDINGS

**CONFIRMED:** `RuntimeSupervisor` owns the Taskloom scheduler lifecycle. The scheduler has startup recovery/loading and a polling execution loop; scheduled execution is separate from task creation.

**CONFIRMED:** Task creation and scheduled execution use the supervisor-created scheduler architecture rather than a second scheduler path in the inspected source.

**CONFIRMED:** The scheduler cannot execute a task that was never persisted. In the reported failure, scheduler registration/loading is downstream of the missing insert.

**UNKNOWN:** The production scheduler’s exact runtime state during this request is not proven by source. No live process inspection was performed.

## PROVIDER / TOOL-ROUND FINDINGS

**CONFIRMED:** Provider calls occur in the normal AI dispatch path. Structured actions are converted into registered tool calls and executed through the existing tool executor.

**CONFIRMED:** The dispatcher has a bounded tool-round loop and returns `Tool round limit reached` when the maximum is exhausted.

**CONFIRMED:** A round-limit result prevents a later `create_task` tool call in that dispatch, so `TaskInterpreter`, `TaskCreationService`, and `TaskRepository.create_task` are not reached by that request.

**LIKELY:** Scheduling is unnecessarily dependent on provider tool selection for ordinary natural-language scheduling requests, because the deterministic scheduling vocabulary exists but is not established as an unconditional pre-provider route.

## DIAGNOSTIC FINDINGS

**CONFIRMED:** The reported diagnostics show recent loop progress, recent Telegram updates/events, recent event dispatch, and recent RPC activity. Those signals do not support a proven event-loop stall during the failed request.

**CONFIRMED:** `TASK_STARVATION` is based on long-lived task snapshots being unchanged/stale. Permanent tasks such as Telethon loops, keepalive, profile scheduler, and task scheduler can remain unchanged while legitimately waiting.

**LIKELY:** The warning is a snapshot-age heuristic rather than proof that the scheduler is blocked, deadlocked, or starved. It is therefore not a demonstrated cause of this failure.

**UNKNOWN:** Without production task stacks or a reproduction with runtime tracing, the warning cannot be ruled out as a secondary operational issue, but the supplied recent-heartbeat evidence does not connect it to task creation failure.

## CONFIRMED FACTS

- The exact request is intended to be recurring and should create a durable interval task.
- Persian digit normalization and interval/time-unit vocabulary exist.
- `create_task` is a registered executable action/tool path.
- Task creation has a repository persistence boundary.
- The provider/tool loop can terminate with `Tool round limit reached`.
- A round-limit termination occurs before a task insert if no `create_task` call has been emitted.
- Immediate `بنویس سلام` has a distinct deterministic immediate-write path.
- No database row means the task creation boundary was not completed for the observed request.
- The supplied health signals show recent loop/update/event/RPC activity.
- No source, schema, or runtime evidence proves a scheduler deadlock for this request.

## LIKELY CAUSES

- Provider selection/tool-loop exhaustion occurs before scheduling intent is converted into `create_task`.
- The deterministic scheduling parser is a safety/fallback mechanism rather than the mandatory first routing boundary for unified natural-language requests.
- The production provider may be failing to emit the exact structured `create_task` payload required by the dispatcher/interpreter contract.

## UNKNOWN / MISSING EVIDENCE

- The exact deployed commit/configuration/provider model at failure time.
- The provider’s raw responses and tool-call sequence for the exact Persian phrase.
- Whether the failing request entered a local fast path, provider path, or provider fallback branch in that specific production event.
- The exact TaskInterpreter candidate/error, if the interpreter was invoked.
- Live Supabase insert/RLS outcome.
- Live scheduler task stacks and persisted-task count during the incident.

## DESIRED BEHAVIOR

The incoming phrase should be classified as a high-confidence recurring request before ordinary provider reasoning can consume it. It should produce a validated interval task with action text `سلام`, persist it under the authenticated owner, and record the trusted task-creation chat/context as its default destination. The scheduler should later load and execute the durable task through the existing registered tool and TelegramAPI boundaries.

## IMPLEMENTATION PLAN

Do not implement in this investigation. The smallest future implementation boundary is:

1. Establish a deterministic, pre-provider scheduling-intent route for high-confidence Persian/English recurring requests.
2. Pass the original request unchanged into the existing task creation/interpreter/service boundary.
3. Preserve ordinary conversational/provider routing for non-scheduled requests such as `پری یه سرچ بزن` and ordinary immediate actions.
4. Add focused routing tests proving the exact phrase and comparison cases, including the no-provider-round guarantee for deterministic scheduling.
5. Keep persistence, scheduler, ownership, destination, and Telegram execution within the existing approved boundaries.

## ALREADY IMPLEMENTED

- Persian/English tokenization and digit normalization in `backend/ai/actions.py`.
- Deterministic intent vocabulary for scheduling and immediate writes.
- Structured `create_task` action validation and resolution.
- `TaskInterpreter` schedule/action candidate validation.
- `TaskCreationService` and owner-scoped task repository persistence.
- Durable scheduler polling/recovery and registered-tool execution.
- Immediate write routing through the existing send tool.
- Provider tool-round bounding and explicit round-limit failure.
- Runtime health/diagnostic snapshots and long-lived task monitoring.

## REMAINING WORK

- Prove and implement the pre-provider deterministic scheduling routing boundary in a separate implementation task.
- Add/adjust only focused regression tests for exact Persian/English scheduling and provider bypass behavior.
- Reproduce with the deployed provider and capture sanitized provider/tool-round evidence.
- Separately assess whether the starvation heuristic needs improvement; it is not established as the Taskloom root cause.

## DATABASE / SCHEMA IMPACT

No schema change appears necessary for the routing defect. The existing task persistence contract is downstream and already has the fields needed for interval scheduling, ownership, versioning, and task context. Do not execute SQL or modify Supabase during this investigation.

## HARD CONSTRAINTS

- Investigation only; no production implementation was performed.
- The Self Bot remains the sole Telegram execution authority.
- Providers may reason and propose structured actions but must not directly control Telegram.
- Reuse the existing dispatcher, TaskInterpreter, services, repository, scheduler, ToolExecutor, and TelegramAPI boundaries.
- No second executor, scheduler, retry engine, Telegram transport, arbitrary RPC, arbitrary chat ID, or provider bypass.
- Owner identity must remain trusted runtime context; natural-language/model output cannot override it.
- Database/Supabase schema and data are manually controlled; no migrations or SQL changes.
- Ordinary AI requests must remain ordinary provider requests unless they satisfy the high-confidence scheduling boundary.

## VALIDATION

- Inspected current nested repository branch/HEAD/status before editing.
- Read the prior `INVESTIGATION.md` as context and replaced it entirely.
- Inspected the current Taskloom routing, action parser, interpreter, creation, repository, scheduler, execution, supervisor, and diagnostic paths.
- Searched current source for scheduling, `create_task`, provider rounds, scheduler lifecycle, and `TASK_STARVATION` references.
- Compared the supplied production evidence against the source path.
- No tests were modified or added.
- No production code, configuration, dependency, migration, SQL, Supabase data, or UI was modified.
- No live Telegram or Supabase verification was performed.
- Final file-scope verification is required after this rewrite: only `INVESTIGATION.md` should be the new change inside the nested repository; the outer pre-existing `tests/test_stage13.py` modification remains untouched.
