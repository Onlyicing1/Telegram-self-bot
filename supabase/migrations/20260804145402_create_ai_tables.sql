/*
# Create AI conversation persistence tables

1. New Tables
- `ai_sessions` — One row per AI conversation session per owner. Stores session ID, owner ID, provider, model, status, token counts, and timestamps. Persists across restarts.
- `ai_messages` — Individual messages within a session. Stores role (user/assistant/system/tool), content, token count, provider, model, and timestamp. Enables conversation history recovery after restart.
- `ai_memories` — Three-tier memory system (short/long/permanent). Stores owner ID, tier, category, content, importance score, expiration, and metadata. Used for context injection into prompts.
- `ai_tool_history` — Audit log of every tool the AI called. Stores tool name, arguments, result, success status, and latency.

2. Security
- RLS enabled on all tables.
- SELECT-only granted to anon + authenticated (read-only dashboard access).
- All writes go through the backend's service-role key which bypasses RLS.

3. Important Notes
- These tables are independent (no foreign keys) to match the existing schema pattern.
- owner_id is a bigint matching Telegram user IDs (same as existing tables).
- All tables use bigserial primary keys for compatibility with existing patterns.
- Idempotent: uses IF NOT EXISTS so re-running is safe.
*/

-- AI Sessions table
CREATE TABLE IF NOT EXISTS ai_sessions (
    id              bigserial    PRIMARY KEY,
    session_id      text         NOT NULL UNIQUE,
    owner_id        bigint       NOT NULL,
    provider        text         DEFAULT '',
    model           text         DEFAULT '',
    status          text         DEFAULT 'active'
                                CHECK (status IN ('active', 'completed', 'error', 'closed')),
    total_tokens    integer      DEFAULT 0,
    message_count   integer      DEFAULT 0,
    created_at      timestamptz  DEFAULT now(),
    updated_at      timestamptz  DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_ai_sessions_owner ON ai_sessions (owner_id);
CREATE INDEX IF NOT EXISTS idx_ai_sessions_session_id ON ai_sessions (session_id);

ALTER TABLE ai_sessions ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "anon_select_ai_sessions" ON ai_sessions;
CREATE POLICY "anon_select_ai_sessions" ON ai_sessions FOR SELECT
    TO anon, authenticated USING (true);

-- AI Messages table
CREATE TABLE IF NOT EXISTS ai_messages (
    id              bigserial    PRIMARY KEY,
    session_id      text         NOT NULL,
    owner_id        bigint       NOT NULL,
    role            text         NOT NULL DEFAULT 'user'
                                CHECK (role IN ('system', 'user', 'assistant', 'tool')),
    content         text         NOT NULL DEFAULT '',
    token_count     integer      DEFAULT 0,
    provider        text         DEFAULT '',
    model           text         DEFAULT '',
    created_at      timestamptz  DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_ai_messages_session ON ai_messages (session_id);
CREATE INDEX IF NOT EXISTS idx_ai_messages_owner ON ai_messages (owner_id);
CREATE INDEX IF NOT EXISTS idx_ai_messages_created ON ai_messages (created_at DESC);

ALTER TABLE ai_messages ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "anon_select_ai_messages" ON ai_messages;
CREATE POLICY "anon_select_ai_messages" ON ai_messages FOR SELECT
    TO anon, authenticated USING (true);

-- AI Memories table (three-tier memory system)
CREATE TABLE IF NOT EXISTS ai_memories (
    id              bigserial    PRIMARY KEY,
    owner_id        bigint       NOT NULL,
    tier            text         NOT NULL DEFAULT 'long'
                                CHECK (tier IN ('short', 'long', 'permanent')),
    category        text         NOT NULL DEFAULT 'context'
                                CHECK (category IN ('fact', 'preference', 'context', 'summary', 'instruction')),
    content         text         NOT NULL,
    importance      real         DEFAULT 0.5,
    expires_at      timestamptz,
    metadata        jsonb        DEFAULT '{}',
    created_at      timestamptz  DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_ai_memories_owner ON ai_memories (owner_id);
CREATE INDEX IF NOT EXISTS idx_ai_memories_tier ON ai_memories (tier);
CREATE INDEX IF NOT EXISTS idx_ai_memories_owner_tier ON ai_memories (owner_id, tier);

ALTER TABLE ai_memories ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "anon_select_ai_memories" ON ai_memories;
CREATE POLICY "anon_select_ai_memories" ON ai_memories FOR SELECT
    TO anon, authenticated USING (true);

-- AI Tool History table
CREATE TABLE IF NOT EXISTS ai_tool_history (
    id              bigserial    PRIMARY KEY,
    owner_id        bigint       NOT NULL,
    session_id      text         DEFAULT '',
    tool_name       text         NOT NULL,
    arguments       jsonb        DEFAULT '{}',
    result_success  boolean      DEFAULT false,
    result_message  text         DEFAULT '',
    result_data     jsonb        DEFAULT '{}',
    latency_ms      real         DEFAULT 0,
    created_at      timestamptz  DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_ai_tool_history_owner ON ai_tool_history (owner_id);
CREATE INDEX IF NOT EXISTS idx_ai_tool_history_created ON ai_tool_history (created_at DESC);

ALTER TABLE ai_tool_history ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "anon_select_ai_tool_history" ON ai_tool_history;
CREATE POLICY "anon_select_ai_tool_history" ON ai_tool_history FOR SELECT
    TO anon, authenticated USING (true);
