-- saved_items.sql — Media save records (forward + deep saves)
-- Part of the LifeOS Telegram Self-Bot database schema.
-- See DATABASE_ARCHITECTURE.md for full documentation.

CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE TABLE IF NOT EXISTS saved_items (
    id              bigserial    PRIMARY KEY,
    save_code       text         NOT NULL,
    save_type        text         NOT NULL DEFAULT 'forward'
                                 CHECK (save_type IN ('forward', 'deep')),
    origin_chat_id   bigint,
    origin_msg_id    bigint,
    saved_chat_id    bigint,
    saved_msg_id     bigint,
    sender_name      text,
    sender_id        bigint,
    mime_type        text,
    file_id          text,
    file_size        bigint,
    media_type       text,
    tags             text[]       DEFAULT '{}',
    caption          text,
    file_name        text,
    short_code       text,
    owner_id         bigint       NOT NULL,
    created_at       timestamptz  DEFAULT now()
);

-- Unique constraints
ALTER TABLE saved_items
    DROP CONSTRAINT IF EXISTS saved_items_save_code_key;
ALTER TABLE saved_items
    ADD CONSTRAINT saved_items_save_code_key UNIQUE (save_code);

-- Unique partial index for short_code (nullable, unique when present)
DROP INDEX IF EXISTS idx_saved_items_short_code;
CREATE UNIQUE INDEX IF NOT EXISTS idx_saved_items_short_code
    ON saved_items (short_code) WHERE short_code IS NOT NULL;

-- Lookup indexes
CREATE INDEX IF NOT EXISTS idx_saved_items_owner
    ON saved_items (owner_id);

CREATE INDEX IF NOT EXISTS idx_saved_items_save_code
    ON saved_items (save_code);

CREATE INDEX IF NOT EXISTS idx_saved_items_created
    ON saved_items (created_at DESC);

CREATE INDEX IF NOT EXISTS idx_saved_items_created_at
    ON saved_items (created_at DESC);

-- Composite index for per-owner recent-item queries
CREATE INDEX IF NOT EXISTS idx_saved_items_owner_created
    ON saved_items (owner_id, created_at DESC);

-- Trigram indexes for full-text ILIKE search (used by .find)
CREATE INDEX IF NOT EXISTS idx_saved_items_caption_trgm
    ON saved_items USING gin (caption gin_trgm_ops);

CREATE INDEX IF NOT EXISTS idx_saved_items_file_name_trgm
    ON saved_items USING gin (file_name gin_trgm_ops);

CREATE INDEX IF NOT EXISTS idx_saved_items_save_code_trgm
    ON saved_items USING gin (save_code gin_trgm_ops);

CREATE INDEX IF NOT EXISTS idx_saved_items_short_code_trgm
    ON saved_items USING gin (short_code gin_trgm_ops);

CREATE INDEX IF NOT EXISTS idx_saved_items_mime_trgm
    ON saved_items USING gin (mime_type gin_trgm_ops);

-- RLS
ALTER TABLE saved_items ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "anon_select_saved_items" ON saved_items;
CREATE POLICY "anon_select_saved_items" ON saved_items FOR SELECT
    TO anon, authenticated USING (true);
