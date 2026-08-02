-- Persist Bio Engine and Username Engine active state.
--
-- Ensures is_active column exists on bio_state and username_state.
-- Idempotent: uses ADD COLUMN IF NOT EXISTS — safe to re-run.
-- Existing rows receive FALSE as the default automatically.
-- No tables recreated, no data lost.

ALTER TABLE bio_state
    ADD COLUMN IF NOT EXISTS is_active boolean NOT NULL DEFAULT false;

ALTER TABLE username_state
    ADD COLUMN IF NOT EXISTS is_active boolean NOT NULL DEFAULT false;
