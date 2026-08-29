# Implementation Report — Durable AI Task/Scheduler Database Foundation

## Date
2026-08-29

## Objective / phase
Phase 2 database foundation for the durable AI Task/Scheduler system is implemented and documented. Exactly two durable tables, repository state contracts, fallback behavior, and focused tests are included. No scheduler, task parser, task execution, notification, or Telegram behavior is implemented.

## Repository / branch / commits
- Repository: `https://github.com/Onlyicing1/Telegram-self-bot`
- Branch: `main`
- Base commit: `da74cc4cab6833f6427599b9d0e3b89c22328df3`
- Implementation base commit: `7c66f8464621a2a4fcbbaf93cb793e75bd76a8e9`
- Current commit: pending until this closure report is committed.

## Exact files changed in this closure
- `DATABASE_ARCHITECTURE.md` — added actual Phase 2 schema documentation and migration inventory entry.
- `IMPLEMENTATION_REPORT.md` — replaced with this single current-state report.

Phase 2 implementation files already present and verified:
- `supabase/migrations/20260829000001_create_ai_tasks.sql`
- `backend/ai/database/task_repository.py`
- `backend/ai/database/manager.py`
- `tests/test_task_repository.py`

No other files were changed by this closure.

## Actual Phase 2 implementation

The migration `supabase/migrations/20260829000001_create_ai_tasks.sql` contains exactly two tables in dependency order: `ai_tasks`, followed by `ai_task_occurrences`.

`task_repository.py` provides task/occurrence records, structural validation, owner-scoped in-memory persistence, task CAS versioning, occurrence identity uniqueness, claims, transitions, retry timing, immutable snapshots, and attempt limits. `RepositoryManager.task` exposes the process repository. The current `SupabaseTaskRepository` class is an explicit not-yet-wired boundary; no live database call was made by this phase.

## Schema: `ai_tasks`

Exact columns and effective definitions from the migration:

- `id bigserial PRIMARY KEY`
- `owner_id bigint NOT NULL`
- `label text NOT NULL`
- `status text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'paused', 'completed', 'failed', 'expired', 'deleted'))`
- `version integer NOT NULL DEFAULT 1 CHECK (version > 0)`
- `schedule_type text NOT NULL CHECK (schedule_type IN ('once', 'interval', 'daily', 'weekly'))`
- `schedule jsonb NOT NULL`
- `timezone text NOT NULL`
- `next_run_at timestamptz`
- `actions jsonb NOT NULL CHECK (jsonb_typeof(actions) = 'array')`
- `notification_destination jsonb NOT NULL`
- `created_at timestamptz NOT NULL DEFAULT now()`
- `updated_at timestamptz NOT NULL DEFAULT now()`
- `terminal_at timestamptz`

Additional migration constraints:

- `length(btrim(label)) > 0`
- `jsonb_array_length(actions) BETWEEN 1 AND 5`
- `octet_length(actions::text) <= 32768`
- `octet_length(schedule::text) <= 16384`
- `octet_length(notification_destination::text) <= 4096`

Indexes:

- `idx_ai_tasks_status_next_run` on `(status, next_run_at)`
- `idx_ai_tasks_owner_updated` on `(owner_id, updated_at DESC)`

RLS is enabled. The migration grants SELECT to `anon` and `authenticated`, drops any same-named select policy, and adds no public write policy. There is no owner foreign key. Schedule semantics remain application-level.

## Schema: `ai_task_occurrences`

Exact columns and effective definitions from the migration:

- `id bigserial PRIMARY KEY`
- `task_id bigint NOT NULL REFERENCES ai_tasks(id) ON DELETE RESTRICT`
- `owner_id bigint NOT NULL`
- `occurrence_key text NOT NULL`
- `definition_version integer NOT NULL CHECK (definition_version > 0)`
- `action_snapshot jsonb NOT NULL CHECK (jsonb_typeof(action_snapshot) = 'array')`
- `scheduled_for timestamptz NOT NULL`
- `attempt smallint NOT NULL DEFAULT 1 CHECK (attempt BETWEEN 1 AND 3)`
- `status text NOT NULL DEFAULT 'claimed' CHECK (status IN ('claimed', 'running', 'succeeded', 'failed', 'retry_pending', 'cancelled', 'expired', 'interrupted'))`
- `claimed_at timestamptz`
- `started_at timestamptz`
- `finished_at timestamptz`
- `retry_at timestamptz`
- `error_metadata jsonb NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(error_metadata) = 'object')`
- `result_metadata jsonb NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(result_metadata) = 'object')`
- `created_at timestamptz NOT NULL DEFAULT now()`
- `updated_at timestamptz NOT NULL DEFAULT now()`

Additional migration constraints:

- `length(btrim(occurrence_key)) > 0`
- `jsonb_array_length(action_snapshot) BETWEEN 1 AND 5`
- `octet_length(action_snapshot::text) <= 32768`
- `octet_length(error_metadata::text) <= 8192`
- `octet_length(result_metadata::text) <= 8192`
- `retry_pending` requires non-null `retry_at`

Indexes:

- unique `uq_ai_task_occurrences_task_key` on `(task_id, occurrence_key)`
- `idx_ai_task_occurrences_owner_scheduled` on `(owner_id, scheduled_for DESC)`
- `idx_ai_task_occurrences_task_scheduled` on `(task_id, scheduled_for DESC)`

RLS is enabled. The migration grants SELECT to `anon` and `authenticated`, adds no public write policy, and uses `ON DELETE RESTRICT` to preserve occurrence history. No `(status, retry_at)` index exists because the current repository does not issue that query.

## Ownership / versioning / occurrence identity

`owner_id` is the numeric Telegram owner identity and is required explicitly by repository methods. No users table or owner foreign key was invented. Repository reads and fallback operations filter by owner. The occurrence task relationship is enforced by the task foreign key plus repository owner consistency checks.

Task versions start at 1 and successful updates use expected-version compare-and-set, incrementing exactly once. Occurrences retain `definition_version` and a deep-copied action snapshot, so future task edits do not rewrite history. `(task_id, occurrence_key)` protects durable occurrence uniqueness only; it does not provide exactly-once Telegram execution.

## Manual Supabase application SQL

The following is copied from the repository migration and is a manual handoff. The AI agent did **not** execute it. Live Supabase application remains a separate user-controlled deployment step and was not verified.

```sql
CREATE TABLE IF NOT EXISTS ai_tasks (
    id                         bigserial    PRIMARY KEY,
    owner_id                   bigint       NOT NULL,
    label                      text         NOT NULL,
    status                     text         NOT NULL DEFAULT 'active'
                               CHECK (status IN ('active', 'paused', 'completed', 'failed', 'expired', 'deleted')),
    version                    integer      NOT NULL DEFAULT 1
                               CHECK (version > 0),
    schedule_type              text         NOT NULL
                               CHECK (schedule_type IN ('once', 'interval', 'daily', 'weekly')),
    schedule                   jsonb        NOT NULL,
    timezone                   text         NOT NULL,
    next_run_at               timestamptz,
    actions                    jsonb        NOT NULL
                               CHECK (jsonb_typeof(actions) = 'array'),
    notification_destination   jsonb        NOT NULL,
    created_at                 timestamptz  NOT NULL DEFAULT now(),
    updated_at                 timestamptz  NOT NULL DEFAULT now(),
    terminal_at                timestamptz,
    CONSTRAINT ai_tasks_label_not_blank CHECK (length(btrim(label)) > 0),
    CONSTRAINT ai_tasks_actions_count CHECK (jsonb_array_length(actions) BETWEEN 1 AND 5),
    CONSTRAINT ai_tasks_payload_size CHECK (octet_length(actions::text) <= 32768),
    CONSTRAINT ai_tasks_schedule_size CHECK (octet_length(schedule::text) <= 16384),
    CONSTRAINT ai_tasks_destination_size CHECK (octet_length(notification_destination::text) <= 4096)
);

CREATE INDEX IF NOT EXISTS idx_ai_tasks_status_next_run
    ON ai_tasks (status, next_run_at);
CREATE INDEX IF NOT EXISTS idx_ai_tasks_owner_updated
    ON ai_tasks (owner_id, updated_at DESC);

ALTER TABLE ai_tasks ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "anon_select_ai_tasks" ON ai_tasks;

GRANT SELECT ON ai_tasks TO anon, authenticated;

CREATE TABLE IF NOT EXISTS ai_task_occurrences (
    id                    bigserial    PRIMARY KEY,
    task_id               bigint       NOT NULL
                          REFERENCES ai_tasks(id) ON DELETE RESTRICT,
    owner_id              bigint       NOT NULL,
    occurrence_key        text         NOT NULL,
    definition_version    integer      NOT NULL CHECK (definition_version > 0),
    action_snapshot       jsonb        NOT NULL
                          CHECK (jsonb_typeof(action_snapshot) = 'array'),
    scheduled_for         timestamptz  NOT NULL,
    attempt               smallint     NOT NULL DEFAULT 1
                          CHECK (attempt BETWEEN 1 AND 3),
    status                text         NOT NULL DEFAULT 'claimed'
                          CHECK (status IN ('claimed', 'running', 'succeeded', 'failed', 'retry_pending', 'cancelled', 'expired', 'interrupted')),
    claimed_at            timestamptz,
    started_at            timestamptz,
    finished_at           timestamptz,
    retry_at              timestamptz,
    error_metadata        jsonb        NOT NULL DEFAULT '{}'
                          CHECK (jsonb_typeof(error_metadata) = 'object'),
    result_metadata       jsonb        NOT NULL DEFAULT '{}'
                          CHECK (jsonb_typeof(result_metadata) = 'object'),
    created_at            timestamptz  NOT NULL DEFAULT now(),
    updated_at            timestamptz  NOT NULL DEFAULT now(),
    CONSTRAINT ai_task_occurrences_key_not_blank CHECK (length(btrim(occurrence_key)) > 0),
    CONSTRAINT ai_task_occurrences_action_count CHECK (jsonb_array_length(action_snapshot) BETWEEN 1 AND 5),
    CONSTRAINT ai_task_occurrences_payload_size CHECK (octet_length(action_snapshot::text) <= 32768),
    CONSTRAINT ai_task_occurrences_error_size CHECK (octet_length(error_metadata::text) <= 8192),
    CONSTRAINT ai_task_occurrences_result_size CHECK (octet_length(result_metadata::text) <= 8192),
    CONSTRAINT ai_task_occurrences_retry_state CHECK (
        (status = 'retry_pending' AND retry_at IS NOT NULL)
        OR (status <> 'retry_pending')
    )
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_ai_task_occurrences_task_key
    ON ai_task_occurrences (task_id, occurrence_key);
CREATE INDEX IF NOT EXISTS idx_ai_task_occurrences_owner_scheduled
    ON ai_task_occurrences (owner_id, scheduled_for DESC);
CREATE INDEX IF NOT EXISTS idx_ai_task_occurrences_task_scheduled
    ON ai_task_occurrences (task_id, scheduled_for DESC);

ALTER TABLE ai_task_occurrences ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "anon_select_ai_task_occurrences" ON ai_task_occurrences;
GRANT SELECT ON ai_task_occurrences TO anon, authenticated;
```

## Manual Supabase rollback SQL

This is destructive and must only be run manually when intentionally removing the Phase 2 tables. The AI agent did **not** execute it. Dependency order is mandatory:

```sql
DROP TABLE IF EXISTS ai_task_occurrences;
DROP TABLE IF EXISTS ai_tasks;
```

No unrelated table is included.

## Tests and validation

Actually executed:

- `python3 -m pytest tests/test_task_repository.py -q --no-header` → **6 passed**.
- `python3 -m pytest tests/ -q --no-header` → **1080 passed, 23 skipped, 1 warning**.
- `python3 -m compileall -q backend tests` → passed.
- `git diff --check` → passed before closure edits; final closure diff will be checked again.

No live Supabase integration test or live schema verification was performed.

## Architecture and security boundaries

No scheduler, task parser, task execution worker, notification path, provider change, Engine/Dispatcher change, ToolExecutor execution change, RuntimeSupervisor lifecycle hook, Telegram behavior, arbitrary command execution, SQL business logic, or third durable table was added. Database code stores state only. Existing Self Bot and TelegramAPI execution authority remains unchanged.

## Current limitations / remaining work

The Supabase repository methods are not yet wired; the current tested implementation is the in-memory fallback. Later phases must add bounded async Supabase repository operations without changing the two-table model. Schedule semantics, DST, missed runs, scheduler lifecycle, task creation, action execution, notification, management UI, and retry classification remain future work. Live Supabase state is unknown until the user manually applies and verifies the migration.

## Final delivery

- Current commit: pending.
- Push result: pending.
- Remote HEAD: pending.
- Final working tree: pending.
