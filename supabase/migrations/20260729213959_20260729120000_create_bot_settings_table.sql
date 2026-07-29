/*
# Create bot_settings table — centralized configuration store

## Purpose
Replaces the panel_settings table with a general-purpose key-value
settings store. Every configurable feature (bio engine, save engine,
delete batching, panel auto-close, log retention, etc.) reads its
configuration from this table. The database is the single source of
truth; in-memory caches are refresh-only.

## New Tables

### bot_settings
- key (text, primary key) — unique settings key (e.g. "auto_close_enabled",
  "max_deep_save_mb", "delete_batch_size", "log_cleanup_days",
  "panel_auto_close_seconds")
- value (text, not null) — the setting value stored as text; the
  settings service coerces to the expected type (bool, int, str) on read.
- value_type (text, not null default 'str') — hint for deserialization:
  'bool', 'int', 'str'. The service uses this to parse correctly.
- updated_at (timestamptz) — last update timestamp.

## Migrated Data
- The auto_close_enabled setting from panel_settings is migrated to
  bot_settings with key='auto_close_enabled', value='true', value_type='bool'.
  If panel_settings already has a row, its current value is used instead.

## Security
- RLS enabled on bot_settings.
- SELECT granted to anon + authenticated (read-only dashboard access).
- All writes go through the backend service-role key (bypasses RLS).

## Important Notes
1. panel_settings table is NOT dropped — existing code may still
   reference it during the transition. The settings_service reads from
   bot_settings going forward.
2. The migration is idempotent — safe to re-run.
3. Default settings are inserted with ON CONFLICT DO NOTHING so existing
   values are never overwritten.
*/

CREATE TABLE IF NOT EXISTS bot_settings (
    key text PRIMARY KEY,
    value text NOT NULL,
    value_type text NOT NULL DEFAULT 'str',
    updated_at timestamptz DEFAULT now()
);

ALTER TABLE bot_settings ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "anon_select_bot_settings" ON bot_settings;
CREATE POLICY "anon_select_bot_settings" ON bot_settings FOR SELECT
    TO anon, authenticated USING (true);

-- Migrate auto_close_enabled from panel_settings if it exists
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'panel_settings') THEN
        INSERT INTO bot_settings (key, value, value_type, updated_at)
        SELECT 'auto_close_enabled',
               CASE WHEN auto_close_enabled THEN 'true' ELSE 'false' END,
               'bool',
               COALESCE(updated_at, now())
        FROM panel_settings
        WHERE key = 'global'
        ON CONFLICT (key) DO NOTHING;

        -- If panel_settings has no rows, insert the default
        INSERT INTO bot_settings (key, value, value_type)
        VALUES ('auto_close_enabled', 'true', 'bool')
        ON CONFLICT (key) DO NOTHING;
    ELSE
        INSERT INTO bot_settings (key, value, value_type)
        VALUES ('auto_close_enabled', 'true', 'bool')
        ON CONFLICT (key) DO NOTHING;
    END IF;
END $$;

-- Insert default values for all configurable settings
INSERT INTO bot_settings (key, value, value_type)
VALUES
    ('panel_auto_close_seconds', '120', 'int'),
    ('max_deep_save_mb', '50', 'int'),
    ('delete_batch_size', '100', 'int'),
    ('log_cleanup_days', '7', 'int')
ON CONFLICT (key) DO NOTHING;
