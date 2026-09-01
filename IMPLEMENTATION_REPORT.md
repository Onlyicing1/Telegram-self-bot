# Implementation Report — Taskloom Phase 1

## Phase

Phase 1: durable AI-task/occurrence representation and persistence observability.

Scheduler-side AI preparation/readiness, latency warnings, Delete, and timezone-display fixes remain out of scope.

## Architecture decisions

- Added a dedicated bounded `AIInstruction` contract. It is explicitly identified as `kind=ai_instruction`, versioned, and never placed in the executable action list.
- Persisted the task-level instruction as the additive nullable `ai_instruction` field on `ai_tasks`. Existing deterministic tasks remain valid with `NULL`.
- Added occurrence-level `preparation_metadata`, defaulting to `{}`. A future prepared value is an explicit `prepared_action` envelope containing a definition version, timestamp, and validated registered-tool action only.
- Kept `action_snapshot` as the existing executable-action contract. Provider prose cannot be stored there as executable state.
- Preserved the existing owner-scoped repository, scheduler, coordinator, ToolRegistry/ToolExecutor, TelegramAPI, and Self Bot execution boundary.
- Added structured repository/service logs for task and occurrence persistence attempts, successes, exception class/message, and fallback activation. Messages are truncated and no secrets or payload contents are logged.
- The existing in-memory fallback remains; fallback activation is now visible rather than being indistinguishable from durable success.

## Files changed

- `backend/ai/task_contract.py` — new bounded instruction and prepared-action contracts.
- `backend/ai/database/task_repository.py` — model fields, validation, serialization/deserialization, persistence observability, and fallback handling.
- `backend/ai/task_creation.py` — accepts and validates the explicit AI instruction and logs the service persistence boundary.
- `backend/ai/task_interpreter.py` — imports the contract bound for future structured instruction support.
- `supabase/migrations/20260829000001_create_ai_tasks.sql` — adds the two nullable/defaulted data-contract fields and constraints.
- `DATABASE_ARCHITECTURE.md` — updates the canonical `ai_tasks` and `ai_task_occurrences` contracts.
- `tests/test_task_contract.py` — focused contract validation tests.
- `tests/test_task_repository.py` — existing repository contract coverage exercised with the new fields.
- `tests/test_task_nl_creation.py` — corrected stale expectation: high-confidence deterministic scheduling uses zero provider rounds.
- `IMPLEMENTATION_REPORT.md` — this current report.

## Schema changes and SQL

The repository migration adds:

```sql
ai_tasks.ai_instruction text NULL
ai_task_occurrences.preparation_metadata jsonb NOT NULL DEFAULT '{}'
```

with bounded checks for instruction size and preparation metadata size/object shape. The exact application SQL is in `supabase/migrations/20260829000001_create_ai_tasks.sql`; it was not executed against Supabase.

This migration file is the repository’s authoritative create SQL. Because the tables are created with `CREATE TABLE IF NOT EXISTS`, no separate live alteration was performed here. If the tables already exist in a deployed database, apply the dependency-safe additive SQL manually only after review:

```sql
ALTER TABLE ai_tasks
  ADD COLUMN IF NOT EXISTS ai_instruction text;
ALTER TABLE ai_tasks
  ADD CONSTRAINT ai_tasks_ai_instruction_size CHECK (
    ai_instruction IS NULL OR
    (length(btrim(ai_instruction)) > 0 AND octet_length(ai_instruction) <= 16384)
  );
ALTER TABLE ai_task_occurrences
  ADD COLUMN IF NOT EXISTS preparation_metadata jsonb NOT NULL DEFAULT '{}';
ALTER TABLE ai_task_occurrences
  ADD CONSTRAINT ai_task_occurrences_preparation_object
    CHECK (jsonb_typeof(preparation_metadata) = 'object');
ALTER TABLE ai_task_occurrences
  ADD CONSTRAINT ai_task_occurrences_preparation_size
    CHECK (octet_length(preparation_metadata::text) <= 8192);
```

Dependency-safe rollback SQL, for a database where no later application code/data depends on these fields:

```sql
ALTER TABLE ai_task_occurrences
  DROP CONSTRAINT IF EXISTS ai_task_occurrences_preparation_size;
ALTER TABLE ai_task_occurrences
  DROP CONSTRAINT IF EXISTS ai_task_occurrences_preparation_object;
ALTER TABLE ai_task_occurrences
  DROP COLUMN IF EXISTS preparation_metadata;
ALTER TABLE ai_tasks
  DROP CONSTRAINT IF EXISTS ai_tasks_ai_instruction_size;
ALTER TABLE ai_tasks
  DROP COLUMN IF EXISTS ai_instruction;
```

No SQL was run against live Supabase.

## Tests and verification

- Focused Taskloom/contract tests: **25 passed**.
- Full suite: **1233 passed, 23 skipped, 1 warning**.
- `python3 -m compileall -q backend tests`: passed.
- `git diff --check`: passed.
- No live Telegram verification was performed.
- No live Supabase verification or mutation was performed.

## Preserved behavior and scope exclusions

- Existing static/deterministic task creation remains compatible.
- No scheduler preparation lifecycle or readiness/CAS semantics were implemented.
- No provider precomputation loop or latency warning was implemented.
- Delete and timezone-display bugs were intentionally left untouched.
- No Telegram behavior, RuntimeSupervisor behavior, configuration, dependencies, or unrelated feature was changed.
- The pre-existing unrelated modification in `tests/test_stage13.py` was preserved and excluded from this implementation.

## Git delivery

- Commit SHA: pending until the implementation is committed.
- Push status: pending until the commit is pushed.
- Remote `origin/main`: pending until fetch and `git ls-remote` verification.
- Final working-tree status: pending final delivery verification.

## Next implementation stage

Phase 2: per-occurrence AI preparation lifecycle, timing before the scheduled deadline, durable readiness/CAS semantics, restart recovery, definition-version invalidation, retry reuse, and measured latency-warning behavior.
