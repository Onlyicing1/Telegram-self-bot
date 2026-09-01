# INVESTIGATION

## CURRENT OBSERVATION

Taskloom can create and execute a recurring task. The displayed task shows `send_message`, `interval`, `Current chat`, `Asia/Tehran`, and a successful occurrence. The remaining reports are: no task row visible in the user's database, Delete does not remove the task from the list, and displayed timestamps appear UTC rather than Tehran local time.

The repository was inspected without live Supabase access or SQL execution. Database state therefore cannot be independently confirmed here.

## 1. TASK CREATION AND DURABLE PERSISTENCE

The creation boundary is:

`backend/bot/handlers/ai_unified.py::_execute_ai()` → `engine.execute(request)` → `backend/ai/engine/dispatcher.py::dispatch()` deterministic action path → `backend/ai/tools/executor.py::ToolExecutor.execute_calls()` → `backend/ai/tools/task.py::CreateTaskTool.execute()` → `backend/ai/task_creation.py::TaskCreationService.create()` → `backend/ai/database/task_repository.py::TaskRepository` implementation.

`CreateTaskTool.execute()` obtains the repository through `get_repository_manager().task`, constructs `TaskCreationService`, and awaits `service.create(candidate, datetime.now(timezone.utc))`. `TaskCreationService.create()` validates the candidate, parses the schedule, computes `next_run_at`, converts that value to UTC, and calls `repository.create_task(owner_id, payload)`.

`SupabaseTaskRepository.create_task()` validates the payload and executes `client.table("ai_tasks").insert(payload).execute()`. It converts the returned row to `TaskRecord`. On non-validation exceptions it logs a warning and invokes its configured in-memory fallback repository. `InMemoryTaskRepository.create_task()` validates and stores a `TaskRecord` in process memory only.

The first occurrence is not inserted by task creation. `TaskScheduler.run_once()` later queries due tasks, calls `repository.create_occurrence(...)`, and the Supabase implementation inserts into `ai_task_occurrences`; the in-memory implementation stores it only in memory. The scheduler is created once by `RuntimeSupervisor._start_task_scheduler()` and started after client setup.

**Status:** The source proves a durable insert is attempted when the active repository is `SupabaseTaskRepository` and its client insert succeeds. It also proves that a Supabase exception silently degrades to the in-memory fallback. The reported absence of a database row is **NOT CONFIRMED from this workspace** because live database state and production logs for the insert are unavailable. A successful UI/task execution can be backed entirely by the fallback repository if Supabase access failed, so UI success does not prove durable persistence.

## 2. TASK LIST SOURCE

`backend/bot/handlers/taskloom.py::_taskloom_panel()` → `_service(owner_id)` → `TaskManagementService.list_tasks()` → `repository.list_tasks(owner_id)`.

`TaskManagementService` is a thin owner-scoped wrapper. `SupabaseTaskRepository.list_tasks()` queries `ai_tasks` filtered by `owner_id`, ordered by `updated_at` descending. On any exception it logs and returns `self._fallback.list_tasks(owner_id)`. The in-memory implementation returns records held in its `_tasks` dictionary.

The panel has no separate task-list cache. Each panel render calls the service/repository again. However, the repository object is a process-level singleton returned by `get_task_repository()`, and its fallback is process-local. A task can therefore remain visible after a database failure because list reads fall back to the same in-memory repository that accepted the task.

A refresh callback re-enters `_task_detail_panel()` or the list panel and consequently queries the repository again; it does not force a database-only read.

## 3. DELETE FLOW

The detail panel constructs `action:taskloom_delete:<task_id>:<version>` in `backend/bot/handlers/taskloom.py::_task_detail_panel()`.

The callback chain is:

`backend/helper/panels.py::_callback_router()` → `_handle_action()` → registered `taskloom_delete` handler → `taskloom._delete_action()` → `_mutate(event, extra, "delete")` → `TaskManagementService.delete()` → `TaskManagementService.set_status()` → `repository.transition_task(owner_id, task_id, "deleted", expected_version)`.

Deletion is a **soft delete/status transition**, not a hard row delete. `InMemoryTaskRepository.transition_task()` validates the transition and sets `status="deleted"`, increments `version`, and retains the record. `SupabaseTaskRepository.transition_task()` calls `update_task()`, which performs an `ai_tasks.update(...)` filtered by `id`, `owner_id`, and `version`, setting status and version; it does not delete the row.

After mutation, `_mutate()` calls `_task_detail_panel()` again and returns the refreshed detail view. The Delete action therefore does not itself render the Taskloom list. Returning from detail to the list invokes `_taskloom_panel()`, whose `list_tasks()` implementations currently return all owner records, including `deleted` records; neither `SupabaseTaskRepository.list_tasks()` nor `InMemoryTaskRepository.list_tasks()` filters terminal/deleted statuses.

**Confirmed source-level failure:** even if the delete transition succeeds, the list query intentionally/currently includes records with `status="deleted"`, so the deleted task remains in the visible list. This is independent of whether the database update succeeded.

**Additional unresolved possibility:** if the Supabase update fails, `SupabaseTaskRepository.update_task()` falls back to its in-memory repository. This can create a split-brain presentation in which the transition is recorded only in fallback memory. The exact production outcome is not provable without logs/database state.

## 4. TIMEZONE FLOW

Task creation receives the configured timezone through `ToolContext.tz_str`; `CreateTaskTool` passes it to the interpreter or deterministic candidate path. `TaskCreationService.create()` validates the candidate timezone and schedule timezone, computes the next run against the timezone-aware UTC reference, and serializes `next_run_at` as UTC.

`TaskRepository._now()` uses UTC. Supabase row parsing in `_parse_dt()` parses ISO timestamps and attaches UTC to naive values. `TaskRecord.next_run_at`, `created_at`, `updated_at`, and occurrence `scheduled_for` are therefore timezone-aware UTC instants after repository conversion. This is correct storage/normalization behavior and does not itself mean the value should be displayed as UTC.

The current Taskloom UI obtains timestamps in `backend/bot/handlers/taskloom.py::_fmt_dt(value)`, which is simply:

`return value.strftime("%m-%d %H:%M")`

There is no `ZoneInfo(task.timezone)`, `astimezone(...)`, or other display conversion in `_fmt_dt()`. Consequently, an aware datetime parsed/stored as UTC is formatted using its existing UTC clock fields. The task's `timezone` metadata is displayed separately but is not used by `_fmt_dt()`.

The affected fields are `task.next_run_at`, `task.updated_at`, `task.created_at` where shown, and `occurrence.scheduled_for`. The source-grounded failure is therefore in the **UI formatter**, not storage: the formatter discards timezone context by formatting the datetime without converting to the task's declared timezone. The smallest future implementation location is `taskloom.py::_fmt_dt()` or a narrowly scoped caller that passes the task timezone; no fixed offset should be introduced.

## 5. RELATIONSHIP BETWEEN BUGS

Persistence and list/delete can be related through the repository fallback: a Supabase failure can leave a task and later status changes in process memory, making the UI appear functional while durable state is absent. This relationship is **LIKELY**, not proven for the reported production instance.

The Delete visibility problem is independently **CONFIRMED FROM SOURCE** because list methods include deleted records even after a successful soft-delete transition.

Timezone display is independently **CONFIRMED FROM SOURCE** as a formatter conversion defect. It does not depend on database persistence, deletion, the scheduler, or helper connectivity.

## 6. HELPER BOT DEPENDENCY

The Helper Bot is used by the Glass UI inline delivery and callback registration path, as established in the prior investigation. Once the Taskloom detail view is visible and actions such as Pause/Resume execute, the current observations demonstrate that the relevant UI path is reachable in that runtime state.

Task persistence, repository transitions, scheduler execution, and timestamp formatting do not import or call the Helper Bot. The source provides no causal dependency from `helper_connected=False` to the three reported backend/list/timezone defects. Helper connectivity may prevent creation or callback delivery of a panel in the normal inline path, but it does not explain a deleted row remaining in a repository list or UTC formatting of an already-rendered task.

## 7. RECENT task.py REPAIR

The repaired `backend/ai/tools/task.py` is involved in task creation entry and can prevent all task-tool execution if it is syntactically invalid. The current successful task creation/execution observation shows no demonstrated remaining syntax-wiring failure in that path. Its code delegates persistence to `TaskCreationService` and does not implement list rendering, delete transitions, or timestamp formatting.

No remaining impact on the three reported problems is demonstrated from source. The database fallback behavior is in `task_repository.py`, deletion visibility is in `list_tasks()`/Taskloom rendering, and timezone display is in `_fmt_dt()`.

## 8. CONFIRMED ROOT CAUSES

1. **Delete visibility root cause — CONFIRMED FROM SOURCE:** deletion is implemented as a `status="deleted"` transition, while both list implementations return all owner tasks without excluding deleted/terminal records. A successfully deleted task can therefore remain in the list.
2. **Timezone display root cause — CONFIRMED FROM SOURCE:** `taskloom._fmt_dt()` formats UTC-normalized aware datetimes directly and never converts them to `task.timezone` (`Asia/Tehran`).
3. **Persistence failure — NOT CONFIRMED as a live incident:** the repository has a confirmed fallback path that can make UI/task execution succeed without a durable Supabase row when Supabase operations fail, but no live evidence proves that this occurred for the reported task.

## 9. REMAINING UNKNOWN

- Whether the production `ai_tasks` insert succeeded, failed, or was routed to the in-memory fallback.
- Whether the production process was configured with a real Supabase client and valid service-role credentials at task creation time.
- Whether the displayed task came from the same process/repository instance as the database queried by the user.
- Whether the Delete update succeeded in Supabase or only in fallback memory.
- The exact production datetime values and timezone offsets behind the displayed screenshots.
- Whether the deployed artifact exactly matches this repository HEAD.

## 10. MINIMAL NEXT IMPLEMENTATION SCOPE

- Persistence: add production-observable verification around the existing `SupabaseTaskRepository.create_task()` result/fallback boundary, without changing schema; first confirm the actual production repository/client configuration before changing behavior.
- Delete visibility: retain the existing soft-delete transition but exclude `deleted` (and, if product policy requires, other terminal statuses) from the existing `list_tasks()` read path or its narrowly scoped service boundary.
- Timezone: convert aware timestamps with `ZoneInfo(task.timezone)` at the existing Taskloom formatting boundary before `strftime`; preserve UTC storage and avoid fixed offsets.

No implementation was performed in this investigation.

