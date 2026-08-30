# Implementation Report — Stage 15

## Stage
- **Completed stage:** Stage 15 — Durable Scheduled Execution and Retry Re-execution
- **Previous stage:** Stage 14 — Telegram Task Management Interaction Boundary
- **Next stage:** Stage 16 — Runtime Persisted-Outcome Notification Transport

## Objective and scope
Stage 15 closes the durable runtime bridge from scheduler-coordinated occurrences to the existing execution coordinator and consumes due retry-pending occurrences through the same scheduler loop. It preserves the existing repository state machine, retry policy, ToolExecutor boundary, owner isolation, and RuntimeSupervisor lifecycle authority.

## Exact files changed
- `backend/ai/task_scheduler.py` — accepts an injected execution coordinator, claims eligible occurrences, hands them to execution, and polls due retry-pending occurrences.
- `backend/ai/database/task_repository.py` — adds owner-scoped due-retry occurrence discovery for in-memory and Supabase-backed repositories.
- `backend/runtime/supervisor.py` — constructs the task execution coordinator from the existing TelegramAPI/tool registry and injects it into the single task scheduler.
- `tests/test_stage15.py` — focused scheduled execution, retry pickup, ownership, duplicate wake, and boundary tests.
- `IMPLEMENTATION_REPORT.md` — current-state report.

Pre-existing workspace changes in `INVESTIGATION.md` and `tests/test_stage13.py` were preserved and were not included.

## Implementation details
`TaskScheduler` remains the only polling loop. For a newly due task, it creates the deterministic occurrence, claims it through `TaskRepository`, and only then calls `TaskExecutionCoordinator.execute` when a coordinator is configured. The scheduler itself never resolves or executes action JSON. Existing standalone scheduler tests remain compatible when no coordinator is injected.

The repository now exposes bounded, owner-scoped `list_due_retry_occurrences`. Due retry-pending occurrences are claimed through the same CAS/state-machine claim operation and passed to the coordinator only after the returned record is `running`. Not-yet-due retries are ignored, and duplicate wakes cannot reclaim already-running/succeeded occurrences.

`RuntimeSupervisor` creates the coordinator using the existing `TelegramAPI`, `ToolContext`, default `ToolRegistry`, and `ToolExecutor`, then injects it into the one scheduler instance. No second loop, worker, timer, or execution authority was added.

## Ownership and security
All due-task and retry queries are owner-scoped. Claims require the authoritative owner ID and repository state. Execution remains inside `TaskExecutionCoordinator` and `ToolExecutor`; persisted action data is never executed by the scheduler. No arbitrary Telegram RPC, SQL, RPC, shell, provider, or code execution path was introduced. Cancellation is re-raised by scheduler loops and scheduler shutdown cancels its supervised task.

## Database/schema status
**Database/schema changes: NONE.** The existing `ai_tasks` and `ai_task_occurrences` tables are unchanged. No migration was created or modified, and no Supabase SQL was executed. Live Supabase and Telegram execution were not verified.

## Tests and validation actually executed
- `python3 -m pytest tests/test_stage15.py tests/test_task_scheduler.py tests/test_task_execution.py tests/test_retry.py -q` — **16 passed**.
- `python3 -m pytest tests/ -q --no-header` — **1136 passed, 23 skipped, 1 warning**.
- `python3 -m compileall -q backend tests` — passed.
- `git diff --check` — passed.

## Architecture preserved
RuntimeSupervisor remains the sole runtime lifecycle authority. TaskScheduler remains durable coordination and handoff only. TaskExecutionCoordinator remains the occurrence execution boundary. ToolRegistry/ToolExecutor remain registered action execution authorities. TaskRepository remains persistence/state authority. Retry classification/backoff remains in `backend/ai/retry.py`; Stage 15 only consumes persisted retry state. Profile scheduling is untouched.

## Limitations and remaining work
Live Supabase, Telegram, provider, and process-level end-to-end execution were not verified. Notification transport and lifecycle notification wiring remain outside this stage, as do Glass task panels and dashboard task APIs. Retry handling still depends on the existing coordinator failure transitions and does not claim exactly-once side effects; at-least-once semantics remain authoritative.

## Delivery
- **Implementation commit:** pending.
- **Push:** pending.
- **Remote HEAD:** pending.
- **Final working tree:** pre-existing `INVESTIGATION.md` and `tests/test_stage13.py` changes must remain preserved; Stage 15 files will be isolated for delivery.
