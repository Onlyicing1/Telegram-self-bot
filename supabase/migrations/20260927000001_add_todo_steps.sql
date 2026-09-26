/*
# Multi-step Todos: the ordered steps of ONE todo

A Todo is the unscheduled kind of `ai_tasks` row (`schedule_type = 'todo'`, see
`20260926000001_add_todo_schedule_type.sql`). This migration adds the ONLY new
concept of the multi-step phase: a Todo may own an ordered list of steps, and
each step is either still remaining or completed.

There is no second task model here. A step is NOT a task: it has no schedule,
no action, no occurrence, no notification destination and no independent
identity outside its parent Todo. It cannot be executed, claimed or scheduled,
and it is never returned by any task query — the durable-task tables and the
Taskloom scheduler are untouched.

    ai_tasks (schedule_type = 'todo')  1 ─── * todo_steps (ordered by position)

## Columns

- `id`           — surrogate identity (stable for the lifetime of the row).
- `task_id`      — the parent Todo (`ai_tasks.id`). `ON DELETE CASCADE`: steps
                   are the Todo's own children, so removing the Todo removes
                   them in the same statement — a step can never be orphaned,
                   and no application cleanup pass is required to guarantee
                   that. (The occurrence table uses `ON DELETE RESTRICT`
                   because an occurrence is EXECUTION HISTORY the repository
                   deliberately deletes first; a step has no history value
                   outside its parent.)
- `owner_id`     — the owning Telegram user. Every repository read/write
                   filters by it, and the parent row must carry the same
                   owner (enforced in the repository/service boundary).
- `position`     — 1-based order inside the parent Todo. Positions are NEVER
                   renumbered: a step keeps its position for its whole life, so
                   "the step at position N" is stable even after a delete (the
                   user-facing step NUMBER is the ordinal in position order,
                   which is always contiguous). `UNIQUE (task_id, position)`
                   makes a duplicated order structurally impossible.
- `title`        — the owner's own words, nonblank, max 256 characters (the
                   same bound as `ai_tasks.label`). Never invented and never
                   auto-generated.
- `status`       — `active` (still remaining) or `completed`. Deliberately
                   minimal: there is no BLOCKED/CANCELLED/PAUSED/IN_PROGRESS
                   state, because nothing in this architecture can produce one.
- `version`      — compare-and-set counter, exactly like `ai_tasks.version`.
                   A stale mutation fails and writes nothing.
- `completed_at` — the instant the step was completed; NULL while active. The
                   CHECK binds it to the status so the pair can never disagree.
- `created_at` / `updated_at` — durable timestamps.

## Constraints and indexes

Every constraint is declared inline in the CREATE (this table has no legacy
shape to reconcile, so it needs no `ADD COLUMN IF NOT EXISTS` / data-guarded
constraint pass): a nonblank bounded title, a positive position, a positive
version, the two-value status, and the `status`/`completed_at` agreement.
`UNIQUE (task_id, position)` is the ONLY index the application needs: it
provides the ordered per-Todo read and the parent-key lookup the
`ON DELETE CASCADE` uses, and a Todo holds at most 50 steps, so no additional
owner/task index is warranted.

## Security

RLS is enabled and `anon`/`authenticated` receive SELECT only — the same
read-only dashboard boundary every other table has. Every write goes through
the backend's service-role client, and the repository filters by `owner_id`, so
one owner can never read or mutate another owner's steps. The AI never reaches
the database: it calls a tool, the tool calls the service, and the service goes
through this table's repository methods only.

Idempotent: safe to run more than once. Applying it to the live Supabase
project is a separate manual owner action.

## MANUAL SUPABASE ACTION REQUIRED

Apply this file (it is part 8 of the ONE complete setup script in
DATABASE_ARCHITECTURE.md §31.3, so it is normally applied by running that block
once). Rollback (destroys the step rows; the Todos themselves are untouched and
keep working, only their step lists are lost):

    DROP TABLE IF EXISTS todo_steps;

Nothing else in the schema changes: no existing table, column, index, policy,
grant or row is touched.
*/

CREATE TABLE IF NOT EXISTS todo_steps (
    id           bigserial   PRIMARY KEY,
    task_id      bigint      NOT NULL REFERENCES ai_tasks(id) ON DELETE CASCADE,
    owner_id     bigint      NOT NULL,
    position     integer     NOT NULL,
    title        text        NOT NULL,
    status       text        NOT NULL DEFAULT 'active',
    version      integer     NOT NULL DEFAULT 1,
    completed_at timestamptz,
    created_at   timestamptz NOT NULL DEFAULT now(),
    updated_at   timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT todo_steps_position_positive
        CHECK (position > 0),
    CONSTRAINT todo_steps_title_not_blank
        CHECK (length(btrim(title)) > 0 AND length(title) <= 256),
    CONSTRAINT todo_steps_status_check
        CHECK (status IN ('active', 'completed')),
    CONSTRAINT todo_steps_version_check
        CHECK (version >= 1),
    CONSTRAINT todo_steps_completed_state
        CHECK ((status = 'completed') = (completed_at IS NOT NULL)),
    CONSTRAINT todo_steps_task_position_key
        UNIQUE (task_id, position)
);

ALTER TABLE todo_steps ENABLE ROW LEVEL SECURITY;

GRANT SELECT ON todo_steps TO anon, authenticated;

DROP POLICY IF EXISTS "anon_insert_todo_steps" ON todo_steps;
DROP POLICY IF EXISTS "anon_update_todo_steps" ON todo_steps;
DROP POLICY IF EXISTS "anon_delete_todo_steps" ON todo_steps;
DROP POLICY IF EXISTS "anon_select_todo_steps" ON todo_steps;
CREATE POLICY "anon_select_todo_steps" ON todo_steps FOR SELECT
    TO anon, authenticated USING (true);

COMMENT ON TABLE todo_steps IS
    'The ordered steps of ONE todo (ai_tasks row with schedule_type = ''todo''). A step is not a task: it has no schedule, no action and no occurrence, and it is never executed or scheduled.';
COMMENT ON COLUMN todo_steps.task_id IS
    'Parent todo. ON DELETE CASCADE: deleting the todo deletes its steps, so a step can never be orphaned.';
COMMENT ON COLUMN todo_steps.position IS
    '1-based order inside the parent todo. Never renumbered; UNIQUE (task_id, position) makes a duplicated order impossible.';
COMMENT ON COLUMN todo_steps.status IS
    'active (still remaining) or completed. No other state exists in this architecture.';
COMMENT ON COLUMN todo_steps.version IS
    'Compare-and-set counter; a stale step mutation fails and writes nothing.';

NOTIFY pgrst, 'reload schema';

SELECT 'todo_steps' AS check, count(*) AS present
FROM information_schema.tables
WHERE table_schema = 'public'
  AND table_name   = 'todo_steps';
