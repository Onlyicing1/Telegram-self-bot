-- ============================================================================
-- LifeOS / Telegram Self-Bot — Canonical Supabase Bootstrap & Reconciliation
--
-- Generated from a repository-wide database contract audit of commit
-- 30bb3a426c2ec419be9d8f43373d85ce27d77099 (origin/main).
--
-- Properties
--   * Establishes the COMPLETE database state required by CURRENT code:
--     13 code-active tables + 1 legacy table (ghost_chats, preserved).
--   * Safe on a FRESH database; idempotent/reconciling on an existing one
--     (IF NOT EXISTS / DO-guarded constraints / ON CONFLICT DO NOTHING).
--   * Additive only: no DROP TABLE, no DROP COLUMN, no data destruction.
--     The only intentional drops are anon WRITE policies that contradict
--     the documented SELECT-only dashboard boundary.
--   * Security model: ALL writes use the service-role key (bypasses RLS);
--     anon + authenticated get SELECT-only (read-only dashboard).
--   * No functions/triggers/RPC endpoints are created — the codebase has
--     zero .rpc() calls.
--   * Historical application data (saved items, AI sessions/messages/
--     memories/usage/stats, logs, allow-list values) is intentionally NOT
--     fabricated. Only deterministic, project-defined seed rows are inserted.
-- ============================================================================

BEGIN;

-- ─── 1. Extensions ──────────────────────────────────────────────────────────
-- pg_trgm powers the saved_items trigram indexes used by the
-- db_client.search_saves caption/save_code/mime_type ILIKE paths.
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- ─── 2. saved_items ─────────────────────────────────────────────────────────
-- Writer: backend/db/client.py (insert/query/list/search/delete/count/stats)
-- Payload: backend/services/save_service.py::execute_save
-- save_type has NO CHECK: 20260714111706 supersedes the ('forward','deep')
-- CHECK from 20260712234229.
-- file_name / short_code are legacy columns (no live writer) kept additively.
CREATE TABLE IF NOT EXISTS saved_items (
    id              bigserial    PRIMARY KEY,
    save_code       text         NOT NULL UNIQUE,
    save_type       text         NOT NULL DEFAULT 'forward',
    origin_chat_id  bigint,
    origin_msg_id   bigint,
    saved_chat_id   bigint,
    saved_msg_id    bigint,
    sender_name     text,
    sender_id       bigint,
    mime_type       text,
    file_id         text,
    file_size       bigint,
    media_type      text,
    tags            text[]       DEFAULT '{}',
    caption         text,
    file_name       text,
    short_code      text,
    owner_id        bigint       NOT NULL,
    created_at      timestamptz  DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_saved_items_owner            ON saved_items (owner_id);
CREATE INDEX IF NOT EXISTS idx_saved_items_save_code        ON saved_items (save_code);
CREATE INDEX IF NOT EXISTS idx_saved_items_created_at       ON saved_items (created_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_saved_items_short_code
    ON saved_items (short_code) WHERE short_code IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_saved_items_owner_created    ON saved_items (owner_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_saved_items_caption_trgm     ON saved_items USING gin (caption gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_saved_items_file_name_trgm   ON saved_items USING gin (file_name gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_saved_items_save_code_trgm   ON saved_items USING gin (save_code gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_saved_items_short_code_trgm  ON saved_items USING gin (short_code gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_saved_items_mime_trgm        ON saved_items USING gin (mime_type gin_trgm_ops);

ALTER TABLE saved_items ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "anon_insert_saved_items" ON saved_items;
DROP POLICY IF EXISTS "anon_update_saved_items" ON saved_items;
DROP POLICY IF EXISTS "anon_delete_saved_items" ON saved_items;
DROP POLICY IF EXISTS "anon_select_saved_items" ON saved_items;
CREATE POLICY "anon_select_saved_items" ON saved_items FOR SELECT
    TO anon, authenticated USING (true);

-- ─── 3. bio_state ───────────────────────────────────────────────────────────
-- Writer/reader: backend/db/client.py (bio state + Bio ProfileEngine)
CREATE TABLE IF NOT EXISTS bio_state (
    id           bigserial    PRIMARY KEY,
    owner_id     bigint       NOT NULL UNIQUE,
    template     text         NOT NULL DEFAULT '🕒 {time} | 💭 {mood}',
    mood         text         NOT NULL DEFAULT '😊',
    custom_text  text         NOT NULL DEFAULT '',
    is_active    boolean      NOT NULL DEFAULT false,
    last_bio     text         NOT NULL DEFAULT '',
    updated_at   timestamptz  DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_bio_state_owner ON bio_state (owner_id);

ALTER TABLE bio_state ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "anon_insert_bio_state" ON bio_state;
DROP POLICY IF EXISTS "anon_update_bio_state" ON bio_state;
DROP POLICY IF EXISTS "anon_delete_bio_state" ON bio_state;
DROP POLICY IF EXISTS "anon_select_bio_state" ON bio_state;
CREATE POLICY "anon_select_bio_state" ON bio_state FOR SELECT
    TO anon, authenticated USING (true);

-- ─── 4. username_state ──────────────────────────────────────────────────────
-- Writer/reader: backend/db/client.py (username state + Username ProfileEngine)
CREATE TABLE IF NOT EXISTS username_state (
    id           bigserial    PRIMARY KEY,
    owner_id     bigint       NOT NULL UNIQUE,
    template     text         NOT NULL DEFAULT '{time} | {mood}',
    mood         text         NOT NULL DEFAULT '😊',
    custom_text  text         NOT NULL DEFAULT '',
    is_active    boolean      NOT NULL DEFAULT false,
    last_name    text         NOT NULL DEFAULT '',
    updated_at   timestamptz  DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_username_state_owner ON username_state (owner_id);

ALTER TABLE username_state ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "anon_insert_username_state" ON username_state;
DROP POLICY IF EXISTS "anon_update_username_state" ON username_state;
DROP POLICY IF EXISTS "anon_delete_username_state" ON username_state;
DROP POLICY IF EXISTS "anon_select_username_state" ON username_state;
CREATE POLICY "anon_select_username_state" ON username_state FOR SELECT
    TO anon, authenticated USING (true);

-- ─── 5. bot_logs ────────────────────────────────────────────────────────────
-- Writer/reader: backend/db/client.py log/list_logs/count_logs/clean_logs
-- (also backend/runtime/startup_check.py, dormant).
-- level has NO CHECK: 20260714111706 supersedes the ('INFO','WARN','ERROR')
-- CHECK from 20260712234229.
CREATE TABLE IF NOT EXISTS bot_logs (
    id         bigserial    PRIMARY KEY,
    owner_id   bigint       NOT NULL,
    level      text         NOT NULL DEFAULT 'INFO',
    message    text         NOT NULL,
    context    jsonb        DEFAULT '{}',
    created_at timestamptz  DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_bot_logs_owner      ON bot_logs (owner_id);
CREATE INDEX IF NOT EXISTS idx_bot_logs_created_at ON bot_logs (created_at DESC);

ALTER TABLE bot_logs ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "anon_insert_bot_logs" ON bot_logs;
DROP POLICY IF EXISTS "anon_update_bot_logs" ON bot_logs;
DROP POLICY IF EXISTS "anon_delete_bot_logs" ON bot_logs;
DROP POLICY IF EXISTS "anon_select_bot_logs" ON bot_logs;
CREATE POLICY "anon_select_bot_logs" ON bot_logs FOR SELECT
    TO anon, authenticated USING (true);

-- ─── 6. panel_settings ──────────────────────────────────────────────────────
-- Sole accessor: backend/services/panel_settings_repository.py (key='global').
-- Column set = the 12 settings_service._DEFAULTS settings + the two
-- legacy-orphan columns (update_stale_seconds, ghost_seen_retention_seconds)
-- preserved additively. CHECK ranges mirror settings_service._VALIDATORS.
CREATE TABLE IF NOT EXISTS panel_settings (
    key                          text        PRIMARY KEY,
    auto_close_enabled           boolean     NOT NULL DEFAULT true,
    auto_close_delay             integer     NOT NULL DEFAULT 120,
    max_deep_save_mb             integer     NOT NULL DEFAULT 50,
    delete_batch_size            integer     NOT NULL DEFAULT 100,
    log_retention_days           integer     NOT NULL DEFAULT 7,
    panel_timeout_seconds        integer     NOT NULL DEFAULT 300,
    allow_multiple_panels        boolean     NOT NULL DEFAULT false,
    reuse_existing_panel         boolean     NOT NULL DEFAULT true,
    language                     text        NOT NULL DEFAULT 'en',
    debug_callbacks              boolean     NOT NULL DEFAULT false,
    owner_only                   boolean     NOT NULL DEFAULT true,
    dashboard_font               text        NOT NULL DEFAULT 'default',
    update_stale_seconds         integer     NOT NULL DEFAULT 300,
    ghost_seen_retention_seconds bigint      NOT NULL DEFAULT 2592000,
    updated_at                   timestamptz DEFAULT now()
);

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'panel_settings_auto_close_delay_check') THEN
        ALTER TABLE panel_settings ADD CONSTRAINT panel_settings_auto_close_delay_check
            CHECK (auto_close_delay BETWEEN 5 AND 3600);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'panel_settings_max_deep_save_mb_check') THEN
        ALTER TABLE panel_settings ADD CONSTRAINT panel_settings_max_deep_save_mb_check
            CHECK (max_deep_save_mb BETWEEN 1 AND 500);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'panel_settings_delete_batch_size_check') THEN
        ALTER TABLE panel_settings ADD CONSTRAINT panel_settings_delete_batch_size_check
            CHECK (delete_batch_size BETWEEN 1 AND 1000);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'panel_settings_log_retention_days_check') THEN
        ALTER TABLE panel_settings ADD CONSTRAINT panel_settings_log_retention_days_check
            CHECK (log_retention_days BETWEEN 1 AND 365);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'panel_settings_panel_timeout_seconds_check') THEN
        ALTER TABLE panel_settings ADD CONSTRAINT panel_settings_panel_timeout_seconds_check
            CHECK (panel_timeout_seconds BETWEEN 30 AND 86400);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'panel_settings_language_check') THEN
        ALTER TABLE panel_settings ADD CONSTRAINT panel_settings_language_check
            CHECK (length(btrim(language)) > 0);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'panel_settings_dashboard_font_check') THEN
        ALTER TABLE panel_settings ADD CONSTRAINT panel_settings_dashboard_font_check
            CHECK (dashboard_font IN (
                'default', 'serif_bold', 'serif_italic', 'serif_bold_italic',
                'sans', 'sans_bold', 'sans_italic', 'sans_bold_italic',
                'script', 'script_bold', 'fraktur', 'fraktur_bold',
                'double_struck', 'mono', 'small_caps', 'circled',
                'circled_dark', 'fullwidth', 'parenthesized', 'underline',
                'strikethrough', 'overline', 'wavy_underline'
            ));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'panel_settings_ghost_seen_retention_seconds_check') THEN
        ALTER TABLE panel_settings ADD CONSTRAINT panel_settings_ghost_seen_retention_seconds_check
            CHECK (ghost_seen_retention_seconds = 0
                   OR ghost_seen_retention_seconds BETWEEN 300 AND 31536000);
    END IF;
END $$;

-- Required singleton row: repository updates target key='global' and
-- silently no-op without it.
INSERT INTO panel_settings (key) VALUES ('global')
ON CONFLICT (key) DO NOTHING;

ALTER TABLE panel_settings ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "anon_insert_panel_settings" ON panel_settings;
DROP POLICY IF EXISTS "anon_update_panel_settings" ON panel_settings;
DROP POLICY IF EXISTS "anon_delete_panel_settings" ON panel_settings;
DROP POLICY IF EXISTS "anon_select_panel_settings" ON panel_settings;
CREATE POLICY "anon_select_panel_settings" ON panel_settings FOR SELECT
    TO anon, authenticated USING (true);

-- ─── 7. bot_settings ────────────────────────────────────────────────────────
-- Live consumer: backend/services/ghost_seen_v2.py (ghost_seen_allowed_chats
-- KV row, created at runtime — deliberately NOT seeded here).
-- The five seed rows below are legacy defaults from migration 20260729213959,
-- consumed by no current reader; retained verbatim for migration fidelity.
CREATE TABLE IF NOT EXISTS bot_settings (
    key         text        PRIMARY KEY,
    value       text        NOT NULL,
    value_type  text        NOT NULL DEFAULT 'str',
    updated_at  timestamptz DEFAULT now()
);

INSERT INTO bot_settings (key, value, value_type) VALUES
    ('auto_close_enabled', 'true', 'bool'),
    ('panel_auto_close_seconds', '120', 'int'),
    ('max_deep_save_mb', '50', 'int'),
    ('delete_batch_size', '100', 'int'),
    ('log_cleanup_days', '7', 'int')
ON CONFLICT (key) DO NOTHING;

ALTER TABLE bot_settings ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "anon_insert_bot_settings" ON bot_settings;
DROP POLICY IF EXISTS "anon_update_bot_settings" ON bot_settings;
DROP POLICY IF EXISTS "anon_delete_bot_settings" ON bot_settings;
DROP POLICY IF EXISTS "anon_select_bot_settings" ON bot_settings;
CREATE POLICY "anon_select_bot_settings" ON bot_settings FOR SELECT
    TO anon, authenticated USING (true);

-- ─── 8. ai_config ───────────────────────────────────────────────────────────
-- Writer/reader: backend/ai/config_store.py (get/save/record_request).
-- trigger_en / trigger_fa are nullable text; the writer normalizes empty
-- strings to NULL. UNIQUE(owner_id) backs the writer's select-then-
-- insert/update flow.
CREATE TABLE IF NOT EXISTS ai_config (
    id              bigserial    PRIMARY KEY,
    owner_id        bigint       NOT NULL UNIQUE,
    provider        text         DEFAULT '',
    model           text         DEFAULT '',
    temperature     real         DEFAULT 1.0,
    max_tokens      integer      DEFAULT 4096,
    system_prompt   text         DEFAULT '',
    history_budget  integer      DEFAULT 4000,
    is_configured   boolean      DEFAULT false,
    trigger_en      text         DEFAULT NULL,
    trigger_fa      text         DEFAULT NULL,
    last_request_at timestamptz,
    last_latency_ms real         DEFAULT 0,
    created_at      timestamptz  DEFAULT now(),
    updated_at      timestamptz  DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_ai_config_owner ON ai_config (owner_id);

ALTER TABLE ai_config ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "anon_insert_ai_config" ON ai_config;
DROP POLICY IF EXISTS "anon_update_ai_config" ON ai_config;
DROP POLICY IF EXISTS "anon_delete_ai_config" ON ai_config;
DROP POLICY IF EXISTS "anon_select_ai_config" ON ai_config;
CREATE POLICY "anon_select_ai_config" ON ai_config FOR SELECT
    TO anon, authenticated USING (true);

-- ─── 9. ai_sessions ─────────────────────────────────────────────────────────
-- Writer/reader: backend/ai/persistence.py (create/update/get_session).
CREATE TABLE IF NOT EXISTS ai_sessions (
    id            bigserial    PRIMARY KEY,
    session_id    text         NOT NULL UNIQUE,
    owner_id      bigint       NOT NULL,
    provider      text         DEFAULT '',
    model         text         DEFAULT '',
    status        text         DEFAULT 'active',
    total_tokens  integer      DEFAULT 0,
    message_count integer      DEFAULT 0,
    created_at    timestamptz  DEFAULT now(),
    updated_at    timestamptz  DEFAULT now()
);

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ai_sessions_status_check') THEN
        ALTER TABLE ai_sessions ADD CONSTRAINT ai_sessions_status_check
            CHECK (status IN ('active', 'completed', 'error', 'closed'));
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_ai_sessions_owner      ON ai_sessions (owner_id);
CREATE INDEX IF NOT EXISTS idx_ai_sessions_session_id ON ai_sessions (session_id);

ALTER TABLE ai_sessions ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "anon_insert_ai_sessions" ON ai_sessions;
DROP POLICY IF EXISTS "anon_update_ai_sessions" ON ai_sessions;
DROP POLICY IF EXISTS "anon_delete_ai_sessions" ON ai_sessions;
DROP POLICY IF EXISTS "anon_select_ai_sessions" ON ai_sessions;
CREATE POLICY "anon_select_ai_sessions" ON ai_sessions FOR SELECT
    TO anon, authenticated USING (true);

-- ─── 10. ai_messages ────────────────────────────────────────────────────────
-- Writer/reader: backend/ai/persistence.py (add_message/get_messages).
CREATE TABLE IF NOT EXISTS ai_messages (
    id          bigserial    PRIMARY KEY,
    session_id  text         NOT NULL,
    owner_id    bigint       NOT NULL,
    role        text         NOT NULL DEFAULT 'user',
    content     text         NOT NULL DEFAULT '',
    token_count integer      DEFAULT 0,
    provider    text         DEFAULT '',
    model       text         DEFAULT '',
    created_at  timestamptz  DEFAULT now()
);

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ai_messages_role_check') THEN
        ALTER TABLE ai_messages ADD CONSTRAINT ai_messages_role_check
            CHECK (role IN ('system', 'user', 'assistant', 'tool'));
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_ai_messages_session ON ai_messages (session_id);
CREATE INDEX IF NOT EXISTS idx_ai_messages_owner   ON ai_messages (owner_id);
CREATE INDEX IF NOT EXISTS idx_ai_messages_created ON ai_messages (created_at DESC);

ALTER TABLE ai_messages ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "anon_insert_ai_messages" ON ai_messages;
DROP POLICY IF EXISTS "anon_update_ai_messages" ON ai_messages;
DROP POLICY IF EXISTS "anon_delete_ai_messages" ON ai_messages;
DROP POLICY IF EXISTS "anon_select_ai_messages" ON ai_messages;
CREATE POLICY "anon_select_ai_messages" ON ai_messages FOR SELECT
    TO anon, authenticated USING (true);

-- ─── 11. ai_memories ────────────────────────────────────────────────────────
-- Writer/reader: backend/ai/persistence.py
-- (save/query/delete_expired/delete/count_memories).
CREATE TABLE IF NOT EXISTS ai_memories (
    id          bigserial    PRIMARY KEY,
    owner_id    bigint       NOT NULL,
    tier        text         NOT NULL DEFAULT 'long',
    category    text         NOT NULL DEFAULT 'context',
    content     text         NOT NULL,
    importance  real         DEFAULT 0.5,
    expires_at  timestamptz,
    metadata    jsonb        DEFAULT '{}',
    created_at  timestamptz  DEFAULT now()
);

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ai_memories_tier_check') THEN
        ALTER TABLE ai_memories ADD CONSTRAINT ai_memories_tier_check
            CHECK (tier IN ('short', 'long', 'permanent'));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ai_memories_category_check') THEN
        ALTER TABLE ai_memories ADD CONSTRAINT ai_memories_category_check
            CHECK (category IN ('fact', 'preference', 'context', 'summary', 'instruction'));
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_ai_memories_owner      ON ai_memories (owner_id);
CREATE INDEX IF NOT EXISTS idx_ai_memories_tier       ON ai_memories (tier);
CREATE INDEX IF NOT EXISTS idx_ai_memories_owner_tier ON ai_memories (owner_id, tier);

ALTER TABLE ai_memories ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "anon_insert_ai_memories" ON ai_memories;
DROP POLICY IF EXISTS "anon_update_ai_memories" ON ai_memories;
DROP POLICY IF EXISTS "anon_delete_ai_memories" ON ai_memories;
DROP POLICY IF EXISTS "anon_select_ai_memories" ON ai_memories;
CREATE POLICY "anon_select_ai_memories" ON ai_memories FOR SELECT
    TO anon, authenticated USING (true);

-- ─── 12. ai_tool_history ────────────────────────────────────────────────────
-- Writer: backend/ai/persistence.py::record_tool_call (INSERT only).
-- result_data is migration-defined but never inserted by current code
-- (legacy-preserved column).
CREATE TABLE IF NOT EXISTS ai_tool_history (
    id             bigserial    PRIMARY KEY,
    owner_id       bigint       NOT NULL,
    session_id     text         DEFAULT '',
    tool_name      text         NOT NULL,
    arguments      jsonb        DEFAULT '{}',
    result_success boolean      DEFAULT false,
    result_message text         DEFAULT '',
    result_data    jsonb        DEFAULT '{}',
    latency_ms     real         DEFAULT 0,
    created_at     timestamptz  DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_ai_tool_history_owner   ON ai_tool_history (owner_id);
CREATE INDEX IF NOT EXISTS idx_ai_tool_history_created ON ai_tool_history (created_at DESC);

ALTER TABLE ai_tool_history ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "anon_insert_ai_tool_history" ON ai_tool_history;
DROP POLICY IF EXISTS "anon_update_ai_tool_history" ON ai_tool_history;
DROP POLICY IF EXISTS "anon_delete_ai_tool_history" ON ai_tool_history;
DROP POLICY IF EXISTS "anon_select_ai_tool_history" ON ai_tool_history;
CREATE POLICY "anon_select_ai_tool_history" ON ai_tool_history FOR SELECT
    TO anon, authenticated USING (true);

-- ─── 13. ai_usage ───────────────────────────────────────────────────────────
-- Writer/reader: SupabaseUsageRepository
-- (backend/ai/database/usage_repository.py).
-- id is NOT sent by the writer (bigserial); token_source carries the honesty
-- label verbatim (actual / estimated / unavailable).
CREATE TABLE IF NOT EXISTS ai_usage (
    id                bigserial    PRIMARY KEY,
    owner_id          bigint       NOT NULL,
    session_id        text,
    provider          text,
    model             text,
    prompt_tokens     integer      DEFAULT 0,
    completion_tokens integer      DEFAULT 0,
    total_tokens      integer      DEFAULT 0,
    latency_ms        real         DEFAULT 0,
    token_source      text         DEFAULT NULL,
    created_at        timestamptz  DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_ai_usage_owner      ON ai_usage (owner_id);
CREATE INDEX IF NOT EXISTS idx_ai_usage_created_at ON ai_usage (created_at);

ALTER TABLE ai_usage ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "anon_insert_ai_usage" ON ai_usage;
DROP POLICY IF EXISTS "anon_update_ai_usage" ON ai_usage;
DROP POLICY IF EXISTS "anon_delete_ai_usage" ON ai_usage;
DROP POLICY IF EXISTS "anon_select_ai_usage" ON ai_usage;
CREATE POLICY "anon_select_ai_usage" ON ai_usage FOR SELECT
    TO anon, authenticated USING (true);

-- ─── 14. ai_provider_stats ──────────────────────────────────────────────────
-- Writer/reader: SupabaseProviderStatsRepository
-- (backend/ai/database/provider_stats_repository.py) which upserts with
-- on_conflict="provider_name,owner_id" — the composite PRIMARY KEY below IS
-- that conflict target.
CREATE TABLE IF NOT EXISTS ai_provider_stats (
    provider_name            text        NOT NULL,
    owner_id                 bigint      NOT NULL DEFAULT 0,
    total_requests           integer     NOT NULL DEFAULT 0,
    successful_requests      integer     NOT NULL DEFAULT 0,
    failed_requests          integer     NOT NULL DEFAULT 0,
    total_prompt_tokens      integer     NOT NULL DEFAULT 0,
    total_completion_tokens  integer     NOT NULL DEFAULT 0,
    avg_latency_ms           real        NOT NULL DEFAULT 0,
    last_request_at          timestamptz,
    updated_at               timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (provider_name, owner_id)
);

ALTER TABLE ai_provider_stats ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "anon_insert_ai_provider_stats" ON ai_provider_stats;
DROP POLICY IF EXISTS "anon_update_ai_provider_stats" ON ai_provider_stats;
DROP POLICY IF EXISTS "anon_delete_ai_provider_stats" ON ai_provider_stats;
DROP POLICY IF EXISTS "anon_select_ai_provider_stats" ON ai_provider_stats;
CREATE POLICY "anon_select_ai_provider_stats" ON ai_provider_stats FOR SELECT
    TO anon, authenticated USING (true);

-- ─── 15. ghost_chats (LEGACY — preserved, owner-gated removal) ──────────────
-- Zero .table("ghost_chats") references in current backend/tests/src code;
-- the live Ghost Seen allow-list is the bot_settings KV row. The table is
-- kept additively per the no-destruction rule; dropping it remains an owner
-- decision gated on a live-data check.
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
DROP POLICY IF EXISTS "anon_insert_ghost_chats" ON ghost_chats;
DROP POLICY IF EXISTS "anon_update_ghost_chats" ON ghost_chats;
DROP POLICY IF EXISTS "anon_delete_ghost_chats" ON ghost_chats;
DROP POLICY IF EXISTS "anon_select_ghost_chats" ON ghost_chats;
CREATE POLICY "anon_select_ghost_chats" ON ghost_chats FOR SELECT
    TO anon, authenticated USING (true);

COMMIT;
