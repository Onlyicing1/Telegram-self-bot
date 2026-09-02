# INVESTIGATION

Scope: exact source path that turns the `create_task` execution into
`AI_EXEC_TRACE stage=tool_result tool=create_task success=False` for request
`dd62aecd3d7f`. Investigation only — no code, tests, config, schema, Supabase,
or Telegram changes were made. `tests/test_stage13.py` untouched.

## 1. PRODUCTION EVIDENCE

Confirmed ordering from the supplied logs:

1. `create_task` is available (AI_TOOL_AVAILABILITY).
2. `AI_EXEC_TRACE stage=intent_resolved intent=create_task kind=executable` —
   deterministic classifier matched (dispatcher.py:1064).
3. `stage=tool_selected tools=['create_task']` (dispatcher.py:1136).
4. `stage=tool_execute tools=['create_task']` (dispatcher.py:1140).
5. `ROUTER_SCORE` for cohere/gemini/openai/openrouter/groq/sambanova,
   `PROVIDER_SKIPPED provider=you reason=capability=web_search`,
   `ROUTER_SELECTED provider=cohere model=command-a-plus-05-2025 score=0.677`,
   `AI_PROVIDER_ATTEMPT provider=cohere ... attempt=1` (manager.py:144/153/641).
6. ~5.8 s after tool execution began:
   `stage=tool_result tool=create_task success=False` (dispatcher.py:1160).
7. `stage=telegram_response success=True` (dispatcher.py:1205).

Only ONE `AI_PROVIDER_ATTEMPT attempt=1` appears; no `attempt=2`, no further
provider attempts, no `AI_PROVIDER_FAILOVER`. The failure surfaced ~5.8 s in —
far below `create_task`'s 45 s executor cap (task.py:29) and the interpreter's
30 s cap (task.py:28, interpreter.py:27), so no executor/interpreter timeout
fired.

Because `intent_resolved` → `tool_selected` → `tool_execute` → `tool_result`
→ `telegram_response` is emitted back-to-back by
`Dispatcher._try_local_fast_path` (dispatcher.py:1064–1205), the execution ran
on the deterministic fast path, not the provider tool loop.

## 2. FAILED TOOLRESULT ORIGIN

`backend/ai/tools/task.py`, class `CreateTaskTool`, method
`async def execute(self, context, arguments)`. It contains exactly seven
`ToolResult(success=False, ...)` returns:

| # | Line | Message | Reached from |
|---|------|---------|--------------|
| 1 | task.py:87 | `Missing task request argument.` | `arguments["request"]` not a non-empty string |
| 2 | task.py:90 | `Task request is too long.` | request > 2000 chars |
| 3 | task.py:94 | `Owner identity is unavailable; task was not created.` | `context.owner_id` invalid |
| 4 | task.py:108 | `AI provider manager is unavailable; task was not created.` | no `provider_manager` after fallback lookup |
| 5 | task.py:117–124 | `I could not turn that into a safe, unambiguous schedule, so I did not create any task. Restate it as an interval (e.g. 'every X minutes'), a time, or a daily/weekly cadence with a clear action.` | single `except (TaskInterpretationError, asyncio.TimeoutError, Exception)` around `TaskInterpreter.interpret()` |
| 6 | task.py:170 | `Could not resolve chat destination:\n{options}` | candidate carried `chat_name` that `resolve_chat_name` failed to resolve |
| 7 | task.py:194 | `The task could not be persisted; nothing was created.` | `except (TaskCreationError, Exception)` around `TaskCreationService.create()` |

The `ToolExecutionResult` wrapping that becomes the logged `tool_result`
event is built in `backend/ai/tools/executor.py::_execute_single` (success
path returns at executor.py:261–267; the `except Exception` fallback at
executor.py:290–299). The dispatcher fast path then emits
`stage=tool_result tool=%s success=%s` (dispatcher.py:1158–1161).

## 3. EXACT FAILURE BRANCHES

Conditions for each branch, with current-source reachability for this request:

1. **task.py:87** — unreachable here: the deterministic parser already
   validated `request` (actions.py:494–496 rejects empty/missing before
   emitting the tool call).
2. **task.py:90** — unreachable here: the parser enforces the same 2000-char
   bound (actions.py:497–498) as `MAX_REQUEST_CHARS`.
3. **task.py:94** — requires a ToolContext without a valid owner id. The fast
   path builds context from the authenticated AIRequest
   (dispatcher.py:940–976); possible only on a wiring bug, no evidence.
4. **task.py:108** — requires `provider_manager` to be absent from
   `context.extra` AND `get_engine()` to fail. Excluded: a provider attempt
   is proven by the logs (manager.py:641 runs inside
   `ProviderManager.chat`, reached only via that manager).
5. **task.py:117–124** — fires whenever `TaskInterpreter.interpret()` raises
   any exception or the 30 s `wait_for` expires. Every interpreter failure
   (§5) funnels here. Occurs strictly AFTER the provider attempt. Consistent
   with the observed logs and timing.
6. **task.py:170** — requires the candidate to contain
   `notification_destination.chat_name`; only reachable after a successful
   interpretation. Not excluded by logs but produces a distinguishable
   message; also implies interpreter success (§7 gap).
7. **task.py:194** — fires on any exception from `TaskCreationService.create`
   (task_creation.py:24–78: field checks, `parse_schedule`, and the
   repository call). `create()` logs
   `TASK_CREATE_PERSIST_ATTEMPT repository=%s owner_id=%s has_ai_instruction=%s`
   (task_creation.py:71–74) BEFORE touching the repository; the Phase 1
   Supabase repository additionally logs its own attempt/success/fallback
   events. None of those lines appear in production logs.

## 4. ACTUAL ACTIVE PATH

CONFIRMED (source + logs):

- The active path is the deterministic fast path: `_try_local_fast_path`
  resolved `create_task`, built tool context via `_build_tool_context`
  (dispatcher.py:961–965 attempted `_build_deterministic_task_candidate`),
  and executed through `ToolExecutor.execute_calls` (dispatcher.py:1144).
- A provider call occurred inside `create_task` — i.e. inside
  `TaskInterpreter.interpret()` (task.py:111 → interpreter.py:63–66) — so the
  deterministic candidate was NOT injected; the interpreter ran a live
  provider round.
- The failed `ToolResult` returned through `_execute_single` (executor.py) to
  `execute_calls`, was logged at dispatcher.py:1160, summarized by
  `_summarize_tool_results`, and delivered via `_build_fast_path_result`
  (dispatcher.py:1180–1205) — which is why the Telegram response still
  succeeded.

UNKNOWN (not observable from current logs): which single sub-branch inside
branch 5 produced the failure, and (if branch 6 applied) that it was reached
at all. Enumerated interpreter failure points, all raising
`TaskInterpretationError` and all collapsing into the one wrapper at
task.py:117:

- interpreter.py:42 — request empty/too long (impossible here; parser-validated)
- interpreter.py:85 — `ProviderManager.chat` raised (manager never raises; it
  returns `success=False` responses — manager.py:101 docstring and §5 — so
  this is effectively dead for provider errors)
- interpreter.py:87 — `response.success is False` (provider failure →
  `chat()` returns the emergency-fallback response with
  `success=False`, manager.py:830–868)
- interpreter.py:90 — empty/non-string `response.text`
- interpreter.py:95 — `json.loads` failed OR `parse_candidate_output` raised
  `TaskCandidateError` (task_candidate.py:47–107: field-set mismatch
  `"candidate fields are incomplete or unsupported"`, invalid label/timezone,
  action canonicalization, `MAX_PAYLOAD_BYTES`, schedule mismatch). Note:
  the interpreter's own instruction tells the model to answer ambiguous
  requests with JSON `null`; `json.loads("null")` yields `None`, and
  `from_untrusted(None)` raises `TaskCandidateError("candidate must be an
  object")` — a self-instruction-driven rejection path.

Timing (~5.8 s, single attempt=1, no attempt=2 / failover / skipped lines for
the other scored providers) is consistent with: Cohere's one attempt returned
quickly with either `success=False` (branch interpreter.py:87) or a
non-empty but unusable text (interpreter.py:90/95 — non-empty text suppresses
`_has_no_usable_output` failover, manager.py:727–740, so no second provider
is tried). It is NOT consistent with a timeout (branch would need ≥30 s) or
with branch 7.

## 5. TASKINTERPRETER / PROVIDER CONTRACT

- Call site: `CreateTaskTool.execute` →
  `TaskInterpreter(provider_manager).interpret(request, timezone=tz_str)`
  wrapped in `asyncio.wait_for(..., 30)` (task.py:111–113).
- Input: the ORIGINAL user request string, passed verbatim by the
  deterministic parser: `parse_command_intent` classifies the message and
  `tool_calls=[{"name": "create_task", "arguments": {"request":
  result.schedule_text}}]` (actions.py:496–500); `schedule_text` is the
  stripped request (actions.py:298–306). No transformation or translation.
- Prompt: system message with the safety/schema instructions (incl. Persian
  interval recognition and "return JSON null if ambiguous"), a second system
  message containing `CANDIDATE_SCHEMA` JSON (interpreter.py:25–36, 47–60),
  and the user request as the user message (interpreter.py:61–65).
- Provider call: `self._providers.chat(messages, tools=[])` — i.e.
  `ProviderManager.chat` routes to Cohere `command-a-plus-05-2025`; `chat()`
  NEVER raises; failures become `success=False` responses
  (manager.py:101–198; `_call_once` converts timeouts/exceptions into
  `success=False` responses, manager.py:596–620; exhausted candidates end in
  `_fallback` with `success=False` and `errors` metadata, manager.py:830–868).
  No interpreter-internal provider fallback exists beyond the manager's own
  chain — only Cohere was attempted per the logs.
- Expected output: exactly one JSON object matching `CANDIDATE_SCHEMA`;
  parsed via `json.loads` then `parse_candidate_output` →
  `TaskCandidate.from_untrusted` (interpreter.py:68–72,
  task_candidate.py:47–107), which normalizes send aliases to the single
  bounded `send_message{text}` action and rejects everything else.
- Failure conversion: ANY of the above failures → `TaskInterpretationError`
  → caught at task.py:117 → branch-5 ToolResult. The exception message and
  cause are discarded there (the except clause binds nothing).

## 6. PERSISTENCE STATUS

**Persistence is not reached in this failure path.**

Source proof: branches 1–5 and (if reached) 6 all return before
task.py:186–190, where `TaskCreationService` is constructed. Branch 5 — the
confirmed active branch — returns at the interpreter wrapper. Corroborating
log proof: `TaskCreationService.create` logs `TASK_CREATE_PERSIST_ATTEMPT`
(task_creation.py:71–74) before any repository work, and the Phase 1
Supabase task repository logs its own persistence events; none appear in the
supplied production logs. Therefore `TaskRepository.create_task`, the Supabase
insert, and the in-memory fallback were NOT involved, and no database failure
can be inferred from the missing row.

## 7. MISSING PRODUCTION EVIDENCE

The logs do not expose:

- the returned `ToolResult.message` — the fast-path result log at
  dispatcher.py:1158–1161 prints only `tool` and `success`, unlike the
  provider tool loop, which logs `AI_EXECUTION_ERROR ... error=%s`
  (dispatcher.py:683–687);
- the exception class / `TaskInterpretationError` cause swallowed at
  task.py:117 (and, if branch 7 were ever active, the cause swallowed at
  task.py:193);
- the provider response outcome: `response.success`, `response.text`,
  `metadata.provider_matrix` / `errors` from manager.py:157–198 / 830–868 —
  needed to separate "provider returned failure" from "provider returned
  unusable JSON (incl. the instructed `null`)".

Single sanitized log statement that would expose the missing information on
the next run: inside the `except` at `backend/ai/tools/task.py:117`, one line
logging the failure category and exception class only — e.g.
`AI_EXEC_TRACE request_id=%s stage=create_task_interpret_failed category=%s`
with category ∈ {provider_failure, provider_empty_output, candidate_invalid,
timeout} derived from the `TaskInterpretationError.__cause__` / exception
type — plus logging `er.message` (static tool message, no request text) in
the fast-path `stage=tool_result` line at dispatcher.py:1160. No request
text, message contents, tokens, or provider credentials would be emitted.

## 8. MINIMAL NEXT IMPLEMENTATION

Smallest observability change (do NOT implement in this investigation):

1. In `backend/ai/tools/task.py::execute`, replace the bare
   `except (TaskInterpretationError, asyncio.TimeoutError, Exception):` at
   task.py:117 with a form that binds the exception and emits the single
   sanitized category log described in §7 before returning the same
   ToolResult message. Optionally do the same for the persistence `except`
   at task.py:193.
2. In `backend/ai/engine/dispatcher.py:1158–1161`, include the static tool
   message (`er.message`) in the fast-path `stage=tool_result` log line.

With those two lines, the next production occurrence distinguishes
provider-failure vs unusable-output vs candidate-validation vs timeout
immediately, from source-verified evidence rather than inference. No
behavioral change is required for observability.

## 9. OUT OF SCOPE

This investigation does NOT implement:

- AI-backed per-occurrence preparation
- Phase 1 durable AI instruction representation
- scheduler changes
- occurrence state changes
- Delete fix
- timezone fix
- provider redesign
- database changes
- Telegram changes
- retry-engine changes
