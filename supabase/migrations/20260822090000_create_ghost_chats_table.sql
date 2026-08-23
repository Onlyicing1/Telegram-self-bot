-- Migration: CREATE ghost_chats table for Ghost Seen feature
-- Doc-first: see DATABASE_ARCHITECTURE.md §22 Ghost Seen
-- Idempotent: safe to re-run

CREATE TABLE IF NOT EXISTS ghost_chats (
    chat_id         bigint       PRIMARY KEY,
    display_name    text         NOT NULL DEFAULT '',
    last_preview    text         NOT NULL DEFAULT '',
    last_message_at timestamptz,
    unread_count    integer      NOT NULL DEFAULT 0,
    created_at      timestamptz  DEFAULT now(),
    updated_at      timestamptz  DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_ghost_chats_last_message
    ON ghost_chats (last_message_at DESC);

ALTER TABLE ghost_chats ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "anon_select_ghost_chats" ON ghost_chats;
CREATE POLICY "anon_select_ghost_chats" ON ghost_chats FOR SELECT
    TO anon, authenticated USING (true);