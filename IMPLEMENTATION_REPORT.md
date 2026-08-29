# Implementation Report — Durable AI Task/Scheduler Database Foundation

## Date
2026-08-29

## Objective / phase
Phase 2 database foundation for the durable AI Task/Scheduler system is implemented and documented. Exactly two durable tables, repository state contracts, fallback behavior, and focused tests are included. No scheduler, task parser, task execution, notification, or Telegram behavior is implemented.

## Repository / branch / commits
- Repository: `https://github.com/Onlyicing1/Telegram-self-bot`
- Branch: `main`
- Base commit: `da74cc4cab6833f6427599b9d0e3b89c22328df3`
- Implementation commit: `7c66f8464621a2a4fcbbaf93cb793e75bd76a8e9`
- Closure commit: pending until this report is committed.

## Exact files changed in closure
- `DATABASE_ARCHITECTURE.md` — added actual Phase 2 schema documentation and corrected the relationships/migration table to distinguish the task FK and repository migration state.
- `IMPLEMENTATION_REPORT.md` — replaced with this single current-state report.

Phase 2 implementation files already present and verified:
- `supabase/migrations/20260829000001_create_ai_tasks.sql`
- `backend/ai/database/task_repository.py`
- `backend/ai/database/manager.py`
- `tests/test_task_repository.py`

## Actual Phase 2 implementation

The migration contains exactly two tables in dependency order: `ai_tasks`, followed by `ai_task_occurrences`. The repository provides task/occurrence records, structural validation, owner-scoped in-memory persistence, task CAS versioning, occurrence identity uniqueness, claims, transitions, retry timing, immutable snapshots, and attempt limits. `RepositoryManager.task` exposes the process repository. The Supabase repository class remains intentionally unwired for the later integration phase.

## Actual schema: `ai_tasks`

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
```

The table's allowed statuses are `active`, `paused`, `completed`, `failed`, `expired`, and `deleted`. Allowed schedule types are `once`, `interval`, `daily`, and `weekly`. Full schedule/timezone semantics are not encoded in SQL.

## Actual schema: `ai_task_occurrences`

```sql
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

Allowed occurrence statuses are `claimed`, `running`, `succeeded`, `failed`, `retry_pending`, `cancelled`, `expired`, and `interrupted`. The FK uses `ON DELETE RESTRICT`; the unique `(task_id, occurrence_key)` index protects durable occurrence identity only, not exactly-once Telegram side effects.

## Manual Supabase application SQL

The two SQL blocks above are the complete application SQL extracted from `supabase/migrations/20260829000001_create_ai_tasks.sql`, including tables, constraints, indexes, RLS enablement, policy drops, and grants. They are a manual Supabase application handoff. The AI agent did **not** execute this SQL. Live Supabase state was not verified and remains a separate manual deployment step.

## Manual Supabase rollback SQL

This rollback is destructive and must be run manually only when intentionally removing the Phase 2 tables. It was not executed by the AI agent and does not affect unrelated tables:

```sql
DROP TABLE IF EXISTS ai_task_occurrences;
DROP TABLE IF EXISTS ai_tasks;
```

## RLS, ownership, and security

Both tables enable RLS and grant SELECT to `anon`/`authenticated` without public write policies. Backend access follows the service-role convention. Repository calls require explicit owner IDs and apply owner filtering; no users table or owner FK was invented. `task_id` is the sole enforced relationship. JSON is structurally bounded and never executed. No arbitrary shell, SQL, RPC, provider, Telethon, natural-language command, or Telegram action was added.

## Tests and validation actually executed

- `python3 -m pytest tests/test_task_repository.py -q --no-header` → **6 passed**.
- `python3 -m pytest tests/ -q --no-header` → **1080 passed, 23 skipped, 1 warning**.
- `python3 -m compileall -q backend tests` → passed.
- `git diff --check` → passed.

No live Supabase integration test, migration application, rollback, or live schema verification was performed.

## Architecture preserved / intentionally untouched

RuntimeSupervisor, profile.scheduler, Engine, Dispatcher, ProviderManager, ToolExecutor execution behavior, TelegramAPI/self-client behavior, task scheduler runtime, task parsing, task execution, notifications, dashboard APIs/UI, and all existing unrelated migrations/tables remain untouched. Database code stores state only. Exactly two durable task tables exist; no third table or trigger was added.

## Limitations / remaining work

The Supabase repository methods are not wired yet; current tests exercise the in-memory repository fallback. Later phases must add bounded async Supabase operations, scheduler lifecycle, deterministic schedule calculations, task parsing/creation, action execution, notification, and management behavior. Live database deployment remains unverified. No scheduler or Telegram scheduling is operational.

## Commit / delivery

- Closure commit: pending.
- Push result: pending.
- Remote HEAD: pending.
- Final working-tree state: pending.
