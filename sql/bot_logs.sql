-- bot_logs.sql — Structured activity log
-- Part of the LifeOS Telegram Self-Bot database schema.
-- See DATABASE_ARCHITECTURE.md for full documentation.

CREATE TABLE IF NOT EXISTS bot_logs (
    id          bigserial    PRIMARY KEY,
    owner_id    bigint       NOT NULL,
    level       text         NOT NULL DEFAULT 'INFO'
                              CHECK (level IN ('INFO', 'WARN', 'ERROR')),
    message     text         NOT NULL,
    context     jsonb        DEFAULT '{}',
    created_at  timestamptz  DEFAULT now()
);

-- Lookup indexes
CREATE INDEX IF NOT EXISTS idx_bot_logs_owner
    ON bot_logs (owner_id);

CREATE INDEX IF NOT EXISTS idx_bot_logs_created
    ON bot_logs (created_at DESC);

CREATE INDEX IF NOT EXISTS idx_bot_logs_created_at
    ON bot_logs (created_at DESC);

-- RLS
ALTER TABLE bot_logs ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "anon_select_bot_logs" ON bot_logs;
CREATE POLICY "anon_select_bot_logs" ON bot_logs FOR SELECT
    TO anon, authenticated USING (true);

-- Note: a legacy UPDATE policy (anon_update_bot_logs) exists in the live
-- database from the initial migration. It is not part of the authoritative
-- schema and can be safely dropped:
-- DROP POLICY IF EXISTS "anon_update_bot_logs" ON bot_logs;
