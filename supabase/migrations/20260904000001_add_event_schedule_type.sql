/*
# Event-triggered tasks: extend ai_tasks.schedule_type

Adds 'event' to the schedule_type CHECK constraint so a task can be a
durable event-driven automation ("when John sends me a message, run X")
with no wall-clock time. The trigger spec itself lives in the existing
`schedule` jsonb ({"trigger": {...}}); no new tables or columns.

Application layer (backend/ai/task_trigger.py) validates the trigger
structure and resolves sender/chat references from trusted runtime
context; this migration only widens the enum.
*/

ALTER TABLE ai_tasks DROP CONSTRAINT IF EXISTS ai_tasks_schedule_type_check;
ALTER TABLE ai_tasks ADD CONSTRAINT ai_tasks_schedule_type_check
    CHECK (schedule_type IN ('once', 'interval', 'daily', 'weekly', 'event'));

-- Manual application (Supabase SQL editor):
--   ALTER TABLE ai_tasks DROP CONSTRAINT IF EXISTS ai_tasks_schedule_type_check;
--   ALTER TABLE ai_tasks ADD CONSTRAINT ai_tasks_schedule_type_check
--       CHECK (schedule_type IN ('once', 'interval', 'daily', 'weekly', 'event'));
--
-- Rollback:
--   ALTER TABLE ai_tasks DROP CONSTRAINT IF EXISTS ai_tasks_schedule_type_check;
--   ALTER TABLE ai_tasks ADD CONSTRAINT ai_tasks_schedule_type_check
--       CHECK (schedule_type IN ('once', 'interval', 'daily', 'weekly'));