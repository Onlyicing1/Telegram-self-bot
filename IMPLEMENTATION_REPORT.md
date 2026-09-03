# IMPLEMENTATION REPORT

## 1. IMPLEMENTATION METADATA

| Field | Value |
|---|---|
| Repository | `Onlyicing1/Telegram-self-bot` |
| Branch | `main` |
| Starting HEAD | `3a70cec9190a177de06a127725ec788ab3be0bcd` |
| Final HEAD | The delivery commit containing this implementation and current-state report |
| Report reconciliation | The recovered `create_task`/`AI_TASK_TRACE` documentation from the newer remote report was retained in §4 while stale prior task-show delivery metadata was replaced with this implementation's current state |
| Implementation date | 2026-09-03 |
| Task/chunk | Replace regex-based task natural-language routing with AI-driven registered-tool selection and extend task retrieval semantics |
| Work type | investigation + implementation |
| Final implementation status | **COMPLETE WITH LIMITATIONS** — source, unit, provider-schema, dispatcher/executor contract, and compile checks pass; live Telegram verification remains unproven |

This is the single current-state report for the implementation. It records
only behavior and validation established from the current source, working-tree
diff, and commands actually run.

---

## 2. OBJECTIVE

The task was to replace the current deterministic task-show routing with
proper AI-driven tool selection. The previous task-specific branch in
`backend/ai/actions.py::parse_command_intent` recognized a finite Persian and
English vocabulary and executed `task_list` or `task_inspect` before any
provider round. That solved the narrow production request **"لیست تسک رو ببین"**
but made arbitrary task phrasing depend on continued parser vocabulary rather
than semantic tool selection.

The intended path is:

```text
Telegram request
  → AI provider receives registered tool schemas
  → native tool call or validated JSON action
  → ToolRegistry / ToolExecutor
  → existing TaskManagementService
  → real task result
```

The implementation also extends retrieval semantics so a model can express
requests such as “show completed tasks” through an optional validated
`task_list.status` argument rather than another phrase-specific rule.

The Self Bot remains the execution authority. Providers propose tool calls;
they do not receive direct Telegram, SQL, shell, or arbitrary HTTP execution
access.

---

## 3. ROOT CAUSE

### Architectural root cause

The concrete architectural cause was the deterministic task-show branch in
`backend/ai/actions.py`:

- `_TASK_STEMS`, `_EN_TASK_WORDS`, and `_TASK_LIST_VERBS` encoded a finite
  language vocabulary.
- `_has_task_mention`, `_extract_task_id`, and
  `_parse_task_show_intent` converted matching text directly to
  `task_list`/`task_inspect` calls.
- `parse_command_intent` ran from the dispatcher fast path before the provider
  was asked to interpret the request.

That mechanism bypassed AI semantic selection for task-management requests and
could not generalize to new wording such as status-filtered reads or lifecycle
operations without adding more local vocabulary.

### Earlier production symptom

The supplied production evidence was that **"لیست تسک رو ببین"** produced
narration, provider retry/fallback behavior, and an error instead of a task
operation. The current implementation does not claim live resolution of that
request without a functioning provider: the required architecture deliberately
routes task phrasing back through AI selection. The current source proves the
local action/schema/executor contracts, but it does not prove a live provider's
semantic output.

### Failure-layer classification

- Prior architectural failure: **deterministic intent-routing layer**.
- Provider behavior in the supplied live incident: **not independently
  reproduced in this workspace**.
- Current live production outcome: **NOT PROVEN** until Telegram/provider
  verification is performed.

---

## 4. EXACT IMPLEMENTATION CHANGES

### `backend/ai/actions.py`

- Removed the deterministic task vocabulary and task-show helpers:
  `_TASK_STEMS`, `_EN_TASK_WORDS`, `_TASK_LIST_VERBS`,
  `_has_task_mention`, `_extract_task_id`, and
  `_parse_task_show_intent`.
- Task-management phrases now fall through `parse_command_intent` as
  `conversational`, allowing the provider path to select a registered tool.
- Added `status` to the validated action-field allowlist and to
  `ActionParseResult`.
- Added an action guard so `status` is accepted only by `task_list`.
- Added case-insensitive validation for the closed lifecycle vocabulary:
  `active`, `paused`, and `completed`.
- `resolve_tool_calls` now maps a valid task-list status to the concrete
  `task_list` executor call; invalid or misplaced fields remain rejected.
- `parse_action_text` preserves the normalized status value in its result.

### `backend/ai/task_management.py`

- Extended `TaskManagementService.list_tasks` to accept an optional status.
- Filtering occurs after the existing owner-scoped repository query and uses
  the task record's canonical status. Repository interfaces and persistence
  ownership rules were not changed.

### `backend/ai/task_management_interface.py`

- Extended `list_text` with a keyword-only optional status filter.
- Filtered results render a status-aware title while preserving the existing
  `No tasks found.` empty-result text and list formatting.

### `backend/ai/tools/task_management_tools.py`

- Updated `TaskListTool.description` to advertise status-filtered reads.
- Exposed `status` as an optional string enum in the tool schema.
- Validated the tool argument at execution time and delegated the filtered
  operation to the existing `TaskManagementService`/`list_text` boundary.
- No new persistence or execution authority was introduced.

### `backend/ai/tools/executor.py`

- Added task lifecycle tool labels for in-flight status updates:
  `task_list`, `task_inspect`, and `task_transition`.
- Native calls still execute only through the existing `ToolExecutor`.

### `backend/ai/prompt/template.py`

- Added semantic task-tool guidance covering list, inspect, pause/resume, and
  complete operations.
- Documented that task IDs and optimistic-concurrency versions must come from
  actual task-tool results rather than being invented.
- Added the status-filter JSON fallback example for completed tasks.

### Focused tests

- `tests/test_task_show_intent.py` now verifies that former task-vocabulary
  sentences reach the AI path, while valid task JSON actions still resolve to
  registered calls and invalid actions/fields remain rejected.
- `tests/test_new_tool_action_path.py` covers valid, normalized, invalid, and
  misplaced `task_list.status` JSON values.
- `tests/test_capability_exposure_tools.py` covers real registry/executor
  reachability, owner-scoped filtered results, invalid status handling, and
  provider-facing schema generation. It also asserts that optional
  `task_list.status` is present in `properties` but absent from `required`.

No provider implementation, database schema, migration, Telegram RPC surface,
or UI was changed.

### Recovered current-state documentation — `create_task` interpretation fix + `AI_TASK_TRACE` observability

The remote current-state report also documents the already-delivered
`create_task` observability work recovered from git history. That documentation
is retained here so this report does not discard the newer remote report-only
commit while describing the current implementation.

**Proven root cause of the original no-diagnostic `create_task` failures:**
`backend/ai/task_interpreter.py` → `TaskInterpreter.interpret()`. Providers
frequently wrap the compliant JSON candidate in markdown fences (` ``` `/` ```json `);
the original code called `json.loads(raw)` directly, the `JSONDecodeError`
collapsed into `TaskInterpretationError`, and the tool boundary's anonymous
`except (…, Exception)` returned a generic message while logging nothing —
production showed `tool=create_task success=False` with zero diagnostics.
Failure layer: AI interpretation parsing (not registry/executor/dispatcher/
repository). Evidence: the source investigation at commit `c40af4a` proved

the interpretation wrapper (branch 5 of seven `ToolResult(success=False)`
branches in `CreateTaskTool.execute`) was the active one by timing (~5.8 s),
single provider attempt, and absence of persistence log lines.

**Exact changes recorded in the remote report:**

- `backend/ai/task_interpreter.py` — `_load_candidate_json(raw)` retries
  extraction against a markdown-fenced block (`_JSON_BLOCK_RE`) when direct
  `json.loads` fails; candidate validation remains with
  `parse_candidate_output`. `interpret(..., request_id=...)` threads the
  correlation id into `AI_TASK_TRACE stage=interpretation_start`, logs
  structured provider failures and successes, logs candidate rejections, and
  preserves causes through timeout and interpretation-end reporting.
- `backend/ai/tools/task.py` — failure classification, log-safe label hashing,
  and `_trace`/`_fail` lifecycle records add `AI_TASK_TRACE` stages including
  receipt, validation, provider resolution, interpretation, destination
  resolution, definition validation, repository creation, success, and
  terminal failure while keeping user-facing messages unchanged.
- `backend/ai/engine/dispatcher.py` — the fast-path `stage=tool_result` log
  includes the static tool message instead of only tool and success.
- `backend/ai/database/task_repository.py` — Supabase create fallback logs
  `create_task_fallback_start`/`create_task_fallback_result` and annotates the
  in-memory record with `fallback_backend`.
- `tests/test_task_nl_creation.py` — ambiguity-failure expectations and
  `AI_TASK_TRACE` lifecycle coverage were updated, including success,
  terminal-failure, fallback-backend, request-id, and provider-failure cases.

The recovered report recorded 34 focused tests passing, `compileall -q
backend tests` passing, and `git diff --check` passing for that earlier phase;
those results are historical evidence, not new validation claimed by this
implementation task. Live Telegram verification remains unproven.

---

### Recovered current-state documentation — `create_task` interpretation fix + `AI_TASK_TRACE` observability (commits `2ab0db8`, `6566ea7`, in origin/main history)

The documentation of the already-delivered `create_task` observability work,
which was lost when its report rewrite was dropped during a failed rebase
(`48ee64d`, not in remote history), is restored here because the remote
current-state report did not cover it. The descriptions below were verified
against the code at the current HEAD (incl. the later `create_task` commits
`50a16da`, `8274b4a`, `df23029`, `49f9422`) before inclusion.

**Proven root cause of the original no-diagnostic `create_task` failures:**
`backend/ai/task_interpreter.py` → `TaskInterpreter.interpret()`. Providers
frequently wrap the compliant JSON candidate in markdown fences (` ``` `/` ```json `);
the original code called `json.loads(raw)` directly, the `JSONDecodeError`
collapsed into `TaskInterpretationError`, and the tool boundary's anonymous
`except (…, Exception)` returned a generic message while logging nothing —
production showed `tool=create_task success=False` with zero diagnostics.
Failure layer: AI interpretation parsing (not registry/executor/dispatcher/
repository). Evidence: the source investigation at commit `c40af4a` proved
the interpretation wrapper (branch 5 of seven `ToolResult(success=False)`
branches in `CreateTaskTool.execute`) was the active one by timing (~5.8 s),
single provider attempt, and absence of persistence log lines.

**Exact changes (all verified present at current HEAD):**

- `backend/ai/task_interpreter.py` — `_load_candidate_json(raw)`: retries
  extraction against a markdown-fenced block (`_JSON_BLOCK_RE`) when direct
  `json.loads` fails; candidate validation remains with
  `parse_candidate_output`. `interpret(..., request_id=...)` threads the
  correlation id into `AI_TASK_TRACE stage=interpretation_start`, and no
  longer collapses concrete provider failures into a generic message:
  `response.success is False` now logs `stage=provider_result success=false`
  (provider, attempted-provider list, providers-tried count, category
  `all_providers_failed` or the concrete `failure_type`/`error_type`,
  200-char sanitized detail) and raises a structured error that survives
  into the tool trace; provider success logs provider/model/fallback/latency;
  candidate rejections log `TASK_INTERPRET_REJECTED` with detail; timeout is
  converted with cause preserved; `stage=interpretation_end` reports
  schedule type, action count, provider, latency.
- `backend/ai/tools/task.py` — `_classify_interpretation_failure` maps
  failures to bounded sanitized categories (`timeout`,
  `candidate_invalid_json`, `candidate_invalid`, structured
  `provider=… category=… detail=…` propagation, `unexpected_exception`);
  `_label_hash` gives a SHA-256 12-hex log-safe label fingerprint; `_trace`/
  `_fail` emit one `AI_TASK_TRACE` record per stage and one terminal
  `create_task_failed` record (`failed_stage`, category, exception class,
  sanitized detail, `elapsed_ms`, `persisted=false`) while keeping every
  user-facing message unchanged. Stage coverage: `create_task_received`,
  `create_task_validation_start/end` (deterministic vs `nl_interpretation`),
  `create_task_provider_resolution`, `create_task_interpretation_start`,
  `interpretation_failed`, `create_task_destination_resolution`
  (chat-name/current-chat), `create_task_definition_validation_start/end`,
  `create_task_repository_create_start/result`, and terminal
  `create_task_success` (task id, version, `fallback_used`, final backend,
  elapsed ms).
- `backend/ai/engine/dispatcher.py` (`2ab0db8`) — the fast-path
  `stage=tool_result` log now includes the static tool message (first 160
  chars) instead of only `tool` and `success`.
- `backend/ai/database/task_repository.py` (`6566ea7`) — the Supabase
  create fallback no longer silently returns the in-memory result: it logs
  `create_task_fallback_start`/`create_task_fallback_result` (reason, task
  id, version) and annotates the record with `fallback_backend`
  (observability attribute, not a schema column).
- `tests/test_task_nl_creation.py` — updated ambiguity-failure expectations
  for the new trace stages plus an `AI_TASK_TRACE` lifecycle test class
  (success-path stage sequence, terminal-failure records, fallback-backend
  annotation, request-id correlation, structured provider-failure
  propagation); 20 test functions.

**Validation recorded for that phase** (executed at HEAD `6566ea7`, before
the later remote `create_task` commits): focused suites
`test_task_nl_creation.py`, `test_task_contract.py`, `test_task_repository.py`
→ 34 passed; `compileall -q backend tests` passed; `git diff --check`
passed. Live Telegram verification of the trace chain remained NOT PROVEN
then and is still outstanding.

## 5. FILES CHANGED

| File | Category | Purpose |
|---|---|---|
| `backend/ai/actions.py` | runtime | Remove deterministic task-show routing and validate/resolve `task_list.status` |
| `backend/ai/prompt/template.py` | prompt | Teach semantic task-tool selection and status-filter fallback output |
| `backend/ai/task_management.py` | service | Add owner-scoped status filtering |
| `backend/ai/task_management_interface.py` | presentation | Render filtered task lists |
| `backend/ai/tools/executor.py` | runtime | Add task-tool execution labels |
| `backend/ai/tools/task_management_tools.py` | tool | Expose and validate optional task-list status |
| `tests/test_capability_exposure_tools.py` | test | Verify executor behavior and provider schema optionality |
| `tests/test_new_tool_action_path.py` | test | Verify JSON action status semantics |
| `tests/test_task_show_intent.py` | test | Verify AI routing replaces deterministic task vocabulary |
| `IMPLEMENTATION_REPORT.md` | documentation | Record this current implementation state |

The working tree also contains an unrelated untracked `telegram-self-bot/`
directory. It was not inspected for implementation changes, modified, staged,
or included in this task.

---

## 6. BEHAVIOR AND ARCHITECTURE IMPACT

### Task routing

Before:

```text
recognized task phrase → local vocabulary parser → task tool
```

After:

```text
natural-language task request → provider-visible registered schemas
→ native task call or JSON action → local validation
→ ToolExecutor → TaskManagementService
```

`parse_command_intent("لیست تسک رو ببین", has_reply=False)` now returns
`conversational` rather than selecting `task_list` locally. This is deliberate:
task phrasing is no longer hardcoded into the deterministic parser.

### Retrieval semantics

`task_list` remains `READ_ONLY` and owner-scoped. It now accepts one optional
status filter from the closed set `active`, `paused`, or `completed`. An
unfiltered call retains the previous list behavior.

### Security boundaries

- The provider can select only capabilities exposed by `ToolRegistry`.
- JSON actions are checked by `validate_action` and resolved through the same
  executor path as native calls.
- `task_transition` still requires the current `expected_version` and remains
  owner-scoped with CAS semantics.
- The model cannot choose a Telegram destination or bypass the service layer.
- No database schema or migration was added.

---

## 7. VALIDATION

| Validation | Command | Result |
|---|---|---|
| Full test suite | `python3 -m pytest tests/ -q` | **1510 passed, 23 skipped, 1 warning** |
| Focused routing/schema suites | `python3 -m pytest tests/test_task_show_intent.py tests/test_new_tool_action_path.py tests/test_capability_exposure_tools.py tests/test_19_ai_actions.py tests/test_25_fast_path.py -q` | **129 passed** |
| Native dispatcher/executor coverage | `python3 -m pytest tests/test_10_tool_calls.py tests/test_19_ai_actions.py tests/test_capability_exposure_tools.py -q` | **76 passed** |
| Compile check | `python3 -m py_compile backend/ai/actions.py backend/ai/task_management.py backend/ai/task_management_interface.py backend/ai/tools/task_management_tools.py backend/ai/tools/executor.py backend/ai/prompt/template.py tests/test_task_show_intent.py tests/test_new_tool_action_path.py tests/test_capability_exposure_tools.py` | **passed** |
| Diff whitespace check | `git diff --check` | **passed** |

The tests verify both concrete JSON-action resolution and the existing native
provider-call path's use of `ToolExecutor`. They do not substitute for a live
provider or Telegram run.

---

## 8. LIVE TELEGRAM VERIFICATION

**Not performed.** No deployed Self Bot, live Telegram session, or production
provider credentials were used by this agent.

Live verification must confirm that a request such as `Nova لیست تسک رو ببین`
causes the configured provider to select the registered `task_list` capability,
that the call reaches `ToolExecutor`, and that the returned owner-scoped task
list is delivered. A request for completed tasks must additionally produce the
validated argument `{"status":"completed"}`.

Because that test was not run here, this report does not claim that the live
provider will always select a tool or that the production incident is fully
resolved under provider failure conditions.

---

## 9. DATABASE / SUPABASE IMPACT

- Database schema changed: **NO**.
- Migrations changed: **NO**.
- Repository interfaces changed: **NO**.
- Live Supabase verification: **NO**.
- The status filter operates on the existing owner-scoped task records after
  retrieval and introduces no new table or persistence path.

---

## 10. DELIVERY STATE

- The implementation changes in this report are delivered on `main`; the
  exact commit and remote SHA are verified in the final delivery record.
- No force-push was used, and the newer remote report commit was preserved.
- The unrelated untracked `telegram-self-bot/` directory was preserved.

---

## 11. LIMITATIONS

1. AI-driven routing intentionally restores provider dependence for task
   requests; it does not guarantee execution when every provider is down or a
   model ignores the supplied schemas.
2. The status filter is a single closed-enum read filter. Pagination,
   multi-status filtering, and new lifecycle states were not added.
3. Live Telegram/provider verification was not available in this workspace.
4. The implementation was uncommitted at the start of delivery; final commit and remote verification are recorded in the delivery response.
