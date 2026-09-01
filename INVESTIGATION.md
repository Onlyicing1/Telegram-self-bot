# INVESTIGATION

## 1. PRODUCTION EVIDENCE

The supplied ordering proves that the request was classified as executable
`create_task`, entered `ToolExecutor`, and reached provider routing. Cohere was
selected and an attempt was logged. Roughly two seconds later the tool result
was `success=False`; the Telegram response itself then succeeded. The excerpt
does not include the provider response body, its metadata, the interpreter
exception, or the tool result message/error. The later `[Errno 11]` warnings
occur after the Telegram response and concern message/provider-statistics
persistence.

## 2. ACTUAL SOURCE CALL CHAIN

The current path is:

`Dispatcher._try_local_fast_path()` → `parse_command_intent()` resolves the
request to an executable `create_task` call carrying the original stripped
request under `arguments["request"]` →
`ToolExecutor.execute_calls()` → `_execute_single()` →
`CreateTaskTool.execute()`.

`CreateTaskTool.execute()` validates the request and owner, then calls
`TaskInterpreter(provider_manager).interpret(request, timezone=tz_str)` unless
`context.extra["deterministic_task_candidate"]` exists. The current dispatcher
can construct that deterministic candidate in
`Dispatcher._build_tool_context()` for the narrow interval/write shape; when it
does, the provider is bypassed for the tool itself. The supplied provider logs
therefore indicate that this particular runtime path did not use that
candidate, or that the provider logs belong to an earlier provider stage; the
excerpt alone cannot reconcile that distinction.

The provider path is:

`TaskInterpreter.interpret()` → `ProviderManager.chat(messages, tools=[])` →
selected provider → `ProviderResponse` → `json.loads(response.text)` →
`parse_candidate_output()` → `TaskCandidate.from_untrusted()` →
`TaskCandidate.as_creation_candidate()` → destination resolution →
`TaskCreationService.create()` → `TaskRepository.create_task()` → either
`SupabaseTaskRepository.create_task()` and its insert, or an in-memory
repository/fallback.

`ToolExecutor` returns a failed `ToolExecutionResult` whenever the tool returns
a failed `ToolResult`; it does not expose the tool's internal exception unless
its own outer execution wrapper catches an exception.

## 3. FIRST FAILURE POINT

The earliest failure point proven by the source and compatible with the
provider timing is `TaskInterpreter.interpret()`, after the provider returns.
Its provider call is bounded and its response is accepted only when all of the
following succeed: `response.success` is true, `response.text` is nonblank,
JSON decoding succeeds, and `parse_candidate_output()` accepts the exact
candidate contract.

However, the production excerpt does not contain the distinguishing response
or exception log. Therefore the precise first failing sub-operation cannot be
identified from the supplied production evidence.

## 4. FAILURE MECHANISM

`CreateTaskTool.execute()` has two broad exception-conversion boundaries:

1. The interpreter block catches `asyncio.TimeoutError` and
   `TaskInterpretationError` (the tuple also redundantly includes `Exception`).
   It returns the generic message beginning:
   `I could not turn that into a safe, unambiguous schedule...`.
2. The creation block catches `TaskCreationError` (again redundantly with
   `Exception`) and returns:
   `The task could not be persisted; nothing was created.`

`TaskInterpreter` converts provider exceptions into `TaskInterpretationError`
(`task interpretation provider failed`), provider `success=False` into
`TaskInterpretationError`, blank output into `TaskInterpretationError`, and
`JSONDecodeError` or `TaskCandidateError` into `TaskInterpretationError`.
`TaskCandidate.from_untrusted()` rejects non-object/prose output, missing or
extra fields, invalid labels/timezones/schedules/actions, oversized payloads,
invalid schedule data, and unsafe message-action content.

`Production exception not observable from current logs.` The exact data lost is
the provider response text/metadata and the chained underlying exception or
candidate validation message. The smallest diagnostic needed is a
request-correlated, sanitized log at the `CreateTaskTool` interpreter `except`
that records `request_id`, failure category (`provider_failure`, `timeout`,
`empty_output`, `json_invalid`, or `candidate_invalid`), exception class, and
bounded sanitized message—never provider response secrets or user payload.

## 5. PROVIDER CONTRACT

`TaskInterpreter` sends three messages: a system instruction requiring exactly
one JSON object, a system message containing `CANDIDATE_SCHEMA`, and the
original stripped user request. It calls `ProviderManager.chat(..., tools=[])`.
The expected text is a JSON object with exactly these fields:

- `label` (bounded string)
- `schedule_type` (`once`, `interval`, `daily`, or `weekly`)
- `schedule` (object)
- `timezone` (bounded IANA-like string accepted by downstream schedule checks)
- `actions` (one to five action objects)
- `notification_destination` (object)

For message writing, the prompt requires action `send_message` with only a
bounded `text` argument and no destination. The parser canonicalizes permitted
send aliases to `send_message`; other names remain subject to the registered
tool boundary. Interval schedules use seconds and do not require a schedule
timezone; non-interval schedules must match the task timezone. JSON `null`,
ordinary prose, empty text, malformed JSON, missing fields, extra fields, or
invalid schedule/action data are rejected before candidate creation.

ProviderManager itself catches provider exceptions and returns a failed
`ProviderResponse`; it also may exhaust fallback routing and return
`success=False`. Thus a provider attempt log does not prove that a usable
candidate reached `TaskCandidate` construction. Conversely, no source evidence
shows that a candidate reached persistence for this failed result.

## 6. PERSISTENCE STATUS

The failing result is not sufficient to prove persistence. If interpreter
validation failed, destination resolution failed, or the provider manager
returned failure, `TaskCreationService.create()` is never reached. If creation
is reached, the service validates/parses the schedule and logs
`TASK_CREATE_PERSIST_ATTEMPT` before calling the repository.

A Supabase insert is attempted only when the repository manager was initialized
with both `SUPABASE_URL` and `SUPABASE_SERVICE_ROLE_KEY` and the task repository
is `SupabaseTaskRepository`. Its `create_task()` logs an attempt and calls
`table("ai_tasks").insert(payload).execute()`. Supabase exceptions fall back to
`InMemoryTaskRepository.create_task()`; validation `ValueError`/`TypeError` is
re-raised instead. With no Supabase configuration, the manager selects the
in-memory repository directly. The supplied logs do not show any of these
persistence markers, so TaskCreationService, repository creation, Supabase
insert, and fallback are **not proven reached**. No database state can be
inferred from the excerpt.

## 7. [Errno 11] SEPARATE ISSUE

The later `[Errno 11] Resource temporarily unavailable` warnings are emitted
by the unrelated asynchronous AI message/statistics persistence paths after
the Telegram response. The supplied ordering does not establish causality,
and the inspected create-task path does not use those operations as its
interpreter or task-repository result. They must be investigated separately;
they cannot be identified as the cause of `create_task success=False` from this
evidence.

## 8. ROOT CAUSE STATUS

**UNKNOWN — INSUFFICIENT PRODUCTION EVIDENCE**

The source identifies the exact conversion boundaries and all principal
provider/interpreter validation branches, but the production excerpt omits the
provider response and the sanitized exception/category needed to select one
branch. It also contains no task-persistence marker proving that creation was
attempted.

## 9. MINIMAL NEXT IMPLEMENTATION TARGET

Add only request-correlated, sanitized failure-category logging at the two
`CreateTaskTool` exception boundaries, preserving exception chaining and
without logging raw request text, provider output, credentials, or Telegram
payloads. The log must distinguish interpreter/provider failures from creation,
destination, and persistence failures and include the exception class and
bounded sanitized message. No behavior or architecture change is required to
make the next production run diagnostic.

## 10. OUT OF SCOPE

This investigation does not implement:

- AI-backed per-occurrence preparation
- Phase 1 durable AI instruction representation
- scheduler changes
- new occurrence states
- Delete fix
- timezone fix
- database schema changes
- provider redesign
- new retry engines
- new executors
- new Telegram transports

No production code, tests, dependencies, configuration, SQL/schema, Supabase,
or Telegram behavior was changed. The unrelated `tests/test_stage13.py`
modification was preserved.
