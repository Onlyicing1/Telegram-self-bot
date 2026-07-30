-- panel_settings.sql — Glass Panel configuration (column-per-setting model)
-- Part of the LifeOS Telegram Self-Bot database schema.
-- See DATABASE_ARCHITECTURE.md for full documentation.
--
-- This table stores ONLY configuration values used by the Glass Panel.
-- It is a singleton (one row, key = "global").
-- Each setting is a real typed column — no key-value store.
--
-- Adding a new panel setting requires only:
--   1. ALTER TABLE panel_settings ADD COLUMN ...
--   2. A validator + accessor in settings_service.py
-- No panel code needs to change.

CREATE TABLE IF NOT EXISTS panel_settings (
    key                        text         PRIMARY KEY,
    auto_close_enabled         boolean      NOT NULL DEFAULT true,
    panel_auto_close_seconds   integer      NOT NULL DEFAULT 120,
    max_deep_save_mb            integer      NOT NULL DEFAULT 50,
    delete_batch_size           integer      NOT NULL DEFAULT 100,
    log_cleanup_days           integer      NOT NULL DEFAULT 7,
    update_stale_seconds       integer      NOT NULL DEFAULT 300,
    updated_at                 timestamptz  DEFAULT now()
);

-- Seed the singleton row if it doesn't exist
INSERT INTO panel_settings (key)
SELECT 'global'
WHERE NOT EXISTS (SELECT 1 FROM panel_settings WHERE key = 'global');

-- RLS
ALTER TABLE panel_settings ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "anon_select_panel_settings" ON panel_settings;
CREATE POLICY "anon_select_panel_settings" ON panel_settings FOR SELECT
    TO anon, authenticated USING (true);
