/* panel_settings: outbound bandwidth governor usage state (JSONB).

One dedicated JSONB column on the existing singleton row. The quota service
owns its schema; the repository is the only direct accessor. Additive and
idempotent.
*/

ALTER TABLE panel_settings ADD COLUMN IF NOT EXISTS outbound_bandwidth_state jsonb;
ALTER TABLE panel_settings ALTER COLUMN outbound_bandwidth_state SET DEFAULT '{}'::jsonb;
UPDATE panel_settings SET outbound_bandwidth_state = '{}'::jsonb WHERE outbound_bandwidth_state IS NULL;
ALTER TABLE panel_settings ALTER COLUMN outbound_bandwidth_state SET NOT NULL;

ALTER TABLE panel_settings DROP CONSTRAINT IF EXISTS panel_settings_outbound_bandwidth_state_check;
ALTER TABLE panel_settings
    ADD CONSTRAINT panel_settings_outbound_bandwidth_state_check
    CHECK (jsonb_typeof(outbound_bandwidth_state) = 'object');

INSERT INTO panel_settings (key)
VALUES ('global')
ON CONFLICT (key) DO NOTHING;

NOTIFY pgrst, 'reload schema';
