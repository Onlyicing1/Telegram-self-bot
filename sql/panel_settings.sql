-- panel_settings.sql — Global panel preferences (auto-close toggle)
-- Part of the LifeOS Telegram Self-Bot database schema.
-- See DATABASE_ARCHITECTURE.md for full documentation.

CREATE TABLE IF NOT EXISTS panel_settings (
    key                 text         PRIMARY KEY,
    auto_close_enabled  boolean      NOT NULL DEFAULT true,
    updated_at          timestamptz  DEFAULT now()
);

-- RLS
ALTER TABLE panel_settings ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "anon_select_panel_settings" ON panel_settings;
CREATE POLICY "anon_select_panel_settings" ON panel_settings FOR SELECT
    TO anon, authenticated USING (true);
