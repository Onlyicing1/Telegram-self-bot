# Current Implementation Report

## Repository and Delivery State

| Field | Value |
|---|---|
| Repository | `Onlyicing1/Telegram-self-bot` |
| Branch | `main` |
| Prior HEAD before this fix | `223f9e4bbfb580aba00812232d870f634d1124f3` |
| Implementation commit | `5db7e4dd888f108d11f33b0caf3f84f0f587a944` |
| Local HEAD | `5db7e4dd888f108d11f33b0caf3f84f0f587a944` |
| `origin/main` | `5db7e4dd888f108d11f33b0caf3f84f0f587a944` |
| Local HEAD == remote HEAD | YES |

## Current Implementation State

**IMPLEMENTED** - Natural-language scheduling requests now reach durable task
creation instead of being diverted into unrelated AI/tool activity or the
tool-round limit.

The delivered fix is:

```text
Telegram "هر 1 دقیقه یک بار برای من بنویس سلام"
  -> ai_unified -> AIRequest -> Dispatcher
  -> _try_local_fast_path (scheduling-intent detection, BEFORE provider/tool rounds)
  -> create_task tool -> TaskInterpreter (bounded, tools=[])      (interpret)
  -> TaskCandidate validation                                    (validate)
  -> TaskCreationService.create(authoritative owner_id)          (create)
  -> TaskRepository.create_task (ai_tasks, CAS version)          (persist)
  -> TaskScheduler (single scheduler, later executes the actions)
```

The provider tool loop now also exposes a registered `create_task` tool, so
requests that are not deterministically matched (for example an ambiguous or
at-a-time scheduled delete) are still reachable through the same boundary.

## Defect and Root Cause

A natural-language task request such as "هر 1 دقیقه یک بار برای من بنویس سلام"
produced unrelated AI/tool activity, hit the tool-round limit, and never
created a durable task. The database contained no created task. Root cause,
traced through the actual AI execution path:

1. The AI tool registry (`backend/ai/tools/registry.py::create_default_registry`)
   exposed delete/save/bio/username/… tools but **no task-creation tool**, so
   the provider path could never create a task.
2. The deterministic fast path (`backend/ai/actions.py::parse_command_intent`)
   classified "write/send" phrasing ("بنویس" -> `send`) as an `UNSUPPORTED`
   action and returned immediately, so a scheduling request was either
   abandoned ("Unsupported action: send") or, when it reached the provider,
   consumed tool rounds on unrelated tools before hitting `MAX_TOOL_ROUNDS = 3`.
3. Even the canonical task-creation boundary could not persist a **new interval
   task**: `TaskCreationService.create` computed the first run with
   `next_occurrence()`, which raises `ScheduleError` for `interval` schedules
   (interval requires a previous occurrence). Therefore an interval task could
   not be durably created at all.

No Freebuff-specific bug/fix material exists in the repository; the "Freebuff"
framing in the task is not source-backed. The fix follows the repository's own
documented architecture (`INVESTIGATION.md`): the canonical boundary is
`TaskInterpreter -> TaskCreationService -> TaskRepository`, and the AI path
must reach that boundary rather than a parallel executor.

## Exact Implementation

- `backend/ai/actions.py`
  - Added `create_task` to `ACTION_NAMES` and `EXECUTABLE_ACTION_NAMES`, and
    `request` to `ALLOWED_FIELDS`.
  - Added `schedule_text` to `ActionParseResult`.
  - Added `create_task` handling to `validate_action` (requires a non-empty,
    bounded `request`) and `resolve_tool_calls` (maps to the `create_task` tool).
  - Added a conservative `_is_scheduling_intent()` detection (Persian/English):
    a strong recurring cadence, or an interval marker ("هر"/"every"/… + time
    unit), or an explicit plan/reminder verb combined with an action verb. It
    is deliberately narrow: a historical delete ("…ساعت ۹ دیروز…") is NOT
    misclassified as a scheduled task.
  - `parse_command_intent` now routes a scheduling intent to an executable
    `create_task` action BEFORE the send/delete/save vocabulary can divert it.
- `backend/ai/task_creation.py`
  - Fixed first-run scheduling for a brand-new `interval` task: `TaskCreationService`
    computes the first run as one interval after the reference time via
    `advance_interval` (the existing `next_occurrence` cannot anchor an
    interval without a previous occurrence).
- `backend/ai/task_interpreter.py`
  - `interpret()` now accepts an optional `timezone` hint (backward
    compatible) so the candidate carries the owner's IANA timezone.
- `backend/ai/tools/task.py` (new)
  - `CreateTaskTool`: a `READ_WRITE` tool that runs every creation through
    `TaskInterpreter -> TaskCreationService.create(authoritative owner_id) ->
    TaskRepository.create_task`. It fabricates nothing: an ambiguous request
    (interpreter returns JSON null) yields a non-destructive failure ToolResult.
- `backend/ai/tools/registry.py`
  - Registered `CreateTaskTool` in `create_default_registry`, so it appears in
    the provider's native tool definitions and prompt tool block.
- `backend/ai/tools/executor.py`
  - Added the `create_task` status label.
- `tests/test_task_nl_creation.py` (new)
  - Regression tests: interval routing -> `create_task`; scheduled destructive
    request -> `create_task`; bare send unchanged; historical delete not
    misclassified; `create_task` persisted interval task under owner; ambiguity
    creates nothing; owner isolation; destructive scheduled task persisted with
    bounded owner-scoped action; and the Dispatcher fast path creating the task
    with only the interpreter's single bounded provider call (no tool-round
    exhaustion).

## Files Changed

| File | Change |
|---|---|
| `backend/ai/actions.py` | `create_task` action + scheduling-intent detection |
| `backend/ai/task_creation.py` | first-run scheduling for new interval tasks |
| `backend/ai/task_interpreter.py` | optional timezone hint |
| `backend/ai/tools/task.py` | new `CreateTaskTool` |
| `backend/ai/tools/registry.py` | register `CreateTaskTool` |
| `backend/ai/tools/executor.py` | `create_task` status label |
| `tests/test_task_nl_creation.py` | new regression tests |

`IMPLEMENTATION_REPORT.md` is changed separately by the documentation delivery
commit.

## Architecture Preserved

**PRESERVED**

- `TaskScheduler` remains the single scheduler and polling authority.
- `TaskExecutionCoordinator` remains the execution/failure authority.
- `TaskRepository` remains the durable persistence, owner-filtering,
  state-transition, and CAS authority.
- `ToolRegistry`/`ToolExecutor` remain the registered action execution
  boundary (the `create_task` tool delegates to the service layer).
- `Self Bot` remains the execution authority; the AI only returns candidate
  actions, never direct Telegram control.
- No arbitrary Telegram RPC/SQL/shell, no persisted-code execution, no provider
  bypass, no second scheduler/worker/retry engine was introduced.
- Destructive scheduled actions remain owner-scoped and execute later through
  the registered `ToolExecutor`.

## Security and Ownership

- Owner identity comes only from trusted runtime context (`ToolContext.owner_id`
  populated from `AIRequest.owner_id`), never from model output.
- `TaskInterpreter` returns JSON null on ambiguity; `CreateTaskTool` creates
  nothing in that case.
- `TaskCreationService` requires a valid owner id and validates the candidate;
  `TaskRepository.create_task` is owner-scoped and CAS-versioned.
- The new tool is `READ_WRITE`: in this single-owner self-bot the owner's
  message is the authorization, matching existing deterministic tools.

## Retry / Scheduler Relationship

- This fix only creates durable tasks. The existing retry policy
  (`MAX_ATTEMPTS = 3`, `can_retry`, `retry_delay`), interrupted-occurrence
  recovery, and scheduler pickup are unchanged and remain as previously
  delivered (`1e1b121`).

## Database and Supabase

- Database/schema impact: **UNCHANGED**. No migration, SQL, columns, or tables
  were added or changed.
- The existing `ai_tasks` table's `schedule`, `actions`,
  `notification_destination`, `next_run_at`, `status`, `version`,
  `created_at`/`updated_at`/`terminal_at` fields were sufficient.
- Supabase production data was not modified. Supabase live connectivity and RLS
  behavior were not verified in this environment.

## Validation Actually Executed

- `python3 -m compileall -q backend tests` - passed.
- `git diff --check` and `git diff --cached --check` - passed.
- Focused new regression tests:
  `python3 -m pytest tests/test_task_nl_creation.py` - 11 passed.
- Focused existing + new:
  `tests/test_task_nl_creation.py tests/test_19_ai_actions.py tests/test_25_fast_path.py` - 54 passed.
- Broader focused suite (tool wiring, runtime, actions, fast path, delete
  regression, task execution/management/scheduler, stage 15/16/17):
  `240 passed`.
- Full suite: `python3 -m pytest tests/ -q --no-header` - **1172 passed,
  23 skipped, 1 warning** (baseline was 1161; +11 new tests).

## Live Verification and Limitations

- Live Telegram end-to-end (real scheduling request creating a task and the
  scheduler executing it against a live account): **NOT VERIFIED**.
- Live Supabase persistence of a created task: **NOT VERIFIED**.
- The task-execution side of a recurring "write hello" action requires a
  registered message-sending tool; the tool registry currently has no generic
  `send` tool. Task CREATION and durable persistence are delivered here;
  executing an unregistered action later would fail at execution time. This is
  a pre-existing task-action-set limitation, not introduced by this change.
- The scheduling detector is intentionally conservative; complex or ambiguous
  at-time requests depend on the provider recognizing the newly registered
  `create_task` tool.
- This environment lacks `tzdata` for non-UTC IANA zones, so tests use `UTC`;
  non-UTC timezone behavior is not exercised in this sandbox.

## Intentionally Untouched Files

- `tests/test_stage13.py` is an unrelated pre-existing working-tree
  modification. It was not edited, staged, committed, or pushed by this work.
- `INVESTIGATION.md`, migrations/SQL, Telegram handlers, runtime supervisor,
  scheduler, retry, execution coordinator, dashboard, and unrelated services
  were untouched.

## Git Delivery Record

- Implementation commit: `5db7e4dd888f108d11f33b0caf3f84f0f587a944`
  (`fix: route natural-language scheduling requests to durable task creation`).
- This report/documentation commit is delivered separately.
- Push result: SUCCESS (verified by `git fetch origin main`, `git rev-parse
  HEAD`, `git rev-parse origin/main`, and `git ls-remote`).
- Final local HEAD and `origin/main`: both `5db7e4dd888f108d11f33b0caf3f84f0f587a944`.
- Local HEAD == remote HEAD: YES.
- Final working tree: only the pre-existing unrelated unstaged modification
  `M tests/test_stage13.py` remains.