# INVESTIGATION

## CURRENT PRODUCTION EVIDENCE

The supplied logs prove:

1. The Dispatcher classified the request as executable `create_task`.
2. The existing `ToolExecutor` was attached and selected `create_task`.
3. The request entered the provider-backed task path. Provider routing selected Cohere and attempted `command-a-plus-05-2026`.
4. Approximately 3.2 seconds later, the executor recorded `create_task success=False`.
5. Telegram delivery of the failure response succeeded.

The logs do **not** include the provider response body, response metadata, exception class/message, candidate validation error, or repository persistence markers. Therefore they cannot identify the exact inner failure by themselves.

## EXACT EXECUTION TRACE

The current source path is:

```text
Dispatcher.dispatch(request)
  -> _try_local_fast_path()
  -> parse_command_intent()
  -> create_task tool call
  -> ToolExecutor.execute_calls()
  -> ToolExecutor._execute_single()
  -> CreateTaskTool.execute()
  -> TaskInterpreter.interpret()
  -> ProviderManager.chat(..., tools=[])
  -> provider routing / selected Cohere provider
  -> ProviderResponse
  -> JSON decoding
  -> parse_candidate_output()
  -> TaskCandidate.from_untrusted()
  -> destination resolution, if requested
  -> TaskCreationService.create()
  -> TaskRepository.create_task()
  -> SupabaseTaskRepository.create_task(), or fallback
```

The Dispatcher’s local classification does not itself create the task. In the current production path, `_build_tool_context()` only attaches a deterministic candidate for the narrow interval/write form it can build. The supplied logs show a provider routing attempt, so the `CreateTaskTool` path proceeded without an attached deterministic candidate for this request.

`ToolExecutor._execute_single()` invokes the tool inside its bounded timeout and converts a returned unsuccessful `ToolResult` into the logged `tool_result success=False`. It also converts an uncaught tool exception into a failed `ToolExecutionResult`, but the supplied log does not show which of those two cases occurred.

## EXACT FAILURE

The first source location that intentionally converts a provider/interpreter failure into the user-facing safe-schedule failure is `backend/ai/tools/task.py::CreateTaskTool.execute()`:

```python
try:
    candidate = await asyncio.wait_for(
        TaskInterpreter(provider_manager).interpret(request, timezone=tz_str),
        timeout=INTERPRET_TIMEOUT_SECONDS,
    )
except asyncio.CancelledError:
    raise
except (TaskInterpretationError, asyncio.TimeoutError, Exception):
    return ToolResult(
        success=False,
        message="I could not turn that into a safe, unambiguous schedule...",
    )
```

The broad exception clause means the user-facing result does not preserve the original exception. `TaskInterpreter.interpret()` can raise `TaskInterpretationError` for any of these concrete conditions:

- provider call raises an exception;
- provider returns `success=False`;
- provider returns empty/non-string text;
- provider text is not valid JSON;
- JSON does not satisfy `TaskCandidate.from_untrusted()`.

The current source and supplied production logs do **not** recover which of these occurred. No `TaskCreationService` or repository-specific error can be concluded from the shown logs, because the interpreter failure conversion occurs before those calls.

There is a second failure conversion in `CreateTaskTool.execute()` around creation:

```python
try:
    service = TaskCreationService(get_repository_manager().task, owner_id)
    task = await service.create(candidate, datetime.now(timezone.utc))
except asyncio.CancelledError:
    raise
except (TaskCreationError, Exception):
    return ToolResult(success=False, message="The task could not be persisted; nothing was created.")
```

That branch is distinguishable by its different message, but the supplied `AI_EXEC_TRACE stage=tool_result success=False` line does not include the `ToolResult.message`. Thus the log excerpt cannot prove whether the interpreter branch or creation branch produced the failure. It also cannot prove whether a later destination-resolution return (`Could not resolve chat destination`) was used.

## PROVIDER CONTRACT

`TaskInterpreter.interpret(request, timezone)` enforces:

- request must be a nonblank string of at most `MAX_REQUEST_CHARS = 2000` characters;
- one provider request is made through `ProviderManager.chat(messages, tools=[])`;
- the provider receives two system messages and one user message:
  - instructions requiring exactly one JSON task candidate;
  - the JSON-serialized `CANDIDATE_SCHEMA`;
  - the stripped original request;
- provider tools are explicitly disabled for this interpretation call (`tools=[]`);
- when a timezone is supplied, the prompt asks for that IANA timezone and says interval schedules carry no timezone field;
- the response must be `ProviderResponse.success == True`;
- `response.text` must be nonblank JSON;
- JSON must be an object with exactly these fields:

```text
label
schedule_type
schedule
timezone
actions
notification_destination
```

`TaskCandidate.from_untrusted()` then requires:

- nonblank bounded label, maximum 256 characters;
- nonblank bounded timezone, maximum 128 characters;
- object schedule and destination;
- one to five actions;
- each action has a tool name and object arguments;
- message aliases are canonicalized to the registered `send_message` action with bounded `text` only;
- unknown action names are retained as candidate data and are later subject to the existing execution/registry boundary;
- schedule parsing succeeds for `once`, `interval`, `daily`, or `weekly`;
- non-interval schedule timezone equals task timezone;
- optional `chat_name` is a bounded nonblank string; numeric model-provided chat IDs are not accepted as a candidate destination field by the candidate contract.

For the exact Persian request, the current schema can represent the intended static operation if the provider returns a valid candidate, for example an interval schedule plus a canonical `send_message` action and empty destination. The source does not prove that Cohere returned such a candidate.

## PERSISTENCE STATUS

Based on the supplied evidence and current source:

- `TaskInterpreter.interpret()` is definitely reached: provider routing and a provider attempt occur inside that call.
- Whether `TaskInterpreter` completed successfully is unknown.
- `TaskCandidate` validation may or may not have been reached; the logs omit the response and validation result.
- `TaskCreationService.create()` is reached only after successful interpretation and destination handling. The supplied logs do not prove it was reached.
- `TaskRepository.create_task()` is reached only after successful `TaskCreationService` validation. The supplied logs do not prove it was reached.
- `SupabaseTaskRepository.create_task()` is not proven to have been attempted. Its `TASK_PERSIST_CREATE_ATTEMPT` log is absent from the supplied excerpt, but absence from an excerpt is not proof that it did not occur.
- The in-memory fallback is not proven to have been involved. It would only be used after a Supabase repository exception, and the repository fallback log is absent from the excerpt.
- No database row visibility conclusion can be made from this log excerpt alone.

If the safe-schedule interpreter message was returned, the failure occurred before persistence. If the persistence message was returned, the failure occurred in task creation, schedule validation, destination handling, repository construction, Supabase insert, or fallback creation. The excerpt does not include the distinguishing message.

## ROOT CAUSE STATUS

**UNKNOWN — INSUFFICIENT PRODUCTION EVIDENCE**

The provider attempt and later failed tool result narrow the path, but the current broad exception handling removes the exact exception from the visible result, and the supplied trace omits the returned `ToolResult.message` plus the interpreter/provider/persistence logs needed to distinguish the remaining branches. A provider failure, malformed/incomplete provider JSON, candidate validation failure, destination resolution failure, schedule parsing failure, repository failure, and fallback activation therefore cannot be ranked as confirmed from this evidence.

The strongest source-derived conclusion is that the failure is most likely converted in one of the two broad exception boundaries in `CreateTaskTool.execute()`, with the first boundary (`TaskInterpreter.interpret`) occurring before any persistence. It is **not** source-proven that persistence was attempted.

## MINIMAL NEXT IMPLEMENTATION TARGET

Add sanitized, request-correlated observability at the existing boundaries without changing behavior:

1. Log entry/exit around `TaskInterpreter.interpret()` with request ID, input length, provider name, response success, finish reason, text length, tool-call count, and a categorized failure (`provider_exception`, `provider_failure`, `empty_response`, `invalid_json`, `candidate_validation`). Never log prompt text, provider credentials, or message contents.
2. Preserve the existing exception class as structured log metadata before converting it to the safe user-facing `ToolResult`.
3. Include a sanitized failure category in the `create_task` `ToolResult.data` or trace event so `AI_EXEC_TRACE stage=tool_result` identifies interpreter versus creation failure without exposing secrets.
4. Retain the existing task-service/repository persistence attempt, success, and fallback markers and correlate them with the same request ID.

This is the smallest diagnostic change needed to make the next production occurrence distinguishable. No scheduler, AI precomputation, Taskloom redesign, Delete fix, timezone fix, schema change, or Telegram behavior change is required for this investigation.

## REQUIRED CONSTRAINTS

- No production code changed.
- No tests changed.
- No dependencies or configuration changed.
- No database/schema/SQL/Supabase mutation performed.
- No Telegram mutation or live verification performed.
- No architecture redesign performed.
- `tests/test_stage13.py` was preserved.
- Only this `INVESTIGATION.md` file is intended to be changed.
