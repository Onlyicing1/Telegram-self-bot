/*
# Expand panel_settings as the permanent configuration storage for the helper panel

## Purpose

The existing `panel_settings` table only has `key` and `auto_close_enabled`.
The Settings panel already exposes 5 configurable values. This migration
redesigns `panel_settings` as a single-row, typed-column configuration table
so that every helper setting lives in its own properly-typed column with
a sensible default. Supabase becomes the single source of truth — the
panel UI reads every value directly from the database and writes changes
back immediately.

## Changes

### New columns added to `panel_settings`

| Column                      | Type        | Default | Purpose                                          |
|----------------------------|-------------|---------|--------------------------------------------------|
| auto_close_delay_seconds   | integer     | 120     | Seconds before an inline panel auto-closes       |
| max_deep_save_mb            | integer     | 50      | Maximum file size (MB) for deep saves            |
| delete_batch_size           | integer     | 100     | Messages per delete_messages() API call          |
| log_retention_days          | integer     | 7       | Days of logs to retain before cleanup            |
| panel_countdown_interval    | integer     | 30      | Seconds between countdown re-renders             |
| input_timeout_seconds       | integer     | 120     | Seconds before a pending input request expires   |

### Existing column preserved
- `key` (text, primary key) — unchanged
- `auto_close_enabled` (boolean, default true) — unchanged
- `updated_at` (timestamptz, default now()) — unchanged

### Data migration
- Migrates values from `bot_settings` key-value table if it exists
  (auto_close_enabled, panel_auto_close_seconds, max_deep_save_mb,
  delete_batch_size, log_cleanup_days) into the new typed columns.
- Ensures exactly one row with key='global' exists after migration.
- All existing rows are preserved — no data is lost.

### Security
- RLS remains enabled (already was).
- SELECT policy for anon + authenticated remains (already existed).
- No new write policies — all writes go through the backend service-role key.

## Important Notes
1. This migration is idempotent — safe to re-run.
2. Existing installations migrate automatically — no manual editing required.
3. The `bot_settings` key-value table is NOT dropped — it remains for
   backward compatibility but is no longer the source of truth.
4. CHECK constraints enforce sensible ranges on all integer columns.
*/

-- Add new columns with defaults (idempotent)
ALTER TABLE panel_settings
    ADD COLUMN IF NOT EXISTS auto_close_delay_seconds integer NOT NULL DEFAULT 120
        CHECK (auto_close_delay_seconds >= 10 AND auto_close_delay_seconds <= 3600);

ALTER TABLE panel_settings
    ADD COLUMN IF NOT EXISTS max_deep_save_mb integer NOT NULL DEFAULT 50
        CHECK (max_deep_save_mb >= 1 AND max_deep_save_mb <= 500);

ALTER TABLE panel_settings
    ADD COLUMN IF NOT EXISTS delete_batch_size integer NOT NULL DEFAULT 100
        CHECK (delete_batch_size >= 1 AND delete_batch_size <= 1000);

ALTER TABLE panel_settings
    ADD COLUMN IF NOT EXISTS log_retention_days integer NOT NULL DEFAULT 7
        CHECK (log_retention_days >= 1 AND log_retention_days <= 365);

ALTER TABLE panel_settings
    ADD COLUMN IF NOT EXISTS panel_countdown_interval integer NOT NULL DEFAULT 30
        CHECK (panel_countdown_interval >= 5 AND panel_countdown_interval <= 120);

ALTER TABLE panel_settings
    ADD COLUMN IF NOT EXISTS input_timeout_seconds integer NOT NULL DEFAULT 120
        CHECK (input_timeout_seconds >= 10 AND input_timeout_seconds <= 600);

-- Ensure the 'global' row exists
INSERT INTO panel_settings (key, auto_close_enabled)
VALUES ('global', true)
ON CONFLICT (key) DO NOTHING;

-- Migrate values from bot_settings if it exists
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'bot_settings') THEN
        -- auto_close_enabled
        UPDATE panel_settings ps
        SET auto_close_enabled = (bs.value = 'true'),
            updated_at = now()
        FROM bot_settings bs
        WHERE bs.key = 'auto_close_enabled'
          AND ps.key = 'global';

        -- panel_auto_close_seconds -> auto_close_delay_seconds
        UPDATE panel_settings ps
        SET auto_close_delay_seconds = GREATEST(10, LEAST(3600, bs.value::int)),
            updated_at = now()
        FROM bot_settings bs
        WHERE bs.key = 'panel_auto_close_seconds'
          AND ps.key = 'global';

        -- max_deep_save_mb
        UPDATE panel_settings ps
        SET max_deep_save_mb = GREATEST(1, LEAST(500, bs.value::int)),
            updated_at = now()
        FROM bot_settings bs
        WHERE bs.key = 'max_deep_save_mb'
          AND ps.key = 'global';

        -- delete_batch_size
        UPDATE panel_settings ps
        SET delete_batch_size = GREATEST(1, LEAST(1000, bs.value::int)),
            updated_at = now()
        FROM bot_settings bs
        WHERE bs.key = 'delete_batch_size'
          AND ps.key = 'global';

        -- log_cleanup_days -> log_retention_days
        UPDATE panel_settings ps
        SET log_retention_days = GREATEST(1, LEAST(365, bs.value::int)),
            updated_at = now()
        FROM bot_settings bs
        WHERE bs.key = 'log_cleanup_days'
          AND ps.key = 'global';
    END IF;
END $$;

-- Ensure RLS and SELECT policy are in place (idempotent)
ALTER TABLE panel_settings ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "anon_select_panel_settings" ON panel_settings;
CREATE POLICY "anon_select_panel_settings" ON panel_settings FOR SELECT
    TO anon, authenticated USING (true);
