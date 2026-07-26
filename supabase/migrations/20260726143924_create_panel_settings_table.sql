/*
# Create panel_settings table for global panel preferences

1. New Tables
- `panel_settings`
  - `key` (text, primary key) — settings key (e.g. "global")
  - `auto_close_enabled` (boolean, default true) — whether panels auto-close after 120s
  - `updated_at` (timestamptz) — last update timestamp

2. Security
- Enable RLS on `panel_settings`.
- Allow anon + authenticated SELECT (read-only dashboard access).
- All writes go through the backend service-role key (bypasses RLS).
*/

CREATE TABLE IF NOT EXISTS panel_settings (
    key text PRIMARY KEY,
    auto_close_enabled boolean NOT NULL DEFAULT true,
    updated_at timestamptz DEFAULT now()
);

ALTER TABLE panel_settings ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "anon_select_panel_settings" ON panel_settings;
CREATE POLICY "anon_select_panel_settings" ON panel_settings FOR SELECT
    TO anon, authenticated USING (true);
