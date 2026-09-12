/*
# ai_task_occurrences.preparation_metadata — schema-drift repair

`preparation_metadata` was introduced in 20260829000001_create_ai_tasks.sql,
but that file creates the table with ``CREATE TABLE IF NOT EXISTS``. A
database whose ``ai_task_occurrences`` table was created BEFORE the column was
added therefore never received it: re-running that migration is a no-op. The
application (OccurrenceRecord.preparation_metadata + the prepare-ahead /
occurrence-transition write paths) has since required the column, so PostgREST
answers PGRST204 ("Could not find the 'preparation_metadata' column ... in the
schema cache") once the schema cache is reloaded.

This migration is the minimal, idempotent repository-side repair: it adds the
column with exactly the definition the original migration declares, on every
database regardless of when the table was created. It is safe to apply twice.

No other schema change is made — no new tables, no new indexes, no policy
change, no RLS change.

Manual application (Supabase SQL editor):

    ALTER TABLE ai_task_occurrences
        ADD COLUMN IF NOT EXISTS preparation_metadata jsonb NOT NULL DEFAULT '{}';

    ALTER TABLE ai_task_occurrences
        DROP CONSTRAINT IF EXISTS ai_task_occurrences_preparation_metadata_object;
    ALTER TABLE ai_task_occurrences
        ADD CONSTRAINT ai_task_occurrences_preparation_metadata_object
        CHECK (jsonb_typeof(preparation_metadata) = 'object');

    ALTER TABLE ai_task_occurrences
        DROP CONSTRAINT IF EXISTS ai_task_occurrences_preparation_size;
    ALTER TABLE ai_task_occurrences
        ADD CONSTRAINT ai_task_occurrences_preparation_size
        CHECK (octet_length(preparation_metadata::text) <= 8192);

    NOTIFY pgrst, 'reload schema';

Rollback (only if the column is truly unused):

    ALTER TABLE ai_task_occurrences
        DROP CONSTRAINT IF EXISTS ai_task_occurrences_preparation_size;
    ALTER TABLE ai_task_occurrences
        DROP CONSTRAINT IF EXISTS ai_task_occurrences_preparation_metadata_object;
    ALTER TABLE ai_task_occurrences
        DROP COLUMN IF EXISTS preparation_metadata;
*/

ALTER TABLE ai_task_occurrences
    ADD COLUMN IF NOT EXISTS preparation_metadata jsonb NOT NULL DEFAULT '{}';

ALTER TABLE ai_task_occurrences
    DROP CONSTRAINT IF EXISTS ai_task_occurrences_preparation_metadata_object;
ALTER TABLE ai_task_occurrences
    ADD CONSTRAINT ai_task_occurrences_preparation_metadata_object
    CHECK (jsonb_typeof(preparation_metadata) = 'object');

ALTER TABLE ai_task_occurrences
    DROP CONSTRAINT IF EXISTS ai_task_occurrences_preparation_size;
ALTER TABLE ai_task_occurrences
    ADD CONSTRAINT ai_task_occurrences_preparation_size
    CHECK (octet_length(preparation_metadata::text) <= 8192);

NOTIFY pgrst, 'reload schema';
