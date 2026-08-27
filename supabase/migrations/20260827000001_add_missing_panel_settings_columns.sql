/*
# Add the 10 missing panel_settings typed columns

Resolves DATABASE_ARCHITECTURE.md §19.3 / §20 item 1.

`settings_service._DEFAULTS` (backend/services/settings_service.py) defines
12 typed settings; before this migration only `key`, `auto_close_enabled`,
`updated_at`, `dashboard_font`, `ghost_seen_retention_seconds` (and the
legacy `update_stale_seconds`) existed as columns. Every write to the 10
missing columns was rejected by PostgREST (unknown column), silently
degraded to the in-memory cache, and still reported success — settings
never survived a restart.

Types and defaults are taken from `_DEFAULTS`; the CHECK constraints mirror
the `_VALIDATORS` ranges exactly. Idempotent: safe to run more than once.
Applying it to the live Supabase project is a separate manual owner action.

2. Security
- No change: RLS is already enabled on `panel_settings` with a SELECT-only
  policy for anon + authenticated. All writes go through the backend
  service-role key (bypasses RLS).
*/

ALTER TABLE panel_settings
    ADD COLUMN IF NOT EXISTS auto_close_delay      integer NOT NULL DEFAULT 120,
    ADD COLUMN IF NOT EXISTS max_deep_save_mb      integer NOT NULL DEFAULT 50,
    ADD COLUMN IF NOT EXISTS delete_batch_size     integer NOT NULL DEFAULT 100,
    ADD COLUMN IF NOT EXISTS log_retention_days    integer NOT NULL DEFAULT 7,
    ADD COLUMN IF NOT EXISTS panel_timeout_seconds integer NOT NULL DEFAULT 300,
    ADD COLUMN IF NOT EXISTS allow_multiple_panels boolean NOT NULL DEFAULT false,
    ADD COLUMN IF NOT EXISTS reuse_existing_panel  boolean NOT NULL DEFAULT true,
    ADD COLUMN IF NOT EXISTS language              text    NOT NULL DEFAULT 'en',
    ADD COLUMN IF NOT EXISTS debug_callbacks       boolean NOT NULL DEFAULT false,
    ADD COLUMN IF NOT EXISTS owner_only            boolean NOT NULL DEFAULT true;

-- Ensure the singleton row exists so repository updates always hit a row.
INSERT INTO panel_settings (key)
VALUES ('global')
ON CONFLICT (key) DO NOTHING;

-- CHECK constraints mirroring settings_service._VALIDATORS ranges.
ALTER TABLE panel_settings DROP CONSTRAINT IF EXISTS panel_settings_auto_close_delay_check;
ALTER TABLE panel_settings
    ADD CONSTRAINT panel_settings_auto_close_delay_check
    CHECK (auto_close_delay BETWEEN 5 AND 3600);

ALTER TABLE panel_settings DROP CONSTRAINT IF EXISTS panel_settings_max_deep_save_mb_check;
ALTER TABLE panel_settings
    ADD CONSTRAINT panel_settings_max_deep_save_mb_check
    CHECK (max_deep_save_mb BETWEEN 1 AND 500);

ALTER TABLE panel_settings DROP CONSTRAINT IF EXISTS panel_settings_delete_batch_size_check;
ALTER TABLE panel_settings
    ADD CONSTRAINT panel_settings_delete_batch_size_check
    CHECK (delete_batch_size BETWEEN 1 AND 1000);

ALTER TABLE panel_settings DROP CONSTRAINT IF EXISTS panel_settings_log_retention_days_check;
ALTER TABLE panel_settings
    ADD CONSTRAINT panel_settings_log_retention_days_check
    CHECK (log_retention_days BETWEEN 1 AND 365);

ALTER TABLE panel_settings DROP CONSTRAINT IF EXISTS panel_settings_panel_timeout_seconds_check;
ALTER TABLE panel_settings
    ADD CONSTRAINT panel_settings_panel_timeout_seconds_check
    CHECK (panel_timeout_seconds BETWEEN 30 AND 86400);

ALTER TABLE panel_settings DROP CONSTRAINT IF EXISTS panel_settings_language_check;
ALTER TABLE panel_settings
    ADD CONSTRAINT panel_settings_language_check
    CHECK (length(btrim(language)) > 0);
