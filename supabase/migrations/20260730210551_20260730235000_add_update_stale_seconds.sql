/*
# Add update_stale_seconds to panel_settings

1. Changes
- Add `update_stale_seconds` integer column to `panel_settings` table.
- Default: 300 (5 minutes). Range: 60..3600 seconds.
- This setting controls how long the self-client watchdog tolerates
  no incoming Telegram updates before triggering a full client recovery.
- When the watchdog detects that `last_telethon_event` is older than
  this threshold while the heartbeat RPC (get_me) still succeeds, it
  declares the update-receive loop stalled and rebuilds the client.

2. Security
- No RLS changes. Existing SELECT-only policy for anon+authenticated remains.

3. Important notes
- The column is nullable=false with a default so existing rows get 300.
- The watchdog reads this value via settings_service on every tick.
*/

ALTER TABLE panel_settings
  ADD COLUMN IF NOT EXISTS update_stale_seconds integer NOT NULL DEFAULT 300;
