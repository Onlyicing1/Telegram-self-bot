-- bio_state.sql — Singleton-per-owner bio cron engine state
-- Part of the LifeOS Telegram Self-Bot database schema.
-- See DATABASE_ARCHITECTURE.md for full documentation.

CREATE TABLE IF NOT EXISTS bio_state (
    id           bigserial    PRIMARY KEY,
    owner_id     bigint       NOT NULL,
    template     text         NOT NULL DEFAULT '🕒 {time} | 💭 {mood}',
    mood         text         NOT NULL DEFAULT '😊',
    custom_text  text         NOT NULL DEFAULT '',
    is_active    boolean      NOT NULL DEFAULT false,
    last_bio     text         NOT NULL DEFAULT '',
    updated_at   timestamptz  DEFAULT now()
);

-- Unique constraint: one row per owner
ALTER TABLE bio_state
    DROP CONSTRAINT IF EXISTS bio_state_owner_id_key;
ALTER TABLE bio_state
    ADD CONSTRAINT bio_state_owner_id_key UNIQUE (owner_id);

-- Lookup index (redundant with unique constraint, but explicit)
CREATE INDEX IF NOT EXISTS idx_bio_state_owner
    ON bio_state (owner_id);

-- RLS
ALTER TABLE bio_state ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "anon_select_bio_state" ON bio_state;
CREATE POLICY "anon_select_bio_state" ON bio_state FOR SELECT
    TO anon, authenticated USING (true);
