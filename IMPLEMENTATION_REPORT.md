# Implementation Report — Durable AI Task/Scheduler Database Foundation

## Date
2026-08-29

## Repository / branch / base commit
- Repository: `https://github.com/Onlyicing1/Telegram-self-bot`
- Branch: `main`
- Base commit: `da74cc4cab6833f6427599b9d0e3b89c22328df3`

## Objective and phase
Implemented Phase 2 database foundation for the durable AI Task/Scheduler system. This phase adds exactly two durable concepts, their repository/fallback state contract, and focused tests. It does not implement scheduling, task parsing, task execution, notifications, or Telegram behavior.

## Exact files changed
- `supabase/migrations/20260829000001_create_ai_tasks.sql`
- `backend/ai/database/task_repository.py`
- `backend/ai/database/manager.py`
- `tests/test_task_repository.py`
- `IMPLEMENTATION_REPORT.md`

`INVESTIGATION.md` was intentionally left untouched.

## Migrations
Created one additive migration containing exactly two tables in dependency order:

1. `ai_tasks`: `bigserial` ID, numeric owner ID, bounded label, checked lifecycle and schedule types, version, JSONB schedule/actions/destination, UTC timestamp fields, terminal timestamp, structural JSON/action limits, due-task and owner indexes, RLS enabled, and no public write policy.
2. `ai_task_occurrences`: `bigserial` ID, task foreign key with `ON DELETE RESTRICT`, owner ID, unique task/occurrence identity, version and action snapshot, UTC scheduled time, attempt range 1–3, checked statuses, claim/execution/retry timestamps, bounded object metadata, history indexes, RLS enabled, and no public write policy.

The migration adds SELECT grants matching repository conventions but no public INSERT/UPDATE/DELETE grants. No third table, trigger, scheduler SQL, or business logic was added.

## Repository and fallback changes
Added `backend/ai/database/task_repository.py` with:

- `TaskRepository` interface.
- `TaskRecord` and `OccurrenceRecord` dataclasses.
- `InMemoryTaskRepository` implementing owner-scoped creation, retrieval, listing, compare-and-set task versioning, lifecycle transitions, occurrence idempotency, ownership checks, claims, transitions, retry timing validation, immutable occurrence fields, and maximum attempt enforcement.
- Structural validation for status/type values, nonblank bounded labels/keys, JSON shape, action count, and payload sizes.
- `SupabaseTaskRepository` interface placeholder for later repository wiring; it performs no database operation in this phase.
- Process-local fallback factory.

`RepositoryManager` now exposes one in-memory task repository under `task`, preserving the existing central manager convention. No scheduler or execution caller was connected.

## Tests added
`tests/test_task_repository.py` covers:

- task creation, owner isolation, initial version, CAS success/stale failure, and exact version increment;
- occurrence creation, duplicate identity behavior, owner isolation, and immutable snapshots;
- attempts 1–3 and rejection of attempt 4;
- valid claim/retry/interrupted transitions;
- rejection of missing retry timing and terminal-to-running transitions;
- task terminal lifecycle and preservation of occurrence history;
- malformed JSON, action count, and metadata validation.

These are unit tests of the in-memory repository/fallback. No live Supabase integration test ran.

## Validation actually executed
- `python3 -m pytest tests/test_task_repository.py -q --no-header` → **6 passed**.
- `python3 -m pytest tests/ -q --no-header` → **1080 passed, 23 skipped, 1 warning**.
- `python3 -m compileall -q backend tests` → passed.
- `git diff --check` → passed.

No live database or migration execution was performed.

## Ownership, RLS, and security
All repository methods require explicit `owner_id` and filter owner-scoped reads/writes in the fallback. Occurrences verify that their task belongs to the same owner. The migration has RLS enabled on both tables, no owner foreign key because the repository has no owner table, and no public write policies. The service-role backend convention remains unchanged.

Persisted JSON is structurally validated and never executed. No arbitrary shell, SQL, RPC, provider, Telethon, natural-language, or command execution was added. ToolExecutor, RuntimeSupervisor, profile scheduler, Engine, Dispatcher, ProviderManager, and TelegramAPI/self-client boundaries remain unchanged.

## Database/schema impact
Two new tables were added by one migration. No existing table or historical migration was modified. No SQL was executed against Supabase and no live Supabase state was changed.

## Limitations and remaining work
- `SupabaseTaskRepository` is intentionally not wired yet; database repository HTTP operations belong to the next repository integration step and must retain bounded async access/fallback behavior.
- Full schedule validation, IANA timezone/DST handling, next-run calculation, missed-run policy, retry classification/backoff execution, scheduler lifecycle, task parsing, task creation, action execution, notifications, and management UI remain unimplemented.
- Exact live RLS/schema state was not verified.
- Database-level owner/task consistency beyond the task foreign key remains an application/repository responsibility; no trigger was introduced.
- No exactly-once Telegram guarantee is claimed.

## Intentionally untouched
`INVESTIGATION.md`, `DATABASE_ARCHITECTURE.md`, existing migrations, profile scheduler, RuntimeSupervisor lifecycle, AI providers, Engine, Dispatcher, ToolExecutor execution behavior, handlers, dashboard, and Telegram execution code.

## Commit / delivery
- Implementation commit: pending until this report is committed after validation.
- Push result: pending.
- Remote verification: pending.
- Final working-tree state: pending.
