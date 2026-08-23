-- Migration: Ghost Seen retention as a duration (seconds), replacing the
-- days-only column. Sub-day windows (e.g. 30 minutes, 2 hours) are now
-- first-class Glass UI presets.
--
-- Idempotent: safe to run more than once. The backfill runs only while
-- the legacy ghost_seen_retention_days column still exists; afterwards
-- the statement is skipped and the seconds column is left untouched.
--
-- NOTE (honesty): this migration is a FILE in the repository. Applying it
-- to the live Supabase project is a separate manual owner action.

ALTER TABLE panel_settings
    ADD COLUMN IF NOT EXISTS ghost_seen_retention_seconds bigint NOT NULL DEFAULT 2592000;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'panel_settings'
          AND column_name = 'ghost_seen_retention_days'
    ) THEN
        UPDATE panel_settings
        SET ghost_seen_retention_seconds = ghost_seen_retention_days::bigint * 86400;

        ALTER TABLE panel_settings
            DROP CONSTRAINT IF EXISTS panel_settings_ghost_seen_retention_check;

        ALTER TABLE panel_settings
            DROP COLUMN ghost_seen_retention_days;
    END IF;
END $$;

UPDATE panel_settings
SET ghost_seen_retention_seconds = 2592000
WHERE ghost_seen_retention_seconds IS NULL
   OR ghost_seen_retention_seconds < 1800
   OR ghost_seen_retention_seconds > 31536000;

INSERT INTO panel_settings (key, ghost_seen_retention_seconds)
VALUES ('global', 2592000)
ON CONFLICT (key) DO NOTHING;

ALTER TABLE panel_settings
    DROP CONSTRAINT IF EXISTS panel_settings_ghost_seen_retention_seconds_check;

ALTER TABLE panel_settings
    ADD CONSTRAINT panel_settings_ghost_seen_retention_seconds_check
    CHECK (ghost_seen_retention_seconds BETWEEN 1800 AND 31536000);
