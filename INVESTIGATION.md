# INVESTIGATION

## PROBLEM

The exact outgoing request `هر 1 دقیقه یک بار برای من بنویس سلام` is reported in production as returning `I could not turn that into a safe, unambiguous schedule...` with no durable task row. The current nested repository is `Onlyicing1/Telegram-self-bot`, branch `main`, at `e5c97295ed7c11e5ec15add33b3a9e6403c59288`. The outer workspace contains a pre-existing unrelated `tests/test_stage13.py` modification; it was not inspected for this investigation's implementation and remains outside the nested repository.

## EXACT CURRENT EXECUTION PATH

The active source path is:

```text
Telegram outgoing NewMessage
  -> backend/bot/handlers/ai_unified.py::register
     -> ai_unified_handler
     -> _execute_ai
     -> AIRequest(user_message=prompt_text, chat_id=event.chat_id, ...)
  -> backend/ai/engine/engine.py::Engine.execute
  -> backend/ai/engine/dispatcher.py::Dispatcher.dispatch
     -> conversation runtime
     -> existing _try_local_fast_path(request, ...)
        -> parse_command_intent(request.user_message, has_reply=...)
     -> if no fast-path result: prompt builder
     -> ProviderManager.chat(...)
     -> structured-action parsing / provider tool loop
     -> ToolExecutor.execute_calls
        -> create_task tool, if the provider emits one
     -> backend/ai/tools/task.py::CreateTaskTool.execute
     -> TaskInterpreter.interpret
     -> TaskCreationService.create
     -> TaskRepository.create_task
     -> durable task/occurrence persistence
  -> RuntimeSupervisor-owned TaskScheduler
     -> recover/run_once
     -> TaskExecutionCoordinator
```

The handler only activates after the configured `Nova`/Persian trigger or a reply-to-AI condition. It strips the trigger before `_execute_ai`; the request text passed to `AIRequest.user_message` is otherwise the original prompt text. `_execute_ai` captures `event.chat_id` as `AIRequest.chat_id`.

## PRE-PROVIDER ROUTING

**CONFIRMED:** The current committed dispatcher calls `_try_local_fast_path()` after the conversation-runtime stage and before prompt building/provider selection. That method calls `parse_command_intent()` on `request.user_message` and can execute returned tool calls through the existing `ToolExecutor` without a provider round.

**CONFIRMED:** In the current source, `parse_command_intent("هر 1 دقیقه یک بار برای من بنویس سلام", has_reply=False)` is expected to produce an executable `create_task` result: `_tokenize()` normalizes digits; `_is_scheduling_intent()` requires an action verb and recognizes `هر` plus the nearby `دقیقه`; `parse_command_intent()` returns `action="create_task"`, `target="schedule"`, `schedule_text` equal to the stripped original request, and a `create_task` tool call whose `request` is that same text.

**CONFIRMED:** Therefore the repository source does contain a pre-provider interception for this exact phrase. The prior investigation's statement that the parser was not guaranteed to run before providers is superseded by the current committed dispatcher code.

**IMPORTANT LIMITATION:** The fast path is conditional on both `request.allow_tools` and `self._tool_executor is not None`. `AIRequest.allow_tools` defaults to true in the inspected request contract, but the live runtime's actual request/engine wiring must be confirmed from deployment logs to prove those conditions held for the reported event.

**CONFIRMED SOURCE-LEVEL FAILURE POSSIBILITY:** If the dispatcher has no attached tool executor, the condition `tools_allowed and self._tool_executor is not None` is false, so the parser is not invoked at all and the request proceeds to the provider. `Engine.__init__` initially creates an executor only when a tool registry is supplied; later runtime attachment is responsible for wiring tools. The exact deployed engine wiring for the failing event is not available in this repository snapshot.

## EXACT FAILURE POINT

The exact schedule-rejection text is generated in `backend/ai/tools/task.py::CreateTaskTool.execute()` inside its exception handler around `TaskInterpreter(provider_manager).interpret(...)`:

```text
I could not turn that into a safe, unambiguous schedule, so I did not create any task...
```

The handler catches `TaskInterpretationError`, `asyncio.TimeoutError`, and all other exceptions and returns a failed `ToolResult` with that message. Thus, if the observed text is exactly this message, the source-level implication is that `CreateTaskTool.execute()` was reached and its interpreter call raised or timed out. This contradicts the older hypothesis that the message necessarily means the provider exhausted before `create_task`; the wording itself identifies the CreateTaskTool exception branch.

**CONFIRMED:** The provider/tool-round exhaustion text is generated elsewhere, in `backend/ai/engine/dispatcher.py` when pending provider tool calls remain after `MAX_TOOL_ROUNDS`, and is rendered by `ai_unified.py::_describe_empty_result()` / response delivery. It is a different failure message. The reported schedule-rejection wording is not the dispatcher round-limit wording.

**UNKNOWN:** The supplied production observation combines a schedule-rejection message with a separate observation of `Tool round limit reached`; without the raw request telemetry, it cannot be proven whether these occurred in the same dispatch or in separate attempts.

## TASK INTERPRETER / CREATION PATH

There are two possible current paths:

1. **Local fast path active:** `Dispatcher._try_local_fast_path()` recognizes the request, calls `ToolExecutor.execute_calls()` with `create_task`, and `CreateTaskTool.execute()` is reached. `CreateTaskTool` obtains `provider_manager` from trusted context and calls the bounded provider-backed `TaskInterpreter`. If that interpretation raises, the exact safe-schedule message is returned before `TaskCreationService` and `TaskRepository`.
2. **Fast path unavailable:** if tools are disabled or no executor is attached, `_try_local_fast_path()` is skipped; the normal provider path runs. A provider-emitted `create_task` call can still reach `CreateTaskTool`, while provider round exhaustion can prevent it from being reached.

`CreateTaskTool` then converts a valid `TaskCandidate` to a creation candidate, resolves an explicit `chat_name` through the authenticated client's dialogs, or stores the trusted current request chat from `context.extra["chat_id"]`. Only after that does it call `TaskCreationService.create(...)`. The repository insert is therefore downstream of successful interpretation and destination handling.

For the reported exact schedule-rejection message, **CONFIRMED:** the CreateTaskTool failure branch is the direct source. **LIKELY:** `TaskInterpreter.interpret()` failed to obtain/validate a structured candidate from its provider response, or timed out. **UNKNOWN:** the exact provider response and exception are not present in the repository.

## REQUEST DECOMPOSITION

At the deterministic action-parser level, the exact request decomposes as:

- **Recurrence:** high-confidence recurring interval intent.
- **Interval:** recurrence marker `هر`; time unit `دقیقه`; numeric token `1`; `یک بار` is retained as wording and does not prevent interval recognition.
- **Action:** Persian write verb `بنویس`; deterministic parser maps the whole request to `create_task`, not immediate `send`.
- **Task request:** the stripped original string is passed as `arguments["request"]`.
- **Destination:** no explicit chat name is present; `CreateTaskTool` uses trusted `context.extra["chat_id"]` when available. The model cannot supply an arbitrary numeric destination through this fallback.
- **Owner/context:** `AIRequest.owner_id` comes from the owner-gated handler; `AIRequest.chat_id` comes from the Telegram event; `CreateTaskTool` uses the context owner and dispatcher-injected request chat.

The deterministic parser does not itself extract the final schedule interval or action payload; that remains the responsibility of `TaskInterpreter`, which is provider-backed and validates the resulting `TaskCandidate`.

## TEST COVERAGE

Current nested repository test inventory does not contain the previously reported `tests/test_taskloom_milestone.py`, `tests/test_taskloom_ui.py`, or `tests/test_send_write_immediate.py`. The available focused test `tests/test_task_nl_creation.py` was run during this investigation and passed 11 tests, but it exercises task interpretation/creation behavior rather than proving the live `Dispatcher.dispatch()` path with a provider that would fail if called.

The source contains parser coverage in the repository's existing tests/history, but the current checkout provides no focused test file proving all of the following together:

- actual `ai_unified` handler activation,
- actual `Engine.execute`/`Dispatcher.dispatch`,
- an attached runtime `ToolExecutor`,
- exact Persian request,
- zero provider calls,
- and successful `CreateTaskTool` persistence.

**CONFIRMED COVERAGE GAP:** parser-level recognition is not equivalent to end-to-end production-path proof. A passing parser/task test can coexist with a runtime whose tool executor is absent, whose TaskInterpreter provider fails, or whose deployed commit differs from the checkout.

## REGRESSION / IMPLEMENTATION STATUS

The previous intended fix was to connect deterministic scheduling recognition to the dispatcher before provider calls. The current committed source now visibly contains that connection: `Dispatcher.dispatch()` calls `_try_local_fast_path()` before prompt/provider execution, and `_try_local_fast_path()` calls `parse_command_intent()`.

Therefore the fix was **wired and placed before provider execution in the repository code**. It is not accurate to classify the current repository state as simply “never wired.” The remaining confirmed issue is that successful pre-provider classification only selects the existing `create_task` tool; it does not remove the provider-backed `TaskInterpreter` call inside `CreateTaskTool`. A provider failure there can still produce the exact safe-schedule message and no row.

No duplicate scheduler, executor, or Telegram transport was found in the inspected active path. The current source/report history is internally inconsistent about older destination semantics, but that is not the immediate cause of this schedule-rejection message.

## ROOT CAUSE

**CONFIRMED ROOT CAUSE**

The latest repository code does perform deterministic pre-provider classification, but the exact production failure message is produced after classification by `CreateTaskTool.execute()` when its provider-backed `TaskInterpreter.interpret()` raises or times out. The exception is caught broadly and converted into the safe-schedule rejection, so `TaskCreationService` and `TaskRepository.create_task` are never called and no `ai_tasks` row can be inserted.

The exact underlying interpreter exception/provider response is not available, so the specific provider-side reason for interpreter failure remains unknown. The evidence rules out treating the failure as a pure Persian interval-matcher miss in the current repository.

## EVIDENCE

- `backend/bot/handlers/ai_unified.py::ai_unified_handler` owner-gates outgoing messages, detects the trigger/reply activation, and calls `_execute_ai`.
- `backend/bot/handlers/ai_unified.py::_execute_ai` constructs `AIRequest` with `user_message=prompt_text`, `owner_id=owner_id`, and `chat_id=event.chat_id`.
- `backend/ai/engine/engine.py::Engine.execute` delegates to `Dispatcher.dispatch`.
- `backend/ai/engine/dispatcher.py::Dispatcher.dispatch` invokes `_try_local_fast_path` before prompt building/provider calls when tools are allowed and an executor is attached.
- `backend/ai/engine/dispatcher.py::_try_local_fast_path` calls `parse_command_intent` and executes the resulting `create_task` call through `ToolExecutor`.
- `backend/ai/actions.py::_is_scheduling_intent` checks action verbs, recurrence vocabulary, and interval introduction/time-unit proximity; `parse_command_intent` returns `create_task` for the exact phrase at source level.
- `backend/ai/tools/task.py::CreateTaskTool.execute` calls `TaskInterpreter.interpret` and catches all interpretation exceptions with the exact reported safe-schedule message.
- `backend/ai/task_interpreter.py::TaskInterpreter.interpret` is provider-backed and returns a validated candidate or raises `TaskInterpretationError`.
- `backend/ai/task_creation.py::TaskCreationService.create` and `backend/ai/database/task_repository.py::TaskRepository.create_task` are downstream persistence boundaries.
- `backend/ai/engine/dispatcher.py` separately reports tool-round exhaustion when pending provider tool calls remain after `MAX_TOOL_ROUNDS=3`.
- `backend/ai/task_scheduler.py` and `backend/runtime/supervisor.py` place scheduling/execution downstream of persistence; they cannot execute a task that was never created.
- `tests/test_task_nl_creation.py` passed 11 tests, but no current test proved the complete active handler-to-dispatcher zero-provider-round route.

## MINIMAL NEXT IMPLEMENTATION TARGET

Keep the existing dispatcher fast path and approved ToolExecutor/task boundaries. Add the smallest source-grounded reliability change needed so a high-confidence recurring request reaches a deterministic/validated task-creation input without failing solely inside the provider-backed interpretation step, while preserving the existing provider architecture for ordinary AI requests. Before coding, capture the exact `TaskInterpreter` provider contract/response failure in a focused dispatcher-to-tool regression test. Do not add a scheduler, executor, transport, arbitrary destination mechanism, or database schema change.

## HARD CONSTRAINTS

- Investigation only for this document; no production code, tests, configuration, dependencies, SQL, migrations, Supabase data, or `IMPLEMENTATION_REPORT.md` were modified.
- The Self Bot remains the only Telegram execution authority.
- Providers may reason and interpret but must not directly control Telegram.
- Preserve the existing `TaskInterpreter -> TaskCreationService -> TaskRepository` persistence boundary and the RuntimeSupervisor-owned single TaskScheduler.
- Ordinary AI requests such as `پری یه سرچ بزن` remain provider-driven.
- No duplicate scheduler, executor, retry engine, Telegram transport, or provider architecture.
- No arbitrary Telegram RPC, arbitrary chat IDs, shell execution, or unrestricted recipient selection.
- Owner identity and destination context must remain trusted runtime data.
- No database/schema/Supabase changes are part of this diagnosis.

## VALIDATION

- Verified outer repository and nested repository roots, branch, HEAD, remotes, and status.
- Nested repository: `main`, HEAD `e5c97295ed7c11e5ec15add33b3a9e6403c59288`, remote `https://github.com/Onlyicing1/Telegram-self-bot.git`.
- Confirmed current nested source modifications before this document: `backend/ai/actions.py` and `backend/ai/engine/dispatcher.py`; these are pre-existing implementation changes and were not altered by this investigation.
- Read current `INVESTIGATION.md` and `IMPLEMENTATION_REPORT.md` for context; this file is being completely replaced.
- Inspected current handler, request construction, dispatcher, action parser, task tool, interpreter, creation, repository, scheduler, supervisor, and relevant runtime paths.
- Ran `python3 -m pytest tests/test_task_nl_creation.py -q --no-header`: **11 passed**.
- Ran `python3 -m compileall -q backend tests`: **passed**.
- Ran `git diff --check`: **passed before this document rewrite**; final diff check is required after the rewrite.
- No live Telegram or Supabase verification was performed.
