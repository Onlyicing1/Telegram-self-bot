/*
# Create LifeOS Core Tables

## Purpose
Creates the three core tables required by the LifeOS Telegram Self-Bot:
saved_items, bio_state, and bot_logs.

## New Tables

### 1. saved_items
Stores metadata for both forward saves and deep saves.
- id: auto-incrementing primary key
- save_code: unique human-readable code (SV-000001 format)
- save_type: 'forward' or 'deep'
- origin_chat_id: Telegram chat ID where the message originated
- origin_msg_id: Telegram message ID of the original message
- saved_chat_id: Chat ID where the message was saved (Saved Messages)
- saved_msg_id: Message ID in the saved location
- sender_name: Display name of the original sender
- sender_id: Telegram user ID of the sender
- mime_type: MIME type of the media
- file_id: Telegram file ID reference
- file_size: Size in bytes
- media_type: Classified type (Photo, Video, Audio, etc.)
- tags: Array of hashtag strings for organization
- caption: Generated caption (deep saves only)
- owner_id: Telegram user ID of the bot owner
- created_at: Timestamp of the save

### 2. bio_state
Singleton-like state for the bio cron engine, keyed by owner_id.
- id: auto-incrementing primary key
- owner_id: Telegram user ID of the bot owner
- template: Bio template string with {time}, {mood}, {text} tokens
- mood: Current mood value
- custom_text: Freeform text token value
- is_active: Whether the bio cron is running
- last_bio: Last rendered bio string (for deduplication)
- updated_at: Last update timestamp

### 3. bot_logs
Structured log entries for the bot's activity.
- id: auto-incrementing primary key
- owner_id: Telegram user ID of the bot owner
- level: Log level (INFO, WARN, ERROR)
- message: Log message text
- context: JSONB context data
- created_at: Timestamp of the log entry

## Security
- RLS enabled on all tables.
- The backend uses the service-role key which bypasses RLS for all writes.
- Only SELECT policies are granted to anon+authenticated (read-only dashboard).
- INSERT/UPDATE/DELETE are denied for anon+authenticated — all writes
  go through the backend's service-role key.

## Indexes
- saved_items.owner_id for filtered queries
- saved_items.save_code for lookups
- bio_state.owner_id for singleton lookups
- bot_logs.owner_id + created_at for log queries and cleanup
*/

CREATE TABLE IF NOT EXISTS saved_items (
    id bigserial PRIMARY KEY,
    save_code text UNIQUE NOT NULL,
    save_type text NOT NULL DEFAULT 'forward',
    origin_chat_id bigint,
    origin_msg_id bigint,
    saved_chat_id bigint,
    saved_msg_id bigint,
    sender_name text,
    sender_id bigint,
    mime_type text,
    file_id text,
    file_size bigint,
    media_type text,
    tags text[] DEFAULT '{}',
    caption text,
    owner_id bigint NOT NULL,
    created_at timestamptz DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_saved_items_owner ON saved_items (owner_id);
CREATE INDEX IF NOT EXISTS idx_saved_items_save_code ON saved_items (save_code);
CREATE INDEX IF NOT EXISTS idx_saved_items_created_at ON saved_items (created_at DESC);

ALTER TABLE saved_items ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "anon_select_saved_items" ON saved_items;
DROP POLICY IF EXISTS "anon_insert_saved_items" ON saved_items;
DROP POLICY IF EXISTS "anon_update_saved_items" ON saved_items;
DROP POLICY IF EXISTS "anon_delete_saved_items" ON saved_items;

CREATE POLICY "anon_select_saved_items" ON saved_items FOR SELECT
    TO anon, authenticated USING (true);


CREATE TABLE IF NOT EXISTS bio_state (
    id bigserial PRIMARY KEY,
    owner_id bigint UNIQUE NOT NULL,
    template text NOT NULL DEFAULT '🕒 {time} | 💭 {mood}',
    mood text NOT NULL DEFAULT '😊',
    custom_text text NOT NULL DEFAULT '',
    is_active boolean NOT NULL DEFAULT false,
    last_bio text NOT NULL DEFAULT '',
    updated_at timestamptz DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_bio_state_owner ON bio_state (owner_id);

ALTER TABLE bio_state ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "anon_select_bio_state" ON bio_state;
DROP POLICY IF EXISTS "anon_insert_bio_state" ON bio_state;
DROP POLICY IF EXISTS "anon_update_bio_state" ON bio_state;
DROP POLICY IF EXISTS "anon_delete_bio_state" ON bio_state;

CREATE POLICY "anon_select_bio_state" ON bio_state FOR SELECT
    TO anon, authenticated USING (true);


CREATE TABLE IF NOT EXISTS bot_logs (
    id bigserial PRIMARY KEY,
    owner_id bigint NOT NULL,
    level text NOT NULL DEFAULT 'INFO',
    message text NOT NULL,
    context jsonb DEFAULT '{}',
    created_at timestamptz DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_bot_logs_owner ON bot_logs (owner_id);
CREATE INDEX IF NOT EXISTS idx_bot_logs_created_at ON bot_logs (created_at DESC);

ALTER TABLE bot_logs ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "anon_select_bot_logs" ON bot_logs;
DROP POLICY IF EXISTS "anon_insert_bot_logs" ON bot_logs;
DROP POLICY IF EXISTS "anon_delete_bot_logs" ON bot_logs;

CREATE POLICY "anon_select_bot_logs" ON bot_logs FOR SELECT
    TO anon, authenticated USING (true);
