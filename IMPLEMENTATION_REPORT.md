# Implementation Report — Stage 16

## Stage
- **Completed stage:** Stage 16 — Runtime Persisted-Outcome Notification Transport
- **Previous stage:** Stage 15 — Durable Scheduled Execution and Retry Re-execution
- **Next stage:** Not yet established. Remaining source-justified gaps are Glass/UI task panels and dashboard task APIs; the next agent must derive the next stage from the repository and this report.

## Objective and scope
Wire persisted task outcomes (succeeded / failed / retry_pending / cancelled) into the existing notification boundary at runtime. The scheduler notifies only after `TaskOutcomeNotifier` re-verifies the occurrence's actual persisted status in the repository. No second scheduler, notification loop, or worker was created; notification delivery stays outside the scheduler's execution authority.

## Exact files changed
- `backend/ai/task_scheduler.py` — added `outcome_notifier` constructor parameter and `_notify_outcome()` hook, invoked in `_execute_claimed()` after the coordinator returns. Cancellation re-raised; other notifier failures logged and isolated.
- `backend/runtime/supervisor.py` — constructs `TaskNotificationService` + `TaskOutcomeNotifier` and injects the notifier into `TaskScheduler`. The Telegram sender resolves `self.client` at call time so a rebuilt client never leaves stale state.
- `tests/test_stage16.py` — 13 focused tests for the runtime notification wiring.
- `IMPLEMENTATION_REPORT.md` — this report.

Pre-existing workspace changes in `INVESTIGATION.md` and `tests/test_stage13.py` were preserved and remain uncommitted.

## Implementation details
- **Persistence-before-notify**: `TaskOutcomeNotifier.notify_persisted()` re-reads the occurrence from `TaskRepository` and only sends when the stored `status` equals the requested outcome kind. If the coordinator's reported status was not actually persisted (e.g. `transition_occurrence` failed), no notification is sent.
- **Outcome kinds**: `succeeded`, `failed`, `retry_pending`, `cancelled` — enforced by both `TaskNotificationService.send()` (allow-list) and `TaskOutcomeNotifier._message()` formatting.
- **Bounded delivery**: 10-second `asyncio.wait_for` timeout and 1024-character message truncation, both pre-existing in `TaskNotificationService`.
- **Failure isolation**: sender exceptions are caught inside `TaskNotificationService` (returns `False`); scheduler-level notifier failures are logged via `logger.exception` and never mutate task/occurrence state. `asyncio.CancelledError` is re-raised at every boundary.
- **No duplicate notifications**: notifications fire only in `_execute_claimed()` after an actual execution attempt; scheduler polling (`list_due_tasks`, `list_due_retry_occurrences`) never notifies. `once` tasks get `next_run_at=NULL` after `advance_next_run`, so `list_due_tasks` (filters `status=active AND next_run_at<=now`) will not rediscover them. Due `retry_pending` occurrences are reclaimed and re-executed — the retry_pending notification happens once, at the transition, and re-execution notifies the new terminal outcome.
- **Lazy client resolution**: `_notify_sender` in the supervisor closes over `self` and reads `self.client` at call time; after a reconnect or hard-reset rebuild, notifications use the new client with no stale references.
- **Ownership**: `owner_id` flows only from `RuntimeSupervisor` → notifier/service/sender. `TaskNotificationService.send()` rejects notifications whose `owner_id` differs from the service's authoritative owner; the sender double-checks `owner == self.owner_id` and that the client exists.

## Notification lifecycle semantics
1. Scheduler claims an occurrence (`claimed`/`retry_pending` → `running`) via repository CAS claim.
2. `TaskExecutionCoordinator.execute()` runs registered tools via `ToolExecutor` and persists `succeeded`/`failed`/`retry_pending` through `transition_occurrence`.
3. Scheduler calls `_notify_outcome(task_id, occurrence_key, result.status)`.
4. `TaskOutcomeNotifier` re-reads the occurrence; if the persisted status matches, it formats a bounded message and sends via `TaskNotificationService`.
5. The Telegram sender resolves the current self-client and sends to the owner's chat.

## Ownership and security
- Owner identity originates exclusively from `RuntimeSupervisor.owner_id` (runtime context); notification payloads and persisted data cannot override it.
- Notification code cannot execute task actions (it only reads occurrence state), cannot mutate task/occurrence state, and performs no Telegram RPC beyond the established `TelegramAPI.send_message` facade.
- No arbitrary Telegram RPC, SQL, RPC, shell, provider, or executable-code path was introduced.

## Database/schema status
**Database/schema changes: NONE.** The existing `ai_tasks` and `ai_task_occurrences` tables are unchanged. No migration was created or modified, and no Supabase SQL was executed. Live Supabase and Telegram delivery were not verified.

## Tests and validation actually executed
- `python3 -m pytest tests/test_stage16.py -q --no-header` — **13 passed**.
- `python3 -m pytest tests/ -q --no-header` — **1149 passed, 23 skipped, 1 warning**.
- `python3 -m compileall -q backend tests` — passed.
- `git diff --check` — passed.

Test coverage includes: persisted succeeded/failed/retry_pending/cancelled each notify exactly once; outcome not persisted → no notification; persisted status differs from reported status → no notification (lying-coordinator case); sender failure leaves occurrence state untouched; cancellation propagates through the scheduler; owner mismatch rejected; duplicate scheduler wake does not duplicate delivery; retry_pending not notified on rediscovery, only on re-execution with its new outcome; notification never mutates occurrence state; source-level guard that the supervisor wires the lazy-client sender.

## Architecture preserved
- RuntimeSupervisor: sole runtime lifecycle authority (also the only place notification dependencies are constructed).
- TaskScheduler: durable coordination + execution handoff; notification is a post-execution hook, not an execution authority.
- TaskExecutionCoordinator / ToolRegistry / ToolExecutor: unchanged execution boundaries.
- TaskRepository: unchanged persistence/state-machine authority.
- TaskNotificationService / TaskOutcomeNotifier: notification boundaries, now actually wired at runtime.
- TelegramAPI: the only Telegram transport used (send_message facade).
- profile.scheduler: untouched and separate.

## Limitations and remaining work
- Live Telegram delivery, live Supabase persistence, and end-to-end process behavior were not verified (no live environment in this workspace).
- `notification_destination` is currently owner-only in practice; richer destinations would need schema or config work (out of scope).
- Remaining source-justified gaps: Glass/UI task panels, dashboard task APIs, and richer task-management UX. These belong to a future stage to be derived by the next agent from the repository state.

## Delivery
- **Implementation commit:** `5a1e47a` (feat: wire persisted task outcome notifications into scheduler runtime).
