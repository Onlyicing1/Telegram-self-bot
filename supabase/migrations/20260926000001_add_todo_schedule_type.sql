/*
# Basic Todo items: extend ai_tasks.schedule_type with 'todo'

A Todo is a durable, owner-scoped item that has a title, a status
(active/completed) and NO schedule and NO action: the owner manages it from
the Todo panel in Telegram. It reuses the existing `ai_tasks` row — there is
no new table, no new column, no new repository and no parallel store.

Two constraints in `20260829000001_create_ai_tasks.sql` cannot express that
row shape, so this migration widens exactly them and nothing else:

  1. `ai_tasks_schedule_type_check` only allowed the five scheduled types
     (once / interval / daily / weekly / event). 'todo' is added to the enum.
     The scheduler is unaffected: a todo has `next_run_at IS NULL` and is
     never returned by the due-task or event-task queries.

  2. `ai_tasks_actions_count` required 1-5 actions, because a scheduled task
     must always have something to run. A todo has NO action. The constraint
     becomes CONDITIONAL: a 'todo' row stores 0-5 actions (always 0 in
     practice — the application never fabricates a placeholder action), while
     every scheduled row keeps the original 1-5 guarantee.

No other table, column, index, policy or grant is touched. Existing rows are
unchanged (the widened enum accepts every row the old constraint accepted).

Manual application (Supabase SQL editor) — this is the only step; the coding
agent never executes SQL against Supabase:

    ALTER TABLE ai_tasks DROP CONSTRAINT IF EXISTS ai_tasks_schedule_type_check;
    ALTER TABLE ai_tasks ADD CONSTRAINT ai_tasks_schedule_type_check
        CHECK (schedule_type IN ('once', 'interval', 'daily', 'weekly', 'event', 'todo'));

    ALTER TABLE ai_tasks DROP CONSTRAINT IF EXISTS ai_tasks_actions_count;
    ALTER TABLE ai_tasks ADD CONSTRAINT ai_tasks_actions_count CHECK (
        (schedule_type = 'todo' AND jsonb_array_length(actions) BETWEEN 0 AND 5)
        OR (schedule_type <> 'todo' AND jsonb_array_length(actions) BETWEEN 1 AND 5)
    );

    NOTIFY pgrst, 'reload schema';

Rollback (only when no todo row exists):

    ALTER TABLE ai_tasks DROP CONSTRAINT IF EXISTS ai_tasks_actions_count;
    ALTER TABLE ai_tasks ADD CONSTRAINT ai_tasks_actions_count
        CHECK (jsonb_array_length(actions) BETWEEN 1 AND 5);

    ALTER TABLE ai_tasks DROP CONSTRAINT IF EXISTS ai_tasks_schedule_type_check;
    ALTER TABLE ai_tasks ADD CONSTRAINT ai_tasks_schedule_type_check
        CHECK (schedule_type IN ('once', 'interval', 'daily', 'weekly', 'event'));
*/

ALTER TABLE ai_tasks DROP CONSTRAINT IF EXISTS ai_tasks_schedule_type_check;
ALTER TABLE ai_tasks ADD CONSTRAINT ai_tasks_schedule_type_check
    CHECK (schedule_type IN ('once', 'interval', 'daily', 'weekly', 'event', 'todo'));

ALTER TABLE ai_tasks DROP CONSTRAINT IF EXISTS ai_tasks_actions_count;
ALTER TABLE ai_tasks ADD CONSTRAINT ai_tasks_actions_count CHECK (
    (schedule_type = 'todo' AND jsonb_array_length(actions) BETWEEN 0 AND 5)
    OR (schedule_type <> 'todo' AND jsonb_array_length(actions) BETWEEN 1 AND 5)
);

NOTIFY pgrst, 'reload schema';
