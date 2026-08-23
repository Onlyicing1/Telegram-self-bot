-- Migration: persist Glass font and Ghost Seen retention settings
-- Idempotent: safe to run more than once.
-- The runtime uses the existing panel_settings repository; no new table is introduced.

ALTER TABLE panel_settings
    ADD COLUMN IF NOT EXISTS dashboard_font text NOT NULL DEFAULT 'default',
    ADD COLUMN IF NOT EXISTS ghost_seen_retention_days integer NOT NULL DEFAULT 30;

UPDATE panel_settings
SET dashboard_font = 'default'
WHERE dashboard_font IS NULL;

UPDATE panel_settings
SET ghost_seen_retention_days = 30
WHERE ghost_seen_retention_days IS NULL
   OR ghost_seen_retention_days < 1
   OR ghost_seen_retention_days > 365;

INSERT INTO panel_settings (key, dashboard_font, ghost_seen_retention_days)
VALUES ('global', 'default', 30)
ON CONFLICT (key) DO NOTHING;

ALTER TABLE panel_settings
    DROP CONSTRAINT IF EXISTS panel_settings_dashboard_font_check;

ALTER TABLE panel_settings
    ADD CONSTRAINT panel_settings_dashboard_font_check
    CHECK (dashboard_font IN (
        'default', 'serif_bold', 'serif_italic', 'serif_bold_italic',
        'sans', 'sans_bold', 'sans_italic', 'sans_bold_italic',
        'script', 'script_bold', 'fraktur', 'fraktur_bold',
        'double_struck', 'mono', 'small_caps', 'circled',
        'circled_dark', 'fullwidth', 'parenthesized', 'underline',
        'strikethrough', 'overline', 'wavy_underline'
    ));

ALTER TABLE panel_settings
    DROP CONSTRAINT IF EXISTS panel_settings_ghost_seen_retention_check;

ALTER TABLE panel_settings
    ADD CONSTRAINT panel_settings_ghost_seen_retention_check
    CHECK (ghost_seen_retention_days BETWEEN 1 AND 365);
